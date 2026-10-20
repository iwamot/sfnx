from dataclasses import replace

import pytest

from sfnx.compiler import as_expr, composed, emitted, from_asl, loose_expressions
from sfnx.expressions import (
    ADD,
    Expr,
    binary,
    code_of,
    expression,
    literal,
    template_of,
    written,
)
from sfnx.jsontypes import STRING, of


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


def exact(code: str, defined: bool, total: bool) -> Expr:
    return expression(code, defined=defined, total=total)


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


HERE = frozenset({("a", 1)})
THERE = frozenset({("a", 2)})


@pytest.mark.parametrize(
    "leaf, value, fails",
    [
        # Code that fails for no value fails only where the value fails.
        (exact("$x = 1", True, True), exact("$y + 1", True, False), {THERE}),
        (exact("$x = 1", True, True), exact("$y", True, True), set()),
        # Code that may fail fails where it was and where the value fails.
        (exact("$x + 1", True, False), exact("$y + 1", True, False), {HERE, THERE}),
    ],
)
def test_a_value_read_in_place_fails_where_it_failed(leaf, value, fails):
    leaf = replace(leaf, fails=frozenset({HERE}))
    value = replace(value, fails=frozenset({THERE}))
    assert composed(leaf, "$y + 1 = 1", [value]).fails == fails


@pytest.mark.parametrize(
    "code, volatile",
    [
        ("$random() + 1", True),
        ("$map($xs, $uuid)", True),
        # A string that spells the name does not call it.
        ("'$random' & $x", False),
    ],
)
def test_the_syntax_tree_says_whether_code_changes(code, volatile):
    assert expression(code).volatile == volatile


def test_a_value_that_changes_makes_the_code_change_as_its_syntax_tree_says():
    leaf = exact("$x + 1", True, True)
    value = exact("($r := $random; $r())", True, True)
    code = composed(leaf, "($r := $random; $r()) + 1", [value])
    assert code.volatile


def test_a_template_s_properties_are_not_known():
    code = composed("{% $x = 1 %}", "$y = 1", [exact("$y", True, True)])
    assert (code.defined, code.total, code.volatile) == (False, False, False)
    assert composed("{% $x %}", "$random()", []).volatile


def test_a_field_is_an_expr_as_it_is_or_as_its_template():
    leaf = exact("$x", True, True)
    assert as_expr(leaf) is leaf
    assert as_expr({"k": "{% $x %}", "n": 1}).code == '{"k": $x, "n": 1}'


ASL = {
    "StartAt": "p",
    "States": {
        "p": {
            "Type": "Pass",
            "Comment": "{% not an expression %}",
            "Assign": {"x": "{%  $a  %}", "y": [1, "{% $b %}"], "z": "text"},
            "Next": "c",
        },
        "c": {
            "Type": "Choice",
            "Choices": [{"Condition": "{% $x > 1 %}", "Next": "r"}],
            "Default": "r",
        },
        "r": {"Type": "Succeed", "Output": {"k": "{% $y %}"}},
    },
}


def test_a_definition_written_as_asl_reads_in_and_writes_out_as_it_was():
    read = from_asl(ASL)
    assert loose_expressions(ASL) == [
        "{%  $a  %}",
        "{% $b %}",
        "{% $x > 1 %}",
        "{% $y %}",
    ]
    assert loose_expressions(read) == []
    assert emitted(read) == ASL
    assert isinstance(read, dict)
    assert as_expr(read["States"]["p"]["Assign"]["x"]).code == "$a"


X = expression("$x", frozenset({"x"}), type=of(STRING), defined=True, total=True)


def joined(left: Expr, right: Expr) -> Expr:
    return binary(left, "&", right, ADD, of(STRING))


@pytest.mark.parametrize(
    "left, right, code",
    [
        # Text written in the source after text written in the source.
        (joined(X, literal("a")), "b", "$x & 'ab'"),
        (joined(joined(literal("p"), X), literal("a")), "b", "'p' & $x & 'ab'"),
        (joined(X, literal("it's")), "\n\\é", '$x & "it\'s\\n\\\\é"'),
        (joined(X, literal("a")), "", "$x & 'a'"),
        (joined(X, literal("${A}")), "-b", "$x & '${A}-b'"),
        # No text written in the source to join to.
        (X, "b", "$x & 'b'"),
        (joined(X, expression("$y")), "b", "$x & $y & 'b'"),
        (expression("($x & 'a')"), "b", "($x & 'a') & 'b'"),
        (expression('$x & "a"'), "b", "$x & \"a\" & 'b'"),
        # Joined, the texts would spell a placeholder neither spells.
        (joined(X, literal("a$")), "{X}", "$x & 'a$' & '{X}'"),
        (literal("a$"), "{X}", "'a$' & '{X}'"),
    ],
)
def test_text_written_in_the_source_joins_the_text_before_it(left, right, code):
    assert joined(left, literal(right)).code == code


@pytest.mark.parametrize(
    "left",
    [
        joined(X, literal("a")),
        # A jsonata() expression, whose type is not known.
        expression("$x & 'a'", frozenset({"x"})),
    ],
)
def test_text_joined_to_an_expression_is_typed_as_the_operation(left):
    value = joined(left, literal("b"))
    assert value.template == "{% $x & 'ab' %}"
    assert (value.variables, value.type, value.defined, value.total) == (
        frozenset({"x"}),
        of(STRING),
        left.defined,
        left.total,
    )


def test_text_joined_to_an_operation_on_other_values_stays_an_operation():
    assert binary(joined(X, literal("a")), "=", literal("b"), ADD, None).code == (
        "$x & 'a' = 'b'"
    )
