import re
import textwrap

import pytest

import sfnx
from sfnx import aws
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.integrations import ResourceError, operation_resource

ACTIVITY = "arn:aws:states:us-east-1:123456789012:activity:review"


def source(body: str, preamble: str = "") -> str:
    return (
        "from sfnx import activity, aws, context, state_machine, task\n"
        f"{preamble}\n\n@state_machine\ndef f(input):\n" + textwrap.indent(body, "    ")
    )


def states(body: str, preamble: str = "") -> dict:
    (compiled,) = compile_source(source(body, preamble)).values()
    return compiled["States"]


def only(body: str, preamble: str = "") -> tuple[str, dict]:
    ((name, state), *_) = states(body, preamble).items()
    return name, state


@pytest.mark.parametrize(
    "kind, service, operation, resource",
    [
        (
            "sdk",
            "dynamodb",
            "update_item",
            "arn:aws:states:::aws-sdk:dynamodb:updateItem",
        ),
        (
            "sdk",
            "rds",
            "describe_db_instances",
            "arn:aws:states:::aws-sdk:rds:describeDBInstances",
        ),
        (
            "sdk",
            "lambda_",
            "get_function",
            "arn:aws:states:::aws-sdk:lambda:getFunction",
        ),
        (
            "sdk",
            "cloudwatchlogs",
            "get_log_events",
            "arn:aws:states:::aws-sdk:cloudwatchlogs:getLogEvents",
        ),
        ("optimized", "lambda_", "invoke", "arn:aws:states:::lambda:invoke"),
        (
            "optimized",
            "states",
            "start_execution",
            "arn:aws:states:::states:startExecution",
        ),
        (
            "optimized",
            "emr_containers",
            "start_job_run",
            "arn:aws:states:::emr-containers:startJobRun",
        ),
        ("optimized", "http", "invoke", "arn:aws:states:::http:invoke"),
    ],
)
def test_operation_resource(kind, service, operation, resource):
    assert operation_resource(kind, service, operation) == resource


@pytest.mark.parametrize(
    "kind, service, operation, message",
    [
        (
            "sdk",
            "dynamodb",
            "updte_item",
            "dynamodb has no operation updte_item; did you mean update_item",
        ),
        ("sdk", "dynamodb", "zzz", "dynamodb has no operation zzz"),
        (
            "sdk",
            "logs",
            "get_log_events",
            (
                "SDK integrations name the service cloudwatchlogs, not logs: "
                "aws.sdk.cloudwatchlogs.get_log_events(...)"
            ),
        ),
        ("optimized", "Lambda", "invoke", "aws.optimized.<service>.<operation>"),
        ("optimized", "lambda_", "startExecution", "in lowercase"),
    ],
)
def test_operation_resource_diagnostics(kind, service, operation, message):
    with pytest.raises(ResourceError, match=re.escape(message)):
        operation_resource(kind, service, operation)


def test_an_operation_is_the_task_that_writes_its_arn():
    written = (
        "aws.sdk.dynamodb.update_item(\n"
        '    TableName="stock",\n'
        '    Key={"sku": {"S": input["sku"]}},\n'
        '    UpdateExpression="SET quantity = quantity - :n",\n'
        "    timeout=10,\n"
        "    heartbeat=5,\n"
        '    role="arn:aws:iam::123456789012:role/stock",\n'
        '    retry=[{"ErrorEquals": ["States.Timeout"], "MaxAttempts": 2}],\n'
        ")\nreturn 0"
    )
    task = (
        'task(\n    "arn:aws:states:::aws-sdk:dynamodb:updateItem",\n'
        '    {"TableName": "stock", "Key": {"sku": {"S": input["sku"]}}, '
        '"UpdateExpression": "SET quantity = quantity - :n"},\n'
        "    timeout=10,\n"
        "    heartbeat=5,\n"
        '    role="arn:aws:iam::123456789012:role/stock",\n'
        '    retry=[{"ErrorEquals": ["States.Timeout"], "MaxAttempts": 2}],\n'
        ")\nreturn 0"
    )
    assert states(
        written.replace('"States.Timeout"', "Timeout"), "from sfnx import Timeout"
    ) == states(task.replace('"States.Timeout"', "Timeout"), "from sfnx import Timeout")


@pytest.mark.parametrize(
    "call, fields",
    [
        (
            'aws.optimized.states.start_execution(StateMachineArn="a", pattern=".sync:2")',
            {
                "Resource": "arn:aws:states:::states:startExecution.sync:2",
                "Arguments": {"StateMachineArn": "a"},
            },
        ),
        (
            (
                'aws.sdk.sqs.send_message(QueueUrl="q", MessageBody=context["Task"]["Token"], '
                'pattern=".waitForTaskToken")'
            ),
            {
                "Resource": "arn:aws:states:::aws-sdk:sqs:sendMessage.waitForTaskToken",
                "Arguments": {
                    "QueueUrl": "q",
                    "MessageBody": "{% $states.context.Task.Token %}",
                },
            },
        ),
        (
            (
                'aws.optimized.http.invoke(ApiEndpoint="https://example.com", Method="GET", '
                'InvocationConfig={"ConnectionArn": "c"})'
            ),
            {
                "Resource": "arn:aws:states:::http:invoke",
                "Arguments": {
                    "ApiEndpoint": "https://example.com",
                    "Method": "GET",
                    "InvocationConfig": {"ConnectionArn": "c"},
                },
            },
        ),
        (
            "aws.sdk.rds.describe_db_instances()",
            {
                "Resource": "arn:aws:states:::aws-sdk:rds:describeDBInstances",
                "Arguments": {},
            },
        ),
        (
            "aws.sdk.dynamodb.put_item(**input)",
            {
                "Resource": "arn:aws:states:::aws-sdk:dynamodb:putItem",
                "Arguments": "{% $states.context.Execution.Input %}",
            },
        ),
    ],
)
def test_operations(call, fields):
    _, state = only(f"{call}\nreturn 0")
    assert {key: state[key] for key in fields} == fields


def test_unpacked_dicts_merge_with_the_parameters():
    _, state = only('aws.sdk.dynamodb.put_item(**input["item"], TableName="t")')
    assert state["Arguments"].startswith("{% $merge([")
    assert state["Arguments"].endswith("{'TableName': 't'}]) %}")


def test_an_operation_is_read_like_a_task():
    body = (
        'item = aws.sdk.dynamodb.get_item(TableName="t", Key={})["Item"]\nreturn item'
    )
    name, state = only(body)
    assert name == "item"
    assert state["Output"] == "{% $states.result.Item %}"


def test_an_operation_is_named_after_its_action():
    name, _ = only('aws.sdk.dynamodb.get_item(TableName="t", Key={})\nreturn 0')
    assert name == "getItem"


def test_the_errors_of_an_operation_are_caught():
    body = (
        "try:\n"
        '    aws.sdk.sqs.send_message(QueueUrl="q", MessageBody="m")\n'
        "except aws.sdk.sqs.errors.QueueDoesNotExistException:\n"
        '    raise aws.sdk.sqs.errors.SqsException("gone")\n'
        "return 0"
    )
    compiled = states(body)
    assert compiled["sendMessage"]["Catch"][0]["ErrorEquals"] == [
        "Sqs.QueueDoesNotExistException"
    ]
    assert compiled["raise"] == {
        "Type": "Fail",
        "Error": "Sqs.SqsException",
        "Cause": "gone",
    }


def test_a_function_calling_an_operation_makes_states():
    preamble = (
        "\ndef reserve(sku):\n"
        '    return aws.sdk.dynamodb.get_item(TableName="t", Key={"sku": {"S": sku}})\n'
    )
    _name, state = only('return reserve(input["sku"])', preamble)
    assert state["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:getItem"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'aws.sdk.dynamodb.updte_item(TableName="t", Key={})',
            "dynamodb has no operation updte_item; did you mean update_item",
        ),
        (
            'aws.sdk.dynamodb.update_item(table_name="t")',
            (
                "API parameters are PascalCase, and timeout=, heartbeat=, role=, "
                "retry= and pattern= set the Task; table_name is neither"
            ),
        ),
        (
            'aws.sdk.dynamodb.update_item("t")',
            "pass the API parameters by name, in PascalCase: TableName=...",
        ),
        ('aws.sdk.dynamodb.update_item(TableName="t")', "updateItem needs Key"),
        (
            'aws.sdk.dynamodb.get_item(TableName="t", Key={}, Tablename="x")',
            "getItem has no argument Tablename; did you mean TableName?",
        ),
        (
            'aws.sdk.sqs.send_message(QueueUrl="q", MessageBody="m", pattern=".sync")',
            "SDK integrations support only .waitForTaskToken, not .sync",
        ),
        (
            'aws.sdk.sqs.send_message(QueueUrl="q", MessageBody="m", pattern=input["p"])',
            "pattern is a literal string",
        ),
        (
            (
                'aws.sdk.sqs.send_message(QueueUrl="q", MessageBody="m", '
                'pattern=".waitForTaskToken")'
            ),
            "pass context",
        ),
        ('aws.sdk.dynamodb(TableName="t")', "call an operation of a service"),
        ("aws.sdk.dynamodb.errors()", "call an operation of a service"),
        ("aws.lambda_.invoke()", "call an operation of a service"),
        (
            (
                'xs: list = input["xs"]\n'
                'ys = [aws.sdk.sqs.send_message(QueueUrl="q", MessageBody=x) for x in xs]'
            ),
            "aws.sdk.sqs.send_message() in a comprehension",
        ),
        (
            'x = aws.optimized.Lambda.invoke(FunctionName="f")',
            "aws.optimized.<service>.<operation> in lowercase",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        states(body)


def test_activity():
    name, state = only(
        f'review = activity("{ACTIVITY}", {{"doc": input["doc"]}}, '
        "timeout=3600, heartbeat=60)\nreturn review"
    )
    assert (name, state) == (
        "review",
        {
            "Type": "Task",
            "Resource": ACTIVITY,
            "Arguments": {"doc": "{% $states.context.Execution.Input.doc %}"},
            "TimeoutSeconds": 3600,
            "HeartbeatSeconds": 60,
            "End": True,
        },
    )


@pytest.mark.parametrize(
    "resource, preamble, name",
    [
        ('"${ReviewArn}"', "", "activity"),
        ("REVIEW", f'REVIEW = "{ACTIVITY}"', "review"),
    ],
)
def test_the_resource_of_an_activity(resource, preamble, name):
    state_name, state = only(f"activity({resource})\nreturn 0", preamble)
    assert state_name == name
    assert "Arguments" not in state


@pytest.mark.parametrize(
    "body, preamble, message",
    [
        (
            'activity("arn:aws:states:::lambda:invoke", input)',
            "",
            "activity takes an activity ARN",
        ),
        (
            "activity(L, input)",
            'L = "arn:aws:states:::lambda:invoke"',
            "an activity ARN",
        ),
        ("activity(input['arn'])", "", "the resource is a literal ARN string"),
        ('activity("${R}", input, role="r")', "", "activity takes timeout="),
        ("activity()", "", "activity takes the activity ARN and its input"),
        ('x = input["a"] and activity("${R}")', "", "activity() here would run"),
    ],
)
def test_activity_diagnostics(body, preamble, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        states(body, preamble)


def test_at_run_time_tasks_are_not_run():
    with pytest.raises(NotImplementedError, match=r"aws\.sdk\.dynamodb\.get_item\(\)"):
        aws.sdk.dynamodb.get_item(TableName="t")
    with pytest.raises(NotImplementedError, match="runs in Step Functions"):
        sfnx.activity(ACTIVITY, {})


def test_at_run_time_private_names_are_not_operations():
    assert not hasattr(aws.sdk.dynamodb, "_operation")
    assert not hasattr(aws.optimized, "_services")
