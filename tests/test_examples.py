"""The examples of examples/: each committed definition is what its source
compiles to, and each machine does what its docstring says when run locally."""

import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from sfnx.cli import document
from sfnx.compiler import compile_file
from tests import asl

EXAMPLES = Path(__file__).parent.parent / "examples"
NAMES = [
    "orders",
    "poll",
    "approval",
    "fanout",
    "settle",
    "hello_world",
    "coding_agent",
]


def definition(name: str) -> dict[str, object]:
    (machine,) = compile_file(EXAMPLES / f"{name}.py").values()
    return machine


class Tasks(Mapping[str, Callable[[object], object]]):
    """Answers each Task by its state name and records the calls; every
    example passes its arguments as a JSON object."""

    def __init__(self, **answers: Callable[[object], object]):
        self.answers = answers
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __getitem__(self, name: str) -> Callable[[object], object]:
        answer = self.answers[name]

        def call(arguments: object) -> object:
            assert isinstance(arguments, dict)
            self.calls.append((name, arguments))
            return answer(arguments)

        return call

    def __iter__(self) -> Iterator[str]:
        return iter(self.answers)

    def __len__(self) -> int:
        return len(self.answers)


def constant(result: object) -> Callable[[object], object]:
    return lambda arguments: result


def failing(error: str, cause: str = "") -> Callable[[object], object]:
    def fail(arguments: object) -> object:
        raise asl.Failure(error, cause)

    return fail


def in_turn(*results: object) -> Callable[[object], object]:
    """One result per call, in order."""
    remaining = iter(results)
    return lambda arguments: next(remaining)


@pytest.mark.parametrize("name", NAMES)
def test_the_committed_definition_is_current(name):
    expected = (EXAMPLES / f"{name}.asl.json").read_bytes()
    assert document(definition(name)) == expected, (
        f"regenerate it: uv run sfnx compile examples/{name}.py"
        f" -o examples/{name}.asl.json"
    )


ORDER = {
    "id": "o1",
    "items": [{"sku": "a", "quantity": 2}, {"sku": "b", "quantity": 1}],
}


def test_orders_reserves_every_item_then_charges():
    tasks = Tasks(updateItem=constant({}), receipt=constant({"Payload": {"total": 30}}))
    assert asl.run(definition("orders"), ORDER, tasks) == {
        "order": "o1",
        "receipt": {"total": 30},
    }
    assert [name for name, _ in tasks.calls] == ["updateItem", "updateItem", "receipt"]
    reserved = [call["ExpressionAttributeValues"] for _, call in tasks.calls[:2]]
    assert reserved == [{":n": {"N": "2"}}, {":n": {"N": "1"}}]
    assert tasks.calls[2][1] == {"FunctionName": "charge", "Payload": ORDER}


def test_orders_fails_as_out_of_stock_before_charging():
    def update(arguments: object) -> object:
        assert isinstance(arguments, dict)
        if arguments["Key"] == {"sku": {"S": "b"}}:
            raise asl.Failure("DynamoDb.ConditionalCheckFailedException", "declined")
        return {}

    tasks = Tasks(updateItem=update, receipt=constant({"Payload": {}}))
    with pytest.raises(asl.Failure) as failure:
        asl.run(definition("orders"), ORDER, tasks)
    assert (failure.value.error, failure.value.cause) == (
        "OutOfStock",
        "b is out of stock",
    )
    assert [name for name, _ in tasks.calls] == ["updateItem", "updateItem"]


def test_orders_retries_a_timeout():
    answers = iter([failing("States.Timeout"), constant({}), constant({})])

    def update(arguments: object) -> object:
        return next(answers)(arguments)

    tasks = Tasks(updateItem=update, receipt=constant({"Payload": {}}))
    asl.run(definition("orders"), ORDER, tasks)
    assert [name for name, _ in tasks.calls] == ["updateItem"] * 3 + ["receipt"]


JOB = {"TranscriptionJobName": "execution"}
DONE = {
    "TranscriptionJob": {
        "TranscriptionJobStatus": "COMPLETED",
        "Transcript": {"TranscriptFileUri": "s3://transcripts/execution.json"},
    }
}
RUNNING = {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}}
FAILED = {
    "TranscriptionJob": {
        "TranscriptionJobStatus": "FAILED",
        "FailureReason": "bad audio",
    }
}


def test_poll_returns_the_transcript_when_the_job_completes_at_the_first_poll():
    tasks = Tasks(startTranscriptionJob=constant({}), job=in_turn(DONE))
    assert asl.run(definition("poll"), {"uri": "s3://audio/a.mp3"}, tasks) == {
        "transcript": "s3://transcripts/execution.json"
    }
    assert tasks.calls == [
        (
            "startTranscriptionJob",
            {
                **JOB,
                "Media": {"MediaFileUri": "s3://audio/a.mp3"},
                "IdentifyLanguage": True,
            },
        ),
        ("job", JOB),
    ]


def test_poll_keeps_polling_while_the_job_runs():
    tasks = Tasks(
        startTranscriptionJob=constant({}), job=in_turn(RUNNING, RUNNING, DONE)
    )
    asl.run(definition("poll"), {"uri": "s3://audio/a.mp3"}, tasks)
    assert [name for name, _ in tasks.calls] == ["startTranscriptionJob"] + ["job"] * 3


def test_poll_fails_with_the_reason_when_the_job_fails():
    tasks = Tasks(startTranscriptionJob=constant({}), job=in_turn(RUNNING, FAILED))
    with pytest.raises(asl.Failure) as failure:
        asl.run(definition("poll"), {"uri": "s3://audio/a.mp3"}, tasks)
    assert (failure.value.error, failure.value.cause) == (
        "TranscriptionFailed",
        "bad audio",
    )


def test_poll_gives_up_after_the_last_poll():
    tasks = Tasks(startTranscriptionJob=constant({}), job=constant(RUNNING))
    with pytest.raises(asl.Failure) as failure:
        asl.run(definition("poll"), {"uri": "s3://audio/a.mp3"}, tasks)
    assert (failure.value.error, failure.value.cause) == (
        "StillRunning",
        "execution is still running after 20 polls",
    )
    assert [name for name, _ in tasks.calls] == ["startTranscriptionJob"] + ["job"] * 20


RELEASE = {"version": "1.2.0"}


def test_approval_deploys_an_approved_release():
    tasks = Tasks(
        decision=constant({"approved": True, "comment": "ship it"}),
        invoke=constant({"Payload": None}),
    )
    assert asl.run(definition("approval"), RELEASE, tasks) == {
        "version": "1.2.0",
        "deployed": True,
        "reason": "ship it",
    }
    assert tasks.calls == [
        (
            "decision",
            {
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:approvals",
                "Message": {"release": "1.2.0", "token": "token"},
            },
        ),
        ("invoke", {"FunctionName": "deploy", "Payload": {"version": "1.2.0"}}),
    ]


def test_approval_fails_as_rejected_without_deploying():
    tasks = Tasks(
        decision=constant({"approved": False, "comment": "not yet"}),
        invoke=constant({"Payload": None}),
    )
    with pytest.raises(asl.Failure) as failure:
        asl.run(definition("approval"), RELEASE, tasks)
    assert (failure.value.error, failure.value.cause) == ("Rejected", "not yet")
    assert [name for name, _ in tasks.calls] == ["decision"]


def test_approval_returns_undeployed_when_nobody_answers():
    tasks = Tasks(
        decision=failing("States.Timeout", "no answer"),
        invoke=constant({"Payload": None}),
    )
    assert asl.run(definition("approval"), RELEASE, tasks) == {
        "version": "1.2.0",
        "deployed": False,
        "reason": "no answer",
    }
    assert [name for name, _ in tasks.calls] == ["decision"]


ALBUM = {
    "album": "trip",
    "photos": ["s3://raw/1.png", "s3://raw/2.png", "s3://raw/3.png"],
}


def resize(arguments: object) -> object:
    assert isinstance(arguments, dict)
    payload = arguments["Payload"]
    assert isinstance(payload, dict)
    return {"Payload": payload["target"]}


def test_fanout_resizes_every_photo_then_notifies_and_indexes():
    tasks = Tasks(
        **{
            "resize.return": resize,
            "notify.return": constant({"MessageId": "m1"}),
            "index.return": constant({"Payload": {"entry": 7}}),
        }
    )
    resized = ["trip/0.jpg", "trip/1.jpg", "trip/2.jpg"]
    assert asl.run(definition("fanout"), ALBUM, tasks) == {
        "album": "trip",
        "photos": resized,
        "index": {"entry": 7},
        "notice": "m1",
    }
    assert [name for name, _ in tasks.calls[:3]] == ["resize.return"] * 3
    assert [call["Payload"] for _, call in tasks.calls[:3]] == [
        {"source": photo, "target": target}
        for photo, target in zip(ALBUM["photos"], resized, strict=True)
    ]
    calls = dict(tasks.calls[3:])
    assert calls["notify.return"]["Message"] == "3 photos of trip are ready"
    assert calls["index.return"]["Payload"] == {"album": "trip", "photos": resized}


def test_fanout_of_an_empty_album_notifies_and_indexes_nothing():
    tasks = Tasks(
        **{
            "resize.return": resize,
            "notify.return": constant({"MessageId": "m1"}),
            "index.return": constant({"Payload": {"entry": 0}}),
        }
    )
    assert asl.run(definition("fanout"), {"album": "trip", "photos": []}, tasks) == {
        "album": "trip",
        "photos": [],
        "index": {"entry": 0},
        "notice": "m1",
    }
    calls = dict(tasks.calls)
    assert calls["notify.return"]["Message"] == "0 photos of trip are ready"
    assert calls["index.return"]["Payload"] == {"album": "trip", "photos": []}


CHARGES = [
    {"currency": "USD", "amount": 5},
    {"currency": "EUR", "amount": 2},
    {"currency": "USD", "amount": 7},
]


def test_settle_totals_the_charges_per_currency():
    tasks = Tasks(charges=constant({"Payload": CHARGES}))
    assert asl.run(definition("settle"), {"date": "2026-09-22"}, tasks) == {
        "date": "2026-09-22",
        "charges": 3,
        "totals": {"USD": 12, "EUR": 2},
    }
    assert tasks.calls == [
        ("charges", {"FunctionName": "load-charges", "Payload": {"date": "2026-09-22"}})
    ]


def test_settle_of_a_day_without_charges_returns_no_totals():
    tasks = Tasks(charges=constant({"Payload": []}))
    assert asl.run(definition("settle"), {"date": "2026-09-22"}, tasks) == {
        "date": "2026-09-22",
        "charges": 0,
        "totals": {},
    }


def test_hello_world_waits_runs_both_branches_and_counts_two_checkpoints():
    summary = asl.run(definition("hello_world"), {})["Summary"]
    assert re.fullmatch(
        r"This Hello World execution began on \d\d/\d\d\. The state machine ran for"
        r" \S+ seconds before the snapshot was taken, passing through 2"
        r" checkpoints, and has successfully completed\.",
        summary,
    )


def test_hello_world_fails_under_the_template_s_error_name():
    states = definition("hello_world")["States"]
    assert isinstance(states, dict)
    assert states["raise"] == {"Type": "Fail", "Error": "Not a Hello World Example"}


TERM = {
    "record_id": "r1",
    "verbatim": "migrane",
    "encoding_dictionary": "MedDRA",
    "encoding_dictionary_version": "v27.0",
    "source_study": "ONCO-2024-01",
}
MIGRAINE = {"dict_term": "Migraine", "dict_term_code": "10027599"}


def reply(text: str) -> Callable[[object], object]:
    return constant({"Output": {"Message": {"Content": [{"Text": text}]}}})


def written(arguments: object) -> object:
    assert isinstance(arguments, dict)
    return {"Payload": arguments["Payload"]}


def coding(direct: object, agent: Callable[[object], object]) -> Tasks:
    return Tasks(
        direct=constant({"Payload": direct}),
        agent_candidate=agent,
        written=written,
        opened=written,
    )


def test_coding_agent_writes_back_a_direct_match_without_the_agent():
    tasks = coding(
        {"blocked": False, "matched": True, "candidate": MIGRAINE}, reply("")
    )
    result = asl.run(definition("coding_agent"), TERM, tasks)
    assert (result["target_status"], result["candidate"]) == ("autocoded", MIGRAINE)
    assert [name for name, _ in tasks.calls] == ["direct", "written"]


@pytest.mark.parametrize(
    "score, status", [(0.95, "autocoded"), (0.8, "approval_required")]
)
def test_coding_agent_routes_the_agent_s_candidate_by_its_score(score, status):
    answer = json.dumps({**MIGRAINE, "score": score, "rationale": "a misspelling"})
    tasks = coding({"blocked": False, "matched": False}, reply(f"Found: {answer}"))
    result = asl.run(definition("coding_agent"), TERM, tasks)
    assert (result["target_status"], result["candidate"]["dict_term_code"]) == (
        status,
        "10027599",
    )


def test_coding_agent_leaves_a_low_score_open_with_the_rationale():
    answer = json.dumps({**MIGRAINE, "score": 0.5, "rationale": "no good fit"})
    tasks = coding({"blocked": False, "matched": False}, reply(answer))
    assert asl.run(definition("coding_agent"), TERM, tasks) == {
        "record_id": "r1",
        "target_status": "open",
        "failure_reason": None,
        "rationale": "no good fit",
    }


def test_coding_agent_leaves_open_a_reply_without_a_rationale():
    tasks = coding({"blocked": False, "matched": False}, reply('{"score": 0.5}'))
    result = asl.run(definition("coding_agent"), TERM, tasks)
    assert (result["target_status"], result["rationale"]) == ("open", None)


@pytest.mark.parametrize("text", ["sorry, I cannot help", "{not json}"])
def test_coding_agent_leaves_a_reply_without_json_open(text):
    tasks = coding({"blocked": False, "matched": False}, reply(text))
    result = asl.run(definition("coding_agent"), TERM, tasks)
    assert (result["target_status"], result["failure_reason"]) == (
        "open",
        "States.QueryEvaluationError",
    )


def test_coding_agent_leaves_a_blocked_term_open_without_the_agent():
    tasks = coding({"blocked": True, "matched": False}, reply(""))
    result = asl.run(definition("coding_agent"), TERM, tasks)
    assert (result["target_status"], result["failure_reason"]) == ("open", None)
    assert [name for name, _ in tasks.calls] == ["direct", "opened"]
