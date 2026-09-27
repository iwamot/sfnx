from sfnx.compiler import emitted
from sfnx.expressions import code_of, expression, literal, template_of, written


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
