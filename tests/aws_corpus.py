"""Run the corpus of tests/corpus.py in Step Functions and write what each case
gave. Not a test: pytest never collects it, and nothing runs on AWS unless
this is invoked.

    uv run python -m tests.aws_corpus --out results.json [--select ID ...]
        [--category NAME ...] [--role-arn ARN] [--region REGION]

A definition of one state other than a Map or a Parallel goes through
TestState, which creates nothing and needs no role. Any other definition is
created as an Express state machine named sfnx-corpus-<run>-<case>, started
with StartSyncExecution and deleted, which needs --role-arn, a role Step
Functions can assume; without it those cases are written as not-run. A state
machine left behind by a failure is reported, and the run ends by listing
every sfnx-corpus- machine still in the region, from this run or an earlier
one.

The exit status is 0 when every case passed, 1 when any case is a mismatch,
an api-error or not-run, and 2 when the run could not start."""

import argparse
import json
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import botocore.session
from botocore.exceptions import BotoCoreError, ClientError

import sfnx
from tests import corpus

PREFIX = "sfnx-corpus-"
STATUSES = ("passed", "mismatch", "api-error", "not-run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="the results file")
    parser.add_argument("--select", nargs="*", default=[], metavar="ID")
    parser.add_argument("--category", nargs="*", default=[], metavar="NAME")
    parser.add_argument("--role-arn", help="the role the Express state machines use")
    parser.add_argument("--region", help="the region; the default is the profile's")
    args = parser.parse_args(argv)
    try:
        cases = corpus.select(corpus.CASES, args.select, args.category)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    session = botocore.session.get_session()
    region = args.region or session.get_config_variable("region")
    if not region:
        print("no region: pass --region or configure one", file=sys.stderr)
        return 2
    client = session.create_client("stepfunctions", region_name=region)
    run = uuid.uuid4().hex[:8]
    stamp = {
        "region": region,
        "version": sfnx.__version__,
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    records = []
    leftovers: list[str] = []
    for case in cases:
        definition = corpus.compiled(case)
        route = corpus.route_of(definition)
        if route == "execution" and not args.role_arn:
            records.append(
                corpus.record(
                    case,
                    definition,
                    route=route,
                    status="not-run",
                    detail="an execution needs --role-arn",
                    **stamp,
                )
            )
        else:
            records.append(
                measured(
                    client,
                    case,
                    definition,
                    route,
                    args.role_arn,
                    run,
                    leftovers,
                    stamp,
                )
            )
        print(f"{records[-1]['status']:<9} {case.id}  {records[-1]['detail']}")
    leftovers += remaining(client)
    args.out.write_text(json.dumps(records, indent=2) + "\n")
    counts = {s: sum(r["status"] == s for r in records) for s in STATUSES}
    print(", ".join(f"{n} {s}" for s, n in counts.items()), "->", args.out)
    if leftovers:
        print("state machines left behind, delete them by hand:", file=sys.stderr)
        for arn in leftovers:
            print(f"  {arn}", file=sys.stderr)
    return 0 if counts["passed"] == len(records) else 1


def measured(
    client,
    case: corpus.Case,
    definition: Mapping[str, object],
    route: str,
    role_arn: str | None,
    run: str,
    leftovers: list[str],
    stamp: Mapping[str, str],
) -> dict[str, object]:
    """One case run on AWS, as its record."""
    try:
        if route == "test-state":
            outcome = tested(client, definition, case.input)
        else:
            assert role_arn is not None
            outcome = executed(client, definition, case, role_arn, run, leftovers)
    except (BotoCoreError, ClientError) as exc:
        return corpus.record(
            case,
            definition,
            route=route,
            status="api-error",
            detail=str(exc),
            **stamp,
        )
    verdict = corpus.judge(case.remote, outcome)
    return corpus.record(
        case,
        definition,
        route=route,
        status=corpus.status_of(verdict),
        outcome=outcome,
        detail=verdict.detail,
        **stamp,
    )


def tested(client, definition: Mapping[str, object], execution_input: object):
    """TestState of the one state, with the input as the execution's."""
    response = client.test_state(
        definition=json.dumps(definition),
        stateName=definition["StartAt"],
        input=json.dumps(execution_input),
    )
    return corpus.outcome_of(response)


def executed(
    client,
    definition: Mapping[str, object],
    case: corpus.Case,
    role_arn: str,
    run: str,
    leftovers: list[str],
):
    """An Express execution of a state machine created for the case and
    deleted after it; one that cannot be deleted is reported."""
    created = client.create_state_machine(
        name=f"{PREFIX}{run}-{case.id}",
        definition=json.dumps(definition),
        roleArn=role_arn,
        type="EXPRESS",
    )
    arn = created["stateMachineArn"]
    try:
        response = client.start_sync_execution(
            stateMachineArn=arn, input=json.dumps(case.input)
        )
    finally:
        try:
            client.delete_state_machine(stateMachineArn=arn)
        except (BotoCoreError, ClientError):
            leftovers.append(arn)
    return corpus.outcome_of(response)


def remaining(client) -> list[str]:
    """Every corpus state machine still in the region. Deletion is
    asynchronous, so one being deleted is still listed and is not counted."""
    arns = []
    token = None
    while True:
        page = client.list_state_machines(**({"nextToken": token} if token else {}))
        arns += [
            m["stateMachineArn"]
            for m in page["stateMachines"]
            if m["name"].startswith(PREFIX)
        ]
        token = page.get("nextToken")
        if not token:
            break
    return [
        arn
        for arn in arns
        if client.describe_state_machine(stateMachineArn=arn)["status"] != "DELETING"
    ]


if __name__ == "__main__":
    sys.exit(main())
