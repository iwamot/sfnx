import json
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
    "value, code",
    [
        ('jsonata("$random()")', "$random()"),
        ("time.time()", "$millis() / 1000"),
    ],
)
def test_a_return_that_changes_on_evaluation_is_the_output_after_a_task(value, code):
    """The Output is evaluated once, after the call, as the return's state
    after the Task would be."""
    body = R + f")\nreturn [r, {value}]"
    imports = "import time\nfrom sfnx import jsonata, state_machine, task"
    assert definition(body, imports)["States"] == {
        "r": {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": {"FunctionName": "f"},
            "Output": ["{% $states.result %}", f"{{% {code} %}}"],
            "End": True,
        }
    }


@pytest.mark.parametrize(
    "body",
    [
        # A retrier for these errors would run the Task again.
        R + ', retry=[{"ErrorEquals": [Exception]}])\nreturn r["Payload"]',
        R + ', retry=[{"ErrorEquals": [QueryEvaluationError]}])\nreturn r["Payload"]',
        # What could differ between the Task and the state after it.
        R + ')\nreturn [r, context["State"]["Name"]]',
        # Another state comes between.
        R + ")\nwait(1)\nreturn r",
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
    assert states(body)["invoke f"]["Assign"] == {"n": 1}


def test_the_variable_the_task_assigns_can_be_assigned_again():
    body = R + ')\nr = r["Payload"]\nwait(1)\nreturn r'
    assert states(body)["r"]["Assign"] == {"r": "{% $states.result.Payload %}"}


@pytest.mark.parametrize(
    "body",
    [
        R
        + ', retry=[{"ErrorEquals": [Exception]}])\nn = r["Payload"]\nwait(1)\nreturn n',
    ],
)
def test_an_assignment_that_could_differ_keeps_its_pass(body):
    imports = "from sfnx import jsonata, state_machine, task, wait"
    compiled = definition(body, imports)["States"]
    assert compiled["r"]["Assign"] == {"r": "{% $states.result %}"}
    assert any(state["Type"] == "Pass" for state in compiled.values())


def test_an_unpacking_after_a_task_reads_its_result_in_its_assign():
    """The Task's own Assign reads its result as $states.result, where the
    unpacking's Pass would read the variable the Assign gives it."""
    body = R + ')\na, b = r["Payload"]\nwait(1)\nreturn [a, b]'
    compiled = states(body)
    assert compiled["r"]["Assign"] == {
        "a": "{% $states.result.Payload[0] %}",
        "b": "{% $states.result.Payload[1] %}",
    }
    tasks = {"r": lambda arguments: {"Payload": [1, 2]}}
    assert asl.run(definition(body), {}, tasks) == [1, 2]


def test_a_random_value_read_for_each_item_after_a_task_is_evaluated_once():
    """Python evaluates random.random() once, before the comprehension; each
    item reads that value."""
    body = f'r: list[int] = task("{LAMBDA}", {{"FunctionName": "f"}})\nx = random.random()\nreturn [x + i for i in r]'
    compiled = definition(body, "import random\nfrom sfnx import state_machine, task")
    first, second, third = asl.run(compiled, {}, {"r": lambda arguments: [0, 0, 0]})
    assert first == second == third


def test_a_value_that_changes_the_arguments_read_once_goes_in_them():
    body = f'x = random.random()\ny = task("{LAMBDA}", {{"FunctionName": "f", "Payload": x}})\nreturn y'
    compiled = definition(body, "import random\nfrom sfnx import state_machine, task")
    assert compiled["States"] == {
        "y": {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": {"FunctionName": "f", "Payload": "{% $random() %}"},
            "End": True,
        }
    }


@pytest.mark.xfail(
    strict=True,
    reason="example c: the Arguments evaluate the same expression first, but it "
    "may be undefined, and what an undefined field of the Arguments does is "
    "not measured yet",
)
def test_an_assignment_the_arguments_evaluate_first_goes():
    body = f'x = input["a"]\ny = task("{LAMBDA}", {{"FunctionName": "f", "Payload": x}})\nreturn y'
    assert states(body) == {
        "y": {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": {"FunctionName": "f", "Payload": f"{{% {INPUT}.a %}}"},
            "End": True,
        }
    }


@pytest.mark.parametrize(
    "value, payload",
    [
        # Never undefined, and read whole as a field of the Arguments, which
        # evaluate it before the Assign would, with the same variables.
        (
            'input.get("a", 1) * 2',
            f"{{% ($exists({INPUT}.a) ? {INPUT}.a : 1) * 2 %}}",
        ),
        # Nothing in it can fail or be undefined.
        (
            'str(input.get("a", 1))',
            f"{{% $string($exists({INPUT}.a) ? {INPUT}.a : 1) %}}",
        ),
    ],
)
def test_an_assignment_whose_failure_the_arguments_meet_first_goes(value, payload):
    body = f'x = {value}\ny = task("{LAMBDA}", {{"FunctionName": "f", "Payload": x}})\nreturn y'
    assert states(body) == {
        "y": {
            "Type": "Task",
            "Resource": LAMBDA,
            "Arguments": {"FunctionName": "f", "Payload": payload},
            "End": True,
        }
    }


def test_the_fields_of_a_state_read_the_variables_from_before_it():
    """What failure_seen_before relies on: the Arguments, the Assign and the
    Output of a Task all read a variable as it was before the state, however
    its Assign assigns it again."""
    machine = {
        "QueryLanguage": "JSONata",
        "StartAt": "v",
        "States": {
            "v": {"Type": "Pass", "Assign": {"v": 1}, "Next": "t"},
            "t": {
                "Type": "Task",
                "Resource": LAMBDA,
                "Arguments": {"FunctionName": "f", "Payload": "{% $v %}"},
                "Assign": {"v": 2, "w": "{% $v %}"},
                "Output": "{% $v %}",
                "Next": "r",
            },
            "r": {"Type": "Succeed", "Output": "{% [$states.input, $v, $w] %}"},
        },
    }
    sent = []
    tasks = {"t": lambda arguments: sent.append(arguments["Payload"])}
    assert asl.run(machine, {}, tasks) == [1, 2, 1]
    assert sent == [1]


def test_a_value_that_changes_the_arguments_read_twice_keeps_its_pass():
    """Each reading in the Arguments would evaluate it again."""
    body = f'x = random.random()\ntask("{LAMBDA}", {{"FunctionName": "f", "Payload": [x, x]}})'
    compiled = definition(body, "import random\nfrom sfnx import state_machine, task")
    assert compiled["States"]["x"] == {
        "Type": "Pass",
        "Assign": {"x": "{% $random() %}"},
        "Next": "invoke f",
    }


def reading(states: dict, code: str) -> set[tuple[str, str]]:
    """The fields of the states whose expressions hold code."""
    return {
        (name, field)
        for name, state in states.items()
        for field, value in state.items()
        if code in json.dumps(value)
    }


@pytest.mark.parametrize(
    "body, allowed",
    [
        # Read before the call: in a Pass before it, or in its Arguments; not
        # in its Assign or Output, or a state after it, which run after it.
        (
            (
                f't = str(datetime.now())\nr = task("{LAMBDA}", {{"FunctionName": "f"}})\n'
                "return [r, t]"
            ),
            {("t", "Assign"), ("r", "Arguments")},
        ),
        # Read between two calls: after the first ends, in its Assign or
        # Output, and before the second, in its Arguments; not after it.
        (
            (
                f'r = task("{LAMBDA}", {{"FunctionName": "f"}})\nt = str(datetime.now())\n'
                f's = task("{LAMBDA}", {{"FunctionName": "g", "Payload": t}})\nreturn s'
            ),
            {("r", "Assign"), ("r", "Output"), ("s", "Arguments")},
        ),
    ],
)
def test_the_time_stays_between_the_calls_it_is_read_between(body, allowed):
    imports = "from datetime import datetime\n\nfrom sfnx import state_machine, task"
    assert reading(definition(body, imports)["States"], "$now()") <= allowed


def test_a_random_value_a_jsonata_expression_calls_goes_in_the_assign():
    """The Task's Assign runs when it ends (measured), where Python calls
    $random() after the call, and the syntax tree of the text says it calls
    nothing else."""
    body = R + ')\nn = jsonata("$random()")\nwait(1)\nreturn [r, n]'
    imports = "from sfnx import jsonata, state_machine, task, wait"
    compiled = definition(body, imports)["States"]
    assert compiled["r"]["Assign"] == {
        "r": "{% $states.result %}",
        "n": "{% $random() %}",
    }
    assert not any(state["Type"] == "Pass" for state in compiled.values())


def test_a_start_value_a_way_assigns_again_is_read_only_on_the_others():
    """The Choice rule of the if assigns n too, so nothing reads the value
    from the input on that way, and it goes in the Choice: a hand-writer
    reads the input where it is used rather than checking it first. A
    missing key fails only on the way that reads it, which the table of
    differences lists."""
    body = (
        'n = input["n"]\nif input["big"]:\n    n = 10\n'
        f'    task("{LAMBDA}", {{"FunctionName": "f"}})\n    return n\nreturn n'
    )
    compiled = definition(body)
    assert compiled["States"][compiled["StartAt"]]["Type"] == "Choice"
    tasks = {n: lambda arguments: {"Payload": 5} for n in compiled["States"]}
    assert asl.run(compiled, {"big": True}, tasks) == 10
    with pytest.raises(asl.Failure):
        asl.run(compiled, {"big": False}, tasks)
    assert asl.run(compiled, {"n": 1, "big": False}, tasks) == 1


def test_a_start_value_the_task_replaces_before_anything_reads_it_goes():
    """The Task assigns n its result before anything reads the value from
    the input, so a hand-writer would not read it: a missing key fails
    nowhere, which the table of differences lists."""
    body = (
        f'n = input["n"]\nn = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]\n'
        f'task("{LAMBDA}", {{"FunctionName": "g"}})\nreturn n'
    )
    compiled = definition(body)
    tasks = {n: lambda arguments: {"Payload": 5} for n in compiled["States"]}
    assert [s["Type"] for s in compiled["States"].values()] == ["Task", "Task"]
    assert asl.run(compiled, {}, tasks) == 5


@pytest.mark.parametrize(
    "rest, expected",
    [
        # The swap reads n0 as the Task assigned it, not as 2.
        ("n0, n1 = n1, n0\nreturn [n0, n1]", [2, 7]),
        ('n1, n2 = input["x"], 5\nreturn [n0, n1, n2]', [7, 9, 5]),
    ],
)
def test_an_unpacking_after_a_task_replaces_what_an_earlier_one_assigned(
    rest, expected
):
    """n1 = 2 goes in the Task's Assign; the unpacking assigns n1 again, and
    its value replaces that one there."""
    body = f'n0: int = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]\nn1 = 2\n{rest}'
    compiled = definition(body)
    tasks = {"n0": lambda arguments: {"Payload": 7}}
    assert asl.run(compiled, {"x": 9}, tasks) == expected


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
        # The function of a comprehension reads it once for each item.
        R + ")\nn = str(uuid.uuid4())\nreturn [n for i in [1, 2]]",
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
    Assign takes the loop's start once, and the first round, which the start
    decides to take, once too."""
    compiled = states(R + ')\nfor i in range(3):\n    r = "s"\nreturn r')
    assert compiled["r"]["Assign"] == {"r": "s", "i": 1}
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
        (f'task("{LAMBDA}", {{"FunctionName": "f"}})', "invoke f"),
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


# A Lambda invoke, a DynamoDB putItem and an SNS publish on a line of their
# own are named after what they call too, where the definition holds it as
# written: the function, the table or the topic, out of an ARN.
@pytest.mark.parametrize(
    "imports, module, call, name",
    [
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.optimized.dynamodb.put_item(TableName="orders", Item={})',
            "putItem orders",
        ),
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.sdk.dynamodb.put_item(TableName="arn:aws:dynamodb:us-east-1:123456789012:table/orders", Item={})',
            "putItem orders",
        ),
        (
            "from sfnx import aws, state_machine",
            'TOPIC = "arn:aws:sns:us-east-1:123456789012:alerts"\n',
            'aws.optimized.sns.publish(TopicArn=TOPIC, Message="m")',
            "publish alerts",
        ),
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.optimized.lambda_.invoke(FunctionName="arn:aws:lambda:us-east-1:123456789012:function:charge:live")',
            "invoke charge",
        ),
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.optimized.lambda_.invoke(FunctionName="123456789012:function:charge")',
            "invoke charge",
        ),
        # A value read at run time, or filled in at deployment, names nothing.
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.optimized.lambda_.invoke(FunctionName=input["f"])',
            "invoke",
        ),
        (
            "from sfnx import aws, state_machine",
            "",
            'aws.optimized.lambda_.invoke(FunctionName="${ChargeFunctionArn}")',
            "invoke",
        ),
        # A name past 80 characters is the action alone.
        (
            "from sfnx import aws, state_machine",
            "",
            f'aws.optimized.lambda_.invoke(FunctionName="{"f" * 80}")',
            "invoke",
        ),
    ],
)
def test_a_task_on_its_own_line_is_named_after_what_it_calls(
    imports, module, call, name
):
    (compiled,) = compile_source(
        f"{imports}\n{module}\n\n@state_machine\ndef pay(input):\n    {call}\n"
        "    return 1\n"
    ).values()
    assert list(compiled["States"]) == [name]


@pytest.mark.parametrize(
    "imports, body, length, names",
    [
        # "invoke " and 73 characters make 80; the serial would make 82.
        (
            "from sfnx import aws, state_machine",
            "    if input['a']:\n        CALL\n        return 1\n    CALL\n    return 2\n",
            73,
            ["invoke " + "f" * 73, "invoke"],
        ),
        # In a branch, "f." takes two of the 80.
        (
            "from sfnx import aws, parallel, state_machine",
            (
                "    def f():\n        if input['a']:\n            CALL\n            return 1\n"
                "        CALL\n        return 2\n\n    return parallel(f)\n"
            ),
            71,
            ["f.invoke " + "f" * 71, "f.invoke"],
        ),
    ],
)
def test_a_serial_past_80_characters_leaves_the_action_alone(
    imports, body, length, names
):
    target = "f" * length
    call = f'aws.optimized.lambda_.invoke(FunctionName="{target}")'
    (compiled,) = compile_source(
        f"{imports}\n\n\n@state_machine\ndef pay(input):\n" + body.replace("CALL", call)
    ).values()
    found = []

    def tasks(scope: dict) -> None:
        for name, state in scope["States"].items():
            if state["Type"] == "Task":
                found.append(name)
            for branch in state.get("Branches", []):
                tasks(branch)

    tasks(compiled)
    assert found == names
    assert all(len(name) <= 80 for name in found)


def test_two_calls_of_the_same_target_take_serials():
    call = 'aws.optimized.lambda_.invoke(FunctionName="return-expense")'
    (compiled,) = compile_source(
        "from sfnx import aws, state_machine\n\n\n@state_machine\n"
        f'def pay(input):\n    if input["a"]:\n        {call}\n'
        f"        return 1\n    {call}\n    return 2\n"
    ).values()
    names = [n for n, s in compiled["States"].items() if s["Type"] == "Task"]
    assert names == ["invoke return-expense", "invoke return-expense_2"]


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
    """The Task reads each as its expression, reading what a Pass before it
    would; nothing reads them after it, so it assigns none of them."""
    compiled = states(
        f'fee = 10\nname = "f"\nr = task("{LAMBDA}", {{"FunctionName": name}})\nreturn [fee, r]'
    )
    assert list(compiled) == ["r"]
    assert compiled["r"]["Arguments"] == {"FunctionName": "f"}
    assert "Assign" not in compiled["r"]
    assert compiled["r"]["Output"] == [10, "{% $states.result %}"]


FAN_OUT = "from sfnx import inline_map, parallel, state_machine, task, wait"


@pytest.mark.parametrize(
    "call, first",
    [
        ("wait(fee)", "wait"),
        ("r = parallel(one, one)", "r"),
        ("r = inline_map(double, [fee, 2])", "r"),
    ],
)
def test_certain_assignments_that_start_the_machine_go_in_a_wait_parallel_or_map(
    call, first
):
    """None of them can fail, so none fails after the state has run; the
    branches and the processor read the values as written."""
    body = (
        "def one():\n    return fee\n\ndef double(x):\n    return x * fee\n\n"
        f'fee = 2\nname = "f"\n{call}\nreturn [fee, name]'
    )
    compiled = definition(body, FAN_OUT)
    assert compiled["StartAt"] == first
    state = compiled["States"][first]
    # The state and its return read them as written, so none is assigned.
    assert "Assign" not in state
    assert "$fee" not in json.dumps(compiled)
    assert asl.run(compiled, {}, {}) == [2, "f"]


@pytest.mark.parametrize(
    "call",
    [
        f'    task("{LAMBDA}", {{"FunctionName": name}})\n',
        f'    task("{LAMBDA}", {{"FunctionName": name}}, retry=[{{"ErrorEquals": [Exception]}}])\n',
    ],
)
def test_certain_assignments_that_start_the_machine_go_in_a_catching_task(call):
    """None of them can fail, so neither the Catch nor a retrier has a
    failure of the Task's Assign to take, and the catcher assigns them too."""
    body = f'fee = 2\nname = "f"\ntry:\n{call}except Exception:\n    return fee\nreturn name'
    compiled = definition(body)
    state = compiled["States"][compiled["StartAt"]]
    assert state["Type"] == "Task"
    assert state["Arguments"] == {"FunctionName": "f"}
    # The return after the try reads name as written; only the way from the
    # catcher reads fee.
    assert "Assign" not in state
    [catcher] = state["Catch"]
    assert catcher["Assign"] == {"fee": 2}


def test_certain_assignments_that_start_the_machine_go_in_the_catchers_too():
    body = (
        "def one():\n    return 1\n\n"
        "fee = 2\ntry:\n    parallel(one, one)\nexcept Exception:\n    return fee\nreturn fee"
    )
    compiled = definition(body, FAN_OUT)
    state = compiled["States"][compiled["StartAt"]]
    assert state["Type"] == "Parallel" and state["Output"] == 2
    [catcher] = state["Catch"]
    assert catcher["Assign"]["fee"] == 2


CATCHING_PARALLEL = (
    'status = "old"\n'
    'try:\n    r = parallel(ok)\n    y = r[0]["missing"]\n    status = "new"\n'
    "    return y\nexcept Exception:\n    return status"
)


@pytest.mark.parametrize(
    "body, output",
    [
        ('def ok():\n    return {"k": 1}\n\n' + CATCHING_PARALLEL, "old"),
        (
            'def ok():\n    return {"k": 1}\n\ndef branch():\n'
            + textwrap.indent(CATCHING_PARALLEL, "    ")
            + "\n\nreturn parallel(branch)",
            ["old"],
        ),
    ],
)
def test_certain_start_assignments_leave_the_try_body_to_the_catching_parallel(
    body, output
):
    """The statements after the Parallel in the try go in its Assign, whose
    failure its Catch takes: the values that start the machine or a branch,
    which its catcher assigns too, are not among what Python assigned before
    the statement that fails."""
    compiled = definition(body, FAN_OUT)
    assert '"Type": "Pass"' not in json.dumps(compiled)
    assert asl.run(compiled, {}) == output


def test_assignments_that_start_the_machine_keep_their_pass_before_a_parallel():
    # The branches run before the Parallel's Assign, and only a value
    # written in the source reads the same in a branch or a child execution
    # of a distributed map.
    body = "def one():\n    return fee\n\nfee = input\nr = parallel(one, one)\nreturn r"
    compiled = definition(body, FAN_OUT)
    assert compiled["States"][compiled["StartAt"]]["Type"] == "Pass"


def test_a_wait_takes_assignments_that_start_the_machine_and_may_fail():
    """A hand-writer spends no state to check the input first: a key the
    input lacks fails when the wait is over, where Python fails before it,
    which the table of differences lists."""
    compiled = definition('fee = input["fee"]\nwait(1)\nreturn fee', FAN_OUT)
    assert list(compiled["States"]) == ["wait"]
    assert asl.run(compiled, {"fee": 3}) == 3
    with pytest.raises(asl.Failure):
        asl.run(compiled, {})


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


@pytest.mark.parametrize("end", ["", "return\n", "return None\n"])
def test_a_task_before_a_return_without_a_value_ends_the_machine(end):
    body = f'task("{LAMBDA}", {{"FunctionName": "f"}})\n{end}'
    assert states(body) == {
        "invoke f": {
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
    assert list(branch) == ["left.invoke left"]
    assert branch["left.invoke left"]["End"] is True
    assert list(processor)[-1] == "each.invoke each"
    assert processor["each.invoke each"]["End"] is True


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
    assert (
        compiled["invoke f"]["Output"] is None and compiled["invoke f"]["End"] is True
    )
    assert compiled["invoke f"]["Catch"][0]["Next"] == "return"
    assert compiled["return"] == {"Type": "Succeed", "Output": None}


def test_a_task_an_if_runs_ends_on_the_return_after_the_if():
    body = f'if input["a"]:\n    task("{LAMBDA}", {{"FunctionName": "f"}})\n# done\nreturn {{"ok": True}}'
    compiled = definition(body)
    task_state = compiled["States"]["invoke f"]
    assert task_state["Output"] == {"ok": True} and task_state["End"] is True
    assert task_state["Comment"] == "done"
    assert compiled["States"]["if"]["Default"] == "return"
    for a in (True, False):
        tasks = {"invoke f": lambda arguments: {}}
        assert asl.run(compiled, {"a": a}, tasks) == {"ok": True}


def test_a_return_that_reads_a_value_keeps_the_task_going_on_to_it():
    # Moved into the Task, a failing Output would be the Catch's to take.
    body = f'try:\n    task("{LAMBDA}", {{"FunctionName": "f"}})\nexcept Exception:\n    pass\nreturn input["r"]'
    compiled = states(body)
    assert (
        compiled["invoke f"]["Next"] == "return"
        and "Output" not in compiled["invoke f"]
    )


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


@pytest.mark.parametrize(
    "returned, output",
    [
        ("r", "{% $states.result %}"),
        # The Output reads what the Task assigns as the expression it
        # assigns, as it is evaluated with the values from before the Task.
        ('[r["Payload"], 1]', ["{% $states.result.Payload %}", 1]),
    ],
)
def test_each_task_before_a_return_several_paths_share_ends_with_it(returned, output):
    body = (
        f"if input['a']:\n    {R})\nelse:\n    r = task(\"{LAMBDA}\", "
        f'{{"FunctionName": "g"}})\nreturn {returned}'
    )
    compiled = states(body)
    tasks = [s for s in compiled.values() if s["Type"] == "Task"]
    assert len(tasks) == 2 and all(t["Output"] == output and t["End"] for t in tasks)
    assert "return" not in compiled


def test_a_task_before_a_shared_return_drops_what_nothing_reads():
    """Nothing reads n, so the Task does not assign it, and it ends with the
    return the other way shares."""
    body = (
        f"if input['a']:\n    {R})\n    n = r['Payload'] + 1\nelse:\n"
        f'    r = task("{LAMBDA}", {{"FunctionName": "g"}})\nreturn r'
    )
    compiled = states(body)
    [first] = [
        s
        for s in compiled.values()
        if s.get("Arguments", {}).get("FunctionName") == "f"
    ]
    assert "n" not in first.get("Assign", {}) and first.get("End")


@pytest.mark.parametrize(
    "after, returned",
    [
        # A dict holding an expression is no one expression to read d as.
        ("\n    d = {'a': r['Payload']}", "d"),
        # The text reads r and binds it too, which the value would take over.
        ("", 'jsonata("[$r, ($r := 2; $r)]")'),
    ],
)
def test_a_task_before_a_shared_return_it_cannot_read_keeps_going_on(after, returned):
    body = (
        f"if input['a']:\n    {R}){after}\nelse:\n"
        f'    r = task("{LAMBDA}", {{"FunctionName": "g"}})\n    d = r\n'
        f"return {returned}"
    )
    (compiled,) = compile_source(
        source(body, "from sfnx import jsonata, state_machine, task")
    ).values()
    [first] = [
        s
        for s in compiled["States"].values()
        if s.get("Arguments", {}).get("FunctionName") == "f"
    ]
    assert "End" not in first


def test_the_start_takes_in_what_the_first_round_of_a_loop_assigns():
    """The loop's only round goes in the way in, and so in the start Pass,
    which then goes in the Task after it as the Pass is when it goes."""
    body = (
        'n = 2\ns: str = input["s"]\nfor x in [0]:\n    s = f"{s}-{n}"\n'
        f'r = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]\nreturn [r, s]'
    )
    compiled = definition(body)
    assert [s["Type"] for s in compiled["States"].values()] == ["Task"]
    [task] = compiled["States"]
    assert asl.run(compiled, {"s": "a"}, {task: lambda a: {"Payload": 1}}) == [1, "a-2"]
