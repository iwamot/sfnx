import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.integrations import integration
from tests import asl, unpacked

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
    body = f'receipt = task(\n    "{LAMBDA}",\n    {{"FunctionName": "charge", "Payload": input}},\n    timeout=30,\n)\nwait(5)\nreturn receipt'
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
                "Next": "wait",
            },
            "wait": {
                "Type": "Wait",
                "Seconds": 5,
                "Output": "{% $receipt %}",
                "End": True,
            },
        },
    }


def test_a_return_right_after_a_task_is_its_output():
    """The Output reads the result where the return reads the variable, and
    the other variables from before the Task, as the return does."""
    body = (
        'fee: float = input["fee"]\n'
        f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\n'
        'return r["Payload"]["total"] + fee'
    )
    assert states(body)["r"] == {
        "Type": "Task",
        "Resource": LAMBDA,
        "Arguments": {"FunctionName": "f"},
        # fee goes in the Task, which reads it as its expression.
        "Assign": {"fee": f"{{% {INPUT}.fee %}}"},
        "Output": f"{{% $states.result.Payload.total + {INPUT}.fee %}}",
        "End": True,
    }
    tasks = {"r": lambda arguments: {"Payload": {"total": 2}}}
    assert asl.run(definition(body), {"fee": 1}, tasks) == 3


R = f'r = task("{LAMBDA}", {{"FunctionName": "f"}}'


@pytest.mark.parametrize(
    "body",
    [
        # A retrier for these errors would run the Task again.
        R + ', retry=[{"ErrorEquals": [Exception]}])\nreturn r["Payload"]',
        R + ', retry=[{"ErrorEquals": [QueryEvaluationError]}])\nreturn r["Payload"]',
        # What could differ between the Task and the state after it.
        R + ')\nreturn [r, jsonata("$random()")]',
        R + ')\nreturn [r, context["State"]["Name"]]',
        # Another state comes between, or several paths lead to the return.
        R + ")\nwait(1)\nreturn r",
        f"if input['a']:\n    {R})\nelse:\n    {R})\nreturn r",
    ],
)
def test_a_return_that_could_fail_or_read_otherwise_keeps_its_state(body):
    imports = (
        "from sfnx import QueryEvaluationError, context, jsonata, state_machine, task, "
        "wait"
    )
    compiled = definition(body, imports)["States"]
    assert compiled["r"]["Assign"] == {"r": "{% $states.result %}"}
    assert "End" not in compiled["r"]


@pytest.mark.parametrize(
    "body, output",
    [
        (R + ', retry=[{"ErrorEquals": [Exception]}])\nreturn None', None),
        (R + ', retry=[{"ErrorEquals": [Exception]}])', None),
        (
            (
                f"try:\n    {R})\n    return {{'ok': True, 'tags': ['a']}}\n"
                "except Exception:\n    return 0"
            ),
            {"ok": True, "tags": ["a"]},
        ),
    ],
)
def test_a_written_return_is_the_output_of_any_task(body, output):
    """A value written in the source cannot fail, so the Output holds it even
    where a Catch or a retrier would take a failure of the Output."""
    state = definition(body, "from sfnx import state_machine, task")["States"]["r"]
    assert (state["Output"], state["End"]) == (output, True)


def test_a_retrier_for_task_errors_leaves_the_return_to_the_task():
    """States.TaskFailed does not match a failing Output (measured)."""
    body = R + ', retry=[{"ErrorEquals": [TaskFailed]}])\nreturn r["Payload"]'
    state = definition(body, "from sfnx import TaskFailed, state_machine, task")[
        "States"
    ]["r"]
    assert (state["Output"], state["End"]) == ("{% $states.result.Payload %}", True)
    assert "Assign" not in state


def test_assignments_right_after_a_task_go_in_its_assign():
    """They read the result as $states.result, the variables the Task
    assigns as what they take and the others as they were before it."""
    body = (
        'fee: float = input["fee"]\n'
        f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\n'
        'total: float = r["Payload"]["total"]\n'
        "due = total + fee\n"
        "wait(1)\n"
        "return [r, total, due]"
    )
    compiled = states(body)
    assert list(compiled) == ["r", "wait"]
    assert compiled["r"]["Assign"] == {
        "fee": f"{{% {INPUT}.fee %}}",
        "r": "{% $states.result %}",
        "total": "{% $states.result.Payload.total %}",
        "due": f"{{% $states.result.Payload.total + {INPUT}.fee %}}",
    }
    tasks = {"r": lambda arguments: {"Payload": {"total": 2}}}
    assert asl.run(definition(body), {"fee": 1}, tasks) == [
        {"Payload": {"total": 2}},
        2,
        3,
    ]


def test_a_return_after_them_is_the_output():
    body = R + ')\nx = r["Payload"]\nreturn x["a"]'
    state = states(body)["r"]
    assert (state["Output"], state["End"]) == ("{% $states.result.Payload.a %}", True)
    assert "Assign" not in state


def test_an_assignment_after_a_task_on_its_own_line_goes_in_its_assign():
    body = f'task("{LAMBDA}", {{"FunctionName": "f"}})\nn = 1\nwait(n)\nreturn n'
    assert states(body)["invoke"]["Assign"] == {"n": 1}


def test_the_variable_the_task_assigns_can_be_assigned_again():
    body = R + ')\nr = r["Payload"]\nwait(1)\nreturn r'
    assert states(body)["r"]["Assign"] == {"r": "{% $states.result.Payload %}"}


@pytest.mark.parametrize(
    "body",
    [
        R
        + ', retry=[{"ErrorEquals": [Exception]}])\nn = r["Payload"]\nwait(1)\nreturn n',
        R + ')\nn = jsonata("$random()")\nwait(1)\nreturn [r, n]',
        R + ')\na, b = r["Payload"]\nwait(1)\nreturn [a, b]',
    ],
)
def test_an_assignment_that_could_differ_keeps_its_pass(body):
    imports = "from sfnx import jsonata, state_machine, task, wait"
    compiled = definition(body, imports)["States"]
    assert compiled["r"]["Assign"] == {"r": "{% $states.result %}"}
    assert any(state["Type"] == "Pass" for state in compiled.values())


@pytest.mark.parametrize(
    "value, code",
    [("str(uuid.uuid4())", "$uuid()"), ("time.time()", "$millis() / 1000")],
)
def test_the_time_and_a_random_value_go_in_the_assign_of_a_task(value, code):
    """A Task's Assign and Output run when it ends (measured), where Python
    reads them after the call."""
    body = R + f")\nn = {value}\nwait(1)\nreturn [r, n]"
    compiled = definition(
        body, "import time\nimport uuid\nfrom sfnx import state_machine, task, wait"
    )["States"]
    assert compiled["r"]["Assign"] == {
        "r": "{% $states.result %}",
        "n": f"{{% {code} %}}",
    }


@pytest.mark.parametrize(
    "body",
    [
        R + ")\nn = str(uuid.uuid4())\nreturn [n, n]",
        R + ')\nn = str(uuid.uuid4())\nreturn {"a": n, "b": [n]}',
        R + ")\nn = str(uuid.uuid4())\npair = [n, n]\nreturn pair",
        R + ")\nn = str(datetime.now())\nreturn [n, n]",
        R + ")\nn = str(uuid.uuid4())\nreturn pair(n)",
    ],
)
def test_a_value_that_changes_read_twice_after_a_task_is_read_as_its_variable(body):
    """Each reading of the expression would evaluate it again, so what reads
    the value more than once reads the variable the Task's Assign gives it."""
    imports = (
        "import uuid\nfrom datetime import datetime\n\n"
        "from sfnx import state_machine, task\n\n\n"
        "def pair(t):\n    return [t, t]"
    )
    compiled = definition(body, imports)
    assert "n" in compiled["States"]["r"]["Assign"]
    tasks = {"r": lambda arguments: {}}
    result = asl.run(compiled, {}, tasks)
    first, *others = result.values() if isinstance(result, dict) else result
    assert others in ([first], [[first]])


def test_a_value_that_changes_read_once_after_a_task_is_its_output():
    body = R + ')\nn = str(uuid.uuid4())\nreturn {"n": n}'
    compiled = definition(body, "import uuid\n\nfrom sfnx import state_machine, task")
    assert compiled["States"]["r"]["Output"] == {"n": "{% $uuid() %}"}


def test_a_loop_tried_again_after_a_task_leaves_the_task_as_it_was():
    """The loop widens the type of r and compiles again from the Task, whose
    Assign takes the loop's start once."""
    compiled = states(R + ')\nfor i in range(3):\n    r = "s"\nreturn r')
    assert compiled["r"]["Assign"] == {"r": "{% $states.result %}", "i": 0}
    assert compiled["r"]["Next"] == "for"
    assert compiled["return"] == {"Type": "Succeed", "Output": "{% $r %}"}


def test_the_comments_of_the_task_and_the_return_join():
    body = f"# charge\n{R})\n# the receipt\nreturn r"
    assert states(body)["r"]["Comment"] == "charge\nthe receipt"


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
    assert list(compiled) == [name]
    assert (compiled[name]["Output"], compiled[name]["End"]) == (1, True)
    assert "Assign" not in compiled[name]


def test_the_result_can_be_taken_apart_in_the_assignment():
    compiled = states(
        f'amount = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]["amount"]\nwait(1)\nreturn amount'
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


def test_assignments_that_start_the_machine_go_in_the_first_task():
    """The Task reads each as its expression and assigns it, reading what a
    Pass before it would."""
    compiled = states(
        f'fee = 10\nname = "f"\nr = task("{LAMBDA}", {{"FunctionName": name}})\nreturn [fee, r]'
    )
    assert list(compiled) == ["r"]
    assert compiled["r"]["Arguments"] == {"FunctionName": "f"}
    assert compiled["r"]["Assign"] == {"fee": 10, "name": "f"}
    assert compiled["r"]["Output"] == [10, "{% $states.result %}"]


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
            "$count($keys($states.result.Item))",
        ),
        (
            f'page = task("{QUERY}", {{"TableName": "t"}})\nreturn page["Count"] + input["extra"]',
            f"$states.result.Count + {INPUT}.extra",
        ),
        (
            f'page = task("{QUERY}", {{"TableName": "t"}})\nreturn len(page["Items"])',
            "$count($states.result.Items)",
        ),
        (
            'r = task("arn:aws:states:::http:invoke", {"ApiEndpoint": "https://e", "Method": "GET", "Authentication": {"ConnectionArn": "c"}})\nreturn len(r["Headers"])',
            "$count($keys($states.result.Headers))",
        ),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\nreturn r["StatusCode"] + input["x"]',
            f"$states.result.StatusCode + {INPUT}.x",
        ),
        (
            f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\ntotal: float = r["Payload"]["total"]\nreturn total + input["x"]',
            f"$states.result.Payload.total + {INPUT}.x",
        ),
    ],
)
def test_result_types(body, output):
    """A return right after the Task is its Output, so the result is read
    there as $states.result."""
    [end] = [s for s in states(body).values() if s["Type"] == "Succeed" or s.get("End")]
    assert end["Output"] == f"{{% {output} %}}"


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
    assert states(f'task("{resource}", {arguments})\nreturn 1')


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
            'r = task("arn:aws:states:::aws-sdk:rds:describeDBInstances", {"DBInstanceIdentifier": "d"})',
            "describeDBInstances has no argument DBInstanceIdentifier; did you mean DbInstanceIdentifier?",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:dynamodb:putItem", {"TableName": "t", "Item": {"k": {"BOOL": True}}})',
            "putItem has no field BOOL here; did you mean Bool?",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:ecs:runTask", {"TaskDefinition": "t", "NetworkConfiguration": {"awsvpcConfiguration": {"Subnets": ["s"]}}})',
            "runTask has no field awsvpcConfiguration here; did you mean AwsvpcConfiguration?",
        ),
        (
            'r = task("arn:aws:states:::aws-sdk:ec2:describeInstances", {"Filters": [{**input["filter"], "Valuez": ["v"]}]})',
            "describeInstances has no field Valuez here; did you mean Values?",
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
            f'r = task("{PUBLISH}", {{"Message": "m"}}, role=None)',
            "role is the ARN of an IAM role, not a null",
        ),
        (
            f'r = task("{PUBLISH}", {{"Message": "m"}}, role=1)',
            "role is the ARN of an IAM role, not a number",
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
        f"{{% $merge([{unpacked(f'{INPUT}.key')}, {{'TableName': 't'}}]) %}}"
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


@pytest.mark.parametrize("end", ["", "return\n", "return None\n"])
def test_a_task_before_a_return_without_a_value_ends_the_machine(end):
    body = f'task("{LAMBDA}", {{"FunctionName": "f"}})\n{end}'
    assert states(body) == {
        "invoke": {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": {"FunctionName": "f"},
            "Output": None,
            "End": True,
        }
    }


def test_a_branch_and_a_processor_end_on_their_last_state():
    body = f"""
def left():
    task("{LAMBDA}", {{"FunctionName": "left"}})

def each(x):
    task("{LAMBDA}", {{"FunctionName": "each", "Payload": x}})

parallel(left)
inline_map(each, input["items"])
"""
    compiled = definition(
        body, "from sfnx import inline_map, parallel, state_machine, task"
    )["States"]
    assert list(compiled) == ["parallel", "map"]
    assert compiled["map"]["End"] is True
    branch = compiled["parallel"]["Branches"][0]["States"]
    processor = compiled["map"]["ItemProcessor"]["States"]
    assert list(branch) == ["left.invoke"] and branch["left.invoke"]["End"] is True
    assert list(processor)[-1] == "each.invoke"
    assert processor["each.invoke"]["End"] is True


def test_a_task_that_catches_ends_itself_and_its_catcher_at_the_succeed():
    # The return after the try is a value written in the source, which the
    # Task's Output cannot fail on; the catcher still needs the Succeed.
    body = f"""
try:
    task("{LAMBDA}", {{"FunctionName": "f"}})
except Exception:
    pass
"""
    compiled = states(body)
    assert compiled["invoke"]["Output"] is None and compiled["invoke"]["End"] is True
    assert compiled["invoke"]["Catch"][0]["Next"] == "return"
    assert compiled["return"] == {"Type": "Succeed", "Output": None}


def test_a_task_an_if_runs_ends_on_the_return_after_the_if():
    body = f'if input["a"]:\n    task("{LAMBDA}", {{"FunctionName": "f"}})\n# done\nreturn {{"ok": True}}'
    compiled = definition(body)
    task_state = compiled["States"]["invoke"]
    assert task_state["Output"] == {"ok": True} and task_state["End"] is True
    assert task_state["Comment"] == "done"
    assert compiled["States"]["if"]["Default"] == "return"
    for a in (True, False):
        tasks = {"invoke": lambda arguments: {}}
        assert asl.run(compiled, {"a": a}, tasks) == {"ok": True}


def test_a_return_that_reads_a_value_keeps_the_task_going_on_to_it():
    # Moved into the Task, a failing Output would be the Catch's to take.
    body = f'try:\n    task("{LAMBDA}", {{"FunctionName": "f"}})\nexcept Exception:\n    pass\nreturn input["r"]'
    compiled = states(body)
    assert compiled["invoke"]["Next"] == "return" and "Output" not in compiled["invoke"]


@pytest.mark.parametrize(
    "body",
    [
        # A Catch would take a failure of the Task's Assign.
        f'fee = input["fee"]\ntry:\n    task("{LAMBDA}", {{"FunctionName": "f"}})\nexcept Exception:\n    pass\nreturn fee',
        # The loop leads back to its Choice, which would assign them again.
        'fee = input["fee"]\nwhile fee > input["cap"]:\n    fee = fee - 1\nreturn fee',
        # The Choice binds the name, which would take over the expression.
        'fee = input["fee"]\nxs: list = input["xs"]\nif [fee + 1 for fee in xs]:\n    return fee\nreturn 0',
    ],
)
def test_assignments_that_start_the_machine_keep_their_pass(body):
    compiled = definition(body, "from sfnx import state_machine, task")
    first = compiled["States"][compiled["StartAt"]]
    assert first["Type"] == "Pass"
