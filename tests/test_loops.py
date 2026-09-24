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


def test_for_over_a_list_is_a_counter_and_a_choice():
    # A counter and a Choice whose rule adds the item and leads back to it: the
    # body's assignments go in the rule, as the increment does.
    body = 'values: list[float] = input["items"]\ntotal = 0\nfor value in values:\n    total = total + value\nreturn total'
    assert run(body, {"items": [1, 2, 3]}) == 6
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
                {
                    "Condition": "{% $value_index < $count($values) %}",
                    "Assign": {
                        "total": "{% $total + $values[$value_index] %}",
                        "value_index": "{% $value_index + 1 %}",
                    },
                    "Next": "for",
                }
            ],
            "Default": "return",
        },
        "return": {"Type": "Succeed", "Output": "{% $total %}"},
    }


def test_what_follows_a_loop_goes_in_its_choice():
    """Only the Default leads out of a loop without break, and the Choice's
    own Assign applies only there."""
    body = 'xs: list = input["xs"]\nn = 0\nfor x in xs:\n    n = n + 1\ndone = n * 2\nreturn done'
    compiled = states(body)
    assert compiled["for"]["Assign"] == {"done": "{% $n * 2 %}"}
    assert run(body, {"xs": [1, 2]}) == 4
    body = (
        'xs: list = input["xs"]\nn = 0\nfor x in xs:\n    if x > 1:\n        break\n'
        "    n = n + 1\ndone = n * 2\nreturn done"
    )
    # A break joins the Default after the loop, so each takes it.
    compiled = states(body)
    assert compiled["for"]["Assign"] == {"done": "{% $n * 2 %}"}
    assert compiled["if"]["Choices"][0]["Assign"] == {"done": "{% $n * 2 %}"}
    assert run(body, {"xs": [1, 2]}) == 2


def test_enumerate_names_the_counter():
    # The counter a person writes, under the name the source gives it.
    body = 'xs: list[float] = input["xs"]\ntotal = 0\nfor i, x in enumerate(xs):\n    total = total + i * x\nreturn total'
    assert run(body, {"xs": [5, 6, 7]}) == 20
    assert states(body) == {
        "xs": {
            "Type": "Pass",
            "Assign": {"xs": f"{{% {INPUT}.xs %}}", "total": 0, "i": 0},
            "Next": "for",
        },
        "for": {
            "Type": "Choice",
            "Choices": [
                {
                    "Condition": "{% $i < $count($xs) %}",
                    "Assign": {
                        "total": "{% $total + $i * $xs[$i] %}",
                        "i": "{% $i + 1 %}",
                    },
                    "Next": "for",
                }
            ],
            "Default": "return",
        },
        "return": {"Type": "Succeed", "Output": "{% $total %}"},
    }


def test_zip_counts_to_the_shorter_list():
    body = 'xs: list = input["xs"]\nys: list = input["ys"]\nfor a, b in zip(xs, ys):\n    pair = [a, b]'
    compiled = states(body)
    assert compiled["for"]["Choices"][0]["Condition"] == (
        "{% $a_index < $min([$count($xs), $count($ys)]) %}"
    )
    assert compiled["for"]["Choices"][0]["Assign"] == {
        "pair": ["{% $xs[$a_index] %}", "{% $ys[$a_index] %}"],
        "a_index": "{% $a_index + 1 %}",
    }


def test_items_reads_the_value_under_each_key():
    body = 'd: dict[str, float] = input["d"]\ntotal = 0\nfor k, v in d.items():\n    total = total + v'
    compiled = states(body)
    assert compiled["for"]["Choices"][0]["Condition"] == (
        "{% $k_index < $count($keys($d)) %}"
    )
    assert compiled["for"]["Choices"][0]["Assign"] == {
        "total": "{% $total + $lookup($d, $keys($d)[$k_index]) %}",
        "k_index": "{% $k_index + 1 %}",
    }


def test_while_leads_back_to_its_choice():
    body = 'n: float = input["n"]\nwhile n > 0:\n    n = n - 1\nreturn n'
    assert states(body) == {
        "n": {"Type": "Pass", "Assign": {"n": f"{{% {INPUT}.n %}}"}, "Next": "while"},
        "while": {
            "Type": "Choice",
            # The body is only the rule's Assign, so the rule leads back to
            # its own Choice.
            "Choices": [
                {
                    "Condition": "{% $n > 0 %}",
                    "Assign": {"n": "{% $n - 1 %}"},
                    "Next": "while",
                }
            ],
            "Default": "return",
        },
        "return": {"Type": "Succeed", "Output": "{% $n %}"},
    }


def test_while_true_leads_back_to_its_first_state():
    body = f'while True:\n    r = task("{PUBLISH}", {{"Message": "m"}})\n    if r["MessageId"] != "":\n        break\n    wait(5)\nreturn r'
    compiled = states(body)
    assert list(compiled) == ["r", "if", "wait", "return"]
    assert compiled["wait"]["Next"] == "r"
    assert compiled["if"]["Choices"][0]["Next"] == "return"


def test_continue_and_the_body_each_take_the_increment():
    """Where the two join, each path's last state or rule takes it."""
    body = 'items: list[float] = input["items"]\nfor item in items:\n    if item < 0:\n        continue\n    wait(item)\nreturn 1'
    compiled = states(body)
    increment = {"item_index": "{% $item_index + 1 %}"}
    rule = compiled["if"]["Choices"][0]
    assert (rule["Assign"], rule["Next"]) == (increment, "for")
    assert (compiled["wait"]["Assign"], compiled["wait"]["Next"]) == (increment, "for")
    assert run(body, {"items": [-1, 0]}) == 1


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
    # The increment after the Wait is its Assign.
    assert compiled["wait"]["Assign"] == {"i": f"{{% {increment} %}}"}


def test_dict_loops_over_keys():
    body = 'prices: dict[str, float] = input["prices"]\ntotal = 0\nfor name in prices:\n    total = total + prices[name]\nreturn total'
    compiled = states(body)
    assert (
        compiled["for"]["Choices"][0]["Condition"]
        == "{% $name_index < $count($keys($prices)) %}"
    )
    assert (
        compiled["for"]["Choices"][0]["Assign"]["total"]
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


def test_a_list_holding_a_written_expression_is_copied_first():
    # The expression jsonata() takes is not parsed, so it may call $uuid under
    # a spelling no search of the text finds.
    body = 'for item in [jsonata("$uuid ()")]:\n    return item == item\nreturn False'
    (compiled,) = compile_source("from sfnx import jsonata\n" + source(body)).values()
    assert compiled["States"]["item_items"]["Assign"] == {
        "item_items": ["{% $uuid () %}"],
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
    assert list(compiled) == ["wait", "for", "if", "return"]
    # The Wait takes the assignments before the loop once, not once per attempt.
    assert list(compiled["wait"]["Assign"]) == ["xs", "acc", "x_index"]
    assert compiled["wait"]["Next"] == "for"
    assert run(body, {"xs": [1, 2]}) == 3


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
        s["Assign"] for s in compiled.values() if s.get("Next") == "for"
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
        # enumerate: the index counts from 0, and the item is the body's to assign.
        (
            'xs: list[float] = input["xs"]\ntotal = 0\nfor i, x in enumerate(xs):\n    total = total + i * x\nreturn total',
            {"xs": [3, 1, 2]},
            5,
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs):\n    x = i\n    xs = xs + [x]\nreturn xs',
            {"xs": [7, 8]},
            [7, 8, 0, 1],
        ),
        (
            'd: dict = input["d"]\nout = []\nfor i, k in enumerate(d):\n    out = out + [i, k]\nreturn out',
            {"d": {"a": 1, "b": 2}},
            [0, "a", 1, "b"],
        ),
        # zip stops at the shorter list.
        (
            'xs: list[float] = input["xs"]\nys: list[str] = input["ys"]\nout = []\nfor a, b in zip(xs, ys):\n    out = out + [b + str(a)]\nreturn out',
            {"xs": [1, 2, 3], "ys": ["a", "b"]},
            ["a1", "b2"],
        ),
        (
            'xs: list[float] = input["xs"]\nys: list[str] = input["ys"]\nout = []\nfor a, b in zip(xs, ys):\n    out = out + [b + str(a)]\nreturn out',
            {"xs": [1], "ys": ["a", "b"]},
            ["a1"],
        ),
        (
            'xs: list[float] = input["xs"]\nys: list[str] = input["ys"]\nout = []\nfor a, b in zip(xs, ys):\n    out = out + [b + str(a)]\nreturn out',
            {"xs": [], "ys": ["a", "b"]},
            [],
        ),
        (
            'xs: list[float] = input["xs"]\nys: list[float] = input["ys"]\nout = []\nfor a, b in zip(xs, ys):\n    if a == 2:\n        continue\n    if b == 30:\n        break\n    a = a + b\n    out = out + [a]\nreturn out',
            {"xs": [1, 2, 3, 4], "ys": [10, 20, 30, 40]},
            [11],
        ),
        # A zipped list the body changes is copied first, like any other.
        (
            'xs: list[float] = input["xs"]\nys: list[float] = input["ys"]\nfor a, b in zip(xs, ys):\n    ys = ys + [a + b]\nreturn ys',
            {"xs": [1, 2], "ys": [10, 20, 30]},
            [10, 20, 30, 11, 22],
        ),
        # items() reads each value under its key.
        (
            'd: dict[str, float] = input["d"]\ntotal = 0\nkeys = ""\nfor k, v in d.items():\n    total = total + v\n    keys = keys + k\nreturn [total, keys]',
            {"d": {"a": 1, "b": 2.5}},
            [3.5, "ab"],
        ),
        (
            'd: dict[str, float] = input["d"]\nout = []\nfor k, v in d.items():\n    d = {}\n    out = out + [k, v]\nreturn out',
            {"d": {"a": 1, "b": 2}},
            ["a", 1, "b", 2],
        ),
        (
            'd: dict[str, float] = input["d"]\nout = []\nfor k, v in d.items():\n    v = v * 2\n    out = out + [v]\nreturn out',
            {"d": {}},
            [],
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
            "enumerate() gives two variables: for i, item in enumerate(items)",
        ),
        (
            'xs: list = input["xs"]\nfor i, x, y in enumerate(xs):\n    pass',
            "enumerate() gives two variables: for i, item in enumerate(items)",
        ),
        (
            'xs: list = input["xs"]\nfor (i, x), y in enumerate(xs):\n    pass',
            "enumerate() gives two variables: for i, item in enumerate(items)",
        ),
        (
            'xs: list = input["xs"]\nfor x in zip(xs, xs):\n    pass',
            "zip() gives two variables: for a, b in zip(xs, ys)",
        ),
        (
            'd: dict = input["d"]\nfor k in d.items():\n    pass',
            "items() gives two variables: for k, v in d.items()",
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs, 1):\n    pass',
            (
                "enumerate() counts from 0; add the start to i in the body: "
                "for i, item in enumerate(items)"
            ),
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate():\n    pass',
            "enumerate() counts from 0; add the start to i in the body",
        ),
        (
            'xs: list = input["xs"]\nfor a, b in zip(xs, xs, xs):\n    pass',
            (
                "zip() takes two lists in a loop: for a, b in zip(xs, ys); for more, "
                "count with range: for i in range(len(xs))"
            ),
        ),
        (
            'xs: list = input["xs"]\nfor a, b in zip(xs):\n    pass',
            "zip() takes two lists in a loop",
        ),
        (
            'd: dict = input["d"]\nfor k, v in d.items(1):\n    pass',
            "items() is written for k, v in d.items()",
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs):\n    i = 0',
            "i is the index of enumerate and cannot be assigned; copy it: j = i",
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs):\n    if x:\n        i += 1',
            "i is the index of enumerate and cannot be assigned",
        ),
        (
            'xs: list = input["xs"]\nfor x, x in zip(xs, xs):\n    pass',
            "the two loop variables need different names: for a, b in zip(xs, ys)",
        ),
        (
            'xs: list = input["xs"]\nfor k, v in xs.items():\n    pass',
            "xs is an array; items() is a dict method: for k, v in d.items()",
        ),
        (
            's: str = input["s"]\nfor i, c in enumerate(s):\n    pass',
            "s is a string; for iterates lists, the keys of dicts and range()",
        ),
        (
            'xs: list = input["xs"]\nfor a, b in zip(xs, input["ys"]):\n    pass',
            "for depends on what it iterates, so the type of input['ys'] must be known",
        ),
        (
            'xs: list = input["xs"]\nfor input, x in enumerate(xs):\n    pass',
            "input is the execution input",
        ),
        (
            'xs: list = input["xs"]\nfor i, input in enumerate(xs):\n    pass',
            "input is the execution input",
        ),
        (
            'xs: list = input["xs"]\nfor i, x in enumerate(xs):\n    pass\nreturn i',
            "i is the loop variable and ends with the loop",
        ),
        (
            'xs: list = input["xs"]\nfor a, b in zip(xs, xs):\n    pass\nreturn b',
            "b is the loop variable and ends with the loop",
        ),
        # Outside a for statement, each says where it is taken.
        (
            'xs: list = input["xs"]\nreturn [x for i, x in enumerate(xs)]',
            "enumerate() is only for a for loop: for i, item in enumerate(items)",
        ),
        (
            'xs: list = input["xs"]\nreturn enumerate(xs)',
            "enumerate() is only for a for loop",
        ),
        (
            'xs: list = input["xs"]\nreturn [a for a, b in zip(xs, xs)]',
            (
                "zip() is a list here only as list(zip(a, b)), or a loop: "
                "for a, b in zip(xs, ys)"
            ),
        ),
        (
            'd: dict = input["d"]\nreturn d.items()',
            (
                "items() gives two variables: for k, v in d.items() or "
                "{k: v for k, v in d.items()}"
            ),
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


@pytest.mark.parametrize(
    "before",
    [
        'if input["a"]:\n    n = 1\nelse:\n    n = 2\n',
        'n = 0\nfor i in range(input["a"]):\n    n = n + i\n',
    ],
)
def test_while_true_leads_back_to_what_its_body_assigns_first(before):
    """The first assignment of the body could go in the Assign of what comes
    right before the loop, a join or the exit of a for; the loop leads back
    to it, so it stays a state of the body."""
    body = (
        "kept: list = input['rows']\n"
        + before
        + "while True:\n"
        + "    kept = kept[:-1]\n"
        + "    if len(kept) < 3:\n"
        + "        break\n"
        + "return kept"
    )
    assert run(body, {"a": 3, "rows": [1, 2, 3, 4, 5, 6]}) == [1, 2]


def test_while_true_after_a_task_keeps_its_first_assignment():
    """What follows a Task can go in its Assign, but not what starts a loop
    that leads back to it."""
    body = (
        f'n: int = task("{PUBLISH}", {{"Message": "m"}})["n"]\n'
        "k = 0\n"
        "while True:\n"
        "    k = k + 1\n"
        "    if k > n:\n"
        "        break\n"
        "return k"
    )
    assert run(body, {}, {"n": lambda arguments: {"n": 3}}) == 4
