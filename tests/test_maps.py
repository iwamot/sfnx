import re
import textwrap

import pytest

import sfnx
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"
LAMBDA = "arn:aws:states:::lambda:invoke"
HEADER = (
    "from sfnx import Timeout, distributed_map, inline_map, parallel, state_machine, task\n\n\n"
    "class Declined(Exception):\n    pass\n"
)


def source(body: str, after: str = "") -> str:
    return (
        HEADER
        + "\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
        + after
    )


def states(body: str, after: str = "") -> dict:
    (compiled,) = compile_source(source(body, after)).values()
    return compiled["States"]


def run(body: str, execution_input: object, tasks: dict | None = None) -> object:
    (compiled,) = compile_source(source(body)).values()
    return asl.run(compiled, execution_input, tasks)


def test_inline_map_matches_the_handwritten_map():
    # An inline Map passing the item and its index through the ItemSelector.
    body = 'def twice(value, index):\n    return {"index": index, "value": value * 2}\n\nreturn inline_map(twice, input["items"], max_concurrency=2)'
    assert states(body)["return"] == {
        "Type": "Map",
        "Items": f"{{% {INPUT}.items %}}",
        "ItemSelector": {
            "value": "{% $states.context.Map.Item.Value %}",
            "index": "{% $states.context.Map.Item.Index %}",
        },
        "MaxConcurrency": 2,
        "ItemProcessor": {
            "ProcessorConfig": {"Mode": "INLINE"},
            "StartAt": "twice.return",
            "States": {
                "twice.return": {
                    "Type": "Succeed",
                    "Output": {
                        "index": "{% $states.input.index %}",
                        "value": "{% $states.input.value * 2 %}",
                    },
                }
            },
        },
        "End": True,
    }


def test_a_function_that_makes_states_binds_its_parameters_first():
    body = f'rate: float = input["rate"]\n\ndef charge(order):\n    receipt = task("{LAMBDA}", {{"FunctionName": "charge", "Payload": order}})\n    return receipt["Payload"]["amount"] * rate\n\ntotals = inline_map(charge, input["orders"])\nreturn totals'
    compiled = states(body)
    processor = compiled["totals"]["ItemProcessor"]
    assert processor["StartAt"] == "charge.order"
    assert processor["States"]["charge.order"] == {
        "Type": "Pass",
        "Assign": {"order": "{% $states.input.order %}"},
        "Next": "charge.receipt",
    }
    assert (
        processor["States"]["charge.receipt"]["Arguments"]["Payload"] == "{% $order %}"
    )
    # The return right after the Map ends the machine with its result.
    assert "Assign" not in compiled["totals"]
    assert compiled["totals"]["End"] is True
    tasks = {
        "charge.receipt": lambda arguments: {
            "Payload": {"amount": arguments["Payload"]["amount"]}
        }
    }
    assert run(body, {"rate": 2, "orders": [{"amount": 1}, {"amount": 3}]}, tasks) == [
        2,
        6,
    ]


def test_distributed_map_matches_the_handwritten_map():
    # A distributed Map reading S3, batching, tolerating failures and writing results.
    body = (
        'def summarize(items, factor: float):\n    return {"count": len(items), "first": items[0]["amount"] * factor}\n\n'
        "return distributed_map(\n    summarize,\n"
        '    source={"Resource": "arn:aws:states:::s3:getObject", "ReaderConfig": {"InputType": "JSON", "MaxItems": 1000},\n'
        '            "Arguments": {"Bucket": input["sourceBucket"], "Key": input["sourceKey"]}},\n'
        '    args={"factor": input["factor"]},\n'
        '    batch={"MaxItemsPerBatch": input["batchSize"], "MaxInputBytesPerBatch": 65536},\n'
        '    max_concurrency=input["concurrency"], tolerated_failure_count=1, tolerated_failure_percentage=5, label="Dataset",\n'
        '    result={"Resource": "arn:aws:states:::s3:putObject", "Arguments": {"Bucket": input["resultBucket"], "Prefix": "sfnx/results"},\n'
        '            "WriterConfig": {"Transformation": "NONE", "OutputType": "JSON"}},\n'
        ")"
    )
    state = states(body)["return"]
    assert list(state) == [
        "Type",
        "Label",
        "ItemReader",
        "ItemBatcher",
        "MaxConcurrency",
        "ToleratedFailureCount",
        "ToleratedFailurePercentage",
        "ItemProcessor",
        "ResultWriter",
        "End",
    ]
    assert state["ItemBatcher"]["BatchInput"] == {"factor": f"{{% {INPUT}.factor %}}"}
    assert state["ItemProcessor"]["ProcessorConfig"] == {
        "Mode": "DISTRIBUTED",
        "ExecutionType": "STANDARD",
    }
    assert state["ItemProcessor"]["States"]["summarize.return"]["Output"] == {
        "count": f"{{% $count({INPUT}.Items) %}}",
        "first": f"{{% {INPUT}.Items[0].amount * {INPUT}.BatchInput.factor %}}",
    }


def test_distributed_map_passes_args_through_the_item_selector():
    body = 'rate: float = input["rate"]\n\ndef charge(order, rate):\n    return order["amount"] * rate\n\nreturn distributed_map(charge, input["orders"], args={"rate": rate}, execution_type="EXPRESS")'
    state = states(body)["return"]
    assert state["ItemSelector"] == {
        "order": "{% $states.context.Map.Item.Value %}",
        "rate": "{% $rate %}",
    }
    assert (
        state["ItemProcessor"]["States"]["charge.return"]["Output"]
        == f"{{% {INPUT}.order.amount * {INPUT}.rate %}}"
    )
    assert state["ItemProcessor"]["ProcessorConfig"]["ExecutionType"] == "EXPRESS"
    assert run(body, {"rate": 3, "orders": [{"amount": 1}, {"amount": 2}]}) == [3, 6]


def test_map_names_retry_and_catch():
    body = 'def f(x):\n    return x\n\ntry:\n    inline_map(f, input["xs"], retry=[{"ErrorEquals": [Timeout]}])\nexcept Declined:\n    return 0\nreturn 1'
    state = states(body)["map"]
    assert state["Retry"] == [{"ErrorEquals": ["States.Timeout"]}]
    assert state["Catch"][0]["ErrorEquals"] == ["Declined"]


def test_result_types():
    body = 'def f(x):\n    return "a"\n\nnames = inline_map(f, input["xs"])\nreturn len(names)'
    assert states(body)["names"]["Output"] == "{% $count($states.result) %}"
    body = 'def f(x: list):\n    return len(x)\n\nreturn inline_map(f, input["xs"])'
    assert (
        states(body)["return"]["ItemProcessor"]["States"]["f.return"]["Output"]
        == "{% $count($states.input.x) %}"
    )
    body = 'xs: list[str] = input["xs"]\ndef f(x):\n    return x + "!"\n\nreturn inline_map(f, xs)'
    assert (
        states(body)["return"]["ItemProcessor"]["States"]["f.return"]["Output"]
        == "{% $states.input.x & '!' %}"
    )
    body = 'def f(x):\n    return 1\n\nr = distributed_map(f, source={"Resource": "arn:aws:states:::s3:listObjectsV2"}, result={"WriterConfig": {"OutputType": "JSON"}})\nreturn len(r)'
    with pytest.raises(CompileError, match="len depends on the type"):
        states(body)


def test_nested_maps_and_batches_evaluate():
    body = 'def row(cells: list[float]):\n    def cell(value):\n        return value * 10\n    return inline_map(cell, cells)\n\nreturn inline_map(row, input["rows"])'
    assert run(body, {"rows": [[1, 2], [3]]}) == [[10, 20], [30]]
    body = 'def count(items, label):\n    return {"n": len(items), "label": label}\n\nreturn distributed_map(count, input["xs"], args={"label": input["label"]}, batch={"MaxItemsPerBatch": 2})'
    assert run(body, {"xs": [1, 2, 3], "label": "b"}) == [
        {"n": 2, "label": "b"},
        {"n": 1, "label": "b"},
    ]


@pytest.mark.parametrize(
    "function, expected",
    [
        (
            "def f(x: float):\n    if x > 0:\n        pass\n    else:\n        x = 10\n    return x",
            [10, 1],
        ),
        ("def f(x: float):\n    if x > 0:\n        x = 10\n    return x", [-1, 10]),
        ("def f(x: float):\n    while x < 3:\n        x = x + 1\n    return x", [3, 3]),
    ],
)
def test_a_parameter_the_function_assigns_holds_its_value(function, expected):
    # The paths that do not assign it, and a loop leading back, read the value.
    for call in ("inline_map(f, [-1, 1])", "distributed_map(f, [-1, 1])"):
        assert run(f"{function}\n\nreturn {call}", {}) == expected


def test_a_function_that_always_raises():
    body = 'def f(x):\n    raise Declined("no")\n\nreturn inline_map(f, [1])'
    with pytest.raises(asl.Failure, match="Declined: no"):
        run(body, {})


def test_a_label_in_a_loop_is_used_once():
    body = 'total = 0\nfor i in range(2):\n    def f(x):\n        return x\n    r = distributed_map(f, [i], label="once")\n    total = total + r[0]\nreturn total'
    assert run(body, {}) == 1


def test_batches_without_args_have_no_batch_input():
    body = 'def count(items):\n    return len(items)\n\nreturn distributed_map(count, input["xs"], batch={"MaxItemsPerBatch": 2})'
    assert states(body)["return"]["ItemBatcher"] == {"MaxItemsPerBatch": 2}
    assert run(body, {"xs": [1, 2, 3]}) == [2, 1]


def test_runtime_helpers_run_in_python():
    assert sfnx.inline_map(lambda x: x * 2, [1, 2]) == [2, 4]
    assert sfnx.inline_map(lambda x, i: [x, i], ["a"]) == [["a", 0]]
    assert sfnx.distributed_map(lambda x, rate: x * rate, [1, 2], args={"rate": 3}) == [
        3,
        6,
    ]
    with pytest.raises(NotImplementedError, match="runs in Step Functions"):
        sfnx.distributed_map(lambda x: x, source={})


def test_distributed_map_passes_what_step_functions_passes_in_python():
    body = 'def f(v: int):\n    return v * 2\n\nreturn distributed_map(f, {"a": 1, "b": 2})'
    assert run(body, {}) == [2, 4]
    assert sfnx.distributed_map(lambda v: v * 2, {"a": 1, "b": 2}) == [2, 4]
    batches = [
        ({"MaxItemsPerBatch": 2}, [[1, 2], [3]]),
        ({"MaxInputBytesPerBatch": 100}, [[1, 2, 3]]),
    ]
    for batch, expected in batches:
        assert sfnx.distributed_map(lambda xs: xs, [1, 2, 3], batch=batch) == expected
    assert sfnx.distributed_map(lambda xs: xs, [], batch={"MaxItemsPerBatch": 2}) == []
    assert sfnx.distributed_map(
        lambda xs, label: [label, *xs],
        [1],
        args={"label": "b"},
        batch={"MaxInputBytesPerBatch": 100},
    ) == [["b", 1]]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "def f(x):\n    return x\nreturn inline_map(f)",
            "inline_map takes the function and the items",
        ),
        (
            'def f(x):\n    return x\nreturn inline_map(f, input["xs"], label="a")',
            "this map takes max_concurrency, retry=",
        ),
        (
            'def f():\n    return 1\nreturn inline_map(f, input["xs"])',
            "takes the item, and its index if needed",
        ),
        (
            'def f(a, b, c):\n    return 1\nreturn inline_map(f, input["xs"])',
            "takes the item, and its index if needed",
        ),
        (
            'def f(x=1):\n    return 1\nreturn inline_map(f, input["xs"])',
            "takes plain parameters only",
        ),
        (
            'def f(x):\n    return x\nreturn inline_map(f, "abc")',
            "'abc' is a string; inline_map takes a list",
        ),
        ('return inline_map(lambda x: x, input["xs"])', "pass the function by name"),
        (
            'def f(x):\n    return x\nreturn inline_map(f, input["xs"], max_concurrency=-1)',
            "max_concurrency is a whole number from 0 or more",
        ),
        (
            'def f(x):\n    return x\nreturn inline_map(f, input["xs"], max_concurrency="2")',
            "max_concurrency is a number, not a string",
        ),
        (
            f'order = 1\ndef f(order):\n    task("{LAMBDA}", {{"FunctionName": "f"}})\n    return order\nreturn inline_map(f, input["xs"])',
            "order is assigned outside this function too",
        ),
        (
            'def f(x):\n    return x\nr = inline_map(f, input["xs"]) if input["a"] else 0',
            "inline_map() here would run whether or not this part is taken",
        ),
        (
            "def f(x):\n    return x\nreturn distributed_map(f)",
            "give the items either as the second argument or as source=",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], source={"Resource": "r"})',
            "not both or neither",
        ),
        (
            "def f(x):\n    return x\nreturn distributed_map()",
            "distributed_map takes the function",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, "abc")',
            "distributed_map takes a list or a dict",
        ),
        (
            'def f(x, rate):\n    return x\nreturn distributed_map(f, input["xs"])',
            "exactly the names in args=",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], args={"rate": 1})',
            "exactly the names in args=",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], args=input)',
            "args is a dict written here",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], args={1: 2})',
            "the keys of args are strings",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, source={"Arguments": {}})',
            "source needs Resource",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, source={"Resource": "r", "Path": 1})',
            "source takes Arguments, ReaderConfig, Resource",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], batch={})',
            "batch sets MaxItemsPerBatch, MaxInputBytesPerBatch or both",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], batch={"BatchInput": {}})',
            "batch takes MaxInputBytesPerBatch, MaxItemsPerBatch",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], tolerated_failure_percentage=101)',
            "tolerated_failure_percentage is a whole number from 0 to 100",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], label="has space")',
            "label is 1 to 40 characters",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], label="a" * 41)',
            "label is a literal string",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], label="'
            + "a" * 41
            + '")',
            "label is 1 to 40 characters",
        ),
        (
            'def f(x):\n    return x\nreturn distributed_map(f, input["xs"], execution_type="FAST")',
            'execution_type is "STANDARD" or "EXPRESS"',
        ),
        (
            'def f(x):\n    return x\na = distributed_map(f, input["xs"], label="same")\nreturn distributed_map(f, input["xs"], label="same")',
            "another distributed_map is labeled same; give each its own label",
        ),
        # The parameter a function that makes states binds is its own to assign.
        (
            "def f(x):\n    def g():\n        x = 2\n        return x\n    parallel(g)\n    return x\nreturn inline_map(f, [1])",
            "x is assigned outside this function too",
        ),
        (
            'rate = 2\ndef f(x):\n    return x * rate\nreturn distributed_map(f, input["xs"])',
            "rate is outside f, which distributed_map runs as a child execution that cannot read it; pass it with args=",
        ),
        (
            'def f(x):\n    return input\nreturn distributed_map(f, input["xs"])',
            "input is outside f",
        ),
        (
            'def f(x):\n    if x:\n        y = 1\n    return y\nreturn distributed_map(f, input["xs"])',
            "y is not assigned on every path",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        states(body)
    assert message in raised.value.message


def test_module_level_functions_see_none_of_the_machine():
    with pytest.raises(CompileError, match=re.escape("rate is not assigned here")):
        states(
            'rate = 1\nreturn inline_map(scale, input["xs"])',
            "\n\ndef scale(x):\n    return x * rate\n",
        )
