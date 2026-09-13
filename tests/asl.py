"""Run compiled definitions without an emulator, evaluating JSONata with
jsonata-python, so a program's ASL result can be compared with CPython's."""

from collections.abc import Callable, Mapping

import jsonata
from jsonata.utils import Utils


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
    try:
        result = expression.evaluate(None, nulls({**variables, "states": states}))
    except jsonata.JException as exc:
        raise Failure("States.QueryEvaluationError", str(exc)) from exc
    if result is None:
        raise Failure("States.QueryEvaluationError", f"{code} is undefined")
    return Utils.convert_nulls(result)


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


def run(
    definition: Mapping[str, object],
    execution_input: object,
    tasks: Mapping[str, Callable[[object], object]] | None = None,
) -> object:
    """Run a definition. tasks answers each Task by its state name, given the
    evaluated Arguments."""
    context = {"Execution": {"Input": execution_input}}
    return scope(
        definition, {}, execution_input, context, {} if tasks is None else tasks
    )


def scope(
    definition: Mapping[str, object],
    variables: dict[str, object],
    state_input: object,
    context: object,
    tasks: Mapping[str, Callable[[object], object]],
) -> object:
    """The states of a machine or a Parallel branch. A branch gets a copy of
    the variables, so it reads the outside and assigns its own."""
    states = definition["States"]
    assert isinstance(states, dict)
    name = definition["StartAt"]
    for _ in range(10_000):
        state = states[name]
        frame = {"input": state_input, "context": context}
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
            try:
                if kind == "Task":
                    arguments = value(state.get("Arguments"), variables, frame)
                    result = tasks[name](arguments)
                elif kind == "Parallel":
                    result = [
                        scope(branch, dict(variables), state_input, context, tasks)
                        for branch in state["Branches"]
                    ]
                else:
                    result = run_map(state, variables, frame, context, tasks)
                # Assign and Output both read the variables from before the
                # state, and their errors are the state's to catch.
                frame = {**frame, "result": result}
                assigned = {
                    k: value(v, variables, frame)
                    for k, v in state.get("Assign", {}).items()
                }
                output = (
                    value(state["Output"], variables, frame)
                    if "Output" in state
                    else result
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
                frame = {**frame, "errorOutput": error_output}
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


def run_map(
    state: Mapping[str, object],
    variables: dict[str, object],
    frame: object,
    context: Mapping[str, object],
    tasks: Mapping[str, Callable[[object], object]],
) -> object:
    """A Map over Items. Inline iterations read the variables around them;
    distributed ones are child executions whose input is their only data."""
    items = value(state["Items"], variables, frame)
    assert isinstance(items, list)
    processor = state["ItemProcessor"]
    assert isinstance(processor, dict)
    distributed = processor["ProcessorConfig"]["Mode"] == "DISTRIBUTED"
    inputs = []
    if "ItemBatcher" in state:
        batcher = value(state["ItemBatcher"], variables, frame)
        assert isinstance(batcher, dict)
        size = batcher.get("MaxItemsPerBatch", len(items) or 1)
        for start in range(0, len(items), size):
            batch = {"Items": items[start : start + size]}
            if "BatchInput" in batcher:
                batch["BatchInput"] = batcher["BatchInput"]
            inputs.append(batch)
    else:
        for position, item in enumerate(items):
            selecting = {**context, "Map": {"Item": {"Index": position, "Value": item}}}
            selected = value(
                state.get("ItemSelector", "{% $states.context.Map.Item.Value %}"),
                variables,
                {**frame, "context": selecting},
            )
            inputs.append(selected)
    results = []
    for child_input in inputs:
        if distributed:
            child_context = {"Execution": {"Input": child_input}}
            results.append(scope(processor, {}, child_input, child_context, tasks))
        else:
            results.append(
                scope(processor, dict(variables), child_input, context, tasks)
            )
    return results
