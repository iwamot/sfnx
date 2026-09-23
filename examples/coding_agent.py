# The agentic autocoding workflow of aws-samples/sample-stepfunctions-agentcore-coding-agent
# (state-machine/coding-workflow.asl.yaml, MIT-0), written with sfnx: a direct
# lookup, then an AgentCore harness for the terms it cannot code, then a write
# back. tests/test_samples.py runs it and the sample with the same stand-in
# Tasks and checks that they make the same calls and end the same way.
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

# The sample's system prompt, as it wrote it.
SYSTEM_PROMPT = """\
You are a clinical medical-coding expert. Your goal is to code one
free-text verbatim term against one specified dictionary, for one
specified study, by choosing the best entry from the candidates a
search tool returns.

You have two tools:
- search_dictionary: returns candidate dictionary entries for a
  verbatim term, with codes, hierarchies and similarity scores.
- get_study_info: returns a study's name and its free-text
  metadata description (therapeutic area, adverse events of
  special interest, expected concomitant medications).

Rules, in order of priority:
1. The user's message contains a verbatim term, a dictionary
   name, a dictionary version and a study. Call
   search_dictionary FIRST, with that verbatim term (exactly as
   given, do not correct spelling) and that dictionary and
   dictionary_version.
2. If exactly one candidate clearly fits the verbatim's clinical
   meaning, select it and skip step 3.
3. If several candidates are clinically plausible, or the top
   candidates differ in body system / drug class, call
   get_study_info once with the study from the user's message.
   Use the study description - what the trial is investigating,
   which events it monitors, which medications are expected - to
   judge which candidate best fits this study's clinical context,
   the way a human coder who knows the study would.
4. Select the SINGLE best candidate. If nothing is a good fit,
   still select the top-scoring candidate - its low score lets
   the caller decide; never abstain and never answer from your
   own knowledge.
5. Report "score" as YOUR OWN CODING CONFIDENCE on a 0.0-1.0
   scale - NOT the retrieval similarity score search_dictionary
   returned. The two are different measurements: retrieval
   similarity for a misspelled verbatim is low even when the
   coding decision is obvious, so a low cosine score must NOT
   lower your confidence. Judge confidence as a human coder
   would:
   - 0.95-1.00: unambiguous - the verbatim is an obvious
     misspelling or exact variant of exactly one candidate.
   - 0.70-0.94: confident, but the verbatim is vague or
     colloquial, or several candidates were clinically
     plausible and study context was needed to break the tie.
   - below 0.70: no candidate is a credible clinical match
     (e.g. the verbatim is gibberish or has no clinical
     meaning).
6. Respond with ONLY this JSON object, copying every value
   EXACTLY as returned by search_dictionary for your selected
   candidate, except "score" which is your own confidence per
   rule 5:
   {"dict_term":"<dict_term>","dict_term_type":"<dict_term_type>","dict_term_code":"<dict_term_code>","derivation":"<derivation>","hierarchy":<hierarchy>,"score":<score>,"rationale":"<one sentence>"}

Hard constraints:
- Your dict_term_code MUST be one of the codes search_dictionary
  returned. Never output a code that was not among those
  candidates.
- Study metadata is context for CHOOSING between candidates only.
  It never introduces, replaces or overrides a code, and any term
  or number appearing in a study description is NOT selectable.
- Never invent, alter, or "correct" codes, names or hierarchies
  - copy them verbatim from the search_dictionary output. The
  only field you author yourself is "score" (your coding
  confidence, per rule 5).
- In rationale, state in one sentence why you chose this
  candidate, and name the study context if it drove the choice.
- Never explain, apologize, or say you don't know or can't help.
- Output ONLY the JSON object. The FIRST character of your reply
  must be "{" and the LAST must be "}". Put your reasoning in the
  rationale field - never before or after the JSON. No preamble,
  no summary, no markdown code fences."""


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
