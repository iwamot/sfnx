"""Python workflows, compiled to Amazon States Language.

The names here mark what the compiler turns into states. At run time they
leave the functions as they are, so a workflow module imports and calls like
any other Python.
"""

import inspect
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TypeVar, overload

try:
    from sfnx._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

F = TypeVar("F", bound=Callable[..., object])
# A result has the type its annotation declares (a TypedDict class, str, ...):
# a type variable only the return type mentions leaves that to the annotation.
R = TypeVar("R", bound=Mapping[str, object])
V = TypeVar("V")


@overload
def state_machine(function: F, /) -> F: ...


@overload
def state_machine(*, timeout: int | None = None) -> Callable[[F], F]: ...


def state_machine(
    function: F | None = None, /, *, timeout: int | None = None
) -> F | Callable[[F], F]:
    """Mark a function as a state machine: its parameter is the execution input
    and its return value the output. timeout becomes TimeoutSeconds."""
    if function is None:
        return lambda marked: marked
    return function


def wait(
    seconds: float | None = None, /, *, until: str | datetime | None = None
) -> None:
    """A Wait state: pause for seconds, or until a datetime or an RFC 3339
    timestamp in UTC. At run time it returns at once."""


def task(
    resource: str,
    arguments: object = None,
    /,
    *,
    timeout: float | None = None,
    heartbeat: float | None = None,
    role: str | None = None,
    retry: list[dict[str, object]] | None = None,
) -> R:
    """A Task state calling resource, an integration ARN, with arguments. The
    value is the task result, a JSON object; an annotation on the assignment
    (a TypedDict class) declares its fields. It only runs in Step Functions."""
    raise NotImplementedError(f"task({resource!r}) runs in Step Functions")


def jsonata(expression: str, /, **values: object) -> V:
    """A JSONata expression, for what has no Python spelling here. Each value
    is bound to the variable of its name: jsonata("$pad($s, -5, '0')", s=code),
    and an annotation on the assignment declares the type of the result. It
    only runs in Step Functions."""
    raise NotImplementedError(f"jsonata({expression!r}) runs in Step Functions")


def parallel(
    *branches: Callable[[], object],
    retry: list[dict[str, object]] | None = None,
) -> list:
    """A Parallel state whose branches are the functions given. The value is
    the list of what they return, in order. At run time they run one by one."""
    return [branch() for branch in branches]


def inline_map(
    function: Callable[..., object],
    items: list,
    /,
    *,
    max_concurrency: int | None = None,
    retry: list[dict[str, object]] | None = None,
) -> list:
    """A Map state in Inline mode: function runs for each item, and receives
    its index too if it takes two parameters. At run time items run in turn."""
    if len(inspect.signature(function).parameters) == 2:
        return [function(item, index) for index, item in enumerate(items)]
    return [function(item) for item in items]


def distributed_map(
    function: Callable[..., object],
    items: list | dict | None = None,
    /,
    *,
    source: dict[str, object] | None = None,
    args: dict[str, object] | None = None,
    batch: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
    max_concurrency: int | None = None,
    tolerated_failure_count: int | None = None,
    tolerated_failure_percentage: float | None = None,
    label: str | None = None,
    execution_type: str | None = None,
    retry: list[dict[str, object]] | None = None,
) -> list:
    """A Map state in Distributed mode: each item, or each batch, runs as a
    child execution of function with args. At run time function is called in
    turn with each item, each value of a dict, or with batch= each list of up
    to MaxItemsPerBatch items; MaxInputBytesPerBatch, source= and result= only
    work in Step Functions."""
    if items is None:
        raise NotImplementedError("distributed_map(source=...) runs in Step Functions")
    arguments = args or {}
    values = list(items.values()) if isinstance(items, dict) else items
    if batch is None:
        return [function(item, **arguments) for item in values]
    size = batch.get("MaxItemsPerBatch") or len(values) or 1
    assert isinstance(size, int)
    return [
        function(values[start : start + size], **arguments)
        for start in range(0, len(values), size)
    ]


# The Context Object: context["Execution"]["Id"] reads $states.context.Execution.Id.
# Step Functions fills it in, so it is empty when the module runs as Python.
context: dict[str, dict] = {}


# Errors Step Functions reports, for except and for the ErrorEquals of a
# retrier. Each stands for the States. name in its docstring.


class DataLimitExceeded(Exception):
    """States.DataLimitExceeded"""


class ExceedToleratedFailureThreshold(Exception):
    """States.ExceedToleratedFailureThreshold"""


class HeartbeatTimeout(Exception):
    """States.HeartbeatTimeout"""


class HttpSocket(Exception):
    """States.Http.Socket"""


class ItemReaderFailed(Exception):
    """States.ItemReaderFailed"""


class Permissions(Exception):
    """States.Permissions"""


class QueryEvaluationError(Exception):
    """States.QueryEvaluationError"""


class ResultWriterFailed(Exception):
    """States.ResultWriterFailed"""


class TaskFailed(Exception):
    """States.TaskFailed"""


class Timeout(Exception):
    """States.Timeout"""


__all__ = [
    "DataLimitExceeded",
    "ExceedToleratedFailureThreshold",
    "HeartbeatTimeout",
    "HttpSocket",
    "ItemReaderFailed",
    "Permissions",
    "QueryEvaluationError",
    "ResultWriterFailed",
    "TaskFailed",
    "Timeout",
    "__version__",
    "context",
    "distributed_map",
    "inline_map",
    "parallel",
    "state_machine",
    "task",
    "wait",
]
