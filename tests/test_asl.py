import pytest

from tests import asl


def machine(state: dict) -> dict:
    return {"StartAt": "s", "States": {"s": state}}


def test_null_is_a_value_and_undefined_fails():
    succeed = machine({"Type": "Succeed", "Output": "{% $states.input.v %}"})
    assert asl.run(succeed, {"v": None}) is None
    assert asl.run(machine({"Type": "Succeed", "Output": "{% [null] %}"}), {}) == [None]
    with pytest.raises(asl.Failure) as failure:
        asl.run(succeed, {})
    assert failure.value.error == "States.QueryEvaluationError"


def test_assign_and_output_read_the_variables_from_before_the_state():
    task = {"Type": "Task", "Assign": {"x": 1}, "Output": "{% $x %}", "End": True}
    with pytest.raises(asl.Failure, match="undefined"):
        asl.run(machine(task), {}, {"s": lambda arguments: 0})


@pytest.mark.parametrize(
    "errors, error, expected",
    [
        (["States.ALL"], "Oops", True),
        (["States.TaskFailed"], "States.Timeout", False),
        (["States.ALL"], "States.Timeout", True),
        (["States.ALL"], "States.DataLimitExceeded", True),
        (["States.TaskFailed"], "States.DataLimitExceeded", True),
        (["States.DataLimitExceeded"], "States.DataLimitExceeded", True),
        (["States.ALL"], "States.Runtime", False),
        (["States.Runtime"], "States.Runtime", False),
    ],
)
def test_matches(errors, error, expected):
    assert asl.matches(errors, error) is expected
