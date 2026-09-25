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


# Step Functions fails a field whose value holds a function (TestState).
@pytest.mark.parametrize(
    "state",
    [
        {"Type": "Pass", "Assign": {"f": "{% function($v) { $v } %}"}, "End": True},
        {"Type": "Pass", "Output": "{% function($v) { $v } %}", "End": True},
        {"Type": "Pass", "Output": {"a": "{% $uppercase %}"}, "End": True},
        {"Type": "Pass", "Output": "{% [function($v) { $v }] %}", "End": True},
        {"Type": "Pass", "Output": "{% {'a': $uppercase} %}", "End": True},
    ],
)
def test_a_function_is_not_a_value(state):
    with pytest.raises(asl.Failure, match="unsupported result type") as failure:
        asl.run(machine(state), {})
    assert failure.value.error == "States.QueryEvaluationError"


def test_values_of_every_json_type_pass():
    output = "{% {'s': 'a', 'n': 1.5, 'b': true, 'z': null, 'l': [1, {'k': []}]} %}"
    succeed = machine({"Type": "Succeed", "Output": output})
    assert asl.run(succeed, {}) == {
        "s": "a",
        "n": 1.5,
        "b": True,
        "z": None,
        "l": [1, {"k": []}],
    }


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


# $string as Step Functions writes JSON text (measured): a whole number in its
# digits below 1e21, any other number rounded down to 15 significant digits,
# and only the quote, the backslash and \b \f \n \r \t escaped.
STRING_CASES = [
    ("a", "a"),
    (1.0, "1"),
    (1.5e16, "15000000000000000"),
    (1.5e21, "1.5e+21"),
    (1.7976931348623157e308, "1.7976931348623157e+308"),
    (0.1, "0.1"),
    (0.6666666666666666, "0.666666666666666"),
    (-0.6666666666666666, "-0.666666666666667"),
    (1234567890123456.7, "1.23456789012345e+15"),
    (123456789012345.5, "123456789012345"),
    (100.0000000000001, "1e+2"),
    (0.0000015, "0.0000015"),
    (0.00000015, "1.5e-7"),
    (9007199254740993, "9007199254740993"),
    (True, "true"),
    (None, "null"),
    ([1.0, {"a": [0.1, None]}], '[1,{"a":[0.1,null]}]'),
    (
        ['\t\b\f\r\n"\\', "\x01\x7f\u2028/é😀"],
        '["\\t\\b\\f\\r\\n\\"\\\\","\x01\x7f\u2028/é😀"]',
    ),
    ({'k"\x01': 1}, '{"k\\"\x01":1}'),
]


@pytest.mark.parametrize("value, expected", STRING_CASES)
def test_string_writes_what_step_functions_writes(value, expected):
    output = machine({"Type": "Succeed", "Output": "{% $string($states.input.v) %}"})
    assert asl.run(output, {"v": value}) == expected


@pytest.mark.parametrize(
    "code, expected",
    [
        ("$string([0.1 + 0.2])", "[0.3]"),
        ("$string({'a': [], 'b': {}}, true)", '{\n  "a": [],\n  "b": {}\n}'),
        (
            "$string({'c': [1, {'d': 0.5}]}, true)",
            '{\n  "c": [\n    1,\n    {\n      "d": 0.5\n    }\n  ]\n}',
        ),
        ("$string('a', true)", "a"),
        ("$string(function($v) { $v })", ""),
        ("$string([$uppercase, 1])", '["",1]'),
        ("$exists($string($nothing))", False),
    ],
)
def test_string_reads_its_arguments_as_step_functions_does(code, expected):
    output = machine({"Type": "Succeed", "Output": "{% " + code + " %}"})
    assert asl.run(output, {}) == expected


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
        (["States.TaskFailed"], "States.QueryEvaluationError", False),
        (["States.ALL"], "States.QueryEvaluationError", True),
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


def test_a_batch_holds_what_the_item_selector_selects():
    # Step Functions selects each item, then batches what it selected
    # (TestState; measured).
    child = {"StartAt": "c", "States": {"c": {"Type": "Succeed"}}}
    selector = {
        "item": "{% $states.context.Map.Item.Value %}",
        "at": "{% $states.context.Map.Item.Index %}",
    }
    batcher = {"MaxItemsPerBatch": 2, "BatchInput": {"c": "x"}}
    assert asl.run(
        distributed(child, ItemSelector=selector, ItemBatcher=batcher), ["a", "b", "c"]
    ) == [
        {
            "Items": [{"item": "a", "at": 0}, {"item": "b", "at": 1}],
            "BatchInput": {"c": "x"},
        },
        {"Items": [{"item": "c", "at": 2}], "BatchInput": {"c": "x"}},
    ]


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
            "s: the state is in JSONPath, as the definition does not set QueryLanguage",
        ),
        (
            {
                "QueryLanguage": "JSONPath",
                "StartAt": "s",
                "States": {"s": {"Type": "Succeed"}},
            },
            "s: the state is in JSONPath",
        ),
        (
            {
                "StartAt": "m",
                "States": {
                    "m": {
                        "Type": "Map",
                        "QueryLanguage": "JSONata",
                        "Items": [1],
                        "ItemProcessor": {
                            "StartAt": "p",
                            "States": {"p": {"Type": "Pass", "End": True}},
                        },
                        "End": True,
                    }
                },
            },
            "p: the state is in JSONPath",
        ),
        (
            machine({"Type": "Succeed", "QueryLanguage": "JSONPath"}),
            "s: QueryLanguage JSONPath is not run",
        ),
        (machine({"Type": "Activity"}), "s: the state type Activity is not run"),
        (
            machine({"Type": "Pass", "Result": {}, "End": True}),
            "s: Result is not run",
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
                    "Catch": [{"ErrorEquals": ["A"], "Next": "s", "ResultPath": "$"}],
                    "End": True,
                }
            ),
            "s.Catch: ResultPath is not run",
        ),
        (
            machine(
                {
                    "Type": "Choice",
                    "Choices": [
                        {"Variable": "$.a", "BooleanEquals": True, "Next": "s"}
                    ],
                }
            ),
            "s.Choices: Variable is not run",
        ),
        (
            machine(
                {
                    "Type": "Parallel",
                    "Branches": [
                        {
                            "StartAt": "t",
                            "States": {
                                "t": {"Type": "Pass", "Parameters": {}, "End": True}
                            },
                        }
                    ],
                    "End": True,
                }
            ),
            "t: Parameters is not run",
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


# What ValidateStateMachineDefinition rejects and accepts (measured).
TASK = {"Type": "Task", "Resource": LAMBDA}
DONE = {"Type": "Succeed"}


def states(start: str, **named: dict) -> dict:
    return {"QueryLanguage": "JSONata", "StartAt": start, "States": named}


def assigning(name: str = "b") -> dict:
    """A branch that assigns x."""
    state = {"Type": "Pass", "Assign": {"x": 2}, "End": True}
    return {"StartAt": name, "States": {name: state}}


def fan(**fields: object) -> dict:
    return {"Type": "Parallel", "Branches": [assigning()], **fields}


def pass_to(target: str, **fields: object) -> dict:
    return {"Type": "Pass", "Next": target, **fields}


def assigns_x(target: str) -> dict:
    return pass_to(target, Assign={"x": 1})


def ends(state: dict) -> dict:
    return {**state, "End": True}


def choice(rule: dict, default: str, **fields: object) -> dict:
    return {
        "Type": "Choice",
        "Choices": [{"Condition": "{% true %}", **rule}],
        "Default": default,
        **fields,
    }


def output(code: object, state: dict | None = None) -> dict:
    return states("a", a=ends({**(state or {"Type": "Pass"}), "Output": code}))


def caught(**fields: object) -> dict:
    catcher = {"ErrorEquals": ["States.ALL"], "Next": "s", **fields}
    return states("t", t=ends({**TASK, "Catch": [catcher]}), s=DONE)


def mapped(mode: str) -> dict:
    processor = {"ProcessorConfig": {"Mode": mode}, **assigning()}
    return states(
        "a",
        a=assigns_x("p"),
        p=ends({"Type": "Map", "Items": [1], "ItemProcessor": processor}),
    )


@pytest.mark.parametrize(
    "definition, message",
    [
        (output("{% (1 %}"), r"a.Output: \(1 does not parse"),
        (output("{% 1e400 %}"), "a.Output: 1e400 does not parse"),
        (output({"k": ["{% (1 %}"]}), "a.Output: .* does not parse"),
        (
            states("a", a=ends({"Type": "Pass", "Assign": {"Comment": "{% (1 %}"}})),
            "a.Assign: .* does not parse",
        ),
        (states("zz", a=ends({"Type": "Pass"})), "StartAt: there is no state zz"),
        (states("a", a=pass_to("zz")), "a: there is no state zz to go to"),
        (
            states("a", a=choice({"Next": "a"}, "zz")),
            "a: there is no state zz to go to",
        ),
        (caught(Next="zz"), "t: there is no state zz to go to"),
        (output("{% $states.result %}"), r"a.Output: \$states has no result here"),
        (output("{% $states.result.x %}"), r"\$states has no result"),
        (output("{% $states.result[0] %}"), r"\$states has no result"),
        (output("{% $states.`result` %}"), r"\$states has no result"),
        (output("{% $count([$states.result]) %}"), r"\$states has no result"),
        (output("{% $states.foo %}"), r"\$states has no foo"),
        (output("{% $states.result %}", {"Type": "Wait", "Seconds": 0}), "result"),
        (
            states("a", a={**DONE, "Output": "{% $states.result %}"}),
            r"a.Output: \$states has no result",
        ),
        (
            states(
                "a", a=ends({"Type": "Pass", "Assign": {"v": "{% $states.result %}"}})
            ),
            r"a.Assign: \$states has no result",
        ),
        (
            states(
                "a",
                a=choice({"Next": "s", "Output": "{% $states.result %}"}, "s"),
                s=DONE,
            ),
            r"a.Choices: \$states has no result",
        ),
        (
            states("t", t=ends({**TASK, "Arguments": "{% $states.result %}"})),
            r"t.Arguments: \$states has no result",
        ),
        (
            output("{% $states.errorOutput %}", TASK),
            r"a.Output: \$states has no errorOutput",
        ),
        (
            states("a", a={"Type": "Fail", "Cause": "{% $states.errorOutput %}"}),
            r"a.Cause: \$states has no errorOutput",
        ),
        (caught(Output="{% $states.result %}"), r"t.Catch: \$states has no result"),
        (
            states("a", a=assigns_x("p"), p=ends(fan())),
            "b: x is assigned on the way into this branch",
        ),
        (
            states(
                "c", c=choice({"Next": "p", "Assign": {"x": 1}}, "p"), p=ends(fan())
            ),
            "b: x is assigned",
        ),
        (
            states(
                "c",
                c=choice({"Next": "a"}, "p"),
                a=assigns_x("p"),
                p=ends(fan()),
            ),
            "b: x is assigned",
        ),
        (
            states(
                "p",
                p=fan(Next="a"),
                a=assigns_x("c"),
                c=choice({"Next": "p"}, "s"),
                s=DONE,
            ),
            "b: x is assigned",
        ),
        (
            states(
                "p",
                p=fan(Assign={"x": 1}, Next="c"),
                c=choice({"Next": "p"}, "s"),
                s=DONE,
            ),
            "b: x is assigned",
        ),
        (
            states(
                "q",
                q=pass_to("p", Assign={"x": 1}),
                p=ends(fan()),
            ),
            "b: x is assigned",
        ),
        (
            states(
                "t",
                t={
                    **TASK,
                    "Next": "p",
                    "Catch": [
                        {"ErrorEquals": ["States.ALL"], "Assign": {"x": 1}, "Next": "p"}
                    ],
                },
                p=ends(fan()),
            ),
            "b: x is assigned",
        ),
        (
            states(
                "a",
                a=assigns_x("p"),
                p=ends(
                    {
                        "Type": "Parallel",
                        "Branches": [{"StartAt": "q", "States": {"q": ends(fan())}}],
                    }
                ),
            ),
            "b: x is assigned",
        ),
        (
            states(
                "p",
                p=ends(
                    {
                        "Type": "Parallel",
                        "Branches": [
                            {
                                "StartAt": "a",
                                "States": {"a": assigns_x("q"), "q": ends(fan())},
                            }
                        ],
                    }
                ),
            ),
            "b: x is assigned",
        ),
        (mapped("INLINE"), "b: x is assigned"),
        (mapped("DISTRIBUTED"), "b: x is assigned"),
    ],
)
def test_what_step_functions_rejects_is_rejected_before_it_runs(definition, message):
    with pytest.raises(testing.InvalidDefinition, match=message):
        testing.run(definition, {}, lambda call: {})


@pytest.mark.parametrize(
    "definition",
    [
        states("a", a=ends({"Type": "Pass", "Comment": "{% (1 %}"})),
        states("a", a=choice({"Next": "s", "Comment": "{% (1 %}"}, "s"), s=DONE),
        caught(Comment="{% (1 %}"),
        output("{% '$states.result' %}"),
        output("{% $states.input.result %}"),
        output("{% ($states).result %}"),
        output("{% $states.input.x.$states.qux %}"),
        output("{% [$states, $states.input, $states.context] %}"),
        output("{% $states.result %}", TASK),
        output("{% $map([1], function($v) { $states.result }) %}", TASK),
        states("t", t=ends({**TASK, "Assign": {"v": "{% $states.result %}"}})),
        output("{% $states.result %}", fan()),
        caught(Output="{% [$states.errorOutput.Cause, $states.input] %}"),
        caught(Assign={"e": "{% $states.errorOutput %}"}),
        states("p", p=fan(Next="a"), a=ends({"Type": "Pass", "Assign": {"x": 1}})),
        states("p", p=ends(fan(Assign={"x": 1}))),
        states(
            "p",
            p=ends(
                fan(
                    Catch=[
                        {"ErrorEquals": ["States.ALL"], "Assign": {"x": 1}, "Next": "s"}
                    ]
                )
            ),
            s=DONE,
        ),
        states(
            "t",
            t={
                **TASK,
                "Assign": {"x": 1},
                "Next": "s",
                "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "p"}],
            },
            s=DONE,
            p=ends(fan()),
        ),
        states(
            "c",
            c=choice({"Next": "s", "Assign": {"x": 1}}, "p"),
            s=DONE,
            p=ends(fan()),
        ),
        states(
            "c",
            c=choice({"Next": "p"}, "s", Assign={"x": 1}),
            s=DONE,
            p=ends(fan()),
        ),
        states(
            "t",
            t={
                **TASK,
                "Next": "p",
                "Catch": [
                    {"ErrorEquals": ["States.ALL"], "Assign": {"x": 1}, "Next": "s"}
                ],
            },
            s=DONE,
            p=ends(fan()),
        ),
        states(
            "p",
            p={
                "Type": "Parallel",
                "Branches": [{"StartAt": "q", "States": {"q": ends(fan())}}],
                "Next": "a",
            },
            a=ends({"Type": "Pass", "Assign": {"x": 1}}),
        ),
        states(
            "p",
            p=ends({"Type": "Parallel", "Branches": [assigning("b"), assigning("c")]}),
        ),
    ],
)
def test_what_step_functions_accepts_runs(definition):
    testing.run(definition, {}, lambda call: {})


def test_a_definition_whose_every_state_sets_jsonata_runs():
    jsonata = {"QueryLanguage": "JSONata"}
    processor = {
        "StartAt": "p",
        "States": {
            "p": {
                **jsonata,
                "Type": "Pass",
                "Output": "{% $states.input * 2 %}",
                "End": True,
            }
        },
    }
    definition = {
        "StartAt": "m",
        "States": {
            "m": {
                **jsonata,
                "Type": "Map",
                "Items": "{% [1, 2] %}",
                "ItemProcessor": processor,
                "End": True,
            }
        },
    }
    assert testing.run(definition, {}).output == [2, 4]


@pytest.mark.parametrize(
    "code, cause",
    [
        ('$merge([{"a": 1}, [{"b": 2}]])', "T0412: Argument {{index}} of Object"),
        ('1 < "a"', "T2009: The values {{value}}"),
    ],
)
def test_an_error_whose_message_jsonata_python_cannot_write_fails(code, cause):
    """jsonata-python raises an IndexError while it writes the message."""
    failed = testing.run(
        machine({"Type": "Succeed", "Output": "{% " + code + " %}"}), {}
    )
    assert failed.error == "States.QueryEvaluationError"
    assert failed.cause.startswith(cause)


def test_an_index_error_from_a_function_reaches_the_test():
    now = machine({"Type": "Succeed", "Output": "{% $now() %}"})

    def failing() -> str:
        raise IndexError("now")

    with pytest.raises(IndexError, match="now"):
        testing.run(now, {}, functions={"now": failing})


def test_text_that_is_not_json_fails_to_parse():
    parsed = machine({"Type": "Succeed", "Output": "{% $parse('{') %}"})
    assert testing.run(parsed, {}).error == "States.QueryEvaluationError"


# What Step Functions gives for the arguments of the functions it adds and of
# those the runner replaces, measured with TestState or recorded on AWS by
# tiny-asl-machine's conformance tests.
@pytest.mark.parametrize(
    "code, expected",
    [
        ("$exists($parse($nothing))", False),
        ("$exists($parse())", False),
        ("$exists($parse($match('abc', /x/)[0].match))", False),
        ("$exists($hash($nothing, 'SHA-256'))", False),
        ("$exists($hash('a'))", False),
        ("$hash('a', 'MD5')", "0cc175b9c0f1b6a831c399e269772661"),
        ("$exists($partition($nothing, 2))", False),
        ("$partition([1, 2, 3], $nothing)", [[1, 2, 3]]),
        ("$partition([], $nothing)", [[]]),
        ("$exists($range($nothing, 3, 1))", False),
        ("$exists($range(0, $nothing, 1))", False),
        ("$exists($range(0, 3, $nothing))", False),
        ("$exists($range(1, 5))", False),
        ("$exists($range(1, 10, 0.49))", False),
        ("$range(1.4, 5.6, 2.2)", [1, 3, 5]),
        ("$range(-1.5, 2.5, 1)", [-1, 0, 1, 2]),
        ("$partition([1, 2, 3, 4, 5], 2.6)", [[1, 2], [3, 4], [5]]),
        ("$partition([1, 2, 3])", [[1, 2, 3]]),
        ("$exists($partition([1, 2, 3], 0.5))", False),
        ("$random(77) = $random(77)", True),
        ("$random(1.5) < 1", True),
        ("$fromMillis(0, '[H01]:[m01]', '+0900')", "09:00"),
        ("$fromMillis(0, '[H01]', 'utc')", "00"),
        ("$now('[Z]', 'utc') = $now('[Z]')", True),
        ("$exists($formatNumber($nothing, '0'))", False),
        ("$exists($base64decode($nothing))", False),
        ("$exists($decodeUrlComponent($nothing))", False),
        ("$exists($fromMillis($nothing))", False),
        ("$exists($fromMillis('a'))", False),
    ],
)
def test_functions_give_what_step_functions_gives(code, expected):
    output = machine({"Type": "Succeed", "Output": "{% " + code + " %}"})
    assert testing.run(output, {}).output == expected


# $range gives a sequence, as measured on Step Functions.
@pytest.mark.parametrize(
    "code, expected",
    [
        ("$range(1, 9, 9)", 1),
        ("[$range(1, 9, 9)]", [1]),
        ("$exists($range(3, 0, 1))", False),
        ("[$range(3, 0, 1)]", []),
        ("$range(0, 9, 3)", [0, 3, 6, 9]),
    ],
)
def test_range_gives_a_sequence(code, expected):
    output = machine({"Type": "Succeed", "Output": "{% " + code + " %}"})
    assert testing.run(output, {}).output == expected


@pytest.mark.parametrize(
    "code, cause",
    [
        ("$parse(null)", 'Argument 1 of function "parse"'),
        ("$hash('a', $nothing)", "Hash algorithm 'null' must be one of"),
        ("$hash('a', 'sha-256')", "Hash algorithm 'sha-256' must be one of"),
        ("$hash('a', 'SHA256')", "Hash algorithm 'SHA256' must be one of"),
        ("$hash(123, 'SHA-256')", 'Argument 1 of function "hash"'),
        ("$hash()", 'Argument 1 of function "hash"'),
        ("$hash('a', 'SHA-256', 'x')", 'Argument 3 of function "hash"'),
        ("$parse(123)", 'Argument 1 of function "parse"'),
        ("$parse('{}', 1)", 'Argument 2 of function "parse"'),
        ("$uuid('x')", 'Argument 1 of function "uuid"'),
        ("$uuid(1, 2)", 'Argument 1 of function "uuid"'),
        ("$random('seed')", 'Argument 1 of function "random"'),
        ("$random(null)", 'Argument 1 of function "random"'),
        ("$random(77, 88)", 'Argument 2 of function "random"'),
        ("$partition([1, 2, 3], -1)", "Second argument must be zero or greater"),
        ("$partition([1, 2, 3], '2')", 'Argument 2 of function "partition"'),
        ("$partition([1, 2, 3], null)", 'Argument 2 of function "partition"'),
        ("$partition([1, 2, 3], true)", 'Argument 2 of function "partition"'),
        ("$partition('not-array', 2)", 'Argument 1 of function "partition"'),
        ("$partition([1, 2, 3], 2, 99)", 'Argument 3 of function "partition"'),
        ("$range('a', 5, 1)", 'Argument 1 of function "range"'),
        ("$range(1, 5, null)", 'Argument 3 of function "range"'),
        ("$range(1, 5, 1, 99)", 'Argument 4 of function "range"'),
        ("$formatNumber('a', '0')", 'function "formatNumber"'),
        ("$formatNumber(1, 2)", 'Argument 2 of function "formatNumber"'),
        ("$formatNumber(1, '0', 'x')", 'Argument 3 of function "formatNumber"'),
        ("$now(1)", 'Argument 1 of function "now"'),
        ("$fromMillis(0, 1)", 'Argument 2 of function "fromMillis"'),
        ("$base64decode(123)", 'Argument 1 of function "base64decode"'),
        ("$decodeUrlComponent(1)", 'Argument 1 of function "decodeUrlComponent"'),
        ("$string(1, 1)", 'Argument 2 of function "string"'),
        ("$string(1, true, 1)", 'Argument 3 of function "string"'),
    ],
)
def test_functions_fail_where_step_functions_fails(code, cause):
    output = machine({"Type": "Succeed", "Output": "{% " + code + " %}"})
    execution = testing.run(output, {})
    assert execution.error == "States.QueryEvaluationError"
    assert cause in execution.cause


# What a Wait reads at run time: Timestamp and Seconds measured with TestState.
@pytest.mark.parametrize(
    "field, given, fails",
    [
        ("Timestamp", "2016-12-05T21:29:29Z", False),
        ("Timestamp", "2016-12-05T21:29:29.123456789Z", False),
        ("Timestamp", "2016-12-05T21:29:29+09:00", False),
        ("Timestamp", "2016-12-05T21:29Z", False),
        ("Timestamp", "2016-12-05t21:29:29z", False),
        ("Timestamp", "2016-12-05 21:29:29Z", True),
        ("Timestamp", "2016-12-05T21:29:29", True),
        ("Timestamp", "2016-02-30T21:29:29Z", True),
        ("Timestamp", "2016-12-05T25:29:29Z", True),
        ("Timestamp", 1480973369, True),
        ("Seconds", 0, False),
        ("Seconds", 0.0, False),
        ("Seconds", 100_000_000, False),
        ("Seconds", -1, True),
        ("Seconds", 1.5, True),
        ("Seconds", "3", True),
        ("Seconds", None, True),
        ("Seconds", True, True),
    ],
)
def test_a_wait_fails_on_what_step_functions_cannot_read(field, given, fails):
    wait = machine({"Type": "Wait", field: "{% $states.input.v %}", "End": True})
    execution = testing.run(wait, {"v": given})
    assert execution.error == ("States.QueryEvaluationError" if fails else None)


def inline_map(**fields: object) -> dict:
    processor = {"StartAt": "p", "States": {"p": {"Type": "Pass", "End": True}}}
    return machine({"Type": "Map", "ItemProcessor": processor, "End": True, **fields})


# The Map tests below follow results LocalStack's Step Functions tests
# recorded on AWS.
def test_a_map_without_items_or_processor_config_iterates_its_input_inline():
    assert testing.run(inline_map(), [1, "two", True]).output == [1, "two", True]
    assert testing.run(inline_map(), 1).error == "States.QueryEvaluationError"


@pytest.mark.parametrize("items", ["1", "'string'", "true", "{'foo': 'bar'}", "null"])
def test_an_inline_map_fails_on_items_that_are_not_an_array(items):
    execution = testing.run(inline_map(Items="{% " + items + " %}"), {})
    assert execution.error == "States.QueryEvaluationError"


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


def chain(*states: dict) -> dict:
    """A machine of the states given, each named by its position and going
    to the next, the last ending unless it is a Succeed, all reading $v as 0
    from a first Pass."""
    named = {"v": {"Type": "Pass", "Assign": {"v": 0}, "Next": "0"}}
    for position, state in enumerate(states):
        if position < len(states) - 1:
            named[str(position)] = {**state, "Next": str(position + 1)}
        elif state["Type"] == "Succeed":
            named[str(position)] = state
        else:
            named[str(position)] = {**state, "End": True}
    return {"QueryLanguage": "JSONata", "StartAt": "v", "States": named}


OLD_AND_INPUT = {"old": "{% $v %}", "in": "{% $states.input %}"}


@pytest.mark.parametrize(
    "state",
    [
        {"Type": "Pass"},
        {"Type": "Wait", "Seconds": 0},
    ],
)
def test_a_pass_and_a_wait_assign_and_output_and_end(state):
    """Assign and Output read the variables from before the state, and
    without an Output the input goes on (measured)."""
    assigned = {**state, "Assign": {"v": 5}, "Output": OLD_AND_INPUT}
    assert testing.run(chain(assigned), 1).output == {"old": 0, "in": 1}
    assert testing.run(chain(state, {"Type": "Succeed"}), 1).output == 1
    read = {"Type": "Succeed", "Output": "{% $v %}"}
    assert testing.run(chain(assigned, read), 1).output == 5


def choice(test: str) -> dict:
    """A Choice whose one rule and whose Default both assign and output, each
    going to a Succeed that reads $v and the input."""
    return {
        "QueryLanguage": "JSONata",
        "StartAt": "c",
        "States": {
            "c": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Condition": test,
                        "Next": "end",
                        "Assign": {"v": "rule"},
                        "Output": {"by": "rule", "in": "{% $states.input %}"},
                    }
                ],
                "Default": "end",
                "Assign": {"v": "state"},
                "Output": {"by": "state"},
            },
            "end": {
                "Type": "Succeed",
                "Output": {"v": "{% $v %}", "out": "{% $states.input %}"},
            },
        },
    }


def test_a_choice_assigns_and_outputs_by_the_rule_it_takes_or_by_its_default():
    """Measured: a rule that matches uses its own Assign and Output and not
    the state's, and the Default uses the state's."""
    assert testing.run(choice("{% true %}"), 1).output == {
        "v": "rule",
        "out": {"by": "rule", "in": 1},
    }
    assert testing.run(choice("{% false %}"), 1).output == {
        "v": "state",
        "out": {"by": "state"},
    }
    plain = machine(
        {"Type": "Choice", "Choices": [{"Condition": "{% true %}", "Next": "e"}]}
    )
    plain["States"]["e"] = {"Type": "Succeed"}
    assert testing.run(plain, 1).output == 1


def test_a_catcher_outputs_from_the_error_the_input_and_the_old_variables():
    task = {
        "Type": "Task",
        "Resource": LAMBDA,
        "Catch": [
            {
                "ErrorEquals": ["States.ALL"],
                "Next": "caught",
                "Assign": {"v": 5},
                "Output": {**OLD_AND_INPUT, "e": "{% $states.errorOutput.Error %}"},
            }
        ],
    }
    definition = chain(task, {"Type": "Succeed"})
    definition["States"]["caught"] = {
        "Type": "Succeed",
        "Output": {"v": "{% $v %}", "out": "{% $states.input %}"},
    }

    def fail(call: testing.Call) -> object:
        raise testing.Failure("Boom", "c")

    assert testing.run(definition, 1, fail).output == {
        "v": 5,
        "out": {"old": 0, "in": 1, "e": "Boom"},
    }


def test_the_arguments_of_a_parallel_are_the_input_of_every_branch():
    branch = {
        "StartAt": "b",
        "States": {"b": {"Type": "Succeed", "Output": "{% $states.input %}"}},
    }
    parallel = machine(
        {
            "Type": "Parallel",
            "Arguments": {"a": "{% $states.input.x %}"},
            "Branches": [branch, branch],
            "End": True,
        }
    )
    assert testing.run(parallel, {"x": 7}).output == [{"a": 7}, {"a": 7}]


def test_the_credentials_of_a_task_are_accepted():
    task = machine(
        {
            "Type": "Task",
            "Resource": LAMBDA,
            "Credentials": {"RoleArn": "arn:aws:iam::123456789012:role/r"},
            "End": True,
        }
    )
    assert testing.run(task, {}, lambda call: "ok").output == "ok"
