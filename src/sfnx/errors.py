"""ASL error names, from the exception classes that raise and except name."""

import ast
import builtins
from collections.abc import Callable

from sfnx.diagnostics import CompileError
from sfnx.integrations import ResourceError, sdk_error
from sfnx.module import Module, qualified

# The errors Step Functions reports in JSONata workflows that a Retry or Catch
# can name. States.Runtime is left out because nothing catches it.
STATES = {
    "DataLimitExceeded": "States.DataLimitExceeded",
    "ExceedToleratedFailureThreshold": "States.ExceedToleratedFailureThreshold",
    "HeartbeatTimeout": "States.HeartbeatTimeout",
    "HttpSocket": "States.Http.Socket",
    "ItemReaderFailed": "States.ItemReaderFailed",
    "Permissions": "States.Permissions",
    "QueryEvaluationError": "States.QueryEvaluationError",
    "ResultWriterFailed": "States.ResultWriterFailed",
    "TaskFailed": "States.TaskFailed",
    "Timeout": "States.Timeout",
}

EVERYTHING = "States.ALL"

DEFINE = "define your own: class OrderFailed(Exception): pass"


def error_name(node: ast.expr, context: Module) -> str:
    """The error name of an exception class. Exception stands for States.ALL.

    Nested classes spell dotted names: a class ServiceException inside a class
    Lambda is Lambda.ServiceException, as a Lambda integration reports it. A
    class of this module that assigns error = "..." in its body has that
    name instead, which a class name cannot spell, such as one with spaces.
    """
    chain = attributes(node)
    if chain and chain[0] in context.classes:
        return nested(context.classes[chain[0]], chain[1:], node, context)
    target = qualified(node, context.names)
    if target is not None:
        segments = target.split(".")
        if segments[:2] == ["sfnx", "aws"]:
            return sdk(segments[2:], node)
        if segments[0] == "sfnx" and len(segments) == 2:
            if segments[1] not in STATES:
                raise CompileError(f"sfnx has no error named {segments[1]}", node)
            return STATES[segments[1]]
        # Modules are lowercase and classes capitalized, so the name starts
        # at the first class.
        start = next(
            (i for i, s in enumerate(segments) if s[:1].isupper()), len(segments) - 1
        )
        return ".".join(segment(s) for s in segments[start:])
    if isinstance(node, ast.Name):
        if node.id == "Exception":
            return EVERYTHING
        builtin = getattr(builtins, node.id, None)
        if isinstance(builtin, type) and issubclass(builtin, BaseException):
            raise CompileError(
                f"{node.id} is a Python exception and has no ASL error name; {DEFINE}",
                node,
            )
        raise CompileError(f"{node.id} is not defined; {DEFINE}, or import it", node)
    raise CompileError(f"name an exception class here; {DEFINE}", node)


def sdk(path: list[str], node: ast.expr) -> str:
    """The error name of aws.sdk.<service>.errors.<Exception>. The service is
    named as the resource ARN names it, with a _ after a Python keyword:
    lambda_ is lambda."""
    if len(path) != 4 or path[0] != "sdk" or path[2] != "errors":
        raise CompileError(
            "an SDK integration's error is aws.sdk.<service>.errors.<Exception>, "
            "such as aws.sdk.dynamodb.errors.ConditionalCheckFailedException",
            node,
        )
    try:
        return sdk_error(path[1], path[3])
    except ResourceError as exc:
        raise CompileError(str(exc), node) from None


def attributes(node: ast.expr) -> list[str]:
    """The names in a dotted reference such as Lambda.ServiceException."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        base = attributes(node.value)
        return [*base, node.attr] if base else []
    return []


def nested(
    outer: ast.ClassDef, path: list[str], node: ast.expr, context: Module
) -> str:
    current = outer
    names = [outer.name]
    for name in path:
        inner = next(
            (s for s in current.body if isinstance(s, ast.ClassDef) and s.name == name),
            None,
        )
        if inner is None:
            raise CompileError(
                f"{'.'.join(names)} has no class {name}; define it inside "
                f"class {current.name}",
                node,
            )
        current = inner
        names.append(name)
    defined(current, context)
    written = declared_name(current)
    if written is not None:
        return written
    return ".".join(segment(n) for n in names)


def declared_name(node: ast.ClassDef) -> str | None:
    """The name a class declares with error = "..." in its body, if any."""
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "error" for t in targets):
            continue
        value = statement.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            raise CompileError(
                'error is the ASL error name, written as a string: error = "..."',
                value,
            )
        if not value.value:
            raise CompileError("an ASL error name has at least one character", value)
        if value.value.startswith("States."):
            raise CompileError(
                "error names starting with States. are reserved for Step Functions; "
                "name the errors sfnx exports, such as Timeout, instead",
                value,
            )
        return value.value
    return None


def defined(node: ast.ClassDef, context: Module) -> str:
    """A class of this module. ASL error names are flat, so it derives from
    Exception directly."""
    bases = node.bases
    if (
        len(bases) != 1
        or node.keywords
        or not isinstance(bases[0], ast.Name)
        or bases[0].id != "Exception"
        or "Exception" in context.classes
        or "Exception" in context.names
    ):
        location = bases[0] if bases else node
        raise CompileError(
            f"ASL error names have no hierarchy; derive {node.name} from Exception "
            "directly, and catch several with except (A, B)",
            location,
        )
    return node.name


def segment(name: str) -> str:
    """One segment of an ASL error name, from the name of a class.

    ASL takes any characters in an error name, and a Python class name cannot
    start with a digit, so a leading _ is dropped where the rest is not a name
    on its own: _416 spells 416, while _Internal stays _Internal, which ASL
    takes as written.
    """
    if name.startswith("_"):
        rest = name[1:]
        if rest and not rest.isidentifier():
            return rest
    return name


def raised(node: ast.expr, context: Module) -> str:
    """The error name for a raise, which cannot be a Step Functions name."""
    name = error_name(node, context)
    if name == EVERYTHING:
        raise CompileError(f"raise names one error; {DEFINE}", node)
    if name.startswith("States."):
        raise CompileError(
            f"error names starting with States. are reserved for Step Functions; "
            f"{DEFINE}",
            node,
        )
    return name


MAX_SECONDS = 99_999_999
MAX_DELAY = 31_622_400
RETRIER_FIELDS = (
    "ErrorEquals",
    "IntervalSeconds",
    "MaxAttempts",
    "BackoffRate",
    "MaxDelaySeconds",
    "JitterStrategy",
)
EXAMPLE = 'retry=[{"ErrorEquals": [Timeout], "MaxAttempts": 3}]'


def caught(types: list[ast.expr], context: Module) -> list[str]:
    """The ErrorEquals of an except clause or a retrier."""
    names = [error_name(t, context) for t in types]
    if EVERYTHING in names and len(names) > 1:
        raise CompileError(
            "Exception matches every error; list it on its own", types[0]
        )
    return names


def retriers(
    node: ast.expr, context: Module, holds: Callable[[ast.expr], ast.expr]
) -> list[dict[str, object]]:
    """retry=, a list of ASL retriers written as dicts with error classes.

    holds gives what a name assigned outside the machine holds, so a retrier
    ASL repeats state by state is written once and named where it is used."""
    node = holds(node)
    if not isinstance(node, ast.List) or not node.elts:
        raise CompileError(f"retry is a list of retriers: {EXAMPLE}", node)
    result = []
    for position, element in enumerate(node.elts):
        element = holds(element)
        if not isinstance(element, ast.Dict):
            raise CompileError(f"a retrier is a dict: {EXAMPLE}", element)
        retrier: dict[str, object] = {}
        for key, value in zip(element.keys, element.values, strict=True):
            field = key.value if isinstance(key, ast.Constant) else None
            if not (isinstance(field, str) and field in RETRIER_FIELDS):
                raise CompileError(
                    f"retrier fields are {', '.join(RETRIER_FIELDS)}", key or value
                )
            retrier[field] = retrier_field(field, holds(value), context)
        if "ErrorEquals" not in retrier:
            raise CompileError(f"a retrier needs ErrorEquals: {EXAMPLE}", element)
        errors = retrier["ErrorEquals"]
        assert isinstance(errors, list)
        if EVERYTHING in errors and position != len(node.elts) - 1:
            raise CompileError(
                "a retrier for Exception matches every error, so it comes last",
                element,
            )
        result.append(retrier)
    return result


def retrier_field(name: str, node: ast.expr, context: Module) -> object:
    if name == "ErrorEquals":
        if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
            raise CompileError(
                "ErrorEquals is a list of exception classes: [Timeout, TaskFailed]",
                node,
            )
        return caught(node.elts, context)
    value = node.value if isinstance(node, ast.Constant) else None
    ranges = {
        "IntervalSeconds": (1, MAX_SECONDS),
        "MaxAttempts": (0, MAX_SECONDS),
        "MaxDelaySeconds": (1, MAX_DELAY),
    }
    if name in ranges:
        low, high = ranges[name]
        if not (type(value) is int and low <= value <= high):
            raise CompileError(f"{name} is a whole number from {low} to {high:,}", node)
    elif name == "BackoffRate":
        if not (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 1
        ):
            raise CompileError("BackoffRate is a number of 1.0 or more", node)
    elif value not in {"FULL", "NONE"}:
        raise CompileError('JitterStrategy is "FULL" or "NONE"', node)
    return value
