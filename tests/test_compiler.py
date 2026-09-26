import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

HEADER = "from sfnx import state_machine\n\n\n"


def compile_one(source: str) -> dict:
    (definition,) = compile_source(textwrap.dedent(source)).values()
    return definition


def machine(body: str) -> str:
    return HEADER + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")


def python(source: str, execution_input: object) -> object:
    namespace: dict[str, object] = {}
    exec(source, namespace)
    function = namespace["pay"]
    assert callable(function)
    return function(execution_input)


def test_succeed_only():
    assert compile_one(machine('return input["amount"]')) == {
        "QueryLanguage": "JSONata",
        "StartAt": "return",
        "States": {
            "return": {
                "Type": "Succeed",
                "Output": "{% $states.context.Execution.Input.amount %}",
            }
        },
    }


def test_a_return_of_an_assignment_is_one_succeed():
    """Nothing reads the variable after the return, so the Output reads its
    expression, and fails where the Pass would."""
    definition = compile_one(machine('amount = input["amount"]\nreturn amount'))
    assert definition == {
        "QueryLanguage": "JSONata",
        "StartAt": "return",
        "States": {
            "return": {
                "Type": "Succeed",
                "Output": "{% $states.context.Execution.Input.amount %}",
            },
        },
    }


def test_independent_assignments_share_a_pass():
    definition = compile_one(
        machine('fee = 10\nrate = input["rate"]\nreturn [fee, rate]')
    )
    assert definition["States"] == {
        "fee": {
            "Type": "Pass",
            "Assign": {"fee": 10, "rate": "{% $states.context.Execution.Input.rate %}"},
            "Next": "return",
        },
        "return": {"Type": "Succeed", "Output": ["{% $fee %}", "{% $rate %}"]},
    }


def test_a_read_of_a_pending_assignment_reads_its_expression():
    """Assign reads the values from before the state, so b reads what a takes
    rather than $a, and the two share a Pass, as a hand-writer spells a path
    out again."""
    definition = compile_one(machine('a = input["n"]\nb = [a]\nc = 2\nreturn b'))
    n = "{% $states.context.Execution.Input.n %}"
    assert definition["States"] == {
        "a": {"Type": "Pass", "Assign": {"a": n, "b": [n], "c": 2}, "Next": "return"},
        "return": {"Type": "Succeed", "Output": "{% $b %}"},
    }


def test_a_read_of_a_value_that_changes_starts_a_new_state():
    """Reading its expression again would give another value."""
    source = "import random\n" + machine("a = random.random()\nb = [a]\nreturn b")
    definition = compile_one(source)
    assert definition["States"]["a"] == {
        "Type": "Pass",
        "Assign": {"a": "{% $random() %}"},
        "Next": "return",
    }
    assert definition["States"]["return"]["Output"] == ["{% $a %}"]


def test_reassignment_starts_a_new_state_with_a_serial_name():
    """The first value is still evaluated, as Python evaluates it."""
    definition = compile_one(machine('x = input["w"]\nx = input["x"]\nreturn [x]'))
    assert list(definition["States"]) == ["x", "x_2", "return"]
    assert definition["States"]["x"]["Next"] == "x_2"


@pytest.mark.parametrize(
    "first",
    ["3", 'input["x"].get("k", 1)', "y"],
)
def test_a_first_value_that_cannot_fail_is_replaced_in_the_same_state(first):
    """A first value that neither fails nor is undefined has nothing for
    Python to evaluate, so the new value takes its place."""
    body = f'y = 2\nn = {first}\ns = "a"\nn = 0\nreturn [n, s, y]'
    definition = compile_one(machine(body))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == [0, "a", 2]


@pytest.mark.parametrize(
    "first",
    ['input["x"]', '10 / input.get("d", 1)', "random.random()"],
)
def test_a_first_value_that_can_fail_or_change_keeps_its_state(first):
    body = f'n = {first}\ns = "a"\nn = 0\nreturn [n, s]'
    definition = compile_one("import random\n" + machine(body))
    assert [s["Type"] for s in definition["States"].values()] == ["Pass", "Succeed"]


@pytest.mark.parametrize(
    "value, output",
    [
        ("[1, 2]", [[1, 2]]),
        ('{"a": 1}', [{"a": 1}]),
        ('[y, "a"]', [[2, "a"]]),
    ],
)
def test_a_list_or_dict_of_values_that_are_never_undefined_goes_in_the_return(
    value, output
):
    definition = compile_one(machine(f"y = 2\nv = {value}\nreturn [v]"))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == output


@pytest.mark.parametrize(
    "value",
    ['[1, input["x"]]', '{"a": input["x"]}', '{"a": 10 / input.get("d", 1)}'],
)
def test_a_list_or_dict_that_may_be_undefined_or_fail_keeps_its_pass(value):
    definition = compile_one(machine(f"v = {value}\nreturn [v]"))
    assert [s["Type"] for s in definition["States"].values()] == ["Pass", "Succeed"]


def test_a_swap_reads_the_pending_values_in_the_same_state():
    definition = compile_one(machine("a = 3\nb = 2\na, b = b, a\nreturn [a, b]"))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == [2, 3]


@pytest.mark.parametrize(
    "before",
    [
        # b reads a, which read as its expression would give another value.
        "a = random.random()\nb = 2",
        # Python evaluates the first a, which fails on a missing key.
        'a = input["x"]\nb = 2',
    ],
)
def test_a_swap_keeps_its_state_where_a_pending_value_cannot_be_shared(before):
    body = f"{before}\na, b = b, a\nreturn [a, b]"
    definition = compile_one("import random\n" + machine(body))
    types = [s["Type"] for s in definition["States"].values()]
    assert types == ["Pass", "Pass", "Succeed"]


@pytest.mark.parametrize(
    "value, output",
    [
        ("0 + 1", 1),
        ("2 * 3", 6),
        ("5 - 7", -2),
        ("-x", -3),
        ('"a" + "b"', "ab"),
        ("x + 1", 4),
        # Python's quotient rounds down, and its remainder takes the sign of
        # the divisor.
        ("7 // 2", 3),
        ("-7 // 2", -4),
        ("-7 % 2", 1),
        ("7 % -2", -1),
        ("x // 1 + 0", 3),
    ],
)
def test_an_operation_on_values_written_in_the_source_is_its_value(value, output):
    definition = compile_one(machine(f"x = 3\nv = {value}\nreturn [v]"))
    assert definition["States"] == {"return": {"Type": "Succeed", "Output": [output]}}


@pytest.mark.parametrize(
    "value",
    [
        # A double holds neither exactly, so JSONata's value may differ.
        "2 ** 53 + 1",
        "9007199254740993 - 1",
        "9007199254740993 // 1",
        "1.5 + 1",
        "7.5 // 2",
        # The input is known only when it runs.
        'input["x"] + 1',
    ],
)
def test_an_operation_on_other_values_stays_an_expression(value):
    output = compile_one(machine(f"return [{value}]"))["States"]["return"]["Output"]
    assert isinstance(output[0], str) and output[0].startswith("{%")


def test_joining_text_cannot_fail_so_it_goes_in_the_return():
    body = 's = input.get("s", "a")\nt = s + "x"\nreturn [t]'
    assert list(compile_one(machine(body))["States"]) == ["return"]


@pytest.mark.parametrize(
    "body, types",
    [
        # c reads a, which read as its expression would give another value.
        ("a = random.random()\nc, d = a, 1\nreturn [c, d]", ["Pass", "Succeed"]),
        # The text of jsonata() reads x by its name, which no expression
        # replaces.
        (
            'x = 1\nc, d = jsonata("$x + 1"), 2\nreturn [c, d]',
            ["Pass", "Pass", "Succeed"],
        ),
    ],
)
def test_an_unpacking_that_cannot_read_a_pending_value_takes_a_state(body, types):
    header = "import random\nfrom sfnx import jsonata, state_machine\n\n\n"
    source = (
        header + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")
    )
    definition = compile_one(source)
    assert [s["Type"] for s in definition["States"].values()] == types


@pytest.mark.parametrize(
    "value, output",
    [("len([1, 2, 3])", 3), ("len([[1], [2]])", 2), ("len([])", 0)],
)
def test_the_length_of_a_list_written_in_the_source_is_its_value(value, output):
    definition = compile_one(machine(f"return [{value}]"))
    assert definition["States"]["return"]["Output"] == [output]


def test_the_length_of_a_list_holding_an_expression_stays_an_expression():
    output = compile_one(machine('return [len([input["x"], 1])]'))["States"]
    assert output["return"]["Output"][0].startswith("{% $count(")


def test_serial_names_skip_names_in_use():
    definition = compile_one(
        machine(
            'x = input["w"]\nx = input["x"]\nx_2 = x\nx_2 = input["y"]\nreturn [x, x_2]'
        )
    )
    assert list(definition["States"]) == ["x", "x_2", "x_2_2", "return"]


def test_implicit_return_is_null():
    definition = compile_one(machine("x = 1"))
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": None}
    definition = compile_one(machine("return"))
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": None}


def test_machine_without_parameter():
    definition = compile_one(HEADER + "@state_machine\ndef hello():\n    return 'hi'")
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": "hi"}


def test_timeout():
    source = HEADER + "@state_machine(timeout=300)\ndef pay(input):\n    return 1"
    assert compile_one(source)["TimeoutSeconds"] == 300
    source = HEADER + "@state_machine()\ndef pay(input):\n    return 1"
    assert "TimeoutSeconds" not in compile_one(source)


@pytest.mark.parametrize(
    "imports, decorator",
    [
        ("import sfnx", "sfnx.state_machine"),
        ("import sfnx as s", "s.state_machine"),
        ("from sfnx import state_machine as machine", "machine"),
        ("import sfnx.other", "sfnx.state_machine"),
    ],
)
def test_import_forms(imports, decorator):
    source = f"{imports}\n\n\n@{decorator}\ndef pay(input):\n    return 1"
    assert list(compile_source(source)) == ["pay"]


def test_every_machine_in_a_module():
    source = (
        HEADER
        + "class Other:\n    pass\n\n\n@registry[0]\ndef helper():\n    pass\n\n\n"
        + "@state_machine\ndef a(input):\n    return 1\n\n\n"
        + "@state_machine\ndef b(input):\n    return 2\n"
    )
    assert list(compile_source(source)) == ["a", "b"]


def test_docstring_and_pass_emit_nothing():
    definition = compile_one(machine('"""Pay."""\npass\nreturn 1'))
    assert list(definition["States"]) == ["return"]


@pytest.mark.parametrize(
    "body, output",
    [
        ('return input["a b"]', "{% $states.context.Execution.Input.`a b` %}"),
        (
            'return input["a`b"]',
            "{% $lookup($states.context.Execution.Input, 'a`b') %}",
        ),
        ("return input[0]", "{% $states.context.Execution.Input[0] %}"),
        ("return input[-1]", "{% $states.context.Execution.Input[-1] %}"),
        ('return input["a"][0]["b"]', "{% $states.context.Execution.Input.a[0].b %}"),
        ('return {"a": 1}["a"]', "{% {'a': 1}.a %}"),
        ("return '{% x %}'", "{% '{% x %}' %}"),
        ("return '{% x'", "{% '{% x' %}"),
        ("return 'x %}'", "{% 'x %}' %}"),
        ("return ' {% x'", " {% x"),
        (
            'return {"a": [1, input], "b": None}',
            {"a": [1, "{% $states.context.Execution.Input %}"], "b": None},
        ),
    ],
)
def test_expressions(body, output):
    assert compile_one(machine(body))["States"]["return"]["Output"] == output


@pytest.mark.parametrize(
    "body, execution_input",
    [
        ('return input["amount"]', {"amount": 5}),
        ('amount = input["amount"]\nreturn amount', {"amount": 5}),
        ('a = input["a"]\nb = [a, input["b"]]\na = 3\nreturn [a, b]', {"a": 1, "b": 2}),
        ('return input["x y"]', {"x y": 1}),
        ('return input["a`b"]', {"a`b": 1}),
        ('return input["m"][1][0]', {"m": [[1, 2], [3, 4]]}),
        ("return input[-1]", [1, 2, 3]),
        (
            'return {"s": "it\'s", "t": "say \\"hi\\" \\\\ ok", "u": "{% x %}", "v": "{% x"}',
            None,
        ),
        ('order = input["order"]\nreturn {"id": order["id"]}', {"order": {"id": "o"}}),
        ('return {"a": True, "b": False, "c": None, "d": 1.5}', None),
    ],
)
def test_asl_matches_python(body, execution_input):
    source = machine(body)
    definition = compile_one(source)
    assert asl.run(definition, execution_input) == python(source, execution_input)


@pytest.mark.parametrize(
    "source, message, location",
    [
        (machine("return missing"), "missing is not assigned here", "6:12"),
        (machine("return 1\nx = 2"), "never reached", "7:5"),
        (machine("x[0] = 1"), "one variable per statement", "6:5"),
        (machine("x = y = 1"), "one variable per statement", "6:9"),
        (machine("return b'x'"), "only JSON values", "6:12"),
        (machine("return 1e999"), "JSON numbers are finite", "6:12"),
        (machine("return {1: 2}"), "keys are strings", "6:13"),
        (machine("return {**1}"), "1 is a number; ** unpacks dicts", "6:15"),
        (machine("return input[True]"), "True is a boolean; keys are strings", "6:18"),
        (machine("with input:\n    pass"), "with is not supported", "6:5"),
        (machine("return input.x"), 'read a key with x["key"]', "6:12"),
        (machine("x" * 81 + " = 1"), "at most 80 characters", "6:5"),
        (
            machine("x" * 80 + ' = input["w"]\n' + "x" * 80 + ' = input["x"]'),
            "longer than 80",
            "7:5",
        ),
        # The spelling is checked too: a renamed name grows, as does one
        # numbered past a name the module uses.
        (
            machine("_" + "1" * 79 + " = 1"),
            "is written value_" + "1" * 79 + " in the definition",
            "6:5",
        ),
        (
            machine("x" * 79 + " = 1\n_" + "x" * 79 + " = 2"),
            "is written " + "x" * 79 + "_2 in the definition",
            "7:5",
        ),
        (
            machine("a: list = input\nfor " + "x" * 76 + " in a:\n    pass"),
            "needs a variable " + "x" * 76 + "_index here",
            "7:5",
        ),
        (HEADER + "def pay(input):\n    return 1", "no state machine here", "1:1"),
        (
            HEADER + "@state_machine\nasync def pay(input):\n    return 1",
            "async def",
            "5:1",
        ),
        (
            HEADER + "@state_machine\n@other\ndef pay(input):\n    return 1",
            "no other decorators",
            "6:1",
        ),
        (
            HEADER + "@state_machine(300)\ndef pay(input):\n    return 1",
            "by keyword",
            "4:2",
        ),
        (
            HEADER + "@state_machine(name='x')\ndef pay(input):\n    return 1",
            "takes only timeout",
            "4:16",
        ),
        (
            HEADER + "@state_machine(timeout=0)\ndef pay(input):\n    return 1",
            "positive number",
            "4:24",
        ),
        (
            HEADER + "@state_machine(timeout=1.5)\ndef pay(input):\n    return 1",
            "positive number",
            "4:24",
        ),
        (
            HEADER + "@state_machine\ndef pay(a, b):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            HEADER + "@state_machine\ndef pay(input=1):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            HEADER + "@state_machine\ndef pay(*input):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            machine('return context["Execution"]["Id"]'),
            "context is not imported; write from sfnx import context",
            "6:12",
        ),
        (
            machine('task("arn:aws:states:::lambda:invoke", {"FunctionName": "f"})'),
            "task is not imported; write from sfnx import task",
            "6:5",
        ),
        (machine("x = wait(1)"), "wait is not imported", "6:9"),
        (
            HEADER
            + "def task():\n    x = 1\n    return x\n\n\n@state_machine\ndef pay(input):\n"
            + "    x = task() + 1",
            "task() runs its body here, as states",
            "11:9",
        ),
        ("from sfnx import *\n", "instead of *", "1:1"),
        ("def broken(:\n", "invalid syntax", "1:12"),
        # A column counts characters, not the bytes of the UTF-8 the parser
        # reads: the caret names frozenset on each of these lines.
        (
            machine('return {"日本語": frozenset(input)}'),
            "calling frozenset() is not supported",
            "6:20",
        ),
        (
            machine('return {"🙂": frozenset(input)}'),
            "calling frozenset() is not supported",
            "6:18",
        ),
        (
            HEADER
            + '@state_machine\ndef pay(input):\n\treturn {"日": frozenset(input)}',
            "calling frozenset() is not supported",
            "6:15",
        ),
        # Python counts the column of a syntax error in characters already.
        (HEADER + 'x = {"日本語": 1}\ny = 日本語語 ? 3\n', "invalid syntax", "5:10"),
    ],
)
def test_diagnostics(source, message, location):
    with pytest.raises(CompileError) as raised:
        compile_source(source, "app.py")
    assert message in raised.value.message
    assert str(raised.value).startswith(f"app.py:{location}: ")


def test_relative_imports_are_not_sfnx():
    source = (
        "from . import state_machine\n\n\n@state_machine\ndef pay(input):\n    return 1"
    )
    with pytest.raises(CompileError, match="no state machine here"):
        compile_source(source)


def test_names_do_not_depend_on_lines():
    source = machine('amount = input["amount"]\nreturn amount')
    assert compile_source(source) == compile_source("\n\n" + source)


@pytest.mark.parametrize(
    "body, states",
    [
        # The return reads the assignment, or leaves one that cannot fail.
        ('x = input["a"]\nreturn x', ["return"]),
        ('x = input.get("a")\nreturn [x]', ["return"]),
        ("x = 1", ["return"]),
        # Python evaluates an assignment the return does not read, which may
        # fail, and one that changes on evaluation is evaluated once.
        ('x = input["a"]\nreturn 1', ["x", "return"]),
        ("x = random.random()\nreturn [x, x]", ["x", "return"]),
    ],
)
def test_a_return_after_assignments(body, states):
    definition = compile_one("import random\n" + machine(body))
    assert list(definition["States"]) == states
