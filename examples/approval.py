from typing import TypedDict

from sfnx import Timeout, context, state_machine, task


class Decision(TypedDict):
    approved: bool
    comment: str


class Rejected(Exception):
    pass


@state_machine
def release(input):
    """Ask a person to approve a release, wait a day for the answer, and deploy it."""
    try:
        # The message carries the task token; the approver sends the decision
        # back with SendTaskSuccess, and the Task ends with it as its result.
        decision: Decision = task(
            "arn:aws:states:::sns:publish.waitForTaskToken",
            {
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:approvals",
                "Message": {
                    "release": input["version"],
                    "token": context["Task"]["Token"],
                },
            },
            timeout=86400,
        )
    except Timeout:
        return {"version": input["version"], "deployed": False, "reason": "no answer"}
    if not decision["approved"]:
        raise Rejected(decision["comment"])
    task(
        "arn:aws:states:::lambda:invoke",
        {"FunctionName": "deploy", "Payload": {"version": input["version"]}},
    )
    return {
        "version": input["version"],
        "deployed": True,
        "reason": decision["comment"],
    }
