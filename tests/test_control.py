import ast
import json
import re
import textwrap

import pytest

from sfnx import testing
from sfnx.compiler import (
    always_reads,
    compile_source,
    definitions,
    grouped,
    read_as_values,
)
from sfnx.diagnostics import CompileError
from sfnx.locations import PREFIX
from tests import asl, truthy

INPUT = "$states.context.Execution.Input"
HEADER = "from sfnx import state_machine, wait\n\n\n"


def source(body: str) -> str:
    return HEADER + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")


def definition(body: str) -> dict:
    (compiled,) = compile_source(source(body)).values()
    return compiled


def test_choice_example():
    body = (
        'if input["amount"] > 1000:\n    fee = 100\n    tier = "big"\n'
        'else:\n    fee = 10\n    tier = "small"\nreturn fee'
    )
    assert definition(body) == {
        "QueryLanguage": "JSONata",
        "StartAt": "if",
        "States": {
            # What a branch assigns first goes in its rule, and what else
            # assigns in the Choice's own Assign, which the Default takes.
            "if": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Condition": f"{{% {INPUT}.amount > 1000 %}}",
                        "Assign": {"fee": 100, "tier": "big"},
                        "Next": "return",
                    }
                ],
                "Assign": {"fee": 10, "tier": "small"},
                "Default": "return",
            },
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
            {
                "Condition": f"{{% {INPUT}.a > 2 %}}",
                "Assign": {"x": 2},
                "Next": "return_2",
            },
            {
                "Condition": f"{{% {INPUT}.a > 1 %}}",
                "Assign": {"x": 1},
                "Next": "return_2",
            },
            {"Condition": f"{{% {truthy(f'{INPUT}.b')} %}}", "Next": "return"},
        ],
        "Assign": {"x": 0},
        "Default": "return_2",
    }


def test_if_without_else_defaults_to_what_follows():
    body = 'x = 1\nif input["a"]:\n    x = 2\n    y = 3\nreturn x'
    states = definition(body)["States"]
    assert states["if"]["Default"] == "return"
    assert states["if"]["Choices"][0]["Next"] == "return"
    # x = 1 goes in the Choice, which assigns it on every path.
    assert states["if"]["Assign"] == {"x": 1}


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


def test_assignments_after_a_wait_are_its_assign():
    body = "n = 0\nwait(1)\n# counted\nn = n + 1\nm = 2\nreturn n + m"
    compiled = definition(body)
    assert compiled["States"]["wait"] == {
        "Type": "Wait",
        "Comment": "counted",
        "Seconds": 1,
        "Assign": {"n": "{% $n + 1 %}", "m": 2},
        "Next": "return",
    }
    assert asl.run(compiled, {}) == 3


@pytest.mark.parametrize(
    "body, joined, passes",
    [
        # The Execution part of the context is the same in both states.
        (
            'wait(1)\nx = context["Execution"]["Id"]',
            {"x": "{% $states.context.Execution.Id %}"},
            [],
        ),
        # A value reading an assignment before it reads its expression.
        ("wait(1)\nx = 1\ny = x + 1", {"x": 1, "y": "{% 1 + 1 %}"}, []),
        # Where another path joins, each path's last state takes it.
        ('if input["wait"]:\n    wait(1)\nx = 1', {"x": 1}, []),
        # The time and a random value, which the Wait reads when it ends
        # (measured), as Python reads them after it.
        ("wait(1)\nx = str(uuid.uuid4())", {"x": "{% $uuid() %}"}, []),
        ("wait(1)\nx = str(datetime.now())", {"x": "{% $now() %}"}, []),
        # A value that reads otherwise in another state, or may.
        ('wait(1)\nx = jsonata("$random()")', None, ["x"]),
        ('wait(1)\nx = context["State"]["EnteredTime"]', None, ["x"]),
        ('wait(1)\nx = context["State"]["Name"]', None, ["x"]),
        ("wait(1)\nx = context", None, ["x"]),
        # What comes before a loop joins the Wait; the body the loop leads back
        # to keeps its own states.
        (
            "wait(1)\nx = 0\nwhile True:\n    x = x + 1\n    if x > 2:\n        break\n    wait(1)",
            {"x": 0},
            ["x"],
        ),
    ],
)
def test_what_a_wait_assigns(body, joined, passes):
    imports = "import uuid\nfrom datetime import datetime\nfrom sfnx import context, jsonata\n"
    # The Wait after them takes what is pending, which a return alone would
    # write into its Output instead.
    (compiled,) = compile_source(
        imports + source(body + "\nwait(0)\nreturn 1")
    ).values()
    states = compiled["States"]
    assert states["wait"].get("Assign") == joined
    assert [n for n, s in states.items() if s["Type"] == "Pass"] == passes


@pytest.mark.parametrize(
    "body, rule, default, passes",
    [
        # Each branch's first assignments, and what else assigns.
        (
            'if input["a"]:\n    x = 1\n    y = 2\nelse:\n    x = 3',
            {"x": 1, "y": 2},
            {"x": 3},
            [],
        ),
        # A value reading an assignment before it reads its expression.
        (
            'if input["a"]:\n    x = 1\n    y = x + 1',
            {"x": 1, "y": "{% 1 + 1 %}"},
            None,
            [],
        ),
        # What follows an if without else is where the branches join, and each
        # takes it.
        ('if input["a"]:\n    x = 1\ny = 2', {"x": 1, "y": 2}, {"y": 2}, []),
        # So does a random value, read once on the path taken.
        (
            'if input["a"]:\n    x = 1\ny = str(uuid.uuid4())',
            {"x": 1, "y": "{% $uuid() %}"},
            {"y": "{% $uuid() %}"},
            [],
        ),
        # Unless every branch returns, when only the Default leads there.
        ('if input["a"]:\n    return 0\ny = 2', None, {"y": 2}, []),
        # An else that put its assignments in the Choice takes what follows
        # too, unless it reads what they assign.
        (
            'if input["a"]:\n    return 0\nelse:\n    x = 1\ny = 2',
            None,
            {"x": 1, "y": 2},
            [],
        ),
        (
            'if input["a"]:\n    return 0\nelse:\n    x = 1\ny = x + 1',
            None,
            {"x": 1},
            ["y"],
        ),
        # The time and a random value read the same in the Choice.
        ('if input["a"]:\n    x = str(uuid.uuid4())', {"x": "{% $uuid() %}"}, None, []),
        (
            'if input["a"]:\n    x = 1\n    y = 2\nelse:\n    x = str(datetime.now())',
            {"x": 1, "y": 2},
            {"x": "{% $now() %}"},
            [],
        ),
        # A value that reads otherwise in another state, or may.
        ('if input["a"]:\n    x = context["State"]["Name"]', None, None, ["x"]),
        ('if input["a"]:\n    x = jsonata("$random()")', None, None, ["x"]),
    ],
)
def test_what_a_choice_assigns(body, rule, default, passes):
    imports = "import uuid\nfrom datetime import datetime\nfrom sfnx import context, jsonata\n"
    # The Wait after them takes what is pending, which a return alone would
    # write into its Output instead.
    (compiled,) = compile_source(
        imports + source(body + "\nwait(0)\nreturn 1")
    ).values()
    states = compiled["States"]
    assert states["if"]["Choices"][0].get("Assign") == rule
    assert states["if"].get("Assign") == default
    assert [n for n, s in states.items() if s["Type"] == "Pass"] == passes


def test_a_choice_runs_the_assign_of_the_branch_taken():
    body = 'if input["a"] > 1:\n    x = "big"\nelif input["a"] > 0:\n    x = "small"\nelse:\n    x = "none"\nreturn x'
    compiled = definition(body)
    assert [asl.run(compiled, {"a": a}) for a in (2, 1, 0)] == ["big", "small", "none"]


def test_the_comments_of_a_branch_go_with_its_assignments():
    body = '# sizes\nif input["a"]:\n    # big\n    x = 1\n    y = 1\nelse:\n    # small\n    x = 2\nreturn x'
    choice = definition(body)["States"]["if"]
    assert choice["Comment"] == "sizes\nsmall"
    assert choice["Choices"][0]["Comment"] == "big"


def test_the_comments_of_an_if_that_only_assigns_go_with_its_assignment():
    body = '# sizes\nif input["a"]:\n    # big\n    x = 1\nelse:\n    # small\n    x = 2\nreturn x'
    assert definition(body)["States"]["return"]["Comment"] == "sizes\nbig\nsmall"


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
        # A function called directly runs as states, so the call is a
        # statement's value, not a part of an expression, unless its body is
        # one return of a value.
        (
            "def f():\n    x = 1\n    return x\nreturn f() + 1",
            (
                "f() runs its body here, as states; call it on its own line, "
                "assign its result or return it: result = f(...). A function "
                "whose body is one return of a value is called inside an "
                "expression"
            ),
        ),
        (
            "def f():\n    x = 1\n    return x\nif f():\n    pass",
            "f() runs its body here, as states",
        ),
        ("def f():\n    return\nreturn [f()]", "f() runs its body here, as states"),
        # A function of the writer's own keeps its own message, even under the
        # name of a built-in that says what to write instead.
        (
            "def filter(item):\n    x = item\n    return x\nreturn [filter(input)]",
            "filter() runs its body here, as states",
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


def test_what_follows_a_return_from_a_loop_goes_in_the_rule_that_leaves_it():
    body = """
def poll(i):
    while True:
        wait(10)
        job = input["jobs"][i]
        if job["done"]:
            return job["id"]

r = poll(0)
other = input["x"] + 1
return [r, other]
"""
    states = definition(body)["States"]
    assert [n for n, s in states.items() if s["Type"] == "Pass"] == []
    rule = states["if"]["Choices"][0]
    assert set(rule["Assign"]) == {"r", "other"}
    assert asl.run(definition(body), {"jobs": [{"done": True, "id": "a"}], "x": 1}) == [
        "a",
        2,
    ]


@pytest.mark.parametrize(
    "body, kept",
    [
        # Paths that return the same end at one Succeed.
        (
            'if input["a"]:\n    return None\nif input["b"]:\n    return None\nreturn 1',
            "return",
        ),
        # And paths that raise the same, at one Fail.
        (
            'if input["a"]:\n    raise Failed("no")\nif input["b"]:\n    raise Failed("no")\nreturn 1',
            "raise",
        ),
    ],
)
def test_paths_that_end_the_same_share_the_state(body, kept):
    source = "class Failed(Exception):\n    pass\n\n\n"
    states = compile_source(
        HEADER
        + source
        + "@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )
    ((_, compiled),) = states.items()
    ends = [
        n for n, s in compiled["States"].items() if s["Type"] in {"Succeed", "Fail"}
    ]
    assert kept in ends and f"{kept}_2" not in ends
    # The second if's rule follows the first's in one Choice.
    assert [rule["Next"] for rule in compiled["States"]["if"]["Choices"]] == [
        kept,
        kept,
    ]


@pytest.mark.parametrize(
    "body",
    [
        'if input["a"]:\n    return 1\nreturn 2',
        'if input["a"]:\n    # early\n    return None\n# late\nreturn None',
    ],
)
def test_paths_that_end_otherwise_keep_their_states(body):
    states = definition(body)["States"]
    assert [n for n, s in states.items() if s["Type"] == "Succeed"] == [
        "return",
        "return_2",
    ]


def test_a_shared_end_names_the_source_of_each_path():
    body = 'if input["a"]:\n    return None\nreturn None'
    (plain,) = compile_source(source(body)).values()
    (located,) = definitions(source(body), "app.py", located=True).values()
    assert list(located["States"]) == list(plain["States"])
    comment = located["States"]["return"]["Comment"]
    assert comment.startswith(PREFIX)
    spans = json.loads(comment.removeprefix(PREFIX))["spans"]
    assert [span["at"] for span in spans] == ["7:9-7:20", "8:5-8:16"]


TASKS = "from sfnx import context, state_machine, task\n\n\nclass Failed(Exception):\n    pass\n\n\n"


def tasks_definition(body: str) -> dict:
    (compiled,) = compile_source(
        TASKS + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")
    ).values()
    return compiled


def called(compiled: dict, execution_input: object) -> list[object]:
    execution = testing.run(compiled, execution_input, lambda call: {})
    return [(call.arguments, execution.error) for call in execution.calls]


def test_paths_that_end_with_the_same_task_share_it():
    # Both paths unlock the record last, as a hand-writer runs one Unlock.
    body = (
        'if input["a"]:\n'
        '    task("arn:aws:states:::lambda:invoke", {"FunctionName": "skipped"})\n'
        '    task("arn:aws:states:::lambda:invoke", {"FunctionName": "unlock"})\n'
        "    return None\n"
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "done"})\n'
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "unlock"})'
    )
    compiled = tasks_definition(body)
    states = compiled["States"]
    assert [n for n, s in states.items() if s["Type"] == "Task"] == [
        "invoke",
        "invoke_2",
        "invoke_3",
    ]
    assert states["invoke"]["Next"] == states["invoke_3"]["Next"] == "invoke_2"
    for a in (True, False):
        assert [args for args, _ in called(compiled, {"a": a})] == [
            {"FunctionName": "skipped" if a else "done"},
            {"FunctionName": "unlock"},
        ]


def test_states_the_same_once_what_follows_is_shared_are_shared_too():
    # The Fails are shared first, and then the Tasks that lead to them.
    body = (
        'if input["a"]:\n'
        '    task("arn:aws:states:::lambda:invoke", {"FunctionName": "alert"})\n'
        '    raise Failed("no")\n'
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "charge"})\n'
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "alert"})\n'
        'raise Failed("no")'
    )
    compiled = tasks_definition(body)
    states = compiled["States"]
    assert [n for n, s in states.items() if s["Type"] in {"Task", "Fail"}] == [
        "invoke",
        "raise",
        "invoke_2",
    ]
    assert called(compiled, {"a": True}) == [({"FunctionName": "alert"}, "Failed")]


def test_a_state_that_reads_its_name_keeps_its_own():
    body = (
        'if input["a"]:\n'
        '    task("arn:aws:states:::lambda:invoke", {"FunctionName": "skipped"})\n'
        '    task("arn:aws:states:::lambda:invoke", {"FunctionName": "unlock", "Payload": context["State"]["Name"]})\n'
        "    return None\n"
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "done"})\n'
        'task("arn:aws:states:::lambda:invoke", {"FunctionName": "unlock", "Payload": context["State"]["Name"]})'
    )
    compiled = tasks_definition(body)
    names = [called(compiled, {"a": a})[1][0]["Payload"] for a in (True, False)]
    assert names == ["invoke_2", "invoke_4"]


def test_an_if_right_after_an_if_that_ends_is_one_choice():
    """The rules of the second follow the first's, and what the first
    assigns on its Default goes in each of them, as it runs before them."""
    body = (
        'if input["a"]:\n    return 1\n# the fallback\ny = input["y"]\n'
        'if input["b"]:\n    return 2\nreturn y'
    )
    states = definition(body)["States"]
    choice = states["if"]
    assert [rule["Next"] for rule in choice["Choices"]] == ["return", "return_2"]
    assert choice["Choices"][1]["Assign"] == {"y": f"{{% {INPUT}.y %}}"}
    assert choice["Choices"][1]["Comment"] == "the fallback"
    plain = definition(body.replace("# the fallback\n", ""))["States"]["if"]
    assert "Comment" not in plain["Choices"][1]
    assert choice["Assign"] == {"y": f"{{% {INPUT}.y %}}"}
    assert "if_2" not in states
    assert asl.run(definition(body), {"a": False, "b": False, "y": 3}) == 3


def test_an_if_that_reads_what_the_first_assigns_reads_it_as_its_expression():
    """The first Choice evaluates its Default's Assign with the variables and
    the input its tests read, so the second's tests read the expression."""
    body = 'if input["a"]:\n    return 1\ny = input["y"]\nif y:\n    return 2\nreturn 3'
    states = definition(body)["States"]
    assert "if_2" not in states
    assert states["if"]["Choices"][1]["Condition"] == (
        f"{{% ($y := {INPUT}.y; $type($y) = 'array' ? $count($y) > 0 : $boolean($y)) %}}"
    )
    assert states["if"]["Choices"][1]["Assign"] == {"y": f"{{% {INPUT}.y %}}"}
    for a, y, expected in [(True, True, 1), (False, True, 2), (False, False, 3)]:
        assert asl.run(definition(body), {"a": a, "y": y}) == expected


def test_a_value_that_changes_is_not_read_again_in_the_next_choice():
    body = (
        'if input["a"]:\n    return 1\ny = random.random()\nif y > 0.5:\n'
        "    return 2\nreturn 3"
    )
    ((_, compiled),) = compile_source("import random\n" + source(body)).items()
    assert compiled["States"]["if_2"]["Type"] == "Choice"


@pytest.mark.parametrize(
    "body, inputs",
    [
        # An if that starts the body of an if: a rule `a and b`, then `a`.
        (
            'if input["a"]:\n    if input["b"]:\n        return 1\n    return 2\nreturn 3',
            [{"a": a, "b": b} for a in (True, False) for b in (True, False)],
        ),
        # The second reads what the rule assigns.
        (
            (
                'if input["a"]:\n    x = input["n"] + 1\n    if x > 2:\n        return x\n'
                "    return 0\nreturn -1"
            ),
            [{"a": a, "n": n} for a in (True, False) for n in (1, 5)],
        ),
        # A value written out with an expression in it.
        (
            (
                'if input["z"]:\n    return 0\ny = {"a": input["a"]}\nif y["a"] > 1:\n'
                "    return y\nreturn 1"
            ),
            [{"z": z, "a": a} for z in (True, False) for a in (1, 5)],
        ),
        # A loop's test after an if.
        (
            (
                'n = 0\nif input["z"]:\n    return 0\nwhile n < input["k"]:\n    n = n + 1\n'
                "return n"
            ),
            [{"z": z, "k": k} for z in (True, False) for k in (0, 3)],
        ),
    ],
)
def test_a_choice_takes_in_the_choice_a_rule_leads_to(body, inputs):
    compiled = definition(body)["States"]
    for execution_input in inputs:
        env = {"input": execution_input}
        exec("def f(input):\n" + textwrap.indent(body, "    "), env)
        assert asl.run(definition(body), execution_input) == env["f"](execution_input)
    choices = [s for s in compiled.values() if s["Type"] == "Choice"]
    assert any(len(s["Choices"]) > 1 for s in choices)


def test_a_choice_that_leads_back_to_itself_is_taken_in_once():
    body = 'n = 0\nif input["z"]:\n    return 0\nwhile n < 3:\n    n = n + 1\nreturn n'
    compiled = definition(body)["States"]
    assert compiled["if"]["Choices"][1]["Next"] == "while"
    assert compiled["while"]["Choices"][0]["Next"] == "while"
    assert asl.run(definition(body), {"z": False}) == 3


@pytest.mark.parametrize(
    "condition, values, read",
    [
        # Read once, the expression; more than once, bound once in a block.
        ("$a > 1", {"a": "{% $x.a %}"}, "$x.a > 1"),
        ("$a", {"a": True}, "true"),
        (
            "$a > 1 and $a < 5",
            {"a": "{% $x + 1 %}"},
            "($a := $x + 1; $a > 1 and $a < 5)",
        ),
        # A block would bind a before b, which reads the a from before.
        (
            "$a > $b and $a < 5 and $b < 5",
            {"a": "{% $x %}", "b": "{% $a + 1 %}"},
            "$x > ($a + 1) and $x < 5 and ($a + 1) < 5",
        ),
        (
            "$a.k = 1",
            {"a": {"k": "{% $x %}", "l": [1, "s"]}},
            '({"k": $x, "l": [1, "s"]}).k = 1',
        ),
        # What names its state, changes when evaluated, or binds a name read.
        ("$states.context.State.Name = 'x' and $a", {"a": "{% $x %}"}, None),
        ("$a > 1", {"a": "{% $random() %}"}, None),
        ("($a := 1; $a > $b)", {"a": "{% $x %}", "b": "{% $y %}"}, None),
    ],
)
def test_a_choice_reads_what_the_transition_assigns(condition, values, read):
    choice = {
        "Type": "Choice",
        "Choices": [
            {
                "Condition": f"{{% {condition} %}}",
                "Next": "n",
                "Assign": {"c": "{% $a %}"},
            }
        ],
        "Default": "d",
    }
    found = read_as_values(choice, values)
    if read is None:
        assert found is None
        return
    assert found is not None
    rules, default, own = found
    assert rules[0]["Condition"] == f"{{% {read} %}}"
    assert rules[0]["Assign"] == {"c": values["a"]}
    assert (default, own) == ("d", {})


@pytest.mark.parametrize(
    "code, group",
    [
        ("$x.a", "$x.a"),
        ("$count($x)", "$count($x)"),
        ("'s'", "'s'"),
        ("($v := 1; $v)", "($v := 1; $v)"),
        ("($a) + ($b)", "(($a) + ($b))"),
        ("('(' & $a)", "(('(' & $a))"),
        ("$a + 1", "($a + 1)"),
    ],
)
def test_a_value_read_in_place_of_a_variable_is_grouped_where_it_must_be(code, group):
    assert grouped(code) == group


FLAGGED = (
    "from sfnx import state_machine, task\n\n\n"
    "class Declined(Exception):\n    pass\n\n\n"
    "@state_machine\ndef pay(input):\n"
)
CHARGE = 'task("arn:aws:states:::lambda:invoke", {"FunctionName": "charge"})'


def flagged(body: str) -> dict:
    ((_, compiled),) = compile_source(FLAGGED + textwrap.indent(body, "    ")).items()
    return compiled


def test_a_flag_known_on_each_path_sends_the_path_on_directly():
    """The catcher that sets the flag leads where the tests of the flag
    would send it, and so does the Task that succeeds, which leaves it None:
    no Choice is left."""
    body = (
        f"failed = None\ntry:\n    {CHARGE}\nexcept Declined:\n"
        '    failed = "charge"\n'
        f"if failed is None:\n    {CHARGE}\n"
        'if failed == "charge":\n    return "declined"\nreturn "done"'
    )
    compiled = flagged(body)
    states = compiled["States"]
    assert [s["Type"] for s in states.values()].count("Choice") == 0
    first = states["invoke"]
    assert first["Next"] == "invoke_2"
    assert states[first["Catch"][0]["Next"]]["Output"] == "declined"

    def charge(arguments):
        return {}

    def declined(arguments):
        raise asl.Failure("Declined", "")

    assert asl.run(compiled, {}, {"invoke": charge, "invoke_2": charge}) == "done"
    assert asl.run(compiled, {}, {"invoke": declined}) == "declined"


@pytest.mark.parametrize(
    "value, test, kept, result",
    [
        # The rule's assignment goes in the Task that now skips the Choice.
        ('"a"', 'if flag == "a":\n    x = 1\n    return x', False, 1),
        ('"a"', 'if flag != "b":\n    return 1', False, 1),
        ("None", "if flag is not None:\n    return 1", False, 0),
        # JSONata's = holds for the same type only: true is not 1.
        ("True", "if flag == 1:\n    return 1", False, 0),
        ("1.0", "if flag == 1:\n    return 1", False, 1),
    ],
)
def test_what_the_known_value_decides(value, test, kept, result):
    body = f"flag = {value}\n{CHARGE}\n{test}\nreturn 0"
    compiled = flagged(body)
    states = compiled["States"]
    assert any(s["Type"] == "Choice" for s in states.values()) == kept
    assert asl.run(compiled, {}, {"invoke": lambda arguments: {}}) == result


PAST_A_CHOICE = (
    'status = "new"\nprefix = input["prefix"]\ntry:\n'
    f"    found = {CHARGE}\n"
    '    if found["blocked"]:\n        status = "open"\n'
    '    else:\n        status = "done"\n'
    'except Declined:\n    status = "open"\n'
    'if status != "open":\n    return "finished"\n'
    'note = f"{prefix}NOTE"\n'
    f"{CHARGE}\nreturn note"
)


@pytest.mark.parametrize(
    "note, choices",
    [
        # The note reads nothing the catcher or the rule assigns: both go on
        # to the last call, and only the if on the Task's result is left.
        ("!", 1),
        # It reads the status they assign, which the Choice's Default would
        # read after they assign it: the paths keep the Choice.
        ("{status}", 2),
    ],
)
def test_a_path_past_a_choice_takes_the_assignments_of_its_default(note, choices):
    compiled = flagged(PAST_A_CHOICE.replace("NOTE", note))
    states = compiled["States"]
    assert [s["Type"] for s in states.values()].count("Choice") == choices

    def blocked(arguments):
        return {"blocked": True}

    def declined(arguments):
        raise asl.Failure("Declined", "")

    ok = {"invoke": lambda arguments: {}}
    written = "open" if note == "{status}" else "!"
    given = {"prefix": "p:"}
    assert asl.run(compiled, given, {"found": blocked, **ok}) == "p:" + written
    assert asl.run(compiled, given, {"found": declined, **ok}) == "p:" + written
    assert (
        asl.run(compiled, given, {"found": lambda arguments: {"blocked": False}})
        == "finished"
    )


@pytest.mark.parametrize(
    "body, choices, runs",
    [
        # Each branch assigns the one variable: one conditional expression.
        (
            'if input["a"] > 2:\n    x = "big"\nelif input["a"] > 1:\n    x = "some"\nelse:\n    x = "none"',
            0,
            [({"a": 3}, "big"), ({"a": 2}, "some"), ({"a": 0}, "none")],
        ),
        # Without else, the variable keeps the value it had.
        (
            'x = input["x"]\nif x is None:\n    x = 0',
            0,
            [({"x": None}, 0), ({"x": 5}, 5)],
        ),
        # A variable that has no value on the other path keeps the Choice.
        ('if input["a"]:\n    x = 1\nelse:\n    return 0', 1, [({"a": 1}, 1)]),
        # So does a branch that does more than assign it.
        (
            'x = 0\nif input["a"]:\n    x = 1\n    y = 2',
            1,
            [({"a": 1}, 1), ({"a": 0}, 0)],
        ),
        # A flag given a value written in the source and tested later keeps
        # its Choice, for the later test to be decided on each path.
        (
            'x = "no"\nif input["a"]:\n    x = "yes"\nif x == "yes":\n    return 1',
            1,
            [({"a": 1}, 1), ({"a": 0}, "no")],
        ),
    ],
)
def test_an_if_that_only_assigns_one_variable(body, choices, runs):
    compiled = definition(body + "\nreturn x")
    states = compiled["States"]
    assert [s["Type"] for s in states.values()].count("Choice") == choices
    for execution_input, expected in runs:
        assert asl.run(compiled, execution_input) == expected


def test_an_if_whose_branch_makes_a_state_keeps_its_choice():
    body = f'x = 0\nif input["a"]:\n    x = {CHARGE}\nreturn x'
    states = flagged(body)["States"]
    assert [s["Type"] for s in states.values()].count("Choice") == 1


@pytest.mark.parametrize(
    "first, second, merged, result",
    [
        # The new value reads the first where it is always evaluated, and the
        # first is never undefined: it is written into the new one, and both
        # go in one Assign.
        (
            'input["a"].get("b")',
            "x = 0 if not isinstance(x, (int, float)) else x",
            True,
            2,
        ),
        ('input["a"].get("b")', "x = x + 1", True, 3),
        # The first may be undefined, which the test reads without failing
        # where Python fails on the missing key: it keeps its own state.
        (
            'input["a"]["b"]',
            "x = 0 if not isinstance(x, (int, float)) else x",
            False,
            2,
        ),
        # It is read only where JSONata may not evaluate it, or not at all.
        ('input["a"].get("b")', "x = 0 if input['b'] else x", False, 0),
        ('input["a"].get("b")', 'x = input.get("c", x)', False, 5),
        ('input["a"].get("b")', "x = 1", False, 1),
    ],
)
def test_a_name_assigned_again(first, second, merged, result):
    body = f"wait(1)\nx = {first}\n{second}\nwait(1)\nreturn x"
    (compiled,) = compile_source(
        "from sfnx import state_machine, wait\n\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    ).values()
    assert len(compiled["States"]) == (3 if merged else 4)
    assert asl.run(compiled, {"a": {"b": 2}, "b": True, "c": 5}) == result
    if first.endswith('["b"]'):
        with pytest.raises(asl.Failure):
            asl.run(compiled, {"a": {}})


def test_an_if_whose_branch_calls_a_function_directly_keeps_its_choice():
    body = (
        "def fee(amount):\n    return amount * 2\n\n"
        'x = 0\nif input["a"]:\n    x = fee(input["a"])\nreturn x'
    )
    states = definition(body)["States"]
    assert [s["Type"] for s in states.values()].count("Choice") == 1


@pytest.mark.parametrize(
    "code, reads",
    [
        ("x", True),
        ("y", False),
        ("a if x else b", True),
        ("x if a else b", False),
        ("x or a", True),
        ("a or x", False),
        ("not x", True),
        ("x < a < b", True),
        ("a < x", True),
        ("a < b < x", False),
        ("x + 1", True),
        ("1 - x", True),
        ('x["k"]', True),
        ("x.k", True),
        ("isinstance(x, int)", True),
        ("len(x)", True),
        ("max(x, 1)", False),
        ("d.get(k, x)", False),
        ("[x for a in b]", False),
    ],
)
def test_where_an_expression_always_reads_a_variable(code, reads):
    assert always_reads(ast.parse(code, mode="eval").body, "x") == reads
