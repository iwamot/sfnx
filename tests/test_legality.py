import logging

import pytest

from sfnx.expressions import expression
from sfnx.legality import (
    BLANKET,
    PROOF,
    Differs,
    Field,
    Reject,
    captures,
    context_invariant,
    dependencies_known,
    evaluated_as_before,
    evaluated_once,
    failure_escapes,
    failure_seen_before,
    fields_of,
    held_elsewhere,
    read_at_most_once,
    refused,
    resolve_reads,
)

INPUT = "$states.context.Execution.Input"
TASK = {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke"}
MAY_FAIL = expression(f"{INPUT}.a")
SAFE = expression("$exists($a)", defined=True, total=True)


@pytest.mark.parametrize(
    "handlers, value, reason",
    [
        # Nothing takes a failure of an expression: it ends the execution.
        ({}, MAY_FAIL, None),
        ({"Catch": [{"ErrorEquals": ["Declined"], "Next": "x"}]}, MAY_FAIL, None),
        ({"Retry": [{"ErrorEquals": ["States.TaskFailed"]}]}, MAY_FAIL, None),
        # A catcher or a retrier for States.ALL or States.QueryEvaluationError
        # would take it, unless nothing can fail.
        (
            {"Catch": [{"ErrorEquals": ["States.ALL"], "Next": "x"}]},
            MAY_FAIL,
            Reject.HANDLER_TAKES_FAILURE,
        ),
        (
            {"Retry": [{"ErrorEquals": ["States.QueryEvaluationError"]}]},
            MAY_FAIL,
            Reject.HANDLER_TAKES_FAILURE,
        ),
        ({"Catch": [{"ErrorEquals": ["States.ALL"], "Next": "x"}]}, SAFE, None),
        ({"Catch": [{"ErrorEquals": ["States.ALL"], "Next": "x"}]}, [1, "a"], None),
    ],
)
def test_where_a_failure_ends_the_execution_as_it_did(handlers, value, reason):
    assert failure_escapes({**TASK, **handlers}, value) is reason


@pytest.mark.parametrize(
    "code, differs, reason",
    [
        # The execution's input reads alike in every state.
        (f"{INPUT}.a", Differs.STATES, None),
        ("$states.input.a", Differs.STATES, Reject.STATE_CONTEXT_CHANGES),
        ("$states.input.a", Differs.CONTEXT, None),
        (
            "$states.context.State.EnteredTime",
            Differs.CONTEXT,
            Reject.STATE_CONTEXT_CHANGES,
        ),
        ("$states.context.State.EnteredTime", Differs.STATE, Reject.STATE_NAME_READ),
        ("$states.context.Task.Token", Differs.STATE, None),
    ],
)
def test_what_reads_the_same_of_states_where_it_goes(code, differs, reason):
    assert context_invariant([code], differs) is reason


def test_the_values_an_assign_gives_what_code_reads():
    assign = {"a": expression(f"{INPUT}.a"), "b": 1, "c": expression("$x")}
    values = resolve_reads(assign, ["$a + $b"], "here")
    assert values is not None
    assert {n: v.code for n, v in values.items()} == {"a": f"{INPUT}.a", "b": "1"}
    everything = resolve_reads(assign, None, "here")
    assert everything is not None and everything.keys() == {"a", "b", "c"}


def test_an_assign_that_is_no_one_expression_is_refused(caplog):
    caplog.set_level(logging.DEBUG, "sfnx.legality")
    assign = {"a": {"k": expression("$x")}}
    assert resolve_reads(assign, ["$a"], "here") is None
    assert "here: UNRESOLVED_READ (proof)" in caplog.text


@pytest.mark.parametrize(
    "holder, reason",
    [
        ({"Output": "{% $a + 1 %}"}, None),
        # A function whose parameter the value reads, a binding of the name,
        # and a string that spells it.
        ({"Output": "{% function($x) { $a + $x } %}"}, Reject.NAME_CAPTURE),
        ({"Output": "{% ($a := 2; $a) %}"}, Reject.NAME_CAPTURE),
        ({"Output": "{% '$a' & $a %}"}, Reject.NAME_CAPTURE),
    ],
)
def test_where_a_value_would_be_read_otherwise(holder, reason):
    assert captures(holder, {"a": expression("$x + 1", frozenset({"x"}))}) is reason


@pytest.mark.parametrize("read", ["$a", "$xs[$a]"])
def test_a_value_that_may_read_the_context_is_not_written_in(read):
    """Read where the context is the same or in a filter alike: the value is
    kept out wherever the variable is read."""
    value = expression("$exists($string()) ? 1 : 0")
    holder = {"Output": f"{{% {read} %}}"}
    assert captures(holder, {"a": value}) is Reject.READS_CONTEXT


def test_a_refusal_is_noted_with_its_kind(caplog):
    caplog.set_level(logging.DEBUG, "sfnx.legality")
    assert not refused(None, "here")
    assert refused(Reject.STATE_NAME_READ, "here")
    assert "here: STATE_NAME_READ (blanket)" in caplog.text


def test_each_reason_is_a_proof_or_a_blanket():
    assert {reason.kind for reason in Reject} == {PROOF, BLANKET}


@pytest.mark.parametrize(
    "codes, reason",
    [
        (["$a + 1", "$random()", "$now()"], None),
        (["$a", "$eval('1')"], Reject.DEPENDENCIES_UNKNOWN),
    ],
)
def test_what_reads_what_is_written_out(codes, reason):
    assert dependencies_known(codes) is reason


RANDOM = expression("$random()", defined=True, total=True)
NOW = expression("$now()", defined=True, total=True)


@pytest.mark.parametrize(
    "value, codes, kept, reason",
    [
        # What does not change on evaluation goes anywhere.
        (MAY_FAIL, ["[$x, $x]"], {"x"}, None),
        # A random value read once where its assignment goes.
        (RANDOM, ["$x + 1"], set(), None),
        # Nothing reads it.
        (RANDOM, ["$y"], {"x"}, None),
        # The assignment that stays evaluates it once more.
        (RANDOM, ["$x + 1"], {"x"}, Reject.CHANGES_EVALUATION_COUNT),
        (RANDOM, ["$x", "$x"], set(), Reject.CHANGES_EVALUATION_COUNT),
        (
            RANDOM,
            ["$map($r, function($i) { $x })"],
            set(),
            Reject.CHANGES_EVALUATION_COUNT,
        ),
        # The time is not moved, however often it is read: whether its reads
        # stay between the same calls and waits is not shown.
        (NOW, ["$x"], set(), Reject.CHANGES_EVALUATION_INSTANCE),
        (expression("$eval('1')"), ["$x"], set(), Reject.DEPENDENCIES_UNKNOWN),
        # An Expr that is not settled is judged by its code, which shows the
        # functions it calls under any name.
        (expression("$a + 1", opaque=True), ["[$x, $x]"], {"x"}, None),
        (
            expression("($r := $random; $r())", opaque=True),
            ["$x"],
            {"x"},
            Reject.CHANGES_EVALUATION_COUNT,
        ),
    ],
)
def test_what_a_value_read_where_it_is_assigned_may_be_read_in(
    value, codes, kept, reason
):
    assert evaluated_once({"x": value}, codes, kept) is reason


@pytest.mark.parametrize(
    "value, fields, reason",
    [
        # What does not change on evaluation goes anywhere.
        (MAY_FAIL, [Field("[$x, $x]", False)], None),
        # A random value read at most once.
        (RANDOM, [Field("$x + 1", True)], None),
        (RANDOM, [Field("$y", True)], None),
        (
            RANDOM,
            [Field("$x", True), Field("$x", False)],
            Reject.CHANGES_EVALUATION_COUNT,
        ),
        (RANDOM, [Field("[$x, $x]", True)], Reject.CHANGES_EVALUATION_COUNT),
        (
            RANDOM,
            [Field("$map($r, function($i) { $x })", True)],
            Reject.CHANGES_EVALUATION_COUNT,
        ),
        (RANDOM, [Field("$x", False)], None),
        (RANDOM, [Field("$x", True, repeated=True)], Reject.CHANGES_EVALUATION_COUNT),
        # The time read in one field, before the call or the wait, however
        # often that one evaluation reads it.
        (NOW, [Field("[$x, $x]", True)], None),
        (
            NOW,
            [Field("$x", True), Field("$x", True)],
            Reject.CHANGES_EVALUATION_INSTANCE,
        ),
        (NOW, [Field("$x", False)], Reject.CROSSES_EFFECT),
        (expression("$eval('1')"), [Field("$x", True)], Reject.DEPENDENCIES_UNKNOWN),
    ],
)
def test_what_a_value_that_changes_may_be_read_in(value, fields, reason):
    assert evaluated_as_before(value, "x", fields) is reason


def where(state: dict) -> list[Field]:
    return [Field(f.code.strip(), f.before, f.repeated) for f in fields_of(state)]


def test_where_a_state_evaluates_its_fields():
    task = {
        **TASK,
        "Arguments": {"Payload": "{% $a %}"},
        "Assign": {"b": "{% $b %}"},
        "Catch": [{"ErrorEquals": ["States.ALL"], "Assign": {"c": "{% $c %}"}}],
        "Next": "x",
    }
    assert where(task) == [
        Field("$a", True),
        Field("$b", False),
        Field("$c", False),
    ]
    retried = {**task, "Retry": [{"ErrorEquals": ["Declined"]}]}
    assert all(f.repeated for f in fields_of(retried))
    choice = {"Type": "Choice", "Choices": [{"Condition": "{% $a %}", "Next": "x"}]}
    assert where(choice) == [Field("$a", True)]
    each = {"Type": "Map", "Items": "{% $a %}", "ItemSelector": {"v": "{% $b %}"}}
    assert where(each) == [Field("$a", True), Field("$b", False, repeated=True)]


DEFINED = expression("$a * 2", frozenset({"a"}), defined=True)


@pytest.mark.parametrize(
    "state, value, seen",
    [
        # The Arguments evaluate the same code before the call.
        ({**TASK, "Arguments": {"Payload": "{% $a * 2 %}"}}, DEFINED, True),
        ({**TASK, "Arguments": "{% ($a*2) %}"}, DEFINED, True),
        # A value that may be undefined, and one read in part only.
        ({**TASK, "Arguments": {"Payload": "{% $a * 2 %}"}}, MAY_FAIL, False),
        ({**TASK, "Arguments": {"Payload": "{% $a * 2 + 1 %}"}}, DEFINED, False),
        # A retrier evaluates the Arguments again; a Choice calls nothing.
        (
            {**TASK, "Arguments": "{% $a * 2 %}", "Retry": [{"ErrorEquals": ["X"]}]},
            DEFINED,
            False,
        ),
        (
            {"Type": "Choice", "Choices": [{"Condition": "{% $a * 2 %}"}]},
            DEFINED,
            False,
        ),
    ],
)
def test_a_failure_the_state_meets_before_its_assign(state, value, seen):
    assert failure_seen_before(value, state, state) is seen
    assert not failure_seen_before(value, {"Next": "x"}, state)


@pytest.mark.parametrize(
    "codes, reason",
    [
        (["$x + 1"], None),
        (["$y", "[$x, 1]"], None),
        (["$x", "$x"], Reject.CHANGES_EVALUATION_COUNT),
        (["$map($r, function($i) { $x })"], Reject.CHANGES_EVALUATION_COUNT),
    ],
)
def test_what_reads_a_variable_at_most_once(codes, reason):
    assert read_at_most_once(codes, "x") is reason


@pytest.mark.parametrize(
    "value, codes, held",
    [
        # Read in place of its variable elsewhere, where it may not fail.
        (MAY_FAIL, [f"{INPUT}.a", f"[{INPUT}.a]"], True),
        (MAY_FAIL, [f"{INPUT}.a"], False),
        # The text holds it, the syntax tree does not.
        (MAY_FAIL, [f"{INPUT}.a", f"{INPUT}.ab"], False),
        # The syntax tree holds it, the text does not: as the start of a path
        # read further, or in parentheses.
        (
            MAY_FAIL,
            [f"{INPUT}.a", f"$map({INPUT}.a.items, function($v) {{ $v }})"],
            True,
        ),
        (
            expression(f"({INPUT}.a)"),
            [f"({INPUT}.a)", f"$map({INPUT}.a, function($v) {{ $v }})"],
            True,
        ),
        # Nothing in it can fail or be undefined.
        (SAFE, ["$exists($a)", "[$exists($a)]"], False),
        (expression("$a"), ["$a", "[$a]"], False),
        (expression("$states.result"), ["$states.result", "[$states.result]"], False),
    ],
)
def test_a_value_whose_failure_another_expression_takes(value, codes, held):
    assert held_elsewhere(value, codes) is held
