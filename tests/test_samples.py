"""examples/coding_agent.py against the workflow it is written from: the
agentic coding workflow of aws-samples/sample-stepfunctions-agentcore-coding-agent
(state-machine/coding-workflow.asl.yaml at ab76ef3, MIT-0), kept as JSON in
tests/samples/. Run with the same stand-in Tasks, the two make the same calls
with the same arguments and end the same way."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from sfnx import testing
from sfnx.compiler import compile_file

ROOT = Path(__file__).parent.parent
SAMPLE = json.loads((ROOT / "tests/samples/coding_workflow.asl.json").read_text())
TERM = {
    "record_id": "r1",
    "verbatim": "migrane",
    "encoding_dictionary": "MedDRA",
    "encoding_dictionary_version": "v27.0",
    "source_study": "ONCO-2024-01",
}
MIGRAINE = {"dict_term": "Migraine", "dict_term_code": "10027599"}


def example() -> dict[str, object]:
    (machine,) = compile_file(ROOT / "examples/coding_agent.py").values()
    return machine


def reply(text: str) -> dict[str, object]:
    return {"Output": {"Message": {"Content": [{"Text": text}]}}}


def agent(score: float, rationale: str = "a misspelling") -> dict[str, object]:
    answer = {**MIGRAINE, "score": score, "rationale": rationale}
    return reply(f"Found: {json.dumps(answer)}")


def tasks_for(
    direct: object, harness: object = None, write_back: object = None
) -> Callable[[testing.Call], object]:
    """Answers by what is called: the direct lookup, the harness, the write
    back. A Failure given for one is raised."""

    def answer(call: testing.Call) -> object:
        arguments = call.arguments
        assert isinstance(arguments, dict)
        if call.resource.endswith("invokeHarness"):
            result = harness
        elif arguments["FunctionName"] == "${CheckDirectFunctionArn}":
            failed = isinstance(direct, testing.Failure)
            result = direct if failed else {"Payload": direct}
        else:
            payload = arguments["Payload"]
            assert isinstance(payload, dict)
            failing = write_back is not None and payload["target_status"] != "open"
            result = write_back if failing else {"Payload": payload}
        if isinstance(result, testing.Failure):
            raise result
        return result

    return answer


MISS = {"blocked": False, "matched": False}
SCENARIOS = {
    "blocked": ({**MISS, "blocked": True}, None, None),
    "matched": ({**MISS, "matched": True, "candidate": MIGRAINE}, None, None),
    "agent confident": (MISS, agent(0.95), None),
    "agent unsure": (MISS, agent(0.8), None),
    "agent doubtful": (MISS, agent(0.5), None),
    "agent without a score": (MISS, reply('{"rationale": "x"}'), None),
    "agent without a rationale": (MISS, reply('{"score": 0.5}'), None),
    "agent without JSON": (MISS, reply("sorry"), None),
    "agent with broken JSON": (MISS, reply("{not json}"), None),
    "lookup fails": (testing.Failure("Lambda.Unknown", "boom"), None, None),
    "agent fails": (MISS, testing.Failure("States.TaskFailed", "x"), None),
    "write back fails": (
        {**MISS, "matched": True, "candidate": MIGRAINE},
        None,
        testing.Failure("ValidationError", "no code"),
    ),
}


def outcome(definition: dict, answers: tuple) -> tuple:
    """How a run ends, and every call it makes with its arguments."""
    execution = testing.run(
        definition, TERM, tasks_for(*answers), functions={"uuid": lambda: "u"}
    )
    calls = [(call.resource, call.arguments) for call in execution.calls]
    failed = execution.error is not None
    ended = (execution.error, execution.cause) if failed else execution.output
    return ended, calls


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_example_does_what_the_sample_does(scenario):
    assert outcome(example(), SCENARIOS[scenario]) == outcome(
        SAMPLE, SCENARIOS[scenario]
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_example_enters_no_more_states_than_the_sample(scenario):
    """A Standard workflow is billed per state transition."""
    functions = {"uuid": lambda: "u"}
    answers = SCENARIOS[scenario]
    entered = [
        len(testing.run(d, TERM, tasks_for(*answers), functions=functions).states)
        for d in (example(), SAMPLE)
    ]
    assert entered[0] <= entered[1]
