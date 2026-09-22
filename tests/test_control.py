import re
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl, truthy

INPUT = "$states.context.Execution.Input"
HEADER = "from sfnx import state_machine, wait\n\n\n"


def source(body: str) -> str:
    return HEADER + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")


def definition(body: str) -> dict:
    (compiled,) = compile_source(source(body)).values()
    return compiled


def test_choice_example():
    body = 'if input["amount"] > 1000:\n    fee = 100\nelse:\n    fee = 10\nreturn fee'
    assert definition(body) == {
        "QueryLanguage": "JSONata",
        "StartAt": "if",
        "States": {
            "if": {
                "Type": "Choice",
                "Choices": [
                    {"Condition": f"{{% {INPUT}.amount > 1000 %}}", "Next": "fee"}
                ],
                "Default": "fee_2",
            },
            "fee": {"Type": "Pass", "Assign": {"fee": 100}, "Next": "return"},
            "fee_2": {"Type": "Pass", "Assign": {"fee": 10}, "Next": "return"},
            "return": {"Type": "Succeed", "Output": "{% $fee %}"},
        },
    }


def test_elif_adds_rules_to_one_choice():
    body = (
        'if input["a"] > 2:\n    x = 2\nelif input["a"] > 1:\n    x = 1\n'
        "elif input['b']:\n    return 0\nelse:\n    x = 0\nreturn x"
    )
    states = definition(body)["States"]
    assert states["if"] == {
        "Type": "Choice",
        "Choices": [
            {"Condition": f"{{% {INPUT}.a > 2 %}}", "Next": "x"},
            {"Condition": f"{{% {INPUT}.a > 1 %}}", "Next": "x_2"},
            {"Condition": f"{{% {truthy(f'{INPUT}.b')} %}}", "Next": "return"},
        ],
        "Default": "x_3",
    }
    assert states["x"]["Next"] == states["x_3"]["Next"] == "return_2"


def test_if_without_else_defaults_to_what_follows():
    body = 'x = 1\nif input["a"]:\n    x = 2\nreturn x'
    states = definition(body)["States"]
    assert states["if"]["Default"] == "return"
    assert states["x_2"]["Next"] == "return"
    assert states["x"]["Next"] == "if"


def test_empty_branch_links_to_what_follows():
    states = definition('if input["a"]:\n    pass\nreturn 1')["States"]
    assert states["if"] == {
        "Type": "Choice",
        "Choices": [{"Condition": f"{{% {truthy(f'{INPUT}.a')} %}}", "Next": "return"}],
        "Default": "return",
    }


def test_trailing_if_falls_through_to_the_implicit_return():
    states = definition('if input["a"]:\n    return 1')["States"]
    assert states["if"]["Default"] == "return_2"
    assert states["return_2"] == {"Type": "Succeed", "Output": None}


def test_branches_that_all_return_end_the_function():
    body = 'if input["a"]:\n    return 1\nelse:\n    return 2'
    assert list(definition(body)["States"]) == ["if", "return", "return_2"]


@pytest.mark.parametrize(
    "call, field",
    [
        ("wait(10)", {"Seconds": 10}),
        ("wait(0)", {"Seconds": 0}),
        ('wait(input["delay"])', {"Seconds": f"{{% {INPUT}.delay %}}"}),
        ('wait(until="2026-09-13T01:59:00Z")', {"Timestamp": "2026-09-13T01:59:00Z"}),
        (
            'wait(until="2026-09-13T01:59:00.5Z")',
            {"Timestamp": "2026-09-13T01:59:00.5Z"},
        ),
        ('wait(until=input["resumeAt"])', {"Timestamp": f"{{% {INPUT}.resumeAt %}}"}),
    ],
)
def test_wait(call, field):
    states = definition(f"x = 1\n{call}\nreturn x")["States"]
    assert states == {
        "x": {"Type": "Pass", "Assign": {"x": 1}, "Next": "wait"},
        "wait": {"Type": "Wait", **field, "Next": "return"},
        "return": {"Type": "Succeed", "Output": "{% $x %}"},
    }


DATETIME_IMPORTS = "from datetime import datetime, timedelta\n"


def with_datetimes(body: str) -> dict:
    (compiled,) = compile_source(DATETIME_IMPORTS + source(body)).values()
    return compiled


@pytest.mark.parametrize(
    "call, timestamp",
    [
        ("wait(until=datetime.now())", "$now()"),
        (
            "wait(until=datetime.now() + timedelta(hours=1))",
            "$fromMillis($millis() + 3600000)",
        ),
        (
            'wait(until=datetime.fromisoformat(input["at"]) - timedelta(minutes=5))',
            f"$fromMillis($toMillis({INPUT}.at) - 300000)",
        ),
        # A datetime written as its text is the text, as it was before.
        ("wait(until=str(datetime.now()))", "$now()"),
    ],
)
def test_wait_until_a_datetime(call, timestamp):
    """until= takes a datetime, which is the timestamp text Timestamp holds."""
    states = with_datetimes(f"{call}\nreturn 1")["States"]
    assert states["wait"] == {
        "Type": "Wait",
        "Timestamp": "{% " + timestamp + " %}",
        "Next": "return",
    }


def test_wait_until_a_datetime_runs():
    body = (
        'wait(until=datetime.fromisoformat(input["at"]) + timedelta(days=1))\nreturn 1'
    )
    assert asl.run(with_datetimes(body), {"at": "2026-09-15T13:43:06.735Z"}) == 1


def test_wait_through_the_module():
    source = "import sfnx\n\n\n@sfnx.state_machine\ndef pay(input):\n    sfnx.wait(1)\n"
    (compiled,) = compile_source(source).values()
    assert compiled["States"]["wait"] == {
        "Type": "Wait",
        "Seconds": 1,
        "Next": "return",
    }


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        (
            'if input["amount"] > 1000:\n    fee = 100\nelse:\n    fee = 10\nreturn fee',
            {"amount": 5000},
            100,
        ),
        (
            'if input["amount"] > 1000:\n    fee = 100\nelse:\n    fee = 10\nreturn fee',
            {"amount": 5},
            10,
        ),
        (
            'if input["a"] > 2:\n    x = 2\nelif input["a"] > 1:\n    x = 1\nelse:\n    x = 0\nreturn x',
            {"a": 1.5},
            1,
        ),
        ('x = 1\nif input["a"]:\n    x = 2\n    wait(1)\nreturn x', {"a": [0]}, 2),
        (
            'items: list = input["a"]\nif items:\n    return "some"\nreturn "none"',
            {"a": [0]},
            "some",
        ),
        (
            'v: str | None = input["v"]\nif v is None:\n    return "missing"\nreturn v + "!"',
            {"v": "x"},
            "x!",
        ),
        (
            'v: str | None = input["v"]\nif v is None:\n    return "missing"\nreturn v + "!"',
            {"v": None},
            "missing",
        ),
        ('wait(until="2026-09-13T01:59:00Z")\nreturn 1', {}, 1),
        # x += v is x = x + v.
        ('t = input["t"] + 0\nt += 2\nt *= 3\nt -= 1\nt //= 2\nreturn t', {"t": 1}, 4),
        ('s: str = input["s"]\ns += "!"\nreturn s', {"s": "a"}, "a!"),
        # A value that may be a list is tested for one when it is evaluated.
        *(
            (f'v: list | None = input["v"]\n{test}', {"v": v, "go": True}, expected)
            for test in (
                'if v:\n    return "some"\nreturn "none"',
                'if not v:\n    return "none"\nreturn "some"',
                'if v and input["go"]:\n    return "some"\nreturn "none"',
            )
            for v, expected in (([0], "some"), ([], "none"), (None, "none"))
        ),
    ],
)
def test_evaluation(body, execution_input, expected):
    assert asl.run(definition(body), execution_input) == expected


@pytest.mark.parametrize(
    "body, code",
    [
        # isinstance narrows the variable in each branch.
        (
            'v: str | float = input["v"]\nif isinstance(v, str):\n    return v + "!"\nelse:\n    return v + 1',
            ["$v & '!'", "$v + 1"],
        ),
        # A test that failed narrows the rules after it.
        (
            'v: str | float | None = input["v"]\nif v is None:\n    return 0\nelif isinstance(v, str):\n    return v + "!"\nreturn v + 1',
            ["0", "$v & '!'", "$v + 1"],
        ),
        (
            'v: str | None = input["v"]\nif v is not None:\n    return v + "!"\nreturn 1',
            ["$v & '!'", "1"],
        ),
        (
            'v: str | None = input["v"]\nif not (v is None):\n    return v + "!"\nreturn 1',
            ["$v & '!'", "1"],
        ),
        (
            'v: str | None = input["v"]\nif v is None or v == "":\n    return 1\nreturn v + "!"',
            ["1", "$v & '!'"],
        ),
        (
            'x = input["x"]\nif isinstance(x, list):\n    return len(x)\nreturn 0',
            ["$count($x)", "0"],
        ),
        (
            'v: str | None = input["v"]\nreturn v + "!" if v is not None else ""',
            ["$exists($v) and $v != null ? $v & '!' : ''"],
        ),
        (
            'v: str | None = input["v"]\nreturn v is not None and len(v) > 0',
            ["$exists($v) and $v != null and $length($v) > 0"],
        ),
        (
            'v: str | None = input["v"]\nreturn v is None or len(v) > 0',
            ["$not($exists($v) and $v != null) or $length($v) > 0"],
        ),
        (
            'v: list[float] | None = input["v"]\nif v is not None:\n    return v[0] + 1\nreturn 0',
            ["$v[0] + 1", "0"],
        ),
        (
            'v: str | None = input["v"]\nif isinstance(v, (float, int)):\n    return v + 1\nreturn 0',
            ["$v + 1", "0"],
        ),
        (
            'v: str = input["v"]\nif isinstance(v, str):\n    return v + "!"\nreturn v + "?"',
            ["$v & '!'", "$v & '?'"],
        ),
    ],
)
def test_narrowing(body, code):
    states = definition(body)["States"]
    outputs = [s["Output"] for s in states.values() if s["Type"] == "Succeed"]
    assert outputs == [int(c) if c.isdigit() else f"{{% {c} %}}" for c in code]


def test_types_join_after_branches():
    body = 'if input["a"]:\n    x = 1\nelse:\n    x = "one"\nreturn x + 1'
    with pytest.raises(CompileError, match=re.escape("x may be number | string")):
        definition(body)
    body = 'if input["a"]:\n    x = 1\nelse:\n    x = 2\nreturn x + input["b"]'
    assert definition(body)["States"]["return"]["Output"] == f"{{% $x + {INPUT}.b %}}"


def test_declarations_join_after_branches():
    # Each branch declares a type, so a later value of no type of its own
    # may be either.
    body = 'if input["a"]:\n    x: str = input["x"]\nelse:\n    x: list = input["x"]\nx = input["new"]\nreturn len(x)'
    with pytest.raises(CompileError, match=re.escape("x may be array | string")):
        definition(body)
    # A declaration belongs to the name, as in Python, so one on some branches
    # holds after them too.
    body = 'if "page" in input:\n    page: dict = input["page"]\nelse:\n    page = {}\npage = input["next"]\nreturn len(page)'
    assert (
        definition(body)["States"]["return"]["Output"] == "{% $count($keys($page)) %}"
    )


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'if input["a"]:\n    x = 1\nreturn x',
            "x is not assigned on every path to here; assign it before the if, loop or try, or on every path",
        ),
        (
            'if input["a"]:\n    x = 1\nelse:\n    y = 1\nreturn y',
            "y is not assigned on every path",
        ),
        (
            # An earlier branch of the same if assigning x does not make x
            # unassigned altogether in the next.
            'if input["a"]:\n    x = 1\nif input["b"]:\n    x = 2\nelse:\n    y = x',
            "x is not assigned on every path",
        ),
        ('if input["a"]:\n    return 1\nelse:\n    return 2\nx = 1', "never reached"),
        (
            'input = input["a"]',
            "input is the execution input; assign the new value to another name",
        ),
        (
            "x = wait(1)",
            "wait() makes a state and has no value; call it on its own line",
        ),
        ("wait()", "wait takes seconds or until="),
        ("wait(1, until='x')", "wait takes seconds or until="),
        ("wait(seconds=1)", "wait takes seconds or until="),
        ("wait(1.5)", "Wait takes whole seconds from 0 to 99,999,999"),
        ("wait(-1)", "Wait takes whole seconds"),
        ("wait(100_000_000)", "Wait takes whole seconds"),
        ("wait('10')", "'10' is a string; wait takes seconds"),
        ("wait(until='2026-09-13 01:59:00')", "Wait timestamps are UTC with T and Z"),
        (
            "wait(until='2026-09-13T01:59:00+09:00')",
            "Wait timestamps are UTC with T and Z",
        ),
        ("wait(until=10)", "10 is a number; until takes a timestamp string"),
        (
            "xs = [1]\nxs += [2]",
            "xs += extends the list in place, which other names for it see in Python; write xs = xs + [2]",
        ),
        ('d = {"a": 1}\nd["a"] += 1', "assign one variable per statement"),
        ("t += 1", "t is not assigned here"),
        (
            "match input:\n    case 1:\n        pass",
            "match is not supported; write if / elif / else",
        ),
        ("global x", "global is not supported; return the value"),
        ('print("x")', "a value on a line of its own does nothing in a state machine"),
        ("import json", "import at the top of the module"),
        ("x = 1\ndel x", "del is not supported"),
        ("class A:\n    pass", "define error classes at the top of the module"),
        ("return {1, 2}", "JSON has lists only; write a list: [a, b]"),
        ("return (n := 1)", "assign the value on a line of its own first"),
        ('xs: list = input["xs"]\nreturn [*xs]', "unpacking with * is not supported"),
        ('xs: list = input["xs"]\nreturn xs[::2]', "a slice takes no step"),
        (
            "def f():\n    return 1\nreturn f()",
            "f() cannot be called directly; a function runs as states through parallel(f) or a map",
        ),
        (
            "def f():\n    return 1\nf()",
            "f() cannot be called directly; a function runs as states through parallel(f) or a map",
        ),
        # A function of the writer's own keeps its own message, even under the
        # name of a built-in that says what to write instead.
        (
            "def filter(item):\n    return item\nreturn filter(input)",
            "filter() cannot be called directly; a function runs as states",
        ),
        (
            'if input["a"] + input["b"]:\n    pass',
            "the type of input['a'] must be known",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        definition(body)
    assert message in raised.value.message
