import re
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"
PUBLISH = "arn:aws:states:::aws-sdk:sns:publish"


def source(body: str) -> str:
    return (
        "from sfnx import state_machine, task, wait\n\n\n"
        "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")
    )


def states(body: str) -> dict:
    (compiled,) = compile_source(source(body)).values()
    return compiled["States"]


def run(body: str, execution_input: object, tasks: dict | None = None) -> object:
    (compiled,) = compile_source(source(body)).values()
    return asl.run(compiled, execution_input, tasks)


def test_for_over_a_list_matches_the_handwritten_loop():
    # The loop a person writes: a counter, a Choice and one Pass per iteration.
    body = 'values: list[float] = input["items"]\ntotal = 0\nfor value in values:\n    total = total + value\nreturn total'
    assert states(body) == {
        "values": {
            "Type": "Pass",
            "Assign": {
                "values": f"{{% {INPUT}.items %}}",
                "total": 0,
                "value_index": 0,
            },
            "Next": "for",
        },
        "for": {
            "Type": "Choice",
            "Choices": [
                {"Condition": "{% $value_index < $count($values) %}", "Next": "total"}
            ],
            "Default": "return",
        },
        "total": {
            "Type": "Pass",
            "Assign": {
                "total": "{% $total + $values[$value_index] %}",
                "value_index": "{% $value_index + 1 %}",
            },
            "Next": "for",
        },
        "return": {"Type": "Succeed", "Output": "{% $total %}"},
    }


def test_while_leads_back_to_its_choice():
    body = 'n: float = input["n"]\nwhile n > 0:\n    n = n - 1\nreturn n'
    assert states(body) == {
        "n": {"Type": "Pass", "Assign": {"n": f"{{% {INPUT}.n %}}"}, "Next": "while"},
        "while": {
            "Type": "Choice",
            "Choices": [{"Condition": "{% $n > 0 %}", "Next": "n_2"}],
            "Default": "return",
        },
        "n_2": {"Type": "Pass", "Assign": {"n": "{% $n - 1 %}"}, "Next": "while"},
        "return": {"Type": "Succeed", "Output": "{% $n %}"},
    }


def test_while_true_leads_back_to_its_first_state():
    body = f'while True:\n    r = task("{PUBLISH}", {{"Message": "m"}})\n    if r["MessageId"] != "":\n        break\n    wait(5)\nreturn r'
    compiled = states(body)
    assert list(compiled) == ["r", "if", "wait", "return"]
    assert compiled["wait"]["Next"] == "r"
    assert compiled["if"]["Choices"][0]["Next"] == "return"


def test_continue_gets_the_increment_its_own_state():
    body = 'items: list[float] = input["items"]\nfor item in items:\n    if item < 0:\n        continue\n    wait(item)\nreturn 1'
    compiled = states(body)
    assert compiled["if"]["Choices"][0]["Next"] == "item_index"
    assert compiled["wait"]["Next"] == "item_index"
    assert compiled["item_index"] == {
        "Type": "Pass",
        "Assign": {"item_index": "{% $item_index + 1 %}"},
        "Next": "for",
    }


@pytest.mark.parametrize(
    "loop, condition, increment",
    [
        ("range(3)", "$i < 3", "$i + 1"),
        ('range(1, input["n"])', f"$i < {INPUT}.n", "$i + 1"),
        ("range(10, 0, -2)", "$i > 0", "$i + -2"),
    ],
)
def test_range(loop, condition, increment):
    compiled = states(f"for i in {loop}:\n    wait(i)\nreturn 1")
    assert compiled["for"]["Choices"][0]["Condition"] == f"{{% {condition} %}}"
    assert compiled["i_2"]["Assign"] == {"i": f"{{% {increment} %}}"}


def test_dict_loops_over_keys():
    body = 'prices: dict[str, float] = input["prices"]\ntotal = 0\nfor name in prices:\n    total = total + prices[name]\nreturn total'
    compiled = states(body)
    assert (
        compiled["for"]["Choices"][0]["Condition"]
        == "{% $name_index < $count($keys($prices)) %}"
    )
    assert (
        compiled["total"]["Assign"]["total"]
        == "{% $total + $lookup($prices, $keys($prices)[$name_index]) %}"
    )


def test_a_list_the_body_changes_is_copied_first():
    body = 'items: list = input["items"]\nfor item in items:\n    items = items + [item]\nreturn items'
    compiled = states(body)
    assert compiled["item_items"]["Assign"] == {
        "item_items": "{% $items %}",
        "item_index": 0,
    }
    assert (
        compiled["for"]["Choices"][0]["Condition"]
        == "{% $item_index < $count($item_items) %}"
    )


def test_a_range_stop_the_body_changes_is_copied_first():
    body = 'n: float = input["n"]\nfor i in range(n):\n    n = n - 1\nreturn n'
    compiled = states(body)
    assert "$i < $i_stop" in compiled["for"]["Choices"][0]["Condition"]


def test_a_list_that_changes_on_evaluation_is_copied_first():
    # Read again at each item, [$uuid()] would give another item each time.
    body = "for item in [str(uuid.uuid4())]:\n    return item == item\nreturn False"
    (compiled,) = compile_source("import uuid\n" + source(body)).values()
    assert compiled["States"]["item_items"]["Assign"] == {
        "item_items": ["{% $uuid() %}"],
        "item_index": 0,
    }
    assert asl.run(compiled, None) is True


def test_a_range_stop_that_changes_on_evaluation_is_copied_first():
    body = (
        "n = 0\nfor i in range(int(random.random() * 3) + 1):\n    n = n + 1\nreturn n"
    )
    (compiled,) = compile_source("import random\n" + source(body)).values()
    assert "$i < $i_stop" in compiled["States"]["for"]["Choices"][0]["Condition"]
    assert asl.run(compiled, None) in {1, 2, 3}


def test_loop_variables_take_the_spelling_of_the_loop_variable():
    # _x is spelled x, and a variable named _x_index would start with _ too.
    body = 'a: list = input["a"]\nfor _x in a:\n    wait(1)\nreturn 0'
    compiled = states(body)
    assert compiled["for"]["Choices"][0]["Condition"] == "{% $x_index < $count($a) %}"
    assigned = {k for state in compiled.values() for k in state.get("Assign", {})}
    assert assigned == {"a", "x_index"}


def test_loop_variables_do_not_clash():
    body = 'value_index = 5\na: list = input["a"]\nfor value in a:\n    for value in a:\n        wait(1)\nreturn value_index'
    compiled = states(body)
    conditions = [
        s["Choices"][0]["Condition"] for s in compiled.values() if s["Type"] == "Choice"
    ]
    assert conditions == [
        "{% $value_index_2 < $count($a) %}",
        "{% $value_index_3 < $count($a) %}",
    ]


def test_types_widen_around_the_loop():
    body = 'xs: list[float] = input["xs"]\nacc = None\nfor x in xs:\n    if acc is None:\n        acc = x\n    else:\n        acc = acc + x\nreturn acc'
    assert run(body, {"xs": [1, 2, 3]}) == 6
    body = 'words: list[str] = input["words"]\nline = ""\nfor word in words:\n    line = line + word\nreturn line + "!"'
    assert states(body)["return"]["Output"] == "{% $line & '!' %}"


def test_types_widen_without_an_error():
    body = 'x = 1\nfor i in range(3):\n    x = "a"\nreturn x + 1'
    with pytest.raises(CompileError, match=re.escape("x may be number | string")):
        states(body)


@pytest.mark.parametrize(
    "body, message",
    [
        # The attempt with the types as declared gets further than the relaxed one.
        (
            's: str = ""\nitems: list = input["items"]\nfor item in items:\n    s = s + item["name"]\n    x = (1, 2)',
            "JSON has no tuples",
        ),
        # The relaxed attempt gets further than the first.
        (
            "acc = None\nfor i in range(2):\n    acc = (acc or 0) + 1\n    x = (1, 2)",
            "JSON has no tuples",
        ),
        # Both fail on the same line: the first names the types it knew.
        (
            'x = None\nfor i in range(2):\n    x = x + i + "a"',
            "+ cannot join null and number",
        ),
    ],
)
def test_a_loop_that_fails_twice_reports_the_attempt_that_got_further(body, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        states(body)


def test_a_comprehension_variable_is_not_an_assignment_of_the_loop():
    body = 'zs: list = input["zs"]\nfor i in range(2):\n    ys = [i for i in zs]\nreturn None'
    assert "for" in states(body)
    body = 'items: list = input["items"]\nzs: list = input["zs"]\nacc: list = []\nfor item in items:\n    acc = acc + [items for items in zs]\nreturn acc'
    assert "item_items" not in states(body)
    assert run(body, {"items": [1, 2], "zs": [5, 6]}) == [5, 6, 5, 6]


def test_a_loop_after_other_states_is_tried_again_from_the_same_point():
    body = 'wait(1)\nxs: list[float] = input["xs"]\nacc = None\nfor x in xs:\n    if acc is None:\n        acc = x\n    else:\n        acc = acc + x\nreturn acc'
    compiled = states(body)
    assert list(compiled) == [
        "wait",
        "xs",
        "for",
        "if",
        "acc",
        "acc_2",
        "x_index",
        "return",
    ]
    assert compiled["wait"]["Next"] == "xs"


def test_while_narrows_after_the_loop():
    body = 'v: str | None = input["v"]\nwhile v is None:\n    v = "x"\nreturn v + "!"'
    assert states(body)["return"]["Output"] == "{% $v & '!' %}"


@pytest.mark.parametrize(
    "body, first",
    [
        # What the loop copies or starts from is not pending, or is.
        (
            'items: list = input["items"]\nwait(1)\nfor item in items:\n    items = items + [item]\nreturn items',
            {"item_items": "{% $items %}", "item_index": 0},
        ),
        (
            'n: float = input["n"]\nwait(1)\nfor i in range(n):\n    n = n - 1\nreturn n',
            {"i_stop": "{% $n %}", "i": 0},
        ),
        (
            'first: float = input["first"]\nfor i in range(first, 10):\n    wait(i)\nreturn 1',
            {"i": "{% $first %}"},
        ),
        ("i = 5\nfor i in range(3):\n    wait(i)\nreturn 1", {"i": 0}),
    ],
)
def test_loop_initialization(body, first):
    compiled = states(body)
    initialization = next(
        s["Assign"]
        for s in compiled.values()
        if s["Type"] == "Pass" and s.get("Next") == "for"
    )
    assert initialization == first


def test_a_body_that_always_returns_has_no_increment():
    compiled = states('xs: list = input["xs"]\nfor x in xs:\n    return x\nreturn None')
    assert [s["Type"] for s in compiled.values()] == [
        "Pass",
        "Choice",
        "Succeed",
        "Succeed",
    ]


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        (
            'values: list[float] = input["items"]\ntotal = 0\nfor value in values:\n    total = total + value\nreturn total',
            {"items": [3, 1, 2]},
            6,
        ),
        (
            'values: list[float] = input["items"]\ntotal = 0\nfor value in values:\n    total = total + value\nreturn total',
            {"items": []},
            0,
        ),
        (
            'n: float = input["n"]\nsteps = []\nwhile n > 0:\n    if n == 3:\n        n = n - 1\n        continue\n    steps = steps + [n]\n    n = n - 1\nreturn steps',
            {"n": 5},
            [5, 4, 2, 1],
        ),
        (
            'found = None\nfor i in range(10):\n    if i * i > input["limit"]:\n        found = i\n        break\nreturn found',
            {"limit": 20},
            5,
        ),
        (
            'found = None\nfor i in range(10):\n    if i * i > input["limit"]:\n        found = i\n        break\nreturn found',
            {"limit": 200},
            None,
        ),
        (
            "out = []\nfor i in range(10, 0, -3):\n    out = out + [i]\nreturn out",
            {},
            [10, 7, 4, 1],
        ),
        (
            'prices: dict[str, float] = input["prices"]\ntotal = 0\nfor name in prices:\n    total = total + prices[name]\nreturn total',
            {"prices": {"a": 1, "b": 2.5}},
            3.5,
        ),
        (
            'items: list = input["items"]\nfor item in items:\n    items = items + [item]\nreturn items',
            {"items": [1, 2]},
            [1, 2, 1, 2],
        ),
        (
            'rows: list[list[float]] = input["rows"]\ntotal = 0\nfor row in rows:\n    for cell in row:\n        if cell < 0:\n            break\n        total = total + cell\nreturn total',
            {"rows": [[1, -1, 5], [2, 3]]},
            6,
        ),
        (
            "n = 0\nwhile True:\n    n = n + 1\n    if n >= 4:\n        break\nreturn n",
            {},
            4,
        ),
        # The stop reads the loop variable before the loop assigns it.
        (
            "i = 3\nresult = []\nfor i in range(i):\n    result = result + [i]\nreturn result",
            {},
            [0, 1, 2],
        ),
        # A loop variable the body assigns on some paths holds its value after.
        (
            "result = []\nfor x in [-1, 1]:\n    if x > 0:\n        pass\n    else:\n        x = 10\n    result = result + [x]\nreturn result",
            {},
            [10, 1],
        ),
        (
            "result = []\nfor x in [-1, 1]:\n    if x > 0:\n        x = 10\n    result = result + [x]\nreturn result",
            {},
            [-1, 10],
        ),
    ],
)
def test_evaluation(body, execution_input, expected):
    assert run(body, execution_input) == expected


def test_a_type_that_grows_on_every_iteration_becomes_unknown():
    assert run("x = []\nfor i in range(3):\n    x = [x]\nreturn x", {}) == [[[[]]]]
    assert run('d = {}\nfor i in range(2):\n    d = {"a": d}\nreturn d', {}) == {
        "a": {"a": {}}
    }


def test_a_failed_attempt_leaves_no_function_behind():
    # The first attempt defines f before failing on acc + 1; the next one
    # assigns f on the other branch again.
    body = 'acc = None\nwhile input["go"]:\n    if input["c"]:\n        f = 1\n    else:\n        def f():\n            return 1\n    acc = acc + 1\nreturn acc'
    assert run(body, {"go": False, "c": True}) is None


def test_a_failed_attempt_leaves_no_task_behind():
    # The first attempt fails on acc or "" after taking the task(); the next
    # one, with acc of no known type, takes the same task() again.
    body = 'acc = None\nfor i in range(2):\n    acc = task("arn:aws:states:::aws-sdk:sqs:getQueueUrl", {"QueueName": "q"})["QueueUrl"] + (acc or "")\nreturn acc'
    assert run(body, {}, {"acc_2": lambda arguments: {"QueueUrl": "q"}}) == "qq"


@pytest.mark.parametrize(
    "body, message",
    [
        ("break", "break is only for loops"),
        ("continue", "continue is only for loops"),
        (
            'for x in input["a"]:\n    pass',
            "for depends on what it iterates, so the type of input['a'] must be known",
        ),
        (
            's: str = input["s"]\nfor c in s:\n    pass',
            "s is a string; for iterates lists, the keys of dicts and range()",
        ),
        (
            'xs: list = input["xs"]\nfor x in xs:\n    pass\nelse:\n    pass',
            "loops with else are not supported",
        ),
        (
            'while input["a"]:\n    pass\nelse:\n    pass',
            "loops with else are not supported",
        ),
        ('xs: list = input["xs"]\nfor a, b in xs:\n    pass', "loop over one variable"),
        (
            'xs: list = input["xs"]\nfor input in xs:\n    pass',
            "input is the execution input",
        ),
        (
            'xs: list = input["xs"]\nfor x in xs:\n    pass\nreturn x',
            "x is the loop variable and ends with the loop",
        ),
        (
            'xs: list = input["xs"]\nfor x in xs:\n    y = x\nreturn y',
            "y is not assigned on every path",
        ),
        (
            'xs: list = input["xs"]\nfor i, x in xs:\n    pass',
            "loop over one variable",
        ),
        (
            'xs: list = input["xs"]\nfor x in enumerate(xs):\n    pass',
            "enumerate() is not supported; count with range",
        ),
        # The loop a writer counts with unpacks two variables, so enumerate()
        # and zip() say what to count with before the message about unpacking.
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs):\n    pass',
            "enumerate() is not supported; count with range",
        ),
        (
            (
                'xs: list = input["xs"]\nys: list = input["ys"]\n'
                "for x, y in zip(xs, ys):\n    pass"
            ),
            "zip() is a list here only as list(zip(a, b))",
        ),
        ("return range(1, 2, 0)", "the step of range is a nonzero whole number"),
        ("for i in range():\n    pass", "range takes a stop"),
        ("for i in range(1, 2, 3, 4):\n    pass", "range takes a stop"),
        (
            "for i in range(0, 10, 0):\n    pass",
            "the step of range is a nonzero whole number",
        ),
        (
            'for i in range(0, 10, input["step"]):\n    pass',
            "the step of range is a nonzero whole number",
        ),
        ("for i in range('a'):\n    pass", "'a' is a string, and range takes numbers"),
        (
            "for i in range(3):\n    i = 0",
            "i counts the loop; assign the new value to another name",
        ),
        ("while True:\n    pass", "this loop does nothing and never ends"),
        (
            'xs: list[str] = input["xs"]\ntotal = 0\nfor x in xs:\n    total = total - x',
            "x is a string, and - takes numbers",
        ),
        # A range variable counts past its last value, and an empty range
        # would leave the value from before in Python.
        (
            "for i in range(3):\n    pass\nreturn i",
            "i is the loop variable and ends with the loop",
        ),
        (
            "i = 7\nfor i in range(0):\n    pass\nreturn i",
            "i is the loop variable and ends with the loop",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        states(body)
    assert message in raised.value.message
