import ast
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.module import constants
from tests import asl

LAMBDA = "arn:aws:states:::lambda:invoke"
GET_ITEM = "arn:aws:states:::dynamodb:getItem"


def source(outside: str, body: str) -> str:
    """A module that assigns names at its top level and holds one machine."""
    return (
        "from sfnx import Timeout, distributed_map, parallel, state_machine, task\n\n\n"
        "class Lambda:\n    class ServiceException(Exception):\n        pass\n\n\n"
        f"{textwrap.dedent(outside)}\n\n"
        "@state_machine\ndef flow(input):\n"
        + textwrap.indent(textwrap.dedent(body), "    ")
    )


def definition(outside: str, body: str) -> dict:
    (compiled,) = compile_source(source(outside, body)).values()
    return compiled


def rejected(outside: str, body: str) -> str:
    with pytest.raises(CompileError) as raised:
        definition(outside, body)
    return raised.value.message


def test_the_names_a_module_assigns_at_its_top_level():
    found = constants(
        ast.parse("A = 1\nB: int = 2\nC = D = 3\nB += 1\nE: int\nF.g = 4\n")
    )
    assert set(found) == {"A", "B"}
    assert found["B"].declared is not None
    assert found["A"].declared is None


def test_a_constant_is_written_where_it_is_used():
    compiled = definition(
        'TABLE = "stock"',
        f'return task("{GET_ITEM}", {{"TableName": TABLE, "Key": {{"id": {{"S": "1"}}}}}})',
    )
    arguments = compiled["States"]["return"]["Arguments"]
    assert arguments == {"TableName": "stock", "Key": {"id": {"S": "1"}}}


def test_a_constant_reads_another_constant():
    compiled = definition(
        'TABLE = "stock"\nQUERY = {"TableName": TABLE, "Key": {"id": {"S": "1"}}}',
        f'return task("{GET_ITEM}", QUERY)',
    )
    arguments = compiled["States"]["return"]["Arguments"]
    assert arguments == {"TableName": "stock", "Key": {"id": {"S": "1"}}}


def test_the_last_assignment_of_a_name_is_what_it_holds():
    compiled = definition("LIMIT = 1\nLIMIT = 2", "return LIMIT")
    assert compiled["States"]["return"]["Output"] == 2


def test_a_constant_holds_what_the_name_it_read_held_there():
    written = source("A = 1\nB = A\nA = 2", "return [A, B]")
    (compiled,) = compile_source(written).values()
    namespace: dict[str, object] = {}
    exec(written, namespace)
    flow = namespace["flow"]
    assert callable(flow)
    assert asl.run(compiled, {}) == flow({}) == [2, 1]


def test_a_constant_takes_no_state_of_its_own():
    compiled = definition(
        'QUERY = {"TableName": "stock", "Key": {"id": {"S": "1"}}}',
        f'return task("{GET_ITEM}", QUERY)',
    )
    assert list(compiled["States"]) == ["return"]


def test_an_annotation_declares_the_type_of_a_constant():
    compiled = definition('TOP: list[str] = ["a", "b"]', 'return TOP[0] + "!"')
    assert compiled["States"]["return"]["Output"] == "{% (['a', 'b'])[0] & '!' %}"


def test_an_annotation_a_constant_cannot_declare_is_rejected():
    assert "annotate with" in rejected('TOP: tuple = ["a"]', "return TOP")


def test_a_retrier_is_written_once_and_named_where_it_is_used():
    compiled = definition(
        'RETRIES = [{"ErrorEquals": [Lambda.ServiceException], "MaxAttempts": 3}]',
        f'a = task("{LAMBDA}", {{"FunctionName": "a"}}, retry=RETRIES)\n'
        f'b = task("{LAMBDA}", {{"FunctionName": "b"}}, retry=RETRIES)\n'
        "return [a, b]",
    )
    retry = [{"ErrorEquals": ["Lambda.ServiceException"], "MaxAttempts": 3}]
    assert compiled["States"]["a"]["Retry"] == retry
    assert compiled["States"]["b"]["Retry"] == retry


def test_a_retrier_and_its_errors_are_read_one_by_one():
    compiled = definition(
        "ERRORS = [Timeout, Lambda.ServiceException]\n"
        'SLOW = {"ErrorEquals": ERRORS, "MaxAttempts": 2}',
        f'return task("{LAMBDA}", {{"FunctionName": "a"}}, retry=[SLOW])',
    )
    assert compiled["States"]["return"]["Retry"] == [
        {"ErrorEquals": ["States.Timeout", "Lambda.ServiceException"], "MaxAttempts": 2}
    ]


def test_the_resource_of_a_task_is_read_from_a_constant():
    compiled = definition(
        f'INVOKE = "{LAMBDA}"', 'return task(INVOKE, {"FunctionName": "a"})'
    )
    assert compiled["States"]["return"]["Resource"] == LAMBDA


def test_the_timeout_of_the_machine_is_read_from_a_constant():
    source = (
        "from sfnx import state_machine\n\n\nTIMEOUT = 300\n\n\n"
        "@state_machine(timeout=TIMEOUT)\ndef flow(input):\n    return 1\n"
    )
    (compiled,) = compile_source(source).values()
    assert compiled["TimeoutSeconds"] == 300


def test_the_label_and_the_arguments_of_a_map_are_read_from_constants():
    compiled = definition(
        'LABEL = "run"\nARGS = {"factor": 2}',
        "def each(item, factor):\n"
        "    return factor\n\n"
        'items: list = input["items"]\n'
        "return distributed_map(each, items, label=LABEL, args=ARGS)",
    )
    state = compiled["States"]["return"]
    assert state["Label"] == "run"
    assert state["ItemSelector"]["factor"] == 2


def test_a_branch_reads_a_constant():
    compiled = definition(
        "LIMIT = 10",
        "def one():\n    return LIMIT\n\ndef two():\n    return 2\n\nreturn parallel(one, two)",
    )
    branch = compiled["States"]["return"]["Branches"][0]
    assert branch["States"]["one.return"]["Output"] == 10


def test_a_distributed_map_reads_a_constant_its_child_execution_cannot():
    compiled = definition(
        "LIMIT = 10",
        "def each(item):\n"
        "    return [item, LIMIT]\n\n"
        'items: list = input["items"]\n'
        "return distributed_map(each, items)",
    )
    processor = compiled["States"]["return"]["ItemProcessor"]["States"]
    assert processor["each.return"]["Output"] == [
        "{% $states.context.Execution.Input.item %}",
        10,
    ]


def test_a_variable_of_the_machine_wins_over_a_constant():
    compiled = definition('TABLE = "outside"', 'TABLE = "inside"\nreturn TABLE')
    assert compiled["States"]["TABLE"]["Assign"] == {"TABLE": "inside"}
    assert compiled["States"]["return"]["Output"] == "{% $TABLE %}"


def test_a_loop_variable_of_the_machine_wins_over_a_constant():
    message = rejected(
        'ITEM = "outside"',
        'items: list = input["items"]\nfor ITEM in items:\n    total = ITEM\nreturn ITEM',
    )
    assert "ITEM is the loop variable" in message


def test_a_constant_the_compiler_would_have_to_run_is_rejected():
    message = rejected("import time\n\nNOW = time.time()", "return NOW")
    assert message.startswith(
        "NOW holds time.time(), which the compiler would have to run"
    )


def test_a_computed_part_of_a_constant_is_rejected():
    message = rejected('PREFIX = ["a" + "b"]', "return PREFIX")
    assert message.startswith("PREFIX holds 'a' + 'b'")


def test_a_constant_assigned_from_itself_is_rejected():
    assert rejected("A = A", "return A") == (
        "A is assigned from itself outside the machine; write the value out"
    )


def test_constants_assigned_from_each_other_are_rejected():
    assert "assigned from itself" in rejected("A = [B]\nB = [A]", "return A")


def test_a_constant_assigned_from_itself_in_a_retrier_is_rejected():
    message = rejected(
        "RETRIES = RETRIES",
        f'return task("{LAMBDA}", {{"FunctionName": "a"}}, retry=RETRIES)',
    )
    assert "assigned from itself" in message


def test_a_name_imported_from_elsewhere_is_not_a_constant():
    source = (
        "from sfnx import state_machine\nfrom config import TABLE\n\n\n"
        "@state_machine\ndef flow(input):\n    return TABLE\n"
    )
    with pytest.raises(CompileError) as raised:
        compile_source(source)
    assert (
        raised.value.message == "TABLE is not assigned here; assign it before this line"
    )


def test_a_constant_does_not_read_a_variable_of_the_machine():
    message = rejected(
        'QUERY = {"TableName": name}', 'name = input["name"]\nreturn QUERY'
    )
    assert message == "name is not assigned here; assign it before this line"


def test_the_module_runs_in_python_and_the_asl_gives_the_same_value():
    written = source(
        "RATE = 0.1\nFEES: list[float] = [1.0, 2.0]",
        'total: float = input["total"]\nreturn [total * RATE, FEES[0]]',
    )
    (compiled,) = compile_source(written).values()
    namespace: dict[str, object] = {}
    exec(written, namespace)
    flow = namespace["flow"]
    assert callable(flow)
    execution_input = {"total": 20.0}
    assert asl.run(compiled, execution_input) == flow(execution_input) == [2.0, 1.0]
