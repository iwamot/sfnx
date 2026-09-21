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
        "replace",
        "reverse",
        "round",
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


@dataclass(frozen=True)
class Expr:
    """One value: JSONata source, and the template that writes it into a state.

    A literal's template is the JSON itself (`"Assign": {"fee": 100}`); anything
    else is a `{% ... %}` string, and objects and arrays mix the two. type is
    what is known about the value; boolean says the code always yields a JSON
    boolean, so a condition can use it without $boolean. constructor says the
    code is an array constructor, which one around it keeps as one element.
    volatile says the code may give another value when it is evaluated again,
    as $random() and $uuid() do, so what writes it twice binds it once first.
    """

    code: str
    template: object
    variables: frozenset[str] = frozenset()
    precedence: int = ATOM
    type: Type | None = None
    boolean: bool = False
    constructor: bool = False
    volatile: bool = False


def expression(
    code: str,
    variables: frozenset[str] = frozenset(),
    precedence: int = ATOM,
    type: Type | None = None,
    boolean: bool = False,
    constructor: bool = False,
    volatile: bool = False,
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
    return expression("$" + spelling(name, spelled), frozenset({name}), type=type)


def literal(value: object) -> Expr:
    """A JSON scalar. A string that opens or closes like a `{% %}` template is
    written as an expression: Step Functions would evaluate it, or reject a
    half-open one."""
    if isinstance(value, str):
        code = string(value)
        if value.startswith("{%") or value.endswith("%}"):
            return expression(code, type=of(STRING))
        return Expr(code, value, type=of(STRING))
    if isinstance(value, bool):
        return Expr(json.dumps(value), value, type=of(BOOLEAN), boolean=True)
    if value is None:
        return Expr("null", None, type=of(NULL))
    assert isinstance(value, (int, float))
    if not math.isfinite(value):
        raise ValueError("JSON has no infinite numbers")
    precedence = UNARY if value < 0 else ATOM
    return Expr(json.dumps(value), value, precedence=precedence, type=of(NUMBER))


def string(value: str) -> str:
    """A JSONata string literal. JSONata has no \\' escape, so a string with a
    single quote keeps the JSON double quotes."""
    escaped = json.dumps(value, ensure_ascii=False)
    if "'" in value:
        return escaped
    return "'" + escaped[1:-1].replace('\\"', '"') + "'"


def uses(values: list[Expr]) -> frozenset[str]:
    return frozenset().union(*(value.variables for value in values))


def changes(values: list[Expr]) -> bool:
    """Whether a value among values may differ when it is evaluated again."""
    return any(value.volatile for value in values)


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
    )


def operand(value: Expr, precedence: int) -> str:
    """The code of value, parenthesized if it binds looser than precedence."""
    return value.code if value.precedence >= precedence else f"({value.code})"


def call(
    function: str, arguments: list[Expr], type: Type | None, boolean: bool = False
) -> Expr:
    assert function in FUNCTIONS
    code = f"${function}(" + ", ".join(a.code for a in arguments) + ")"
    volatile = function in VOLATILE or changes(arguments)
    return expression(
        code, uses(arguments), type=type, boolean=boolean, volatile=volatile
    )


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
    )


def conditional(test: Expr, then: Expr, otherwise: Expr, type: Type | None) -> Expr:
    code = f"{operand(test, CONDITIONAL + 1)} ? {then.code} : {otherwise.code}"
    return expression(
        code,
        uses([test, then, otherwise]),
        CONDITIONAL,
        type,
        then.boolean and otherwise.boolean,
        volatile=changes([test, then, otherwise]),
    )


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
    )


def negate(value: Expr) -> Expr:
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
    )


def index(value: Expr, position: Expr) -> Expr:
    """value[position]; JSONata counts negative positions from the end. A
    position right after another one is parenthesized: jsonata-python reads
    `$x[$i][0]` as one step and returns `$x[$i]`, while `($x[$i])[0]` indexes."""
    items = value.type.items if value.type else None
    base = value.code
    base = f"({base})" if base.endswith("]") else operand(value, ATOM)
    code = f"{base}[{position.code}]"
    return expression(
        code, uses([value, position]), type=items, volatile=changes([value, position])
    )
