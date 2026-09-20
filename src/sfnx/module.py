"""What a module binds at its top level: imports, classes and values."""

import ast
import io
import tokenize
from dataclasses import dataclass

from sfnx.diagnostics import CompileError
from sfnx.expressions import spellings
from sfnx.jsontypes import AnnotationError, Type, annotation, structure

# What a name outside the machine may hold, along with the minus sign of a
# negative number. The compiler reads the module without running it, so the
# value is data written out, not a computation.
DATA = (ast.Constant, ast.List, ast.Dict, ast.Name, ast.Attribute)

TYPED_DICT = frozenset({"typing.TypedDict", "typing_extensions.TypedDict"})
# The qualifiers of a field that say whether its key may be left out, which
# is read with `in` or .get(); the value keeps the type inside them.
REQUIREMENT = frozenset(
    {
        "typing.NotRequired",
        "typing.Required",
        "typing_extensions.NotRequired",
        "typing_extensions.Required",
    }
)
ON_THE_CLASS = "derive it from TypedDict directly and declare every field on the class"


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
    typed: dict[str, Type]


def module(tree: ast.Module, source: str) -> Module:
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    names = identifiers(tree)
    imported = imports(tree)
    return Module(
        imported,
        classes,
        functions,
        constants(tree),
        names,
        spellings(names),
        comments(source),
        typed(tree, imported),
    )


def typed(tree: ast.Module, names: dict[str, str]) -> dict[str, Type]:
    """The TypedDict classes the module defines, as the object types their
    fields declare, read without running the module. A class names the ones
    written above it, so they are read in order; one that names itself or one
    below it would need a type that refers to another, which the types here
    do not, and is rejected."""
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(qualified(base, names) in TYPED_DICT for base in node.bases)
    ]
    pending = {node.name for node in classes}
    for node in tree.body:
        functional = written_as_call(node, names)
        if functional is not None:
            raise CompileError(
                f"write {functional} as a class: class {functional}(TypedDict):",
                node,
            )
        if (
            isinstance(node, ast.ClassDef)
            and node not in classes
            and any(isinstance(b, ast.Name) and b.id in pending for b in node.bases)
        ):
            raise CompileError(f"a TypedDict does not inherit; {ON_THE_CLASS}", node)
    found: dict[str, Type] = {}
    for node in classes:
        found[node.name] = typed_dict(node, names, found, pending)
        pending.discard(node.name)
    return found


def typed_dict(
    node: ast.ClassDef, names: dict[str, str], above: dict[str, Type], pending: set[str]
) -> Type:
    """One TypedDict class as the object type of its fields, which may name
    the classes above it. A field's NotRequired or Required says whether the
    key may be left out, which is what `in` and .get() read; the value has
    the type inside it either way."""
    if len(node.bases) != 1:
        raise CompileError(f"a TypedDict does not inherit; {ON_THE_CLASS}", node)
    for keyword in node.keywords:
        if keyword.arg != "total" or not (
            isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, bool)
        ):
            raise CompileError(
                "a TypedDict takes total=False here and no other argument", keyword
            )
    fields: list[tuple[str, Type | None]] = []
    for statement in node.body:
        if isinstance(statement, ast.Pass) or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if (
            not isinstance(statement, ast.AnnAssign)
            or not isinstance(statement.target, ast.Name)
            or statement.value is not None
        ):
            raise CompileError(
                "a TypedDict declares its fields only, one per line: name: type",
                statement,
            )
        declared = requirement(statement.annotation, names)
        recursive = next(
            (
                n
                for n in ast.walk(declared)
                if isinstance(n, ast.Name) and n.id in pending
            ),
            None,
        )
        if recursive is not None:
            raise CompileError(
                "recursive TypedDicts are not supported; a field names a TypedDict "
                "written above its class, not the class itself or one below",
                recursive,
            )
        try:
            fields.append((statement.target.id, annotation(declared, above)))
        except AnnotationError as exc:
            raise CompileError(str(exc), exc.node) from exc
    return structure(*fields)


def requirement(node: ast.expr, names: dict[str, str]) -> ast.expr:
    """The type inside NotRequired[T] or Required[T], or the annotation as
    it is."""
    if isinstance(node, ast.Subscript) and qualified(node.value, names) in REQUIREMENT:
        return node.slice
    return node


def written_as_call(node: ast.stmt, names: dict[str, str]) -> str | None:
    """The name a top-level assignment binds to TypedDict("Name", {...}),
    the form the compiler does not read."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target: ast.expr = node.targets[0]
        value = node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target = node.target
        value = node.value
    else:
        return None
    if (
        isinstance(target, ast.Name)
        and isinstance(value, ast.Call)
        and qualified(value.func, names) in TYPED_DICT
    ):
        return target.id
    return None


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
