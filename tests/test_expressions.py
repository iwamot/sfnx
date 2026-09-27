import pytest

from sfnx.compiler import as_expr, composed, emitted
from sfnx.expressions import (
    CHANGES,
    Expr,
    code_of,
    expression,
    literal,
    template_of,
    written,
)


def test_a_field_reads_alike_as_an_expr_or_as_its_template():
    value = expression("$a + 1")
    assert template_of(value) == "{% $a + 1 %}"
    assert code_of(value) == code_of("{% $a + 1 %}") == "$a + 1"
    assert not written(value)
    assert not written({"k": [value]})


def test_a_value_written_out_has_no_code():
    assert code_of(literal(1)) is None
    assert code_of({"k": "{% $a %}"}) is None
    assert written(literal("s"))
    assert template_of(2) == 2


def test_a_definition_is_emitted_with_each_expr_as_its_template():
    states = {"p": {"Type": "Pass", "Assign": {"x": expression("$a")}, "End": True}}
    assert emitted({"StartAt": "p", "States": states}) == {
        "StartAt": "p",
        "States": {"p": {"Type": "Pass", "Assign": {"x": "{% $a %}"}, "End": True}},
    }


def exact(code: str, defined: bool, total: bool, volatile: int = 0) -> Expr:
    return expression(code, defined=defined, total=total, volatile=volatile)


@pytest.mark.parametrize(
    "value, defined, total",
    [
        # A value like a variable keeps what the field's expression was.
        (exact("$y", True, True), True, True),
        # One that may be undefined may make the code undefined, and may fail
        # where the expression met undefined for a variable's value.
        (exact("$y.a", False, True), False, False),
        # One that may fail makes the code fail, and is still never undefined.
        (exact("$y + 1", True, False), True, False),
    ],
)
def test_a_value_read_in_place_passes_its_properties_on(value, defined, total):
    leaf = exact("$x = 1", True, True)
    code = composed(leaf, f"{value.code} = 1", [value])
    assert (code.defined, code.total) == (defined, total)


def test_a_value_that_changes_makes_the_code_change():
    leaf = exact("$x + 1", True, True)
    value = exact("$random()", True, True, CHANGES)
    assert composed(leaf, "$random() + 1", [value]).volatile == CHANGES


def test_a_template_s_properties_are_not_known():
    code = composed("{% $x = 1 %}", "$y = 1", [exact("$y", True, True)])
    assert (code.defined, code.total, code.volatile) == (False, False, 0)
    assert composed("{% $x %}", "$random()", []).volatile == CHANGES


def test_a_field_is_an_expr_as_it_is_or_as_its_template():
    leaf = exact("$x", True, True)
    assert as_expr(leaf) is leaf
    assert as_expr("{% $x %}").code == "$x"
    assert as_expr({"k": "{% $x %}", "n": 1}).code == '{"k": $x, "n": 1}'
