import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.integrations import integration
from tests import asl

INPUT = "$states.context.Execution.Input"
LAMBDA = "arn:aws:states:::lambda:invoke"
GET_ITEM = "arn:aws:states:::aws-sdk:dynamodb:getItem"
QUERY = "arn:aws:states:::aws-sdk:dynamodb:query"
PUBLISH = "arn:aws:states:::aws-sdk:sns:publish"


def source(
    body: str, imports: str = "from sfnx import context, state_machine, task, wait"
) -> str:
    return f"{imports}\n\n\n@state_machine\ndef pay(input):\n" + textwrap.indent(
        body, "    "
    )


def definition(
    body: str, imports: str = "from sfnx import context, state_machine, task, wait"
) -> dict:
    (compiled,) = compile_source(source(body, imports)).values()
    return compiled


def states(body: str) -> dict:
    return definition(body)["States"]


def test_a_task_assigns_its_result():
    body = f'receipt = task(\n    "{LAMBDA}",\n    {{"FunctionName": "charge", "Payload": input}},\n    timeout=30,\n)\nreturn receipt'
    assert definition(body) == {
        "QueryLanguage": "JSONata",
        "StartAt": "receipt",
        "States": {
            "receipt": {
                "Type": "Task",
                "Resource": LAMBDA,
                "Arguments": {"FunctionName": "charge", "Payload": f"{{% {INPUT} %}}"},
                "TimeoutSeconds": 30,
                "Assign": {"receipt": "{% $states.result %}"},
                "Next": "return",
            },
            "return": {"Type": "Succeed", "Output": "{% $receipt %}"},
        },
    }


def test_a_task_at_the_end_ends_the_machine():
    assert states(f'return task("{PUBLISH}", {{"Message": "hi"}})') == {
        "return": {
            "Type": "Task",
            "Resource": PUBLISH,
            "Arguments": {"Message": "hi"},
            "End": True,
        }
    }
    assert states(
        f'return {{"id": task("{PUBLISH}", {{"Message": "hi"}})["MessageId"]}}'
    )["return"] == {
        "Type": "Task",
        "Resource": PUBLISH,
        "Arguments": {"Message": "hi"},
        "Output": {"id": "{% $states.result.MessageId %}"},
        "End": True,
    }


@pytest.mark.parametrize(
    "call, name",
    [
        (f'task("{PUBLISH}", {{"Message": "hi"}})', "publish"),
        (f'task("{LAMBDA}", {{"FunctionName": "f"}})', "invoke"),
        (
            'task("arn:aws:states:::states:startExecution.sync:2", {"StateMachineArn": "a"})',
            "startExecution",
        ),
        (
            'task("arn:aws:states:us-east-1:123456789012:activity:approve-order", input)',
            "approve-order",
        ),
        (
            'task("arn:aws:lambda:us-east-1:123456789012:function:charge:live", input)',
            "charge",
        ),
        ('task("${ChargeFunctionArn}", {"Payload": input})', "task"),
        (
            'task("arn:aws:states:::http:invoke", {"ApiEndpoint": "https://example.com", "Method": "GET", "InvocationConfig": {"ConnectionArn": "c"}})',
            "invoke",
        ),
        ('task("arn:aws-cn:states:::aws-sdk:dynamodb:listTables")', "listTables"),
    ],
)
def test_a_task_on_its_own_line_is_named_after_its_action(call, name):
    compiled = states(f"{call}\nreturn 1")
    assert list(compiled) == [name, "return"]
    assert compiled[name]["Next"] == "return"
    assert "Assign" not in compiled[name]


def test_the_result_can_be_taken_apart_in_the_assignment():
    compiled = states(
        f'amount = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]["amount"]\nreturn amount'
    )
    assert compiled["amount"]["Assign"] == {
        "amount": "{% $states.result.Payload.amount %}"
    }


def test_options():
    body = f'r = task("{LAMBDA}", {{"FunctionName": "f"}}, timeout=input["timeout"], heartbeat=10, role=input["role"])\nreturn r'
    state = states(body)["r"]
    assert state["TimeoutSeconds"] == f"{{% {INPUT}.timeout %}}"
    assert state["HeartbeatSeconds"] == 10
    assert state["Credentials"] == {"RoleArn": f"{{% {INPUT}.role %}}"}


def test_service_integrations_always_have_arguments():
    state = states('task("arn:aws:states:::aws-sdk:dynamodb:listTables")\nreturn 1')[
        "listTables"
    ]
    assert state["Arguments"] == {}
    state = states(
        'task("arn:aws:states:us-east-1:123456789012:activity:w")\nreturn 1'
    )["w"]
    assert "Arguments" not in state


def test_arguments_that_are_not_a_dict():
    state = states(
        'task("arn:aws:states:us-east-1:123456789012:activity:w", input["payload"])\nreturn 1'
    )["w"]
    assert state["Arguments"] == f"{{% {INPUT}.payload %}}"


def test_pending_assignments_come_before_the_task():
    compiled = states(
        f'fee = 10\nname = "f"\nr = task("{LAMBDA}", {{"FunctionName": name}})\nreturn [fee, r]'
    )
    assert list(compiled) == ["fee", "r", "return"]
    assert compiled["fee"]["Assign"] == {"fee": 10, "name": "f"}


def test_through_the_module():
    compiled = definition(
        f'r = sfnx.task("{PUBLISH}", {{"Message": "m"}})\nreturn r',
        "import sfnx\nfrom sfnx import state_machine",
    )
    assert compiled["States"]["r"]["Resource"] == PUBLISH


@pytest.mark.parametrize(
    "body, output",
    [
        # Types come from the botocore model.
        (
            f'item = task("{GET_ITEM}", {{"TableName": "t", "Key": {{}}}})\nreturn len(item["Item"])',
            "$count($keys($item.Item))",
        ),
        (
            f'page = task("{QUERY}", {{"TableName": "t"}})\nreturn page["Count"] + input["extra"]',
            f"$page.Count + {INPUT}.extra",
        ),
        (
            f'page = task("{QUERY}", {{"TableName": "t"}})\nreturn len(page["Items"])',
            "$count($page.Items)",
        ),
        (
            'r = task("arn:aws:states:::http:invoke", {"ApiEndpoint": "https://e", "Method": "GET", "Authentication": {"ConnectionArn": "c"}})\nreturn len(r["Headers"])',
            "$count($keys($r.Headers))",
        ),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\nreturn r["StatusCode"] + input["x"]',
            f"$r.StatusCode + {INPUT}.x",
        ),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\ntotal: float = r["Payload"]["total"]\nreturn total + input["x"]',
            f"$total + {INPUT}.x",
        ),
    ],
)
def test_result_types(body, output):
    assert states(body)["return"]["Output"] == f"{{% {output} %}}"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\nreturn len(r["Payload"])',
            "len depends on the type",
        ),
        (
            'r = task("arn:aws:states:::states:startExecution.sync:2", {"StateMachineArn": "a"})\nreturn len(r)',
            "len depends on the type",
        ),
    ],
)
def test_what_external_code_returns_is_unknown(body, message):
    with pytest.raises(CompileError, match=message):
        states(body)


def test_asl_run():
    body = (
        f'item = task("{GET_ITEM}", {{"TableName": "orders", "Key": {{"id": {{"S": input["id"]}}}}}})\n'
        'if "Item" not in item:\n    return "missing"\n'
        f'return task("{LAMBDA}", {{"FunctionName": "charge", "Payload": item["Item"]}})["Payload"]'
    )
    seen = []

    def get_item(arguments):
        seen.append(arguments)
        return {"Item": {"id": {"S": "o-1"}}}

    tasks = {
        "item": get_item,
        "return_2": lambda arguments: {"Payload": {"charged": arguments["Payload"]}},
    }
    assert asl.run(definition(body), {"id": "o-1"}, tasks) == {
        "charged": {"id": {"S": "o-1"}}
    }
    assert seen == [{"TableName": "orders", "Key": {"id": {"S": "o-1"}}}]
    assert (
        asl.run(definition(body), {"id": "o-2"}, {"item": lambda arguments: {}})
        == "missing"
    )


@pytest.mark.parametrize(
    "resource, arguments",
    [
        # Optimized integrations with actions of their own are not checked.
        ("arn:aws:states:::apigateway:invoke", '{"ApiEndpoint": "x"}'),
        (
            "arn:aws:states:::elasticmapreduce:addStep.sync",
            '{"ClusterId": "c", "Step": {}}',
        ),
        (
            "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
            '{"QueueUrl": "q", "MessageBody": {"token": context["Task"]["Token"]}}',
        ),
        (
            "arn:aws:states:::aws-sdk:emrserverless:startJobRun",
            '{"ApplicationId": "a", "ExecutionRoleArn": "r", "ClientToken": "t"}',
        ),
        # Service names that differ from botocore's.
        ("arn:aws:states:::aws-sdk:sfn:startExecution", '{"StateMachineArn": "a"}'),
        ("arn:aws:states:::aws-sdk:eventbridge:putEvents", '{"Entries": []}'),
        ("arn:aws:states:::aws-sdk:cloudwatchlogs:describeLogGroups", "{}"),
        (
            "arn:aws:states:::aws-sdk:dynamodb:putItem.waitForTaskToken",
            '{"TableName": "t", "Item": {"token": {"S": context["Task"]["Token"]}}}',
        ),
        ("arn:aws:states:::aws-sdk:organizations:describeOrganization", "{}"),
        (GET_ITEM, "input"),
    ],
)
def test_accepted_resources(resource, arguments):
    assert states(f'task("{resource}", {arguments})\nreturn 1')["return"]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            f'if task("{PUBLISH}", {{"Message": "m"}}):\n    pass',
            "task() makes a Task state; call it on its own line or assign its result",
        ),
        (f'wait(task("{PUBLISH}", {{"Message": "m"}}))', "task() makes a Task state"),
        # a < b < c evaluates c only when a < b holds.
        (
            f'r = 2 < 1 < task("{LAMBDA}", {{"FunctionName": "f"}})["StatusCode"]',
            "task() here would run whether or not this part is taken",
        ),
        (
            f'r = input["a"] and task("{PUBLISH}", {{"Message": "m"}})',
            "task() here would run whether or not this part is taken",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}) if input["a"] else 0',
            "task() here would run whether or not",
        ),
        (
            f'r = [task("{PUBLISH}", {{"Message": "a"}}), task("{PUBLISH}", {{"Message": "b"}})]',
            "one task() or parallel() per line",
        ),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": task("{PUBLISH}", {{"Message": "b"}})}})',
            "task() makes a Task state",
        ),
        ('r = task(input["arn"], {})', "the resource is a literal ARN string"),
        ("r = task()", "task takes a resource ARN and its arguments"),
        (
            f'r = task("{PUBLISH}", {{}}, {{}})',
            "task takes a resource ARN and its arguments",
        ),
        ('r = task("lambda:invoke", {})', "the resource is not a Task ARN"),
        (
            'r = task("arn:aws:states:::aws-sdk:logs:describeLogGroups", {})',
            "SDK integrations name the service cloudwatchlogs, not logs: arn:aws:states:::aws-sdk:cloudwatchlogs:describeLogGroups",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:stepfunctions:startExecution", {"StateMachineArn": "a"})',
            "SDK integrations name the service sfn, not stepfunctions",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:states:startExecution", {"StateMachineArn": "a"})',
            "SDK integrations name the service sfn, not states",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:dynamodbb:getItem", {})',
            "no AWS SDK service is named dynamodbb; did you mean dynamodb",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:dynamodb:getitem", {})',
            "dynamodb has no API action getitem; did you mean getItem?",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:dynamodb:frobnicate", {})',
            "dynamodb has no API action frobnicate",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:dynamodb:getItem.sync", {})',
            "SDK integrations support only .waitForTaskToken, not .sync",
        ),
        (
            'r = task("arn:aws:states:::lambda:invoke.later", {})',
            ".later is not an integration pattern",
        ),
        (
            f'r = task("{GET_ITEM}", {{"Tablename": "t", "Key": {{}}}})',
            "getItem has no argument Tablename; did you mean TableName?",
        ),
        (f'r = task("{GET_ITEM}", {{"Zzz": "t"}})', "getItem has no argument Zzz"),
        (
            f'r = task("{GET_ITEM}", {{**input, "Zzz": "t"}})',
            "getItem has no argument Zzz",
        ),
        (
            f'r = task("{GET_ITEM}", {{"TableName": "t"}})',
            "getItem needs Key in the arguments",
        ),
        (
            f'r = task("{GET_ITEM}")',
            "getItem needs Key, TableName: pass them as task(resource, {...})",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:sfn:startExecution", {"stateMachineArn": "a"})',
            "has no argument stateMachineArn; did you mean StateMachineArn?",
        ),
        (
            f'r = task("{LAMBDA}", {{"Payload": 1}})',
            "invoke needs FunctionName in the arguments",
        ),
        (
            'r = task("arn:aws:states:::http:invoke", {"ApiEndpoint": "https://e", "Method": "GET"})',
            "an HTTP Task needs a connection",
        ),
        (
            'r = task("arn:aws:states:::http:invoke", {"ApiEndpoint": "https://e", "Method": "FETCH", "InvocationConfig": {}})',
            "Method is one of DELETE, GET",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, timeout=0)',
            "timeout is whole seconds from 1 to 99,999,999",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, heartbeat=1.5)',
            "heartbeat is whole seconds",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, timeout="30")',
            "timeout is a number of seconds, not a string",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, timeout=30, heartbeat=30)',
            "heartbeat must be shorter than timeout",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, TopicArn="t")',
            "task takes timeout=, heartbeat=, role= and retry=; put API parameters in the arguments dict",
        ),
        ("r = sfnx.wait(1)", "sfnx.wait() makes a state and has no value"),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f", "Extra": 1}})',
            "invoke has no argument Extra",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:emrserverless:startJobRun", {"ApplicationId": "a", "ExecutionRoleArn": "r"})',
            "startJobRun needs ClientToken in the arguments",
        ),
        (
            'r = task("arn:aws:states:us-east-1:123456789012:activity:w", {}, role="r")',
            "role= applies to Lambda functions and AWS service integrations",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(
            source(
                body, "import sfnx\nfrom sfnx import context, state_machine, task, wait"
            )
        )
    assert message in raised.value.message


def test_unpacked_arguments_leave_required_keys_to_run_time():
    body = f'return task("{GET_ITEM}", {{**input["key"], "TableName": "t"}})'
    assert states(body)["return"]["Arguments"] == (
        f"{{% $merge([{INPUT}.key, {{'TableName': 't'}}]) %}}"
    )


def test_task_does_not_run_in_python():
    import sfnx

    with pytest.raises(NotImplementedError, match="runs in Step Functions"):
        sfnx.task(LAMBDA, {})


def test_integration_kinds():
    assert integration(GET_ITEM).kind == "sdk"
    assert integration(LAMBDA).kind == "optimized"
    assert integration("arn:aws:states:::apigateway:invoke").required == frozenset()


def test_a_task_in_the_first_comparison_of_a_chain_always_runs():
    body = f'return 1 < task("{LAMBDA}", {{"FunctionName": "f"}})["StatusCode"] < 300'
    assert states(body)["return"]["Output"] == (
        "{% 1 < $states.result.StatusCode and $states.result.StatusCode < 300 %}"
    )
