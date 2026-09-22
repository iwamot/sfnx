# Verification

What the tests guarantee, and how to check the generated definitions against Step Functions itself.

## Two levels

| Check | What it compares | Runs |
|---|---|---|
| `tests/test_differential.py` | random programs in CPython and, compiled, in the interpreter of `tests/asl.py` | every `validate.sh` |
| `tests/test_corpus.py` | the fixed cases of `tests/corpus.py` in the interpreter, with `$random` fixed and counted, and in CPython where the case claims agreement | every `validate.sh` |
| `tests/aws_corpus.py` | the same fixed cases in Step Functions | on request, with AWS credentials |

The first two need no credentials and pass or fail on their own. They show that the compiler and the interpreter agree with Python, not that Step Functions does: the interpreter is jsonata-python plus the behaviors `docs/design.md` records as measured. The third runs the definitions where they will run, and is the check to repeat when the translator changes what an expression means, or when a new Step Functions behavior is measured.

## The corpus

A case is a program body, an input, what it must give and why. It also says which guarantee it claims:

- **`python`**: CPython gives the same value. A case where Python raises a different error, or where the result changes on every evaluation, does not claim it.
- **`random` and `calls`**: what `$random` returns locally, in order, and how many times it is called. A call past the end of the values fails the run, so a definition that evaluates an expression twice is caught, and so is one that evaluates it when it should not.
- **`on_aws`**: for a result that changes on every evaluation, the condition the result must satisfy in Step Functions, in place of the value. The number of calls is not measured there; a value in range is not evidence of a single evaluation, and the results file says `unmeasured`.

The categories are truth of a value, numbers, `join`, `**`, lists of none, one and several items, dicts from a comprehension, missing keys and null, volatile expressions, and a Catch with the variables it sees. Add a case by appending to `CASES`; its id must be new, and `test_corpus.py` runs it locally at once.

## Running on AWS

The runner reads credentials and the region as botocore does, with `--region` taking precedence. Credentials from `aws login` are readable only through the CLI, so export them into the environment first:

```bash
eval "$(aws configure export-credentials --format env)"
```

```bash
uv run python -m tests.aws_corpus --out results.json
```

`--select ID ...` and `--category NAME ...` narrow the run; an id or a category that matches nothing is an error.

A definition of one state goes through `TestState`, which creates nothing and needs no role. A definition of several states is created as an Express state machine named `sfnx-corpus-<run>-<case>`, started with `StartSyncExecution` and deleted. Those cases need `--role-arn`, and are written as `not-run` without it. The role is one Step Functions can assume; the corpus calls no service, so it needs no permissions of its own:

```bash
aws iam create-role --role-name sfnx-corpus --assume-role-policy-document '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
```

The caller needs `states:TestState`, `states:CreateStateMachine`, `states:StartSyncExecution`, `states:DeleteStateMachine`, `states:ListStateMachines`, `states:DescribeStateMachine` and `iam:PassRole` on the role. Both `TestState` and `StartSyncExecution` stop after five minutes; the corpus runs Pass, Succeed, Fail and Parallel states only, so a case takes well under a second. Express executions are billed by request, duration and memory.

Every state machine is deleted after its execution; deletion is asynchronous, and a machine still being deleted is not reported. One that could not be deleted is reported at the end, with every `sfnx-corpus-` machine still in the region from any run, to delete by hand:

```bash
aws stepfunctions delete-state-machine --state-machine-arn <arn>
```

## Reading the results

The results file is a JSON list with one record per case:

| Field | Meaning |
|---|---|
| `id`, `category`, `source`, `definition`, `input` | the case and the definition that was sent |
| `expected` | a value, an error name or a named condition |
| `cpython` | `agrees` when the case claims CPython gives the same value, `not compared` otherwise |
| `route` | `test-state` or `execution` |
| `region`, `sfnx`, `time` | where and when the definitions were run, and the version that compiled them, which carries the commit and a date when the tree had changes |
| `status` | `passed`, `mismatch`, `api-error` or `not-run` |
| `actual`, `detail` | what came back, and what differed or failed |
| `calls` | `unmeasured`: the number of evaluations is checked locally only |

A `not-run` or an `api-error` is not a pass. A `mismatch` on AWS with a passing local run means the interpreter and Step Functions disagree: measure the behavior, record it in `docs/design.md`, and fix the compiler or the interpreter, rather than the expectation.
