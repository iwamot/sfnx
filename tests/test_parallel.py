import re
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

PUBLISH = "arn:aws:states:::aws-sdk:sns:publish"
HEADER = "from sfnx import Timeout, parallel, state_machine, task\n\n\nclass Declined(Exception):\n    pass\n"


def source(body: str, after: str = "") -> str:
    return (
        HEADER
        + "\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
        + after
    )


def states(body: str, after: str = "") -> dict:
    (compiled,) = compile_source(source(body, after)).values()
    return compiled["States"]


def run(body: str, execution_input: object, tasks: dict | None = None) -> object:
    (compiled,) = compile_source(source(body)).values()
    return asl.run(compiled, execution_input, tasks)


EMAIL_AUDIT = (
    'order = input["order"]\n\n'
    'def email():\n    return {"to": order["email"]}\n\n'
    f'def audit():\n    entry = task("{PUBLISH}", {{"Message": order["id"]}})\n    return entry["MessageId"]\n\n'
)


def test_branches_are_named_after_their_functions():
    compiled = states(
        EMAIL_AUDIT
        + "message, receipt = parallel(email, audit)\nreturn [message, receipt]"
    )
    parallel = compiled["message"]
    assert parallel["Type"] == "Parallel"
    assert parallel["Branches"] == [
        {
            "StartAt": "email.return",
            "States": {
                "email.return": {
                    "Type": "Succeed",
                    "Output": {"to": "{% $order.email %}"},
                }
            },
        },
        {
            "StartAt": "audit.entry",
            "States": {
                "audit.entry": {
                    "Type": "Task",
                    "Resource": PUBLISH,
                    "Arguments": {"Message": "{% $order.id %}"},
                    "Assign": {"entry": "{% $states.result %}"},
                    "Next": "audit.return",
                },
                "audit.return": {"Type": "Succeed", "Output": "{% $entry.MessageId %}"},
            },
        },
    ]
    assert parallel["Assign"] == {
        "message": "{% $states.result[0] %}",
        "receipt": "{% $states.result[1] %}",
    }


def test_state_names_are_unique_across_the_whole_definition():
    body = EMAIL_AUDIT + "parallel(email)\nreturn parallel(email)"
    compiled = states(body)
    first = compiled["parallel"]["Branches"][0]["States"]
    second = compiled["return"]["Branches"][0]["States"]
    assert list(first) == ["email.return"]
    assert list(second) == ["email.return_2"]


def test_where_parallel_can_be_written():
    body = (
        EMAIL_AUDIT
        + "results = parallel(email, audit)\nparallel(email)\nreturn parallel(audit)"
    )
    compiled = states(body)
    assert compiled["results"]["Assign"] == {"results": "{% $states.result %}"}
    assert compiled["parallel"]["Next"] == "return"
    assert compiled["return"]["End"] is True


def test_retry_and_catch():
    body = (
        EMAIL_AUDIT
        + 'try:\n    r = parallel(audit, retry=[{"ErrorEquals": [Timeout]}])\nexcept Declined:\n    return "declined"\nreturn r'
    )
    state = states(body)["r"]
    assert state["Retry"] == [{"ErrorEquals": ["States.Timeout"]}]
    assert state["Catch"][0]["ErrorEquals"] == ["Declined"]
    assert list(state) == ["Type", "Branches", "Retry", "Catch", "Assign", "Next"]


def test_a_module_function_sees_none_of_the_machine():
    after = f'\n\ndef ping():\n    task("{PUBLISH}", {{"Message": "ping"}})\n'
    assert (
        states("return parallel(ping)", after)["return"]["Branches"][0]["StartAt"]
        == "ping.publish"
    )
    with pytest.raises(CompileError, match="x is not assigned here"):
        states("x = 1\nreturn parallel(show)", "\n\ndef show():\n    return x\n")


def test_branch_returns_join_their_types():
    body = 'def pick():\n    if input["a"]:\n        return "x"\n    return 1\n\n(r,) = parallel(pick)\nreturn r + 1'
    with pytest.raises(CompileError, match=re.escape("r may be number | string")):
        states(body)


def test_branch_types_join_into_the_result():
    body = 'def one():\n    return 1\n\ndef two():\n    return 2\n\na, b = parallel(one, two)\nreturn a + input["x"]'
    assert (
        states(body)["return"]["Output"]
        == "{% $a + $states.context.Execution.Input.x %}"
    )


BRANCHES = (
    'def name():\n    return "x"\n\n'
    "def numbers():\n    return [1, 2]\n\n"
    f'def sent():\n    return task("{PUBLISH}", {{"Message": "m"}})\n\n'
)


def test_unpacking_keeps_the_type_of_each_branch():
    body = BRANCHES + "a, b = parallel(name, numbers)\nreturn b + [3]"
    assert run(body, {}) == [1, 2, 3]


def test_a_position_written_as_a_number_keeps_the_type_of_its_branch():
    body = BRANCHES + "r = parallel(name, numbers)\nreturn [r[0] + 'y', r[-1] + [3]]"
    assert run(body, {}) == ["xy", [1, 2, 3]]


def test_a_declaration_types_what_unpacking_assigns():
    body = BRANCHES + "b: list\na, b = parallel(numbers, sent)\nreturn b + [3]"
    assert run(body, {}, {"sent.return": lambda arguments: [4]}) == [4, 3]


def test_nested_parallel_and_hidden_names_across_scopes():
    body = (
        'xs: list = input["xs"]\n'
        "def inner():\n    for x in xs:\n        pass\n    return 1\n"
        "def outer():\n    return parallel(inner)\n"
        "r = parallel(outer)\n"
        "for x in xs:\n    pass\n"
        "return r"
    )
    compiled = states(body)
    inner = compiled["r"]["Branches"][0]["States"]["outer.return"]["Branches"][0][
        "States"
    ]
    assert "{% $x_index < $count($xs) %}" in [
        s["Choices"][0]["Condition"] for s in inner.values() if s["Type"] == "Choice"
    ]
    assert (
        compiled["for"]["Choices"][0]["Condition"] == "{% $x_index_2 < $count($xs) %}"
    )


def test_unpacking():
    compiled = states('a, b = 1, input["b"]\nb, a = a, b\nreturn [a, b]')
    assert compiled["a"]["Assign"] == {
        "a": 1,
        "b": "{% $states.context.Execution.Input.b %}",
    }
    assert compiled["b"]["Assign"] == {"b": "{% $a %}", "a": "{% $b %}"}
    assert run('a, b = 1, input["b"]\nb, a = a, b\nreturn [a, b]', {"b": 2}) == [2, 1]
    assert states("pair = [1, 2]\na, b = pair\nreturn a")["a"]["Assign"] == {
        "a": "{% $pair[0] %}",
        "b": "{% $pair[1] %}",
    }


def test_unpacking_keeps_a_value_that_changes():
    # Every name reads the value again, so one that would give other elements
    # each time is kept by the state before them, as a loop keeps its items.
    body = 'a, b = jsonata("($x := $uuid(); [$x, $x])")\nreturn a == b'
    (compiled,) = compile_source("from sfnx import jsonata\n" + source(body)).values()
    assert compiled["States"]["a_items"]["Assign"] == {
        "a_items": "{% ($x := $uuid(); [$x, $x]) %}"
    }
    assert compiled["States"]["a"]["Assign"] == {
        "a": "{% $a_items[0] %}",
        "b": "{% $a_items[1] %}",
    }
    assert asl.run(compiled, {}) is True


def test_unpacking_keeps_a_changing_value_after_what_it_reads():
    # The value the names take reads an assignment of its own, which Assign
    # evaluates with the values from before the state, so it waits for it.
    body = 'n = input["n"]\na, b = jsonata("[$m, $m]", m=n)\nreturn [a, b]'
    (compiled,) = compile_source("from sfnx import jsonata\n" + source(body)).values()
    assert list(compiled["States"]) == ["n", "a_items", "a", "return"]
    assert compiled["States"]["a_items"]["Assign"] == {
        "a_items": "{% ($m := $n; [$m, $m]) %}"
    }
    assert asl.run(compiled, {"n": 7}) == [7, 7]


def test_evaluation():
    body = (
        EMAIL_AUDIT
        + 'message, receipt = parallel(email, audit)\nreturn {"message": message, "receipt": receipt}'
    )
    tasks = {
        "audit.entry": lambda arguments: {"MessageId": "m-" + arguments["Message"]}
    }
    assert run(body, {"order": {"id": "o1", "email": "a@example.com"}}, tasks) == {
        "message": {"to": "a@example.com"},
        "receipt": "m-o1",
    }


def test_a_failing_branch_is_caught_around_the_parallel():
    body = 'def check():\n    if input["amount"] > 10:\n        raise Declined("too much")\n    return "ok"\n\ntry:\n    (r,) = parallel(check)\nexcept Declined as e:\n    return str(e)\nreturn r'
    assert run(body, {"amount": 5}) == "ok"
    assert run(body, {"amount": 50}) == "too much"


@pytest.mark.parametrize(
    "body, message",
    [
        ("return parallel()", "parallel takes the functions to run"),
        ("return parallel(lambda: 1)", "pass the function by name"),
        (
            "def f():\n    return 1\nreturn parallel(f, timeout=3)",
            "parallel takes only retry=",
        ),
        (
            "return parallel(missing)",
            "missing is not a function defined here; define it with def missing(...):",
        ),
        (
            "def f(x):\n    return x\nreturn parallel(f)",
            "a branch takes no parameters; f reads the variables around it instead",
        ),
        ("def f():\n    return 1\nf = 2", "f is a function here"),
        ("f = 2\ndef f():\n    return 1", "f is a variable here"),
        (
            "x = 1\ndef f():\n    return 1\nx = parallel(f) if input['a'] else 0",
            "parallel() here would run whether or not this part is taken",
        ),
        (
            "def f():\n    return 1\nif parallel(f):\n    pass",
            "parallel() makes a Parallel state; call it on its own line",
        ),
        (
            "total = 0\ndef f():\n    total = 1\n    return total\nreturn parallel(f)",
            "total is assigned outside this function too",
        ),
        (
            "def f():\n    for total in range(1):\n        pass\n    return 1\ntotal = 0\nreturn parallel(f)",
            "total is assigned outside this function too",
        ),
        ("def f():\n    return 1\na, b.c = parallel(f)", "unpack into variable names"),
        ("a, a = 1, 2", "unpack into different names"),
        ("() = [1]", "unpack into at least one name"),
        ("a, b = 1, 2, 3", "2 names take 2 values"),
        ("def f():\n    break\nreturn parallel(f)", "break is only for loops"),
        # A name the function assigns is its own from its first line, as in
        # Python, so it does not read the loop variable around it.
        (
            "out = []\nfor x in [-1, 1]:\n    def f():\n        if x > 0:\n            x = 10\n        return x\n    out = out + parallel(f)\nreturn out",
            "x is not assigned here",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        states(body)
    assert message in raised.value.message


@pytest.mark.parametrize(
    "body",
    [
        # Python would run whichever definition the branch taken made.
        "if input['a']:\n    def f():\n        return 1\nelse:\n    def f():\n        return 2\nreturn parallel(f)",
        # Python would not define f on the other path.
        "if input['a']:\n    def f():\n        return 1\nreturn parallel(f)",
        "xs: list = input['xs']\nfor x in xs:\n    def f():\n        return 1\nreturn parallel(f)",
        "n = 0\nwhile n < 1:\n    n = n + 1\n    try:\n        task('arn:aws:states:::lambda:invoke', {'FunctionName': 'g'})\n    except Exception:\n        def f():\n            return 1\nreturn parallel(f)",
    ],
)
def test_a_function_defined_on_some_paths_only(body):
    with pytest.raises(
        CompileError, match="f is not defined the same way on every path"
    ):
        states(body)


@pytest.mark.parametrize(
    "body",
    [
        "def f():\n    return 1\nout = []\nfor i in range(2):\n    out = out + parallel(f)\n    def f():\n        return 2\nreturn out",
        "def f():\n    return 1\nfor i in range(2):\n    def f():\n        return 2\nreturn parallel(f)",
        "def f():\n    return 1\nn = 0\nwhile n < 2:\n    n = n + 1\n    if n > 1:\n        def f():\n            return 2\nreturn parallel(f)",
    ],
)
def test_a_function_defined_again_in_a_loop(body):
    # Python would run the new definition in later iterations and after the loop.
    with pytest.raises(CompileError, match="f is defined before the loop too"):
        states(body)


def test_a_function_defined_in_a_loop_is_used_in_it():
    body = "out = []\nfor i in range(2):\n    def f():\n        return i\n    out = out + parallel(f)\nreturn out"
    assert run(body, {}) == [0, 1]


def test_a_function_defined_before_the_branches_is_the_same_on_each():
    body = "def f():\n    return 1\nif input['a']:\n    x = 1\nelse:\n    x = 2\nreturn parallel(f)"
    assert run(body, {"a": True}) == [1]


def test_a_branch_that_always_raises():
    # It returns nothing, so the result has the types of the other branches.
    body = 'def f():\n    return 1\ndef g():\n    raise Declined("no")\na, b = parallel(f, g)\nreturn a + 1'
    with pytest.raises(asl.Failure) as failure:
        run(body, {})
    assert (failure.value.error, failure.value.cause) == ("Declined", "no")


def test_a_comprehension_variable_is_not_the_function_s_own():
    # Python keeps the comprehension's x to itself, so x after it is the loop's.
    body = "for x in [1]:\n    def f():\n        return [x for x in [2]] + [x]\n    return parallel(f)"
    assert run(body, {}) == [[2, 1]]


def test_parallel_runs_the_branches_in_python():
    import sfnx

    assert sfnx.parallel(lambda: 1, lambda: 2) == [1, 2]


def test_decorated_branch(tmp_path):
    with pytest.raises(
        CompileError, match="a function for parallel takes no decorators"
    ):
        states("return parallel(f)", "\n\n@staticmethod\ndef f():\n    return 1\n")
