import re
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"
LAMBDA = "arn:aws:states:::lambda:invoke"
PUBLISH = "arn:aws:states:::aws-sdk:sns:publish"
CHARGE = f'task("{LAMBDA}", {{"FunctionName": "charge"}})'
NOTIFY = f'task("{PUBLISH}", {{"Message": "m"}})'
CLASSES = """class Declined(Exception):
    pass


class Expired(Exception):
    pass


class Lambda:
    class ServiceException(Exception):
        pass
"""


def source(body: str, preamble: str = CLASSES) -> str:
    return (
        "from sfnx import Timeout, TaskFailed, context, state_machine, task, wait\n"
        + preamble
        + "\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def states(body: str, preamble: str = CLASSES) -> dict:
    (compiled,) = compile_source(source(body, preamble)).values()
    return compiled["States"]


def run(body: str, execution_input: object, tasks: dict) -> object:
    (compiled,) = compile_source(source(body)).values()
    return asl.run(compiled, execution_input, tasks)


def fails(error: str, cause: str = ""):
    def task(arguments):
        raise asl.Failure(error, cause)

    return task


def test_each_task_gets_a_catch_for_each_except():
    body = f"try:\n    receipt = {CHARGE}\n    note = 1\n    {NOTIFY}\nexcept Declined as e:\n    return str(e)\nreturn receipt"
    compiled = states(body)
    assert compiled["receipt"]["Catch"] == [
        {
            "ErrorEquals": ["Declined"],
            "Assign": {"e": "{% $states.errorOutput %}"},
            "Next": "return",
        }
    ]
    assert compiled["publish"]["Catch"] == compiled["receipt"]["Catch"]
    # A value written in the source cannot fail, so the Task with the Catch
    # takes it.
    assert "note" not in compiled
    assert compiled["receipt"]["Assign"] == {
        "receipt": "{% $states.result %}",
        "note": 1,
    }
    assert compiled["return"] == {"Type": "Succeed", "Output": "{% $e.Cause %}"}
    assert list(compiled["receipt"]) == [
        "Type",
        "Resource",
        "Arguments",
        "Catch",
        "Assign",
        "Next",
    ]


def test_values_written_in_the_source_go_in_the_state_that_catches():
    # Right after the Task in the try, as a hand-writer assigns a status in
    # the Task and in its catcher.
    body = (
        f"try:\n    {NOTIFY}\n    status = 200\nexcept Declined:\n    status = 500\n"
        f"{CHARGE.replace('charge', 'log')}\nreturn status"
    )
    ((_, definition),) = compile_source(source(body, CLASSES)).items()
    compiled = definition["States"]
    assert compiled["publish"]["Assign"] == {"status": 200}
    assert compiled["publish"]["Catch"][0]["Assign"] == {"status": 500}
    assert [n for n, s in compiled.items() if s["Type"] == "Pass"] == []

    def declined(arguments):
        raise asl.Failure("Declined", "no")

    log = {"invoke": lambda arguments: {}}
    assert asl.run(definition, {}, {"publish": lambda arguments: {}, **log}) == 200
    assert asl.run(definition, {}, {"publish": declined, **log}) == 500


def test_values_written_in_the_source_go_in_each_state_where_paths_join():
    body = f"try:\n    {NOTIFY}\nexcept Declined:\n    pass\nstatus = 200\n{CHARGE}\nreturn status"
    compiled = states(body)
    assert compiled["publish"]["Assign"] == {"status": 200}
    assert compiled["publish"]["Catch"][0]["Assign"] == {"status": 200}
    assert [n for n, s in compiled.items() if s["Type"] == "Pass"] == []


def test_several_errors_and_clauses_in_order():
    body = f"try:\n    {NOTIFY}\nexcept (Declined, Expired):\n    return 1\nexcept Lambda.ServiceException:\n    return 2\nexcept Exception:\n    return 3\nreturn 0"
    assert [c["ErrorEquals"] for c in states(body)["publish"]["Catch"]] == [
        ["Declined", "Expired"],
        ["Lambda.ServiceException"],
        ["States.ALL"],
    ]


def test_nested_try_puts_the_inner_catch_first():
    body = f"try:\n    try:\n        {NOTIFY}\n    except Declined:\n        return 1\nexcept Expired:\n    return 2\nreturn 0"
    assert [c["ErrorEquals"] for c in states(body)["publish"]["Catch"]] == [
        ["Declined"],
        ["Expired"],
    ]
    body = f"try:\n    try:\n        {NOTIFY}\n    except Exception:\n        return 1\nexcept Expired:\n    return 2\nreturn 0"
    assert [c["ErrorEquals"] for c in states(body)["publish"]["Catch"]] == [
        ["States.ALL"]
    ]


def test_tasks_in_branches_loops_and_handlers():
    body = (
        f'try:\n    if input["a"]:\n        {NOTIFY}\n    for i in range(2):\n        {NOTIFY}\n'
        f"except Declined:\n    {NOTIFY}\nreturn 0"
    )
    compiled = states(body)
    assert "Catch" in compiled["publish"] and "Catch" in compiled["publish_2"]
    assert "Catch" not in compiled["publish_3"]


def test_else_is_not_caught():
    body = f"try:\n    {NOTIFY}\nexcept Declined:\n    return 1\nelse:\n    {NOTIFY}\nreturn 0"
    compiled = states(body)
    assert "Catch" in compiled["publish"]
    assert "Catch" not in compiled["publish_2"]


def test_the_message_of_a_caught_error_is_its_cause():
    body = f'try:\n    {CHARGE}\nexcept Declined as e:\n    raise Expired(e)\nexcept Exception as e:\n    return f"failed: {{e}}"\nreturn None'
    compiled = states(body)
    assert compiled["raise"]["Cause"] == "{% $e.Cause %}"
    assert compiled["return"]["Output"] == "{% 'failed: ' & $e.Cause %}"


def test_bare_raise_raises_what_was_caught():
    body = f'try:\n    {NOTIFY}\nexcept Declined:\n    wait(1)\n    if input["a"]:\n        raise\nreturn 0'
    compiled = states(body)
    assert compiled["publish"]["Catch"][0]["Assign"] == {
        "caught": "{% $states.errorOutput %}"
    }
    assert compiled["raise"] == {
        "Type": "Fail",
        "Error": "{% $caught.Error %}",
        "Cause": "{% $caught.Cause %}",
    }
    body = f"try:\n    {NOTIFY}\nexcept Declined as e:\n    raise\nreturn 0"
    assert states(body)["raise"]["Error"] == "{% $e.Error %}"
    # A try around it that catches other errors lets it end the execution.
    body = f"try:\n    try:\n        {NOTIFY}\n    except Declined:\n        raise\n    {CHARGE}\nexcept Timeout:\n    pass\nreturn 0"
    assert states(body)["raise"]["Type"] == "Fail"


@pytest.mark.parametrize("name, spelled", [("_err", "err"), ("states", "states_val")])
def test_bare_raise_reads_the_error_under_its_spelling(name, spelled):
    body = f"try:\n    {NOTIFY}\nexcept Declined as {name}:\n    raise\nreturn 0"
    compiled = states(body)
    assert compiled["publish"]["Catch"][0]["Assign"] == {
        spelled: "{% $states.errorOutput %}"
    }
    assert compiled["raise"] == {
        "Type": "Fail",
        "Error": f"{{% ${spelled}.Error %}}",
        "Cause": f"{{% ${spelled}.Cause %}}",
    }
    with pytest.raises(asl.Failure) as failure:
        run(body, {}, {"publish": fails("Declined", "no")})
    assert (failure.value.error, failure.value.cause) == ("Declined", "no")


def test_a_raise_that_nothing_catches_is_a_fail():
    body = f'try:\n    {NOTIFY}\n    if input["a"]:\n        raise Expired()\nexcept Declined:\n    return 1\nreturn 0'
    assert states(body)["raise"] == {"Type": "Fail", "Error": "Expired"}


@pytest.mark.parametrize(
    "preamble, error, name",
    [
        ("import errors", "errors.Lambda.ServiceException", "Lambda.ServiceException"),
        (
            "from app.errors import Lambda",
            "Lambda.ServiceException",
            "Lambda.ServiceException",
        ),
        ("from app import errors", "errors.too_large", "too_large"),
        (CLASSES, "Lambda.ServiceException", "Lambda.ServiceException"),
        (
            (
                "class States:\n    class Http:\n        class StatusCode:\n"
                "            class _416(Exception):\n                pass\n"
            ),
            "States.Http.StatusCode._416",
            "States.Http.StatusCode.416",
        ),
        ("import errors", "errors.Http.StatusCode._416", "Http.StatusCode.416"),
        (
            'class NotFound(Exception):\n    code = 404\n    error = "Not a Hello World Example"\n',
            "NotFound",
            "Not a Hello World Example",
        ),
        (
            'class Lambda:\n    class Busy(Exception):\n        """Too many at once."""\n\n        error: str = "Lambda.TooManyRequestsException"\n',
            "Lambda.Busy",
            "Lambda.TooManyRequestsException",
        ),
    ],
)
def test_dotted_error_names(preamble, error, name):
    body = f"try:\n    {NOTIFY}\nexcept {error}:\n    return 1\nreturn 0"
    assert states(body, preamble)["publish"]["Catch"][0]["ErrorEquals"] == [name]


def test_a_declared_error_name_is_raised():
    preamble = 'class NotFound(Exception):\n    error = "Not a Hello World Example"\n'
    compiled = states('if input["x"]:\n    raise NotFound()\nreturn 0', preamble)
    assert compiled["raise"] == {"Type": "Fail", "Error": "Not a Hello World Example"}


@pytest.mark.parametrize(
    "declared, message",
    [
        (
            "error = 1",
            'error is the ASL error name, written as a string: error = "..."',
        ),
        ('error = ""', "an ASL error name has at least one character"),
        ('error = "States.Timeout"', "error names starting with States. are reserved"),
    ],
)
def test_a_declared_error_name_is_a_string_of_its_own(declared, message):
    preamble = f"class Oops(Exception):\n    {declared}\n"
    body = f"try:\n    {NOTIFY}\nexcept Oops:\n    return 1\nreturn 0"
    with pytest.raises(CompileError, match=re.escape(message)):
        states(body, preamble)


def test_type_of_a_caught_error_names_it():
    body = f"try:\n    r = {CHARGE}\nexcept Exception as e:\n    return [type(e).__name__, str(e)]\nreturn r"
    assert states(body)["return"]["Output"] == ["{% $e.Error %}", "{% $e.Cause %}"]
    tasks = {"r": fails("Declined", "card expired")}
    assert run(body, {}, tasks) == ["Declined", "card expired"]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "return type(input).__name__",
            "type(x).__name__ reads the name of a caught error",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept Exception as e:\n    return type(e, 1).__name__\nreturn 0",
            "type() takes one argument",
        ),
    ],
)
def test_type_name_is_for_a_caught_error(body, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        states(body)


def test_retry():
    body = (
        f'r = task("{LAMBDA}", {{"FunctionName": "f"}}, retry=[\n'
        '    {"ErrorEquals": [Timeout, Lambda.ServiceException], "IntervalSeconds": 2, "MaxAttempts": 0,\n'
        '     "BackoffRate": 1.5, "MaxDelaySeconds": 30, "JitterStrategy": "FULL"},\n'
        '    {"ErrorEquals": [Exception]},\n'
        "])\nreturn r"
    )
    assert states(body)["r"]["Retry"] == [
        {
            "ErrorEquals": ["States.Timeout", "Lambda.ServiceException"],
            "IntervalSeconds": 2,
            "MaxAttempts": 0,
            "BackoffRate": 1.5,
            "MaxDelaySeconds": 30,
            "JitterStrategy": "FULL",
        },
        {"ErrorEquals": ["States.ALL"]},
    ]


def test_retry_comes_before_catch():
    body = f'try:\n    task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [TaskFailed]}}])\nexcept Declined:\n    return 1\nreturn 0'
    assert list(states(body)["publish"]) == [
        "Type",
        "Resource",
        "Arguments",
        "Retry",
        "Catch",
        "Output",
        "End",
    ]


@pytest.mark.parametrize(
    "errors, expected, retry_counts",
    [
        (["Lambda.ServiceException"], 1, [0, 1]),
        (["Lambda.ServiceException"] * 2, "Lambda.ServiceException", [0, 1]),
        (["Declined"], "Declined", [0]),
    ],
)
def test_evaluation_of_retry(errors, expected, retry_counts):
    body = f'try:\n    r = task("{LAMBDA}", {{"FunctionName": "f", "Payload": context["State"]["RetryCount"]}}, retry=[{{"ErrorEquals": [Lambda.ServiceException], "MaxAttempts": 1}}])\nexcept Exception as e:\n    return e["Error"]\nreturn r["Payload"]'
    seen = []

    def task(arguments):
        """Fails with the errors in turn, then returns its arguments."""
        seen.append(arguments["Payload"])
        if len(seen) <= len(errors):
            raise asl.Failure(errors[len(seen) - 1])
        return arguments

    assert run(body, {}, {"r": task}) == expected
    assert seen == retry_counts


def test_handlers_see_what_was_assigned_before_the_failing_task():
    body = f'status = "new"\ntry:\n    {NOTIFY}\n    status = "sent"\n    receipt = {CHARGE}\nexcept Exception as e:\n    return {{"status": status, "error": e["Error"]}}\nreturn receipt["Payload"]'
    ok = {
        "publish": lambda arguments: {},
        "receipt": lambda arguments: {"Payload": "paid"},
    }
    assert run(body, {}, ok) == "paid"
    assert run(body, {}, {**ok, "publish": fails("Declined")}) == {
        "status": "new",
        "error": "Declined",
    }
    assert run(body, {}, {**ok, "receipt": fails("Lambda.Unknown")}) == {
        "status": "sent",
        "error": "Lambda.Unknown",
    }


def test_a_failed_task_leaves_the_declarations_from_before():
    body = f'x: list = []\ntry:\n    x: str = {CHARGE}["Payload"]\nexcept Exception:\n    x = input["items"]\n    return len(x)\nreturn 0'
    assert run(body, {"items": [1, 2]}, {"x_2": fails("Declined")}) == 2


def test_a_name_bound_to_different_code_on_the_ways_in_is_not_joined():
    # A Catch from each of two list loops reads x as a different expression.
    body = f'a: list = input["a"]\nb: list = input["b"]\ntry:\n    for x in a:\n        task("{LAMBDA}", {{"FunctionName": "f", "Payload": x}})\n    for x in b:\n        task("{LAMBDA}", {{"FunctionName": "g", "Payload": x}})\nexcept Exception:\n    return x'
    with pytest.raises(CompileError, match="x is the loop variable"):
        states(body)


def test_an_expression_that_fails_in_a_task_is_caught():
    body = f'try:\n    x = {CHARGE}["Payload"] - 1\nexcept Exception:\n    return "caught"\nreturn x'
    assert run(body, {}, {"x": lambda arguments: {"Payload": 2}}) == 1
    assert run(body, {}, {"x": lambda arguments: {"Payload": "oops"}}) == "caught"
    assert run(body, {}, {"x": lambda arguments: {}}) == "caught"


def test_an_expression_that_fails_after_the_task_is_caught():
    """The assignment goes in the Task's Assign, whose Catch takes its
    failure, as the except clause takes it in Python."""
    body = f'try:\n    x = {CHARGE}["Payload"]\n    y = x - 1\nexcept Exception:\n    return "caught"\nreturn y'
    assert run(body, {}, {"x": lambda arguments: {"Payload": 2}}) == 1
    assert run(body, {}, {"x": lambda arguments: {"Payload": "oops"}}) == "caught"


def test_an_assignment_the_except_clause_reads_after_is_not_caught():
    """A failing Assign loses the Task's result too, which Python keeps, so
    where the except clause reads it the assignment keeps its Pass, whose
    failure no Catch takes."""
    body = f'x = 0\ntry:\n    x = {CHARGE}["Payload"]\n    y = x - 1\nexcept Exception:\n    return x\nreturn y'
    assert any(
        s["Type"] == "Pass" and "y" in s.get("Assign", {})
        for s in states(body).values()
    )
    with pytest.raises(asl.Failure) as failure:
        run(body, {}, {"x_2": lambda arguments: {"Payload": "oops"}})
    assert failure.value.error == "States.QueryEvaluationError"


def test_evaluation_of_rethrow_and_loops():
    body = f'total = 0\nfor i in range(3):\n    try:\n        r = task("{LAMBDA}", {{"FunctionName": "f"}})\n        total = total + 1\n    except Declined:\n        continue\n    except Exception:\n        raise\nreturn total'
    assert run(body, {}, {"r": lambda arguments: {}}) == 3
    assert run(body, {}, {"r": fails("Declined")}) == 0
    with pytest.raises(asl.Failure) as failure:
        run(body, {}, {"r": fails("Lambda.Unknown", "boom")})
    assert (failure.value.error, failure.value.cause) == ("Lambda.Unknown", "boom")


def test_a_loop_inside_try_is_tried_again_with_its_catches():
    body = f'xs: list[float] = input["xs"]\nacc = None\ntry:\n    for x in xs:\n        {NOTIFY}\n        if acc is None:\n            acc = x\n        else:\n            acc = acc + x\nexcept Declined:\n    return -1\nreturn acc'
    compiled = states(body)
    assert len(compiled["publish"]["Catch"]) == 1
    assert run(body, {"xs": [1, 2]}, {"publish": lambda arguments: {}}) == 3


@pytest.mark.parametrize(
    "body, message",
    [
        (
            f"try:\n    {NOTIFY}\nexcept:\n    pass",
            "name what to catch: except Exception",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept Declined:\n    pass\nfinally:\n    pass",
            "finally is not supported",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept Exception:\n    pass\nexcept Declined:\n    pass",
            "except Exception catches every error, so the clauses after it never run",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept (Exception, Declined):\n    pass",
            "Exception matches every error; list it on its own",
        ),
        (
            "try:\n    x = 1\nexcept Declined:\n    pass",
            "nothing in this try reports an error to except",
        ),
        # Python would catch what the inner clause raises again.
        (
            f"try:\n    try:\n        {NOTIFY}\n    except Declined:\n        raise\nexcept Exception:\n    pass",
            "raise ends the execution with a Fail state, which the except around it does not catch",
        ),
        (
            f"try:\n    try:\n        {NOTIFY}\n    except Exception:\n        raise\nexcept Declined:\n    pass",
            "raise ends the execution with a Fail state, which the except around it",
        ),
        (
            f"try:\n    try:\n        {NOTIFY}\n    except Declined:\n        raise\nexcept Declined:\n    pass",
            "raise ends the execution with a Fail state, which the except around it",
        ),
        (
            f"try:\n    {NOTIFY}\n    raise Declined()\nexcept Declined:\n    pass",
            "a raise ends the execution with a Fail state, which except does not catch",
        ),
        (
            f"try:\n    {NOTIFY}\n    raise Declined()\nexcept Exception:\n    pass",
            "which except does not catch",
        ),
        (
            f"try:\n    {NOTIFY}\n    x = 1\nexcept Declined:\n    return x",
            "x is not assigned here",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept Declined as e:\n    pass\nreturn e",
            "e is not assigned",
        ),
        (
            f"try:\n    {NOTIFY}\nexcept Lambda.Missing:\n    pass",
            "Lambda has no class Missing; define it inside class Lambda",
        ),
        ("raise", "name the error to raise"),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry={{"ErrorEquals": [Timeout]}})',
            "retry is a list of retriers",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[])',
            "retry is a list of retriers",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[Timeout])',
            "a retrier is a dict",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"MaxAttempts": 1}}])',
            "a retrier needs ErrorEquals",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": Timeout}}])',
            "ErrorEquals is a list of exception classes",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": []}}])',
            "ErrorEquals is a list of exception classes",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "Tries": 1}}])',
            "retrier fields are ErrorEquals",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{**input}}])',
            "retrier fields are ErrorEquals",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "MaxAttempts": -1}}])',
            "MaxAttempts is a whole number from 0 to 99,999,999",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "IntervalSeconds": input["i"]}}])',
            "IntervalSeconds is a whole number from 1",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "MaxDelaySeconds": 31622401}}])',
            "MaxDelaySeconds is a whole number from 1 to 31,622,400",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "BackoffRate": 0.5}}])',
            "BackoffRate is a number of 1.0 or more",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "BackoffRate": True}}])',
            "BackoffRate is a number of 1.0 or more",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Timeout], "JitterStrategy": "SOME"}}])',
            'JitterStrategy is "FULL" or "NONE"',
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [Exception]}}, {{"ErrorEquals": [Timeout]}}])',
            "a retrier for Exception matches every error, so it comes last",
        ),
        (
            f'task("{PUBLISH}", {{"Message": "m"}}, retry=[{{"ErrorEquals": [ValueError]}}])',
            "ValueError is a Python exception",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        states(body)
    assert message in raised.value.message


def test_a_caught_error_named_after_a_function_is_renamed():
    body = f'try:\n    string = {CHARGE}\nexcept Exception as type:\n    return [str(type), type["Error"]]\nreturn string'
    catcher = states(body)["string"]["Catch"][0]
    assert catcher["Assign"] == {"type_val": "{% $states.errorOutput %}"}
    assert run(body, {}, {"string": fails("Declined", "no")}) == ["no", "Declined"]


def test_assignments_that_start_an_except_clause_go_in_its_catch():
    """They read the error as the error output the Catch assigns, and one
    reads what another before it assigns as its expression."""
    body = (
        f"try:\n    {CHARGE}\nexcept Declined as e:\n"
        '    reason = str(e)\n    note = {"reason": reason, "kind": type(e).__name__}\n'
        f"    {NOTIFY}\n    return note\nreturn 1"
    )
    compiled = states(body)
    assert compiled["invoke"]["Catch"][0] == {
        "ErrorEquals": ["Declined"],
        "Assign": {
            "e": "{% $states.errorOutput %}",
            "reason": "{% $states.errorOutput.Cause %}",
            "note": {
                "reason": "{% $states.errorOutput.Cause %}",
                "kind": "{% $states.errorOutput.Error %}",
            },
        },
        "Next": "publish",
    }
    note = run(body, {}, {"invoke": fails("Declined", "no funds"), "publish": dict})
    assert note == {"reason": "no funds", "kind": "Declined"}


def test_after_a_state_in_an_except_clause_the_error_is_its_variable():
    body = (
        f"try:\n    {CHARGE}\nexcept Declined as e:\n"
        f"    {NOTIFY}\n    reason = str(e)\n    {NOTIFY}\n    return reason\nreturn 1"
    )
    assert states(body)["publish"]["Assign"] == {"reason": "{% $e.Cause %}"}


@pytest.mark.parametrize(
    "statements, kept, result",
    [
        # The Pass reads the Task's result as the expression the Task assigns.
        ('x = r["Payload"] + 1', False, 3),
        # The second assignment of limits reads the dict written in the source
        # that the Task assigns, as that dict, and goes in the Task; the Pass
        # of x reads the dict the Task then assigns, whose value holds an
        # expression, and stays.
        (
            (
                'limits = {"n": 1}\n    limits = {"n": limits["n"] + r["Payload"]}\n'
                '    x = limits["n"]'
            ),
            True,
            3,
        ),
        # It reads the name of the state it is in, which is the Task's there.
        ('x = r["Payload"]\n    y = context["State"]["Name"]', True, 2),
        # Its expression binds the name the Task assigns, which the Task's
        # expression would take the place of.
        ('x = jsonata("($r := 5; $r + 1)")', True, 6),
        # A string in its expression spells the name, which is not a read.
        ("x = jsonata(\"'$r'\")", True, "$r"),
    ],
)
def test_what_goes_in_the_assign_of_a_task_inside_try(statements, kept, result):
    body = f'try:\n    r = {CHARGE}\n    {statements}\nexcept Exception:\n    return "caught"\nwait(1)\nreturn x'
    preamble = CLASSES + "from sfnx import jsonata\n"
    (compiled,) = compile_source(source(body, preamble)).values()
    assert any(s["Type"] == "Pass" for s in compiled["States"].values()) == kept
    tasks = {"r": lambda arguments: {"Payload": 2}}
    assert asl.run(compiled, {}, tasks) == result


AFTER_A_TRY = [
    # After a try whose except clause returns, only the Task leads on.
    (
        f'try:\n    r = {CHARGE}\nexcept Exception:\n    return "caught"\n'
        'x = r["Payload"]["n"]\nwait(1)\nreturn x'
    ),
    # After an inner try, the statement is in the outer one only, whose
    # except clause is not the one the Task's first catcher runs.
    (
        f"try:\n    try:\n        r = {CHARGE}\n    except Declined:\n"
        '        return "declined"\n    x = r["Payload"]["n"]\n'
        'except Exception:\n    return "caught"\nwait(1)\nreturn x'
    ),
    # At the start of the next try, whose clauses are its own, however like
    # the first try's.
    (
        f'try:\n    r = {CHARGE}\nexcept Exception:\n    return "caught"\n'
        f'try:\n    x = r["Payload"]["n"]\n    {CHARGE}\nexcept Exception:\n'
        '    return "caught"\nwait(1)\nreturn x'
    ),
]


@pytest.mark.parametrize(
    "returned, kept, result",
    [
        # n is assigned after the statement that fails, so the except clause
        # reads the n from before the Task either way.
        ("n", False, 0),
        # r is assigned before it, which Python keeps and a failing Assign
        # loses, so the assignment keeps its Pass, whose failure ends the
        # execution, as the table of the ASL's own semantics lists.
        ("r", True, None),
    ],
)
def test_an_except_clause_that_reads_only_what_follows_the_failure_takes_the_assign(
    returned, kept, result
):
    body = (
        f"n = 0\nr = {{}}\ntry:\n    r = {CHARGE}\n    n = r['Payload']['n']\n"
        f'    m = "done"\nexcept Exception:\n    return {returned}\nreturn [n, m]'
    )
    compiled = states(body)
    task = next(n for n, s in compiled.items() if s["Type"] == "Task")
    assert ("n" not in compiled[task]["Assign"]) == kept
    failing = {task: lambda arguments: {"Payload": {}}}
    if result is None:
        with pytest.raises(asl.Failure) as failure:
            run(body, {}, failing)
        assert failure.value.error == "States.QueryEvaluationError"
    else:
        assert run(body, {}, failing) == result
    assert run(body, {}, {task: lambda arguments: {"Payload": {"n": 2}}}) == [2, "done"]


@pytest.mark.parametrize("body", AFTER_A_TRY)
def test_a_statement_after_a_try_is_not_in_its_reach(body):
    """The Task's catchers are those of the try bodies it is in, and the
    statement is not in all of them, so it keeps its Pass, whose failure no
    Catch takes, as for any statement a Task's Assign does not hold."""
    compiled = states(body)
    assert any(s["Type"] == "Pass" and "x" in s["Assign"] for s in compiled.values())
    with pytest.raises(asl.Failure) as failure:
        run(body, {}, {"r": lambda arguments: {"Payload": {}}})
    assert failure.value.error == "States.QueryEvaluationError"


@pytest.mark.parametrize(
    "statement, kept, result",
    [
        # Nothing in it fails or is undefined: a failure of the Task's Assign
        # is the Task's own, and the except clause may read what it assigns.
        ('x = r.get("n")', False, 2),
        ('x = 0 if not isinstance(r.get("n"), (int, float)) else r.get("n")', False, 2),
        # A missing key is undefined, and a comparison may fail.
        ('x = r["n"]', True, 2),
        ('x = r.get("n", 0) > 1', True, True),
    ],
)
def test_an_assignment_that_cannot_fail_goes_in_the_task_the_except_reads(
    statement, kept, result
):
    body = (
        f'r = None\ntry:\n    r = json.loads({CHARGE}["Payload"])\n    {statement}\n'
        "except Exception:\n    return r\nreturn x"
    )
    preamble = CLASSES + "import json\n"
    (compiled,) = compile_source(source(body, preamble)).values()
    states = compiled["States"]
    passes = [s for s in states.values() if s["Type"] == "Pass"]
    assert any("x" in s["Assign"] for s in passes) == kept
    tasks = {"r_2": lambda arguments: {"Payload": '{"n": 2}'}}
    assert asl.run(compiled, {}, tasks) == result


def test_a_value_read_twice_is_bound_once():
    """The Task's value for r is longer than a path, so the assignment that
    reads it twice binds it to r first, where it reads it once."""
    body = (
        f'r = None\ntry:\n    r = json.loads({CHARGE}["Payload"])\n'
        '    x = 0 if not isinstance(r.get("n"), (int, float)) else r.get("n")\n'
        "except Exception:\n    return r\nreturn x"
    )
    (compiled,) = compile_source(source(body, CLASSES + "import json\n")).values()
    assign = next(s for s in compiled["States"].values() if s["Type"] == "Task")[
        "Assign"
    ]
    assert assign["x"].startswith("{% ($r := $parse(")
    assert assign["x"].count("$parse(") == 1


def test_a_value_bound_once_is_not_read_by_another_put_in_place():
    """The Task assigns n and m, and m reads the n from before the Task:
    binding the new n where the return reads it twice would change what m
    reads, so both are put in place."""
    body = (
        'n: float = input["n"]\ntry:\n'
        f"    {CHARGE}\n    n = n + 1\n    m = n * 2\n"
        '    return n * n + m\nexcept Exception:\n    return "caught"'
    )
    compiled = states(body)
    ending = next(s for s in compiled.values() if s.get("End"))
    assert ":=" not in ending["Output"]
    assert run(body, {"n": 2}, {"invoke": lambda arguments: {}}) == 15


def test_values_bound_once_do_not_read_each_other():
    """m reads the n from before the Task, so where the return reads both
    twice, binding them would give m the new n: both are put in place."""
    body = (
        'n: float = input["n"]\ntry:\n'
        f"    {CHARGE}\n    n = n + 1\n    m = n * 2\n"
        '    return m * m + n * n\nexcept Exception:\n    return "caught"'
    )
    ending = next(s for s in states(body).values() if s.get("End"))
    assert ":=" not in ending["Output"]
    assert run(body, {"n": 2}, {"invoke": lambda arguments: {}}) == 45


def test_an_assignment_the_catcher_leads_to_as_well_keeps_its_pass():
    """After an except clause that passes, the catcher leads to the
    assignment too: it cannot fail, but it would have to be written in the
    Task and again for the catcher, so it keeps its Pass."""
    body = (
        f"r = None\ntry:\n    r = {CHARGE}\nexcept Exception:\n    pass\n"
        "x = r\nwait(1)\nreturn x"
    )
    compiled = states(body)
    assert any(s["Type"] == "Pass" and "x" in s["Assign"] for s in compiled.values())
    assert run(body, {}, {"r_2": fails("Lambda.Unknown")}) is None
