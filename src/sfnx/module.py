"""What a module binds at its top level: imports and classes."""

import ast
import io
import tokenize
from dataclasses import dataclass

from sfnx.diagnostics import CompileError


@dataclass(frozen=True)
class Module:
    """names maps imported local names to what they import (sfnx.wait);
    classes and functions hold what the module defines at its top level,
    identifiers every name it binds or reads, and comments the comment lines
    right above a line of code, by that line."""

    names: dict[str, str]
    classes: dict[str, ast.ClassDef]
    functions: dict[str, ast.FunctionDef]
    identifiers: frozenset[str]
    comments: dict[int, str]


def module(tree: ast.Module, source: str) -> Module:
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    return Module(
        imports(tree), classes, functions, identifiers(tree), comments(source)
    )


def comments(source: str) -> dict[int, str]:
    """The lines of comments with nothing else on them that come right above a
    line of code, joined, by the number of that line. A blank line ends them."""
    texts: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and token.line.lstrip().startswith("#"):
            text = token.string[1:]
            texts[token.start[0]] = text.removeprefix(" ")
    found: dict[int, str] = {}
    block: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if number in texts:
            block.append(texts[number])
        elif line.strip():
            if block:
                found[number] = "\n".join(block)
            block = []
        else:
            block = []
    return found


def identifiers(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return frozenset(names)


def imports(tree: ast.Module) -> dict[str, str]:
    """Local names bound by module-level imports, to the names they import."""
    names: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.partition(".")[0]
                names[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name == "*":
                    raise CompileError(
                        f"import the names you use from {node.module} instead of *",
                        node,
                    )
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def qualified(node: ast.expr, names: dict[str, str]) -> str | None:
    """The imported name an expression refers to, such as sfnx.state_machine."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = qualified(node.value, names)
        return f"{base}.{node.attr}" if base else None
    return None
