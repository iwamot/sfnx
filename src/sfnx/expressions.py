"""JSONata expressions and the JSON templates that carry them in state fields."""

import json
import math
import re
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
# looser than the place it is put in.
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
        "boolean",
        "ceil",
        "contains",
        "count",
        "each",
        "exists",
        "filter",
        "floor",
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
        "parse",
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
        "trim",
        "type",
        "uppercase",
        "uuid",
    }
)


@dataclass(frozen=True)
class Expr:
    """One value: JSONata source, and the template that writes it into a state.

    A literal's template is the JSON itself (`"Assign": {"fee": 100}`); anything
    else is a `{% ... %}` string, and objects and arrays mix the two. type is
    what is known about the value; boolean says the code always yields a JSON
    boolean, so a condition can use it without $boolean. constructor says the
    code is an array constructor, which one around it keeps as one element.
    """

    code: str
    template: object
    variables: frozenset[str] = frozenset()
    precedence: int = ATOM
    type: Type | None = None
    boolean: bool = False
    constructor: bool = False


def expression(
    code: str,
    variables: frozenset[str] = frozenset(),
    precedence: int = ATOM,
    type: Type | None = None,
    boolean: bool = False,
    constructor: bool = False,
) -> Expr:
    return Expr(
        code, "{% " + code + " %}", variables, precedence, type, boolean, constructor
    )


def spelling(name: str, identifiers: frozenset[str]) -> str:
    """The Step Functions variable, or JSONata parameter, for a Python name. A
    variable hides the JSONata function of its name from every later state, so
    a name the generated expressions call as a function gets _val, numbered
    when the module already uses that name."""
    if name not in FUNCTIONS:
        return name
    spelled = f"{name}_val"
    serial = 1
    while spelled in identifiers:
        serial += 1
        spelled = f"{name}_val_{serial}"
    return spelled


def variable(name: str, identifiers: frozenset[str], type: Type | None = None) -> Expr:
    return expression("$" + spelling(name, identifiers), frozenset({name}), type=type)


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
    wrapped = Expr("[[" + item.code + "]]", None, item.variables)
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
    )


def operand(value: Expr, precedence: int) -> str:
    """The code of value, parenthesized if it binds looser than precedence."""
    return value.code if value.precedence >= precedence else f"({value.code})"


def call(
    function: str, arguments: list[Expr], type: Type | None, boolean: bool = False
) -> Expr:
    assert function in FUNCTIONS
    code = f"${function}(" + ", ".join(a.code for a in arguments) + ")"
    return expression(code, uses(arguments), type=type, boolean=boolean)


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
    return expression(code, uses([left, right]), precedence, type, boolean)


def conditional(test: Expr, then: Expr, otherwise: Expr, type: Type | None) -> Expr:
    code = f"{operand(test, CONDITIONAL + 1)} ? {then.code} : {otherwise.code}"
    return expression(
        code,
        uses([test, then, otherwise]),
        CONDITIONAL,
        type,
        then.boolean and otherwise.boolean,
    )


def negate(value: Expr) -> Expr:
    if value.precedence == ATOM:
        code = "-" + value.code
    else:
        code = f"-({value.code})"
    return expression(code, value.variables, UNARY, of(NUMBER))


def field(value: Expr, key: str) -> Expr:
    """value[key] for a literal string key: a path step."""
    values = value.type.field(key) if value.type else None
    if "`" in key:
        return call("lookup", [value, literal(key)], values)
    step = key if NAME.fullmatch(key) and key not in LITERALS else "`" + key + "`"
    return expression(f"{operand(value, ATOM)}.{step}", value.variables, type=values)


def index(value: Expr, position: Expr) -> Expr:
    """value[position]; JSONata counts negative positions from the end. A
    position right after another one is parenthesized: jsonata-python reads
    `$x[$i][0]` as one step and returns `$x[$i]`, while `($x[$i])[0]` indexes."""
    items = value.type.items if value.type else None
    base = value.code
    base = f"({base})" if base.endswith("]") else operand(value, ATOM)
    code = f"{base}[{position.code}]"
    return expression(code, uses([value, position]), type=items)
