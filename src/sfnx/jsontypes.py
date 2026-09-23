"""JSON types, as annotations declare them and operators need them."""

import ast
from collections.abc import Mapping
from dataclasses import dataclass

NUMBER = "number"
STRING = "string"
BOOLEAN = "boolean"
NULL = "null"
ARRAY = "array"
OBJECT = "object"

ANNOTATIONS = (
    "annotate with float, str, bool, list, dict, None, list[X], dict[str, X], "
    "a union such as str | None, or a TypedDict class of this module"
)


@dataclass(frozen=True)
class Type:
    """The JSON types a value may have. items and values describe the elements
    of an array and the values of an object; None means nothing is declared.
    fields are the members of an object whose keys are known, as an AWS API
    response has them. empty says an array is known to have no items, such as
    `[]`, so what it joins keeps the items of the other. positions are the
    types at each place of an array whose places are known, as parallel()
    gives one per branch."""

    kinds: frozenset[str]
    items: "Type | None" = None
    values: "Type | None" = None
    fields: "tuple[tuple[str, Type | None], ...] | None" = None
    empty: bool = False
    positions: "tuple[Type | None, ...] | None" = None

    def field(self, key: str) -> "Type | None":
        """The type of the value under a key."""
        if self.fields is not None:
            return dict(self.fields).get(key)
        return self.values

    @property
    def kind(self) -> str | None:
        """The only type the value can have, or None if there are several."""
        return next(iter(self.kinds)) if len(self.kinds) == 1 else None

    def describe(self) -> str:
        return " | ".join(sorted(self.kinds))


def article(described: str) -> str:
    """A type as a sentence names it, with the article English wants before it:
    a number, an array, an object."""
    return ("an " if described[0] in "aeiou" else "a ") + described


def of(kind: str, items: Type | None = None, values: Type | None = None) -> Type:
    return Type(frozenset({kind}), items, values)


def union(first: Type | None, second: Type | None) -> Type | None:
    """A value that may come from either; unknown if either is."""
    if first is None or second is None:
        return None
    fields = first.fields if first.fields == second.fields else None
    if OBJECT not in second.kinds:
        fields = first.fields
    elif OBJECT not in first.kinds:
        fields = second.fields
    positions = first.positions if first.positions == second.positions else None
    if ARRAY not in second.kinds:
        positions = first.positions
    elif ARRAY not in first.kinds:
        positions = second.positions
    items = elements(first, second, ARRAY, first.items, second.items)
    if first.empty:
        items = second.items if ARRAY in second.kinds else None
    elif second.empty:
        items = first.items if ARRAY in first.kinds else None
    return Type(
        first.kinds | second.kinds,
        items,
        elements(first, second, OBJECT, first.values, second.values),
        fields,
        first.empty and second.empty,
        positions,
    )


def elements(
    first: Type, second: Type, kind: str, one: Type | None, other: Type | None
) -> Type | None:
    if kind not in first.kinds:
        return other
    if kind not in second.kinds:
        return one
    return union(one, other)


class AnnotationError(ValueError):
    def __init__(self, node: ast.expr):
        super().__init__(ANNOTATIONS)
        self.node = node


def annotation(node: ast.expr, named: Mapping[str, Type] | None = None) -> Type:
    """The type an annotation declares. named holds the types the module
    declares under a name, its TypedDict classes."""
    if isinstance(node, ast.Name):
        if named is not None and node.id in named:
            return named[node.id]
        scalar = {
            "float": NUMBER,
            "int": NUMBER,
            "str": STRING,
            "bool": BOOLEAN,
            "list": ARRAY,
            "dict": OBJECT,
        }.get(node.id)
        if scalar:
            return of(scalar)
    elif isinstance(node, ast.Constant) and node.value is None:
        return of(NULL)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        declared = union(annotation(node.left, named), annotation(node.right, named))
        assert declared is not None
        return declared
    elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == "list":
            return of(ARRAY, items=annotation(node.slice, named))
        if (
            node.value.id == "dict"
            and isinstance(node.slice, ast.Tuple)
            and len(node.slice.elts) == 2
            and isinstance(node.slice.elts[0], ast.Name)
            and node.slice.elts[0].id == "str"
        ):
            return of(OBJECT, values=annotation(node.slice.elts[1], named))
    raise AnnotationError(node)


def restrict(declared: Type | None, kinds: frozenset[str]) -> Type:
    """What a value is after a test showed it is one of kinds."""
    if declared is None:
        return Type(kinds)
    kept = declared.kinds & kinds
    if not kept:
        return Type(kinds)
    return Type(
        kept,
        declared.items,
        declared.values,
        declared.fields,
        positions=declared.positions,
    )


def exclude(declared: Type, kinds: frozenset[str]) -> Type:
    """What a value is after a test showed it is none of kinds."""
    kept = declared.kinds - kinds
    if not kept:
        return declared
    return Type(
        kept,
        declared.items,
        declared.values,
        declared.fields,
        positions=declared.positions,
    )


# What except ... as e binds: the error output of a Catch.
ERROR_OUTPUT = Type(
    frozenset({OBJECT}), fields=(("Cause", of(STRING)), ("Error", of(STRING)))
)


def structure(*fields: tuple[str, Type | None]) -> Type:
    return Type(frozenset({OBJECT}), fields=fields)


# The Context Object, as $states.context has it.
CONTEXT = structure(
    (
        "Execution",
        structure(
            ("Id", of(STRING)),
            ("Input", None),
            ("Name", of(STRING)),
            ("RoleArn", of(STRING)),
            ("StartTime", of(STRING)),
            ("RedriveCount", of(NUMBER)),
            ("RedriveTime", of(STRING)),
        ),
    ),
    (
        "State",
        structure(
            ("EnteredTime", of(STRING)),
            ("Name", of(STRING)),
            ("RetryCount", of(NUMBER)),
        ),
    ),
    ("StateMachine", structure(("Id", of(STRING)), ("Name", of(STRING)))),
    ("Task", structure(("Token", of(STRING)))),
    ("Map", None),
)
CONTEXT_OBJECTS = [CONTEXT, *(t for _, t in CONTEXT.fields or () if t is not None)]
