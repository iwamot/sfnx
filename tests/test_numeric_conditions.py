"""Numeric Choice simplification preserves ASL outcomes, including failures."""

import textwrap

from hypothesis import given, settings
from hypothesis import strategies as st

from sfnx import testing
from sfnx.compiler import both, compile_source, definitions, emitted, from_asl
from sfnx.syntax import EXACT, stronger_numeric_test

PATH = "$states.context.Execution.Input.x"
BOUNDS = st.one_of(
    st.integers(-EXACT + 1, EXACT - 1),
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
)
SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-EXACT, EXACT),
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)
VALUES = st.recursive(
    SCALARS,
    lambda s: st.one_of(
        st.lists(s, max_size=3), st.dictionaries(st.text(max_size=3), s, max_size=3)
    ),
    max_leaves=8,
)


def choice(condition):
    return {
        "QueryLanguage": "JSONata",
        "StartAt": "test",
        "States": {
            "test": {
                "Type": "Choice",
                "Choices": [{"Condition": "{% " + condition + " %}", "Next": "yes"}],
                "Default": "no",
            },
            "yes": {"Type": "Succeed", "Output": True},
            "no": {"Type": "Succeed", "Output": False},
        },
    }


def agrees(before, after, data):
    expected = testing.run(before, data)
    actual = testing.run(after, data)
    assert actual.error == expected.error
    if actual.error is None:
        assert actual.output == expected.output


@settings(max_examples=500, derandomize=True, database=None, deadline=None)
@given(
    BOUNDS, BOUNDS, st.booleans(), st.booleans(), st.booleans(), VALUES, st.booleans()
)
def test_numeric_conditions_preserve_choice_results_and_failures(
    a, b, lower, strict_a, strict_b, value, present
):
    direction = ">" if lower else "<"
    first = f"{PATH} {direction}{'' if strict_a else '='} {a}"
    second = f"{PATH} {direction}{'' if strict_b else '='} {b}"
    simplified = stronger_numeric_test(first, second)
    assert simplified is not None
    before = choice(first + " and " + second)
    after = choice(simplified)
    # A copied rule is followed by the original first test, so a missing
    # value still fails the Choice before any transition is taken.
    for definition in (before, after):
        definition["States"]["test"]["Choices"].append(
            {"Condition": "{% " + first + " %}", "Next": "no"}
        )
    joined = emitted(
        both(
            from_asl("{% " + first + " %}"),
            from_asl("{% " + second + " %}"),
            first_follows=True,
        )
    )
    assert joined == "{% " + simplified + " %}"
    agrees(before, after, {"x": value} if present else {})


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(
    st.lists(st.tuples(st.integers(-20, 20), st.booleans()), min_size=2, max_size=10),
    st.booleans(),
    VALUES,
    st.booleans(),
)
def test_nested_dsl_tests_match_a_handwritten_asl_chain(bounds, lower, value, present):
    direction = ">" if lower else "<"
    body = "return -1"
    states = {"done": {"Type": "Succeed", "Output": -1}}
    for i in reversed(range(len(bounds))):
        number, strict = bounds[i]
        op = direction + ("" if strict else "=")
        body = (
            f'if input["x"] {op} {number}:\n'
            + textwrap.indent(body, "    ")
            + f"\nreturn {i}"
        )
        states[f"test{i}"] = {
            "Type": "Choice",
            "Choices": [
                {
                    "Condition": f"{{% {PATH} {op} {number} %}}",
                    "Next": f"test{i + 1}" if i + 1 < len(bounds) else "done",
                }
            ],
            "Default": f"stop{i}",
        }
        states[f"stop{i}"] = {"Type": "Succeed", "Output": i}
    source = (
        "from sfnx import state_machine\n@state_machine\ndef main(input):\n"
        + textwrap.indent(body, "    ")
    )
    (compiled,) = definitions(source, "<nested-tests>", False, checking=True).values()
    reference = {"QueryLanguage": "JSONata", "StartAt": "test0", "States": states}
    agrees(reference, compiled, {"x": value} if present else {})
    for _, state in testing.every_state(compiled):
        for rule in state.get("Choices", []):
            assert " and " not in rule["Condition"]


def test_numeric_conditions_keep_assignment_provenance():
    # The checks after every pass compare the assignments each expression
    # reads, including the values read by a comparison that disappears.
    source = """from sfnx import state_machine
@state_machine
def main(input):
    n: int = input["n"]
    if n > 1:
        if n >= 2:
            return 2
    return 0
"""
    (compiled,) = definitions(source, "<provenance>", False, checking=True).values()
    (unchecked,) = compile_source(source).values()
    assert compiled == unchecked
    assert testing.run(compiled, {"n": 2}).output == 2


def test_a_standalone_conjunction_keeps_its_undefined_behavior():
    first = f"{PATH} > 1"
    second = f"{PATH} > 2"
    joined = emitted(
        both(from_asl("{% " + first + " %}"), from_asl("{% " + second + " %}"))
    )
    assert joined == "{% " + first + " and " + second + " %}"
    assert testing.run(choice(first + " and " + second), {}).output is False
    assert testing.run(choice(second), {}).error == "States.QueryEvaluationError"
