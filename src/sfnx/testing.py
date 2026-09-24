"""Run a definition in JSONata mode locally, with each Task answered by a
function of the test's, so a test can follow where a workflow goes and what it
returns without AWS. The JSONata is evaluated with jsonata-python, with the
functions Step Functions adds and the behaviors docs/design.md records as
measured; docs/testing.md lists where a local run can still differ."""

import base64
import binascii
import decimal
import hashlib
import json
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeGuard

try:
    import jsonata
    from jsonata.functions import Functions
    from jsonata.parser import Parser
    from jsonata.utils import Utils
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "sfnx.testing evaluates JSONata with jsonata-python; install sfnx[testing]",
        name=exc.name,
    ) from exc

EXCEEDED = "The specified tolerated failure threshold was exceeded"
MAP_RUN = "arn:aws:states:us-east-1:123456789012:mapRun:machine"


class Failure(Exception):
    """An error and its cause: what a tasks function raises to fail its Task,
    and what reading the output of a failed execution raises."""

    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause


class Unsupported(ValueError):
    """A definition, or a field of one, that run() does not interpret. It is
    raised before anything runs, so it does not depend on the path taken."""


class InvalidDefinition(ValueError):
    """A definition Step Functions rejects when it validates it, so that it
    never runs there. It is raised before anything runs."""


@dataclass(frozen=True)
class Call:
    """A Task called, or the ItemReader of a Map read: the state, its Resource
    as written, and its Arguments evaluated."""

    state: str
    resource: str
    arguments: object


Tasks = Callable[[Call], object]


class Execution:
    """What a run did: the states entered and the calls made, in order, and
    the output or the error."""

    def __init__(
        self,
        states: tuple[str, ...],
        calls: tuple[Call, ...],
        output: object = None,
        failure: Failure | None = None,
    ):
        self.states = states
        self.calls = calls
        self.error = None if failure is None else failure.error
        self.cause = None if failure is None else failure.cause
        self._output = output

    @property
    def output(self) -> object:
        """The output of the execution, or the Failure it failed with raised,
        so that a test expecting an output shows the error it got instead."""
        if self.error is not None:
            raise Failure(self.error, self.cause or "")
        return self._output

    def __repr__(self) -> str:
        ending = (
            f"output={self._output!r}"
            if self.error is None
            else f"error={self.error!r}, cause={self.cause!r}"
        )
        return f"Execution(states={self.states!r}, calls={self.calls!r}, {ending})"


@dataclass
class Record:
    """The tasks function of a run, and what the run has entered and called."""

    tasks: Tasks | None
    states: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def call(self, state: str, resource: object, arguments: object) -> object:
        assert isinstance(resource, str)
        made = Call(state, resource, arguments)
        self.calls.append(made)
        if self.tasks is None:
            raise ValueError(f"{state} calls {resource}, and run() was given no tasks")
        return self.tasks(made)


REPLACED: ContextVar[Mapping[str, Callable[..., object]]] = ContextVar("replaced")


def run(
    definition: Mapping[str, object],
    execution_input: object,
    tasks: Tasks | None = None,
    *,
    functions: Mapping[str, Callable[..., object]] | None = None,
) -> Execution:
    """Run a definition with the execution input given. tasks is called with
    each Task and each ItemReader, and returns the result or raises Failure.
    functions replaces JSONata functions by name, such as now or uuid, for a
    result that would otherwise change on every run."""
    check(definition)
    record = Record(tasks)
    token = REPLACED.set(dict(functions or {}))
    try:
        output = scope(
            definition,
            {},
            execution_input,
            execution_context(execution_input),
            record,
        )
    except Failure as failure:
        return Execution(tuple(record.states), tuple(record.calls), failure=failure)
    finally:
        REPLACED.reset(token)
    return Execution(tuple(record.states), tuple(record.calls), output)


# The fields run() interprets, or can leave aside because time does not pass
# in a local run: a Wait returns at once, a Retry does not wait between
# attempts, and a timeout fires only when a tasks function raises
# States.Timeout.
COMMON = frozenset({"Type", "Comment", "QueryLanguage"})
ACTION = frozenset({"Assign", "Output", "Retry", "Catch", "Next", "End"})
FIELDS = {
    "Task": ACTION
    | {"Resource", "Arguments", "Credentials", "TimeoutSeconds", "HeartbeatSeconds"},
    "Parallel": ACTION | {"Branches", "Arguments"},
    "Map": ACTION
    | {
        "ItemProcessor",
        "Items",
        "ItemReader",
        "ItemSelector",
        "ItemBatcher",
        "ResultWriter",
        "MaxConcurrency",
        "ToleratedFailureCount",
        "ToleratedFailurePercentage",
        "Label",
    },
    "Pass": frozenset({"Assign", "Output", "Next", "End"}),
    "Wait": frozenset({"Seconds", "Timestamp", "Assign", "Output", "Next", "End"}),
    "Choice": frozenset({"Choices", "Default", "Assign", "Output"}),
    "Succeed": frozenset({"Output"}),
    "Fail": frozenset({"Error", "Cause"}),
}
MACHINE = frozenset(
    {"StartAt", "States", "Comment", "QueryLanguage", "TimeoutSeconds", "Version"}
)
BRANCH = frozenset({"StartAt", "States", "Comment"})
PROCESSOR = BRANCH | {"ProcessorConfig"}
RULE = frozenset({"Condition", "Next", "Assign", "Output", "Comment"})
CATCHER = frozenset({"ErrorEquals", "Next", "Assign", "Output", "Comment"})
RETRIER = frozenset(
    {
        "ErrorEquals",
        "MaxAttempts",
        "IntervalSeconds",
        "BackoffRate",
        "MaxDelaySeconds",
        "JitterStrategy",
        "Comment",
    }
)
READER = frozenset({"Resource", "Arguments", "ReaderConfig"})
WRITER = frozenset({"Resource", "Arguments", "WriterConfig"})
BATCHER = frozenset({"MaxItemsPerBatch", "MaxInputBytesPerBatch", "BatchInput"})


def check(definition: Mapping[str, object]) -> None:
    """Raise Unsupported for a definition with a state that is not in JSONata
    mode, or with a state or a field run() does not interpret, and
    InvalidDefinition for one Step Functions rejects."""
    check_fields("the definition", definition, MACHINE)
    language = definition.get("QueryLanguage", "JSONPath")
    assert isinstance(language, str)
    check_states(definition, language)
    check_valid(definition, frozenset())


def check_states(machine: Mapping[str, object], language: str) -> None:
    """A state is in its own QueryLanguage, or else in the definition's, which
    is JSONPath when the definition does not set one; a state in a branch or
    a Map reads the definition's too, not the Parallel's or the Map's
    (measured)."""
    listed = machine["States"]
    assert isinstance(listed, dict)
    for name, state in listed.items():
        assert isinstance(state, dict)
        kind = state["Type"]
        if kind not in FIELDS:
            raise Unsupported(
                f"{name}: the state type {kind} is not run by sfnx.testing"
            )
        if "QueryLanguage" in state and state["QueryLanguage"] != "JSONata":
            raise Unsupported(
                f"{name}: QueryLanguage {state['QueryLanguage']} is not run by"
                " sfnx.testing, which runs JSONata only"
            )
        if state.get("QueryLanguage", language) != "JSONata":
            raise Unsupported(
                f"{name}: the state is in {language}, as the definition does not"
                " set QueryLanguage to JSONata; set it there or on the state, as"
                " sfnx.testing runs JSONata only"
            )
        check_fields(name, state, COMMON | FIELDS[kind])
        for key, allowed in [("Retry", RETRIER), ("Catch", CATCHER), ("Choices", RULE)]:
            for entry in state.get(key, []):
                check_fields(f"{name}.{key}", entry, allowed)
        for key, allowed in [
            ("ItemReader", READER),
            ("ResultWriter", WRITER),
            ("ItemBatcher", BATCHER),
        ]:
            if isinstance(state.get(key), dict):
                check_fields(f"{name}.{key}", state[key], allowed)
        for branch in state.get("Branches", []):
            check_fields(f"{name}.Branches", branch, BRANCH)
            check_states(branch, language)
        if "ItemProcessor" in state:
            check_fields(f"{name}.ItemProcessor", state["ItemProcessor"], PROCESSOR)
            check_states(state["ItemProcessor"], language)


def check_fields(
    where: str, found: Mapping[str, object], allowed: frozenset[str]
) -> None:
    for key in found:
        if key not in allowed:
            raise Unsupported(f"{where}: {key} is not run by sfnx.testing")


# The fields of $states in every expression. The result is there too in the
# Assign and Output of the states that make one, and the error in those of a
# catcher (ValidateStateMachineDefinition; measured).
STATES_FIELDS = frozenset({"input", "context"})
RESULT_STATES = frozenset({"Task", "Parallel", "Map"})
# The fields of a state whose expressions are not the state's own: a Comment
# holds none, and the others hold rules, catchers or states of their own.
NESTED = frozenset({"Comment", "Choices", "Catch", "Branches", "ItemProcessor"})


def check_valid(machine: Mapping[str, object], outer: frozenset[str]) -> None:
    """What Step Functions rejects in a definition, in the states of the
    machine or of one branch: a transition to a state that is not among them,
    an expression that does not parse or reads a field $states lacks there,
    and an Assign of a name in outer, the variables assigned on the way into
    the branch (measured)."""
    states = machine["States"]
    assert isinstance(states, dict)
    if machine["StartAt"] not in states:
        raise InvalidDefinition(f"StartAt: there is no state {machine['StartAt']}")
    for name, state in states.items():
        for target, _ in transitions(state):
            if target not in states:
                raise InvalidDefinition(f"{name}: there is no state {target} to go to")
    for name, state in states.items():
        read = STATES_FIELDS | ({"result"} if state["Type"] in RESULT_STATES else set())
        for key, template in state.items():
            if key not in NESTED:
                allowed = read if key in {"Assign", "Output"} else STATES_FIELDS
                check_expressions(f"{name}.{key}", template, allowed)
        for rule in state.get("Choices", []):
            for key, template in rule.items():
                if key != "Comment":
                    check_expressions(f"{name}.Choices", template, STATES_FIELDS)
        errors = STATES_FIELDS | {"errorOutput"}
        for catcher in state.get("Catch", []):
            for key, template in catcher.items():
                if key != "Comment":
                    check_expressions(f"{name}.Catch", template, errors)
        for assign in [state, *state.get("Choices", []), *state.get("Catch", [])]:
            for variable in assign.get("Assign", {}):
                if variable in outer:
                    raise InvalidDefinition(
                        f"{name}: {variable} is assigned on the way into this"
                        " branch, and a branch cannot assign it again"
                    )
        if state["Type"] in {"Parallel", "Map"}:
            into = reaching(states, name)
            before = {
                variable
                for source in states.values()
                for target, assigned in transitions(source)
                if target in into
                for variable in assigned
            }
            branches = state.get("Branches") or [state["ItemProcessor"]]
            for branch in branches:
                check_valid(branch, outer | before)


def transitions(state: Mapping[str, object]) -> list[tuple[object, dict[str, object]]]:
    """Where a state goes next, each with the variables assigned on the way:
    the state's for its Next or its Default, and a Choice rule's or a
    catcher's own."""
    assign = state.get("Assign", {})
    assert isinstance(assign, dict)
    taken = [(state[key], assign) for key in ["Next", "Default"] if key in state]
    for key in ["Choices", "Catch"]:
        entries = state.get(key, [])
        assert isinstance(entries, list)
        taken += [(entry["Next"], entry.get("Assign", {})) for entry in entries]
    return taken


def reaching(states: Mapping[str, Mapping[str, object]], name: str) -> set[str]:
    """The state named and every state from which it can be reached."""
    found = {name}
    grown = True
    while grown:
        grown = False
        for source, state in states.items():
            if source not in found and any(
                target in found for target, _ in transitions(state)
            ):
                found.add(source)
                grown = True
    return found


def check_expressions(where: str, template: object, allowed: frozenset[str]) -> None:
    """Every expression in a field parses, and reads of $states only the
    fields in allowed. Step Functions looks at a path that starts at $states,
    not at one that reaches it another way, such as ($states).result
    (measured)."""
    if isinstance(template, dict):
        for value in template.values():
            check_expressions(where, value, allowed)
    elif isinstance(template, list):
        for value in template:
            check_expressions(where, value, allowed)
    elif (code := expression(template)) is not None:
        try:
            parsed = jsonata.Jsonata(code).ast
        except jsonata.JException as exc:
            raise InvalidDefinition(
                f"{where}: {code.strip()} does not parse: {exc}"
            ) from exc
        for field in states_fields(parsed):
            if field not in allowed:
                raise InvalidDefinition(f"{where}: $states has no {field} here")


def states_fields(parsed: object) -> set[str]:
    """The fields of $states a parsed expression reads by name."""
    found: set[str] = set()
    pending = [parsed]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
            continue
        if not isinstance(node, Parser.Symbol):
            continue
        steps = node.steps if node.type == "path" else None
        if (
            steps
            and steps[0].type == "variable"
            and steps[0].value == "states"
            and len(steps) > 1
            and steps[1].type == "name"
        ):
            found.add(str(steps[1].value))
        pending.extend(v for k, v in vars(node).items() if k != "_outer_instance")
    return found


def expression(template: object) -> str | None:
    """The JSONata of a string written as {% ... %}."""
    if (
        isinstance(template, str)
        and template.startswith("{%")
        and template.endswith("%}")
    ):
        return template[2:-2]
    return None


def evaluate(code: str, variables: Mapping[str, object], states: object) -> object:
    """jsonata-python reads a Python None as undefined, so JSON null goes in as
    its null value, and an undefined result fails as it does in Step Functions."""
    expression = jsonata.Jsonata(code)
    expression.set_output_convert_nulls(False)
    # The functions Step Functions adds to JSONata.
    expression.register_lambda("parse", parse)
    expression.register_lambda("uuid", new_uuid)
    expression.register_lambda("random", random_number)
    expression.register_lambda("range", range_numbers)
    expression.register_lambda("now", now)
    expression.register_lambda("millis", lambda: int(time.time() * 1000))
    expression.register_lambda("hash", digest)
    expression.register_lambda("partition", partition)
    # The functions whose Step Functions behavior differs from jsonata-python's.
    expression.register_lambda("fromMillis", from_millis)
    expression.register_lambda("decodeUrlComponent", decode_url_component)
    expression.register_lambda("base64decode", base64_decode)
    expression.register_lambda("formatNumber", format_number)
    for name, function in REPLACED.get({}).items():
        expression.register_lambda(name, function)
    try:
        bindings = {k: nulls(v) for k, v in {**variables, "states": states}.items()}
        result = expression.evaluate(None, bindings)
    except jsonata.JException as exc:
        raise Failure("States.QueryEvaluationError", str(exc)) from exc
    except IndexError as exc:
        message = unwritten(exc)
        if message is None:
            raise
        raise Failure("States.QueryEvaluationError", message) from exc
    if result is None:
        raise Failure("States.QueryEvaluationError", f"{code} is undefined")
    return Utils.convert_nulls(result)


def unwritten(exc: IndexError) -> str | None:
    """The code and template of a JSONata error whose message jsonata-python
    fails to write: T0412 and T2009 have three places for the values and are
    given two. An IndexError raised anywhere else is not one."""
    trace = exc.__traceback__
    assert trace is not None
    while trace.tb_next is not None:
        trace = trace.tb_next
    frame = trace.tb_frame
    if frame.f_code is not jsonata.JException.msg.__code__:
        return None
    return f"{frame.f_locals['error']}: {frame.f_locals['message']}"


# jsonata-python passes an undefined argument as None, JSON null as its null
# value, and leaves an omitted one out, so the functions below take what they
# are given and check it as Step Functions does: too many arguments or one of
# the wrong type fail with T0410, and undefined gives undefined unless a
# docstring says otherwise (measured).


def mismatch(name: str, position: int) -> jsonata.JException:
    return jsonata.JException(
        f'T0410: Argument {position} of function "{name}" does not match'
        " function signature"
    )


def at_most(name: str, args: tuple[object, ...], count: int) -> None:
    if len(args) > count:
        raise mismatch(name, count + 1)


def is_number(value: object) -> TypeGuard[int | float]:
    """A JSON number: an int or a float, not a boolean."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse(*args: object) -> object:
    """$parse as Python's json reads the text, which accepts NaN and rejects
    single quotes where Step Functions does the opposite. No text or undefined
    gives undefined, and anything but a string fails (measured)."""
    at_most("parse", args, 1)
    text = args[0] if args else None
    if text is None:
        return None
    if not isinstance(text, str):
        raise mismatch("parse", 1)
    try:
        return nulls(json.loads(text))
    except ValueError as exc:
        raise jsonata.JException(str(exc)) from exc


def new_uuid(*args: object) -> str:
    """$uuid, which takes no argument (measured)."""
    at_most("uuid", args, 0)
    return str(uuid.uuid4())


def random_number(*args: object) -> float:
    """$random, from 0 up to 1, and the same for the same seed. The number a
    seed gives comes from its SHA-256, not the one Step Functions gives. A seed
    that is not a number fails (measured)."""
    at_most("random", args, 1)
    seed = args[0] if args else None
    if seed is None:
        return Functions.random()
    if not is_number(seed):
        raise mismatch("random", 1)
    bits = hashlib.sha256(repr(float(seed)).encode()).digest()[:7]
    return int.from_bytes(bits) / 2**56


def text_argument(name: str, args: tuple[object, ...]) -> str | None:
    """The one text a function takes: None for undefined, and a failure for
    anything but a string (measured)."""
    at_most(name, args, 1)
    text = args[0] if args else None
    if text is not None and not isinstance(text, str):
        raise mismatch(name, 1)
    return text


def decode_url_component(*args: object) -> str | None:
    """$decodeUrlComponent as Step Functions evaluates it: + is a space, a
    malformed escape such as %zz fails, and a broken UTF-8 sequence is U+FFFD
    (measured)."""
    text = text_argument("decodeUrlComponent", args)
    if text is None:
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", text):
        raise jsonata.JException(
            f"Malformed URL passed to $decodeUrlComponent(): {text}"
        )
    return urllib.parse.unquote_plus(text)


def base64_decode(*args: object) -> str | None:
    """$base64decode as Step Functions evaluates it: text missing its padding
    is read, and a character outside the alphabet fails (measured)."""
    text = text_argument("base64decode", args)
    if text is None:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise jsonata.JException(str(exc)) from exc


# The pictures the compiler writes for a format spec: whole numbers, grouped
# or not, with the digits after the decimal point the spec asked for, and the
# zeros a whole number is filled to, with the sign inside them.
GENERATED_PICTURE = re.compile(
    r"(?P<grouped>#,##)?0(?:\.(?P<decimals>0+))?|(?P<zeros>0+);-0+"
)


def format_number(*args: object) -> str | None:
    """$formatNumber as Step Functions evaluates it: the number is rounded
    half to even on the decimal it is written as, so 0.125 to two places is
    0.12 and 2.675 is 2.68 (measured). jsonata-python rounds the binary value
    instead, giving 0.13 and 2.67, so the pictures the compiler writes are
    evaluated here and any other is left to it. An undefined number gives
    undefined (measured)."""
    at_most("formatNumber", args, 3)
    value = args[0] if args else None
    picture = args[1] if len(args) > 1 else None
    options = args[2] if len(args) > 2 else None
    if value is None:
        return None
    if not is_number(value):
        raise mismatch("formatNumber", 1)
    if not isinstance(picture, str):
        raise mismatch("formatNumber", 2)
    if options is not None and not isinstance(options, dict):
        raise mismatch("formatNumber", 3)
    found = GENERATED_PICTURE.fullmatch(picture)
    if found is None or options is not None:
        return Functions.format_number(value, picture, options)
    places = len(found["decimals"] or "")
    with decimal.localcontext() as context:
        # The widest decimal a double holds is 309 digits, and the picture
        # asks for its own after the point.
        context.prec = 309 + places + 1
        written = decimal.Decimal(repr(value)).quantize(
            decimal.Decimal(1).scaleb(-places), rounding=decimal.ROUND_HALF_EVEN
        )
    if not found["zeros"]:
        return f"{written:,f}" if found["grouped"] else f"{written:f}"
    # The sign is written before the zeros and counts inside the width.
    width = len(found["zeros"])
    digits = f"{abs(written):f}"
    if written < 0:
        return "-" + digits.rjust(width - 1, "0")
    return digits.rjust(width, "0")


# A timezone as JSONata writes one. Step Functions reads utc as UTC, where
# jsonata-python fails on it (measured), and so does this for any other text.
TIMEZONE = re.compile(r"[+-]\d{4}")


def timezone_of(value: object) -> str | None:
    return value if isinstance(value, str) and TIMEZONE.fullmatch(value) else None


def now(*args: object) -> str:
    """$now(): the time in UTC to the millisecond, as Step Functions gives it,
    or written with the picture string and timezone given, which jsonata-python
    formats the way Step Functions does (measured)."""
    at_most("now", args, 2)
    picture = args[0] if args else None
    if picture is not None and not isinstance(picture, str):
        raise mismatch("now", 1)
    moment = datetime.now(UTC)
    if picture is None:
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    written = Functions.datetime_from_millis(
        int(moment.timestamp() * 1000),
        picture,
        timezone_of(args[1] if len(args) > 1 else None),
    )
    assert written is not None
    return written


def from_millis(*args: object) -> str | None:
    """$fromMillis, with the timezone read as Step Functions reads it. Millis
    that are not a number, text among them, give undefined (measured)."""
    at_most("fromMillis", args, 3)
    millis = args[0] if args else None
    picture = args[1] if len(args) > 1 else None
    if not is_number(millis):
        return None
    if picture is not None and not isinstance(picture, str):
        raise mismatch("fromMillis", 2)
    return Functions.datetime_from_millis(
        millis, picture, timezone_of(args[2] if len(args) > 2 else None)
    )


# The algorithms $hash takes, spelled only this way (measured).
ALGORITHMS = {
    "MD5": "md5",
    "SHA-1": "sha1",
    "SHA-256": "sha256",
    "SHA-384": "sha384",
    "SHA-512": "sha512",
}


def digest(*args: object) -> str | None:
    """$hash: the hex digest of the UTF-8 text. Undefined text or no algorithm
    gives undefined; no argument, text that is not a string, and an undefined
    or unknown algorithm fail (measured)."""
    at_most("hash", args, 2)
    if not args:
        raise mismatch("hash", 1)
    text = args[0]
    if text is None:
        return None
    if not isinstance(text, str):
        raise mismatch("hash", 1)
    if len(args) == 1:
        return None
    algorithm = args[1]
    name = ALGORITHMS.get(algorithm) if isinstance(algorithm, str) else None
    if name is None:
        shown = "null" if algorithm is None else algorithm
        raise jsonata.JException(
            f"D3137: Hash algorithm '{shown}' must be one of"
            " SHA-1, SHA-384, SHA-256, SHA-512, MD5"
        )
    return hashlib.new(name, text.encode()).hexdigest()


def partition(*args: object) -> list | None:
    """$partition: batches of the size, a fraction cut to a whole number. No
    items, undefined items or a size of 0 give undefined, and no size or an
    undefined one makes the items one batch, even when there are none. Items
    that are not an array, a size that is not a number, and a size below 0
    fail (measured)."""
    at_most("partition", args, 2)
    items = args[0] if args else None
    if items is None:
        return None
    if not isinstance(items, list):
        raise jsonata.JException(
            'T0412: Argument 1 of function "partition" must be an array of undefined'
        )
    size = args[1] if len(args) > 1 else None
    if size is None:
        return [items]
    if not is_number(size):
        raise mismatch("partition", 2)
    if size < 0:
        raise jsonata.JException("D3137: Second argument must be zero or greater")
    whole = int(size)
    if whole == 0:
        return None
    batches = [items[i : i + whole] for i in range(0, len(items), whole)]
    return batches or None


def range_numbers(*args: object) -> list[int] | int | None:
    """$range: from first by step through last, included when reached, each
    cut to a whole number. Fewer than three arguments, an undefined one or a
    step of 0 give undefined, and one that is not a number fails. The numbers
    are a sequence, as a path gives: none is undefined and one is that number
    (measured)."""
    at_most("range", args, 3)
    if len(args) < 3 or any(value is None for value in args):
        return None
    bounds: list[int] = []
    for position, value in enumerate(args, 1):
        if not is_number(value):
            raise mismatch("range", position)
        bounds.append(int(value))
    first, last, step = bounds
    if step == 0:
        return None
    result = []
    value = first
    while (value <= last) if step > 0 else (value >= last):
        result.append(value)
        value += step
    if len(result) < 2:
        return result[0] if result else None
    return result


def nulls(data: object) -> object:
    if data is None:
        return Utils.NULL_VALUE
    if isinstance(data, dict):
        return {k: nulls(v) for k, v in data.items()}
    if isinstance(data, list):
        return [nulls(v) for v in data]
    return data


def value(template: object, variables: Mapping[str, object], states: object) -> object:
    if (code := expression(template)) is not None:
        return evaluate(code, variables, states)
    if isinstance(template, dict):
        return {k: value(v, variables, states) for k, v in template.items()}
    if isinstance(template, list):
        return [value(v, variables, states) for v in template]
    return template


def matches(errors: list[str], error: str) -> bool:
    """States.Runtime is caught by nothing. States.ALL matches every other
    error. States.TaskFailed matches all but States.Timeout and
    States.QueryEvaluationError, so States.DataLimitExceeded from a result
    over the quota is matched."""
    if error == "States.Runtime":
        return False
    if error in errors or "States.ALL" in errors:
        return True
    return "States.TaskFailed" in errors and error not in {
        "States.Timeout",
        "States.QueryEvaluationError",
    }


def execution_context(execution_input: object) -> dict[str, object]:
    """The Context Object of an execution, with placeholder values. RedriveTime
    exists only in a redriven execution."""
    return {
        "Execution": {
            "Id": "arn:aws:states:us-east-1:123456789012:execution:machine:execution",
            "Input": execution_input,
            "Name": "execution",
            "RoleArn": "arn:aws:iam::123456789012:role/machine",
            "StartTime": "2026-01-01T00:00:00Z",
            "RedriveCount": 0,
        },
        "StateMachine": {
            "Id": "arn:aws:states:us-east-1:123456789012:stateMachine:machine",
            "Name": "machine",
        },
    }


def entered(
    context: Mapping[str, object],
    name: str,
    state: Mapping[str, object],
    retries: int = 0,
) -> dict[str, object]:
    """The Context Object in a state. State.RetryCount exists only in the
    states that retry (measured in a Task and a Map), and Task.Token only in a
    .waitForTaskToken Task."""
    about: dict[str, object] = {"EnteredTime": "2026-01-01T00:00:00Z", "Name": name}
    if state["Type"] in {"Task", "Parallel", "Map"}:
        about["RetryCount"] = retries
    entered = {**context, "State": about}
    resource = state.get("Resource")
    if isinstance(resource, str) and resource.endswith(".waitForTaskToken"):
        entered["Task"] = {"Token": "token"}
    return entered


def scope(
    definition: Mapping[str, object],
    variables: dict[str, object],
    state_input: object,
    context: Mapping[str, object],
    record: Record,
) -> object:
    """The states of a machine or a Parallel branch. A branch gets a copy of
    the variables, so it reads the outside and assigns its own."""
    states = definition["States"]
    assert isinstance(states, dict)
    name = definition["StartAt"]
    assert isinstance(name, str)
    for _ in range(10_000):
        state = states[name]
        record.states.append(name)
        frame = {"input": state_input, "context": entered(context, name, state)}
        kind = state["Type"]
        if kind == "Succeed":
            return value(state.get("Output", state_input), variables, frame)
        if kind == "Fail":
            raise Failure(
                str(value(state.get("Error", ""), variables, frame)),
                str(value(state.get("Cause", ""), variables, frame)),
            )
        if kind == "Choice":
            rule = next(
                (r for r in state["Choices"] if condition(r, variables, frame)),
                None,
            )
            if rule is None and "Default" not in state:
                raise Failure("States.NoChoiceMatched")
            # A rule that matches assigns and outputs by its own fields, and
            # the Default by the state's; the other's do not apply (measured).
            taken = state if rule is None else rule
            assigned, state_input = settled(taken, variables, frame, state_input)
            variables.update(assigned)
            name = state["Default"] if rule is None else rule["Next"]
            continue
        if kind in {"Task", "Parallel", "Map"}:
            retries: list[int] = []
            try:
                assigned, output = retried(
                    state, name, variables, state_input, context, record, retries
                )
            except Failure as failure:
                catcher = next(
                    (
                        c
                        for c in state.get("Catch", [])
                        if matches(c["ErrorEquals"], failure.error)
                    ),
                    None,
                )
                if catcher is None:
                    raise
                error_output = {"Error": failure.error, "Cause": failure.cause}
                frame = {
                    "input": state_input,
                    "context": entered(context, name, state, sum(retries)),
                    "errorOutput": error_output,
                }
                assigned, state_input = settled(catcher, variables, frame, error_output)
                variables.update(assigned)
                name = catcher["Next"]
                continue
        else:
            if kind == "Wait":
                waited(state, variables, frame)
            assigned, output = settled(state, variables, frame, state_input)
        variables.update(assigned)
        if state.get("End"):
            return output
        state_input = output
        name = state["Next"]
    raise AssertionError("the definition did not end within 10,000 states")


def condition(
    rule: Mapping[str, object], variables: Mapping[str, object], frame: object
) -> bool:
    test = value(rule["Condition"], variables, frame)
    if not isinstance(test, bool):
        raise Failure("States.QueryEvaluationError", f"{test!r} is not a boolean")
    return test


def waited(
    state: Mapping[str, object], variables: Mapping[str, object], frame: object
) -> None:
    """A Wait evaluates what it waits for, fails where Step Functions cannot
    read it (measured), and returns at once."""
    if "Seconds" in state:
        seconds = value(state["Seconds"], variables, frame)
        if not is_number(seconds) or not float(seconds).is_integer() or seconds < 0:
            raise Failure(
                "States.QueryEvaluationError",
                f"Seconds is {json.dumps(seconds)}, not a whole number of 0 or more",
            )
    else:
        timestamp = value(state["Timestamp"], variables, frame)
        if not isinstance(timestamp, str) or not offset_date_time(timestamp):
            raise Failure(
                "States.QueryEvaluationError",
                f"Timestamp is {json.dumps(timestamp)}, not an ISO-8601 date and"
                " time with an offset",
            )


# An ISO-8601 extended offset date-time, as a Wait reads its Timestamp: T or
# t, seconds and up to nine digits of their fraction optional, and Z, z or an
# offset (measured).
OFFSET_DATE_TIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})"
)


def offset_date_time(text: str) -> bool:
    """Whether the text is such a date-time, on a day and at a time that
    exist."""
    found = OFFSET_DATE_TIME.fullmatch(text)
    if found is None:
        return False
    year, month, day, hour, minute, second = (int(v or 0) for v in found.groups())
    try:
        datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return False
    return True


def settled(
    fields: Mapping[str, object],
    variables: Mapping[str, object],
    frame: object,
    default: object,
) -> tuple[dict[str, object], object]:
    """The Assign and the Output of a state, a Choice rule or a catcher. Both
    read the variables from before it (measured), and without an Output the
    output is the default given."""
    assign = fields.get("Assign", {})
    assert isinstance(assign, dict)
    assigned = {k: value(v, variables, frame) for k, v in assign.items()}
    output = (
        value(fields["Output"], variables, frame) if "Output" in fields else default
    )
    return assigned, output


def retried(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    state_input: object,
    context: Mapping[str, object],
    record: Record,
    retries: list[int],
) -> tuple[dict[str, object], object]:
    """A Task, Parallel or Map with its Retry, counting in retries the attempts
    of each retrier, which a Catch reads as State.RetryCount. A failure in
    Arguments, Assign or Output is retried too, and only the first retrier that
    matches counts it: once that one has no attempts left, the state fails even
    if a later one matches."""
    retriers = state.get("Retry", [])
    assert isinstance(retriers, list)
    retries[:] = [0] * len(retriers)
    while True:
        frame = {
            "input": state_input,
            "context": entered(context, name, state, sum(retries)),
        }
        try:
            return attempt(state, name, variables, frame, context, record)
        except Failure as failure:
            index = next(
                (
                    i
                    for i, retrier in enumerate(retriers)
                    if matches(retrier["ErrorEquals"], failure.error)
                ),
                None,
            )
            if index is None or retries[index] >= retriers[index].get("MaxAttempts", 3):
                raise
            retries[index] += 1


def attempt(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    frame: dict[str, object],
    context: Mapping[str, object],
    record: Record,
) -> tuple[dict[str, object], object]:
    kind = state["Type"]
    if kind == "Task":
        arguments = value(state.get("Arguments"), variables, frame)
        result = record.call(name, state["Resource"], arguments)
    elif kind == "Parallel":
        branches = state["Branches"]
        assert isinstance(branches, list)
        # Arguments, when given, is the input of every branch (measured).
        branch_input = (
            value(state["Arguments"], variables, frame)
            if "Arguments" in state
            else frame["input"]
        )
        result = [
            scope(branch, dict(variables), branch_input, context, record)
            for branch in branches
        ]
    else:
        result = run_map(state, name, variables, frame, context, record)
    # Assign and Output both read the variables from before the state, and
    # their errors are the state's to retry and catch.
    return settled(state, variables, {**frame, "result": result}, result)


def run_map(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    frame: Mapping[str, object],
    context: Mapping[str, object],
    record: Record,
) -> object:
    """A Map over Items, what its ItemReader reads, or without either, its
    input. Without a ProcessorConfig it is inline. Inline iterations read the
    variables around them; distributed ones are child executions whose input is
    their only data. The iterations run one after another, in the order of the
    items.

    An object of items passes each entry as {"Key": ..., "Value": ...}, whose
    fields Map.Item has too. A failed child execution takes its place in the
    result with its error and counts each of its items as failed; unless the
    thresholds tolerate them, the Map fails with
    States.ExceedToleratedFailureThreshold. With a ResultWriter, the result is
    where the results were written."""
    processor = state["ItemProcessor"]
    assert isinstance(processor, dict)
    config = processor.get("ProcessorConfig", {})
    assert isinstance(config, dict)
    distributed = config.get("Mode", "INLINE") == "DISTRIBUTED"
    if "ItemReader" in state:
        reader = value(state["ItemReader"], variables, frame)
        assert isinstance(reader, dict)
        items = record.call(name, reader["Resource"], reader.get("Arguments"))
    elif "Items" in state:
        items = value(state["Items"], variables, frame)
        if not isinstance(items, list) and not (
            isinstance(items, dict) and distributed and "ItemBatcher" not in state
        ):
            raise Failure(
                "States.QueryEvaluationError",
                f"Items is {json.dumps(items, default=str)}, not an array",
            )
    else:
        items = frame["input"]
        if not isinstance(items, list):
            raise Failure(
                "States.QueryEvaluationError",
                f"the input is {json.dumps(items)}, not an array",
            )
    if isinstance(items, dict):
        entries: list[dict[str, object]] = [
            {"Key": k, "Value": v} for k, v in items.items()
        ]
    else:
        assert isinstance(items, list)
        entries = [{"Value": v} for v in items]
    # Each child's input, with the number of items it takes.
    inputs: list[tuple[object, int]] = []
    if "ItemBatcher" in state:
        batcher = value(state["ItemBatcher"], variables, frame)
        assert isinstance(batcher, dict)
        size = batcher.get("MaxItemsPerBatch", len(items) or 1)
        for start in range(0, len(items), size):
            batch = {"Items": items[start : start + size]}
            if "BatchInput" in batcher:
                batch["BatchInput"] = batcher["BatchInput"]
            inputs.append((batch, len(batch["Items"])))
    elif "ItemSelector" in state:
        entered_context = frame["context"]
        assert isinstance(entered_context, dict)
        for position, entry in enumerate(entries):
            item = {"Index": position, **entry}
            selecting = {**entered_context, "Map": {"Item": item}}
            selected = value(
                state["ItemSelector"], variables, {**frame, "context": selecting}
            )
            inputs.append((selected, 1))
    else:
        objects = isinstance(items, dict)
        inputs = [(entry if objects else entry["Value"], 1) for entry in entries]
    results = []
    failed = 0
    for child_input, taken in inputs:
        if distributed:
            execution = context["Execution"]
            assert isinstance(execution, dict)
            child_context = {
                **context,
                "Execution": {**execution, "Input": child_input},
            }
            try:
                results.append(scope(processor, {}, child_input, child_context, record))
            except Failure as failure:
                failed += taken
                results.append(
                    {"Status": "FAILED", "Error": failure.error, "Cause": failure.cause}
                )
        else:
            results.append(
                scope(processor, dict(variables), child_input, context, record)
            )
    if failed:
        count = value(state.get("ToleratedFailureCount", 0), variables, frame)
        percentage = value(state.get("ToleratedFailurePercentage", 0), variables, frame)
        assert isinstance(count, int) and isinstance(percentage, int)
        if exceeds(failed, len(entries), count, percentage):
            raise Failure("States.ExceedToleratedFailureThreshold", EXCEEDED)
    if "ResultWriter" in state:
        writer = value(state["ResultWriter"], variables, frame)
        assert isinstance(writer, dict)
        arguments = writer["Arguments"]
        return {
            "MapRunArn": f"{MAP_RUN}/{state.get('Label', 'map')}:run",
            "ResultWriterDetails": {
                "Bucket": arguments["Bucket"],
                "Key": f"{arguments['Prefix']}/run/manifest.json",
            },
        }
    return results


def exceeds(failed: int, total: int, count: int, percentage: int) -> bool:
    """Whether failed items fail a distributed Map: with no threshold set, any
    failure does, and otherwise exceeding one that is set does. A threshold of 0
    is not set, though the documentation does not say so."""
    if not count and not percentage:
        return failed > 0
    return (
        bool(count)
        and failed > count
        or bool(percentage)
        and failed * 100 > percentage * total
    )


__all__ = [
    "Call",
    "Execution",
    "Failure",
    "InvalidDefinition",
    "Tasks",
    "Unsupported",
    "run",
]
