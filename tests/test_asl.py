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
    return machine({"Type": "Task", "Retry": retry, "End": True, **fields})


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
    branch = {"StartAt": "t", "States": {"t": {"Type": "Task", "End": True}}}
    parallel = retrying([{"ErrorEquals": ["Boom"]}], Type="Parallel", Branches=[branch])
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
    assert asl.exceeds(failed, total, count, percentage) is expected


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
    assert read == [{**reader, "Arguments": {"Bucket": "b", "Key": "items.json"}}]
    writer = {
        "Resource": "arn:aws:states:::s3:putObject",
        "Arguments": {"Bucket": "b", "Prefix": "p"},
    }
    written = asl.run(distributed(child, ResultWriter=writer, Label="l"), [1])
    assert written == {
        "MapRunArn": f"{asl.MAP_RUN}/l:run",
        "ResultWriterDetails": {"Bucket": "b", "Key": "p/run/manifest.json"},
    }
