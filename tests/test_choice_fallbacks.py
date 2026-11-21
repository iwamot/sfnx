"""Exits from nested numeric tests stay ordered without repeated prefixes."""

import textwrap

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sfnx import testing
from sfnx.compiler import Rounds, definitions, emitted, take_in_choices
from sfnx.syntax import opposite_numeric_test
from tests.test_numeric_conditions import BOUNDS, PATH, VALUES, agrees, choice


@settings(max_examples=500, derandomize=True, database=None, deadline=None)
@given(BOUNDS, st.sampled_from([">", ">=", "<", "<="]), VALUES, st.booleans())
def test_opposite_numeric_comparisons_preserve_missing_values_and_failures(
    bound, op, value, present
):
    code = f"{PATH} {op} {bound}"
    opposite = opposite_numeric_test(code)
    assert opposite is not None
    before, after = choice(code), choice(opposite)
    after["States"]["test"]["Choices"][0]["Next"] = "no"
    after["States"]["test"]["Default"] = "yes"
    agrees(before, after, {"x": value} if present else {})


@pytest.mark.parametrize(
    "code, expected",
    [
        ("$x>1", "$x<=1"),
        (" $x >= -1.5 ", " $x < -1.5 "),
        ("$入力.`a > b` < 0", "$入力.`a > b` >= 0"),
        ("$x <= 1e3", "$x > 1e3"),
        ("$random() > 0", None),
        ("($x > 0)", None),
        ("$x[0] > 0", None),
        ("$x > 9007199254740992", None),
    ],
)
def test_opposite_conditions_keep_operand_spelling(code, expected):
    assert opposite_numeric_test(code) == expected


def nested(bounds):
    body = "return -1"
    states = {"done": {"Type": "Succeed", "Output": -1}}
    for i in reversed(range(len(bounds))):
        op, bound = bounds[i]
        body = (
            f'if input["v{i}"] {op} {bound}:\n'
            + textwrap.indent(body, "    ")
            + f"\nreturn {i}"
        )
        states[f"test{i}"] = {
            "Type": "Choice",
            "Choices": [
                {
                    "Condition": f"{{% $states.context.Execution.Input.v{i} {op} {bound} %}}",
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
    (compiled,) = definitions(
        source, "<independent-tests>", False, checking=True
    ).values()
    return {"QueryLanguage": "JSONata", "StartAt": "test0", "States": states}, compiled


@settings(max_examples=300, derandomize=True, database=None, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from([">", ">=", "<", "<="]), st.integers(-20, 20)),
        min_size=2,
        max_size=8,
    ),
    st.dictionaries(st.integers(0, 7), VALUES, max_size=8),
)
def test_independent_nested_tests_match_handwritten_asl(bounds, values):
    reference, compiled = nested(bounds)
    agrees(reference, compiled, {f"v{i}": value for i, value in values.items()})
    for _, state in testing.every_state(compiled):
        for rule in state.get("Choices", []):
            assert " and " not in rule["Condition"]


def test_each_exit_is_reachable_in_its_original_order():
    reference, compiled = nested([(">", 0)] * 16)
    data = {f"v{i}": 1 for i in range(16)}
    agrees(reference, compiled, data)
    assert testing.run(compiled, data).output == -1
    for i in range(16):
        data[f"v{i}"] = 0
        # A later missing field must not be read after this test exits.
        candidate = {k: v for k, v in data.items() if k != f"v{i + 1}"}
        agrees(reference, compiled, candidate)
        assert testing.run(compiled, candidate).output == i
        data[f"v{i}"] = 1


@pytest.mark.parametrize(
    "location, extra",
    [
        ("rule", {"Assign": {"n": 1}}),
        ("rule", {"Output": 2}),
        ("rule", {"Comment": "The positive path."}),
        ("default", {"Assign": {"n": 1}}),
        ("default", {"Output": 2}),
    ],
)
def test_a_transition_with_its_own_fields_keeps_its_direction(location, extra):
    definition = choice("$states.context.Execution.Input.x > 0")
    definition["States"]["test"]["Choices"][0]["Next"] = "inner"
    holder = definition["States"]["test"]
    if location == "rule":
        holder = holder["Choices"][0]
    holder.update(extra)
    definition["States"]["inner"] = {
        "Type": "Choice",
        "Choices": [
            {"Condition": "{% $states.context.Execution.Input.y > 0 %}", "Next": "yes"}
        ],
        "Default": "no",
    }
    take_in_choices(definition, Rounds())
    assert "x > 0" in definition["States"]["test"]["Choices"][0]["Condition"].template


def test_same_path_bounds_keep_the_strongest_test_first():
    from tests.optimization import workloads

    workload = next(w for w in workloads() if w.id == "nested-16")
    (compiled,) = definitions(
        workload.source, workload.id, False, checking=True
    ).values()
    first = compiled["States"][compiled["StartAt"]]["Choices"][0]
    assert first["Condition"].endswith("x > 15 %}")
    assert testing.run(compiled, {"x": 16}).output == -1


def test_a_constant_rule_continues_without_a_numeric_opposite():
    definition = choice("unused")
    definition["States"]["test"]["Choices"] = [{"Condition": True, "Next": "inner"}]
    definition["States"]["inner"] = choice("$states.context.Execution.Input.y > 0")[
        "States"
    ]["test"]
    take_in_choices(definition, Rounds())
    assert testing.run(definition, {"y": 1}).output is True
    assert testing.run(definition, {"y": 0}).output is False
    assert testing.run(definition, {}).error == "States.QueryEvaluationError"


def test_a_cyclic_continuation_keeps_its_direction():
    definition = choice("$states.context.Execution.Input.x > 0")
    definition["States"]["test"]["Choices"][0]["Next"] = "inner"
    definition["States"]["inner"] = {
        "Type": "Choice",
        "Choices": [
            {
                "Condition": "{% $states.context.Execution.Input.y > 0 %}",
                "Next": "inner",
            }
        ],
        "Default": "yes",
    }
    take_in_choices(definition, Rounds())
    condition = definition["States"]["test"]["Choices"][0]["Condition"]
    assert "x > 0" in condition.template
    assert testing.run(emitted(definition), {"x": 0}).output is False
    assert testing.run(emitted(definition), {"x": 1, "y": 0}).output is True


def test_an_earlier_matching_rule_keeps_later_tests_unevaluated():
    definition = choice("$states.context.Execution.Input.x > 0")
    rules = definition["States"]["test"]["Choices"]
    rules[0]["Next"] = "inner"
    rules.insert(
        0,
        {
            "Condition": "{% $states.context.Execution.Input.stop = true %}",
            "Next": "yes",
        },
    )
    definition["States"]["inner"] = choice("$states.context.Execution.Input.y > 0")[
        "States"
    ]["test"]
    take_in_choices(definition, Rounds())
    compiled = emitted(definition)
    assert testing.run(compiled, {"stop": True}).output is True
    assert testing.run(compiled, {"x": 0}).output is False
    assert testing.run(compiled, {"x": 1, "y": 0}).output is False
    assert testing.run(compiled, {"x": 1, "y": 1}).output is True
    assert testing.run(compiled, {"x": 1}).error == "States.QueryEvaluationError"
