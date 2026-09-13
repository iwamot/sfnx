"""What a module binds at its top level: imports and classes."""

import ast
from dataclasses import dataclass

from sfnx.diagnostics import CompileError


@dataclass(frozen=True)
class Module:
    """names maps imported local names to what they import (sfnx.wait);
    classes and functions hold what the module defines at its top level."""

    names: dict[str, str]
    classes: dict[str, ast.ClassDef]
    functions: dict[str, ast.FunctionDef]


def module(tree: ast.Module) -> Module:
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    return Module(imports(tree), classes, functions)


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
