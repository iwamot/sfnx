"""A fixed corpus of small programs whose meaning in Step Functions is worth
checking again whenever the translator or the interpreter changes. Each case
records its source, its input, what it must give and why, and which
guarantees apply: whether CPython gives the same value, and how the result is
judged on AWS when the value itself cannot be compared.

Every test run runs the corpus locally through the interpreter, with $random
replaced by a fixed sequence whose calls are counted. tests/aws_corpus.py runs
the same definitions in Step Functions on request, where a volatile result is
judged by a named condition instead of a value, and the number of calls is
not measured.

The random programs of tests/test_differential.py keep to what CPython and ASL
mean the same way; this corpus holds the edges, written by hand."""

import json
import textwrap
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from sfnx.compiler import compile_source
from tests import asl

HEADER = """\
import base64
import random
import urllib.parse
from sfnx import parallel, state_machine


class Declined(Exception):
    pass


@state_machine
def main(input):
"""
QUERY_ERROR = "States.QueryEvaluationError"
UNMEASURED = "unmeasured"


@dataclass(frozen=True)
class Value:
    """The result must equal this JSON value."""

    value: object


@dataclass(frozen=True)
class Error:
    """The execution must fail with this error name."""

    error: str


@dataclass(frozen=True)
class Condition:
    """The result must satisfy the condition of this name, in CONDITIONS."""

    name: str


Expectation = Value | Error | Condition


@dataclass(frozen=True)
class Result:
    value: object


@dataclass(frozen=True)
class Failed:
    error: str
    cause: str = ""


Outcome = Result | Failed


@dataclass(frozen=True)
class Case:
    """One program with one input. expected is what a local run gives, with
    $random returning the values of random in order and called calls times.
    python says whether CPython gives the same value, which is the guarantee
    the case claims; on_aws, when set, is the condition the result must
    satisfy on AWS in place of the value, for a result that changes on
    every evaluation."""

    id: str
    category: str
    body: str
    input: object
    expected: Value | Error
    why: str
    python: bool = True
    random: tuple[float, ...] = ()
    calls: int = 0
    on_aws: Condition | None = None

    @property
    def source(self) -> str:
        return HEADER + textwrap.indent(self.body, "    ")

    @property
    def remote(self) -> Expectation:
        return self.expected if self.on_aws is None else self.on_aws


@dataclass(frozen=True)
class Verdict:
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class LocalRun:
    outcome: Outcome
    calls: int


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def two_numbers_below_one(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(is_number(v) and 0 <= v < 1 for v in value)
    )


# The conditions a result on AWS is judged by when its value cannot be fixed.
CONDITIONS: Mapping[str, Callable[[object], bool]] = {
    "a number in [0, 0.5)": lambda v: is_number(v) and 0 <= v < 0.5,
    "false": lambda v: v is False,
    "two numbers in [0, 1)": two_numbers_below_one,
}

COMPREHENSION = 'xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]'
NESTED = 'xs: list = input["xs"]\nreturn [[x] for x in xs]'
ENTRIES = 'ps: list[dict[str, str]] = input["ps"]\nreturn {p["k"]: p["v"] for p in ps}'
KEPT = 'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 1}'
REWRITTEN = (
    'd: dict[str, float] = input["d"]\nreturn {k: v + 1 for k, v in d.items() if v > 1}'
)
CATCH = """\
status = "new"

def fail():
    raise Declined("no")

try:
    parallel(fail)
    status = "done"
except Declined as e:
    return {"status": status, "message": str(e)}
return status
"""

CASES: tuple[Case, ...] = (
    Case(
        "truth-nested-empty-list",
        "truth",
        'return bool(input["x"])',
        {"x": [[]]},
        Value(True),
        "a list is true when it has items, whatever they are; JSONata's $boolean "
        "looks into the items, so the generated expression counts them instead",
    ),
    Case(
        "truth-zero",
        "truth",
        'return bool(input["x"])',
        {"x": 0},
        Value(False),
        "0 is false in Python and in JSONata",
    ),
    Case(
        "truth-empty-dict",
        "truth",
        'return bool(input["x"])',
        {"x": {}},
        Value(False),
        "an empty dict is false in Python and an empty object in JSONata",
    ),
    Case(
        "truth-text-zero",
        "truth",
        'return bool(input["x"])',
        {"x": "0"},
        Value(True),
        "a string is true when it is not empty, whatever it says",
    ),
    Case(
        "int-truncates-toward-zero",
        "numbers",
        'return int(input["x"])',
        {"x": -1.5},
        Value(-1),
        "int() truncates toward zero, which is $ceil below zero and $floor above",
    ),
    Case(
        "int-of-text",
        "numbers",
        'return int(input["x"])',
        {"x": "42"},
        Value(42),
        "int() reads a string of digits, as $number does",
    ),
    Case(
        "division",
        "numbers",
        'return input["a"] / input["b"]',
        {"a": 7, "b": 2},
        Value(3.5),
        "/ divides as Python does, without truncating",
    ),
    Case(
        "division-by-zero",
        "numbers",
        'return input["a"] / input["b"]',
        {"a": 1, "b": 0},
        Error(QUERY_ERROR),
        "JSONata gives the string Infinity, so the divisor is tested and the "
        "execution fails where it divides; CPython raises ZeroDivisionError, "
        "so only the failure is compared",
        python=False,
    ),
    Case(
        "join-list",
        "join",
        'return ",".join(input["xs"])',
        {"xs": ["a", "b"]},
        Value("a,b"),
        "join puts the separator between the items",
    ),
    Case(
        "join-none",
        "join",
        'return ",".join(input["xs"])',
        {"xs": []},
        Value(""),
        "join of no items is the empty string; $join([]) is too",
    ),
    Case(
        "join-characters",
        "join",
        'return ",".join(input["xs"])',
        {"xs": "ab"},
        Value("a,b"),
        "Python joins the characters of a string, so a string is split first",
    ),
    Case(
        "unpack-later-key-wins",
        "unpack",
        'return {**input["d"], "k": 1}',
        {"d": {"k": 0, "j": 2}},
        Value({"k": 1, "j": 2}),
        "a key written after the unpacking replaces the unpacked one, as $merge "
        "keeps the last",
    ),
    Case(
        "unpack-rejects-a-list",
        "unpack",
        'return {**input["d"], "k": 1}',
        {"d": [1]},
        Error(QUERY_ERROR),
        "** unpacks dicts only; CPython raises TypeError, so only the failure "
        "is compared",
        python=False,
    ),
    Case(
        "comprehension-of-none",
        "lists",
        COMPREHENSION,
        {"xs": []},
        Value([]),
        "$map of no items is undefined; the brackets make it an empty list",
    ),
    Case(
        "comprehension-of-one",
        "lists",
        COMPREHENSION,
        {"xs": [1]},
        Value([2]),
        "$map of one item is the value itself; the brackets keep it a list",
    ),
    Case(
        "comprehension-of-several",
        "lists",
        COMPREHENSION,
        {"xs": [1, 2]},
        Value([2, 4]),
        "$map of several items is a list already",
    ),
    Case(
        "comprehension-of-one-list",
        "lists",
        NESTED,
        {"xs": [1]},
        Value([[1]]),
        "a single result that is a list would lose a level in brackets; "
        "$append([], $map(...)[]) keeps it",
    ),
    Case(
        "comprehension-of-one-nested-list",
        "lists",
        NESTED,
        {"xs": [[1]]},
        Value([[[1]]]),
        "the level is kept when the item is a list too",
    ),
    Case(
        "dict-comprehension-of-none",
        "dicts",
        ENTRIES,
        {"ps": []},
        Value({}),
        "$map of no items is undefined, and $merge of no object is {}",
    ),
    Case(
        "dict-comprehension-of-one",
        "dicts",
        ENTRIES,
        {"ps": [{"k": "a", "v": "1"}]},
        Value({"a": "1"}),
        "$map of one item is the object itself, and the brackets around it make "
        "it the one-object array $merge takes",
    ),
    Case(
        "dict-comprehension-repeated-key",
        "dicts",
        ENTRIES,
        {"ps": [{"k": "a", "v": "1"}, {"k": "a", "v": "2"}]},
        Value({"a": "2"}),
        "$merge gives a later object's key precedence, as a later entry of a "
        "dict comprehension wins in Python",
    ),
    Case(
        "dict-comprehension-list-value",
        "dicts",
        'xs: list[str] = input["xs"]\nys: list = input["ys"]\nreturn {x: ys for x in xs}',
        {"xs": ["a"], "ys": [1, 2]},
        Value({"a": [1, 2]}),
        "an object keeps a list as one value, where an array constructor would "
        "merge its items",
    ),
    Case(
        "items-comprehension-keeps-none",
        "dicts",
        KEPT,
        {"d": {"a": 1}},
        Value({}),
        "$sift that keeps no entry returns undefined, which $merge of no object "
        "turns into {}",
    ),
    Case(
        "items-comprehension-keeps-some",
        "dicts",
        KEPT,
        {"d": {"a": 1, "b": 2}},
        Value({"b": 2}),
        "$sift keeps the entries its function is true for, with the key and the "
        "value as they are",
    ),
    Case(
        "items-comprehension-rewrites-the-value",
        "dicts",
        REWRITTEN,
        {"d": {"a": 1, "b": 2}},
        Value({"b": 3}),
        "$each returns nothing for an entry the condition drops, so the "
        "condition stays in the function rather than filtering the dict first",
    ),
    Case(
        "get-missing-key",
        "missing",
        'return input["d"].get("k")',
        {"d": {}},
        Value(None),
        "get of a missing key is None; $lookup gives undefined, so it is tested "
        "with $exists and null is written",
    ),
    Case(
        "get-null",
        "missing",
        'return input["d"].get("k")',
        {"d": {"k": None}},
        Value(None),
        "a key present with null is null, which $exists tells from a missing one",
    ),
    Case(
        "get-present",
        "missing",
        'return input["d"].get("k")',
        {"d": {"k": 1}},
        Value(1),
        "a present key gives its value",
    ),
    Case(
        "is-none",
        "missing",
        'return input["x"] is None',
        {"x": None},
        Value(True),
        "is None is true for null",
    ),
    Case(
        "missing-key-fails",
        "missing",
        'return input["missing"]',
        {},
        Error(QUERY_ERROR),
        "a missing key is undefined, which fails as an Output; CPython raises "
        "KeyError, so only the failure is compared",
        python=False,
    ),
    Case(
        "base64-encode-utf8",
        "encoding",
        'return base64.b64encode(input["s"].encode()).decode()',
        {"s": "日本 a"},
        Value("5pel5pysIGE="),
        "$base64encode reads the text as UTF-8, as .encode() does",
    ),
    Case(
        "base64-decode-utf8",
        "encoding",
        'return base64.b64decode(input["s"]).decode()',
        {"s": "5pel5pysIGE="},
        Value("日本 a"),
        "$base64decode gives the text back as UTF-8, as .decode() does",
    ),
    Case(
        "base64-decode-without-padding",
        "encoding",
        'return base64.b64decode(input["s"]).decode()',
        {"s": "YWJ"},
        Value("ab"),
        "$base64decode reads text missing its padding; Python raises "
        "binascii.Error, so the value is not compared",
        python=False,
    ),
    Case(
        "base64-decode-outside-the-alphabet",
        "encoding",
        'return base64.b64decode(input["s"]).decode()',
        {"s": "!!"},
        Error(QUERY_ERROR),
        "$base64decode fails on a character outside the alphabet; Python "
        "discards it and gives an empty string, so only the failure is compared",
        python=False,
    ),
    Case(
        "unquote-keeps-plus",
        "encoding",
        'return urllib.parse.unquote(input["s"])',
        {"s": "a+b%2Bc%20d"},
        Value("a+b+c d"),
        "$decodeUrlComponent reads + as a space, so unquote() escapes it first "
        "to keep it, as Python does",
    ),
    Case(
        "unquote-plus-reads-space",
        "encoding",
        'return urllib.parse.unquote_plus(input["s"])',
        {"s": "a+b%2Bc%20d"},
        Value("a b+c d"),
        "unquote_plus() reads + as a space, as $decodeUrlComponent does",
    ),
    Case(
        "unquote-malformed",
        "encoding",
        'return urllib.parse.unquote(input["s"])',
        {"s": "%zz"},
        Error(QUERY_ERROR),
        "$decodeUrlComponent fails on a malformed escape; Python passes it "
        "through, so only the failure is compared",
        python=False,
    ),
    Case(
        "unquote-broken-utf8",
        "encoding",
        'return urllib.parse.unquote(input["s"])',
        {"s": "%E6"},
        Value("�"),
        "a broken UTF-8 sequence is U+FFFD in both",
    ),
    Case(
        "volatile-modulo-once",
        "volatile",
        "return random.random() % 0.5",
        {},
        Value(0.25),
        "% reads its left operand twice, so a value that changes on evaluation "
        "is bound once; 0.75 % 0.5 is 0.25 when the same value is read both "
        "times",
        python=False,
        random=(0.75, 0.25),
        calls=1,
        on_aws=Condition("a number in [0, 0.5)"),
    ),
    Case(
        "volatile-short-circuit",
        "volatile",
        "return False and random.random()",
        {},
        Value(False),
        "and does not evaluate its right operand when the left is false",
        python=False,
        calls=0,
        on_aws=Condition("false"),
    ),
    Case(
        "volatile-separate-calls",
        "volatile",
        "return [random.random(), random.random()]",
        {},
        Value([0.75, 0.25]),
        "two calls written out are two calls, not one value read twice",
        python=False,
        random=(0.75, 0.25),
        calls=2,
        on_aws=Condition("two numbers in [0, 1)"),
    ),
    Case(
        "catch-reads-what-was-assigned-before",
        "catch",
        CATCH,
        {},
        Value({"status": "new", "message": "no"}),
        "a handler sees the variables from before the state that failed, not "
        "the assignments after it, and the Catch assigns the error to the "
        "outer scope where str(e) reads its cause",
    ),
)


class Exhausted(Exception):
    pass


class Sequence:
    """$random as the local run replaces it: the values in order, counting the
    calls, and failing past the end so a call more than expected is never
    answered with a made-up value."""

    def __init__(self, values: Iterable[float]):
        self.values = tuple(values)
        self.calls = 0

    def __call__(self) -> float:
        if self.calls >= len(self.values):
            raise Exhausted(f"$random called more than {len(self.values)} times")
        value = self.values[self.calls]
        self.calls += 1
        return value


def compiled(case: Case) -> dict:
    (definition,) = compile_source(case.source).values()
    return definition


def run_locally(case: Case) -> LocalRun:
    sequence = Sequence(case.random)
    with asl.replaced(random=sequence):
        try:
            outcome: Outcome = Result(asl.run(compiled(case), case.input))
        except asl.Failure as failure:
            outcome = Failed(failure.error, failure.cause)
    return LocalRun(outcome, sequence.calls)


def in_python(case: Case) -> Result:
    """The case run by CPython, for the cases that claim CPython agrees."""
    namespace: dict[str, object] = {}
    exec(compile(case.source, "<case>", "exec"), namespace)
    main = namespace["main"]
    assert callable(main)
    return Result(main(case.input))


def judge(expectation: Expectation, outcome: Outcome) -> Verdict:
    """Whether an outcome is what a case expects, in the environment the
    expectation is for."""
    if isinstance(outcome, Failed):
        if isinstance(expectation, Error):
            if outcome.error == expectation.error:
                return Verdict(True)
            return Verdict(
                False, f"failed with {outcome.error}, not {expectation.error}"
            )
        cause = f": {outcome.cause}" if outcome.cause else ""
        return Verdict(False, f"failed with {outcome.error}{cause}")
    if isinstance(expectation, Error):
        return Verdict(False, f"gave {json.dumps(outcome.value)} instead of failing")
    if isinstance(expectation, Condition):
        if CONDITIONS[expectation.name](outcome.value):
            return Verdict(True)
        return Verdict(False, f"{json.dumps(outcome.value)} is not {expectation.name}")
    if same(outcome.value, expectation.value):
        return Verdict(True)
    return Verdict(
        False, f"gave {json.dumps(outcome.value)}, not {json.dumps(expectation.value)}"
    )


def judge_locally(case: Case, run: LocalRun) -> Verdict:
    """The value and the number of $random calls, which the local run fixes."""
    verdict = judge(case.expected, run.outcome)
    if not verdict.passed:
        return verdict
    if run.calls != case.calls:
        return Verdict(False, f"$random was called {run.calls} times, not {case.calls}")
    return verdict


def same(left: object, right: object) -> bool:
    """JSON equality: numbers by value, booleans apart from numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same(left[k], right[k]) for k in left
        )
    return type(left) is type(right) and left == right


def select(
    cases: Iterable[Case], ids: Iterable[str], categories: Iterable[str]
) -> list[Case]:
    """The cases with one of the ids or in one of the categories, or all of
    them when neither is given. An id or a category that matches nothing is an
    error, so a typo does not pass as an empty run."""
    cases = list(cases)
    wanted_ids = set(ids)
    wanted_categories = set(categories)
    unknown = wanted_ids - {c.id for c in cases}
    unknown |= wanted_categories - {c.category for c in cases}
    if unknown:
        raise ValueError(f"no such case or category: {', '.join(sorted(unknown))}")
    if not wanted_ids and not wanted_categories:
        return cases
    return [c for c in cases if c.id in wanted_ids or c.category in wanted_categories]


def route_of(definition: Mapping[str, object]) -> str:
    """How AWS runs a definition: a single state through TestState, which
    needs no state machine and no role, and anything else as an execution."""
    states = definition["States"]
    assert isinstance(states, dict)
    return "test-state" if len(states) == 1 else "execution"


def outcome_of(response: Mapping[str, object]) -> Outcome:
    """The outcome in a TestState or StartSyncExecution response, which
    carry the same fields: a JSON output, or an error and a cause. A failure
    without an error name, such as TIMED_OUT, keeps its status as the name."""
    status = str(response.get("status", ""))
    if status == "SUCCEEDED":
        output = response["output"]
        assert isinstance(output, str)
        return Result(json.loads(output))
    return Failed(str(response.get("error", status)), str(response.get("cause", "")))


def described(expectation: Expectation) -> dict[str, object]:
    if isinstance(expectation, Value):
        return {"kind": "value", "value": expectation.value}
    if isinstance(expectation, Error):
        return {"kind": "error", "error": expectation.error}
    return {"kind": "condition", "condition": expectation.name}


def actual(outcome: Outcome | None) -> dict[str, object] | None:
    if outcome is None:
        return None
    if isinstance(outcome, Result):
        return {"value": outcome.value}
    return {"error": outcome.error, "cause": outcome.cause}


def record(
    case: Case,
    definition: Mapping[str, object],
    *,
    route: str,
    status: str,
    outcome: Outcome | None = None,
    detail: str = "",
    region: str,
    version: str,
    time: str,
) -> dict[str, object]:
    """One case's line of the results file. status is passed, mismatch,
    api-error or not-run, so a case never run reads as one; calls on AWS
    is unmeasured, as only the local run counts them."""
    return {
        "id": case.id,
        "category": case.category,
        "source": case.source,
        "definition": definition,
        "input": case.input,
        "expected": described(case.remote),
        "cpython": "agrees" if case.python else "not compared",
        "route": route,
        "region": region,
        "sfnx": version,
        "time": time,
        "status": status,
        "actual": actual(outcome),
        "detail": detail,
        "calls": UNMEASURED,
    }


def status_of(verdict: Verdict) -> str:
    return "passed" if verdict.passed else "mismatch"
