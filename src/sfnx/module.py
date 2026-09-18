"""What a module binds at its top level: imports, classes and values."""

import ast
import io
import tokenize
from dataclasses import dataclass

from sfnx.diagnostics import CompileError
from sfnx.expressions import spellings

# What a name outside the machine may hold. The compiler reads the module
# without running it, so the value is data written out, not a computation.
DATA = (ast.Constant, ast.List, ast.Dict, ast.Name, ast.Attribute)

SELF_ASSIGNED = "is assigned from itself outside the machine; write the value out"


@dataclass(frozen=True)
class Constant:
    """A name assigned at the top level: the value written there, which the
    compiler writes in wherever the name is read, and its annotation."""

    value: ast.expr
    declared: ast.expr | None


@dataclass(frozen=True)
class Module:
    """names maps imported local names to what they import (sfnx.wait);
    classes and functions hold what the module defines at its top level,
    constants what it assigns there, identifiers every name it binds or reads,
    spellings the Step Functions variable of each name that cannot be one as it
    is, and comments the comment lines right above a line of code, by that
    line."""

    names: dict[str, str]
    classes: dict[str, ast.ClassDef]
    functions: dict[str, ast.FunctionDef]
    constants: dict[str, Constant]
    identifiers: frozenset[str]
    spellings: dict[str, str]
    comments: dict[int, str]


def module(tree: ast.Module, source: str) -> Module:
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    names = identifiers(tree)
    return Module(
        imports(tree),
        classes,
        functions,
        constants(tree),
        names,
        spellings(names),
        comments(source),
    )


def constants(tree: ast.Module) -> dict[str, Constant]:
    """The names the module assigns at its top level, to what is written for
    them. A name assigned twice holds what the last assignment writes, and one
    assigned another name holds what that name held where it was read, as they
    do when Python runs the module."""
    found: dict[str, Constant] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                found[target.id] = Constant(written(node.value, found), None)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            found[node.target.id] = Constant(
                written(node.value, found), node.annotation
            )
    return found


def written(value: ast.expr, found: dict[str, Constant]) -> ast.expr:
    """The value a line writes for a name: a name given another name takes
    what that one holds there, and not what a line below assigns it. Every
    value kept is read this way already, so following one name is enough."""
    if isinstance(value, ast.Name) and value.id in found:
        return found[value.id].value
    return value


def holds(node: ast.expr, constants: dict[str, Constant]) -> ast.expr:
    """What a name outside the machine holds, followed as far as one name is
    assigned another. Anything else is the node itself."""
    seen: set[str] = set()
    while isinstance(node, ast.Name) and node.id in constants:
        if node.id in seen:
            raise CompileError(f"{node.id} {SELF_ASSIGNED}", node)
        seen.add(node.id)
        node = data(node.id, constants[node.id].value, node)
    return node


def data(name: str, value: ast.expr, node: ast.expr) -> ast.expr:
    """The value written for a name outside the machine, which the compiler
    reads as data: it does not run the module."""
    computed = next(
        (
            found
            for found in ast.walk(value)
            if isinstance(found, ast.expr) and not isinstance(found, DATA)
        ),
        None,
    )
    if computed is not None:
        raise CompileError(
            f"{name} holds {ast.unparse(computed)}, which the compiler would have to "
            "run; a name outside the machine holds JSON data and exception classes",
            node,
        )
    return value


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
