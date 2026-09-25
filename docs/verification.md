# Verification

What the tests guarantee, and how to check the generated definitions against Step Functions itself.

## Two levels

| Check | What it compares | Runs |
|---|---|---|
| `tests/test_differential.py` | random programs in CPython and, compiled, in `sfnx.testing` | every `validate.sh` |
| `tests/test_corpus.py` | the fixed cases of `tests/corpus.py` in the interpreter, with `$random` fixed and counted, and in CPython where the case claims agreement | every `validate.sh` |
| `tests/aws_corpus.py` | the same fixed cases in Step Functions | on request, with AWS credentials |

The first two need no credentials and pass or fail on their own. They show that the compiler and the interpreter agree with Python, not that Step Functions does: the interpreter, `sfnx.testing`, is jsonata-python plus the behaviors `docs/design.md` records as measured. The third runs the definitions where they will run, and is the check to repeat when the translator changes what an expression means, or when a new Step Functions behavior is measured.

## The corpus

A case is a program body, an input, what it must give and why. It also says which guarantee it claims:

- **`python`**: CPython gives the same value. A case where Python raises a different error, or where the result changes on every evaluation, does not claim it.
- **`random` and `calls`**: what `$random` returns locally, in order, and how many times it is called. A call past the end of the values fails the run, so a definition that evaluates an expression twice is caught, and so is one that evaluates it when it should not.
- **`on_aws`**: for a result that changes on every evaluation, the condition the result must satisfy in Step Functions, in place of the value. The number of calls is not measured there; a value in range is not evidence of a single evaluation, and the results file says `unmeasured`.
- **`backs`**: for a case that runs a Step Functions behavior the compiler relies on, a phrase of [design.md](design.md) that states it. The paragraph names the case after the phrase, in parentheses after `corpus:`, and `test_corpus.py` checks both ways.
- **`states`**: the types of the top-level states, where the case runs that behavior only while the compiler writes those states, such as a `return` in the `Output` of a Parallel. A change of the compiler that writes others fails the test, so the case is rewritten rather than left testing something else.

The categories are truth of a value, numbers, `join`, `**`, lists of none, one and several items, dicts from a comprehension, `any` and `all`, missing keys and null, encoded text, times written with a picture string, volatile expressions, Catch on a Parallel and an inline Map with the statements the compiler folds into them, retries of an inline Map, and Choices that take in the rules of others. Add a case by appending to `CASES`; its id must be new and at most 59 characters, to fit the name of a state machine, and `test_corpus.py` runs it locally at once.

## Running on AWS

The runner reads credentials and the region as botocore does, with `--region` taking precedence. Credentials from `aws login` are readable only through the CLI, so export them into the environment first:

```bash
eval "$(aws configure export-credentials --format env)"
```

```bash
uv run python -m tests.aws_corpus --out results.json
```

`--select ID ...` and `--category NAME ...` narrow the run; an id or a category that matches nothing is an error.

A definition of one state other than a Map or a Parallel, which `TestState` refuses, goes through `TestState`, which creates nothing and needs no role. Any other definition is created as an Express state machine named `sfnx-corpus-<run>-<case>`, started with `StartSyncExecution` and deleted. Those cases need `--role-arn`, and are written as `not-run` without it. The role is one Step Functions can assume; the corpus calls no service, so it needs no permissions of its own:

```bash
aws iam create-role --role-name sfnx-corpus --assume-role-policy-document '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
```

The caller needs `states:TestState`, `states:CreateStateMachine`, `states:StartSyncExecution`, `states:DeleteStateMachine`, `states:ListStateMachines`, `states:DescribeStateMachine` and `iam:PassRole` on the role. Both `TestState` and `StartSyncExecution` stop after five minutes; the corpus runs Pass, Choice, Succeed, Fail, Parallel and inline Map states only, so a case takes a few seconds at most, with the one-second waits of its retries. Express executions are billed by request, duration and memory.

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

A `not-run` or an `api-error` is not a pass. A `mismatch` on AWS with a passing local run means the interpreter and Step Functions disagree: measure the behavior, record it in `docs/design.md`, and fix the compiler or the interpreter, rather than the expectation. A difference the interpreter keeps is listed in [testing.md](testing.md#where-a-local-run-differs) too, since users run their own definitions through it.
