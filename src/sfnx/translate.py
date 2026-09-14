"""Python expressions to JSONata, with the spelling chosen by the operand types."""

import ast
import difflib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace

from sfnx.diagnostics import CompileError
from sfnx.expressions import (
    ADD,
    AND,
    COMPARE,
    FUNCTIONS,
    MULTIPLY,
    OR,
    Expr,
    array,
    binary,
    call,
    conditional,
    expression,
    field,
    index,
    literal,
    negate,
    obj,
    uses,
)
from sfnx.integrations import HTTP_METHODS, Integration, ResourceError, integration
from sfnx.jsontypes import (
    ARRAY,
    BOOLEAN,
    CONTEXT,
    CONTEXT_OBJECTS,
    ERROR_OUTPUT,
    NULL,
    NUMBER,
    OBJECT,
    STRING,
    Type,
    exclude,
    of,
    restrict,
    union,
)
from sfnx.module import qualified

COMPARISONS: dict[type[ast.cmpop], str] = {
    ast.Eq: "=",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}

# isinstance classes and the $type() name of each.
CLASSES = {
    "str": STRING,
    "float": NUMBER,
    "int": NUMBER,
    "bool": BOOLEAN,
    "list": ARRAY,
    "dict": OBJECT,
}


def truth(value: Expr) -> Expr:
    """A JSON boolean with Python's truthiness. $boolean agrees with bool()
    except on a non-empty array whose members are all falsy, so a known array
    is counted instead, and a value that may be an array is tested for one when
    it is evaluated. A value of unknown type keeps $boolean."""
    if value.boolean:
        return value
    if value.type is None or ARRAY not in value.type.kinds:
        return call("boolean", [value], of(BOOLEAN), boolean=True)
    counted = binary(
        call("count", [value], of(NUMBER)),
        ">",
        literal(0),
        COMPARE,
        of(BOOLEAN),
        True,
    )
    if value.type.kind == ARRAY:
        return counted
    kind = call("type", [value], of(STRING))
    test = binary(kind, "=", literal("array"), COMPARE, of(BOOLEAN), True)
    otherwise = call("boolean", [value], of(BOOLEAN), boolean=True)
    return conditional(test, counted, otherwise, of(BOOLEAN))


def logical(value: Expr) -> Expr:
    """An operand of JSONata's and, or and $not, which cast it with $boolean."""
    if value.type and ARRAY in value.type.kinds:
        return truth(value)
    return value


# What to write instead of the expressions the language leaves out, by node.
EXPRESSIONS = {
    "Attribute": 'attributes are not supported; read a key with x["key"]',
    "Lambda": "lambda is not supported; define the function with def for parallel() "
    "or a map, or write the expression where it is used",
    "NamedExpr": "assignment expressions are not supported; assign the value on a "
    "line of its own first",
    "Set": "JSON has lists only; write a list: [a, b]",
    "Starred": "unpacking with * is not supported; write the items out",
    "Await": "await is not supported; call task() on a line of its own",
    "Yield": "yield is not supported; return a list",
    "YieldFrom": "yield from is not supported; return a list",
}

MAX_SECONDS = 99_999_999
TASK_OPTIONS = ("timeout", "heartbeat", "role")
# The names sfnx exports that a workflow calls or reads, so that one used
# without an import is told apart from an unknown name.
EXPORTS = frozenset(
    {"context", "task", "wait", "parallel", "inline_map", "distributed_map"}
)


@dataclass(frozen=True)
class StateCall:
    """A call that makes a state, such as task() or parallel(), found in a
    statement for the statement to add. name is the state name for a call on a
    line of its own; state holds its fields up to Retry, which resolves its
    error classes against the module."""

    node: ast.Call
    name: str
    state: dict[str, object]
    retry: ast.expr | None


# Builds the state of a call whose functions compile to scopes of their own,
# parallel() and the maps, giving its fields and the type of its result.
Compose = Callable[[ast.Call, str], tuple[dict[str, object], Type | None]]

COMPOSED = {"parallel": "Parallel", "inline_map": "Map", "distributed_map": "Map"}


class Translator:
    """Translate expressions against the variables bound where they appear.

    names maps imported names to what they import, so a call to sfnx.wait can be
    told apart; partial holds variables assigned on some paths to here only.
    """

    def __init__(
        self,
        bindings: dict[str, Expr],
        names: dict[str, str],
        partial: set[str],
        compose: Compose,
    ):
        self.bindings = bindings
        self.names = names
        self.partial = partial
        # A statement that can become a Task lets one task() in; the call is
        # kept here. Inside a branch of an expression it would not always run.
        self.accepts_task = False
        self.task: StateCall | None = None
        self.compose = compose
        # The function of a distributed_map being compiled, which reads nothing
        # from outside its parameters.
        self.isolated: str | None = None
        self.local: set[str] = set()
        self.conditional = 0
        self.comprehending = 0
        # Whether the arguments of a .waitForTaskToken task are being
        # translated, and whether they read the task token.
        self.token_readable = False
        self.token_read = False
        # Loop variables of loops that have ended.
        self.expired: set[str] = set()
        # Whether a name is a function defined for parallel() or a map.
        self.is_function: Callable[[str], bool] = lambda name: False

    def statement_value(self, node: ast.expr) -> tuple[Expr, StateCall | None]:
        """The value of an assignment, a return or an expression statement,
        with the task() it calls, if any."""
        self.accepts_task = True
        try:
            value = self.expr(node)
            call = self.task
        finally:
            # A statement that fails to translate leaves no task() behind for
            # the next attempt.
            self.accepts_task = False
            self.task = None
        return value, call

    @contextmanager
    def branch(self) -> Iterator[None]:
        self.conditional += 1
        try:
            yield
        finally:
            self.conditional -= 1

    def expr(self, node: ast.expr) -> Expr:
        if isinstance(node, ast.Constant):
            return self.constant(node)
        if isinstance(node, ast.Name):
            if node.id in self.bindings:
                return self.bindings[node.id]
            if self.names.get(node.id) == "sfnx.context":
                return expression("$states.context", type=CONTEXT)
            self.check_import(node)
            if node.id in self.expired:
                raise CompileError(
                    f"{node.id} is the loop variable and ends with the loop; "
                    "assign it to another variable inside the loop",
                    node,
                )
            if node.id in self.partial:
                raise CompileError(
                    f"{node.id} is not assigned on every path to here; assign it "
                    "before the if, loop or try, or on every path",
                    node,
                )
            if self.isolated is not None and node.id not in self.local:
                raise CompileError(
                    f"{node.id} is outside {self.isolated}, which distributed_map runs "
                    "as a child execution that cannot read it; pass it with "
                    f'args={{"{node.id}": {node.id}}}',
                    node,
                )
            raise CompileError(
                f"{node.id} is not assigned here; assign it before this line", node
            )
        if (
            isinstance(node, ast.Attribute)
            and qualified(node, self.names) == "sfnx.context"
        ):
            return expression("$states.context", type=CONTEXT)
        if isinstance(node, ast.List):
            return array([self.expr(item) for item in node.elts])
        if isinstance(node, ast.Tuple):
            raise CompileError("JSON has no tuples; write a list: [a, b]", node)
        if isinstance(node, ast.Dict):
            return self.mapping(node)
        if isinstance(node, ast.Subscript):
            return self.subscript(node)
        if isinstance(node, ast.UnaryOp):
            return self.unary(node)
        if isinstance(node, ast.BinOp):
            return self.arithmetic(node)
        if isinstance(node, ast.BoolOp):
            return self.boolean(node)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.IfExp):
            test = self.condition(node.test)
            when, unless = self.narrowing(node.test)
            with self.narrowed(when), self.branch():
                then = self.expr(node.body)
            with self.narrowed(unless), self.branch():
                otherwise = self.expr(node.orelse)
            return conditional(
                test,
                then,
                otherwise,
                union(then.type, otherwise.type),
            )
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.ListComp):
            return self.comprehension(node)
        if isinstance(node, ast.JoinedStr):
            return self.formatted(node)
        if isinstance(node, (ast.SetComp, ast.GeneratorExp)):
            raise CompileError(
                "JSON has lists only; write a list comprehension: [x for x in xs]",
                node,
            )
        if isinstance(node, ast.DictComp):
            raise CompileError(
                "dict comprehensions are not supported; write the dict with its keys, "
                'such as {"id": x}, or build it in a Lambda task',
                node,
            )
        name = type(node).__name__
        raise CompileError(
            EXPRESSIONS.get(name, f"{name} expressions are not supported"), node
        )

    def comprehension(self, node: ast.ListComp) -> Expr:
        """[f(x) for x in xs if c] as $map and $filter over xs, or over the keys
        of a dict. JSONata returns a single value for a one-item result and
        nothing for an empty one, so the result is wrapped in a list. Brackets
        would merge a single result that is a list, so a result that may hold
        lists is kept as an array with [] and appended to an empty one."""
        if len(node.generators) != 1:
            raise CompileError(
                "a comprehension takes one for; nest a for loop for more",
                node.generators[1],
            )
        generator = node.generators[0]
        if not isinstance(generator.target, ast.Name):
            raise CompileError(
                "a comprehension iterates one variable: [x for x in xs]",
                generator.target,
            )
        name = generator.target.id
        check_name(name, generator.target)
        source = self.expr(generator.iter)
        kind = self.known(
            generator.iter,
            source,
            "list",
            "a comprehension depends on what it iterates",
        )
        if kind == OBJECT:
            source = call("keys", [source], of(ARRAY, items=of(STRING)))
        elif kind != ARRAY:
            raise CompileError(
                f"{ast.unparse(generator.iter)} is a {kind}; a comprehension "
                "iterates lists and the keys of dicts",
                generator.iter,
            )
        item = source.type.items if source.type else None
        saved = self.bindings.get(name)
        # The parameter of the JSONata function, not a Step Functions variable:
        # a variable of the same name that another binding reads would be hidden.
        self.bindings[name] = expression("$" + name, type=item)
        self.comprehending += 1
        try:
            tests = []
            narrowed: dict[str, Type] = {}
            for test in generator.ifs:
                with self.narrowed(narrowed):
                    tests.append(self.condition(test))
                    when, _ = self.narrowing(test)
                narrowed = {**narrowed, **when}
            with self.narrowed(narrowed):
                element = self.expr(node.elt)
        finally:
            self.comprehending -= 1
            if saved is None:
                del self.bindings[name]
            else:
                self.bindings[name] = saved
        if name in element.variables | uses(tests):
            raise CompileError(
                f"{name} is a variable that this comprehension reads through another "
                f"name, which its own {name} would hide; choose another name for it",
                generator.target,
            )
        result = source
        if tests:
            test = tests[0]
            for more in tests[1:]:
                test = binary(test, "and", more, AND, of(BOOLEAN), True)
            result = call("filter", [source, function(name, test)], source.type)
        mapped = not (isinstance(node.elt, ast.Name) and node.elt.id == name)
        if mapped:
            if tests and may_be_list(item):
                # $map would iterate the items of a single list $filter kept.
                result = expression(result.code + "[]", result.variables)
            result = call("map", [result, function(name, element)], None)
        else:
            element = replace(element, type=item)
        # The functions leave out their parameter; what the source reads stays,
        # even a variable of the same name. A list written out is a list already.
        if result.constructor:
            code = result.code
        elif (tests or mapped) and may_be_list(element.type):
            code = "$append([], " + result.code + "[])"
        else:
            code = "[" + result.code + "]"
        return expression(
            code,
            result.variables,
            type=of(ARRAY, items=element.type),
            constructor=True,
        )

    def formatted(self, node: ast.JoinedStr) -> Expr:
        """An f-string as the pieces joined with &, each value through $string
        unless it is known to be a string."""
        pieces = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                pieces.append(literal(value.value))
                continue
            assert isinstance(value, ast.FormattedValue)
            if value.conversion != -1:
                raise CompileError(
                    "conversions such as !r and = are not supported; write {x}", value
                )
            if value.format_spec is not None:
                raise CompileError(
                    "format specs are not supported; JSONata formats numbers "
                    "differently, so build the text from the number",
                    value.format_spec,
                )
            part = self.expr(value.value)
            if part.type is None or part.type.kinds != {STRING}:
                part = text(part)
            pieces.append(part)
        if not pieces:
            return literal("")
        result = pieces[0]
        for piece in pieces[1:]:
            result = binary(result, "&", piece, ADD, of(STRING))
        return result

    def condition(self, node: ast.expr) -> Expr:
        """A JSON boolean for if, while and the tests inside expressions.
        Comparisons and their and / or / not are used as they are."""
        if isinstance(node, ast.BoolOp):
            return self.junction(node, self.operands(node, self.logical))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.unary(node)
        return truth(self.expr(node))

    def logical(self, node: ast.expr) -> Expr:
        """An operand of and, or and not, which JSONata casts with $boolean."""
        if isinstance(node, ast.BoolOp):
            return self.condition(node)
        return self.expr(node)

    def constant(self, node: ast.Constant) -> Expr:
        if node.value is None or isinstance(node.value, (bool, int, float, str)):
            try:
                return literal(node.value)
            except ValueError as exc:
                raise CompileError(
                    "JSON numbers are finite; write a smaller number", node
                ) from exc
        raise CompileError(
            "only JSON values are supported: numbers, strings, True, False and None",
            node,
        )

    def mapping(self, node: ast.Dict) -> Expr:
        entries = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise CompileError(
                    "unpacking with ** is not supported; write the keys out, "
                    'such as {"id": x["id"]}',
                    value,
                )
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise CompileError(
                    "JSON object keys are strings; write the key in quotes",
                    key or value,
                )
            entries.append((key.value, self.expr(value)))
        return obj(entries)

    def known(self, node: ast.expr, value: Expr, hint: str, purpose: str) -> str:
        """The one type an operation depends on."""
        if value.type is None:
            parameter = value.code == "$states.context.Execution.Input"
            raise CompileError(unknown(node, hint, purpose, parameter), node)
        if value.type.kind is None:
            raise CompileError(several(node, value.type), node)
        return value.type.kind

    def numeric(self, node: ast.expr, operator: str) -> Expr:
        value = self.expr(node)
        if value.type is not None and value.type.kinds != {NUMBER}:
            if value.type.kind is None:
                raise CompileError(several(node, value.type), node)
            raise CompileError(
                f"{ast.unparse(node)} is a {value.type.kind}, and {operator} takes "
                f"numbers; convert it with float({ast.unparse(node)})",
                node,
            )
        return value

    def unary(self, node: ast.UnaryOp) -> Expr:
        if isinstance(node.op, ast.Not):
            operand = self.logical(node.operand)
            if operand.type and operand.type.kind == ARRAY:
                count = call("count", [operand], of(NUMBER))
                return binary(count, "=", literal(0), COMPARE, of(BOOLEAN), True)
            return call("not", [logical(operand)], of(BOOLEAN), boolean=True)
        if isinstance(node.op, ast.USub):
            operand = node.operand
            if (
                isinstance(operand, ast.Constant)
                and isinstance(operand.value, (int, float))
                and not isinstance(operand.value, bool)
            ):
                return literal(-operand.value)
            return negate(self.numeric(operand, "-"))
        if isinstance(node.op, ast.UAdd):
            raise CompileError(
                f"remove the unary +; write {ast.unparse(node.operand)}", node
            )
        raise CompileError(
            "JSONata has no bitwise operators; compute it in a Lambda task", node
        )

    def arithmetic(self, node: ast.BinOp) -> Expr:
        if isinstance(node.op, ast.Add):
            return self.add(node)
        symbol = {
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.Mod: "%",
            ast.FloorDiv: "//",
            ast.Pow: "**",
        }.get(type(node.op))
        if symbol is None:
            raise CompileError(
                "JSONata has no bitwise or matrix operators; compute it in a Lambda task",
                node,
            )
        left = self.numeric(node.left, symbol)
        right = self.numeric(node.right, symbol)
        number = of(NUMBER)
        if symbol in {"-", "*", "/"}:
            precedence = ADD if symbol == "-" else MULTIPLY
            return binary(left, symbol, right, precedence, number)
        if symbol == "**":
            return call("power", [left, right], number)
        quotient = call("floor", [binary(left, "/", right, MULTIPLY, number)], number)
        if symbol == "//":
            return quotient
        # Python's % takes the sign of the divisor; JSONata's takes the dividend's.
        product = binary(right, "*", quotient, MULTIPLY, number)
        return binary(left, "-", product, ADD, number)

    def add(self, node: ast.BinOp) -> Expr:
        left, right = self.expr(node.left), self.expr(node.right)
        kinds = set()
        for operand, value in ((node.left, left), (node.right, right)):
            if value.type is not None:
                if value.type.kind is None:
                    raise CompileError(several(operand, value.type), operand)
                kinds.add(value.type.kind)
        if not kinds:
            raise CompileError(
                unknown(node.left, "float", "+ adds numbers, joins strings or lists"),
                node.left,
            )
        if len(kinds) > 1:
            raise CompileError(
                f"+ cannot join {' and '.join(sorted(kinds))}; convert one side "
                "with str(...) or float(...)",
                node,
            )
        kind = kinds.pop()
        if kind == NUMBER:
            return binary(left, "+", right, ADD, of(NUMBER))
        if kind == STRING:
            return binary(left, "&", right, ADD, of(STRING))
        if kind == ARRAY:
            joined = union(left.type, right.type)
            items = joined.items if joined else None
            return call("append", [left, right], of(ARRAY, items=items))
        raise CompileError(f"+ takes numbers, strings or lists, not a {kind}", node)

    def operands(
        self, node: ast.BoolOp, translate: Callable[[ast.expr], Expr]
    ) -> list[Expr]:
        """The operands of and / or, each narrowed by the ones before it: the
        right side of `x is not None and x > 0` only runs when x is not None."""
        values = []
        narrowed: dict[str, Type] = {}
        for position, operand in enumerate(node.values):
            with self.narrowed(narrowed):
                if position:
                    with self.branch():
                        values.append(translate(operand))
                else:
                    values.append(translate(operand))
                when, unless = self.narrowing(operand)
            narrowed = {
                **narrowed,
                **(when if isinstance(node.op, ast.And) else unless),
            }
        return values

    def narrowed(self, types: dict[str, Type]) -> AbstractContextManager[None]:
        """Translate with variables retyped as a test proved them."""
        return narrowing_context(self.bindings, types)

    def narrowing(self, node: ast.expr) -> tuple[dict[str, Type], dict[str, Type]]:
        """The types a test proves for variables when it holds and when it
        does not."""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            when, unless = self.narrowing(node.operand)
            return unless, when
        if isinstance(node, ast.BoolOp):
            proven: dict[str, Type] = {}
            for operand in node.values:
                with self.narrowed(proven):
                    when, unless = self.narrowing(operand)
                proven = {
                    **proven,
                    **(when if isinstance(node.op, ast.And) else unless),
                }
            return (proven, {}) if isinstance(node.op, ast.And) else ({}, proven)
        subject, kinds = tested(node)
        if subject is None or subject not in self.bindings:
            return {}, {}
        declared = self.bindings[subject].type
        when = {subject: restrict(declared, kinds)}
        unless = {} if declared is None else {subject: exclude(declared, kinds)}
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.IsNot):
            return unless, when
        return when, unless

    def boolean(self, node: ast.BoolOp) -> Expr:
        values = self.operands(node, self.expr)
        if all(v.boolean for v in values):
            return self.junction(node, values)
        # As a value, `a or b` is a when a is truthy and b otherwise.
        result = values[-1]
        for value in reversed(values[:-1]):
            kind = union(value.type, result.type)
            if isinstance(node.op, ast.Or):
                result = conditional(truth(value), value, result, kind)
            else:
                result = conditional(truth(value), result, value, kind)
        return result

    def junction(self, node: ast.BoolOp, values: list[Expr]) -> Expr:
        operator, precedence = (
            ("or", OR) if isinstance(node.op, ast.Or) else ("and", AND)
        )
        result = logical(values[0])
        for value in values[1:]:
            result = binary(
                result, operator, logical(value), precedence, of(BOOLEAN), True
            )
        return result

    def compare(self, node: ast.Compare) -> Expr:
        tests = []
        left_node = node.left
        left = self.expr(left_node)
        for position, (operator, right_node) in enumerate(
            zip(node.ops, node.comparators, strict=True)
        ):
            if position:
                # a < b < c evaluates c only when a < b holds.
                with self.branch():
                    right = self.expr(right_node)
            else:
                right = self.expr(right_node)
            tests.append(self.comparison(operator, left_node, left, right_node, right))
            left_node, left = right_node, right
        result = tests[0]
        for test in tests[1:]:
            result = binary(result, "and", test, AND, of(BOOLEAN), True)
        return result

    def comparison(
        self,
        operator: ast.cmpop,
        left_node: ast.expr,
        left: Expr,
        right_node: ast.expr,
        right: Expr,
    ) -> Expr:
        boolean = of(BOOLEAN)
        if isinstance(operator, (ast.Eq, ast.NotEq)):
            return binary(
                left, COMPARISONS[type(operator)], right, COMPARE, boolean, True
            )
        if isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            kinds = set()
            for operand, value in ((left_node, left), (right_node, right)):
                if value.type is None:
                    continue
                if value.type.kind is None:
                    raise CompileError(several(operand, value.type), operand)
                if value.type.kind not in {NUMBER, STRING}:
                    raise CompileError(
                        f"{ast.unparse(operand)} is a {value.type.kind}; "
                        "JSONata orders only numbers and strings",
                        operand,
                    )
                kinds.add(value.type.kind)
            if len(kinds) > 1:
                raise CompileError(
                    "a number and a string cannot be ordered; convert one side "
                    "with str(...) or float(...)",
                    left_node,
                )
            symbol = COMPARISONS[type(operator)]
            return binary(left, symbol, right, COMPARE, boolean, True)
        if isinstance(operator, (ast.In, ast.NotIn)):
            test = self.membership(left_node, left, right_node, right)
            if isinstance(operator, ast.NotIn):
                return call("not", [test], boolean, boolean=True)
            return test
        if not (isinstance(right_node, ast.Constant) and right_node.value is None):
            raise CompileError(
                "is compares with None only; compare values with ==", right_node
            )
        present = binary(
            call("exists", [left], boolean, boolean=True),
            "and",
            binary(left, "!=", literal(None), COMPARE, boolean, True),
            AND,
            boolean,
            True,
        )
        if isinstance(operator, ast.IsNot):
            return present
        return call("not", [present], boolean, boolean=True)

    def membership(
        self, left_node: ast.expr, left: Expr, right_node: ast.expr, right: Expr
    ) -> Expr:
        boolean = of(BOOLEAN)
        literal_key = isinstance(left_node, ast.Constant) and isinstance(
            left_node.value, str
        )
        # A literal string asks for a key, as a literal string subscript does,
        # so `"coupon" in input` needs no annotation.
        if literal_key and right.type is None:
            kind = OBJECT
        else:
            kind = self.known(right_node, right, "list", "in depends on the container")
        if kind == ARRAY:
            return binary(left, "in", right, COMPARE, boolean, True)
        if kind == OBJECT:
            if isinstance(left_node, ast.Constant) and isinstance(left_node.value, str):
                return call("exists", [field(right, left_node.value)], boolean, True)
            member = call("lookup", [right, left], None)
            return call("exists", [member], boolean, boolean=True)
        if kind == STRING:
            return call("contains", [right, left], boolean, boolean=True)
        raise CompileError(
            f"{ast.unparse(right_node)} is a {kind}; in looks into lists, dicts "
            "and strings",
            right_node,
        )

    def subscript(self, node: ast.Subscript) -> Expr:
        value = self.expr(node.value)
        key = node.slice
        if isinstance(key, ast.Slice):
            raise CompileError(
                "slices are not supported; loop with range() over the positions "
                "you need: for i in range(1, len(xs)): x = xs[i]",
                key,
            )
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self.container(node.value, value, OBJECT, "string keys look into dicts")
            if value.type is not None and value.type in CONTEXT_OBJECTS:
                self.context_field(key, value.type, key.value)
            return field(value, key.value)
        position = self.expr(key)
        values = value.type.values if value.type else None
        key_kind = None
        if position.type is not None:
            key_kind = self.known(key, position, "str", "")
        if key_kind == STRING:
            self.container(node.value, value, OBJECT, "string keys look into dicts")
            return call("lookup", [value, position], values)
        if key_kind is None:
            kind = self.known(
                node.value, value, "list", "a variable key depends on the container"
            )
            if kind == OBJECT:
                return call("lookup", [value, position], values)
        elif key_kind != NUMBER:
            raise CompileError(
                f"{ast.unparse(key)} is a {key_kind}; "
                "keys are strings and positions are numbers",
                key,
            )
        if value.type is not None and value.type.kinds == {STRING}:
            return call("substring", [value, position, literal(1)], of(STRING))
        self.container(
            node.value, value, ARRAY, "positions look into lists and strings"
        )
        return index(value, position)

    def container(self, node: ast.expr, value: Expr, kind: str, rule: str) -> None:
        if value.type is not None and kind not in value.type.kinds:
            raise CompileError(
                f"{ast.unparse(node)} is a {value.type.describe()}; {rule}", node
            )

    def call(self, node: ast.Call) -> Expr:
        target = qualified(node.func, self.names) or ""
        if target == "sfnx.task":
            return self.task_call(node)
        if target.startswith("sfnx.") and target[5:] in COMPOSED:
            return self.composed_call(node, target[5:])
        if target.startswith("sfnx."):
            raise CompileError(
                f"{ast.unparse(node.func)}() makes a state and has no value; "
                "call it on its own line",
                node,
            )
        if not isinstance(node.func, ast.Name):
            raise CompileError(
                "methods are not supported; write the operation with operators "
                "or supported functions",
                node.func,
            )
        self.check_import(node.func)
        name = node.func.id
        if node.keywords:
            raise CompileError(f"{name}() takes no keyword arguments here", node)
        if name in {"len", "float", "int", "str", "bool"}:
            if len(node.args) != 1:
                raise CompileError(f"{name}() takes one argument: {name}(x)", node)
            argument = self.expr(node.args[0])
            if name == "float":
                return call("number", [argument], of(NUMBER))
            if name == "int":
                number = call("number", [argument], of(NUMBER))
                return call("floor", [number], of(NUMBER))
            if name == "str":
                return text(argument)
            if name == "bool":
                return truth(argument)
            return self.length(node.args[0], argument)
        if name == "isinstance":
            return self.isinstance(node)
        if name == "range":
            raise CompileError(
                "range() is only for for loops: for i in range(10)", node
            )
        if name in {"enumerate", "zip"}:
            raise CompileError(
                f"{name}() is not supported; count with range: "
                "for i in range(len(items)): item = items[i]",
                node,
            )
        if name in {"list", "dict"}:
            raise CompileError(
                f"{name}() does not convert here; declare the type instead: "
                f"x: {name} = ...",
                node,
            )
        if self.is_function(name):
            raise CompileError(
                f"{name}() cannot be called directly; a function runs as states "
                f"through parallel({name}) or a map, or write its body here",
                node,
            )
        raise CompileError(
            f"calling {name}() is not supported; write it with operators, or "
            "compute it in a Lambda task",
            node,
        )

    def check_import(self, node: ast.Name) -> None:
        """A name sfnx exports, used without importing it and not defined here."""
        if (
            node.id in EXPORTS
            and node.id not in self.names
            and not self.is_function(node.id)
        ):
            raise CompileError(
                f"{node.id} is not imported; write from sfnx import {node.id}", node
            )

    def task_call(self, node: ast.Call) -> Expr:
        """task(resource, arguments, timeout=, heartbeat=, role=). The value is
        $states.result, which the statement assigns or returns."""
        self.admit(node, "task", "Task")
        if not node.args or len(node.args) > 2:
            raise CompileError(
                "task takes a resource ARN and its arguments: "
                'task("arn:aws:states:::lambda:invoke", {"FunctionName": ...})',
                node,
            )
        resource_node = node.args[0]
        if not (
            isinstance(resource_node, ast.Constant)
            and isinstance(resource_node.value, str)
        ):
            raise CompileError(
                "the resource is a literal ARN string, as Step Functions requires",
                resource_node,
            )
        try:
            called = integration(resource_node.value)
        except ResourceError as exc:
            raise CompileError(str(exc), resource_node) from exc
        # Nothing inside task() makes a Task of its own.
        self.accepts_task = False
        try:
            arguments, options = self.task_arguments(node, called)
        finally:
            self.accepts_task = True
        state: dict[str, object] = {"Type": "Task", "Resource": resource_node.value}
        if arguments is not None:
            state["Arguments"] = arguments.template
        elif called.kind in {"sdk", "optimized"}:
            # Step Functions requires Arguments for service integrations.
            state["Arguments"] = {}
        fields = {"timeout": "TimeoutSeconds", "heartbeat": "HeartbeatSeconds"}
        for option, name in fields.items():
            if option in options:
                state[name] = options[option].template
        if "role" in options:
            state["Credentials"] = {"RoleArn": options["role"].template}
        self.task = StateCall(node, called.name, state, retry_option(node))
        return expression("$states.result", type=called.result)

    def admit(self, node: ast.Call, function: str, kind: str) -> None:
        """Whether a call that makes a state can be here."""
        if not self.accepts_task:
            raise CompileError(
                f"{function}() makes a {kind} state; call it on its own line or "
                f"assign its result: result = {function}(...)",
                node,
            )
        if self.comprehending:
            raise CompileError(
                f"{function}() in a comprehension would need a state per item; "
                "use inline_map or a for loop",
                node,
            )
        if self.conditional:
            raise CompileError(
                f"{function}() here would run whether or not this part is taken; "
                "call it inside an if statement",
                node,
            )
        if self.task is not None:
            raise CompileError(
                "one task() or parallel() per line; assign each result to its "
                "own variable",
                node,
            )

    def composed_call(self, node: ast.Call, function: str) -> Expr:
        """parallel() or a map: a state built from functions compiled as scopes
        of their own. The value is its result."""
        self.admit(node, function, COMPOSED[function])
        # Nothing in its arguments makes a state of its own.
        self.accepts_task = False
        try:
            state, result = self.compose(node, function)
        finally:
            self.accepts_task = True
        name = "parallel" if function == "parallel" else "map"
        self.task = StateCall(node, name, state, retry_option(node))
        return expression("$states.result", type=result)

    def task_arguments(
        self, node: ast.Call, called: Integration
    ) -> tuple[Expr | None, dict[str, Expr]]:
        arguments = None
        callback = called.pattern == ".waitForTaskToken"
        if len(node.args) == 2:
            self.token_readable, self.token_read = callback, False
            try:
                arguments = self.expr(node.args[1])
            finally:
                self.token_readable = False
            check_arguments(node.args[1], called)
        elif called.required:
            raise CompileError(
                f"{called.name} needs {', '.join(sorted(called.required))}: "
                "pass them as task(resource, {...})",
                node,
            )
        if callback and not self.token_read:
            raise CompileError(
                "a .waitForTaskToken task waits for its token to come back; pass "
                'context["Task"]["Token"] in the arguments',
                node,
            )
        options: dict[str, Expr] = {}
        for keyword in node.keywords:
            if keyword.arg == "retry":
                # The error classes resolve against the module, in the statement.
                continue
            if keyword.arg not in TASK_OPTIONS:
                raise CompileError(
                    "task takes timeout=, heartbeat=, role= and retry=; "
                    "put API parameters in the arguments dict",
                    keyword,
                )
            if keyword.arg == "role" and called.kind in {"activity", "http"}:
                raise CompileError(
                    "role= applies to Lambda functions and AWS service "
                    "integrations, not to activities or HTTP Tasks",
                    keyword,
                )
            options[keyword.arg] = self.expr(keyword.value)
        for name in ("timeout", "heartbeat"):
            if name in options:
                check_seconds(node, name, options[name])
        timeout = options.get("timeout")
        heartbeat = options.get("heartbeat")
        if (
            timeout is not None
            and heartbeat is not None
            and isinstance(timeout.template, int)
            and isinstance(heartbeat.template, int)
            and heartbeat.template >= timeout.template
        ):
            raise CompileError("heartbeat must be shorter than timeout", node)
        return arguments, options

    def context_field(self, key: ast.expr, parent: Type, name: str) -> None:
        """A key of the Context Object, and whether it can be read here."""
        known = [k for k, _ in parent.fields or ()]
        if name not in known:
            close = difflib.get_close_matches(name, known, n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            raise CompileError(
                f"the Context Object has no {name} here{hint} "
                f"(it has {', '.join(known)})",
                key,
            )
        if parent == CONTEXT and name == "Map":
            raise CompileError(
                "Map.Item is readable only where a Map selects its items; "
                "the function a map calls receives the item and its index",
                key,
            )
        if parent == CONTEXT and name == "Task":
            if not self.token_readable:
                raise CompileError(
                    "the task token exists only in the arguments of a "
                    ".waitForTaskToken task",
                    key,
                )
            self.token_read = True

    def length(self, node: ast.expr, value: Expr) -> Expr:
        kind = self.known(node, value, "list", "len depends on the type")
        number = of(NUMBER)
        if kind == ARRAY:
            return call("count", [value], number)
        if kind == STRING:
            return call("length", [value], number)
        if kind == OBJECT:
            return call("count", [call("keys", [value], of(ARRAY))], number)
        raise CompileError(
            f"{ast.unparse(node)} is a {kind}; len takes lists, strings and dicts",
            node,
        )

    def isinstance(self, node: ast.Call) -> Expr:
        if len(node.args) != 2:
            raise CompileError("isinstance takes a value and a class", node)
        value = self.expr(node.args[0])
        classes = node.args[1]
        items = classes.elts if isinstance(classes, ast.Tuple) else [classes]
        names: list[str] = []
        for item in items:
            if not (isinstance(item, ast.Name) and item.id in CLASSES):
                raise CompileError(
                    "isinstance takes str, float, int, bool, list, dict "
                    "or a tuple of them; test None with `is None`",
                    item,
                )
            if CLASSES[item.id] not in names:
                names.append(CLASSES[item.id])
        boolean = of(BOOLEAN)
        kind = call("type", [value], of(STRING))
        if len(names) == 1:
            return binary(kind, "=", literal(names[0]), COMPARE, boolean, True)
        choices = array([literal(n) for n in names])
        return binary(kind, "in", choices, COMPARE, boolean, True)


def unknown(node: ast.expr, hint: str, purpose: str, parameter: bool = False) -> str:
    text = ast.unparse(node)
    if parameter:
        return (
            f"{purpose}, so the type of {text} must be known; annotate the "
            f"parameter: def ...({text}: {hint})"
        )
    if isinstance(node, ast.Name):
        return (
            f"{purpose}, so the type of {text} must be known; annotate it where "
            f"it is assigned: {text}: {hint} = ..."
        )
    return (
        f"{purpose}, so the type of {text} must be known; assign it to an "
        f"annotated variable first: value: {hint} = {text}"
    )


def several(node: ast.expr, declared: Type) -> str:
    text = ast.unparse(node)
    return (
        f"{text} may be {declared.describe()}; narrow it first with "
        f"isinstance({text}, ...) or {text} is not None"
    )


def tested(node: ast.expr) -> tuple[str | None, frozenset[str]]:
    """The variable a narrowing test looks at, and the types it tests for:
    isinstance(x, str), x is None and x is not None. The test has already been
    translated, so an isinstance here has valid classes."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and isinstance(node.args[0], ast.Name)
    ):
        classes = node.args[1]
        items = classes.elts if isinstance(classes, ast.Tuple) else [classes]
        kinds = frozenset(CLASSES[i.id] for i in items if isinstance(i, ast.Name))
        return node.args[0].id, kinds
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.Is, ast.IsNot))
        and isinstance(node.left, ast.Name)
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    ):
        return node.left.id, frozenset({NULL})
    return None, frozenset()


@contextmanager
def narrowing_context(
    bindings: dict[str, Expr], types: dict[str, Type]
) -> Iterator[None]:
    saved = {name: bindings[name] for name in types}
    for name, declared in types.items():
        bindings[name] = replace(bindings[name], type=declared)
    try:
        yield
    finally:
        bindings.update(saved)


def check_arguments(node: ast.expr, called: Integration) -> None:
    """The keys of a literal arguments dict against the API."""
    if not isinstance(node, ast.Dict):
        return
    # The dict has been translated, so its keys are literal strings.
    keys: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str)
        keys[key.value] = value
        if called.allowed is not None and key.value not in called.allowed:
            close = difflib.get_close_matches(key.value, sorted(called.allowed), n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            raise CompileError(f"{called.name} has no argument {key.value}{hint}", key)
    missing = called.required - keys.keys()
    if missing:
        raise CompileError(
            f"{called.name} needs {', '.join(sorted(missing))} in the arguments", node
        )
    if called.kind == "http":
        if not {"Authentication", "InvocationConfig"} & keys.keys():
            raise CompileError(
                "an HTTP Task needs a connection: "
                '"InvocationConfig": {"ConnectionArn": ...}',
                node,
            )
        method = keys["Method"]
        if isinstance(method, ast.Constant) and method.value not in HTTP_METHODS:
            raise CompileError(
                f"Method is one of {', '.join(sorted(HTTP_METHODS))}", method
            )


def check_seconds(node: ast.Call, name: str, value: Expr) -> None:
    seconds = value.template
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        if not (isinstance(seconds, int) and 0 < seconds <= MAX_SECONDS):
            raise CompileError(
                f"{name} is whole seconds from 1 to {MAX_SECONDS:,}", node
            )
    elif value.type is not None and value.type.kinds != {NUMBER}:
        raise CompileError(
            f"{name} is a number of seconds, not a {value.type.describe()}", node
        )


def retry_option(node: ast.Call) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == "retry"), None)


def text(value: Expr) -> Expr:
    """str(x), and what an f-string or a raise makes of a value: the message
    of a caught exception, or the value through $string."""
    if value.type == ERROR_OUTPUT:
        return field(value, "Cause")
    return call("string", [value], of(STRING))


def may_be_list(kind: Type | None) -> bool:
    return kind is None or ARRAY in kind.kinds


def function(parameter: str, body: Expr) -> Expr:
    return expression(f"function(${parameter}) {{ {body.code} }}", body.variables)


def check_name(name: str, node: ast.AST) -> None:
    """A comprehension variable becomes a JSONata parameter, which hides a
    function or $states of the same name inside it."""
    if name == "states" or name in FUNCTIONS or name.startswith("_"):
        raise CompileError(
            f"{name} would hide a JSONata name inside the comprehension; "
            "choose another name",
            node,
        )
