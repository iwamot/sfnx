"""The measurements keep nested states and semantic expectations distinct."""

import json

import pytest

from sfnx.compiler import definitions
from tests import corpus
from tests.optimization import Workload, execute, footprint, main, measure, workloads


def test_footprint_counts_nested_states_and_expression_length():
    processor = {"StartAt": "r", "States": {"r": {"Type": "Succeed", "Output": "é"}}}
    branch = {
        "StartAt": "c",
        "States": {
            "c": {
                "Type": "Choice",
                "Choices": [
                    {"Condition": True, "Next": "m"},
                    {"Condition": False, "Next": "m"},
                ],
                "Default": "m",
            },
            "m": {"Type": "Map", "Items": [], "ItemProcessor": processor, "End": True},
        },
    }
    machine = {
        "StartAt": "p",
        "States": {
            "p": {
                "Type": "Parallel",
                "Branches": [branch],
                "Comment": "{% a long comment is not an expression %}",
                "Next": "r",
            },
            "r": {"Type": "Succeed", "Output": "{% $x + 1 %}"},
        },
    }
    stats = footprint({"main": machine})
    assert stats["states"] == 5
    assert stats["choice_rules"] == 2
    assert stats["max_expression_chars"] == 6
    text = json.dumps(machine, ensure_ascii=False, separators=(",", ":"))
    assert stats["asl_bytes"] == len(text) + 1
    assert (
        footprint(definitions(corpus.HEADER + "    return 1\n", "literal", False))[
            "max_expression_chars"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "body, expected, outcome",
    [
        ("return 1", corpus.Value(1), {"value": 1}),
        (
            "raise Declined('no')",
            corpus.Error("Declined"),
            {"error": "Declined", "cause": "no"},
        ),
    ],
)
def test_execution_is_judged_against_the_dsl_expectation(body, expected, outcome):
    case = corpus.Case("result", "test", body, {}, expected, "The stated DSL result.")
    result = execute(definitions(case.source, case.id, False), case)
    assert result["expected"]
    assert result["entered_states"] == 1
    assert result["outcome"] == outcome


def test_an_unoptimized_definition_is_not_the_semantic_reference():
    case = next(
        c
        for c in corpus.CASES
        if c.id == "catch-takes-a-failure-folded-into-a-parallel"
    )
    row = measure(Workload(case.id, "corpus", case.source, case), 1)
    assert not row["before"]["execution"]["expected"]
    assert row["after"]["execution"]["expected"]
    assert row["after"]["execution"]["outcome"] == {"value": "caught"}


def test_a_wrong_expected_result_is_reported():
    case = corpus.Case(
        "wrong", "test", "return 1", {}, corpus.Value(2), "An intentional mismatch."
    )
    row = measure(Workload(case.id, "corpus", case.source, case), 1)
    assert not row["after"]["execution"]["expected"]
    assert row["after"]["execution"]["detail"] == "gave 1, not 2"


def test_workload_ids_are_unique_and_examples_are_compile_only():
    cases = workloads()
    assert len(cases) == len({c.id for c in cases})
    examples = [c for c in cases if c.group == "examples"]
    assert examples
    assert all(c.case is None for c in examples)


def test_the_runner_writes_selected_results(tmp_path, capsys):
    path = tmp_path / "results.json"
    assert main(["--out", str(path), "--select", "loop-16", "--repeats", "1"]) == 0
    report = json.loads(path.read_text())
    assert report["repeats"] == 1
    assert [row["id"] for row in report["cases"]] == ["loop-16"]
    assert report["cases"][0]["after"]["execution"]["expected"]
    assert "expected" in capsys.readouterr().out


@pytest.mark.parametrize(
    "selection",
    [
        ["--select", "missing"],
        ["--repeats", "0"],
        ["--select", "loop-16", "--group", "examples"],
    ],
)
def test_an_invalid_selection_is_an_error(selection, tmp_path, capsys):
    path = tmp_path / "results.json"
    with pytest.raises(SystemExit) as error:
        main(["--out", str(path), *selection])
    assert error.value.code == 2
    assert not path.exists()
    assert "error:" in capsys.readouterr().err


def test_a_comment_in_arguments_is_data_while_a_rule_comment_is_metadata():
    machine = {
        "StartAt": "c",
        "States": {
            "c": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Condition": True,
                        "Comment": "{% this is not an expression %}",
                        "Next": "t",
                    }
                ],
                "Default": "t",
            },
            "t": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Arguments": {"Comment": "{% $payload %}"},
                "Catch": [
                    {
                        "ErrorEquals": ["States.ALL"],
                        "Comment": "{% neither is this %}",
                        "Next": "r",
                    }
                ],
                "End": True,
            },
            "r": {"Type": "Succeed", "Output": 0},
        },
    }
    assert footprint({"main": machine})["max_expression_chars"] == 8
