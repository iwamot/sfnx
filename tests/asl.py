"""Run compiled definitions without an emulator, evaluating JSONata with
jsonata-python, so a program's ASL result can be compared with CPython's."""

import json
import uuid
from collections.abc import Callable, Mapping

import jsonata
from jsonata.utils import Utils

EXCEEDED = "The specified tolerated failure threshold was exceeded"
MAP_RUN = "arn:aws:states:us-east-1:123456789012:mapRun:machine"


class Failure(Exception):
    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause


def evaluate(code: str, variables: Mapping[str, object], states: object) -> object:
    """jsonata-python reads a Python None as undefined, so JSON null goes in as
    its null value, and an undefined result fails as it does in Step Functions."""
    expression = jsonata.Jsonata(code)
    expression.set_output_convert_nulls(False)
    # The functions Step Functions adds to JSONata.
    expression.register_lambda("parse", parse)
    expression.register_lambda("uuid", lambda: str(uuid.uuid4()))
    try:
        result = expression.evaluate(None, nulls({**variables, "states": states}))
    except jsonata.JException as exc:
        raise Failure("States.QueryEvaluationError", str(exc)) from exc
    if result is None:
        raise Failure("States.QueryEvaluationError", f"{code} is undefined")
    return Utils.convert_nulls(result)


def parse(text: str) -> object:
    """$parse as Python's json reads the text, which accepts NaN and rejects
    single quotes where Step Functions does the opposite."""
    try:
        return nulls(json.loads(text))
    except ValueError as exc:
        raise jsonata.JException(str(exc)) from exc


def nulls(data: object) -> object:
    if data is None:
        return Utils.NULL_VALUE
    if isinstance(data, dict):
        return {k: nulls(v) for k, v in data.items()}
    if isinstance(data, list):
        return [nulls(v) for v in data]
    return data


def value(template: object, variables: Mapping[str, object], states: object) -> object:
    if (
        isinstance(template, str)
        and template.startswith("{%")
        and template.endswith("%}")
    ):
        return evaluate(template[2:-2], variables, states)
    if isinstance(template, dict):
        return {k: value(v, variables, states) for k, v in template.items()}
    if isinstance(template, list):
        return [value(v, variables, states) for v in template]
    return template


def matches(errors: list[str], error: str) -> bool:
    """States.Runtime is caught by nothing. States.ALL matches every other
    error and States.TaskFailed all but States.Timeout, States.DataLimitExceeded
    from a result over the quota included."""
    if error == "States.Runtime":
        return False
    if error in errors or "States.ALL" in errors:
        return True
    return "States.TaskFailed" in errors and error != "States.Timeout"


Tasks = Mapping[str, Callable[[object], object]]


def run(
    definition: Mapping[str, object],
    execution_input: object,
    tasks: Tasks | None = None,
) -> object:
    """Run a definition. tasks answers each Task by its state name, given the
    evaluated Arguments, and gives a Map with an ItemReader its items, given
    the evaluated ItemReader."""
    return scope(
        definition,
        {},
        execution_input,
        execution_context(execution_input),
        {} if tasks is None else tasks,
    )


def execution_context(execution_input: object) -> dict[str, object]:
    """The Context Object of an execution, with placeholder values. RedriveTime
    exists only in a redriven execution."""
    return {
        "Execution": {
            "Id": "arn:aws:states:us-east-1:123456789012:execution:machine:execution",
            "Input": execution_input,
            "Name": "execution",
            "RoleArn": "arn:aws:iam::123456789012:role/machine",
            "StartTime": "2026-01-01T00:00:00Z",
            "RedriveCount": 0,
        },
        "StateMachine": {
            "Id": "arn:aws:states:us-east-1:123456789012:stateMachine:machine",
            "Name": "machine",
        },
    }


def entered(
    context: Mapping[str, object],
    name: str,
    state: Mapping[str, object],
    retries: int = 0,
) -> dict[str, object]:
    """The Context Object in a state. State.RetryCount exists only in the
    states that retry (measured in a Task and a Map), and Task.Token only in a
    .waitForTaskToken Task."""
    about: dict[str, object] = {"EnteredTime": "2026-01-01T00:00:00Z", "Name": name}
    if state["Type"] in {"Task", "Parallel", "Map"}:
        about["RetryCount"] = retries
    entered = {**context, "State": about}
    resource = state.get("Resource")
    if isinstance(resource, str) and resource.endswith(".waitForTaskToken"):
        entered["Task"] = {"Token": "token"}
    return entered


def scope(
    definition: Mapping[str, object],
    variables: dict[str, object],
    state_input: object,
    context: Mapping[str, object],
    tasks: Tasks,
) -> object:
    """The states of a machine or a Parallel branch. A branch gets a copy of
    the variables, so it reads the outside and assigns its own."""
    states = definition["States"]
    assert isinstance(states, dict)
    name = definition["StartAt"]
    for _ in range(10_000):
        state = states[name]
        frame = {"input": state_input, "context": entered(context, name, state)}
        kind = state["Type"]
        if kind == "Succeed":
            return value(state.get("Output", state_input), variables, frame)
        if kind == "Fail":
            raise Failure(
                str(value(state.get("Error", ""), variables, frame)),
                str(value(state.get("Cause", ""), variables, frame)),
            )
        if kind == "Choice":
            for rule in state["Choices"]:
                test = value(rule["Condition"], variables, frame)
                if not isinstance(test, bool):
                    raise Failure(
                        "States.QueryEvaluationError", f"{test!r} is not a boolean"
                    )
                if test:
                    name = rule["Next"]
                    break
            else:
                name = state["Default"]
            continue
        if kind == "Wait":
            if "Seconds" in state:
                seconds = value(state["Seconds"], variables, frame)
                assert isinstance(seconds, int) and 0 <= seconds <= 99_999_999
            else:
                timestamp = value(state["Timestamp"], variables, frame)
                assert isinstance(timestamp, str) and timestamp.endswith("Z")
        elif kind in {"Task", "Parallel", "Map"}:
            retries: list[int] = []
            try:
                assigned, output = retried(
                    state, name, variables, state_input, context, tasks, retries
                )
            except Failure as failure:
                catcher = next(
                    (
                        c
                        for c in state.get("Catch", [])
                        if matches(c["ErrorEquals"], failure.error)
                    ),
                    None,
                )
                if catcher is None:
                    raise
                error_output = {"Error": failure.error, "Cause": failure.cause}
                frame = {
                    "input": state_input,
                    "context": entered(context, name, state, sum(retries)),
                    "errorOutput": error_output,
                }
                assigned = {
                    k: value(v, variables, frame)
                    for k, v in catcher.get("Assign", {}).items()
                }
                variables.update(assigned)
                state_input = error_output
                name = catcher["Next"]
                continue
            variables.update(assigned)
            if state.get("End"):
                return output
            state_input = output
        elif kind == "Pass":
            assigned = {
                k: value(v, variables, frame)
                for k, v in state.get("Assign", {}).items()
            }
            variables.update(assigned)
        else:
            raise AssertionError(f"{kind} is not interpreted yet")
        name = state["Next"]
    raise AssertionError("the definition did not end within 10,000 states")


def retried(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    state_input: object,
    context: Mapping[str, object],
    tasks: Tasks,
    retries: list[int],
) -> tuple[dict[str, object], object]:
    """A Task, Parallel or Map with its Retry, counting in retries the attempts
    of each retrier, which a Catch reads as State.RetryCount. A failure in
    Arguments, Assign or Output is retried too, and only the first retrier that
    matches counts it: once that one has no attempts left, the state fails even
    if a later one matches."""
    retriers = state.get("Retry", [])
    assert isinstance(retriers, list)
    retries[:] = [0] * len(retriers)
    while True:
        frame = {
            "input": state_input,
            "context": entered(context, name, state, sum(retries)),
        }
        try:
            return attempt(state, name, variables, frame, context, tasks)
        except Failure as failure:
            index = next(
                (
                    i
                    for i, retrier in enumerate(retriers)
                    if matches(retrier["ErrorEquals"], failure.error)
                ),
                None,
            )
            if index is None or retries[index] >= retriers[index].get("MaxAttempts", 3):
                raise
            retries[index] += 1


def attempt(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    frame: dict[str, object],
    context: Mapping[str, object],
    tasks: Tasks,
) -> tuple[dict[str, object], object]:
    kind = state["Type"]
    if kind == "Task":
        arguments = value(state.get("Arguments"), variables, frame)
        result = tasks[name](arguments)
    elif kind == "Parallel":
        branches = state["Branches"]
        assert isinstance(branches, list)
        result = [
            scope(branch, dict(variables), frame["input"], context, tasks)
            for branch in branches
        ]
    else:
        result = run_map(state, name, variables, frame, context, tasks)
    # Assign and Output both read the variables from before the state, and
    # their errors are the state's to retry and catch.
    frame = {**frame, "result": result}
    assigned = {
        k: value(v, variables, frame) for k, v in state.get("Assign", {}).items()
    }
    output = value(state["Output"], variables, frame) if "Output" in state else result
    return assigned, output


def run_map(
    state: Mapping[str, object],
    name: str,
    variables: dict[str, object],
    frame: Mapping[str, object],
    context: Mapping[str, object],
    tasks: Tasks,
) -> object:
    """A Map over Items or what its ItemReader reads. Inline iterations read the
    variables around them; distributed ones are child executions whose input is
    their only data.

    An object of items passes each entry as {"Key": ..., "Value": ...}, whose
    fields Map.Item has too. A failed child execution takes its place in the
    result with its error and counts each of its items as failed; unless the
    thresholds tolerate them, the Map fails with
    States.ExceedToleratedFailureThreshold. With a ResultWriter, the result is
    where the results were written."""
    processor = state["ItemProcessor"]
    assert isinstance(processor, dict)
    distributed = processor["ProcessorConfig"]["Mode"] == "DISTRIBUTED"
    if "ItemReader" in state:
        items = tasks[name](value(state["ItemReader"], variables, frame))
    else:
        items = value(state["Items"], variables, frame)
    if isinstance(items, dict):
        assert distributed and "ItemBatcher" not in state
        entries: list[dict[str, object]] = [
            {"Key": k, "Value": v} for k, v in items.items()
        ]
    else:
        assert isinstance(items, list)
        entries = [{"Value": v} for v in items]
    # Each child's input, with the number of items it takes.
    inputs: list[tuple[object, int]] = []
    if "ItemBatcher" in state:
        batcher = value(state["ItemBatcher"], variables, frame)
        assert isinstance(batcher, dict)
        size = batcher.get("MaxItemsPerBatch", len(items) or 1)
        for start in range(0, len(items), size):
            batch = {"Items": items[start : start + size]}
            if "BatchInput" in batcher:
                batch["BatchInput"] = batcher["BatchInput"]
            inputs.append((batch, len(batch["Items"])))
    elif "ItemSelector" in state:
        entered_context = frame["context"]
        assert isinstance(entered_context, dict)
        for position, entry in enumerate(entries):
            item = {"Index": position, **entry}
            selecting = {**entered_context, "Map": {"Item": item}}
            selected = value(
                state["ItemSelector"], variables, {**frame, "context": selecting}
            )
            inputs.append((selected, 1))
    else:
        objects = isinstance(items, dict)
        inputs = [(entry if objects else entry["Value"], 1) for entry in entries]
    results = []
    failed = 0
    for child_input, taken in inputs:
        if distributed:
            execution = context["Execution"]
            assert isinstance(execution, dict)
            child_context = {
                **context,
                "Execution": {**execution, "Input": child_input},
            }
            try:
                results.append(scope(processor, {}, child_input, child_context, tasks))
            except Failure as failure:
                failed += taken
                results.append(
                    {"Status": "FAILED", "Error": failure.error, "Cause": failure.cause}
                )
        else:
            results.append(
                scope(processor, dict(variables), child_input, context, tasks)
            )
    if failed:
        count = value(state.get("ToleratedFailureCount", 0), variables, frame)
        percentage = value(state.get("ToleratedFailurePercentage", 0), variables, frame)
        assert isinstance(count, int) and isinstance(percentage, int)
        if exceeds(failed, len(entries), count, percentage):
            raise Failure("States.ExceedToleratedFailureThreshold", EXCEEDED)
    if "ResultWriter" in state:
        writer = value(state["ResultWriter"], variables, frame)
        assert isinstance(writer, dict)
        arguments = writer["Arguments"]
        return {
            "MapRunArn": f"{MAP_RUN}/{state.get('Label', 'map')}:run",
            "ResultWriterDetails": {
                "Bucket": arguments["Bucket"],
                "Key": f"{arguments['Prefix']}/run/manifest.json",
            },
        }
    return results


def exceeds(failed: int, total: int, count: int, percentage: int) -> bool:
    """Whether failed items fail a distributed Map: with no threshold set, any
    failure does, and otherwise exceeding one that is set does. A threshold of 0
    is not set, though the documentation does not say so."""
    if not count and not percentage:
        return failed > 0
    return (
        bool(count)
        and failed > count
        or bool(percentage)
        and failed * 100 > percentage * total
    )
