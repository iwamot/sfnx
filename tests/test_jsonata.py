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
            (
                'a: float = input["a"]\nb: float = input["b"]\n'
                'return jsonata("$x - $y", x=b, y=a)'
            ),
            "($x := $b; $y := $a; $x - $y)",
        ),
        (
            'count: float = input["c"]\nreturn jsonata("$string($c)", c=count)',
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


def test_the_expression_reads_the_variables_it_names():
    compiled = definition('a = 1\nb = jsonata("$a + 1")\nreturn b')
    assert list(compiled["States"]) == ["a", "b", "return"]
    assert compiled["States"]["b"]["Assign"] == {"b": "{% $a + 1 %}"}
    assert asl.run(compiled, {}) == 2


@pytest.mark.parametrize("name", ["値", "café", "aé"])
def test_the_expression_reads_a_variable_named_outside_ascii(name):
    # Step Functions variable names are Unicode identifiers, so an expression
    # written by hand reads one under whatever name it was declared with.
    compiled = definition(f'{name} = 1\nb = jsonata("${name} + 1")\nreturn b')
    assert list(compiled["States"]) == [name, "b", "return"]
    assert compiled["States"]["b"]["Assign"] == {"b": f"{{% ${name} + 1 %}}"}
    assert asl.run(compiled, {}) == 2


def test_the_expression_reads_a_variable_by_the_name_the_definition_gives_it():
    compiled = definition('count = 1\nb = jsonata("$count_val + 1")\nreturn b')
    assert list(compiled["States"]) == ["count", "b", "return"]
    assert asl.run(compiled, {}) == 2


def test_a_function_the_expression_calls_is_not_a_variable():
    # count is written $count_val, so $count is the JSONata function and the
    # two assignments share one state.
    compiled = definition('count = 2\nb = jsonata("$count([1, 2])")\nreturn [count, b]')
    assert list(compiled["States"]) == ["count", "return"]
    assert asl.run(compiled, {}) == [2, 2]


def test_a_name_outside_the_machine_holds_the_expression():
    source = (
        "from sfnx import jsonata, state_machine\n\n"
        "PADDED = \"$pad($s, -5, '0')\"\n"
        "CODE = PADDED\n\n\n"
        "@state_machine\n"
        "def pay(input):\n"
        '    return [jsonata(PADDED, s=input["a"]), jsonata(CODE, s=input["b"])]\n'
    )
    (compiled,) = compile_source(source).values()
    assert asl.run(compiled, {"a": "1", "b": "22"}) == ["00001", "00022"]


def test_the_parameter_of_a_comprehension_is_not_a_variable_it_reads():
    body = 'xs: list = input["xs"]\nreturn [jsonata("$x * 2") for x in xs]'
    assert asl.run(definition(body), {"xs": [1, 2]}) == [2, 4]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return jsonata(input["e"])',
            "jsonata() takes the expression as a string written in the source",
        ),
        (
            "return jsonata()",
            "jsonata() takes the expression as a string written in the source",
        ),
        (
            'return jsonata("$x", 1)',
            "jsonata() takes the expression as a string written in the source",
        ),
        ('return jsonata("$x", **input)', "jsonata() takes each value by its name"),
        ("n = 1\nreturn jsonata(n)", "jsonata() takes the expression as a string"),
        (
            'return jsonata("$x", count=1)',
            "count would hide $count in the expression; choose another name",
        ),
        (
            'return jsonata("$x", states=1)',
            "states would hide $states in the expression",
        ),
        (
            (
                'a: float = input["a"]\nb: float = input["b"]\n'
                'return jsonata("$a - $b", a=b, b=a)'
            ),
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
