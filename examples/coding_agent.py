# The agentic autocoding workflow of aws-samples/sample-stepfunctions-agentcore-coding-agent
# (state-machine/coding-workflow.asl.yaml, MIT-0), written with sfnx: a direct
# lookup, then an AgentCore harness for the terms it cannot code, then a write
# back. The definition it compiles to routes every term as the sample's does.
import json
import uuid

from sfnx import TaskFailed, Timeout, jsonata, state_machine, task


class Lambda:
    class TooManyRequestsException(Exception):
        pass

    class ServiceException(Exception):
        pass

    class Unknown(Exception):
        pass


class BedrockAgentCore:
    class ThrottlingException(Exception):
        pass

    class InternalServerException(Exception):
        pass


class ThrottlingException(Exception):
    pass


class DatabaseResumingException(Exception):
    pass


class DatabaseUnavailableException(Exception):
    pass


LAMBDA_RETRY = [
    {
        "ErrorEquals": [Lambda.TooManyRequestsException, Lambda.ServiceException],
        "IntervalSeconds": 2,
        "MaxAttempts": 3,
        "BackoffRate": 2.0,
    },
    {
        "ErrorEquals": [
            Lambda.Unknown,
            Timeout,
            ThrottlingException,
            DatabaseResumingException,
            DatabaseUnavailableException,
        ],
        "IntervalSeconds": 5,
        "MaxAttempts": 6,
        "BackoffRate": 2.0,
    },
]

AGENT_RETRY = [
    {
        "ErrorEquals": [
            BedrockAgentCore.ThrottlingException,
            BedrockAgentCore.InternalServerException,
        ],
        "IntervalSeconds": 3,
        "MaxAttempts": 3,
        "BackoffRate": 2.0,
    },
    {
        "ErrorEquals": [Timeout, TaskFailed],
        "IntervalSeconds": 5,
        "MaxAttempts": 3,
        "BackoffRate": 2.0,
    },
]

# Abridged; the sample's prompt spells out each rule.
SYSTEM_PROMPT = (
    "You are a clinical medical-coding expert. Call search_dictionary with the"
    " verbatim term, and get_study_info when several candidates fit. Respond"
    ' with only this JSON object: {"dict_term": ..., "dict_term_code": ...,'
    ' "derivation": ..., "hierarchy": ..., "score": <your confidence from 0 to'
    ' 1>, "rationale": "<one sentence>"}'
)


@state_machine
def coding_workflow(input):
    """Code one clinical verbatim: direct lookup, then the agent, then write back."""
    record_id: str = input["record_id"]
    verbatim: str = input["verbatim"]
    dictionary: str = input["encoding_dictionary"]
    version: str = input["encoding_dictionary_version"]
    study: str = input["source_study"]
    derivation_only = input.get("derivation_only") == True
    failure_reason = None
    agent_candidate = None
    candidate = None
    try:
        direct = task(
            "arn:aws:states:::lambda:invoke",
            {
                "FunctionName": "${CheckDirectFunctionArn}",
                "Payload": {
                    "verbatim": verbatim,
                    "encoding_dictionary": dictionary,
                    "encoding_dictionary_version": version,
                },
            },
            retry=LAMBDA_RETRY,
        )["Payload"]
        if direct["blocked"]:
            status = "open"
        elif direct["matched"]:
            status = "autocoded"
            candidate = direct["candidate"]
        else:
            request = {
                "HarnessArn": "${CodingHarnessArn}",
                "RuntimeSessionId": str(uuid.uuid4()),
                "Messages": [
                    {
                        "Role": "user",
                        "Content": [
                            {
                                "Text": f'Code this verbatim term: "{verbatim}".'
                                f" Dictionary={dictionary} version={version}"
                                f" study={study}."
                            }
                        ],
                    }
                ],
                "Tools": [
                    {
                        "Type": "agentcore_gateway",
                        "Name": "gateway",
                        "Config": {"AgentCoreGateway": {"GatewayArn": "${GatewayArn}"}},
                    }
                ],
                "AllowedTools": [
                    "@gateway/search-dictionary___search_dictionary",
                    "@gateway/study-info___get_study_info",
                ],
                "SystemPrompt": [{"Text": SYSTEM_PROMPT}],
            }
            # The reply is parsed in the statement of the call, so the Task's
            # Catch also takes a reply that holds no JSON object.
            agent_candidate = json.loads(
                jsonata(
                    "$match($text, /\\{[\\s\\S]*\\}/)[0].match",
                    text=task(
                        "arn:aws:states:::bedrockagentcore:invokeHarness",
                        request,
                        retry=AGENT_RETRY,
                    )["Output"]["Message"]["Content"][0]["Text"],
                )
            )
            score = agent_candidate.get("score")
            if not isinstance(score, (int, float)):
                score = 0
            candidate = agent_candidate
            if score >= 0.9:
                status = "autocoded"
            elif score >= 0.7:
                status = "approval_required"
            else:
                status = "open"
    except Exception as e:
        failure_reason = type(e).__name__
        status = "open"
    if status != "open":
        try:
            written = task(
                "arn:aws:states:::lambda:invoke",
                {
                    "FunctionName": "${WriteBackFunctionArn}",
                    "Payload": {
                        "record_id": record_id,
                        "target_status": status,
                        "candidate": candidate,
                        "derivation_only": derivation_only,
                        "encoding_dictionary": dictionary,
                        "encoding_dictionary_version": version,
                    },
                },
                retry=LAMBDA_RETRY,
            )
            return written["Payload"]
        except Exception as e:
            failure_reason = type(e).__name__
    rationale = agent_candidate.get("rationale") if agent_candidate else None
    opened = task(
        "arn:aws:states:::lambda:invoke",
        {
            "FunctionName": "${WriteBackFunctionArn}",
            "Payload": {
                "record_id": record_id,
                "target_status": "open",
                "failure_reason": failure_reason,
                "rationale": rationale,
            },
        },
        retry=LAMBDA_RETRY,
    )
    return opened["Payload"]
