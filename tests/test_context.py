import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError

SQS = "arn:aws:states:::sqs:sendMessage.waitForTaskToken"


def output(
    body: str, imports: str = "from sfnx import context, state_machine, task"
) -> object:
    source = f"{imports}\n\n\n@state_machine\ndef pay(input):\n" + textwrap.indent(
        body, "    "
    )
    (compiled,) = compile_source(source).values()
    return compiled["States"]


@pytest.mark.parametrize(
    "expression, code",
    [
        ('context["Execution"]["Id"]', "$states.context.Execution.Id"),
        ('context["Execution"]["Input"]', "$states.context.Execution.Input"),
        (
            'context["Execution"]["RedriveTime"]',
            "$states.context.Execution.RedriveTime",
        ),
        ('context["State"]["RetryCount"] + 1', "$states.context.State.RetryCount + 1"),
        (
            'context["StateMachine"]["Name"] + "!"',
            "$states.context.StateMachine.Name & '!'",
        ),
        ("context", "$states.context"),
        (
            'context["Execution"].get("RedriveTime")',
            (
                "$exists($states.context.Execution.RedriveTime) ? "
                "$states.context.Execution.RedriveTime : null"
            ),
        ),
    ],
)
def test_context_reads_states_context(expression, code):
    assert output(f"return {expression}")["return"]["Output"] == f"{{% {code} %}}"


def test_through_the_module():
    states = output(
        'return sfnx.context["Execution"]["Name"]',
        "import sfnx\nfrom sfnx import state_machine",
    )
    assert states["return"]["Output"] == "{% $states.context.Execution.Name %}"


def test_a_variable_named_context_is_a_variable():
    states = output('context = {"a": 1}\nreturn [context["a"], context]')
    assert states["return"]["Output"] == ["{% {'a': 1}.a %}", {"a": 1}]


def test_the_task_token_in_a_callback():
    states = output(
        f'answer = task("{SQS}", {{"QueueUrl": "q", "MessageBody": {{"token": context["Task"]["Token"]}}}})\nreturn answer'
    )
    assert states["answer"]["Arguments"]["MessageBody"] == {
        "token": "{% $states.context.Task.Token %}"
    }


@pytest.mark.parametrize("pattern", [".sync", ".sync:2"])
def test_the_task_token_in_a_sync_task(pattern):
    # A .sync Task has a token too, which a child can send back (measured).
    states = output(
        'r = task("arn:aws:states:::states:startExecution' + pattern + '", '
        '{"StateMachineArn": "c", "Input": {"token": context["Task"]["Token"]}})\nreturn r'
    )
    assert states["r"]["Arguments"]["Input"] == {
        "token": "{% $states.context.Task.Token %}"
    }


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return context["Execution"]["Nmae"]',
            "the Context Object has no Nmae here; did you mean Name?",
        ),
        (
            'return context["Exec"]',
            "the Context Object has no Exec here; did you mean Execution? (it has Execution, State, StateMachine, Task, Map)",
        ),
        (
            'return context["Task"]["Token"]',
            "the task token exists only in the arguments of a .waitForTaskToken, .sync or .sync:2 task",
        ),
        (
            'r = task("arn:aws:states:::lambda:invoke", {"FunctionName": "f", "Payload": context["Task"]["Token"]})',
            "the task token exists only in the arguments",
        ),
        (
            f'r = task("{SQS}", {{"QueueUrl": "q", "MessageBody": context["Task"]["Token"]}}, timeout=context["Task"]["Token"])',
            "the task token exists only in the arguments",
        ),
        (
            f'r = task("{SQS}", {{"QueueUrl": "q", "MessageBody": "m"}})',
            'a .waitForTaskToken task waits for its token to come back; pass context["Task"]["Token"] in the arguments',
        ),
        (
            'return context["Map"]["Item"]',
            "Map.Item is readable only where a Map selects its items",
        ),
        (
            'return context["Execution"].get("Nmae")',
            "the Context Object has no Nmae here; did you mean Name?",
        ),
        (
            'return context.get("Task")',
            "the task token exists only in the arguments of a .waitForTaskToken, .sync or .sync:2 task",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        output(body)
    assert message in raised.value.message
