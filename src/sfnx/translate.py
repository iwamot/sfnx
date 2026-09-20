"""Python expressions to JSONata, with the spelling chosen by the operand types."""

import ast
import difflib
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace

from sfnx.diagnostics import CompileError
from sfnx.expressions import (
    ADD,
    AND,
    ATOM,
    COMPARE,
    MULTIPLY,
    OR,
    WRITTEN,
    Expr,
    array,
    binary,
    block,
    call,
    changes,
    conditional,
    expression,
    field,
    index,
    literal,
    negate,
    obj,
    spelling,
    string,
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
    AnnotationError,
    Type,
    annotation,
    exclude,
    of,
    restrict,
    union,
)
from sfnx.module import Constant, data, holds, qualified

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

# The whitespace at both ends of a string, which str.strip() removes. $trim
# removes it too, but also makes every run of whitespace inside the text one
# space, which strip() does not.
OUTER_WHITESPACE = expression(r"/^\s+|\s+$/")

MAX_SECONDS = 99_999_999
TASK_OPTIONS = ("timeout", "heartbeat", "role")
# The names sfnx exports that a workflow calls or reads, so that one used
# without an import is told apart from an unknown name.
EXPORTS = frozenset(
    {"context", "task", "wait", "parallel", "inline_map", "distributed_map", "jsonata"}
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


@dataclass(frozen=True)
class Bound:
    """A slice bound: amount is a position from the start, or with back, the
    count back from the end that a minus sign in the source wrote."""

    amount: Expr
    back: bool


# Builds the state of a call whose functions compile to scopes of their own,
# parallel() and the maps, giving its fields and the type of its result.
Compose = Callable[[ast.Call, str], tuple[dict[str, object], Type | None]]

COMPOSED = {"parallel": "Parallel", "inline_map": "Map", "distributed_map": "Map"}

# Every function of JSONata 2.0.6 and those Step Functions adds, which a value
# bound for jsonata() would hide.
JSONATA_FUNCTIONS = frozenset(
    [
        "string",
        "length",
        "substring",
        "substringBefore",
        "substringAfter",
        "uppercase",
        "lowercase",
        "trim",
        "pad",
        "contains",
        "split",
        "join",
        "match",
        "replace",
        "eval",
        "base64encode",
        "base64decode",
        "encodeUrlComponent",
        "encodeUrl",
        "decodeUrlComponent",
        "decodeUrl",
        "formatNumber",
        "formatBase",
        "formatInteger",
        "parseInteger",
        "number",
        "abs",
        "floor",
        "ceil",
        "round",
        "power",
        "sqrt",
        "random",
        "sum",
        "max",
        "min",
        "average",
        "boolean",
        "not",
        "exists",
        "count",
        "append",
        "sort",
        "reverse",
        "shuffle",
        "distinct",
        "zip",
        "keys",
        "lookup",
        "spread",
        "merge",
        "sift",
        "each",
        "error",
        "assert",
        "type",
        "now",
        "millis",
        "fromMillis",
        "toMillis",
        "map",
        "filter",
        "single",
        "reduce",
        "partition",
        "range",
        "hash",
        "uuid",
        "parse",
    ]
)

# How the built-in functions for numbers are written.
NUMBER_FUNCTIONS = {
    "abs": "abs(x)",
    "round": "round(x) or round(x, digits)",
    "sum": "sum(xs)",
    "max": "max(xs) or max(a, b, ...)",
    "min": "min(xs) or min(a, b, ...)",
}

# Functions of math and random and the JSONata function of each.
MATH_FUNCTIONS = {
    "math.floor": "floor",
    "math.ceil": "ceil",
    "math.sqrt": "sqrt",
    "random.random": "random",
}

# The hashlib functions whose hexdigest() is $hash, and the name of each
# algorithm there.
HASHES = {
    "hashlib.md5": "MD5",
    "hashlib.sha1": "SHA-1",
    "hashlib.sha256": "SHA-256",
    "hashlib.sha384": "SHA-384",
    "hashlib.sha512": "SHA-512",
}

# The calls that make a datetime, a value JSON does not have: str() and an
# f-string write one as the timestamp text it holds, and .timestamp() as the
# seconds since the epoch.
NOW = "datetime.datetime.now"
FROM_ISO = "datetime.datetime.fromisoformat"
FROM_TIMESTAMP = "datetime.datetime.fromtimestamp"
DATETIMES = (NOW, FROM_ISO, FROM_TIMESTAMP)

# Calls whose value is an object that JSON holds as text, so they are written
# in str() or an f-string, the datetimes also in .timestamp(): how many
# arguments the call takes, how it is written and what it returns in Python.
STRINGIFIED = {
    "uuid.uuid4": (0, "uuid.uuid4()", "a UUID object"),
    NOW: (0, "datetime.now()", "a datetime object"),
    FROM_ISO: (1, "datetime.fromisoformat(text)", "a datetime object"),
    FROM_TIMESTAMP: (1, "datetime.fromtimestamp(seconds)", "a datetime object"),
}

# The standard library functions sfnx compiles, by how a call to one reads
# without its import, and the import to write.
MODULE_IMPORTS = {
    "json.loads": "import json",
    "uuid.uuid4": "import uuid",
    "time.time": "import time",
    **dict.fromkeys(
        ("datetime.now", "datetime.fromisoformat", "datetime.fromtimestamp"),
        "from datetime import datetime",
    ),
    "itertools.batched": "import itertools",
    **dict.fromkeys(HASHES, "import hashlib"),
    **{target: f"import {target.partition('.')[0]}" for target in MATH_FUNCTIONS},
}

# The methods of str that compile to a JSONata function, and how each is written.
STRING_METHODS = {
    "split": "s.split(sep) or s.split()",
    "replace": "s.replace(old, new) or s.replace(old, new, count)",
    "lower": "s.lower()",
    "upper": "s.upper()",
    "join": "sep.join(items)",
    "startswith": "s.startswith(prefix)",
    "endswith": "s.endswith(suffix)",
    "ljust": "s.ljust(width) or s.ljust(width, fill)",
    "rjust": "s.rjust(width) or s.rjust(width, fill)",
    "strip": "s.strip()",
}

# The methods of dict that compile to a JSONata function, and how each is written.
DICT_METHODS = {
    "keys": "d.keys()",
    "values": "d.values()",
    "get": "d.get(key) or d.get(key, default)",
}

# The module functions sfnx does not compile that have one spelling here, by
# how a call to one reads, and what to write instead.
MODULE_REWRITES = {
    "json.dumps": "write str(x), the JSON text of a dict or a list",
    "math.pow": "write x ** y",
    "os.path.basename": 'write path.split("/")[-1]',
}

# The methods sfnx does not compile that have one spelling here, by the name of
# the method, and what to write instead.
METHOD_REWRITES = {
    "format": 'write an f-string, such as f"{n} items"',
    "append": "a list is a value here, so write xs = xs + [x]",
    "extend": "a list is a value here, so write xs = xs + ys",
    "insert": "a list is a value here, so write xs = xs[:i] + [x] + xs[i:]",
}

# The built-in functions sfnx does not compile that have one spelling here, by
# name, and what to write instead.
BUILTIN_REWRITES = {
    "any": 'count what matches: len([x for x in xs if x["failed"]]) > 0',
    "all": 'count what does not match: len([x for x in xs if not x["ok"]]) == 0',
    "map": "write a comprehension: [str(x) for x in xs]",
    "filter": "write a comprehension: [x for x in xs if x]",
}

# The conversions of % formatting that an f-string writes as a plain {x}. For
# the rest the message gives an example of an f-string rather than write one.
CONVERSIONS = frozenset({"s", "d", "i"})

# The built-in functions that give a loop or a comprehension two variables.
UNPACKING = frozenset({"enumerate", "zip"})

# The built-in functions that read every item of a generator expression given
# to them, so sum(x for x in xs) means what sum([x for x in xs]) means.
CONSUMERS = frozenset({"sum", "max", "min", "sorted", "list"})

# A variable as JSONata writes one, a function among them. An expression
# written by hand names variables the program never declared, so what a piece
# of code reads is found in the code itself. A name is spelled as Python and
# Step Functions both spell one, which is a Unicode identifier and not ASCII
# alone.
VARIABLE = re.compile(r"\$([^\W\d]\w*)")


class Translator:
    """Translate expressions against the variables bound where they appear.

    names maps imported names to what they import, so a call to sfnx.wait can be
    told apart; partial holds variables assigned on some paths to here only.
    """

    def __init__(
        self,
        bindings: dict[str, Expr],
        names: dict[str, str],
        spellings: dict[str, str],
        constants: dict[str, Constant],
        partial: set[str],
        compose: Compose,
        typed: Mapping[str, Type],
    ):
        self.bindings = bindings
        self.names = names
        self.spellings = spellings
        # The TypedDict classes of the module, which an annotation may name.
        self.typed = typed
        # What the module assigns outside the machine, less the names this
        # scope assigns, which are its own as they are in Python.
        self.constants = constants
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
        # The names JSONata binds inside the expression being translated, the
        # parameters of comprehensions and the variables of blocks, innermost
        # last. They are read as themselves, so nothing else may take them.
        self.inner: list[str] = []
        # Whether the arguments of a .waitForTaskToken task are being
        # translated, and whether they read the task token.
        self.token_readable = False
        self.token_read = False
        # Loop variables of loops that have ended.
        self.expired: set[str] = set()
        # Whether a name is a function defined for parallel() or a map.
        self.is_function: Callable[[str], bool] = lambda name: False

    def spelling(self, name: str) -> str:
        return spelling(name, self.spellings)

    def outside(self, node: ast.Name) -> Expr:
        """A name assigned outside the machine, read as the value written
        there: the compiler writes that value in where the name is used, so
        the name itself reaches neither the definition nor Step Functions."""
        found = self.constants[node.id]
        # The value is written outside the machine, where the variables of the
        # machine are not in scope, as they are not when Python runs the
        # module, and where the names it reads are those assigned above it.
        bindings = self.bindings
        self.bindings = {}
        constants = self.constants
        self.constants = found.scope
        try:
            value = self.expr(data(node.id, found.value, node))
        finally:
            self.constants = constants
            self.bindings = bindings
        if found.declared is None:
            return value
        try:
            return replace(value, type=annotation(found.declared, self.typed))
        except AnnotationError as exc:
            raise CompileError(str(exc), exc.node) from exc

    def holds(self, node: ast.expr) -> ast.expr:
        """The value a name assigned outside the machine holds, for the places
        that take what is written out, such as a resource ARN. A name the
        machine binds is itself, so its variable is read there instead."""
        if isinstance(node, ast.Name) and node.id in self.bindings:
            return node
        return holds(node, self.constants)

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
            # A name the machine does not assign is read outside it, as
            # Python reads a global.
            if node.id in self.constants:
                return self.outside(node)
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
            if unpacking(generator.iter):
                # Raise the advice of enumerate() or zip(), which says what to
                # count with, rather than the message below.
                self.expr(generator.iter)
            raise CompileError(
                "a comprehension iterates one variable: [x for x in xs]",
                generator.target,
            )
        name = generator.target.id
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
        self.bindings[name] = expression("$" + self.spelling(name), type=item)
        self.comprehending += 1
        self.inner.append(self.spelling(name))
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
            self.inner.pop()
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
            result = call(
                "filter", [source, function(self.spelling(name), test)], source.type
            )
        mapped = not (isinstance(node.elt, ast.Name) and node.elt.id == name)
        if mapped:
            if tests and may_be_list(item):
                # $map would iterate the items of a single list $filter kept.
                result = expression(
                    result.code + "[]", result.variables, volatile=result.volatile
                )
            result = call("map", [result, function(self.spelling(name), element)], None)
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
            volatile=result.volatile,
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
            spelled = self.stringified(value.value)
            if spelled is not None:
                pieces.append(spelled)
                continue
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

    def truth(self, value: Expr) -> Expr:
        """A JSON boolean with Python's truthiness. $boolean agrees with
        bool() except on a non-empty array whose members are all falsy, so a
        known array is counted instead, and a value that may be one, an
        unknown type included, is tested for one when it is evaluated.
        Declaring the type is what keeps the shorter $boolean. The test reads
        the value three times, so anything longer than a variable is bound to
        one first."""
        if value.boolean:
            return value
        if value.type is not None and ARRAY not in value.type.kinds:
            return call("boolean", [value], of(BOOLEAN), boolean=True)
        if value.type is not None and value.type.kind == ARRAY:
            return self.counted(value)
        with self.once([value], always=self.bind(value)) as (bindings, (bound,)):
            kind = call("type", [bound], of(STRING))
            test = binary(kind, "=", literal("array"), COMPARE, of(BOOLEAN), True)
            otherwise = call("boolean", [bound], of(BOOLEAN), boolean=True)
            chosen = conditional(test, self.counted(bound), otherwise, of(BOOLEAN))
        return block(bindings, chosen)

    def counted(self, value: Expr) -> Expr:
        """An array is truthy when it holds anything, whatever the items are."""
        return binary(
            call("count", [value], of(NUMBER)),
            ">",
            literal(0),
            COMPARE,
            of(BOOLEAN),
            True,
        )

    def cast(self, value: Expr) -> Expr:
        """An operand of JSONata's and, or and $not, which cast it with
        $boolean. A value that may be an array, an unknown type included, is
        read as truth() reads it, so that and, or and not agree with if."""
        if value.type is None or ARRAY in value.type.kinds:
            return self.truth(value)
        return value

    def condition(self, node: ast.expr) -> Expr:
        """A JSON boolean for if, while and the tests inside expressions.
        Comparisons and their and / or / not are used as they are."""
        if isinstance(node, ast.BoolOp):
            return self.junction(node, self.operands(node, self.logical))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.unary(node)
        return self.truth(self.expr(node))

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
        """A dict literal. With ** it is $merge of the unpacked dicts and the
        runs of keys between them, where a later key wins."""
        parts: list[Expr] = []
        entries: list[tuple[str, Expr]] = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                if entries:
                    parts.append(obj(entries))
                    entries = []
                parts.append(self.unpacked(value))
                continue
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise CompileError(
                    "JSON object keys are strings; write the key in quotes", key
                )
            entries.append((key.value, self.expr(value)))
        if not parts:
            return obj(entries)
        if entries:
            parts.append(obj(entries))
        elif len(parts) == 1:
            # A JSON value is a copy already.
            return replace(parts[0], type=parts[0].type or of(OBJECT))
        listed = expression(
            "[" + ", ".join(p.code for p in parts) + "]",
            uses(parts),
            volatile=changes(parts),
        )
        return call("merge", [listed], of(OBJECT))

    def unpacked(self, node: ast.expr) -> Expr:
        value = self.expr(node)
        if value.type is not None and value.type.kinds != {OBJECT}:
            if value.type.kind is None:
                raise CompileError(several(node, value.type), node)
            raise CompileError(
                f"{ast.unparse(node)} is a {value.type.kind}; ** unpacks dicts", node
            )
        if value.type is not None:
            return value
        # Python raises for anything but a dict. An array constructor merges
        # the items of a list, so $merge would take a list of dicts as the
        # dicts themselves, and ** of a lone value would pass it through.
        with self.once([value], always=self.bind(value)) as (bindings, (bound,)):
            kind = call("type", [bound], of(STRING))
            test = binary(kind, "=", literal("object"), COMPARE, of(BOOLEAN), True)
            raised = call("error", [literal("** unpacks dicts")], of(OBJECT))
            checked = conditional(test, bound, raised, of(OBJECT))
        return block(bindings, checked)

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
            return call("not", [self.cast(operand)], of(BOOLEAN), boolean=True)
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
        if symbol == "/" and self.is_average(node):
            assert isinstance(node.left, ast.Call)
            numbers = self.numbers(node.left.args[0], "sum")
            return call("average", [numbers], of(NUMBER))
        if (
            symbol == "%"
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            # A string on the left is formatting, not the remainder, and the
            # message about numbers would send the writer to convert it.
            raise CompileError(formatting(node.left.value, node.right), node)
        left = self.numeric(node.left, symbol)
        right = self.numeric(node.right, symbol)
        number = of(NUMBER)
        if symbol in {"-", "*"}:
            precedence = ADD if symbol == "-" else MULTIPLY
            return binary(left, symbol, right, precedence, number)
        if symbol == "**":
            return call("power", [left, right], number)
        if written_number(right) == 0:
            raise CompileError(
                f"dividing by {ast.unparse(node.right)} fails every time; "
                "divide by a value that is not zero",
                node.right,
            )
        if symbol in {"/", "//"}:
            # The divisor is written again in the test that divided() puts
            # around the division.
            with self.once([right], [left]) as (bindings, (right,)):
                quotient = binary(left, "/", right, MULTIPLY, number)
                if symbol == "//":
                    quotient = call("floor", [quotient], number)
                return block(bindings, divided(right, quotient))
        assert symbol == "%"
        # Python's % takes the sign of the divisor; JSONata's takes the dividend's.
        # Both sides are written twice, and the divisor once more in the test.
        with self.once([left, right]) as (bindings, (left, right)):
            quotient = call(
                "floor", [binary(left, "/", right, MULTIPLY, number)], number
            )
            product = binary(right, "*", quotient, MULTIPLY, number)
            remainder = binary(left, "-", product, ADD, number)
            return block(bindings, divided(right, remainder))

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
        # As a value, `a or b` is a when a is truthy and b otherwise, which
        # writes a twice.
        result = values[-1]
        for value in reversed(values[:-1]):
            kind = union(value.type, result.type)
            with self.once([value], [result], always=self.bind(value)) as (
                bindings,
                (value,),
            ):
                if isinstance(node.op, ast.Or):
                    chosen = conditional(self.truth(value), value, result, kind)
                else:
                    chosen = conditional(self.truth(value), result, value, kind)
            result = block(bindings, chosen)
        return result

    def junction(self, node: ast.BoolOp, values: list[Expr]) -> Expr:
        operator, precedence = (
            ("or", OR) if isinstance(node.op, ast.Or) else ("and", AND)
        )
        result = self.cast(values[0])
        for value in values[1:]:
            result = binary(
                result, operator, self.cast(value), precedence, of(BOOLEAN), True
            )
        return result

    def compare(self, node: ast.Compare) -> Expr:
        left = self.expr(node.left)
        rights = []
        for position, right_node in enumerate(node.comparators):
            if position:
                # a < b < c evaluates c only when a < b holds.
                with self.branch():
                    rights.append(self.expr(right_node))
            else:
                rights.append(self.expr(right_node))
        return self.chain(node, 0, left, rights)

    def chain(
        self, node: ast.Compare, first: int, left: Expr, rights: list[Expr]
    ) -> Expr:
        """The comparisons of node from the one at first on, joined with and;
        left is the left side of the first and rights are the right sides of
        all of them. a < b < c is a < b and b < c, which writes b twice, so a
        b that changes on evaluation is bound first, in a block that holds the
        comparisons from b on: a < b and ($v := c; b < $v and $v < d)."""
        tests = []
        for position in range(first, len(node.ops)):
            left_node = node.left if position == 0 else node.comparators[position - 1]
            right_node, right = node.comparators[position], rights[position]
            if right.volatile and position < len(node.ops) - 1:
                with self.once([right], [left, *rights]) as (bindings, (right,)):
                    replaced = [*rights]
                    replaced[position] = right
                    rest = self.chain(node, position, left, replaced)
                tests.append(block(bindings, rest))
                break
            tests.append(
                self.comparison(node.ops[position], left_node, left, right_node, right)
            )
            left = right
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
        # left is written twice: it exists, and is not null.
        with self.once([left]) as (bindings, (tested,)):
            present = binary(
                call("exists", [tested], boolean, boolean=True),
                "and",
                binary(tested, "!=", literal(None), COMPARE, boolean, True),
                AND,
                boolean,
                True,
            )
        present = block(bindings, present)
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
            return self.slice(node.value, value, key)
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

    def slice(self, node: ast.expr, value: Expr, key: ast.Slice) -> Expr:
        """xs[a:b] and s[a:b]. A bound written with a minus sign counts back
        from the end; any other bound is a position from the start."""
        kind = self.known(node, value, "list", "a slice depends on the type")
        if kind not in {ARRAY, STRING}:
            raise CompileError(
                f"{ast.unparse(node)} is a {kind}; slices take lists and strings", node
            )
        if key.step is not None:
            if (
                key.lower is None
                and key.upper is None
                and ast.unparse(key.step) == "-1"
            ):
                return reversed_value(value, kind)
            raise CompileError(
                "a slice takes no step other than xs[::-1]; loop with range() over "
                "the positions you need: for i in range(0, len(xs), 2): x = xs[i]",
                key.step,
            )
        lower = self.bound(key.lower)
        upper = self.bound(key.upper)
        if kind == STRING:
            return self.substring(value, lower, upper)
        size = call("count", [value], of(NUMBER))
        reads = [value, *(b.amount for b in (lower, upper) if b)]
        position = self.parameter("i", reads)
        at = expression("$" + position, type=of(NUMBER))
        tests = []
        if lower is not None and (lower.back or written(lower) != 0):
            start = offset(size, lower)
            tests.append(binary(at, ">=", start, COMPARE, of(BOOLEAN), True))
        if upper is not None:
            end = offset(size, upper)
            tests.append(binary(at, "<", end, COMPARE, of(BOOLEAN), True))
        if not tests:
            # A JSON value is a copy already.
            return value
        test = tests[0]
        for more in tests[1:]:
            test = binary(test, "and", more, AND, of(BOOLEAN), True)
        item = value.type.items if value.type else None
        predicate = expression(
            f"function(${self.parameter('v', [value, test])}, ${position}) "
            f"{{ {test.code} }}",
            test.variables,
            volatile=test.volatile,
        )
        kept = call("filter", [value, predicate], value.type)
        code = f"$append([], {kept.code}[])" if may_be_list(item) else f"[{kept.code}]"
        return expression(
            code,
            kept.variables,
            type=of(ARRAY, items=item),
            constructor=True,
            volatile=kept.volatile,
        )

    def substring(self, text: Expr, lower: Bound | None, upper: Bound | None) -> Expr:
        """s[a:b] as $substring, which counts a negative start from the end,
        with its length always given: without one, Step Functions fails at
        most starts on text with characters outside the Basic Multilingual
        Plane."""
        if lower is None and upper is None:
            return text
        length = call("length", [text], of(NUMBER))
        start = lower or Bound(literal(0), back=False)
        if upper is None:
            size = start.amount if start.back else length
        elif start.back == upper.back:
            # From the end, s[-a:-b] holds a - b characters.
            size = (
                difference(start.amount, upper.amount)
                if start.back
                else difference(upper.amount, start.amount)
            )
        elif upper.back:
            size = difference(length, sum_of(upper.amount, start.amount))
        else:
            size = difference(sum_of(upper.amount, start.amount), length)
        return call("substring", [text, signed(start), size], of(STRING))

    def bound(self, node: ast.expr | None) -> Bound | None:
        """A slice bound, a number; a minus sign written before it counts back
        from the end."""
        if node is None:
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return Bound(self.numeric(node.operand, "a slice"), back=True)
        return Bound(self.numeric(node, "a slice"), back=False)

    def parameter(self, base: str, reads: list[Expr]) -> str:
        """A name for a JSONata function parameter that hides nothing the
        function reads."""
        return unused(base, self.hides(reads))

    def hides(self, reads: list[Expr]) -> set[str]:
        """The names a parameter or a block's variable would hide: the
        variables of reads, any other name their code reads, and what the
        comprehensions and blocks around it bind."""
        return (
            {self.spelling(name) for name in uses(reads)}
            | {name for read in reads for name in VARIABLE.findall(read.code)}
            | set(self.inner)
        )

    def bind(self, value: Expr) -> bool:
        """Whether code that reads a value more than once needs a name for it.
        One that is already a variable has one."""
        return VARIABLE.fullmatch(value.code) is None

    @contextmanager
    def once(
        self, values: list[Expr], reads: Sequence[Expr] = (), *, always: bool = False
    ) -> Iterator[tuple[list[tuple[str, Expr]], list[Expr]]]:
        """values that the code being built writes more than once. One that
        may change when it is evaluated again, as $random() does, is bound to
        a variable at the start of a block, which block() then puts around the
        code: `($v := $random(); $v - 3 * $floor($v / 3))`. always binds every
        value, for code that reads one several times and would be long or hard
        to follow written out. Yields the bindings and the values, with the
        variable in the place of each value bound. The variable hides nothing
        the block reads: the values, reads, and what the comprehensions and
        blocks around it bind."""
        taken = self.hides([*values, *reads])
        bindings: list[tuple[str, Expr]] = []
        arguments: list[Expr] = []
        for value in values:
            if not (always or value.volatile):
                arguments.append(value)
                continue
            name = unused("v", taken)
            taken.add(name)
            bindings.append((name, value))
            arguments.append(
                expression(
                    "$" + name, value.variables, type=value.type, boolean=value.boolean
                )
            )
        self.inner.extend(name for name, _ in bindings)
        try:
            yield bindings, arguments
        finally:
            del self.inner[len(self.inner) - len(bindings) :]

    def container(self, node: ast.expr, value: Expr, kind: str, rule: str) -> None:
        if value.type is not None and kind not in value.type.kinds:
            raise CompileError(
                f"{ast.unparse(node)} is a {value.type.describe()}; {rule}", node
            )

    def call(self, node: ast.Call) -> Expr:
        target = qualified(node.func, self.names) or ""
        if target.startswith("sfnx.context."):
            # A method of the Context Object, which is a dict.
            target = ""
        if target == "sfnx.task":
            return self.task_call(node)
        if target == "sfnx.jsonata":
            return self.jsonata(node)
        if target.startswith("sfnx.") and target[5:] in COMPOSED:
            return self.composed_call(node, target[5:])
        if target.startswith("sfnx."):
            raise CompileError(
                f"{ast.unparse(node.func)}() makes a state and has no value; "
                "call it on its own line",
                node,
            )
        if target == "json.loads":
            return self.json_loads(node)
        if target in MATH_FUNCTIONS:
            return self.math_function(node, target)
        if target in STRINGIFIED:
            arity, spelled, returned = STRINGIFIED[target]
            if len(node.args) != arity or node.keywords:
                takes = "one argument" if arity else "no arguments"
                raise CompileError(
                    f"{spelled} takes {takes} here: str({spelled})", node
                )
            written = f"str({spelled})"
            if target in DATETIMES:
                written += f" or {spelled}.timestamp()"
            raise CompileError(
                f"{spelled} is {returned}, not JSON; write {written}", node
            )
        if target in HASHES:
            raise CompileError(
                f"{ast.unparse(node.func)}() is a hash object, not JSON; write "
                f"{ast.unparse(node.func)}(s.encode()).hexdigest()",
                node,
            )
        if target == "itertools.batched":
            raise CompileError(
                "itertools.batched() is a list here only as "
                "list(itertools.batched(items, n))",
                node,
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr == "hexdigest":
            return self.hexdigest(node, node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "timestamp":
            return self.timestamp(node, node.func)
        if target == "time.time":
            if node.args or node.keywords:
                raise CompileError("time.time() takes no arguments", node)
            millis = call("millis", [], of(NUMBER))
            return binary(millis, "/", literal(1000), MULTIPLY, of(NUMBER))
        if not target and isinstance(node.func, ast.Attribute):
            self.check_module_import(node.func)
        if (
            not target
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in STRING_METHODS
        ):
            return self.string_method(node, node.func)
        if (
            not target
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in DICT_METHODS
        ):
            return self.dict_method(node, node.func)
        if not isinstance(node.func, ast.Name):
            called = target or ast.unparse(node.func)
            advice = MODULE_REWRITES.get(called)
            if advice is None and isinstance(node.func, ast.Attribute):
                advice = METHOD_REWRITES.get(node.func.attr)
            if advice is not None:
                raise CompileError(
                    f"{ast.unparse(node.func)}() is not supported; {advice}", node.func
                )
            raise CompileError(
                f"{ast.unparse(node.func)}() is not supported; write the operation "
                "with operators, supported functions or jsonata(), or compute it "
                "in a Lambda task (the methods sfnx compiles are "
                f"{spoken(list(STRING_METHODS))} of strings and "
                f"{spoken(list(DICT_METHODS))} of dicts)",
                node.func,
            )
        self.check_import(node.func)
        name = node.func.id
        if name in CONSUMERS:
            node = consumed(node)
        if name == "sorted":
            return self.ordered(node)
        if name in {"max", "min"} and node.keywords:
            return self.extreme(node, name)
        if node.keywords:
            raise CompileError(f"{name}() takes no keyword arguments here", node)
        if name in {"len", "float", "int", "str", "bool"}:
            if len(node.args) != 1:
                raise CompileError(f"{name}() takes one argument: {name}(x)", node)
            spelled = self.stringified(node.args[0]) if name == "str" else None
            if spelled is not None:
                return spelled
            argument = self.expr(node.args[0])
            if name == "float":
                return call("number", [argument], of(NUMBER))
            if name == "int":
                return self.truncate(call("number", [argument], of(NUMBER)))
            if name == "str":
                return text(argument)
            if name == "bool":
                return self.truth(argument)
            return self.length(node.args[0], argument)
        if name == "isinstance":
            return self.isinstance(node)
        if name == "abs" and len(node.args) == 1:
            return call("abs", [self.numeric(node.args[0], "abs()")], of(NUMBER))
        if name == "round" and len(node.args) in {1, 2}:
            numbers = [self.numeric(a, "round()") for a in node.args]
            return call("round", numbers, of(NUMBER))
        if name == "sum" and len(node.args) == 1:
            return call("sum", [self.numbers(node.args[0], name)], of(NUMBER))
        if name in {"max", "min"} and len(node.args) == 1:
            return call(name, [self.numbers(node.args[0], name)], of(NUMBER))
        if name in {"max", "min"} and len(node.args) > 1:
            values = [self.numeric(a, f"{name}()") for a in node.args]
            listed = expression(
                "[" + ", ".join(v.code for v in values) + "]",
                uses(values),
                volatile=changes(values),
            )
            return call(name, [listed], of(NUMBER))
        if name in {"abs", "round", "sum", "max", "min"}:
            raise CompileError(f"{name}() is written {NUMBER_FUNCTIONS[name]}", node)
        if name == "range":
            return self.range_list(node)
        if name == "reversed":
            if len(node.args) != 1:
                raise CompileError("reversed() takes one argument: reversed(xs)", node)
            listed = self.listed(node.args[0], name)
            return call("reverse", [listed], listed.type)
        if name == "enumerate":
            raise CompileError(
                "enumerate() is not supported; count with range: "
                "for i in range(len(items)): item = items[i]",
                node,
            )
        if name == "zip":
            raise CompileError(
                "zip() is a list here only as list(zip(a, b)); in a loop, count "
                "with range: for i in range(len(items)): item = items[i]",
                node,
            )
        if name == "set":
            raise CompileError(
                "JSON has lists only; keep each item once with list(set(items))", node
            )
        if name == "list":
            if len(node.args) != 1:
                raise CompileError("list() takes one argument: list(d)", node)
            return self.listed(node.args[0])
        if name == "dict":
            raise CompileError(
                "dict() does not convert here; declare the type instead: x: dict = ...",
                node,
            )
        if self.is_function(name):
            raise CompileError(direct_call(name), node)
        if name in BUILTIN_REWRITES:
            raise CompileError(
                f"{name}() is not supported; {BUILTIN_REWRITES[name]}", node
            )
        raise CompileError(
            f"calling {name}() is not supported; write it with operators or "
            "jsonata(), or compute it in a Lambda task",
            node,
        )

    def stringified(self, node: ast.expr) -> Expr | None:
        """uuid.uuid4() or a datetime in str() or an f-string, as the text it
        holds: $uuid(), $now(), or $fromMillis of the moment."""
        if not isinstance(node, ast.Call):
            return None
        target = qualified(node.func, self.names) or ""
        found = STRINGIFIED.get(target)
        if found is None or len(node.args) != found[0] or node.keywords:
            return None
        if target == "uuid.uuid4":
            return call("uuid", [], of(STRING))
        if target == NOW:
            return call("now", [], of(STRING))
        return call("fromMillis", [self.millis(node, target)], of(STRING))

    def millis(self, node: ast.Call, target: str) -> Expr:
        """A datetime as the milliseconds since the epoch that $fromMillis
        takes: $millis() for now, $toMillis of a timestamp, or the seconds
        given, times 1000."""
        if target == NOW:
            return call("millis", [], of(NUMBER))
        if target == FROM_ISO:
            text = self.operand(
                node.args[0],
                STRING,
                "datetime.fromisoformat() reads a timestamp string",
            )
            return call("toMillis", [text], of(NUMBER))
        seconds = self.numeric(node.args[0], "datetime.fromtimestamp()")
        return binary(seconds, "*", literal(1000), MULTIPLY, of(NUMBER))

    def timestamp(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """A datetime's .timestamp(), the seconds since the epoch: the
        milliseconds JSONata counts divided, or the seconds fromtimestamp was
        given."""
        made = method.value
        written = "datetime.fromisoformat(text).timestamp()"
        if not isinstance(made, ast.Call) or node.args or node.keywords:
            raise CompileError(f"timestamp() is written {written}", node)
        target = qualified(made.func, self.names) or ""
        if not target and isinstance(made.func, ast.Attribute):
            self.check_module_import(made.func)
        if target not in DATETIMES:
            raise CompileError(f"timestamp() is written {written}", node)
        arity, spelled, _ = STRINGIFIED[target]
        if len(made.args) != arity or made.keywords:
            raise CompileError(f"write {spelled}", made)
        if target == FROM_TIMESTAMP:
            return self.numeric(made.args[0], "datetime.fromtimestamp()")
        return binary(
            self.millis(made, target), "/", literal(1000), MULTIPLY, of(NUMBER)
        )

    def range_arguments(self, node: ast.Call) -> tuple[Expr, Expr, Expr]:
        """The start, stop and step of range(), whose step is a whole number
        written in the source."""
        arguments = node.args
        if not 1 <= len(arguments) <= 3 or node.keywords:
            raise CompileError(
                "range takes a stop, or a start, a stop and a step: range(10)", node
            )
        values = [self.numeric(a, "range") for a in arguments]
        step = literal(1)
        if len(values) == 3:
            step = values[2]
            if not (type(step.template) is int and step.template != 0):
                raise CompileError(
                    "the step of range is a nonzero whole number, such as 2 or -1",
                    arguments[2],
                )
        start, stop = (literal(0), values[0]) if len(values) == 1 else values[:2]
        return start, stop, step

    def range_list(self, node: ast.Call) -> Expr:
        """range() as a value: [a..b] for a step of 1, and $range, which
        includes its end and returns one number as itself, for any other."""
        start, stop, step = self.range_arguments(node)
        numbers = of(ARRAY, items=of(NUMBER))
        assert type(step.template) is int
        if step.template == 1:
            end = difference(stop, literal(1))
            bounds = [
                v.code if v.precedence >= ADD else f"({v.code})" for v in (start, end)
            ]
            code = f"[{bounds[0]}..{bounds[1]}]"
        else:
            end = (
                difference(stop, literal(1))
                if step.template > 0
                else sum_of(stop, literal(1))
            )
            code = f"[{call('range', [start, end, step], None).code}]"
        return expression(
            code,
            uses([start, stop]),
            type=numbers,
            constructor=True,
            volatile=changes([start, stop]),
        )

    def ordered(self, node: ast.Call) -> Expr:
        """sorted(x), of numbers or strings, which are what JSONata's $sort
        orders without a function, or sorted(x, key=lambda item: ...), which
        becomes the comparison $sort takes. reverse= turns either around."""
        if len(node.args) != 1:
            raise CompileError("sorted() takes one argument: sorted(xs)", node)
        descending = False
        key = None
        for keyword in node.keywords:
            if keyword.arg == "key":
                key = keyword.value
            elif (
                keyword.arg == "reverse"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, bool)
            ):
                descending = keyword.value.value
            else:
                raise CompileError(
                    "sorted() takes key= and reverse=True or reverse=False: "
                    'sorted(xs, key=lambda x: x["price"])',
                    keyword.value,
                )
        listed = self.listed(node.args[0], "sorted")
        if key is not None:
            comparator = self.comparator(listed, key, "sorted", descending)
            return call("sort", [listed, comparator], listed.type)
        items = listed.type.items if listed.type else None
        if items is not None and (
            items.kind is None or items.kind not in {NUMBER, STRING}
        ):
            raise CompileError(
                f"the items of {ast.unparse(node.args[0])} are {items.describe()}; "
                "sorted() orders numbers or strings, as JSONata's $sort does",
                node.args[0],
            )
        ordered = call("sort", [listed], listed.type)
        return call("reverse", [ordered], listed.type) if descending else ordered

    def extreme(self, node: ast.Call, name: str) -> Expr:
        """max(xs, key=lambda item: ...), max(a, b, key=...) and min(...).
        $max and $min take numbers only and no comparison of their own, so the
        items are ordered by the key and the one at the end of it taken."""
        if not node.args:
            raise CompileError(f"{name}() is written {NUMBER_FUNCTIONS[name]}", node)
        if len(node.keywords) != 1 or node.keywords[0].arg != "key":
            raise CompileError(
                f'{name}() takes key=: {name}(xs, key=lambda x: x["price"])', node
            )
        if len(node.args) == 1:
            listed = self.listed(node.args[0], name)
        else:
            listed = array([self.expr(argument) for argument in node.args])
        comparator = self.comparator(listed, node.keywords[0].value, name, False)
        ordered = call("sort", [listed, comparator], listed.type)
        return index(ordered, literal(-1 if name == "max" else 0))

    def comparator(
        self, source: Expr, key: ast.expr, name: str, descending: bool
    ) -> Expr:
        """The function $sort takes, from a key=lambda: the key of one item
        against the key of the other. Python orders by the key and leaves items
        with the same key in order, which $sort does too, so reverse= turns the
        comparison around rather than the result."""
        usage = f'{name}(xs, key=lambda x: x["price"])'
        if not (
            isinstance(key, ast.Lambda)
            and len(key.args.args) == 1
            and not (
                key.args.posonlyargs
                or key.args.kwonlyargs
                or key.args.vararg
                or key.args.kwarg
                or key.args.defaults
            )
        ):
            raise CompileError(
                f"the key of {name}() is a lambda of one item: {usage}", key
            )
        parameter = key.args.args[0].arg
        items = source.type.items if source.type else None
        # The parameters are the function's own names, hiding neither what the
        # list reads nor what the key reads under a name of its own.
        taken = self.hides([source]) | {
            self.spelling(read.id)
            for read in ast.walk(key.body)
            if isinstance(read, ast.Name)
        }
        first = unused("a", taken)
        second = unused("b", taken | {first})
        keys = []
        saved = self.bindings.get(parameter)
        try:
            for variable in (first, second):
                self.bindings[parameter] = expression("$" + variable, type=items)
                self.inner.append(variable)
                try:
                    keys.append(self.expr(key.body))
                finally:
                    self.inner.pop()
        finally:
            if saved is None:
                self.bindings.pop(parameter, None)
            else:
                self.bindings[parameter] = saved
        declared = keys[0].type
        if declared is not None and declared.kind not in {NUMBER, STRING}:
            raise CompileError(
                f"the key of {name}() is {declared.describe()}; JSONata orders "
                "numbers and strings",
                key.body,
            )
        symbol = "<" if descending else ">"
        test = binary(keys[0], symbol, keys[1], COMPARE, of(BOOLEAN), True)
        return comparing(first, second, test)

    def jsonata(self, node: ast.Call) -> Expr:
        """jsonata(expression, name=value): the expression as it is written, in
        a block that binds each value to the variable of its name first, since
        a Python variable does not always keep its name in the definition."""
        usage = "jsonata(\"$pad($s, -5, '0')\", s=code)"
        if len(node.args) != 1 or not (
            isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            raise CompileError(
                f"jsonata() takes the expression as a literal string: {usage}", node
            )
        bindings = []
        values = []
        bound: list[str] = []
        for keyword in node.keywords:
            name = keyword.arg
            if name is None:
                raise CompileError(
                    f"jsonata() takes each value by its name: {usage}", keyword.value
                )
            if name == "states" or name in JSONATA_FUNCTIONS:
                raise CompileError(
                    f"{name} would hide ${name} in the expression; choose another name",
                    keyword,
                )
            value = self.expr(keyword.value)
            # The block binds in order, so a value cannot read a name bound
            # before it, as Python would give it the variable of that name.
            earlier = [
                b for b in bound if b in {self.spelling(v) for v in value.variables}
            ]
            if earlier:
                raise CompileError(
                    f"{name} reads {earlier[0]}, which jsonata() binds before it; "
                    "give the value to bind another name",
                    keyword.value,
                )
            bound.append(name)
            code = value.code if value.precedence == ATOM else f"({value.code})"
            bindings.append(f"${name} := {code}; ")
            values.append(value)
        written = node.args[0].value
        reads = self.variables(written, bound)
        # The text is not parsed, so what it calls is unknown: it may call
        # $random under that name, or under one it binds the function to.
        if not bindings:
            return expression(written, reads, precedence=WRITTEN, volatile=True)
        return expression(
            "(" + "".join(bindings) + written + ")",
            uses(values) | reads,
            volatile=True,
        )

    def variables(self, written: str, bound: list[str]) -> frozenset[str]:
        """The variables an expression written by hand reads: every $name in
        it that neither the call nor a function around it binds, under the
        Python name written that way. A name the definition renames is not
        among them: where the variable count is written $count_val, $count is
        the JSONata function."""
        python = {spelled: name for name, spelled in self.spellings.items()}
        found = set(VARIABLE.findall(written)) - set(bound) - set(self.inner)
        return frozenset(
            python.get(name, name)
            for name in found
            if name in python or name not in self.spellings
        )

    def math_function(self, node: ast.Call, target: str) -> Expr:
        function = MATH_FUNCTIONS[target]
        if function == "random":
            if node.args or node.keywords:
                raise CompileError("random.random() takes no arguments", node)
            return call("random", [], of(NUMBER))
        if node.keywords or len(node.args) != 1:
            raise CompileError(f"{target}() takes one number: {target}(x)", node)
        value = self.numeric(node.args[0], f"{target}()")
        return call(function, [value], of(NUMBER))

    def numbers(self, node: ast.expr, name: str) -> Expr:
        """The list sum(), max() or min() takes: JSONata's functions take numbers
        only, so a list known to hold anything else is rejected."""
        value = self.operand(node, ARRAY, f"{name}() takes a list of numbers")
        items = value.type.items if value.type else None
        if items is not None and items.kinds != {NUMBER}:
            raise CompileError(
                f"the items of {ast.unparse(node)} are {items.describe()}; {name}() "
                f"takes numbers here, as JSONata's ${name} does",
                node,
            )
        return value

    def is_average(self, node: ast.BinOp) -> bool:
        """sum(xs) / len(xs) of one list, which is $average."""
        left, right = node.left, node.right
        return (
            isinstance(left, ast.Call)
            and isinstance(right, ast.Call)
            and isinstance(left.func, ast.Name)
            and isinstance(right.func, ast.Name)
            and left.func.id == "sum"
            and right.func.id == "len"
            and len(left.args) == 1
            and len(right.args) == 1
            and not left.keywords
            and not right.keywords
            and ast.dump(left.args[0]) == ast.dump(right.args[0])
        )

    def json_loads(self, node: ast.Call) -> Expr:
        if node.keywords or len(node.args) != 1:
            raise CompileError("json.loads() takes one string: json.loads(s)", node)
        value = self.operand(node.args[0], STRING, "json.loads() reads a string")
        return call("parse", [value], None)

    def string_method(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """A method of str as the JSONata function for it. Of the JSON types
        only strings have these methods, so a receiver of unknown type needs no
        annotation."""
        name = method.attr
        usage = STRING_METHODS[name]
        receiver = self.operand(
            method.value, STRING, f"{name}() is a string method: {usage}"
        )
        if node.keywords:
            raise CompileError(
                f"{name}() takes no keyword arguments here: {usage}", node
            )
        arguments = node.args
        text = of(STRING)
        if name in {"lower", "upper"} and not arguments:
            return call(f"{name}case", [receiver], text)
        if name == "strip" and not arguments:
            return call("replace", [receiver, OUTER_WHITESPACE, literal("")], text)
        if name == "split" and not arguments:
            # At runs of whitespace: $trim makes each run one space and removes
            # the runs at both ends. Blank text trims to "", which $split reads
            # as one empty part, where Python gives no parts at all.
            strings = of(ARRAY, items=text)
            compacted = call("trim", [receiver], text)
            with self.once([compacted]) as (bindings, (trimmed,)):
                blank = binary(trimmed, "=", literal(""), COMPARE, of(BOOLEAN), True)
                parts = call("split", [trimmed, literal(" ")], strings)
                return block(bindings, conditional(blank, array([]), parts, strings))
        if name == "split" and len(arguments) == 1:
            separator = self.operand(
                arguments[0], STRING, f"{name}() splits at a string"
            )
            if written_text(separator) == "":
                raise CompileError(
                    "split() splits at one character or more; list(s) reads the "
                    "text as its characters",
                    arguments[0],
                )
            return call("split", [receiver, separator], of(ARRAY, items=text))
        if name == "split" and len(arguments) == 2:
            raise CompileError(
                "split() takes no maximum; split it all and read the parts you need: "
                "s.split(sep)[0]",
                arguments[1],
            )
        if name == "replace" and len(arguments) in {2, 3}:
            rule = f"{name}() replaces strings"
            values = [self.operand(a, STRING, rule) for a in arguments[:2]]
            if written_text(values[0]) == "":
                # Python writes new between every character and at both ends.
                raise CompileError(
                    "replace() replaces one character or more; Step Functions "
                    "fails on an empty pattern",
                    arguments[0],
                )
            if len(arguments) == 3:
                count = self.numeric(arguments[2], "the count of replace()")
                if not whole_number(count):
                    # $replace fails below 0, where Python replaces every
                    # occurrence, and takes 2.5 as 2, where Python raises.
                    raise CompileError(
                        "the count of replace() is a whole number of 0 or more; "
                        "leave it out to replace every occurrence",
                        arguments[2],
                    )
                values.append(count)
            return call("replace", [receiver, *values], text)
        if name in {"ljust", "rjust"} and len(arguments) in {1, 2}:
            width = self.numeric(arguments[0], f"the width of {name}()")
            if not whole_number(width):
                # $pad fills on the other side for a width below 0, where
                # Python leaves the text as it is, and takes 6.5 as 6.
                raise CompileError(
                    f"the width of {name}() is a whole number of 0 or more",
                    arguments[0],
                )
            if name == "rjust":
                # $pad fills on the left for a negative width.
                number = width.template
                width = literal(-number) if type(number) is int else negate(width)
            fill = [
                self.operand(a, STRING, f"{name}() fills with a string")
                for a in arguments[1:]
            ]
            written = written_text(fill[0]) if fill else None
            if written is not None and len(written) != 1:
                # $pad repeats a fill of several characters; Python raises.
                raise CompileError(f"{name}() fills with one character", arguments[1])
            return call("pad", [receiver, width, *fill], text)
        if name in {"startswith", "endswith"} and len(arguments) == 1:
            return self.affix(receiver, arguments[0], name)
        if name == "join" and len(arguments) == 1:
            items = self.expr(arguments[0])
            if items.type is not None and not items.type.kinds <= {ARRAY, STRING}:
                raise CompileError(
                    f"{ast.unparse(arguments[0])} is a {items.type.describe()}; "
                    "join() takes a list of strings",
                    arguments[0],
                )
            # Python joins the characters of a string, which $join returns as
            # it is, so a string is split first and one that may be a string
            # is tested for it when it is evaluated.
            kinds = items.type.kinds if items.type is not None else {ARRAY, STRING}
            if kinds == {STRING}:
                items = call("split", [items, literal("")], of(ARRAY))
            elif kinds != {ARRAY}:
                with self.once([items], [receiver], always=self.bind(items)) as (
                    bindings,
                    (bound,),
                ):
                    kind = call("type", [bound], of(STRING))
                    test = binary(
                        kind, "=", literal("string"), COMPARE, of(BOOLEAN), True
                    )
                    characters = call("split", [bound, literal("")], of(ARRAY))
                    items = conditional(test, characters, bound, of(ARRAY))
                return block(bindings, call("join", [items, receiver], text))
            return call("join", [items, receiver], text)
        raise CompileError(f"{name}() is written {usage}", node)

    def dict_method(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """d.keys(), d.values() and d.get(). Of the JSON types only dicts have
        these methods, so a receiver of unknown type needs no annotation."""
        name = method.attr
        mapping = self.operand(
            method.value, OBJECT, f"{name}() is a dict method: {DICT_METHODS[name]}"
        )
        if name == "get" and not node.keywords and len(node.args) in {1, 2}:
            return self.get(mapping, node.args)
        if name == "get" or node.args or node.keywords:
            raise CompileError(f"{name}() is written {DICT_METHODS[name]}", node)
        if name == "keys":
            return keys_of(mapping)
        values = mapping.type.values if mapping.type else None
        each = call("each", [mapping, expression("function($v) { $v }")], None)
        code = (
            f"$append([], {each.code}[])" if may_be_list(values) else f"[{each.code}]"
        )
        return expression(
            code,
            mapping.variables,
            type=of(ARRAY, items=values),
            constructor=True,
            volatile=mapping.volatile,
        )

    def get(self, mapping: Expr, arguments: list[ast.expr]) -> Expr:
        """d.get(key, default) as the value when the key exists, and otherwise
        the default, or null without one."""
        key = arguments[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if mapping.type is not None and mapping.type in CONTEXT_OBJECTS:
                self.context_field(key, mapping.type, key.value)
            value = field(mapping, key.value)
        else:
            name = self.operand(key, STRING, "keys are strings")
            values = mapping.type.values if mapping.type else None
            value = call("lookup", [mapping, name], values)
        default = self.expr(arguments[1]) if len(arguments) == 2 else literal(None)
        # The value is written twice: tested, then read.
        with self.once([value], [default]) as (bindings, (value,)):
            present = call("exists", [value], of(BOOLEAN), boolean=True)
            chosen = conditional(
                present, value, default, union(value.type, default.type)
            )
        return block(bindings, chosen)

    def listed(self, node: ast.expr, name: str = "list") -> Expr:
        """list(x): the keys of a dict, the characters of a string, or a list
        as it is, a JSON value being a copy already; and the lists that set(),
        zip() and itertools.batched() make."""
        made = self.made_list(node) if isinstance(node, ast.Call) else None
        if made is not None:
            return made
        value = self.expr(node)
        kind = self.known(node, value, "dict", f"{name}() depends on the type")
        if kind == OBJECT:
            return keys_of(value)
        if kind == STRING:
            characters = of(ARRAY, items=of(STRING))
            return call("split", [value, literal("")], characters)
        if kind == ARRAY:
            return value
        raise CompileError(
            f"{ast.unparse(node)} is a {kind}; {name}() takes a dict, a list or a "
            "string",
            node,
        )

    def made_list(self, node: ast.Call) -> Expr | None:
        """set(x) as $distinct, zip(a, b) as $zip and itertools.batched(xs, n)
        as $partition: what Python makes a set or an iterator of is a list in
        JSON."""
        if node.keywords:
            return None
        function = node.func.id if isinstance(node.func, ast.Name) else None
        if function == "set" and len(node.args) == 1:
            items = self.listed(node.args[0], "set")
            return call("distinct", [items], items.type)
        if function == "zip" and len(node.args) >= 2:
            lists = [self.operand(a, ARRAY, "zip() takes lists") for a in node.args]
            return call("zip", lists, of(ARRAY, items=of(ARRAY)))
        if (
            qualified(node.func, self.names) == "itertools.batched"
            and len(node.args) == 2
        ):
            items = self.operand(
                node.args[0], ARRAY, "itertools.batched() takes a list"
            )
            size = self.numeric(node.args[1], "the size of itertools.batched()")
            written = written_number(size)
            if written is not None and not (type(written) is int and written >= 1):
                # $partition makes no batches at all for 0 and batches of one
                # for 1.5, where Python raises, and fails for a negative size.
                # A size read at run time is not known here.
                raise CompileError(
                    "the size of itertools.batched() is a whole number of 1 or "
                    "more, such as 10",
                    node.args[1],
                )
            # $partition returns nothing for no items; brackets make that [].
            batches = call("partition", [items, size], None)
            return expression(
                f"[{batches.code}]",
                batches.variables,
                type=of(ARRAY, items=items.type),
                constructor=True,
                volatile=batches.volatile,
            )
        return None

    def hexdigest(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """hashlib.sha256(s.encode()).hexdigest() and the other algorithms, as
        $hash of the text."""
        digest = method.value
        written = "hashlib.sha256(s.encode()).hexdigest()"
        if not isinstance(digest, ast.Call) or node.args or node.keywords:
            raise CompileError(f"hexdigest() is written {written}", node)
        target = qualified(digest.func, self.names) or ""
        if not target and ast.unparse(digest.func) in MODULE_IMPORTS:
            raise CompileError(
                "hashlib is not imported; write import hashlib", digest.func
            )
        encoded = digest.args[0] if len(digest.args) == 1 else None
        if (
            target not in HASHES
            or digest.keywords
            or not isinstance(encoded, ast.Call)
            or not isinstance(encoded.func, ast.Attribute)
            or encoded.func.attr != "encode"
            or encoded.args
            or encoded.keywords
        ):
            raise CompileError(f"hexdigest() is written {written}", node)
        text = self.operand(encoded.func.value, STRING, "encode() is a string method")
        return call("hash", [text, literal(HASHES[target])], of(STRING))

    def affix(self, text: Expr, node: ast.expr, name: str) -> Expr:
        """s.startswith(p) and s.endswith(p): the part of s as long as p,
        compared with p."""
        affix = self.operand(node, STRING, f"{name}() compares with a string")
        template = affix.template
        spelled = (
            template
            if isinstance(template, str) and affix.code == string(template)
            else None
        )
        size = (
            literal(len(spelled))
            if spelled is not None
            else call("length", [affix], of(NUMBER))
        )
        if name == "startswith":
            start = literal(0)
        elif spelled is not None:
            start = literal(-len(spelled))
        else:
            start = difference(call("length", [text], of(NUMBER)), size)
        part = call("substring", [text, start, size], of(STRING))
        return binary(part, "=", affix, COMPARE, of(BOOLEAN), True)

    def operand(self, node: ast.expr, kind: str, rule: str) -> Expr:
        """A value that must be of one kind when its type is known."""
        value = self.expr(node)
        if value.type is not None and value.type.kinds != {kind}:
            if value.type.kind is None:
                raise CompileError(several(node, value.type), node)
            raise CompileError(
                f"{ast.unparse(node)} is a {value.type.kind}; {rule}", node
            )
        return value

    def check_module_import(self, func: ast.Attribute) -> None:
        """A standard library function called without importing its module."""
        spelling = ast.unparse(func)
        if spelling in MODULE_IMPORTS:
            module = ast.unparse(func.value)
            raise CompileError(
                f"{module} is not imported; write {MODULE_IMPORTS[spelling]}", func
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
        resource_node = self.holds(node.args[0])
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

    def truncate(self, number: Expr) -> Expr:
        """int(x): towards zero, as Python truncates. $floor alone takes -1.5
        to -2 and $ceil 1.5 to 2, so the sign chooses between them, and the
        number is bound to a name because the test reads it three times."""
        numeric = of(NUMBER)
        with self.once([number], always=self.bind(number)) as (bindings, (value,)):
            negative = binary(value, "<", literal(0), COMPARE, of(BOOLEAN), True)
            towards_zero = conditional(
                negative,
                call("ceil", [value], numeric),
                call("floor", [value], numeric),
                numeric,
            )
        return block(bindings, towards_zero)

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


def direct_call(name: str) -> str:
    """The message for calling a function of the module directly."""
    return (
        f"{name}() cannot be called directly; a function runs as states "
        f"through parallel({name}) or a map, or write its body here"
    )


def several(node: ast.expr, declared: Type) -> str:
    text = ast.unparse(node)
    return (
        f"{text} may be {declared.describe()}; narrow it first with "
        f"isinstance({text}, ...) or {text} is not None"
    )


def as_fstring(template: str, values: list[ast.expr]) -> str | None:
    """The f-string that writes what template % values writes, or None when a
    conversion has no spelling as {x}, or the conversions and the values do not
    match in number."""
    pieces: list[ast.expr] = []
    plain = ""
    taken = 0
    index = 0
    while index < len(template):
        if template[index] != "%":
            plain += template[index]
            index += 1
            continue
        conversion = template[index + 1 : index + 2]
        index += 2
        if conversion == "%":
            plain += "%"
            continue
        if conversion not in CONVERSIONS or taken == len(values):
            return None
        if plain:
            pieces.append(ast.Constant(plain))
            plain = ""
        pieces.append(ast.FormattedValue(values[taken], conversion=-1))
        taken += 1
    if taken != len(values):
        return None
    if plain:
        pieces.append(ast.Constant(plain))
    return ast.unparse(ast.JoinedStr(pieces))


def formatting(template: str, right: ast.expr) -> str:
    """The message for "..." % values: the f-string that writes the same text,
    or an example of one when the conversions have no spelling here."""
    values = right.elts if isinstance(right, ast.Tuple) else [right]
    written = as_fstring(template, values)
    advice = (
        f"write an f-string: {written}"
        if written
        else 'write an f-string, such as f"{n} items"'
    )
    return f"old-style % formatting is not supported; {advice}"


def consumed(node: ast.Call) -> ast.Call:
    """sum(x for x in xs), max(...), min(...), sorted(...) and list(...) of a
    generator expression, as the same call of the list comprehension: the
    function reads every item, so the value is the same and the ASL is the
    comprehension's. A generator anywhere else is not a JSON value and stays
    rejected."""
    if len(node.args) != 1 or not isinstance(node.args[0], ast.GeneratorExp):
        return node
    generator = node.args[0]
    listed = ast.ListComp(elt=generator.elt, generators=generator.generators)
    ast.copy_location(listed, generator)
    call = ast.Call(func=node.func, args=[listed], keywords=node.keywords)
    return ast.copy_location(call, node)


def unpacking(node: ast.expr) -> bool:
    """Whether an iterable is the enumerate() or zip() two variables come from.
    Each says what to count with instead, which the message about unpacking
    would hide."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in UNPACKING
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
    # The dict has been translated, so its keys are literal strings or the
    # dicts ** unpacks, whose keys are known only when it runs.
    keys: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            continue
        assert isinstance(key, ast.Constant) and isinstance(key.value, str)
        keys[key.value] = value
        if called.allowed is not None and key.value not in called.allowed:
            close = difflib.get_close_matches(key.value, sorted(called.allowed), n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            raise CompileError(f"{called.name} has no argument {key.value}{hint}", key)
    if None in node.keys:
        return
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


def spoken(names: list[str]) -> str:
    """Names as a sentence lists them: a, b and c."""
    return ", ".join(names[:-1]) + " and " + names[-1]


def reversed_value(value: Expr, kind: str) -> Expr:
    """xs[::-1] and s[::-1]: the list, or the characters of the string, in
    reverse order."""
    if kind == ARRAY:
        return call("reverse", [value], value.type)
    characters = call("split", [value, literal("")], None)
    reverse = call("reverse", [characters], None)
    return call("join", [reverse, literal("")], of(STRING))


def keys_of(mapping: Expr) -> Expr:
    """The keys of a dict as a list: $keys returns one key as itself and none
    as nothing, which brackets turn into a list."""
    keys = call("keys", [mapping], None)
    return expression(
        f"[{keys.code}]",
        keys.variables,
        type=of(ARRAY, items=of(STRING)),
        constructor=True,
        volatile=keys.volatile,
    )


def written(bound: Bound) -> int | None:
    """The whole number a bound's amount is written as in the source."""
    template = bound.amount.template
    return template if type(template) is int else None


def offset(size: Expr, bound: Bound) -> Expr:
    """A bound as a position from the start."""
    return difference(size, bound.amount) if bound.back else bound.amount


def signed(bound: Bound) -> Expr:
    """A bound as $substring reads a start, negative when it counts back."""
    if not bound.back:
        return bound.amount
    number = written(bound)
    return literal(-number) if number is not None else negate(bound.amount)


def difference(left: Expr, right: Expr) -> Expr:
    """left - right, computed when both are numbers written in the source."""
    first, second = left.template, right.template
    if type(first) is int and type(second) is int:
        return literal(first - second)
    if type(second) is int and second == 0:
        return left
    return binary(left, "-", right, ADD, of(NUMBER))


def sum_of(left: Expr, right: Expr) -> Expr:
    """left + right, computed when both are numbers written in the source."""
    first, second = left.template, right.template
    if type(first) is int and type(second) is int:
        return literal(first + second)
    if type(second) is int and second == 0:
        return left
    return binary(left, "+", right, ADD, of(NUMBER))


def may_be_list(kind: Type | None) -> bool:
    return kind is None or ARRAY in kind.kinds


def unused(base: str, taken: set[str]) -> str:
    """base, or base numbered past the names in taken."""
    name = base
    serial = 1
    while name in taken:
        serial += 1
        name = f"{base}_{serial}"
    return name


def comparing(first: str, second: str, body: Expr) -> Expr:
    """The function $sort takes: whether the first item comes after the second."""
    return expression(
        f"function(${first}, ${second}) {{ {body.code} }}",
        body.variables,
        volatile=body.volatile,
    )


def function(parameter: str, body: Expr) -> Expr:
    return expression(
        f"function(${parameter}) {{ {body.code} }}",
        body.variables,
        volatile=body.volatile,
    )


def whole_number(value: Expr) -> bool:
    """Whether a count or a width written in the source is one Python takes: a
    whole number of 0 or more. One read at run time is not known here."""
    written = written_number(value)
    return written is None or (type(written) is int and written >= 0)


def written_number(value: Expr) -> int | float | None:
    """The number a value is written as in the source, if it is written as
    one. A literal keeps the JSON itself as its template."""
    template = value.template
    return template if type(template) is int or type(template) is float else None


def written_text(value: Expr) -> str | None:
    """The text a value is written as in the source, if it is written as one.
    A literal keeps the text itself as its template, except for one that opens
    or closes like a `{% %}` template, which is written as an expression."""
    template = value.template
    if type(template) is not str or template.startswith("{%"):
        return None
    return template


def divided(divisor: Expr, value: Expr) -> Expr:
    """value, with the test Python makes before it divides. Dividing by zero
    raises there, while JSONata gives the string "Infinity", which fails in a
    later state that does arithmetic on it, or compares as a string and takes
    a branch without failing. A divisor written as a number needs no test."""
    if written_number(divisor) is not None:
        return value
    test = binary(divisor, "=", literal(0), COMPARE, of(BOOLEAN), True)
    raised = call("error", [literal("division by zero")], value.type)
    return conditional(test, raised, value, value.type)
