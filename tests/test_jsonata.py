import textwrap

import pytest

import sfnx
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.translate import settled
from tests import asl

INPUT = "$states.context.Execution.Input"


def definition(body: str) -> dict:
    source = "from sfnx import jsonata, state_machine, wait\n\n\n@state_machine\n"
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


# The first Wait takes the value from the input, and the second holds the
# expression that reads it, as it is written.
READ_AFTER_WAIT = (
    '{name}: int = input["a"]\nwait(1)\nwait(1)\nb = jsonata("{text}")\nreturn [b]'
)


def test_the_expression_reads_the_variables_it_names():
    compiled = definition(READ_AFTER_WAIT.format(name="a", text="$a + 1"))
    assert compiled["States"]["wait_2"]["Assign"] == {"b": "{% $a + 1 %}"}
    assert asl.run(compiled, {"a": 1}) == [2]


@pytest.mark.parametrize("name", ["値", "café", "aé"])
def test_the_expression_reads_a_variable_named_outside_ascii(name):
    # Step Functions variable names are Unicode identifiers, so an expression
    # written by hand reads one under whatever name it was declared with.
    compiled = definition(READ_AFTER_WAIT.format(name=name, text=f"${name} + 1"))
    assert compiled["States"]["wait_2"]["Assign"] == {"b": f"{{% ${name} + 1 %}}"}
    assert asl.run(compiled, {"a": 1}) == [2]


def test_the_expression_reads_a_variable_by_the_name_the_definition_gives_it():
    compiled = definition(READ_AFTER_WAIT.format(name="count", text="$count_val + 1"))
    assert compiled["States"]["wait_2"]["Assign"] == {"b": "{% $count_val + 1 %}"}
    assert asl.run(compiled, {"a": 1}) == [2]


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


@pytest.mark.parametrize(
    "written, bound, variables, expected",
    [
        # Only the names the call binds, and functions called or passed.
        ("$replace($s, /-|:|\\s/, '')", {"s"}, set(), True),
        ("$map($xs, $string)", {"xs"}, set(), True),
        ("$uppercase ($s)", {"s"}, set(), True),
        # Names the text binds itself, under names no variable has.
        ("$map($xs, function($v) { $v + 1 })", {"xs"}, set(), True),
        ("($t := $s & 'x'; $t)", {"s"}, set(), True),
        ("($t := 1; $t)", set(), {"t"}, False),
        # A variable read by its name, the input, and the state.
        ("$x + 1", set(), {"x"}, False),
        ("$ + 1", set(), set(), False),
        ("$$.a", set(), set(), False),
        ("$states.input.a", set(), set(), False),
        # What gives another value each time, called or bound to a name.
        ("$random()", set(), set(), False),
        ("($f := $millis; $f())", set(), set(), False),
        ("$eval($s)", {"s"}, set(), False),
        # A $ in a regular expression or a string reads as a name.
        ("/^a$/i($s)", {"s"}, set(), False),
        ("'$x' & $s", {"s"}, set(), False),
    ],
)
def test_an_expression_that_reads_only_its_values_is_settled(
    written, bound, variables, expected
):
    assert settled(written, bound, variables) is expected


def test_a_settled_expression_goes_where_other_values_go():
    """It is not read again elsewhere, so it needs no Pass of its own, and
    one read twice is written twice, as any value that does not change."""
    body = (
        'if input["a"]:\n    return 0\n'
        'up = jsonata("$uppercase($s)", s=input["s"])\nreturn [up, up]'
    )
    compiled = definition(body)["States"]
    assert list(compiled) == ["if", "return", "return_2"]
    assert asl.run(definition(body), {"a": False, "s": "x"}) == ["X", "X"]


def test_an_unsettled_expression_read_twice_is_evaluated_once():
    """It goes in the Choice's own Assign, which runs on the Default, and the
    return reads the variable twice, as Python reads the one value."""
    body = 'if input["a"]:\n    return 0\nr = jsonata("$random()")\nreturn [r, r]'
    compiled = definition(body)["States"]
    assert compiled["if"]["Assign"] == {"r": "{% $random() %}"}
    first, second = asl.run(definition(body), {"a": False})
    assert first == second


def test_a_string_in_the_text_that_spells_a_starting_variable_stays_as_it_is():
    """The start's value would go in the return where it reads the variable,
    and the text is not read: '$n' is a string, not n."""
    compiled = definition("n = 3\nreturn jsonata(\"'$n' & $string($n)\")")
    assert asl.run(compiled, {}) == "$n3"


@pytest.mark.parametrize(
    "first, text, execution_input, output",
    [
        # A string that spells x is not a read of it, and stays as it is.
        ("x = 1", "'$x' & $string($x)", {}, "$x1"),
        # A dict holding an expression is no one expression to read x as.
        ('x = {"a": input["a"]}', "$x.a + 1", {"a": 1}, 2),
        # The text reads x and binds it too, which the value would take over.
        ("x = 1", "$x + ($x := 2; $x)", {}, 3),
    ],
)
def test_an_assignment_the_state_before_cannot_read_keeps_its_state(
    first, text, execution_input, output
):
    body = f'{first}\ny = jsonata("{text}")\nreturn [y, 1]'
    compiled = definition(body)
    assert [s["Type"] for s in compiled["States"].values()][:2] == ["Pass", "Pass"]
    assert asl.run(compiled, execution_input) == [output, 1]


@pytest.mark.parametrize("text, output", [("($x := 2; $x)", 2), ("'$x'", "$x")])
def test_a_name_the_text_binds_or_spells_is_not_a_read(text, output):
    """The text binds x for itself, or spells it in a string, so nothing reads
    the x assigned before it, which is not assigned."""
    compiled = definition(f'x = 1\ny = jsonata("{text}")\nreturn [y, 1]')
    assert all("x" not in s.get("Assign", {}) for s in compiled["States"].values())
    assert asl.run(compiled, {}) == [output, 1]


def test_a_return_of_a_value_that_changes_keeps_its_pass():
    """The return is the variable itself, whose value changes on each
    evaluation, so the Pass evaluates it once, as Python does."""
    compiled = definition('b = jsonata("$random()")\nreturn b')
    assert [s["Type"] for s in compiled["States"].values()] == ["Pass", "Succeed"]


@pytest.mark.parametrize(
    "value, text",
    [
        # s changes on each evaluation, so t reads the value s holds.
        ('jsonata("$string($random())")', "$s"),
        # The text reads s and binds it too, which the value would take over.
        ('"a"', "$s & ($s := 'b'; $s)"),
    ],
)
def test_the_way_into_a_loop_keeps_a_round_it_cannot_read(value, text):
    """The way in assigns s, and the first round, which the way in decides,
    reads it, so the way in keeps to its own values."""
    body = (
        f's = {value}\nt = "none"\nc = 0\nwhile True:\n    c = c + 1\n'
        f'    if c > 2:\n        return [s, t]\n    t = jsonata("{text}")\n'
    )
    compiled = definition(body)
    assert compiled["States"]["s"]["Assign"]["c"] == 1
    s, t = asl.run(compiled, {})
    assert t == (s if text == "$s" else "ab")
