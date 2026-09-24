"""Python expressions to JSONata, with the spelling chosen by the operand types."""

import ast
import datetime
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
    entry,
    expression,
    field,
    grouped,
    index,
    kept,
    literal,
    merged,
    negate,
    obj,
    spelling,
    string,
    uses,
)
from sfnx.integrations import (
    HTTP_METHODS,
    Integration,
    ResourceError,
    integration,
    operation_resource,
)
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
    article,
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
    {
        "activity",
        "context",
        "task",
        "wait",
        "parallel",
        "inline_map",
        "distributed_map",
        "jsonata",
    }
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
# f-string write one as the timestamp text it holds, .timestamp() as the
# seconds since the epoch, and wait(until=) waits for the moment itself.
NOW = "datetime.datetime.now"
FROM_ISO = "datetime.datetime.fromisoformat"
FROM_TIMESTAMP = "datetime.datetime.fromtimestamp"
DATETIMES = (NOW, FROM_ISO, FROM_TIMESTAMP)

# A span of time added to a datetime or taken from one. The units are those
# timedelta takes to the millisecond: microseconds is left out, as Step
# Functions keeps time to the millisecond.
TIMEDELTA = "datetime.timedelta"
TIMEDELTA_UNITS = ("weeks", "days", "hours", "minutes", "seconds", "milliseconds")
TIMEDELTA_WRITTEN = "timedelta(hours=1)"

# The strftime directives that have a picture component writing the same
# value, measured against CPython on Step Functions. The rest are left out:
# the locale names and the week numbers are spelled differently, and the three
# below differ in what they hold.
DIRECTIVES = {
    "Y": "[Y0001]",
    "y": "[Y01]",
    "m": "[M01]",
    "d": "[D01]",
    "H": "[H01]",
    "M": "[m01]",
    "S": "[s01]",
    "j": "[d001]",
}
# Why a directive that looks close enough is left out anyway.
UNPICTURED = {
    "f": ", which is microseconds where Step Functions keeps time to the millisecond",
    "z": ", whose offset is empty in Python for a datetime with no time zone",
    "Z": ", whose name is empty in Python for a datetime with no time zone",
}
STRFTIME_WRITTEN = 'datetime.now().strftime("%Y-%m-%d")'

# The format specs of an f-string: a width, with an alignment and the
# character to fill with before it, and the digits after the decimal point a
# number is written with, or d for a whole number. $pad fills on the right for
# a positive width and on the left for a negative one, which is what < and >
# ask for. The 0 before a width fills a whole number to that width with the
# sign inside it, which is the picture's own business, and Python lets an
# alignment written out override it.
FORMAT_SPEC = re.compile(
    r"(?:(?P<fill>[^\n])?(?P<align>[<>]))?"
    r"(?P<zero>0)?"
    r"(?P<width>[1-9][0-9]*)?"
    r"(?:(?P<grouping>,)?\.(?P<precision>[0-9]+)f|(?P<whole>d))?"
)
SPEC_WRITTEN = 'f"{s:>10}", f"{total:,.2f}" or f"{n:05d}"'

# Calls whose value is an object that JSON holds as text, so they are written
# in str() or an f-string, the datetimes also in .timestamp(): how many
# arguments the call takes, how it is written and what it returns in Python.
STRINGIFIED = {
    "uuid.uuid4": (0, "uuid.uuid4()", "a UUID object"),
    NOW: (0, "datetime.now()", "a datetime object"),
    FROM_ISO: (1, "datetime.fromisoformat(text)", "a datetime object"),
    FROM_TIMESTAMP: (1, "datetime.fromtimestamp(seconds)", "a datetime object"),
}

# The base64 functions, whose text is read out with .decode(), the JSONata
# function of each, and how each is written.
BASE64 = {
    "base64.b64encode": "base64encode",
    "base64.b64decode": "base64decode",
}
BASE64_WRITTEN = {
    "base64.b64encode": "base64.b64encode(s.encode()).decode()",
    "base64.b64decode": "base64.b64decode(s).decode()",
}
BASE64_ANY = " or ".join(BASE64_WRITTEN.values())

# urllib.parse.unquote and unquote_plus, both $decodeUrlComponent: Step
# Functions reads + as a space, which is what unquote_plus does, so unquote
# escapes the + first to keep it.
UNQUOTE = "urllib.parse.unquote"
UNQUOTE_PLUS = "urllib.parse.unquote_plus"

# The standard library functions sfnx compiles, by how a call to one reads
# without its import, and the import to write.
MODULE_IMPORTS = {
    "json.loads": "import json",
    **dict.fromkeys(BASE64, "import base64"),
    **dict.fromkeys((UNQUOTE, UNQUOTE_PLUS), "import urllib.parse"),
    "uuid.uuid4": "import uuid",
    "time.time": "import time",
    **dict.fromkeys(
        ("datetime.now", "datetime.fromisoformat", "datetime.fromtimestamp"),
        "from datetime import datetime",
    ),
    "datetime.timedelta": "from datetime import timedelta",
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
    "map": "write a comprehension: [str(x) for x in xs]",
    "filter": "write a comprehension: [x for x in xs if x]",
}

# The conversions of % formatting that an f-string writes as a plain {x}. For
# the rest the message gives an example of an f-string rather than write one.
CONVERSIONS = frozenset({"s", "d", "i"})

# The built-in functions that give a loop two variables, and would give a
# comprehension two.
UNPACKING = frozenset({"enumerate", "zip"})

# The dict comprehension that reads a dict entry by entry, the one place
# besides a for loop where d.items() gives two variables.
ITEMS_COMPREHENSION = "{k: v for k, v in d.items()}"

# The generator expression the messages of any() and all() write.
EVERY = 'x["ok"] for x in xs'

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
        # The parameters of the functions being called directly, each with the
        # argument written for it, for the places that take what is written.
        self.arguments: dict[str, ast.expr] = {}

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
        machine binds is itself, so its variable is read there instead. A
        parameter of a function called directly holds the argument written for
        it."""
        while isinstance(node, ast.Name) and node.id in self.arguments:
            node = self.arguments[node.id]
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
            if node.id in self.arguments:
                # An argument that is not a value where the call is written
                # is not one where the function reads it either.
                return self.expr(self.arguments[node.id])
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
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__name__"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "type"
            and "type" not in self.bindings
        ):
            return self.error_name(node.value)
        if isinstance(node, ast.Attribute) and self.timedelta_span(node.value):
            raise CompileError(
                "a timedelta is seconds through total_seconds() here: "
                f"{parenthesized(node.value)}.total_seconds()",
                node,
            )
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
            return self.dict_comprehension(node)
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
        generator = self.one_for(node)
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
        source = self.iterated(generator.iter)
        item = source.type.items if source.type else None
        with self.parameters({name: item}):
            tests, narrowed = self.conditions(generator.ifs)
            with self.narrowed(narrowed):
                element = self.expr(node.elt)
        self.check_hiding({name: generator.target}, [element, *tests])
        result = source
        if tests:
            result = call(
                "filter",
                [source, function([self.spelling(name)], conjunction(tests))],
                source.type,
            )
        mapped = not named(node.elt, name)
        if mapped:
            if tests and may_be_list(item):
                # $map would iterate the items of a single list $filter kept.
                result = expression(
                    result.code + "[]", result.variables, volatile=result.volatile
                )
            result = call(
                "map", [result, function([self.spelling(name)], element)], None
            )
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

    @contextmanager
    def parameters(self, names: dict[str, Type | None]) -> Iterator[None]:
        """The variables of a comprehension while its conditions and results
        are translated. Each is the parameter of the JSONata function, not a
        Step Functions variable: a variable of the same name that another
        binding reads would be hidden."""
        saved = {name: self.bindings.get(name) for name in names}
        for name, declared in names.items():
            self.bindings[name] = expression("$" + self.spelling(name), type=declared)
            self.inner.append(self.spelling(name))
        self.comprehending += 1
        try:
            yield
        finally:
            self.comprehending -= 1
            for name in names:
                self.inner.pop()
                outer = saved[name]
                if outer is None:
                    del self.bindings[name]
                else:
                    self.bindings[name] = outer

    def conditions(self, ifs: list[ast.expr]) -> tuple[list[Expr], dict[str, Type]]:
        """The if conditions of a comprehension, each narrowing the ones after
        it and the result, as they do in an if statement."""
        tests = []
        narrowed: dict[str, Type] = {}
        for test in ifs:
            with self.narrowed(narrowed):
                tests.append(self.condition(test))
                when, _ = self.narrowing(test)
            narrowed = {**narrowed, **when}
        return tests, narrowed

    def check_hiding(self, names: dict[str, ast.expr], values: list[Expr]) -> None:
        """A comprehension variable that hides a variable the comprehension
        reads under another name, such as the list a for loop around it
        iterates."""
        read = uses(values)
        for name, target in names.items():
            if name in read:
                raise CompileError(
                    f"{name} is a variable that this comprehension reads through "
                    f"another name, which its own {name} would hide; choose "
                    "another name for it",
                    target,
                )

    def dict_comprehension(self, node: ast.DictComp) -> Expr:
        """{f(x): g(x) for x in xs if c} as one pass over xs whose objects are
        merged: the function returns nothing for an item the condition drops,
        which $map leaves out, and $merge of nothing is {}. The condition, the
        key and the value are each evaluated once per item, in the order
        Python evaluates them, and a repeated key keeps the last value, as
        $merge gives a later object precedence."""
        generator = self.one_for(node)
        if unpacked(generator.iter) == "items":
            return self.items_comprehension(node, generator)
        if not isinstance(generator.target, ast.Name):
            if unpacking(generator.iter):
                # Raise the advice of enumerate() or zip(), which says what to
                # count with, rather than the message below.
                self.expr(generator.iter)
            raise CompileError(
                "a dict comprehension iterates one variable, or the key and the "
                f"value of d.items(): {ITEMS_COMPREHENSION}",
                generator.target,
            )
        name = generator.target.id
        source = self.iterated(generator.iter)
        item = source.type.items if source.type else None
        with self.parameters({name: item}):
            tests, narrowed = self.conditions(generator.ifs)
            with self.narrowed(narrowed):
                key = self.expr(node.key)
                value = self.expr(node.value)
        self.check_hiding({name: generator.target}, [key, value, *tests])
        self.json_key(node.key, key)
        body = entry(key, value)
        if tests:
            body = kept(conjunction(tests), body)
        mapped = call("map", [source, function([self.spelling(name)], body)], None)
        return merged(mapped, value.type)

    def items_comprehension(
        self, node: ast.DictComp, generator: ast.comprehension
    ) -> Expr:
        """{k: v for k, v in d.items() if c} over the dict itself: $sift where
        the entries pass through as they are, and $each where the key or the
        value is rewritten, both merged as the comprehension over a list is."""
        assert isinstance(generator.iter, ast.Call)
        names = generator.target.elts if isinstance(generator.target, ast.Tuple) else []
        if len(names) != 2 or not all(isinstance(n, ast.Name) for n in names):
            raise CompileError(
                f"items() gives two variables: {ITEMS_COMPREHENSION}",
                generator.target,
            )
        key_name, value_name = names
        assert isinstance(key_name, ast.Name) and isinstance(value_name, ast.Name)
        if key_name.id == value_name.id:
            raise CompileError(
                f"the two variables need different names: {ITEMS_COMPREHENSION}",
                value_name,
            )
        if generator.iter.args or generator.iter.keywords:
            raise CompileError(
                f"items() is written {ITEMS_COMPREHENSION}", generator.iter
            )
        assert isinstance(generator.iter.func, ast.Attribute)
        source = self.operand(
            generator.iter.func.value,
            OBJECT,
            f"items() is a dict method: {ITEMS_COMPREHENSION}",
        )
        values = source.type.values if source.type else None
        with self.parameters({key_name.id: of(STRING), value_name.id: values}):
            tests, narrowed = self.conditions(generator.ifs)
            with self.narrowed(narrowed):
                key = self.expr(node.key)
                value = self.expr(node.value)
        self.check_hiding(
            {key_name.id: key_name, value_name.id: value_name}, [key, value, *tests]
        )
        # $sift and $each take the value first and the key second.
        spelled = [self.spelling(value_name.id), self.spelling(key_name.id)]
        if named(node.key, key_name.id) and named(node.value, value_name.id):
            if not tests:
                # Every key of a JSON object is a string already, so the
                # comprehension is the dict, and a JSON value is a copy.
                return replace(source, type=source.type or of(OBJECT))
            sifted = call("sift", [source, function(spelled, conjunction(tests))], None)
            return merged(sifted, values)
        self.json_key(node.key, key)
        body = entry(key, value)
        if tests:
            body = kept(conjunction(tests), body)
        return merged(call("each", [source, function(spelled, body)], None), value.type)

    def quantified(self, node: ast.Call, name: str) -> Expr:
        """any(xs) and all(xs) as $reduce over the items, whose function keeps
        the result once it is decided: any() stops at the first item that is
        true and all() at the first that is false. A generator expression
        writes its condition and its item into that function, so neither is
        evaluated for an item past the one that decided the result, as Python
        evaluates neither; a list comprehension written in the call builds the
        whole list first, as Python does. $reduce of an empty list gives the
        initial value, which is any()'s False and all()'s True."""
        if len(node.args) != 1:
            raise CompileError(f"{name}() takes one argument: {name}({EVERY})", node)
        argument = node.args[0]
        tests: list[Expr] = []
        if isinstance(argument, ast.GeneratorExp):
            generator = self.one_for(argument)
            if not isinstance(generator.target, ast.Name):
                if unpacking(generator.iter):
                    # Raise the advice of enumerate() or zip(), which says what
                    # to count with, rather than the message below.
                    self.expr(generator.iter)
                raise CompileError(
                    f"{name}() iterates one variable: {name}({EVERY})",
                    generator.target,
                )
            variable = generator.target.id
            source = self.iterated(generator.iter, f"{name}()", whole=True)
            item = source.type.items if source.type else None
            with self.parameters({variable: item}):
                tests, narrowed = self.conditions(generator.ifs)
                with self.narrowed(narrowed):
                    element = self.truth(self.expr(argument.elt))
            self.check_hiding({variable: generator.target}, [element, *tests])
            spelled = self.spelling(variable)
        else:
            source = self.iterated(argument, f"{name}()", whole=True)
            item = source.type.items if source.type else None
            spelled = self.parameter("x", [source])
            element = self.truth(expression("$" + spelled, type=item))
        # The item that decides the result is the one that differs from the
        # initial value: a true item for any(), a false one for all(). An item
        # a condition drops leaves the result as it is, which is that initial
        # value again.
        neutral = literal(name == "all")
        decided = literal(name == "any")
        element = grouped(element)
        if tests:
            element = grouped(
                conditional(conjunction(tests), element, neutral, of(BOOLEAN))
            )
        accumulator = unused("a", self.hides([source, element, *tests]) | {spelled})
        carried = expression("$" + accumulator, type=of(BOOLEAN), boolean=True)
        body = (
            conditional(carried, decided, element, of(BOOLEAN))
            if name == "any"
            else conditional(carried, element, decided, of(BOOLEAN))
        )
        reducer = function([accumulator, spelled], body)
        return call("reduce", [source, reducer, neutral], of(BOOLEAN), boolean=True)

    def one_for(
        self, node: ast.ListComp | ast.DictComp | ast.GeneratorExp
    ) -> ast.comprehension:
        if len(node.generators) != 1:
            # A comprehension clause has no position of its own; its variable
            # is where the second for is written.
            raise CompileError(
                "a comprehension takes one for; nest a for loop for more",
                node.generators[1].target,
            )
        return node.generators[0]

    def iterated(
        self, node: ast.expr, subject: str = "a comprehension", *, whole: bool = False
    ) -> Expr:
        """What a comprehension iterates: a list, or the keys of a dict. whole
        asks for a list even where the dict is empty, which $keys gives nothing
        for: $map and $filter of nothing give nothing, which the brackets
        around a comprehension turn into an empty list, while $reduce of
        nothing gives nothing in the place of its initial value."""
        source = self.expr(node)
        kind = self.known(
            node, source, "list", f"{subject} depends on what it iterates"
        )
        if kind == OBJECT:
            if whole:
                return keys_of(source)
            return call("keys", [source], of(ARRAY, items=of(STRING)))
        if kind != ARRAY:
            raise CompileError(
                f"{ast.unparse(node)} is {article(kind)}; {subject} iterates "
                "lists and the keys of dicts",
                node,
            )
        return source

    def json_key(self, node: ast.expr, value: Expr) -> None:
        """A key a dict comprehension writes into an object. JSON object keys
        are strings, and $string of an unknown type would write two Python
        keys as one, so a key of another type is rejected rather than
        converted: {x: x for x in [1, "1"]} has two entries in Python and
        would have one here."""
        if value.type is not None and value.type.kinds == {STRING}:
            return
        text = ast.unparse(node)
        subject = (
            f"the type of {text} is not known here"
            if value.type is None
            else f"{text} is {value.type.describe()}"
        )
        raise CompileError(
            f"JSON object keys are strings, and {subject}; write str({text})", node
        )

    def formatted(self, node: ast.JoinedStr) -> Expr:
        """An f-string as the pieces joined with &, each value through $string
        unless it is known to be a string. A value known while the file
        compiles becomes the text itself, which joins the text beside it."""
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
            spec = written_spec(value.format_spec)
            spelled = self.stringified(value.value)
            if spelled is not None:
                if spec is not None:
                    # Python gives the spec to the value itself, and a datetime
                    # reads it as a strftime format rather than a width.
                    advice = (
                        f"; write the datetime with strftime: {STRFTIME_WRITTEN}"
                        if self.datetime_moment(value.value) is not None
                        else ""
                    )
                    raise CompileError(
                        "a format spec here is a width or the digits of a "
                        "number, and Python gives this one to the value "
                        f"itself{advice}",
                        value.format_spec,
                    )
                pieces.append(spelled)
                continue
            part = self.expr(value.value)
            if spec is not None:
                pieces.append(self.formatted_value(part, value, spec))
                continue
            if part.type is None or part.type.kinds != {STRING}:
                part = text(part)
            pieces.append(part)
        pieces = collapsed(pieces)
        if not pieces:
            return literal("")
        result = pieces[0]
        for piece in pieces[1:]:
            result = binary(result, "&", piece, ADD, of(STRING))
        return result

    def formatted_value(self, part: Expr, node: ast.FormattedValue, spec: str) -> Expr:
        """A value with a format spec: the digits after the decimal point, or
        d, are the picture $formatNumber takes, and the width is $pad,
        negative where the text is pushed to the right, as it is for a number
        by default."""
        found = FORMAT_SPEC.fullmatch(spec)
        # The 0 before a width fills a whole number to it; for a string Python
        # reads the 0 as the fill, which is written out as a fill instead.
        if (
            found is None
            or not (found["width"] or found["precision"] or found["whole"])
            or (found["zero"] and not found["whole"])
        ):
            raise CompileError(
                "a format spec here is a width, with a fill and < or > before "
                "it, the digits of a number, or d for a whole one: "
                f"{SPEC_WRITTEN}",
                node.format_spec,
            )
        number = found["precision"] is not None or found["whole"]
        written = self.spec_value(part, node, found)
        # A whole number zero-padded by its picture is at its width already.
        if not found["width"] or (found["zero"] and not found["align"]):
            return written
        size = int(found["width"])
        # Python fills a string on the right and a number on the left.
        right = found["align"] == ">" or (found["align"] is None and number)
        width = literal(-size if right else size)
        fill = [literal(found["fill"])] if found["fill"] else []
        return call("pad", [written, width, *fill], of(STRING))

    def spec_value(
        self, part: Expr, node: ast.FormattedValue, found: re.Match[str]
    ) -> Expr:
        """The text a format spec fills to its width: a number written with
        the digits the spec asks for, or the string itself."""
        if found["precision"] is None and not found["whole"]:
            kind = self.known(node.value, part, "str", "a width pads a string")
            if kind != STRING:
                raise CompileError(
                    f"{ast.unparse(node.value)} is {article(kind)}, and a width "
                    "pads a string here; write a number with .2f or d, or "
                    "build the text from it",
                    node.value,
                )
            return part
        rule = (
            "d writes a whole number"
            if found["whole"]
            else "the digits format a number"
        )
        kind = self.known(node.value, part, "float", rule)
        if kind != NUMBER:
            raise CompileError(
                f"{ast.unparse(node.value)} is {article(kind)}, and {rule} here",
                node.value,
            )
        return call("formatNumber", [part, literal(number_picture(found))], of(STRING))

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
                f"{ast.unparse(node)} is {article(value.type.kind)}; ** unpacks dicts",
                node,
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
                f"{ast.unparse(node)} is {article(value.type.kind)}, and {operator} takes "
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
        if (
            isinstance(node.op, (ast.Add, ast.Sub))
            and self.datetime_moment(node) is not None
        ):
            raise CompileError(
                f"{ast.unparse(node)} is a datetime object, not JSON; write "
                "str(dt), dt.timestamp() or wait(until=dt)",
                node,
            )
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
            hint = written_kind(node.left) or written_kind(node.right) or "float"
            raise CompileError(
                unknown(node.left, hint, "+ adds numbers, joins strings or lists"),
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
        raise CompileError(
            f"+ takes numbers, strings or lists, not {article(kind)}", node
        )

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
                        f"{ast.unparse(operand)} is {article(value.type.kind)}; "
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
        if isinstance(right_node, ast.Constant) and isinstance(right_node.value, bool):
            # = compares a boolean only with a boolean, as is does: 0 = false is
            # false. A missing value makes = false, so is not negates it rather
            # than use !=, which is false for a missing value too.
            test = binary(left, "=", right, COMPARE, boolean, True)
            if isinstance(operator, ast.IsNot):
                return call("not", [test], boolean, boolean=True)
            return test
        if not (isinstance(right_node, ast.Constant) and right_node.value is None):
            raise CompileError(
                "is compares with None, True or False only; compare values with ==",
                right_node,
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
            f"{ast.unparse(right_node)} is {article(kind)}; in looks into lists, dicts "
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
                f"{ast.unparse(key)} is {article(key_kind)}; "
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
                f"{ast.unparse(node)} is {article(kind)}; slices take lists and strings",
                node,
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
        elif not start.back:
            size = (
                difference(length, sum_of(upper.amount, start.amount))
                if upper.back
                else difference(upper.amount, start.amount)
            )
        else:
            # $substring starts a count back past the beginning at the
            # beginning and still takes the whole size, where Python takes
            # only the characters before the end: s[-a:-b] holds a - b
            # characters, s[-a:b] those between a from the end and b, and
            # either holds no more than the characters before its end.
            size = (
                difference(start.amount, upper.amount)
                if upper.back
                else difference(sum_of(upper.amount, start.amount), length)
            )
            before = difference(length, upper.amount) if upper.back else upper.amount
            if not (type(size.template) is int and size.template <= 0):
                size = call("min", [array([size, before])], of(NUMBER))
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
                f"{ast.unparse(node)} is {article(value.type.describe())}; {rule}", node
            )

    def error_name(self, node: ast.Call) -> Expr:
        """type(e).__name__ of a caught error: the Error of its error output,
        which is the name Python gives its class, or the name the class
        declares with error = "..."."""
        if len(node.args) != 1 or node.keywords:
            raise CompileError("type() takes one argument: type(e).__name__", node)
        caught = self.expr(node.args[0])
        if caught.type != ERROR_OUTPUT:
            raise CompileError(
                "type(x).__name__ reads the name of a caught error here: "
                "except Exception as e: ... type(e).__name__",
                node.args[0],
            )
        return field(caught, "Error")

    def call(self, node: ast.Call) -> Expr:
        target = qualified(node.func, self.names) or ""
        if target.startswith("sfnx.context."):
            # A method of the Context Object, which is a dict.
            target = ""
        if target == "sfnx.task":
            return self.task_call(node)
        if target == "sfnx.activity":
            return self.activity_call(node)
        if target.startswith("sfnx.aws."):
            return self.operation_call(node, target)
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
        if target == TIMEDELTA:
            raise CompileError(
                f"{ast.unparse(node)} is a timedelta object, not JSON; add it to "
                "a datetime or take it from one, or write "
                f"{TIMEDELTA_WRITTEN}.total_seconds()",
                node,
            )
        if target in HASHES:
            raise CompileError(
                f"{ast.unparse(node.func)}() is a hash object, not JSON; write "
                f"{ast.unparse(node.func)}(s.encode()).hexdigest()",
                node,
            )
        if target in BASE64:
            raise CompileError(
                f"{ast.unparse(node.func)}() is bytes, not JSON; write "
                f"{BASE64_WRITTEN[target]}",
                node,
            )
        if target in {UNQUOTE, UNQUOTE_PLUS}:
            name = target.rpartition(".")[2]
            if len(node.args) != 1 or node.keywords:
                raise CompileError(f"{name}() takes one string: {name}(s)", node)
            escaped = self.operand(node.args[0], STRING, f"{name}() reads a string")
            if target == UNQUOTE:
                escaped = call(
                    "replace", [escaped, literal("+"), literal("%2B")], of(STRING)
                )
            return call("decodeUrlComponent", [escaped], of(STRING))
        if target == "itertools.batched":
            raise CompileError(
                "itertools.batched() is a list here only as "
                "list(itertools.batched(items, n))",
                node,
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr == "hexdigest":
            return self.hexdigest(node, node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "decode":
            return self.decoded(node, node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "timestamp":
            return self.timestamp(node, node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "total_seconds":
            return self.total_seconds(node, node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "strftime":
            return self.strftime(node, node.func)
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
        if (
            not target
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "items"
        ):
            # d.items() gives a loop or a dict comprehension its two variables
            # and is nothing else.
            raise CompileError(
                "items() gives two variables: for k, v in d.items() or "
                f"{ITEMS_COMPREHENSION}",
                node,
            )
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
        if name in {"all", "any"}:
            return self.quantified(node, name)
        if name == "range":
            return self.range_list(node)
        if name == "reversed":
            if len(node.args) != 1:
                raise CompileError("reversed() takes one argument: reversed(xs)", node)
            listed = self.listed(node.args[0], name)
            return call("reverse", [listed], listed.type)
        if name == "enumerate":
            raise CompileError(
                "enumerate() is only for a for loop: for i, item in enumerate(items)",
                node,
            )
        if name == "zip":
            raise CompileError(
                "zip() is a list here only as list(zip(a, b)), or a loop: "
                "for a, b in zip(xs, ys)",
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
        if (
            isinstance(node, ast.Call)
            and (qualified(node.func, self.names) or "") == "uuid.uuid4"
            and not node.args
            and not node.keywords
        ):
            return call("uuid", [], of(STRING))
        return self.datetime_string(node)

    def datetime_string(
        self, node: ast.expr, picture: Expr | None = None
    ) -> Expr | None:
        """A datetime expression as the text it is written as, which is what
        str(), an f-string, wait(until=) and strftime() take: $now() for the
        moment itself, $fromMillis of the milliseconds for any other. Both
        take the picture strftime() gives, and write the timestamp without
        one."""
        moment = self.datetime_moment(node)
        if moment is None:
            return None
        made, shift = moment
        target = qualified(made.func, self.names) or ""
        written = [picture] if picture is not None else []
        if not shift and target == NOW:
            return call("now", written, of(STRING))
        millis = shifted(self.millis(made, target), shift)
        return call("fromMillis", [millis, *written], of(STRING))

    def strftime(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """dt.strftime(format) as the datetime written with the picture string
        that writes what the format writes."""
        if len(node.args) != 1 or node.keywords:
            raise CompileError(f"strftime() is written {STRFTIME_WRITTEN}", node)
        template = node.args[0]
        if not (isinstance(template, ast.Constant) and isinstance(template.value, str)):
            raise CompileError(
                "the format of strftime() is a literal string, as the picture "
                f"string it becomes is built here: {STRFTIME_WRITTEN}",
                template,
            )
        written = self.datetime_string(method.value, literal(picture(template)))
        if written is None:
            raise CompileError(f"strftime() is written {STRFTIME_WRITTEN}", node)
        return written

    def datetime_moment(self, node: ast.expr) -> tuple[ast.Call, int] | None:
        """The call a datetime expression is made by and the milliseconds the
        timedeltas around it move it, or None where the expression is not a
        datetime. Nothing is translated here, so the expression it recognizes
        is translated once, where it is read."""
        if isinstance(node, ast.Call):
            target = qualified(node.func, self.names) or ""
            if target not in DATETIMES:
                return None
            arity = STRINGIFIED[target][0]
            if len(node.args) != arity or node.keywords:
                return None
            return node, 0
        if not isinstance(node, ast.BinOp) or not isinstance(
            node.op, (ast.Add, ast.Sub)
        ):
            return None
        moment, span = (
            self.datetime_moment(node.left),
            self.timedelta_millis(node.right),
        )
        if moment is None and isinstance(node.op, ast.Add):
            # timedelta + datetime names the same moment as datetime + timedelta.
            moment = self.datetime_moment(node.right)
            span = self.timedelta_millis(node.left)
        if moment is None or span is None:
            return None
        made, shift = moment
        return made, shift + (span if isinstance(node.op, ast.Add) else -span)

    def datetime_millis(self, node: ast.expr) -> Expr | None:
        """A datetime expression as the milliseconds since the epoch, moved by
        the timedeltas written around it."""
        moment = self.datetime_moment(node)
        if moment is None:
            return None
        made, shift = moment
        millis = self.millis(made, qualified(made.func, self.names) or "")
        return shifted(millis, shift)

    def timedelta_millis(self, node: ast.expr) -> int | None:
        """The whole milliseconds a timedelta() call spans, or None where the
        expression is not one. The units are written in the source and added
        up here, so one number goes into the expression."""
        if not isinstance(node, ast.Call):
            return None
        if (qualified(node.func, self.names) or "") != TIMEDELTA:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "timedelta"
                and node.func.id not in self.names
                and not self.is_function(node.func.id)
            ):
                raise CompileError(
                    "timedelta is not imported; write from datetime import timedelta",
                    node.func,
                )
            return None
        if node.args:
            raise CompileError(
                f"timedelta takes its units by name here: {TIMEDELTA_WRITTEN}", node
            )
        units: dict[str, int | float] = {}
        for keyword in node.keywords:
            if keyword.arg == "microseconds":
                raise CompileError(
                    "Step Functions keeps time to the millisecond, so timedelta "
                    "takes no microseconds here",
                    keyword.value,
                )
            if keyword.arg not in TIMEDELTA_UNITS:
                raise CompileError(
                    f"timedelta takes {spoken(list(TIMEDELTA_UNITS))} here: "
                    f"{TIMEDELTA_WRITTEN}",
                    keyword.value if keyword.arg else node,
                )
            units[keyword.arg] = written_unit(keyword)
        try:
            span = datetime.timedelta(**units)
        except OverflowError:
            raise CompileError(
                f"{ast.unparse(node)} is longer than a timedelta holds", node
            ) from None
        microseconds = span // datetime.timedelta(microseconds=1)
        if microseconds % 1000:
            raise CompileError(
                f"Step Functions keeps time to the millisecond, and "
                f"{ast.unparse(node)} is a fraction of one",
                node,
            )
        return microseconds // 1000

    def total_seconds(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """A timedelta's .total_seconds(): the seconds a written timedelta
        spans, or the milliseconds between two datetimes divided."""
        span = method.value
        if not node.args and not node.keywords:
            written = self.timedelta_millis(span)
            if written is not None:
                return literal(
                    written // 1000 if written % 1000 == 0 else written / 1000
                )
            if isinstance(span, ast.BinOp) and isinstance(span.op, ast.Sub):
                later = self.datetime_millis(span.left)
                earlier = self.datetime_millis(span.right)
                if later is not None and earlier is not None:
                    between = binary(later, "-", earlier, ADD, of(NUMBER))
                    return binary(between, "/", literal(1000), MULTIPLY, of(NUMBER))
        raise CompileError(
            "total_seconds() is written (dt - dt2).total_seconds() or "
            f"{TIMEDELTA_WRITTEN}.total_seconds()",
            node,
        )

    def timedelta_span(self, node: ast.expr) -> bool:
        """Whether an expression makes a timedelta: a timedelta() call, or one
        datetime taken from another."""
        if isinstance(node, ast.Call):
            return (qualified(node.func, self.names) or "") == TIMEDELTA
        return (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and self.datetime_moment(node.left) is not None
            and self.datetime_moment(node.right) is not None
        )

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
        if node.args or node.keywords:
            raise CompileError(f"timestamp() is written {written}", node)
        if isinstance(made, ast.Call):
            target = qualified(made.func, self.names) or ""
            if not target and isinstance(made.func, ast.Attribute):
                self.check_module_import(made.func)
            if target in DATETIMES:
                arity, spelled, _ = STRINGIFIED[target]
                if len(made.args) != arity or made.keywords:
                    raise CompileError(f"write {spelled}", made)
                if target == FROM_TIMESTAMP:
                    return self.numeric(made.args[0], "datetime.fromtimestamp()")
        millis = self.datetime_millis(made)
        if millis is None:
            raise CompileError(f"timestamp() is written {written}", node)
        return binary(millis, "/", literal(1000), MULTIPLY, of(NUMBER))

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
        return function([first, second], test)

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
                # $pad takes 6.5 as 6, where Python raises.
                raise CompileError(
                    f"the width of {name}() is a whole number of 0 or more",
                    arguments[0],
                )
            if written_number(width) is None:
                # $pad fills on the other side for a width below 0, where
                # Python leaves the text as it is.
                width = call("max", [array([width, literal(0)])], of(NUMBER))
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
                    f"{ast.unparse(arguments[0])} is {article(items.type.describe())}; "
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
            f"{ast.unparse(node)} is {article(kind)}; {name}() takes a dict, a list or a "
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

    def decoded(self, node: ast.Call, method: ast.Attribute) -> Expr:
        """base64.b64encode(s.encode()).decode() as $base64encode and
        base64.b64decode(s).decode() as $base64decode: bytes are not JSON, so
        the text is encoded and decoded in the same expression."""
        made = method.value
        if not isinstance(made, ast.Call) or node.args or node.keywords:
            raise CompileError(f"decode() is written {BASE64_ANY}", node)
        target = qualified(made.func, self.names) or ""
        if not target and ast.unparse(made.func) in MODULE_IMPORTS:
            raise CompileError("base64 is not imported; write import base64", made.func)
        if target not in BASE64 or made.keywords or len(made.args) != 1:
            raise CompileError(f"decode() is written {BASE64_ANY}", node)
        written = BASE64_WRITTEN[target]
        (argument,) = made.args
        if target == "base64.b64decode":
            text = self.operand(
                argument, STRING, f"b64decode() reads a string: {written}"
            )
        elif (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "encode"
            and not argument.args
            and not argument.keywords
        ):
            text = self.operand(
                argument.func.value, STRING, "encode() is a string method"
            )
        else:
            raise CompileError(f"b64encode() is written {written}", made)
        return call(BASE64[target], [text], of(STRING))

    def affix(self, text: Expr, node: ast.expr, name: str) -> Expr:
        """s.startswith(p) and s.endswith(p): the part of s as long as p,
        compared with p. A string holding a ${Name} placeholder is measured
        where it runs, since the deployment writes another text in its place."""
        affix = self.operand(node, STRING, f"{name}() compares with a string")
        template = affix.template
        spelled = (
            template
            if isinstance(template, str)
            and affix.code == string(template)
            and "${" not in template
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
                f"{ast.unparse(node)} is {article(value.type.kind)}; {rule}", node
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
        resource, called = self.resource(node.args[0])
        arguments = node.args[1] if len(node.args) == 2 else None
        return self.task_state(node, resource, called, arguments, node.keywords)

    def activity_call(self, node: ast.Call) -> Expr:
        """activity(resource, input, timeout=, heartbeat=, retry=): a Task that
        waits for a worker of the activity to send back its result."""
        self.admit(node, "activity", "Task")
        if not node.args or len(node.args) > 2:
            raise CompileError(
                "activity takes the activity ARN and its input: "
                'activity("arn:aws:states:<region>:<account>:activity:review", {...})',
                node,
            )
        resource, called = self.resource(node.args[0])
        if called.kind == "substituted":
            called = replace(called, name="activity")
        elif called.kind != "activity":
            raise CompileError(
                "activity takes an activity ARN, "
                "arn:aws:states:<region>:<account>:activity:<name>, or a ${...} "
                "filled in when the machine is deployed; call other resources "
                "with task() or aws.sdk",
                node.args[0],
            )
        for keyword in node.keywords:
            if keyword.arg not in {"timeout", "heartbeat", "retry"}:
                raise CompileError(
                    "activity takes timeout=, heartbeat= and retry=; put what "
                    "the worker reads in its input",
                    keyword,
                )
        arguments = node.args[1] if len(node.args) == 2 else None
        return self.task_state(node, resource, called, arguments, node.keywords)

    def operation_call(self, node: ast.Call, target: str) -> Expr:
        """aws.sdk.<service>.<operation>(...) and aws.optimized.<service>.
        <operation>(...): a Task calling the operation, whose keyword arguments
        in PascalCase are its Arguments."""
        segments = target.split(".")
        if not (
            len(segments) == 5
            and segments[2] in {"sdk", "optimized"}
            and segments[4] != "errors"
        ):
            raise CompileError(
                "call an operation of a service: aws.sdk.<service>.<operation>"
                "(...), such as aws.sdk.dynamodb.update_item(...), or "
                "aws.optimized.<service>.<operation>(...)",
                node,
            )
        self.admit(node, ast.unparse(node.func), "Task")
        suffix, pattern = integration_pattern(node)
        try:
            resource = operation_resource(*segments[2:])
        except ResourceError as exc:
            raise CompileError(str(exc), node.func) from None
        try:
            called = integration(resource + suffix)
        except ResourceError as exc:
            # The operation's own ARN is valid, so only the pattern can fail.
            raise CompileError(str(exc), pattern) from None
        options = [k for k in node.keywords if k.arg in {*TASK_OPTIONS, "retry"}]
        return self.task_state(
            node, resource + suffix, called, operation_arguments(node), options
        )

    def resource(self, node: ast.expr) -> tuple[str, Integration]:
        """The resource ARN of a task() or an activity(), written literally or
        as a name assigned outside the machine."""
        resource_node = self.holds(node)
        if not (
            isinstance(resource_node, ast.Constant)
            and isinstance(resource_node.value, str)
        ):
            raise CompileError(
                "the resource is a literal ARN string, as Step Functions requires",
                resource_node,
            )
        try:
            return resource_node.value, integration(resource_node.value)
        except ResourceError as exc:
            raise CompileError(str(exc), resource_node) from exc

    def task_state(
        self,
        node: ast.Call,
        resource: str,
        called: Integration,
        arguments_node: ast.expr | None,
        keywords: list[ast.keyword],
    ) -> Expr:
        # Nothing inside the call makes a Task of its own.
        self.accepts_task = False
        try:
            arguments, options = self.task_arguments(
                node, called, arguments_node, keywords
            )
        finally:
            self.accepts_task = True
        state: dict[str, object] = {"Type": "Task", "Resource": resource}
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
        self,
        node: ast.Call,
        called: Integration,
        arguments_node: ast.expr | None,
        keywords: list[ast.keyword],
    ) -> tuple[Expr | None, dict[str, Expr]]:
        arguments = None
        callback = called.pattern == ".waitForTaskToken"
        if arguments_node is not None:
            self.token_readable, self.token_read = callback, False
            try:
                arguments = self.expr(arguments_node)
            finally:
                self.token_readable = False
            check_arguments(arguments_node, called)
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
        for keyword in keywords:
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
            f"{ast.unparse(node)} is {article(kind)}; len takes lists, strings and dicts",
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


def written_kind(node: ast.expr) -> str | None:
    """The annotation a list or a string written in the source suggests for
    an operand of + that may be it: list for x or [], str for x if c else "".
    A number is not looked for, as float is the default."""
    if isinstance(node, ast.BoolOp):
        choices = node.values
    elif isinstance(node, ast.IfExp):
        choices = [node.body, node.orelse]
    else:
        return None
    for choice in choices:
        if isinstance(choice, (ast.List, ast.ListComp)):
            return "list"
        if isinstance(choice, ast.JoinedStr) or (
            isinstance(choice, ast.Constant) and isinstance(choice.value, str)
        ):
            return "str"
    return None


def direct_call(name: str) -> str:
    """The message for calling a function of the module inside an
    expression."""
    return (
        f"{name}() runs its body here, as states; call it on its own line, "
        f"assign its result or return it: result = {name}(...)"
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
    Each says where it is taken instead, which the message about unpacking
    would hide."""
    return unpacked(node) in UNPACKING


def unpacked(node: ast.expr) -> str | None:
    """What gives a loop its two variables: enumerate, zip or items (the dict
    method), or None for any other iterable."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name) and node.func.id in UNPACKING:
        return node.func.id
    if isinstance(node.func, ast.Attribute) and node.func.attr == "items":
        return "items"
    return None


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
            f"{name} is a number of seconds, not {article(value.type.describe())}", node
        )


def integration_pattern(node: ast.Call) -> tuple[str, ast.expr | None]:
    """pattern=".sync", ".sync:2" or ".waitForTaskToken" of an operation call,
    which ends its resource ARN, and the value written for it."""
    value = next((k.value for k in node.keywords if k.arg == "pattern"), None)
    if value is None:
        return "", None
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        raise CompileError(
            "pattern is a literal string, as the resource ARN ends with it: "
            'pattern=".waitForTaskToken"',
            value,
        )
    return value.value, value


def operation_arguments(node: ast.Call) -> ast.expr:
    """The Arguments of an operation call: its keyword arguments in PascalCase,
    and the dicts ** unpacks, as a dict written in their place. A dict unpacked
    on its own is the Arguments as it is, as task(resource, arguments) takes
    it."""
    if node.args:
        raise CompileError(
            "pass the API parameters by name, in PascalCase: TableName=...",
            node.args[0],
        )
    keys: list[ast.expr | None] = []
    values: list[ast.expr] = []
    for keyword in node.keywords:
        if keyword.arg is None:
            keys.append(None)
        elif keyword.arg in {*TASK_OPTIONS, "retry", "pattern"}:
            continue
        elif keyword.arg[0].isupper():
            keys.append(ast.copy_location(ast.Constant(keyword.arg), keyword))
        else:
            raise CompileError(
                "API parameters are PascalCase, and timeout=, heartbeat=, "
                f"role=, retry= and pattern= set the Task; {keyword.arg} is "
                "neither",
                keyword,
            )
        values.append(keyword.value)
    if keys == [None]:
        return values[0]
    return ast.copy_location(ast.Dict(keys, values), node)


def retry_option(node: ast.Call) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == "retry"), None)


def text(value: Expr) -> Expr:
    """str(x), and what an f-string or a raise makes of a value: the message
    of a caught exception, the text a value known here is written as, or the
    value through $string."""
    if value.type == ERROR_OUTPUT:
        return field(value, "Cause")
    written = known_text(value)
    if written is not None:
        return literal(written)
    return call("string", [value], of(STRING))


def collapsed(pieces: list[Expr]) -> list[Expr]:
    """The pieces of an f-string with neighbouring ones whose text is known
    written as one string, as they would be written by hand."""
    result: list[Expr] = []
    for piece in pieces:
        known = known_text(piece)
        before = known_text(result[-1]) if result else None
        if known is None or before is None:
            result.append(piece)
            continue
        result[-1] = literal(before + known)
    return result


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


def written_unit(keyword: ast.keyword) -> int | float:
    """The number a unit of timedelta is given, written in the source."""
    node = keyword.value
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node, sign = node.operand, -1
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return sign * node.value
    raise CompileError(
        f"the units of timedelta are numbers written here: {TIMEDELTA_WRITTEN}; "
        "for a span the input carries, write "
        "datetime.fromtimestamp(dt.timestamp() + seconds)",
        keyword.value,
    )


def number_picture(found: re.Match[str]) -> str:
    """The picture $formatNumber writes a number with: the digits after the
    decimal point the spec asks for, or, for a whole number filled with zeros
    to its width, that width with the sign inside it, which is a picture of
    its own after the ; for a negative number."""
    if not found["whole"]:
        picture = "#,##0" if found["grouping"] else "0"
        places = int(found["precision"])
        return picture + "." + "0" * places if places else picture
    size = int(found["width"] or 0)
    if not found["zero"] or found["align"] or size < 2:
        return "0"
    return "0" * size + ";-" + "0" * (size - 1)


def written_spec(spec: ast.expr | None) -> str | None:
    """The format spec of an f-string field as it is written, or None where
    the field has none. One holding a field of its own is rejected: the width
    is read where the file compiles, not where it runs."""
    if spec is None:
        return None
    assert isinstance(spec, ast.JoinedStr)
    if not spec.values:
        return None
    written = spec.values[0]
    if len(spec.values) == 1 and isinstance(written, ast.Constant):
        assert isinstance(written.value, str)
        return written.value
    raise CompileError(
        f"the width of a format spec is written in the source: {SPEC_WRITTEN}", spec
    )


def picture(node: ast.Constant) -> str:
    """The picture string that writes what a strftime format writes. The text
    between the directives is literal, where the picture syntax reads [ and ]
    as the ends of a component, so each is written twice."""
    template = node.value
    assert isinstance(template, str)
    written = []
    position = 0
    while position < len(template):
        character = template[position]
        if character != "%":
            written.append({"[": "[[", "]": "]]"}.get(character, character))
            position += 1
            continue
        directive = template[position + 1 : position + 2]
        position += 2
        if directive == "%":
            written.append("%")
        elif directive in DIRECTIVES:
            written.append(DIRECTIVES[directive])
        else:
            named = f"%{directive}" if directive else "the % at the end"
            raise CompileError(
                f"strftime() takes {spoken(['%' + d for d in DIRECTIVES] + ['%%'])} "
                f"here, not {named}{UNPICTURED.get(directive, '')}; jsonata() "
                "reaches the picture strings $fromMillis takes",
                node,
            )
    return "".join(written)


def parenthesized(node: ast.expr) -> str:
    """An expression as it is written, in parentheses where a method called on
    it would otherwise read as a method of its last operand."""
    written = ast.unparse(node)
    return written if isinstance(node, ast.Call) else f"({written})"


def shifted(moment: Expr, millis: int) -> Expr:
    """A moment moved by whole milliseconds. A span that runs backwards is
    subtracted rather than added as a negative number, so the expression reads
    as the time it names, and a span of nothing leaves the moment as it is."""
    if millis >= 0:
        return sum_of(moment, literal(millis))
    return difference(moment, literal(-millis))


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


def conjunction(tests: list[Expr]) -> Expr:
    """The conditions of a comprehension as one: [x for x in xs if a if b]
    keeps the items where both hold."""
    test = tests[0]
    for more in tests[1:]:
        test = binary(test, "and", more, AND, of(BOOLEAN), True)
    return test


def named(node: ast.expr, name: str) -> bool:
    """Whether an expression is that name read as it is."""
    return isinstance(node, ast.Name) and node.id == name


def function(parameters: list[str], body: Expr) -> Expr:
    """The function $filter, $map, $sift, $each and $sort take, of one
    parameter or of two."""
    spelled = ", ".join("$" + parameter for parameter in parameters)
    return expression(
        f"function({spelled}) {{ {body.code} }}",
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


def known_text(value: Expr) -> str | None:
    """The text a value becomes where the value is known while the file
    compiles, so that the text itself stands in the definition rather than a
    call that always writes it. A float is left to $string, which writes one
    as JavaScript does rather than as Python does, and so is an integer past
    2^53, whose double is not the number in the source."""
    written = written_text(value)
    if written is not None:
        return written
    template = value.template
    if type(template) is bool:
        return "true" if template else "false"
    if template is None:
        return "null"
    number = written_number(value)
    if type(number) is int and abs(number) < 2**53:
        return str(number)
    return None


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
