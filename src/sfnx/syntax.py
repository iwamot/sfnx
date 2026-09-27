"""What a JSONata expression does with names, read from its syntax tree
rather than from its text, so a name written in a string is told apart from
one the expression reads or binds."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from jsonata.jexception import JException
from jsonata.parser import Parser

# A variable as JSONata spells one, found in the text of a string or a
# regular expression.
SPELLED = re.compile(r"\$([^\W\d]\w*)")


@dataclass(frozen=True)
class Facts:
    """reads holds the variables the expression reads, $states among them,
    that it does not bind itself for certain before it reads them: a
    function it calls by name counts, as JSONata's functions are variables.
    bound holds the names the expression binds, with :=, as the parameters
    of a function it defines, or with @ and # in a path, which take over a
    variable of the name. spelled holds the names a string or a regular
    expression in it writes as $name, as the text of jsonata() may: that is
    not a read, and a value put in the variable's place must leave it as it
    is."""

    reads: frozenset[str]
    bound: frozenset[str]
    spelled: frozenset[str]


@cache
def facts(code: str) -> Facts | None:
    """The facts of the code, or None where the parser cannot read it: the
    text of jsonata() may not be JSONata, which Step Functions rejects when
    it validates the definition, or may be JSONata newer than the parser."""
    try:
        tree = Parser().parse(code)
    except JException:
        return None
    bound: set[str] = set()
    spelled: set[str] = set()
    for node in nodes(tree):
        kind = node.type
        bound.update(binds(node))
        if kind == "bind":
            bound.add(text(node.lhs))
        elif kind == "lambda":
            bound.update(parameters(node))
        elif kind == "string":
            spelled.update(SPELLED.findall(text(node)))
        elif kind == "regex":
            assert isinstance(node.value, re.Pattern)
            pattern = node.value.pattern
            assert isinstance(pattern, str)
            spelled.update(SPELLED.findall(pattern))
    return Facts(
        frozenset(free(tree, frozenset())), frozenset(bound), frozenset(spelled)
    )


def names_read(code: str) -> frozenset[str]:
    """The variables the code reads, or, where the parser cannot read it,
    every name its text spells, the most it can read."""
    found = facts(code)
    return found.reads if found is not None else frozenset(SPELLED.findall(code))


def free(node: Parser.Symbol, bound: frozenset[str]) -> Iterator[str]:
    """The variables node reads that bound does not hold. A block binds a
    name for the expressions after the one that binds it, a function its
    parameters for its body, and a step of a path its @ and # names for its
    own filters and the steps after it. A name bound anywhere else, as inside
    a function's arguments, still counts as read, which is the safe side:
    what reads a variable keeps its assignment and has no value written in."""
    kind = node.type
    if kind == "variable" and node.value not in bound and node.value not in {"", "$"}:
        yield text(node)
    # The fields read in their own way; the others, such as the filters of
    # a block or of a step, read what is bound around the node.
    handled: tuple[str, ...] = ()
    rest = bound | binds(node)
    if kind == "bind":
        assert node.rhs is not None
        yield from free(node.rhs, bound)
        handled = ("lhs", "rhs")
    elif kind == "lambda":
        assert node.body is not None
        yield from free(node.body, bound | parameters(node))
        handled = ("arguments", "body")
    elif kind == "block":
        assert node.expressions is not None
        inner = bound
        for expression in node.expressions:
            yield from free(expression, inner)
            if expression.type == "bind":
                inner = inner | {text(expression.lhs)}
        handled = ("expressions",)
    elif kind == "path":
        assert node.steps is not None
        for step in node.steps:
            yield from free(step, rest)
            rest = rest | binds(step)
        handled = ("steps",)
    for child in children(node, *handled):
        yield from free(child, rest)


def binds(node: Parser.Symbol) -> frozenset[str]:
    """The names a step of a path binds with @ and #."""
    return frozenset(n for n in (node.focus, node.index) if isinstance(n, str))


def parameters(node: Parser.Symbol) -> frozenset[str]:
    assert node.arguments is not None
    return frozenset(text(argument) for argument in node.arguments)


def text(node: Parser.Symbol | None) -> str:
    """The name of a variable node, or the text of a string node."""
    assert node is not None and isinstance(node.value, str)
    return node.value


def nodes(node: Parser.Symbol) -> Iterator[Parser.Symbol]:
    """A node and every node under it."""
    yield node
    for child in children(node):
        yield from nodes(child)


def children(node: Parser.Symbol, *skipped: str) -> Iterator[Parser.Symbol]:
    """The nodes right under a node, but for those in the fields skipped: the
    parser keeps them in its fields, alone, in lists, or in the key and
    value pairs of an object."""
    for key, value in vars(node).items():
        if key != "_outer_instance" and key not in skipped:
            yield from within(value)


def within(value: object) -> Iterator[Parser.Symbol]:
    if isinstance(value, Parser.Symbol):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from within(item)
