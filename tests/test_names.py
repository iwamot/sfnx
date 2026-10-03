"""A `# state:` comment names the state its statement makes, which the
statement then makes and the passes keep under that name."""

import pytest

from sfnx import testing
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError

IMPORTS = "from sfnx import aws, parallel, state_machine\n\n"


def machine(body: str, module: str = "") -> dict:
    source = f"{IMPORTS}{module}\n\n@state_machine\ndef pay(input):\n{body}"
    (compiled,) = compile_source(source).values()
    return compiled


def outline(compiled: dict) -> list[tuple[str, str, list[str]]]:
    """Each state with its type and where it leads, in order."""
    found = []
    for name, state in compiled["States"].items():
        ways = [rule["Next"] for rule in state.get("Choices", [])]
        ways += [state[key] for key in ("Default", "Next") if key in state]
        found.append((name, state["Type"], ways))
    return found


def rejected(body: str, module: str = "") -> str:
    with pytest.raises(CompileError) as raised:
        machine(body, module)
    return raised.value.message


ROUTE = """\
    c: dict = aws.optimized.lambda_.invoke(FunctionName="classify", Payload={})["Payload"]
    routed: list[str] = []
    if c["category"] == "billing" or input["priority"] == "high":  # state: IsUrgent
        aws.optimized.sns.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:urgent", Message="m")
        routed = routed + ["urgent"]
    if c["category"] == "bug":  # state: IsBug
        aws.optimized.dynamodb.put_item(TableName="bugs", Item={})
        routed = routed + ["bugs"]
    if c["confidence"] < 0.5:  # state: NeedsReview
        aws.optimized.dynamodb.put_item(TableName="review", Item={})
        routed = routed + ["review"]
    return routed
"""


def test_named_ifs_keep_their_choices_and_each_test_once():
    """A named Choice is not taken into the one before it, so each test is
    written once, at the cost of entering each Choice."""
    compiled = machine(ROUTE)
    choices = {
        name: [rule["Condition"] for rule in state["Choices"]]
        for name, state in compiled["States"].items()
        if state["Type"] == "Choice"
    }
    assert list(choices) == ["IsUrgent", "IsBug", "NeedsReview"]
    assert all(len(tests) == 1 for tests in choices.values())
    for category, priority, confidence, urgent, bug, entered in [
        ("other", "low", 0.9, 0, 0, 5),
        ("billing", "low", 0.9, 1, 0, 6),
        ("bug", "high", 0.2, 1, 1, 7),
    ]:

        def tasks(call: testing.Call, category=category, confidence=confidence):
            if call.state == "c":
                return {"Payload": {"category": category, "confidence": confidence}}
            return {}

        run = testing.run(compiled, {"priority": priority}, tasks)
        assert len(run.states) == entered == 5 + urgent + bug


APPROVAL = """\
    answer: dict = aws.optimized.lambda_.invoke(FunctionName="ask", Payload={})["Payload"]
    if answer["decision"] == "approve":  # state: IsApproved
        return "approved"
    if input["late"]:
        aws.optimized.lambda_.invoke(  # state: ReturnExpired
            FunctionName="return-expense", Payload={"reason": "expired"}
        )
        return "expired"
    aws.optimized.lambda_.invoke(  # state: ReturnRejected
        FunctionName="return-expense", Payload={"reason": "rejected"}
    )
    return "rejected"
"""


def test_a_name_on_a_task_names_the_task():
    """The name of a call goes on the Task, and on the first line of a call
    written over several."""
    names = [name for name, _, _ in outline(machine(APPROVAL))]
    assert "IsApproved" in names
    assert "ReturnExpired" in names and "ReturnRejected" in names
    assert not any(name.startswith("invoke") for name in names)


def test_a_name_on_an_assignment_or_a_parallel_names_its_state():
    body = (
        '    bucket = input["bucket"]  # state: ReadInput\n'
        '    key = input["key"]\n'
        "    def left():\n"
        '        return aws.optimized.lambda_.invoke(FunctionName="l", Payload=key)\n'
        "    def right():\n"
        '        return aws.optimized.lambda_.invoke(FunctionName="r", Payload=bucket)\n'
        "    a, b = parallel(left, right)  # state: Both\n"
        "    return [a, b]\n"
    )
    compiled = machine(body)
    assert outline(compiled)[:2] == [
        ("ReadInput", "Pass", ["Both"]),
        ("Both", "Parallel", []),
    ]
    assert compiled["States"]["ReadInput"]["Assign"].keys() == {"bucket", "key"}


def test_an_assignment_only_if_that_is_named_makes_its_choice():
    """Unnamed, it is a conditional expression in the next state."""
    body = (
        '    status = "low"\n'
        '    if input["amount"] > 100:  # state: IsLarge\n'
        '        status = "high"\n'
        '    aws.optimized.dynamodb.put_item(TableName="t", Item={"s": {"S": status}})\n'
        "    return status\n"
    )
    assert outline(machine(body)) == [
        ("IsLarge", "Choice", ["putItem t", "putItem t"]),
        ("putItem t", "Task", []),
    ]
    unnamed = machine(body.replace("  # state: IsLarge", ""))
    assert [state["Type"] for state in unnamed["States"].values()] == ["Task"]


def test_a_named_assignment_keeps_its_pass_before_a_task():
    body = (
        '    bucket = input["bucket"]  # state: ReadInput\n'
        '    r = aws.optimized.lambda_.invoke(FunctionName="f", Payload=bucket)\n'
        '    return r["Payload"]\n'
    )
    assert outline(machine(body)) == [("ReadInput", "Pass", ["r"]), ("r", "Task", [])]
    unnamed = machine(body.replace("  # state: ReadInput", ""))
    assert [state["Type"] for state in unnamed["States"].values()] == ["Task"]


def test_a_named_return_keeps_its_succeed_after_a_task():
    body = (
        '    r = aws.optimized.lambda_.invoke(FunctionName="f", Payload={})\n'
        '    return {"ok": True, "result": r["Payload"]}  # state: Done\n'
    )
    assert outline(machine(body)) == [("r", "Task", ["Done"]), ("Done", "Succeed", [])]


def test_a_name_on_a_task_adds_no_pass_or_succeed():
    body = (
        '    r = aws.optimized.lambda_.invoke(FunctionName="f", Payload={})  # state: Charge\n'
        '    return aws.optimized.lambda_.invoke(FunctionName="g", Payload=r)  # state: Fetch\n'
    )
    assert outline(machine(body)) == [
        ("Charge", "Task", ["Fetch"]),
        ("Fetch", "Task", []),
    ]


def test_a_name_on_a_raise_or_a_wait_names_its_state():
    body = (
        "    if input['a']:\n"
        "        raise Failed('no')  # state: Refuse\n"
        "    wait(5)  # state: Pause\n"
        "    return 1\n"
    )
    module = "from sfnx import wait\n\n\nclass Failed(Exception):\n    pass\n"
    kinds = {name: kind for name, kind, _ in outline(machine(body, module))}
    assert kinds["Refuse"] == "Fail" and kinds["Pause"] == "Wait"


def test_a_name_on_a_for_names_its_choice():
    body = (
        "    items: list[str] = input['items']\n"
        "    for item in items:  # state: EachItem\n"
        '        aws.optimized.dynamodb.put_item(TableName="t", Item={"k": {"S": item}})\n'
        "    return 1\n"
    )
    kinds = {name: kind for name, kind, _ in outline(machine(body))}
    assert kinds["EachItem"] == "Choice"


def test_a_named_pass_is_not_copied_into_the_ways_into_a_loop():
    body = (
        "    n = 0\n"
        "    while n < 3:\n"
        "        n = n + 1  # state: Count\n"
        '        aws.optimized.lambda_.invoke(FunctionName="f", Payload=n)\n'
        "    return n\n"
    )
    states = machine(body)["States"]
    assert states["Count"]["Type"] == "Pass"
    assert not any(
        "n" in rule.get("Assign", {})
        for state in states.values()
        for rule in state.get("Choices", [])
    )


def test_a_named_state_is_not_shared_with_one_that_does_the_same():
    body = (
        "    if input['a']:\n"
        '        aws.optimized.lambda_.invoke(FunctionName="unlock", Payload={})  # state: UnlockA\n'
        "        return 1\n"
        '    aws.optimized.lambda_.invoke(FunctionName="unlock", Payload={})\n'
        "    return 1\n"
    )
    names = [name for name, _, _ in outline(machine(body))]
    assert "UnlockA" in names and "invoke unlock" in names


def test_a_named_state_in_a_branch_takes_the_prefix():
    body = (
        "    def left():\n"
        '        return aws.optimized.lambda_.invoke(FunctionName="l", Payload={})  # state: CallLeft\n'
        "    return parallel(left)\n"
    )
    (branch,) = machine(body)["States"]["return"]["Branches"]
    assert list(branch["States"]) == ["left.CallLeft"]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "    x = 1  # State: X\n    return x\n",
            "# state: Name at the end of its line",
        ),
        (
            "    x = 1  # states: X\n    return x\n",
            "# state: Name at the end of its line",
        ),
        (
            "    x = 1  # state : X\n    return x\n",
            "# state: Name at the end of its line",
        ),
        (
            "    # state: X\n    x = 1\n    return x\n",
            "write it at the end of that line",
        ),
        ("    x = 1  # state: \n    return x\n", "needs a name after it"),
        (
            "    x = 1  # state: Same\n    y = x  # state: Same\n    return y\n",
            "the state name Same is taken already",
        ),
        (f"    x = 1  # state: {'X' * 81}\n    return x\n", "longer than 80"),
        (
            (
                "    try:  # state: T\n"
                '        aws.optimized.lambda_.invoke(FunctionName="f", Payload={})\n'
                "    except Exception:\n        return 1\n    return 2\n"
            ),
            "makes no state of its own",
        ),
        (
            "    if input['a']:\n        return 1\n    else:  # state: E\n        return 2\n",
            "no such statement starts here",
        ),
        (
            (
                "    if input['a']:\n        return 1\n    elif input['b']:  # state: E\n"
                "        return 2\n    return 3\n"
            ),
            "no such statement starts here",
        ),
    ],
)
def test_a_name_that_names_no_state_is_rejected(body, message):
    assert message in rejected(body)


def test_a_name_outside_a_machine_is_rejected():
    assert "no such statement starts here" in rejected(
        "    return 1\n", module="X = 1  # state: Constant\n"
    )


def test_the_comment_above_a_named_assignment_describes_its_pass():
    body = (
        "    # The bucket and the key of the upload.\n"
        '    bucket = input["bucket"]  # state: ReadInput\n'
        '    r = aws.optimized.lambda_.invoke(FunctionName="f", Payload=bucket)\n'
        '    return r["Payload"]\n'
    )
    state = machine(body)["States"]["ReadInput"]
    assert state["Comment"] == "The bucket and the key of the upload."


def test_a_name_on_a_declaration_is_rejected():
    body = "    items: list[str]  # state: Items\n    items = []\n    return items\n"
    assert "makes no state of its own to name" in rejected(body)


NOTIFY = (
    "\n\ndef notify(message):\n"
    '    aws.optimized.sns.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:t", Message=message)  # state: Notify\n'
)


def test_a_function_called_twice_names_each_call_with_a_serial():
    body = '    notify("a")\n    notify("b")\n    return 1\n'
    names = [name for name, _, _ in outline(machine(body, NOTIFY))]
    assert names == ["Notify", "Notify_2"]


def test_a_serial_that_takes_a_written_name_past_80_characters_is_rejected():
    """The name the writer chose is not shortened as a derived one is."""
    name = "A " + "b" * 78
    module = (
        "\n\ndef notify(message):\n"
        '    aws.optimized.sns.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:t", '
        f"Message=message)  # state: {name}\n"
    )
    assert [
        n for n, _, _ in outline(machine('    notify("a")\n    return 1\n', module))
    ] == [name]
    message = rejected('    notify("a")\n    notify("b")\n    return 1\n', module)
    assert f"state name {name}_2 is longer than 80 characters" in message


def test_a_call_of_a_function_cannot_be_named():
    body = '    notify("a")  # state: First\n    return 1\n'
    assert "name a statement in it that does" in rejected(body, NOTIFY)


def test_the_same_name_from_another_line_is_still_rejected():
    body = (
        '    notify("a")\n'
        '    aws.optimized.lambda_.invoke(FunctionName="f", Payload={})  # state: Notify\n'
        "    return 1\n"
    )
    assert "the state name Notify is taken already" in rejected(body, NOTIFY)


@pytest.mark.parametrize(
    "body",
    [
        # while True tests nothing, so it has no Choice.
        (
            "    while True:  # state: Loop\n"
            '        r = aws.optimized.lambda_.invoke(FunctionName="f", Payload={})\n'
            "        if r['done']:\n            return 1\n"
        ),
        # A raise the except around it catches goes there, with no Fail.
        (
            "    try:\n"
            "        raise Failed('no')  # state: Refuse\n"
            "    except Failed:\n        return 1\n"
        ),
    ],
)
def test_a_statement_that_makes_no_state_here_cannot_be_named(body):
    module = "\n\nclass Failed(Exception):\n    pass\n"
    assert "makes no state of its own to name" in rejected(body, module)


def test_a_named_succeed_keeps_its_name_where_a_pass_goes_into_it():
    """The Pass before a named Succeed, its only way in, goes into it, and
    the Succeed keeps its name."""
    body = (
        "    if input['c']:\n"
        "        x = random.random()\n"
        "    else:\n"
        "        x = random.random() + 1\n"
        "    return x  # state: Done\n"
    )
    assert outline(machine(body, "import random\n")) == [("Done", "Succeed", [])]


@pytest.mark.parametrize(
    "body",
    [
        "    if input['x']: return 1  # state: IsX\n    return 2\n",
        "    x = 1; y = 2  # state: Mark\n    return y\n",
    ],
)
def test_a_name_on_a_line_of_two_statements_is_rejected(body):
    assert "put the statements on lines of their own" in rejected(body)


@pytest.mark.parametrize(
    "body, missing",
    [
        # Unnamed, the if is a conditional expression: undefined is false.
        (
            (
                '    status = "low"\n    if input["flag"]:\n        status = "high"\n'
                "    return status\n"
            ),
            "low",
        ),
        # Named, it is a Choice, whose Condition gives nothing and fails.
        (
            (
                '    status = "low"\n    if input["flag"]:  # state: IsHigh\n'
                '        status = "high"\n    return status\n'
            ),
            None,
        ),
        # A Task in it makes a Choice too, with no name.
        (
            (
                '    status = "low"\n    if input["flag"]:\n        status = "high"\n'
                '        aws.optimized.lambda_.invoke(FunctionName="f", Payload={})\n'
                "    return status\n"
            ),
            None,
        ),
    ],
)
def test_the_state_an_if_becomes_decides_what_a_missing_key_gives(body, missing):
    compiled = machine(body)
    run = testing.run(compiled, {}, lambda call: {})
    if missing is None:
        assert run.error == "States.QueryEvaluationError"
    else:
        assert run.output == missing
    assert testing.run(compiled, {"flag": True}, lambda call: {}).output == "high"


def test_a_named_assignment_keeps_what_it_assigns():
    """Nothing reads x, but the state is named after its assignment, which
    stays, so the Pass is not left empty."""
    body = '    x = input["x"]  # state: ReadInput\n    return 1\n'
    states = machine(body)["States"]
    assert states["ReadInput"] == {
        "Type": "Pass",
        "Assign": {"x": "{% $states.context.Execution.Input.x %}"},
        "Next": "return",
    }


def test_a_named_pass_loses_what_another_statement_adds_and_nothing_reads():
    body = (
        '    bucket = input["bucket"]  # state: ReadInput\n'
        '    key = input["key"]\n'
        "    return 1\n"
    )
    assert machine(body)["States"]["ReadInput"]["Assign"] == {
        "bucket": "{% $states.context.Execution.Input.bucket %}"
    }


def test_an_ordinary_comment_names_nothing():
    body = "    x = 1  # state of the order\n    return x\n"
    assert [name for name, _, _ in outline(machine(body))] == ["return"]
