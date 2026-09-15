import textwrap

import pytest

import sfnx
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"


def definition(body: str) -> dict:
    source = "from sfnx import jsonata, state_machine\n\n\n@state_machine\n"
    source += "def pay(input):\n" + textwrap.indent(body, "    ")
    (compiled,) = compile_source(source).values()
    return compiled


@pytest.mark.parametrize(
    "body, code",
    [
        (
            'return jsonata("$uppercase($states.input.code)")',
            "$uppercase($states.input.code)",
        ),
        (
            'code: str = input["code"]\nreturn jsonata("$pad($s, -5, \'0\')", s=code)',
            "($s := $code; $pad($s, -5, '0'))",
        ),
        (
            'return jsonata("$formatNumber($x, \'#,##0\')", x=input["n"] + 1)',
            f"($x := ({INPUT}.n + 1); $formatNumber($x, '#,##0'))",
        ),
        ('return jsonata("1 + 1") * 3', "(1 + 1) * 3"),
        (
            'a = 1\nb = 2\nreturn jsonata("$x - $y", x=b, y=a)',
            "($x := $b; $y := $a; $x - $y)",
        ),
        (
            'count = 2\nreturn jsonata("$string($c)", c=count)',
            "($c := $count_val; $string($c))",
        ),
    ],
)
def test_jsonata_writes_the_expression(body, code):
    assert definition(body)["States"]["return"]["Output"] == "{% " + code + " %}"


def test_jsonata_evaluates():
    body = (
        'items: list = input["items"]\n'
        'return [jsonata("$pad($s, -$n, \'0\')", s=input["code"], n=len(items) + 3), '
        'jsonata("$formatNumber($x, \'#,##0.00\')", x=input["amount"]), '
        '[jsonata("$v * 2", v=item) for item in items]]'
    )
    execution_input = {"code": "42", "items": [1, 2], "amount": 1234.5}
    assert asl.run(definition(body), execution_input) == ["00042", "1,234.50", [2, 4]]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return jsonata(input["e"])',
            "jsonata() takes the expression as a literal string",
        ),
        ("return jsonata()", "jsonata() takes the expression as a literal string"),
        (
            'return jsonata("$x", 1)',
            "jsonata() takes the expression as a literal string",
        ),
        ('return jsonata("$x", **input)', "jsonata() takes each value by its name"),
        (
            'return jsonata("$x", count=1)',
            "count would hide $count in the expression; choose another name",
        ),
        (
            'return jsonata("$x", states=1)',
            "states would hide $states in the expression",
        ),
        (
            'a = 1\nb = 2\nreturn jsonata("$a - $b", a=b, b=a)',
            "b reads a, which jsonata() binds before it",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        definition(body)
    assert message in raised.value.message


def test_jsonata_without_import():
    source = "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
    with pytest.raises(CompileError) as raised:
        compile_source(source + '    return jsonata("1")\n')
    assert (
        raised.value.message
        == "jsonata is not imported; write from sfnx import jsonata"
    )


def test_jsonata_does_not_run_in_python():
    with pytest.raises(NotImplementedError, match="runs in Step Functions"):
        sfnx.jsonata("1")
