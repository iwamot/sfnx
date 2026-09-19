"""What a module binds at its top level: imports, classes and values."""

import ast
import io
import tokenize
from dataclasses import dataclass

from sfnx.diagnostics import CompileError
from sfnx.expressions import spellings

# What a name outside the machine may hold, along with the minus sign of a
# negative number. The compiler reads the module without running it, so the
# value is data written out, not a computation.
DATA = (ast.Constant, ast.List, ast.Dict, ast.Name, ast.Attribute)


@dataclass(frozen=True)
class Constant:
    """A name assigned at the top level: the value written there, which the
    compiler writes in wherever the name is read, its annotation, and the
    names the module had assigned by that line, which is what the names
    inside the value read."""

    value: ast.expr
    declared: ast.expr | None
    scope: dict[str, "Constant"]


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
    them and to the names the module had assigned by that line. A name
    assigned twice holds what the last assignment writes, and the names a
    value reads hold what they held where the value was written, as they do
    when Python runs the module: `B = {"n": A}` keeps the A of that line
    whatever a line below assigns A."""
    found: dict[str, Constant] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                found[target.id] = Constant(node.value, None, dict(found))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            found[node.target.id] = Constant(node.value, node.annotation, dict(found))
    return found


def holds(node: ast.expr, constants: dict[str, Constant]) -> ast.expr:
    """What a name outside the machine holds, followed as far as one name is
    assigned another. Anything else is the node itself. Each step reads the
    next name in the scope of the line it was written on, so a name reassigned
    below is not what was read above; that scope holds only lines before it,
    which is what ends the walk."""
    while isinstance(node, ast.Name) and node.id in constants:
        found = constants[node.id]
        node = data(node.id, found.value, node)
        constants = found.scope
    return node


def data(name: str, value: ast.expr, node: ast.expr) -> ast.expr:
    """The value written for a name outside the machine, which the compiler
    reads as data: it does not run the module."""
    computed = next(
        (
            found
            for found in ast.walk(value)
            if isinstance(found, ast.expr)
            and not isinstance(found, DATA)
            and not negative(found)
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


def negative(node: ast.expr) -> bool:
    """Whether a node is a negative number. JSON writes the minus sign as part
    of the number, and Python parses it as a minus in front of one, which is
    the value written out and not a computation."""
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
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
