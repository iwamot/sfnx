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


def test_pass_then_succeed():
    definition = compile_one(machine('amount = input["amount"]\nreturn amount'))
    assert definition == {
        "QueryLanguage": "JSONata",
        "StartAt": "amount",
        "States": {
            "amount": {
                "Type": "Pass",
                "Assign": {"amount": "{% $states.context.Execution.Input.amount %}"},
                "Next": "return",
            },
            "return": {"Type": "Succeed", "Output": "{% $amount %}"},
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


def test_a_read_of_a_pending_assignment_starts_a_new_state():
    definition = compile_one(machine("a = 1\nb = [a]\nc = 2\nreturn b"))
    assert definition["States"] == {
        "a": {"Type": "Pass", "Assign": {"a": 1}, "Next": "b"},
        "b": {"Type": "Pass", "Assign": {"b": ["{% $a %}"], "c": 2}, "Next": "return"},
        "return": {"Type": "Succeed", "Output": "{% $b %}"},
    }


def test_reassignment_starts_a_new_state_with_a_serial_name():
    definition = compile_one(machine("x = 1\nx = 2\nreturn x"))
    assert list(definition["States"]) == ["x", "x_2", "return"]
    assert definition["States"]["x"]["Next"] == "x_2"


def test_serial_names_skip_names_in_use():
    definition = compile_one(machine("x = 1\nx = 2\nx_2 = x\nreturn x"))
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
        (machine("x" * 80 + " = 1\n" + "x" * 80 + " = 2"), "longer than 80", "7:5"),
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
            + "def task():\n    return 1\n\n\n@state_machine\ndef pay(input):\n    x = task()",
            "task() cannot be called directly",
            "10:9",
        ),
        ("from sfnx import *\n", "instead of *", "1:1"),
        ("def broken(:\n", "invalid syntax", "1:12"),
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
