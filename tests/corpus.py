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
from datetime import datetime

from sfnx import TaskFailed, context, inline_map, parallel, state_machine


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
    every evaluation. backs is a phrase of docs/design.md naming the
    behavior the compiler relies on that the case runs, whose paragraph names
    the case in turn, and states the types of the definition's top-level
    states, in order, where the case runs that behavior only while the
    compiler writes those states."""

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
    backs: str = ""
    states: tuple[str, ...] = ()

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


def two_equal_numbers_below_one(value: object) -> bool:
    return (
        two_numbers_below_one(value)
        and isinstance(value, list)
        and value[0] == value[1]
    )


# The conditions a result on AWS is judged by when its value cannot be fixed.
CONDITIONS: Mapping[str, Callable[[object], bool]] = {
    "a number in [0, 0.5)": lambda v: is_number(v) and 0 <= v < 0.5,
    "false": lambda v: v is False,
    "two numbers in [0, 1)": two_numbers_below_one,
    "two equal numbers in [0, 1)": two_equal_numbers_below_one,
}

COMPREHENSION = 'xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]'
NESTED = 'xs: list = input["xs"]\nreturn [[x] for x in xs]'
ENTRIES = 'ps: list[dict[str, str]] = input["ps"]\nreturn {p["k"]: p["v"] for p in ps}'
KEPT = 'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 1}'
QUANTIFIED = 'xs: list = input["xs"]\nreturn {}(xs)'
SHORT_CIRCUIT = 'xs: list[float] = input["xs"]\nreturn {}(10 / x > 1 for x in xs)'
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

FOLDED_INTO_A_PARALLEL = """\
def ok():
    return {"k": 1}

try:
    r = parallel(ok)
    return r[0]["missing"]
except Exception:
    return "caught"
"""
FOLDED_INTO_A_MAP = """\
def f(x):
    return {"k": x}

try:
    rs = inline_map(f, [1])
    return rs[0]["missing"]
except Exception:
    return "caught"
"""
CATCH_IN_A_MAP = """\
xs: list = input["xs"]
status = "new"

def check(x):
    if x > 1:
        raise Declined("too big")
    return x

try:
    ys = inline_map(check, xs)
    status = "done"
except Declined as e:
    return {"status": status, "message": str(e)}
return ys
"""
ASSIGNS_NOTHING = """\
status = "old"

def ok():
    return {"k": 1}

try:
    r = parallel(ok)
    y = r[0]["missing"]
    status = "new"
    return y
except Exception:
    return status
"""
READS_THE_RESULT = """\
r = "none"

def ok():
    return {"k": 1}

try:
    r = parallel(ok)
    y = r[0]["missing"]
    return y
except Exception:
    return r
"""
FLAG = """\
failed_at = None

def fail():
    raise Declined("no")

try:
    parallel(fail)
except Declined:
    failed_at = "payment"
if failed_at is not None:
    return {"failed": failed_at}
return "ok"
"""
RETRY_COUNT = """\
def f(n):
    if n < 2:
        raise Declined("again")
    return n

return inline_map(
    f,
    [context["State"]["RetryCount"]],
    retry=[{"ErrorEquals": [Declined], "MaxAttempts": 3, "IntervalSeconds": 1}],
)
"""
TASK_FAILED = """\
def f(n):
    return {"k": n}

try:
    rs = inline_map(
        f,
        [context["State"]["RetryCount"]],
        retry=[{"ErrorEquals": [TaskFailed], "MaxAttempts": 2, "IntervalSeconds": 1}],
    )
    return 1 / rs[0]["k"]
except Exception:
    return "not retried"
"""
RULE_ASSIGNS = """\
x: float = input["x"]
if x > 1:
    y = x + 1
    if y > 3:
        return "big"
    return y
else:
    y = x - 1
return y
"""
COUNTED_DOWN = """\
n: float = input["n"]
if n > 0:
    n = n - 1
    if n > 0:
        return "two or more"
    return "one"
return "none"
"""
GUARDED = """\
d: dict[str, float] = input["d"]
if d["k"] != 0:
    if 10 / d["k"] > 1:
        return "big"
    return "small"
return "zero"
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
        "format-spec-half-to-even",
        "numbers",
        'return f"{0.125:.2f}"',
        {},
        Value("0.12"),
        "$formatNumber rounds half to even, as Python's format() does, and "
        "0.125 is a value a double holds exactly",
    ),
    Case(
        "format-spec-zeros-hold-the-sign",
        "numbers",
        'return f"{-12:05d}"',
        {},
        Value("-0012"),
        "Python counts the sign inside the zeros of a width, which the picture "
        "says with a negative sub-picture one digit shorter",
    ),
    Case(
        "format-spec-decimal-digits",
        "numbers",
        'return f"{2.675:.2f}"',
        {},
        Value("2.68"),
        "$formatNumber rounds the decimal the number is written as, where "
        "CPython rounds the double it holds (2.67499...), as round(2.675, 2) "
        "already does",
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
        "any-of-none",
        "quantifiers",
        QUANTIFIED.format("any"),
        {"xs": []},
        Value(False),
        "$reduce of an empty array gives the value it starts from, which is "
        "any()'s False",
    ),
    Case(
        "all-of-none",
        "quantifiers",
        QUANTIFIED.format("all"),
        {"xs": []},
        Value(True),
        "$reduce of an empty array gives the value it starts from, which is "
        "all()'s True",
    ),
    Case(
        "any-nested-empty-list",
        "quantifiers",
        QUANTIFIED.format("any"),
        {"xs": [[]]},
        Value(False),
        "an item is read for its truth as bool() reads it: an empty list is "
        "false, where $boolean of [[]] would look into the items",
    ),
    Case(
        "all-nested-list-of-zero",
        "quantifiers",
        QUANTIFIED.format("all"),
        {"xs": [[0]]},
        Value(True),
        "a list with items is true, whatever the items are, where $boolean of "
        "[[0]] is false",
    ),
    Case(
        "any-stops-at-the-first-true-item",
        "quantifiers",
        SHORT_CIRCUIT.format("any"),
        {"xs": [1, 0]},
        Value(True),
        "the first item decides the result, so the second, which divides by "
        "zero, is never evaluated, as Python never evaluates it",
    ),
    Case(
        "all-stops-at-the-first-false-item",
        "quantifiers",
        SHORT_CIRCUIT.format("all"),
        {"xs": [-1, 0]},
        Value(False),
        "the first item decides the result, so the second, which divides by "
        "zero, is never evaluated, as Python never evaluates it",
    ),
    Case(
        "any-of-a-list-comprehension-reads-every-item",
        "quantifiers",
        'xs: list[float] = input["xs"]\nreturn any([10 / x > 1 for x in xs])',
        {"xs": [1, 0]},
        Error(QUERY_ERROR),
        "a list comprehension is built before any() reads it, so the item that "
        "divides by zero fails the execution; CPython raises ZeroDivisionError, "
        "so only the failure is compared",
        python=False,
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
        "strftime-picture",
        "times",
        'return datetime.fromtimestamp(1789479786).strftime("%Y-%m-%d %H:%M:%S")',
        {},
        Value("2026-09-15 13:43:06"),
        "the picture components of $fromMillis write what the directives of "
        "strftime write; Step Functions has no local time zone, so the text is "
        "of the UTC time, where CPython writes the local one",
        python=False,
    ),
    Case(
        "strftime-literals",
        "times",
        'return datetime.fromtimestamp(0).strftime("100%% [ok] %y-%j")',
        {},
        Value("100% [ok] 70-001"),
        "the text between the directives is kept as it is, where a picture "
        "string reads [ and ] as the ends of a component, so each is written "
        "twice",
        python=False,
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
        backs="JSONata's `and` and `or` evaluate no more once the first side decides",
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
        backs="a catcher's also read `$states.errorOutput` and `$states.input`",
    ),
    Case(
        "catch-takes-a-failure-folded-into-a-parallel",
        "catch",
        FOLDED_INTO_A_PARALLEL,
        {},
        Value("caught"),
        "the return goes in the Output of the Parallel before it, whose Catch "
        "takes the Output's failure as the except takes the KeyError",
        backs="whose Catch takes a failure of either",
        states=("Parallel", "Succeed"),
    ),
    Case(
        "catch-takes-a-failure-folded-into-a-map",
        "catch",
        FOLDED_INTO_A_MAP,
        {},
        Value("caught"),
        "the return goes in the Output of the Map before it, whose Catch takes "
        "the Output's failure as the except takes the KeyError",
        backs="whose Catch takes a failure of either",
        states=("Map", "Succeed"),
    ),
    Case(
        "catch-in-a-map-reads-the-error",
        "catch",
        CATCH_IN_A_MAP,
        {"xs": [1, 2]},
        Value({"status": "new", "message": "too big"}),
        "a Fail in an inline Map's processor fails the Map, whose catcher "
        "assigns the error output, and the assignment the Map holds after it "
        "is not made",
        backs="a catcher's also read `$states.errorOutput` and `$states.input`",
        states=("Pass", "Map", "Succeed"),
    ),
    Case(
        "failed-assign-assigns-nothing",
        "catch",
        ASSIGNS_NOTHING,
        {},
        Value("old"),
        "status is assigned after the statement that fails, in the same Assign "
        "of the Parallel, and a failing Assign assigns none of its variables, "
        "so the except reads the value from before the try, as in Python",
        backs="A failing `Assign` assigns nothing, the state's result included",
        states=("Pass", "Parallel", "Succeed"),
    ),
    Case(
        "except-reading-the-result-keeps-the-pass",
        "catch",
        READS_THE_RESULT,
        {},
        Error(QUERY_ERROR),
        "the except reads the result, which a failing Assign would not assign, "
        "so the statement that fails keeps its Pass, which no Catch covers; "
        "CPython runs the except, a difference the language reference lists",
        python=False,
        backs="A failing `Assign` assigns nothing, the state's result included",
        states=("Pass", "Parallel", "Pass", "Succeed", "Succeed"),
    ),
    Case(
        "catch-sets-the-flag-a-choice-tests",
        "catch",
        FLAG,
        {},
        Value({"failed": "payment"}),
        "every way to the if gives the flag a value written in the source, so "
        "the catcher, which assigns the flag, leads straight to the return the "
        "test would send it to, and the Choice goes",
        backs="the test is decided there",
        states=("Pass", "Parallel", "Succeed"),
    ),
    Case(
        "map-retry-evaluates-the-items-again",
        "retry",
        RETRY_COUNT,
        {},
        Value([2]),
        "a retry runs the whole Map state again, Items included, where "
        "State.RetryCount counts the retries before the attempt; CPython has "
        "no context and no retries",
        python=False,
        backs="the number of retries before the current attempt",
        states=("Map",),
    ),
    Case(
        "task-failed-retrier-misses-a-failing-output",
        "retry",
        TASK_FAILED,
        {},
        Value("not retried"),
        "the return goes in the Output of the Map, whose retrier for "
        "States.TaskFailed does not match the Output's failure, so the Catch "
        "takes it at once instead of the Map running again with a divisor of "
        "1; CPython has no context and no retries",
        python=False,
        backs="A `States.TaskFailed` retrier does not match a failing `Output`",
        states=("Map", "Succeed"),
    ),
    Case(
        "choice-rule-assigns-by-its-own-assign",
        "choice",
        RULE_ASSIGNS,
        {"x": 1.5},
        Value(2.5),
        "the assignment that starts the if branch goes in the Choice rule, "
        "which assigns by its own Assign and not by the state's",
        backs="which run only when the `Default` is taken",
        states=("Choice", "Succeed", "Succeed"),
    ),
    Case(
        "choice-default-assigns-by-the-states-assign",
        "choice",
        RULE_ASSIGNS,
        {"x": 0.5},
        Value(-0.5),
        "the assignment of the else branch goes in the Choice's own Assign, "
        "which the Default applies",
        backs="which run only when the `Default` is taken",
        states=("Choice", "Succeed", "Succeed"),
    ),
    Case(
        "choice-taken-in-reads-what-the-rule-assigns",
        "choice",
        RULE_ASSIGNS,
        {"x": 2.5},
        Value("big"),
        "the inner if is taken into the outer Choice, whose test reads y as "
        "the expression the rule assigns it",
        backs="the same variables and the same input",
        states=("Choice", "Succeed", "Succeed"),
    ),
    Case(
        "choice-taken-in-reads-the-value-from-before",
        "choice",
        COUNTED_DOWN,
        {"n": 1},
        Value("one"),
        "the rule assigns n again, and the test taken in reads n - 1 with the "
        "n from before the Choice, as the rule's Assign does",
        backs="the same variables and the same input",
        states=("Choice", "Succeed", "Succeed", "Succeed"),
    ),
    Case(
        "choice-taken-in-is-not-evaluated-past-a-false-test",
        "choice",
        GUARDED,
        {"d": {"k": 0}},
        Value("zero"),
        "the test taken in divides by k, which fails where k is 0; it follows "
        "the outer test with and, which evaluates no more once that is false",
        backs="JSONata's `and` and `or` evaluate no more once the first side decides",
        states=("Choice", "Succeed", "Succeed", "Succeed"),
    ),
    Case(
        "volatile-read-twice-keeps-its-state",
        "volatile",
        "x = random.random()\nreturn [x, x]",
        {},
        Value([0.75, 0.75]),
        "each {% %} of an Output is evaluated on its own, so a random value "
        "read twice keeps the Pass that assigns it, and both items are the "
        "same value",
        python=False,
        random=(0.75,),
        calls=1,
        on_aws=Condition("two equal numbers in [0, 1)"),
        backs="each `{% %}` is evaluated on its own",
        states=("Pass", "Succeed"),
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


def state_types(definition: Mapping[str, object]) -> tuple[str, ...]:
    """The types of a definition's top-level states, in the order written."""
    states = definition["States"]
    assert isinstance(states, dict)
    return tuple(state["Type"] for state in states.values())


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
    needs no state machine and no role, and anything else as an execution,
    a single Map or Parallel included, as TestState refuses those."""
    types = state_types(definition)
    if len(types) == 1 and types[0] not in ("Map", "Parallel"):
        return "test-state"
    return "execution"


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
