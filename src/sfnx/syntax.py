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
    """bound holds the names the expression binds, with := or as the
    parameters of a function it defines, which take over a variable of the
    name. spelled holds the names a string or a regular expression in it
    writes as $name, as the text of jsonata() may: that is not a read, and a
    value put in the variable's place must leave it as it is."""

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
        if kind == "bind":
            bound.add(text(node.lhs))
        elif kind == "lambda":
            assert node.arguments is not None
            bound.update(text(argument) for argument in node.arguments)
        elif kind == "string":
            spelled.update(SPELLED.findall(text(node)))
        elif kind == "regex":
            assert isinstance(node.value, re.Pattern)
            pattern = node.value.pattern
            assert isinstance(pattern, str)
            spelled.update(SPELLED.findall(pattern))
    return Facts(frozenset(bound), frozenset(spelled))


def text(node: Parser.Symbol | None) -> str:
    """The name of a variable node, or the text of a string node."""
    assert node is not None and isinstance(node.value, str)
    return node.value


def nodes(node: Parser.Symbol) -> Iterator[Parser.Symbol]:
    """A node and every node under it: the parser keeps them in its fields,
    alone, in lists, or in the key and value pairs of an object."""
    yield node
    for key, value in vars(node).items():
        if key != "_outer_instance":
            yield from within(value)


def within(value: object) -> Iterator[Parser.Symbol]:
    if isinstance(value, Parser.Symbol):
        yield from nodes(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from within(item)
