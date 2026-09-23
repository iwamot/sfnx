"""The tests' way into sfnx.testing: each Task answered by its state name, and
the output returned or the failure raised, as most tests here want it."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from sfnx import testing
from sfnx.testing import EXCEEDED, MAP_RUN, Call, Failure

__all__ = ["EXCEEDED", "MAP_RUN", "Failure", "replaced", "run"]

REPLACED: ContextVar[Mapping[str, Callable[..., object]]] = ContextVar("replaced")


@contextmanager
def replaced(**functions: Callable[..., object]) -> Iterator[None]:
    """JSONata functions replaced for the definitions run inside the block,
    such as a $random that returns given values and counts its calls. The
    replacement is undone on the way out, so it reaches no other test."""
    token = REPLACED.set(functions)
    try:
        yield
    finally:
        REPLACED.reset(token)


def run(
    definition: Mapping[str, object],
    execution_input: object,
    tasks: Mapping[str, Callable[[object], object]] | None = None,
) -> object:
    """Run a definition and return its output. tasks answers each Task, and
    each ItemReader, by its state name, given the evaluated Arguments."""

    def answer(call: Call) -> object:
        assert tasks is not None
        return tasks[call.state](call.arguments)

    return testing.run(
        definition,
        execution_input,
        None if tasks is None else answer,
        functions=REPLACED.get({}),
    ).output
