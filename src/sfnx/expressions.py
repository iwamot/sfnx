"""JSONata expressions and the JSON templates that carry them in state fields."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sfnx.jsontypes import (
    ARRAY,
    BOOLEAN,
    NULL,
    NUMBER,
    OBJECT,
    STRING,
    Type,
    of,
    union,
)

# JSONata binding powers. A subexpression is parenthesized when it binds
# looser than the place it is put in; an expression written in jsonata() may
# bind as loosely as any.
WRITTEN = 0
CONDITIONAL = 20
OR = 25
AND = 30
COMPARE = 40
ADD = 50
MULTIPLY = 60
UNARY = 70
ATOM = 80

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names JSONata reads as literals, which cannot be path steps.
LITERALS = frozenset({"true", "false", "null"})

# Functions the generated expressions call. A variable with one of these names
# would hide the function from every later state.
FUNCTIONS = frozenset(
    {
        "abs",
        "append",
        "average",
        "base64decode",
        "base64encode",
        "boolean",
        "ceil",
        "contains",
        "count",
        "decodeUrlComponent",
        "distinct",
        "each",
        "error",
        "exists",
        "filter",
        "floor",
        "formatNumber",
        "fromMillis",
        "hash",
        "join",
        "keys",
        "length",
        "lookup",
        "lowercase",
        "map",
        "max",
        "merge",
        "millis",
        "min",
        "not",
        "now",
        "number",
        "pad",
        "parse",
        "partition",
        "power",
        "random",
        "range",
        "reduce",
        "replace",
        "reverse",
        "round",
        "sift",
        "sort",
        "split",
        "sqrt",
        "string",
        "substring",
        "sum",
        "toMillis",
        "trim",
        "type",
        "uppercase",
        "uuid",
        "zip",
    }
)

# The functions that give another value on every call.
VOLATILE = frozenset({"millis", "now", "random", "uuid"})
# How a value may change when it is evaluated again: as the time or a random
# value does, or as a jsonata() expression may, whose text is not read.
# The functions and operators that fail for no value given them.
TOTAL = frozenset({"exists", "type", "not", "boolean", "count", "keys", "append"})
# Functions that give a value for any argument, undefined included: 0 and
# false (measured).
DEFINED = frozenset({"exists", "count"})
# & writes any value as text, so it fails for none.
TOTAL_OPERATORS = frozenset({"=", "!=", "in", "and", "or", "&"})
# The integers a double holds exactly, which JSONata computes with as Python
# does.
EXACT = 2**53
CHANGES = 1
OPAQUE = 2


@dataclass(frozen=True)
class Expr:
    """One value: JSONata source, and the template that writes it into a state.

    A literal's template is the JSON itself (`"Assign": {"fee": 100}`); anything
    else is a `{% ... %}` string, and objects and arrays mix the two. type is
    what is known about the value; boolean says the code always yields a JSON
    boolean, so a condition can use it without $boolean. constructor says the
    code is an array constructor, which one around it keeps as one element.
    volatile says the code may give another value when it is evaluated again,
    as $random() and $uuid() do, so what writes it twice binds it once first:
    CHANGES, or OPAQUE where a jsonata() expression is in it, 0 otherwise.
    defined says the code never gives undefined, which fails an Assign or an
    Output but passes through a test such as $type() without failing: a
    literal, a variable, which no Assign leaves undefined, d.get(), $exists(),
    $count(), $append() of such a value, a comprehension, and lists and dicts
    of such values. total says evaluating the code fails for no value: a
    literal, a variable, a path step, $exists(), $type(), $not(), $boolean(),
    $count(), $keys(), $append(), =, !=, in, and and or, and conditionals,
    blocks, lists, dicts and comprehensions of them.
    """

    code: str
    template: object
    variables: frozenset[str] = frozenset()
    precedence: int = ATOM
    type: Type | None = None
    boolean: bool = False
    constructor: bool = False
    volatile: int = 0
    defined: bool = False
    total: bool = False


def expression(
    code: str,
    variables: frozenset[str] = frozenset(),
    precedence: int = ATOM,
    type: Type | None = None,
    boolean: bool = False,
    constructor: bool = False,
    volatile: int = 0,
    defined: bool = False,
    total: bool = False,
) -> Expr:
    return Expr(
        code,
        "{% " + code + " %}",
        variables,
        precedence,
        type,
        boolean,
        constructor,
        volatile,
        defined,
        total,
    )


def spellings(identifiers: frozenset[str]) -> dict[str, str]:
    """The Step Functions variable, or JSONata parameter, for each Python name
    that cannot be one as it is. Step Functions variable names do not start
    with _, so the underscores go (a digit or nothing left starts with value);
    a variable hides $states and the JSONata function of its name from every
    later state, so those names get _val. A name the module already uses, or
    one given to another name, is numbered."""
    taken = set(identifiers)
    found: dict[str, str] = {}
    for name in sorted(identifiers):
        base = name.lstrip("_")
        if not base or base[0].isdigit():
            base = "value" + (f"_{base}" if base else "")
        if base in FUNCTIONS or base == "states":
            base += "_val"
        if base == name:
            continue
        spelled = base
        serial = 1
        while spelled in taken:
            serial += 1
            spelled = f"{base}_{serial}"
        taken.add(spelled)
        found[name] = spelled
    return found


def spelling(name: str, spelled: Mapping[str, str]) -> str:
    return spelled.get(name, name)


def variable(name: str, spelled: Mapping[str, str], type: Type | None = None) -> Expr:
    return expression(
        "$" + spelling(name, spelled),
        frozenset({name}),
        type=type,
        defined=True,
        total=True,
    )


def literal(value: object) -> Expr:
    """A JSON scalar. A string that opens or closes like a `{% %}` template is
    written as an expression: Step Functions would evaluate it, or reject a
    half-open one."""
    if isinstance(value, str):
        code = string(value)
        if value.startswith("{%") or value.endswith("%}"):
            return expression(code, type=of(STRING), defined=True, total=True)
        return Expr(code, value, type=of(STRING), defined=True, total=True)
    if isinstance(value, bool):
        return Expr(
            json.dumps(value),
            value,
            type=of(BOOLEAN),
            boolean=True,
            defined=True,
            total=True,
        )
    if value is None:
        return Expr("null", None, type=of(NULL), defined=True, total=True)
    assert isinstance(value, (int, float))
    if not math.isfinite(value):
        raise ValueError("JSON has no infinite numbers")
    precedence = UNARY if value < 0 else ATOM
    return Expr(
        json.dumps(value),
        value,
        precedence=precedence,
        type=of(NUMBER),
        defined=True,
        total=True,
    )


def string(value: str) -> str:
    """A JSONata string literal. JSONata has no \\' escape, so a string with a
    single quote keeps the JSON double quotes."""
    escaped = json.dumps(value, ensure_ascii=False)
    if "'" in value:
        return escaped
    return "'" + escaped[1:-1].replace('\\"', '"') + "'"


def uses(values: list[Expr]) -> frozenset[str]:
    return frozenset().union(*(value.variables for value in values))


def changes(values: list[Expr]) -> int:
    """How a value among values may differ when it is evaluated again."""
    return max((value.volatile for value in values), default=0)


def array(items: list[Expr]) -> Expr:
    item_type: Type | None = None
    if items:
        item_type = items[0].type
        for item in items[1:]:
            item_type = union(item_type, item.type)
    return Expr(
        "[" + ", ".join(element(item) for item in items) + "]",
        [item.template for item in items],
        uses(items),
        type=Type(frozenset({ARRAY}), item_type, empty=not items),
        constructor=True,
        volatile=changes(items),
        defined=all(item.defined for item in items),
        total=all(item.total for item in items),
    )


def element(item: Expr) -> str:
    """The code of an item in an array constructor. JSONata merges the items of
    an array into the one around it unless the item is written as a constructor
    itself, and `[$xs]` holds the items of $xs, so `[[$xs]]` holds $xs. An item
    that may or may not be an array is tested when it is evaluated; a value that
    is not a constructor is merged, so an array is wrapped once more for it."""
    if item.constructor or (item.type is not None and ARRAY not in item.type.kinds):
        return item.code
    if item.type is not None and item.type.kinds == {ARRAY}:
        return "[" + item.code + "]"
    kind = call("type", [item], of(STRING))
    test = binary(kind, "=", literal("array"), COMPARE, of(BOOLEAN), True)
    wrapped = Expr(
        "[[" + item.code + "]]", None, item.variables, volatile=item.volatile
    )
    return conditional(test, wrapped, item, item.type).code


def obj(entries: list[tuple[str, Expr]]) -> Expr:
    """An object literal, typed by its keys, and by what its values may be for
    a key only known when it runs."""
    values: Type | None = None
    for position, (_, value) in enumerate(entries):
        values = value.type if position == 0 else union(values, value.type)
    return Expr(
        "{" + ", ".join(f"{string(k)}: {v.code}" for k, v in entries) + "}",
        {k: v.template for k, v in entries},
        uses([v for _, v in entries]),
        type=Type(
            frozenset({OBJECT}),
            values=values,
            fields=tuple((k, v.type) for k, v in entries),
        ),
        volatile=changes([v for _, v in entries]),
        defined=all(v.defined for _, v in entries),
        total=all(v.total for _, v in entries),
    )


def operand(value: Expr, precedence: int) -> str:
    """The code of value, parenthesized if it binds looser than precedence."""
    return value.code if value.precedence >= precedence else f"({value.code})"


def call(
    function: str, arguments: list[Expr], type: Type | None, boolean: bool = False
) -> Expr:
    assert function in FUNCTIONS
    if function == "count" and len(arguments) == 1:
        items = arguments[0].template
        if isinstance(items, list) and written(items):
            # The length of a list written in the source, as a hand-writer
            # writes 2 for the regions they list.
            return literal(len(items))
    if function == "length" and len(arguments) == 1:
        text = written_scalar(arguments[0])
        if isinstance(text, str) and "${" not in text:
            # The length of a string written in the source, in code points
            # as Python counts them. A ${Name} placeholder is measured where
            # it runs, since the deployment writes another text in its place.
            return literal(len(text))
    if function == "append" and len(arguments) == 2:
        first, second = (a.template for a in arguments)
        if isinstance(first, list) and isinstance(second, list):
            joined = first + second
            if written(joined):
                # Lists written in the source joined, as a hand-writer writes
                # the one list; JSON is JSONata that means the same.
                code = json.dumps(joined, ensure_ascii=False)
                return Expr(
                    code, joined, type=type, constructor=True, defined=True, total=True
                )
    code = f"${function}(" + ", ".join(a.code for a in arguments) + ")"
    volatile = max(CHANGES if function in VOLATILE else 0, changes(arguments))
    return expression(
        code,
        uses(arguments),
        type=type,
        boolean=boolean,
        volatile=volatile,
        # $append of nothing and a value gives the value (measured).
        defined=function in DEFINED
        or (function == "append" and any(a.defined for a in arguments)),
        total=function in TOTAL and all(a.total for a in arguments),
    )


def written(template: object) -> bool:
    """Whether a template is a value written out, with no expression in it."""
    if isinstance(template, dict):
        return all(written(v) for v in template.values())
    if isinstance(template, list):
        return all(written(v) for v in template)
    return not (isinstance(template, str) and template.startswith("{%"))


def binary(
    left: Expr,
    operator: str,
    right: Expr,
    precedence: int,
    type: Type | None,
    boolean: bool = False,
) -> Expr:
    """left operator right. JSONata operators associate to the left, so an
    equally binding right operand is parenthesized: a - (b - c). Comparisons
    parenthesize both sides, as `a < 1 = true` reads ambiguously."""
    folded = fold(left, operator, right)
    if folded is not None:
        return folded
    left_precedence = precedence + 1 if precedence == COMPARE else precedence
    code = (
        f"{operand(left, left_precedence)} {operator} {operand(right, precedence + 1)}"
    )
    return expression(
        code,
        uses([left, right]),
        precedence,
        type,
        boolean,
        volatile=changes([left, right]),
        defined=left.defined and right.defined,
        total=operator in TOTAL_OPERATORS and left.total and right.total,
    )


def fold(left: Expr, operator: str, right: Expr) -> Expr | None:
    """The value of an operation on values written in the source, as a
    hand-writer writes 1 for 0 + 1: the sum, difference or product of
    integers a double holds exactly, and the joined text of strings. None for
    anything else, which stays an expression."""
    a, b = written_scalar(left), written_scalar(right)
    if operator == "&" and isinstance(a, str) and isinstance(b, str):
        return literal(a + b)
    if not (isinstance(a, int) and isinstance(b, int)):
        return None
    result = {"+": a + b, "-": a - b, "*": a * b}.get(operator)
    if result is None or abs(result) > EXACT or max(abs(a), abs(b)) > EXACT:
        return None
    return literal(result)


def written_scalar(value: Expr) -> object:
    """The integer or the string value holds where it is written in the
    source, or None. A boolean is not an integer here, and a string that
    reads like a template is an expression already."""
    template = value.template
    if isinstance(template, bool) or not isinstance(template, (int, str)):
        return None
    if isinstance(template, str) and template.startswith("{%"):
        return None
    return template


def conditional(test: Expr, then: Expr, otherwise: Expr, type: Type | None) -> Expr:
    code = f"{operand(test, CONDITIONAL + 1)} ? {then.code} : {otherwise.code}"
    return expression(
        code,
        uses([test, then, otherwise]),
        CONDITIONAL,
        type,
        then.boolean and otherwise.boolean,
        volatile=changes([test, then, otherwise]),
        defined=then.defined and otherwise.defined,
        total=test.total and then.total and otherwise.total,
    )


def grouped(value: Expr) -> Expr:
    """value in parentheses where a conditional around it would otherwise read
    as one expression: a branch that is itself a conditional, or an expression
    written by hand, which may bind as loosely as any."""
    if value.precedence > CONDITIONAL:
        return value
    return expression(
        "(" + value.code + ")",
        value.variables,
        ATOM,
        value.type,
        value.boolean,
        volatile=value.volatile,
    )


def kept(test: Expr, value: Expr) -> Expr:
    """value where test holds, and nothing where it does not: a conditional
    with no else. $map and $each leave out an item their function returns
    nothing for, which is how a comprehension's condition drops one."""
    return expression(
        f"{operand(test, CONDITIONAL + 1)} ? {value.code}",
        uses([test, value]),
        CONDITIONAL,
        value.type,
        volatile=changes([test, value]),
    )


def entry(key: Expr, value: Expr) -> Expr:
    """A one-key object whose key is computed: `{$string($x): $x * 2}`. The key
    is parenthesized when it binds as loosely as the `:` that follows it."""
    return expression(
        "{" + operand(key, CONDITIONAL + 1) + ": " + value.code + "}",
        uses([key, value]),
        type=of(OBJECT, values=value.type),
        volatile=changes([key, value]),
    )


def merged(objects: Expr, values: Type | None) -> Expr:
    """The objects of a sequence merged into one, a later key winning over an
    earlier one, as a later entry of a dict comprehension does. $merge of no
    object is {}, so a sequence with nothing in it gives an empty dict."""
    listed = expression(
        "[" + objects.code + "]", objects.variables, volatile=objects.volatile
    )
    return call("merge", [listed], of(OBJECT, values=values))


def block(bindings: list[tuple[str, Expr]], body: Expr) -> Expr:
    """A block that binds each value to its variable, in order, before body:
    `($v := $random(); $v - 3 * $floor($v / 3))`. A value bound once is read
    as often as body needs it, evaluated once. Nothing to bind leaves body as
    it is."""
    if not bindings:
        return body
    bound = "".join(f"${name} := {operand(value, ATOM)}; " for name, value in bindings)
    values = [value for _, value in bindings]
    return expression(
        "(" + bound + body.code + ")",
        uses([*values, body]),
        ATOM,
        body.type,
        body.boolean,
        volatile=changes([*values, body]),
        defined=body.defined,
        total=body.total and all(value.total for value in values),
    )


def negate(value: Expr) -> Expr:
    number = written_scalar(value)
    if isinstance(number, int) and abs(number) <= EXACT:
        return literal(-number)
    if value.precedence == ATOM:
        code = "-" + value.code
    else:
        code = f"-({value.code})"
    return expression(code, value.variables, UNARY, of(NUMBER), volatile=value.volatile)


def field(value: Expr, key: str) -> Expr:
    """value[key] for a literal string key: a path step."""
    values = value.type.field(key) if value.type else None
    if "`" in key:
        return call("lookup", [value, literal(key)], values)
    step = key if NAME.fullmatch(key) and key not in LITERALS else "`" + key + "`"
    return expression(
        f"{operand(value, ATOM)}.{step}",
        value.variables,
        type=values,
        volatile=value.volatile,
        total=value.total,
    )


def index(value: Expr, position: Expr) -> Expr:
    """value[position]; JSONata counts negative positions from the end. A
    position right after another one is parenthesized: jsonata-python reads
    `$x[$i][0]` as one step and returns `$x[$i]`, while `($x[$i])[0]` indexes."""
    items = value.type.items if value.type else None
    places = value.type.positions if value.type else None
    place = position.template
    if (
        places is not None
        and isinstance(place, int)
        and not isinstance(place, bool)
        and -len(places) <= place < len(places)
    ):
        # A position written as a number reads the type of that place.
        items = places[place]
    base = value.code
    base = f"({base})" if base.endswith("]") else operand(value, ATOM)
    code = f"{base}[{position.code}]"
    return expression(
        code, uses([value, position]), type=items, volatile=changes([value, position])
    )
