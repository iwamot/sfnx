"""The corpus run locally, and the judgments the AWS runner shares with it."""

import pytest

from tests import asl, corpus
from tests.corpus import CASES, Condition, Error, Failed, Result, Value

RESULT_OF = {
    "output": "[0.1, 0.2]",
    "status": "SUCCEEDED",
}
FAILURE_OF = {
    "error": "States.QueryEvaluationError",
    "cause": "division by zero",
    "status": "FAILED",
}
STAMP = {
    "region": "ap-northeast-1",
    "version": "0.0.0",
    "time": "2026-09-20T00:00:00Z",
}


def test_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_category_has_a_case():
    assert {case.category for case in CASES} == {
        "truth",
        "numbers",
        "join",
        "unpack",
        "lists",
        "dicts",
        "quantifiers",
        "missing",
        "encoding",
        "volatile",
        "catch",
    }


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_local_run_gives_what_the_case_expects(case):
    verdict = corpus.judge_locally(case, corpus.run_locally(case))
    assert verdict.passed, verdict.detail


PYTHON = [case for case in CASES if case.python]


@pytest.mark.parametrize("case", PYTHON, ids=[case.id for case in PYTHON])
def test_cpython_agrees_where_the_case_says_so(case):
    verdict = corpus.judge(case.expected, corpus.in_python(case))
    assert verdict.passed, verdict.detail


def test_the_conditions_of_the_cases_exist():
    for case in CASES:
        if case.on_aws is not None:
            assert case.on_aws.name in corpus.CONDITIONS


def test_a_doubled_evaluation_is_rejected():
    """The check itself: an expression that reads $random twice where the
    source reads it once gives 0.75 from the sequence 0.75, 0.25 and two
    calls, and the case that expects 0.25 from one call rejects it."""
    doubled = {
        "StartAt": "s",
        "States": {
            "s": {
                "Type": "Succeed",
                "Output": "{% $random() - 0.5 * $floor($random() / 0.5) %}",
            }
        },
    }
    case = next(c for c in CASES if c.id == "volatile-modulo-once")
    sequence = corpus.Sequence(case.random)
    with asl.replaced(random=sequence):
        outcome = Result(asl.run(doubled, {}))
    run = corpus.LocalRun(outcome, sequence.calls)
    assert run == corpus.LocalRun(Result(0.75), 2)
    assert not corpus.judge_locally(case, run).passed


def test_a_right_value_from_the_wrong_number_of_calls_is_rejected():
    case = next(c for c in CASES if c.id == "volatile-modulo-once")
    verdict = corpus.judge_locally(case, corpus.LocalRun(Result(0.25), 2))
    assert verdict == corpus.Verdict(False, "$random was called 2 times, not 1")


def test_a_call_past_the_sequence_fails():
    sequence = corpus.Sequence([0.5])
    assert sequence() == 0.5
    with pytest.raises(corpus.Exhausted):
        sequence()
    assert sequence.calls == 1


def test_the_replacement_reaches_no_other_run():
    definition = {
        "StartAt": "s",
        "States": {"s": {"Type": "Succeed", "Output": "{% $random() %}"}},
    }
    with asl.replaced(random=corpus.Sequence([7])):
        assert asl.run(definition, {}) == 7
    assert 0 <= asl.run(definition, {}) < 1


@pytest.mark.parametrize(
    "name, value, expected",
    [
        ("a number in [0, 0.5)", 0, True),
        ("a number in [0, 0.5)", 0.49, True),
        ("a number in [0, 0.5)", 0.5, False),
        ("a number in [0, 0.5)", -0.1, False),
        ("a number in [0, 0.5)", True, False),
        ("a number in [0, 0.5)", "0.2", False),
        ("a number in [0, 0.5)", None, False),
        ("false", False, True),
        ("false", 0, False),
        ("false", None, False),
        ("false", "false", False),
        ("two numbers in [0, 1)", [0, 0.999], True),
        ("two numbers in [0, 1)", [0, 1], False),
        ("two numbers in [0, 1)", [0.5], False),
        ("two numbers in [0, 1)", [False, 0.5], False),
        ("two numbers in [0, 1)", "ab", False),
    ],
)
def test_conditions(name, value, expected):
    assert corpus.CONDITIONS[name](value) is expected


@pytest.mark.parametrize(
    "expectation, outcome, passed, detail",
    [
        (Value(1), Result(1), True, ""),
        (Value(1), Result(True), False, "gave true, not 1"),
        (Value(1), Failed("E", "c"), False, "failed with E: c"),
        (Error("E"), Failed("E"), True, ""),
        (Error("E"), Failed("F"), False, "failed with F, not E"),
        (Error("E"), Result(None), False, "gave null instead of failing"),
        (Condition("false"), Result(False), True, ""),
        (Condition("false"), Result(0), False, "0 is not false"),
        (Condition("false"), Failed("E"), False, "failed with E"),
    ],
)
def test_judge(expectation, outcome, passed, detail):
    assert corpus.judge(expectation, outcome) == corpus.Verdict(passed, detail)


def test_select():
    cases = corpus.select(CASES, ["truth-zero"], ["volatile"])
    assert [c.id for c in cases] == [
        "truth-zero",
        "volatile-modulo-once",
        "volatile-short-circuit",
        "volatile-separate-calls",
    ]
    assert corpus.select(CASES, [], []) == list(CASES)
    with pytest.raises(ValueError, match="no such case or category: nope, truth-one"):
        corpus.select(CASES, ["truth-one"], ["nope"])


def test_route_of():
    single = corpus.compiled(next(c for c in CASES if c.id == "truth-zero"))
    several = corpus.compiled(next(c for c in CASES if c.id == "comprehension-of-one"))
    assert corpus.route_of(single) == "test-state"
    assert corpus.route_of(several) == "execution"


def test_the_outcome_of_a_response():
    assert corpus.outcome_of(RESULT_OF) == Result([0.1, 0.2])
    assert corpus.outcome_of(FAILURE_OF) == Failed(
        "States.QueryEvaluationError", "division by zero"
    )
    assert corpus.outcome_of({"status": "TIMED_OUT"}) == Failed("TIMED_OUT", "")


def test_the_record_tells_the_statuses_apart():
    case = next(c for c in CASES if c.id == "volatile-modulo-once")
    definition = corpus.compiled(case)
    outcome = corpus.outcome_of({"output": "0.7", "status": "SUCCEEDED"})
    verdict = corpus.judge(case.remote, outcome)
    mismatch = corpus.record(
        case,
        definition,
        route="test-state",
        status=corpus.status_of(verdict),
        outcome=outcome,
        detail=verdict.detail,
        **STAMP,
    )
    assert mismatch["status"] == "mismatch"
    assert mismatch["detail"] == "0.7 is not a number in [0, 0.5)"
    assert mismatch["actual"] == {"value": 0.7}
    assert mismatch["expected"] == {
        "kind": "condition",
        "condition": "a number in [0, 0.5)",
    }
    assert mismatch["cpython"] == "not compared"
    assert mismatch["calls"] == "unmeasured"
    assert mismatch["definition"] == definition
    passed = corpus.record(
        case,
        definition,
        route="test-state",
        status="passed",
        outcome=corpus.outcome_of({"output": "0.2", "status": "SUCCEEDED"}),
        **STAMP,
    )
    assert passed["status"] == "passed" and passed["actual"] == {"value": 0.2}
    not_run = corpus.record(
        case,
        definition,
        route="execution",
        status="not-run",
        detail="no --role-arn",
        **STAMP,
    )
    assert not_run["status"] == "not-run" and not_run["actual"] is None
    api_error = corpus.record(
        case,
        definition,
        route="test-state",
        status="api-error",
        detail="AccessDeniedException",
        **STAMP,
    )
    assert api_error["status"] == "api-error" and api_error["actual"] is None
