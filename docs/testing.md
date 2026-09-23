# Testing

`sfnx.testing` runs a definition on your machine, with each Task answered by a function of your test. A test then checks where the workflow goes, which calls it makes with which arguments, and what it returns or fails with, without AWS credentials or a deployment. It runs definitions in JSONata mode: the ones sfnx compiles, and ones written by hand or by another tool that keep to the states and fields listed in [What runs](#what-runs).

What happens inside a Task is up to the function you give; a run checks the definition, not the services it calls. [Where a local run differs](#where-a-local-run-differs) lists what can come out otherwise in Step Functions.

## Setup

The runner evaluates JSONata with [jsonata-python](https://github.com/rayokota/jsonata-python), which the `testing` extra brings. Add it where the tests run, not to what you deploy:

```bash
uv add --dev "sfnx[testing]"
```

## A test

This test runs [examples/orders.py](../examples/orders.py), which reserves every item of an order in DynamoDB and then charges for it through Lambda:

```python
from sfnx.compiler import compile_file
from sfnx.testing import Call, Failure, run

(ORDERS,) = compile_file("examples/orders.py").values()
ORDER = {"id": "o1", "items": [{"sku": "a", "quantity": 2}, {"sku": "b", "quantity": 1}]}
UPDATE = "arn:aws:states:::aws-sdk:dynamodb:updateItem"
INVOKE = "arn:aws:states:::lambda:invoke"


def stock(*out: str):
    """DynamoDB with the SKUs given out of stock, and a charge of 30."""

    def tasks(call: Call) -> object:
        assert isinstance(call.arguments, dict)
        if call.resource == UPDATE:
            if call.arguments["Key"]["sku"]["S"] in out:
                raise Failure("DynamoDb.ConditionalCheckFailedException", "declined")
            return {}
        assert call.resource == INVOKE
        return {"Payload": {"total": 30}}

    return tasks


def test_an_order_in_stock_is_charged():
    execution = run(ORDERS, ORDER, stock())
    assert execution.output == {"order": "o1", "receipt": {"total": 30}}
    assert [call.resource for call in execution.calls] == [UPDATE, UPDATE, INVOKE]


def test_an_item_out_of_stock_fails_the_order_before_charging():
    execution = run(ORDERS, ORDER, stock("b"))
    assert (execution.error, execution.cause) == ("OutOfStock", "b is out of stock")
    assert INVOKE not in [call.resource for call in execution.calls]
```

Compiling in the test keeps it in step with the source. A definition from elsewhere is a dict too: `json.loads(Path("machine.asl.json").read_text())`.

## The API

```python
run(definition, execution_input, tasks=None, *, functions=None) -> Execution
```

- **`definition`**: the definition as a dict. `${Name}` placeholders stay as written, and the tasks function sees them in `Resource` and `Arguments`.
- **`execution_input`**: the input of the execution, as `StartExecution` would take it once parsed.
- **`tasks`**: a function called with a `Call` for each Task, and for each Map that reads its items through an `ItemReader`. It returns the result of the Task (for the `ItemReader`, the items read), or raises `Failure(error, cause)` to fail it, which the definition's Retry and Catch then handle. Any other exception it raises ends the run and reaches the test as it is. A definition that calls something while `tasks` is `None` raises `ValueError`.
- **`functions`**: JSONata functions replaced by name, without the `$`, for results that change on every run: `functions={"uuid": lambda: "u1", "now": lambda picture=None: "2026-01-01T00:00:00.000Z"}`. The replacements hold for that run only.

A `Call` has `state` (the name of the state), `resource` (its `Resource` as written) and `arguments` (its `Arguments` evaluated, or `None` without any). Tell calls apart by `resource` and `arguments` rather than by `state`: sfnx derives state names from the source, so editing the source, or a minor release, can rename them ([compatibility.md](compatibility.md)).

An `Execution` has:

- **`output`**: what the execution returned. Reading it on a failed execution raises the `Failure`, so a test that expects an output shows the error it got instead.
- **`error`** and **`cause`**: the error of a failed execution, `None` on success.
- **`states`**: the names of the states entered, in order, including those in branches and iterations.
- **`calls`**: every `Call` made, in order, one per attempt when a Retry runs a Task again.

`Unsupported`, a `ValueError`, is raised before anything runs when a state is not in JSONata mode, or the definition has a state or a field the runner does not interpret. Its message names the state and the field. A state is in JSONata mode when it sets `QueryLanguage` to JSONata, or the definition does and the state does not set it; a definition that leaves `QueryLanguage` out is in JSONPath mode, so without it every state, those in branches and Maps included, needs its own. A state in JSONPath mode is rejected this way.

## What runs

Every state type, with the fields it has in JSONata mode:

- **`Assign` and `Output`** on every state that has them, on a Choice rule and on a catcher. Both read the variables from before the state, and a Choice rule that matches assigns and outputs by its own fields, the `Default` by the Choice's, as in Step Functions.
- **`Arguments`** on a Task and a Parallel, whose branches each get it as their input, and **`Retry` and `Catch`** on a Task, a Parallel and a Map.
- **On a Map, inline or distributed**: `Items`, `ItemReader`, `ItemSelector`, `ItemBatcher`, `ResultWriter`, `Label` and the tolerated failures.
- **The rest**: `Choices` and `Default` on a Choice, `Seconds` and `Timestamp` on a Wait, `Error` and `Cause` on a Fail, and `Next` and `End`.

`Comment`, `MaxConcurrency`, `Credentials`, the timing fields of a retrier, and the timeouts of a Task are read and have nothing to do in a local run. A field outside these raises `Unsupported`, the fields of JSONPath mode (`Parameters`, `ResultPath`, `InputPath`, ...) among them.

The Context Object reads as in Step Functions, with fixed placeholder values: `Execution.Id`, `Execution.Name`, `Execution.StartTime`, `State.EnteredTime` and `Task.Token` are the same on every run, and `Execution.Input` is the input given.

## Where a local run differs

- **Time does not pass.** A Wait returns at once, a Retry does not wait between attempts, and `TimeoutSeconds` and `HeartbeatSeconds` never fire on their own; a tasks function raises `Failure("States.Timeout")` to take that path.
- **One thing at a time.** The branches of a Parallel and the iterations of a Map run one after another, in order, whatever `MaxConcurrency` says, so `calls` has that order.
- **Items come from the tasks function.** An `ItemReader` is a call, and its `ReaderConfig` (such as `MaxItems`) is not applied to what the function returns. An `ItemBatcher` cuts batches by `MaxItemsPerBatch` only; `MaxInputBytesPerBatch` is not measured. A `ResultWriter` writes nothing, and the Map's result is a placeholder `MapRunArn` with the key of the manifest Step Functions would write.
- **The JSONata is jsonata-python's.** The functions Step Functions adds (`$parse`, `$uuid`, `$hash`, `$partition`, ...) are there, and so are the behaviors [design.md](design.md#jsonata-in-step-functions) records as measured where jsonata-python differs, such as `$decodeUrlComponent` reading `+` as a space, and `$formatNumber` rounding half to even for the pictures sfnx writes (other pictures round as jsonata-python does). Some measured differences remain: `$substring` counts a negative start in code points where Step Functions counts UTF-16 units; `\s` in a regular expression matches spaces outside ASCII here but only ASCII whitespace in Step Functions; `$sort` may reorder items its function calls equal, which Step Functions keeps in order; `$parse` accepts `NaN` and rejects single quotes, where Step Functions does the opposite; `$random(seed)` gives the same number for the same seed, but not the number Step Functions gives. Others may remain.
- **Error names, not messages.** An expression that fails gives `States.QueryEvaluationError`, as in Step Functions, but its cause is jsonata-python's text, not Step Functions'.
- **No quotas.** The size of payloads and the number of history events are not checked, so `States.DataLimitExceeded` never occurs on its own.

A test that passes locally shows the control flow of the definition against the answers you gave. To check what Step Functions itself computes, run the definition there; [verification.md](verification.md) describes how sfnx measures its own output that way.

## What is public

`run`, `Call`, `Execution`, `Failure`, `Unsupported` and `Tasks` (the type of a tasks function) are the API, and `sfnx.testing.__all__` lists them; [compatibility.md](compatibility.md) says what a release can change of them. The module's other names are its own and change without notice.
