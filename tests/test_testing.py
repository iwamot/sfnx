import re
from pathlib import Path

import pytest

from sfnx import testing
from tests import asl

LAMBDA = "arn:aws:states:::lambda:invoke"


def machine(state: dict) -> dict:
    return {"QueryLanguage": "JSONata", "StartAt": "s", "States": {"s": state}}


def test_null_is_a_value_and_undefined_fails():
    succeed = machine({"Type": "Succeed", "Output": "{% $states.input.v %}"})
    assert asl.run(succeed, {"v": None}) is None
    assert asl.run(machine({"Type": "Succeed", "Output": "{% [null] %}"}), {}) == [None]
    with pytest.raises(asl.Failure) as failure:
        asl.run(succeed, {})
    assert failure.value.error == "States.QueryEvaluationError"


@pytest.mark.parametrize(
    "code, expected",
    [
        ("$decodeUrlComponent('a+b%2Bc%20d')", "a b+c d"),
        ("$decodeUrlComponent('%E6%97%A5')", "日"),
        ("$decodeUrlComponent('%E6')", "�"),
        ("$base64decode('YWJ')", "ab"),
        ("$base64decode('5pel5pysIGE=')", "日本 a"),
        # $formatNumber rounds half to even on the decimal the number is
        # written as, where jsonata-python rounds the double it holds.
        ("$formatNumber(0.125, '0.00')", "0.12"),
        ("$formatNumber(2.675, '0.00')", "2.68"),
        ("$formatNumber(2.5, '0')", "2"),
        ("$formatNumber(3.5, '0')", "4"),
        ("$formatNumber(1234.5678, '#,##0.00')", "1,234.57"),
        ("$formatNumber(1234.5, '#,##0')", "1,234"),
        # The sign is written inside the zeros of the width.
        ("$formatNumber(-12, '00000;-0000')", "-0012"),
        ("$formatNumber(7, '00000;-0000')", "00007"),
        ("$formatNumber(-123456, '00000;-0000')", "-123456"),
        ("$formatNumber(-1.5, '00000;-0000')", "-0002"),
        # A picture the compiler does not write is left to jsonata-python.
        ("$formatNumber(-12, '00000')", "-00012"),
    ],
)
def test_the_functions_read_as_step_functions_reads_them(code, expected):
    assert (
        asl.run(machine({"Type": "Succeed", "Output": "{% " + code + " %}"}), {})
        == expected
    )


@pytest.mark.parametrize("code", ["$decodeUrlComponent('%zz')", "$base64decode('!!')"])
def test_a_malformed_text_fails(code):
    with pytest.raises(asl.Failure) as failure:
        asl.run(machine({"Type": "Succeed", "Output": "{% " + code + " %}"}), {})
    assert failure.value.error == "States.QueryEvaluationError"


def test_assign_and_output_read_the_variables_from_before_the_state():
    task = {
        "Type": "Task",
        "Resource": LAMBDA,
        "Assign": {"x": 1},
        "Output": "{% $x %}",
        "End": True,
    }
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
    assert testing.matches(errors, error) is expected


def flaky(*outcomes: object):
    """A task that answers with each outcome in turn, raising the failures, and
    records the arguments it was given."""
    calls: list[object] = []

    def task(arguments):
        calls.append(arguments)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, asl.Failure):
            raise outcome
        return outcome

    return task, calls


def retrying(retry: list, **fields) -> dict:
    return machine(
        {"Type": "Task", "Resource": LAMBDA, "Retry": retry, "End": True, **fields}
    )


def test_retry_calls_the_task_again_up_to_max_attempts():
    boom = asl.Failure("Boom")
    task, calls = flaky(boom, boom, boom, "ok")
    assert asl.run(retrying([{"ErrorEquals": ["Boom"]}]), {}, {"s": task}) == "ok"
    assert len(calls) == 4
    task, calls = flaky(boom, boom, "ok")
    with pytest.raises(asl.Failure, match="Boom"):
        asl.run(
            retrying([{"ErrorEquals": ["Boom"], "MaxAttempts": 1}]), {}, {"s": task}
        )
    assert len(calls) == 2
    task, calls = flaky(asl.Failure("Other"), "ok")
    with pytest.raises(asl.Failure, match="Other"):
        asl.run(retrying([{"ErrorEquals": ["Boom"]}]), {}, {"s": task})


def test_only_the_first_matching_retrier_counts_a_failure():
    retry = [
        {"ErrorEquals": ["A", "B"], "MaxAttempts": 2},
        {"ErrorEquals": ["C"]},
        {"ErrorEquals": ["States.ALL"], "MaxAttempts": 5},
    ]
    a, b, c = asl.Failure("A"), asl.Failure("B"), asl.Failure("C")
    task, calls = flaky(a, b, c, b, "ok")
    with pytest.raises(asl.Failure, match="B"):
        asl.run(retrying(retry), {}, {"s": task})
    assert len(calls) == 4
    task, calls = flaky(a, "ok")
    with pytest.raises(asl.Failure, match="A"):
        asl.run(
            retrying([{"ErrorEquals": ["A"], "MaxAttempts": 0}, *retry]),
            {},
            {"s": task},
        )


def test_a_failure_in_arguments_or_output_is_retried():
    retry = [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 1}]
    task, calls = flaky({"n": "x"}, {"n": 1})
    output = retrying(retry, Output="{% $states.result.n + 1 %}")
    assert asl.run(output, {}, {"s": task}) == 2
    assert len(calls) == 2
    # The first attempt reads a field the input does not have.
    arguments = retrying(
        retry,
        Arguments="{% $states.context.State.RetryCount = 0 ? $states.input.none : 1 %}",
    )
    task, calls = flaky("ok")
    assert asl.run(arguments, {}, {"s": task}) == "ok"
    assert calls == [1]


def test_a_catch_reads_the_retries_made():
    retry = [{"ErrorEquals": ["States.ALL"], "MaxAttempts": 1}]
    caught = machine(
        {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": "{% $states.context.State.RetryCount %}",
            "Retry": retry,
            "Catch": [
                {
                    "ErrorEquals": ["States.ALL"],
                    "Assign": {"retries": "{% $states.context.State.RetryCount %}"},
                    "Next": "caught",
                }
            ],
            "Next": "caught",
        }
    )
    caught["States"]["caught"] = {
        "Type": "Succeed",
        "Output": "{% [$states.input, $retries] %}",
    }
    boom = asl.Failure("Boom", "c")
    task, calls = flaky(boom, boom)
    assert asl.run(caught, {}, {"s": task}) == [{"Error": "Boom", "Cause": "c"}, 1]
    assert calls == [0, 1]


def test_retry_runs_a_parallel_again():
    branch = {
        "StartAt": "t",
        "States": {"t": {"Type": "Task", "Resource": LAMBDA, "End": True}},
    }
    parallel = machine(
        {
            "Type": "Parallel",
            "Branches": [branch],
            "Retry": [{"ErrorEquals": ["Boom"]}],
            "End": True,
        }
    )
    task, calls = flaky(asl.Failure("Boom"), "ok")
    assert asl.run(parallel, {}, {"t": task}) == ["ok"]
    assert len(calls) == 2


def test_the_context_object():
    read = asl.run(
        machine({"Type": "Succeed", "Output": "{% $states.context %}"}), {"a": 1}
    )
    assert isinstance(read, dict)
    assert {k: sorted(v) for k, v in read.items()} == {
        "Execution": ["Id", "Input", "Name", "RedriveCount", "RoleArn", "StartTime"],
        "State": ["EnteredTime", "Name"],
        "StateMachine": ["Id", "Name"],
    }
    assert read["Execution"]["Input"] == {"a": 1}
    assert read["State"]["Name"] == "s"
    for path in ["Execution.RedriveTime", "Task.Token"]:
        succeed = {"Type": "Succeed", "Output": f"{{% $states.context.{path} %}}"}
        with pytest.raises(asl.Failure, match="undefined"):
            asl.run(machine(succeed), {})
    callback = {
        "Type": "Task",
        "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
        "Arguments": "{% $states.context.Task.Token %}",
        "End": True,
    }
    assert asl.run(machine(callback), {}, {"s": lambda token: token}) == "token"


@pytest.mark.parametrize(
    "failed, total, count, percentage, expected",
    [
        (1, 3, 0, 0, True),
        (1, 3, 1, 100, False),
        (3, 3, 1, 100, True),
        (3, 3, 3, 100, False),
        (1, 3, 0, 100, False),
        (1, 3, 100_000, 34, False),
        (1, 3, 100_000, 33, True),
        (1, 4, 100_000, 25, False),
        (1, 4, 100_000, 24, True),
        (1, 3, 0, 50, False),
        (3, 3, 0, 50, True),
        (1, 3, 5, 0, False),
        (3, 3, 5, 0, False),
        (2, 3, 1, 90, True),
        (2, 3, 5, 50, True),
        (2, 5, 1, 100, True),
        (2, 5, 2, 100, False),
        (2, 5, 100_000, 34, True),
        (2, 5, 100_000, 39, True),
        (2, 5, 100_000, 40, False),
    ],
)
def test_exceeds(failed, total, count, percentage, expected):
    """Each case was a distributed Map run in Step Functions."""
    assert testing.exceeds(failed, total, count, percentage) is expected


def distributed(child: dict | None = None, **fields) -> dict:
    """A distributed Map over the input. Its child by default fails with Boom on
    an item marked fail and returns n otherwise."""
    if child is None:
        child = failing("{% $states.input.fail %}", "{% $states.input.n %}")
    return machine(
        {
            "Type": "Map",
            "Items": "{% $states.input %}",
            "ItemProcessor": {
                "ProcessorConfig": {"Mode": "DISTRIBUTED", "ExecutionType": "STANDARD"},
                **child,
            },
            "End": True,
            **fields,
        }
    )


def failing(condition: str, output: str) -> dict:
    """A processor that fails with Boom when condition holds and otherwise
    returns output."""
    return {
        "StartAt": "check",
        "States": {
            "check": {
                "Type": "Choice",
                "Choices": [{"Condition": condition, "Next": "fail"}],
                "Default": "ok",
            },
            "fail": {"Type": "Fail", "Error": "Boom", "Cause": "c"},
            "ok": {"Type": "Succeed", "Output": output},
        },
    }


def test_a_distributed_map_tolerates_failed_children():
    items = [{"n": 1, "fail": False}, {"n": 2, "fail": True}]
    with pytest.raises(asl.Failure) as failure:
        asl.run(distributed(), items)
    assert failure.value.error == "States.ExceedToleratedFailureThreshold"
    assert asl.run(distributed(ToleratedFailureCount="{% 1 %}"), items) == [
        1,
        {"Status": "FAILED", "Error": "Boom", "Cause": "c"},
    ]


def test_a_failed_batch_counts_its_items():
    items = [{"n": n, "fail": n == 2} for n in range(1, 6)]
    child = failing(
        "{% true in $states.input.Items.fail %}", "{% $states.input.Items.n %}"
    )
    batcher = {"MaxItemsPerBatch": 2}
    with pytest.raises(asl.Failure, match="ExceedToleratedFailureThreshold"):
        asl.run(distributed(child, ItemBatcher=batcher, ToleratedFailureCount=1), items)
    assert asl.run(
        distributed(child, ItemBatcher=batcher, ToleratedFailurePercentage=40), items
    ) == [{"Status": "FAILED", "Error": "Boom", "Cause": "c"}, [3, 4], 5]


def test_an_object_of_items():
    child = {"StartAt": "c", "States": {"c": {"Type": "Succeed"}}}
    selector = {
        "value": "{% $states.context.Map.Item.Value %}",
        "key": "{% $states.context.Map.Item.Key %}",
        "index": "{% $states.context.Map.Item.Index %}",
    }
    items = {"a": 1, "b": {"x": 2}}
    assert asl.run(distributed(child, ItemSelector=selector), items) == [
        {"value": 1, "key": "a", "index": 0},
        {"value": {"x": 2}, "key": "b", "index": 1},
    ]
    assert asl.run(distributed(child), items) == [
        {"Key": "a", "Value": 1},
        {"Key": "b", "Value": {"x": 2}},
    ]


def test_an_item_reader_and_a_result_writer():
    reader = {
        "Resource": "arn:aws:states:::s3:getObject",
        "ReaderConfig": {"InputType": "JSON"},
        "Arguments": {"Bucket": "b", "Key": "{% 'items.json' %}"},
    }
    child = {"StartAt": "c", "States": {"c": {"Type": "Succeed"}}}
    read = []

    def items(evaluated):
        read.append(evaluated)
        return [1, 2]

    assert asl.run(distributed(child, ItemReader=reader), {}, {"s": items}) == [1, 2]
    assert read == [{"Bucket": "b", "Key": "items.json"}]
    writer = {
        "Resource": "arn:aws:states:::s3:putObject",
        "Arguments": {"Bucket": "b", "Prefix": "p"},
    }
    written = asl.run(distributed(child, ResultWriter=writer, Label="l"), [1])
    assert written == {
        "MapRunArn": f"{asl.MAP_RUN}/l:run",
        "ResultWriterDetails": {"Bucket": "b", "Key": "p/run/manifest.json"},
    }


ROOT = Path(__file__).parent.parent


def test_the_guide_runs(monkeypatch):
    """The test docs/testing.md shows passes as written."""
    monkeypatch.chdir(ROOT)
    guide = (ROOT / "docs" / "testing.md").read_text()
    (code,) = re.findall(
        r"^```python\n(from sfnx.*?)^```", guide, re.DOTALL | re.MULTILINE
    )
    namespace: dict[str, object] = {}
    exec(code, namespace)
    tests = [f for name, f in namespace.items() if name.startswith("test_")]
    assert len(tests) == 2
    for test in tests:
        assert callable(test)
        test()


def test_an_execution_records_the_states_and_the_calls():
    definition = {
        "QueryLanguage": "JSONata",
        "StartAt": "get",
        "States": {
            "get": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:s3:getObject",
                "Arguments": {"Key": "{% $states.input.key %}"},
                "Retry": [{"ErrorEquals": ["Busy"]}],
                "Next": "done",
            },
            "done": {"Type": "Succeed"},
        },
    }
    answers = iter([testing.Failure("Busy"), {"Body": "b"}])

    def tasks(call: testing.Call) -> object:
        answer = next(answers)
        if isinstance(answer, testing.Failure):
            raise answer
        return answer

    execution = testing.run(definition, {"key": "k"}, tasks)
    call = testing.Call("get", "arn:aws:states:::aws-sdk:s3:getObject", {"Key": "k"})
    assert execution.output == {"Body": "b"}
    assert (execution.error, execution.cause) == (None, None)
    assert execution.states == ("get", "done")
    assert execution.calls == (call, call)
    assert repr(execution) == (
        f"Execution(states=('get', 'done'), calls=({call!r}, {call!r}),"
        " output={'Body': 'b'})"
    )


def test_a_failed_execution_raises_its_failure_when_the_output_is_read():
    fail = machine({"Type": "Fail", "Error": "Boom", "Cause": "why"})
    execution = testing.run(fail, {})
    assert (execution.error, execution.cause) == ("Boom", "why")
    assert execution.states == ("s",)
    with pytest.raises(testing.Failure) as failure:
        _ = execution.output
    assert (failure.value.error, failure.value.cause) == ("Boom", "why")
    assert repr(execution) == (
        "Execution(states=('s',), calls=(), error='Boom', cause='why')"
    )


def test_an_exception_from_the_tasks_function_reaches_the_test():
    task = machine({"Type": "Task", "Resource": LAMBDA, "End": True})

    def tasks(call: testing.Call) -> object:
        raise KeyError(call.state)

    with pytest.raises(KeyError):
        testing.run(task, {}, tasks)
    with pytest.raises(ValueError, match="s calls .*lambda:invoke, and run"):
        testing.run(task, {})


def test_functions_are_replaced_for_the_run_only():
    now = machine({"Type": "Succeed", "Output": "{% $now() %}"})
    fixed = testing.run(now, {}, functions={"now": lambda: "then"})
    assert fixed.output == "then"
    assert testing.run(now, {}).output != "then"


def test_a_choice_without_a_match_or_a_default_fails():
    choice = machine(
        {"Type": "Choice", "Choices": [{"Condition": "{% false %}", "Next": "s"}]}
    )
    assert testing.run(choice, {}).error == "States.NoChoiceMatched"


@pytest.mark.parametrize(
    "definition, message",
    [
        (
            {"StartAt": "s", "States": {"s": {"Type": "Succeed"}}},
            "the definition does not set QueryLanguage to JSONata",
        ),
        (
            machine({"Type": "Succeed", "QueryLanguage": "JSONPath"}),
            "s: QueryLanguage JSONPath is not run",
        ),
        (machine({"Type": "Activity"}), "s: the state type Activity is not run"),
        (
            machine({"Type": "Pass", "Output": {}, "End": True}),
            "s: Output is not run",
        ),
        (
            machine({"Type": "Task", "Resource": LAMBDA, "Parameters": {}}),
            "s: Parameters is not run",
        ),
        (
            {**machine({"Type": "Succeed"}), "Extra": 1},
            "the definition: Extra is not run",
        ),
        (
            machine(
                {
                    "Type": "Task",
                    "Resource": LAMBDA,
                    "Catch": [{"ErrorEquals": ["A"], "Next": "s", "Output": {}}],
                    "End": True,
                }
            ),
            "s.Catch: Output is not run",
        ),
        (
            machine(
                {
                    "Type": "Choice",
                    "Choices": [{"Condition": "{% true %}", "Next": "s", "Assign": {}}],
                }
            ),
            "s.Choices: Assign is not run",
        ),
        (
            machine(
                {
                    "Type": "Parallel",
                    "Branches": [
                        {
                            "StartAt": "t",
                            "States": {"t": {"Type": "Pass", "End": True}},
                        }
                    ],
                    "End": True,
                }
            ),
            "t: End is not run",
        ),
        (
            machine(
                {
                    "Type": "Map",
                    "Items": [],
                    "ItemProcessor": {
                        "StartAt": "t",
                        "States": {"t": {"Type": "Succeed"}},
                        "Extra": 1,
                    },
                    "End": True,
                }
            ),
            "s.ItemProcessor: Extra is not run",
        ),
        (
            machine(
                {
                    "Type": "Map",
                    "Items": [],
                    "ItemBatcher": {"MaxItemsPerBatch": 1, "Extra": 1},
                    "ItemProcessor": {
                        "ProcessorConfig": {"Mode": "DISTRIBUTED"},
                        "StartAt": "t",
                        "States": {"t": {"Type": "Succeed"}},
                    },
                    "End": True,
                }
            ),
            "s.ItemBatcher: Extra is not run",
        ),
    ],
)
def test_what_the_runner_does_not_interpret_is_rejected_before_it_runs(
    definition, message
):
    with pytest.raises(testing.Unsupported, match=message):
        testing.run(definition, {})


def test_text_that_is_not_json_fails_to_parse():
    parsed = machine({"Type": "Succeed", "Output": "{% $parse('{') %}"})
    assert testing.run(parsed, {}).error == "States.QueryEvaluationError"


def test_a_condition_that_is_not_a_boolean_fails():
    choice = machine(
        {"Type": "Choice", "Choices": [{"Condition": "{% 1 %}", "Next": "s"}]}
    )
    execution = testing.run(choice, {})
    assert (execution.error, execution.cause) == (
        "States.QueryEvaluationError",
        "1 is not a boolean",
    )


def test_a_definition_that_never_ends_stops():
    with pytest.raises(AssertionError, match="did not end within 10,000 states"):
        testing.run(machine({"Type": "Pass", "Next": "s"}), {})


def test_an_item_reader_is_a_call_and_iterations_are_entered_states():
    reader = {
        "Resource": "arn:aws:states:::s3:getObject",
        "ReaderConfig": {"InputType": "JSON"},
        "Arguments": {"Bucket": "b", "Key": "k"},
    }
    child = {"StartAt": "c", "States": {"c": {"Type": "Succeed"}}}
    execution = testing.run(
        distributed(child, ItemReader=reader), {}, lambda call: [1, 2]
    )
    assert execution.output == [1, 2]
    assert execution.calls == (
        testing.Call("s", "arn:aws:states:::s3:getObject", {"Bucket": "b", "Key": "k"}),
    )
    assert execution.states == ("s", "c", "c")
