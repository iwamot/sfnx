# Verification

What the tests guarantee, and how to check the generated definitions against Step Functions itself.

## Two levels

| Check | What it compares | Runs |
|---|---|---|
| `tests/test_differential.py` | random programs in CPython and, compiled, in `sfnx.testing`; the definition before and after the passes; and how often each makes a call that gives another value, which the definition makes no more often than CPython | every `validate.sh` |
| `tests/test_corpus.py` | the fixed cases of `tests/corpus.py` in the interpreter, with `$random` fixed and counted, and in CPython where the case claims agreement | every `validate.sh` |
| `tests/aws_corpus.py` | the same fixed cases in Step Functions | on request, with AWS credentials |

The first two need no credentials and pass or fail on their own. They show that the compiler and the interpreter agree with Python, not that Step Functions does: the interpreter, `sfnx.testing`, is jsonata-python plus the behaviors `docs/design.md` records as measured. The third runs the definitions where they will run, and is the check to repeat when the translator changes what an expression means, or when a new Step Functions behavior is measured.

## What each property rests on

Each property below lists what supports it, what those checks cannot show, and where the property is specified. How often a check runs is given with it: **every run** is every `validate.sh`, **on request** is `tests/aws_corpus.py` in Step Functions, and **recorded** is a measurement made once and written in [design.md](design.md). A limit of a check says what it does not show, not that the property fails there; where the language allows a result other than Python's, [the table of differences](language.md#where-results-differ-from-python) names it, and the checks that compare with CPython keep to values it does not list.

### Values and failures follow Python

- **Rests on**:
  - `test_cpython_and_the_compiled_definition_agree` (every run): 200 random programs give CPython's result or error when compiled and run in `sfnx.testing`.
  - The corpus cases that claim `python` (every run, and on request in Step Functions): truth, numbers, `join`, missing keys and null, encoded text and the other categories of [the corpus](#the-corpus).
  - Unit tests of each spelling (every run), which run definitions on failing and missing values as well, such as a Task result without the key read or of another type.
  - 80 random programs run 240 times in Step Functions against CPython (recorded; [Programs as a whole](design.md#programs-as-a-whole)).
- **Limits**:
  - The random programs hold whole numbers without `/` and `**`, ASCII strings, and inputs that have every key with its declared type. Missing keys, `null`, other types, fractions, text outside ASCII and `jsonata()` are covered by the corpus and the unit tests, at the edges they pick, not by random programs.
  - Their retriers are for their own error: a retrier for `Exception` or `QueryEvaluationError` is covered by unit tests only.
- **Specified in**: [What a spelling guarantees](design.md#principles) and [Where results differ from Python](language.md#where-results-differ-from-python).

### The passes keep what the definition does

- **Rests on**:
  - `test_the_passes_keep_what_the_definition_does` (every run): each random program gives the same output or error before and after the passes, makes the same Task calls with the same arguments in the same order, and calls what gives another value no more often.
  - `misread` and `miscaught` (every run, where the tests compile with `checking=True`: the examples, the random programs and the compiler's tests): each read in the definition reads the assignment it read before the passes, and each except clause sees what it saw.
- **Limits**:
  - The random programs' inputs have every key, so they do not show where a missing key fails after the passes. Where a pass moves that failure after a call or a wait, or drops a value nothing reads, `AD-DEFERRED-FAILURE` and `AD-DEAD-FAILURE` allow it; unit tests check chosen cases of both.
  - The comparison requires the same Task calls, with the same arguments and in the same order, on each execution. Sharing the same Task state between alternative paths keeps that; a pass that dropped a call, or made one call serve two on the same execution, would need the calls compared otherwise.
- **Specified in**: [What the passes keep](design.md#what-the-passes-keep) and [Properties used by the passes](design.md#properties-used-by-the-passes).

### When and how often a value that changes is evaluated

- **Rests on**:
  - `test_the_definition_calls_what_changes_no_more_often_than_cpython` (every run): `$random`, `$uuid`, `$now` and `$millis` are called no more often than CPython calls their spellings.
  - `test_the_passes_read_the_time_between_the_same_calls_and_waits` (every run): a read of the time stays between the same calls and waits through the passes, in programs of one shape: reads of the time, Task calls, `wait(0)` and strings joined from them.
  - The corpus cases of the `volatile` category (every run, and on request in Step Functions), with `$random` fixed and counted locally and a condition on the value in Step Functions.
- **Limits**:
  - The check of the time covers that one shape of program.
  - In Step Functions the number of evaluations is not measured; the results file says `unmeasured`.
  - That these functions give a value and fail for none is assumed ([JSONata in Step Functions](design.md#jsonata-in-step-functions)).
- **Specified in**: [What the passes keep](design.md#what-the-passes-keep) and `AD-TIMING-WITHIN-EFFECT-INTERVAL` in [the table of differences](language.md#written-this-way-on-purpose).

### What `$states` and the Context Object read

- **Rests on**:
  - The corpus cases of the `catch`, `retry` and `choice` categories (every run, and on request in Step Functions): what a catcher reads, that a failing `Assign` assigns nothing, that a retry of a Map evaluates its `Items` again, and which `Assign` a Choice applies.
  - The measurements of `State.RetryCount` and `State.EnteredTime` (recorded).
- **Limits**: `sfnx.testing` gives `State.EnteredTime`, `Task.Token` and the other placeholders of the Context Object one value throughout a run, every state entered at the start time, so a local test cannot tell reads of them apart; what depends on them rests on the recorded measurements.
- **Specified in**: [States](design.md#states).

### Parallel and Map

- **Rests on**:
  - The random programs (every run), which make `parallel()`, `inline_map()` and `distributed_map()`.
  - The corpus cases of a Catch on a Parallel or a Map (every run, and on request in Step Functions).
  - 10 of the 80 programs run in Step Functions had distributed maps, as Standard executions (recorded).
- **Limits**: `sfnx.testing` runs branches and iterations one after another, takes the items of an `ItemReader` from the tasks function and writes nothing for a `ResultWriter` ([Where a local run differs](testing.md#where-a-local-run-differs)).
- **Specified in**: [States](design.md#states) and [Parallel and maps](language.md#parallel-and-maps).

### The interpreter and Step Functions

- **Rests on**:
  - The measurements in [JSONata in Step Functions](design.md#jsonata-in-step-functions) (recorded), and the functions `sfnx.testing` replaces to give what was measured.
  - The corpus in Step Functions (on request).
- **Limits**:
  - The differences that remain are listed in [Where a local run differs](testing.md#where-a-local-run-differs); others may remain.
  - A run in Step Functions covers the cases in its results file, compiled by the commit it records; adding or changing a case does not run it there again.
- **Specified in**: [testing.md](testing.md#where-a-local-run-differs).

### Step Functions accepts the definition

- **Rests on**:
  - The checks `sfnx.testing` makes before a run, raising `InvalidDefinition` for what Step Functions rejects when it validates a definition (every run, for each definition the tests run).
  - The compiler's checks of SDK integrations against botocore (every run).
  - The measurements of ValidateStateMachineDefinition (recorded).
- **Limits**: ValidateStateMachineDefinition is not called on a change, and whether Step Functions supports a service or an action botocore has is not checked; [deployment.md](deployment.md#checking-a-definition-before-deploying-it) describes the check to run before deploying.
- **Specified in**: [Tasks](design.md#tasks) and [the testing API](testing.md#the-api).

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
