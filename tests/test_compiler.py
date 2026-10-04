import copy
import json
import logging
import re
import textwrap
from pathlib import Path

import pytest

from sfnx import testing
from sfnx.compiler import (
    changed_states,
    compile_source,
    definitions,
    from_asl,
    misread,
    note_reads,
)
from sfnx.diagnostics import CompileError
from tests import asl

HEADER = "from sfnx import state_machine\n\n\n"


def compile_one(source: str) -> dict:
    (definition,) = compile_source(textwrap.dedent(source)).values()
    return definition


def machine(body: str) -> str:
    return HEADER + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")


def python(source: str, execution_input: object) -> object:
    namespace: dict[str, object] = {}
    exec(source, namespace)
    function = namespace["pay"]
    assert callable(function)
    return function(execution_input)


def test_succeed_only():
    assert compile_one(machine('return input["amount"]')) == {
        "QueryLanguage": "JSONata",
        "StartAt": "return",
        "States": {
            "return": {
                "Type": "Succeed",
                "Output": "{% $states.context.Execution.Input.amount %}",
            }
        },
    }


def test_a_return_of_an_assignment_is_one_succeed():
    """Nothing reads the variable after the return, so the Output reads its
    expression, and fails where the Pass would."""
    definition = compile_one(machine('amount = input["amount"]\nreturn amount'))
    assert definition == {
        "QueryLanguage": "JSONata",
        "StartAt": "return",
        "States": {
            "return": {
                "Type": "Succeed",
                "Output": "{% $states.context.Execution.Input.amount %}",
            },
        },
    }


def test_independent_assignments_share_a_pass():
    definition = compile_one(
        machine('fee = 10\nrate = input["rate"]\nreturn [fee, rate]')
    )
    assert definition["States"] == {
        "fee": {
            "Type": "Pass",
            "Assign": {"fee": 10, "rate": "{% $states.context.Execution.Input.rate %}"},
            "Next": "return",
        },
        "return": {"Type": "Succeed", "Output": ["{% $fee %}", "{% $rate %}"]},
    }


BOUND_HEADER = "from sfnx import aws, state_machine\n\n\n"


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        # A list built by a comprehension, read three times by one dict.
        (
            (
                'xs: list = input["xs"]\nids = [x["id"] for x in xs if x["bad"]]\n'
                's = {"n": len(ids), "ids": ids}\nreturn s'
            ),
            {"xs": [{"id": 1, "bad": True}, {"id": 2, "bad": False}]},
            {"n": 1, "ids": [1]},
        ),
        # The same after a Task, in its Assign.
        (
            (
                'rs: list = aws.optimized.lambda_.invoke(FunctionName="f", Payload={})'
                '["Payload"]\nids = [r["id"] for r in rs if r["bad"]]\n'
                's = {"n": len(ids), "ids": ids}\n'
                'aws.sdk.s3.put_object(Bucket="b", Key="k", Body=str(s))\nreturn s'
            ),
            {},
            {"n": 1, "ids": [1]},
        ),
        # An undefined value is as undefined bound as written at each read.
        ('p = input["p"] + 1\nr = {"a": p * 2, "b": p * 3}\nreturn r', {}, {}),
    ],
)
def test_a_long_value_read_more_than_once_is_bound_once(
    body, execution_input, expected
):
    """A value that reads a long pending one more than once, on every
    evaluation, binds it at its start rather than writing it at each read."""
    definition = compile_one(
        BOUND_HEADER
        + "@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )
    assert ":=" in json.dumps(definition)

    items = [{"id": 1, "bad": True}, {"id": 2, "bad": False}]
    tasks = {"rs": lambda _: {"Payload": items}, "putObject": lambda _: {}}
    assert asl.run(definition, execution_input, tasks) == expected


@pytest.mark.parametrize("caught", [False, True])
def test_a_binding_moves_only_which_failure_the_cause_names(caught):
    """A value bound at the start is evaluated before what comes first in the
    value: where both fail, the cause names the bound one
    (AD-FAILURE-ORDER), while the error, the calls and the way taken stay."""
    call = 'aws.sdk.s3.put_object(Bucket="b", Key="k", Body=str(s))'
    if caught:
        call = f"try:\n    {call}\nexcept Exception:\n    return 0"
    body = (
        'p = float(input["p"])\ns = {"first": float(input["q"]), "a": p, "b": p}\n'
        f"{call}\nreturn s"
    )
    definition = compile_one(
        BOUND_HEADER
        + "@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )
    assert ":=" in json.dumps(definition)
    failed = testing.run(definition, {"p": "x", "q": "y"}, lambda _: {})
    assert failed.error == "States.QueryEvaluationError"
    assert failed.cause is not None and '"x"' in failed.cause
    assert not failed.calls
    passed = testing.run(definition, {"p": "1", "q": "2"}, lambda _: {})
    assert passed.output == {"first": 2, "a": 1, "b": 1}
    assert len(passed.calls) == 1


def test_a_value_read_on_one_side_of_a_test_is_not_bound():
    """A binding at the start would evaluate a value the test may skip, and
    $number("a") fails, so the value stays written at each read."""
    body = (
        'n = float(input["s"])\n'
        'r = {"a": n * 2 if input["c"] else 0, "b": n * 3 if input["c"] else 0}\n'
        "return r"
    )
    definition = compile_one(machine(body))
    assert ":=" not in json.dumps(definition)
    assert asl.run(definition, {"s": "a", "c": False}) == {"a": 0, "b": 0}


def test_a_read_of_a_pending_assignment_reads_its_expression():
    """Assign reads the values from before the state, so b reads what a takes
    rather than $a, and the two share a Pass, as a hand-writer spells a path
    out again."""
    body = 'a = input["n"]\nb = [a]\nc = 2\nreturn [a, b, c]'
    definition = compile_one(machine(body))
    n = "{% $states.context.Execution.Input.n %}"
    assert definition["States"]["a"]["Assign"] == {"a": n, "b": [n], "c": 2}


@pytest.mark.parametrize(
    "body, output",
    [
        ("a = random.random()\nreturn a", "{% $random() %}"),
        ("a = random.random()\nb = [a]\nreturn b", ["{% $random() %}"]),
    ],
)
def test_a_value_that_changes_read_once_by_the_return_goes_in_its_output(body, output):
    """The Output evaluates it once, as the Pass would, with no Task or Wait
    between the two."""
    definition = compile_one("import random\n" + machine(body))
    assert definition["States"] == {"return": {"Type": "Succeed", "Output": output}}


def test_reassignment_starts_a_new_state_with_a_serial_name():
    """The new value reads the first, which may be undefined, so the first
    keeps its state."""
    body = 'x: float = input["w"]\nx = x + input["x"]\nreturn [x]'
    definition = compile_one(machine(body))
    assert list(definition["States"]) == ["x", "x_2", "return"]
    assert definition["States"]["x"]["Next"] == "x_2"


@pytest.mark.parametrize(
    "first",
    ["3", 'input["x"].get("k", 1)', "y"],
)
def test_a_first_value_that_cannot_fail_is_replaced_in_the_same_state(first):
    """A first value that neither fails nor is undefined has nothing for
    Python to evaluate, so the new value takes its place."""
    body = f'y = 2\nn = {first}\ns = "a"\nn = 0\nreturn [n, s, y]'
    definition = compile_one(machine(body))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == [0, "a", 2]


@pytest.mark.parametrize(
    "first",
    ['input["x"]', '10 / input.get("d", 1)', "random.random()"],
)
def test_a_first_value_nothing_reads_goes(first):
    """Nothing reads the first n before n = 0, so a hand-writer would not
    evaluate it: a missing key fails nowhere, which the table of differences
    lists."""
    body = f'n = {first}\ns = "a"\nn = 0\nreturn [n, s]'
    definition = compile_one("import random\n" + machine(body))
    # What is left of the start, s = "a", goes in the return, whatever the
    # first n was.
    assert list(definition["States"]) == ["return"]
    assert asl.run(definition, {}) == [0, "a"]


@pytest.mark.parametrize(
    "first",
    # A random value, which the Choice would evaluate twice, in its test and
    # in the Assign of its Default, which reads it after, and the State of
    # the context, which names the state it is read in.
    ["random.random()", 'context["State"]["Name"]'],
)
def test_a_start_value_the_next_state_would_read_otherwise_keeps_its_pass(first):
    body = f'n = {first}\nif n == "x":\n    return 1\nreturn [n]'
    source = "import random\nfrom sfnx import context\n" + machine(body)
    definition = compile_one(source)
    assert definition["States"][definition["StartAt"]]["Type"] == "Pass"


@pytest.mark.parametrize(
    "value, output",
    [
        ("[1, 2]", [[1, 2]]),
        ('{"a": 1}', [{"a": 1}]),
        ('[y, "a"]', [[2, "a"]]),
    ],
)
def test_a_list_or_dict_of_values_that_are_never_undefined_goes_in_the_return(
    value, output
):
    definition = compile_one(machine(f"y = 2\nv = {value}\nreturn [v]"))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == output


@pytest.mark.parametrize(
    "value",
    ['[1, input["x"]]', '{"a": input["x"]}', '{"a": input["x"] - 1}'],
)
def test_a_list_or_dict_that_may_be_undefined_or_fail_keeps_its_pass(value):
    definition = compile_one(machine(f"v = {value}\nreturn [v]"))
    assert [s["Type"] for s in definition["States"].values()] == ["Pass", "Succeed"]


def test_a_swap_reads_the_pending_values_in_the_same_state():
    definition = compile_one(machine("a = 3\nb = 2\na, b = b, a\nreturn [a, b]"))
    assert list(definition["States"]) == ["return"]
    assert definition["States"]["return"]["Output"] == [2, 3]


def test_a_swap_keeps_its_state_where_a_pending_value_cannot_be_shared():
    """b reads a, which read as its expression would give another value, so
    the swap does not share the first Pass, and the return right after it
    reads the values the first Pass assigns, each in the other's place:
    the random value once, so the Output evaluates it in the Pass's place."""
    body = "a = random.random()\nb = 2\na, b = b, a\nreturn [a, b]"
    definition = compile_one("import random\n" + machine(body))
    assert definition["States"] == {
        "return": {"Type": "Succeed", "Output": [2, "{% $random() %}"]}
    }


@pytest.mark.parametrize(
    "body",
    [
        # The swap takes the first a whole into b, where it is still
        # evaluated, and fails on a missing key there as Python does.
        'a = input["x"]\nb = 2\na, b = b, a\nreturn [a, b]',
        # So does an assignment of a copy before the name is assigned again.
        'a = input["x"]\nb = a\na = 2\nreturn [a, b]',
    ],
)
def test_a_first_value_another_name_takes_whole_shares_the_state(body):
    source = machine(body)
    definition = compile_one(source)
    assert [s["Type"] for s in definition["States"].values()] == ["Pass", "Succeed"]
    assert definition["States"]["a"]["Assign"] == {
        "a": 2,
        "b": "{% $states.context.Execution.Input.x %}",
    }
    assert asl.run(definition, {"x": 5}) == python(source, {"x": 5}) == [2, 5]
    with pytest.raises(asl.Failure):
        asl.run(definition, {})


@pytest.mark.parametrize(
    "value, output",
    [
        ("0 + 1", 1),
        ("2 * 3", 6),
        ("5 - 7", -2),
        ("-x", -3),
        ('"a" + "b"', "ab"),
        ("x + 1", 4),
        # Python's quotient rounds down, and its remainder takes the sign of
        # the divisor.
        ("7 // 2", 3),
        ("-7 // 2", -4),
        ("-7 % 2", 1),
        ("7 % -2", -1),
        ("x // 1 + 0", 3),
        ("0 < 0", False),
        ("x >= 3", True),
        ("1 <= 2", True),
        ("x == 3", True),
        ("x != 3", False),
        ('"a" == "a"', True),
        ('"a" != "a"', False),
        ("not True", False),
        ("not (x > 3)", True),
    ],
)
def test_an_operation_on_values_written_in_the_source_is_its_value(value, output):
    definition = compile_one(machine(f"x = 3\nv = {value}\nreturn [v]"))
    assert definition["States"] == {"return": {"Type": "Succeed", "Output": [output]}}


@pytest.mark.parametrize(
    "value",
    [
        # A double holds neither exactly, so JSONata's value may differ.
        "2 ** 53 + 1",
        "9007199254740993 - 1",
        "9007199254740993 // 1",
        "1.5 + 1",
        "7.5 // 2",
        "9007199254740993 > 0",
        "4503599627370496 * 4",
        # A quotient may not be whole, and a number that is not is left for
        # JSONata to write.
        "7 / 2",
        # JSONata orders strings by UTF-16 units, and the deployment writes
        # another text in the place of a placeholder.
        '"a" < "b"',
        '"${Name}" == "a"',
        # The input is known only when it runs.
        'input["x"] + 1',
    ],
)
def test_an_operation_on_other_values_stays_an_expression(value):
    output = compile_one(machine(f"return [{value}]"))["States"]["return"]["Output"]
    assert isinstance(output[0], str) and output[0].startswith("{%")


def test_joining_text_cannot_fail_so_it_goes_in_the_return():
    body = 's = input.get("s", "a")\nt = s + "x"\nreturn [t]'
    assert list(compile_one(machine(body))["States"]) == ["return"]


@pytest.mark.parametrize(
    "body, types",
    [
        # c reads a, which read as its expression would give another value;
        # the return reads it twice, so a keeps its Pass.
        ("a = random.random()\nc, d = a, 1\nreturn [c, d, a]", ["Pass", "Succeed"]),
        # The text of jsonata() reads x by its name, which no expression
        # replaces, and spells it in a string, which stays as it is.
        (
            "x = 1\nc, d = jsonata(\"'$x' & $string($x)\"), 2\nreturn [c, d]",
            ["Pass", "Pass", "Succeed"],
        ),
    ],
)
def test_an_unpacking_that_cannot_read_a_pending_value_takes_a_state(body, types):
    header = "import random\nfrom sfnx import jsonata, state_machine\n\n\n"
    source = (
        header + "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")
    )
    definition = compile_one(source)
    assert [s["Type"] for s in definition["States"].values()] == types


@pytest.mark.parametrize(
    "value, output",
    [("len([1, 2, 3])", 3), ("len([[1], [2]])", 2), ("len([])", 0)],
)
def test_the_length_of_a_list_written_in_the_source_is_its_value(value, output):
    definition = compile_one(machine(f"return [{value}]"))
    assert definition["States"]["return"]["Output"] == [output]


@pytest.mark.parametrize(
    "value, output",
    [("len('')", 0), ('len("abc")', 3), ('len("日本")', 2), ('len("😀")', 1)],
)
def test_the_length_of_a_string_written_in_the_source_is_its_value(value, output):
    """Counted in code points, as Python counts them."""
    definition = compile_one(machine(f"return [{value}]"))
    assert definition["States"]["return"]["Output"] == [output]


def test_the_length_of_a_placeholder_stays_an_expression():
    """The deployment writes another text in its place."""
    output = compile_one(machine('return [len("${Bucket}")]'))["States"]
    assert output["return"]["Output"][0] == "{% $length('${Bucket}') %}"


@pytest.mark.parametrize(
    "value, output",
    [
        ("[1] + [2]", [1, 2]),
        ("[] + []", []),
        ('l + [[3], {"k": "é"}]', [1, 2, [3], {"k": "é"}]),
        ("len([1, 2] + [1, 2])", 4),
    ],
)
def test_lists_written_in_the_source_joined_are_the_list(value, output):
    definition = compile_one(machine(f"l = [1, 2]\nreturn [{value}]"))
    assert definition["States"]["return"]["Output"] == [output]


def test_a_list_holding_an_expression_joined_stays_an_expression():
    output = compile_one(machine('return [[1] + [input["x"]]]'))["States"]
    assert output["return"]["Output"][0].startswith("{% $append(")


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        ('n = input["n"]\nn = n\nreturn n', {"n": 4}, 4),
        ('n: int = input["n"]\nn: int = n\nreturn n', {"n": 4}, 4),
        # The loop's variable, assigned in the body, is a variable already.
        ("t = 0\nfor x in [1, 2]:\n    x = x\n    t = t + x\nreturn t", {}, 3),
    ],
)
def test_a_variable_assigned_its_own_value_makes_no_state(
    body, execution_input, expected
):
    source = machine(body)
    compiled = compile_one(source)
    assert not {"n", "x"} & set(compiled["States"])
    assert asl.run(compiled, execution_input) == expected
    assert python(source, execution_input) == expected


def test_a_name_bound_to_an_expression_assigned_itself_becomes_a_variable():
    """The parameter of a function called directly is bound to the argument's
    expression, so assigning it makes the variable the return reads."""
    source = (
        HEADER + "def helper(v):\n    v = v\n    return {'v': v}\n\n\n"
        '@state_machine\ndef pay(input):\n    return helper(input["n"])\n'
    )
    compiled = compile_one(source)
    assert compiled["States"]["v"]["Assign"] == {
        "v": "{% $states.context.Execution.Input.n %}"
    }
    assert asl.run(compiled, {"n": 4}) == {"v": 4}


# Values that neither fail nor are undefined, whatever the input holds.
CERTAIN = (
    'xs: list[int] = input.get("xs", [])\nd: dict[str, int] = input.get("d", {})\n'
)


@pytest.mark.parametrize(
    "value",
    [
        "len([0 for x in [1, 2]])",
        "len(xs)",
        '[{"id": x} for x in xs]',
        "[x for x in xs if x != 0]",
        "[[x] for x in xs]",
        # $reduce and $append fail for no value (measured).
        "len([0 for x in [1, 2] for y in [1, 2]])",
        "[[x, y] for x in xs if x != 0 for y in xs]",
        # $append fails for no value, and gives the other of two where one
        # is nothing (measured).
        '["x"] + ["" for w in ["x"]]',
        "xs + [1]",
        "[1] + xs",
        # $keys fails for no value (measured).
        'len({"a": 1, "b": 2})',
        "len(d)",
        "[k for k in d]",
        "list(d.keys())",
        "d.keys()",
        # $string fails for no value of any JSON type (measured).
        "str(xs)",
        "str(d)",
        'f"-{len(xs)}"',
    ],
)
def test_a_value_that_cannot_fail_goes_in_the_return(value):
    """$count gives 0 for nothing and a comprehension is a list even of
    nothing, so where the list, the conditions and the element cannot fail,
    the return, which does not read it, ends without its Pass."""
    body = f'{CERTAIN}v = {value}\ns = "a"\nreturn s'
    assert list(compile_one(machine(body))["States"]) == ["return"]


@pytest.mark.parametrize(
    "value",
    [
        # * and > fail for a value that is not a number.
        "[x * 2 for x in xs]",
        "[x for x in xs if x > 0]",
        "[x * y for x in xs for y in xs]",
        "[x for x in xs if x > 0 for y in xs]",
        "[y for x in [v * 2 for v in xs] for y in xs]",
    ],
)
def test_a_comprehension_that_may_fail_keeps_its_pass(value):
    # A Parallel takes the start only where none of it can fail, as its
    # branches run before its Assign.
    body = f'{CERTAIN}v = {value}\ns = "a"\nr = parallel(one)\nreturn [s, v, r]'
    source = machine(body).replace(
        "import state_machine", "import parallel, state_machine"
    )
    definition = compile_one(source + "\n\n\ndef one():\n    return 1\n")
    assert [s["Type"] for s in definition["States"].values()] == ["Pass", "Parallel"]


def test_the_length_of_a_list_holding_an_expression_stays_an_expression():
    output = compile_one(machine('return [len([input["x"], 1])]'))["States"]
    assert output["return"]["Output"][0].startswith("{% $count(")


def test_serial_names_skip_names_in_use():
    definition = compile_one(
        machine(
            'x: float = input["w"]\nx = x + input["x"]\n'
            'x_2: float = input["y"]\nx_2 = x_2 + input["z"]\nreturn [x, x_2]'
        )
    )
    assert list(definition["States"]) == ["x", "x_2", "x_2_2", "return"]


def test_implicit_return_is_null():
    definition = compile_one(machine("x = 1"))
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": None}
    definition = compile_one(machine("return"))
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": None}


def test_machine_without_parameter():
    definition = compile_one(HEADER + "@state_machine\ndef hello():\n    return 'hi'")
    assert definition["States"]["return"] == {"Type": "Succeed", "Output": "hi"}


def test_timeout():
    source = HEADER + "@state_machine(timeout=300)\ndef pay(input):\n    return 1"
    assert compile_one(source)["TimeoutSeconds"] == 300
    source = HEADER + "@state_machine()\ndef pay(input):\n    return 1"
    assert "TimeoutSeconds" not in compile_one(source)


@pytest.mark.parametrize(
    "imports, decorator",
    [
        ("import sfnx", "sfnx.state_machine"),
        ("import sfnx as s", "s.state_machine"),
        ("from sfnx import state_machine as machine", "machine"),
        ("import sfnx.other", "sfnx.state_machine"),
    ],
)
def test_import_forms(imports, decorator):
    source = f"{imports}\n\n\n@{decorator}\ndef pay(input):\n    return 1"
    assert list(compile_source(source)) == ["pay"]


def test_every_machine_in_a_module():
    source = (
        HEADER
        + "class Other:\n    pass\n\n\n@registry[0]\ndef helper():\n    pass\n\n\n"
        + "@state_machine\ndef a(input):\n    return 1\n\n\n"
        + "@state_machine\ndef b(input):\n    return 2\n"
    )
    assert list(compile_source(source)) == ["a", "b"]


def test_docstring_and_pass_emit_nothing():
    definition = compile_one(machine('"""Pay."""\npass\nreturn 1'))
    assert list(definition["States"]) == ["return"]


@pytest.mark.parametrize(
    "body, output",
    [
        ('return input["a b"]', "{% $states.context.Execution.Input.`a b` %}"),
        (
            'return input["a`b"]',
            "{% $lookup($states.context.Execution.Input, 'a`b') %}",
        ),
        ("return input[0]", "{% $states.context.Execution.Input[0] %}"),
        ("return input[-1]", "{% $states.context.Execution.Input[-1] %}"),
        ('return input["a"][0]["b"]', "{% $states.context.Execution.Input.a[0].b %}"),
        (
            'return {"a": input["x"]}["a"]',
            "{% {'a': $states.context.Execution.Input.x}.a %}",
        ),
        # Of values written out, the value itself.
        ('return {"a": 1}["a"]', 1),
        ("return '{% x %}'", "{% '{% x %}' %}"),
        ("return '{% x'", "{% '{% x' %}"),
        ("return 'x %}'", "{% 'x %}' %}"),
        ("return ' {% x'", " {% x"),
        (
            'return {"a": [1, input], "b": None}',
            {"a": [1, "{% $states.context.Execution.Input %}"], "b": None},
        ),
    ],
)
def test_expressions(body, output):
    assert compile_one(machine(body))["States"]["return"]["Output"] == output


@pytest.mark.parametrize(
    "body, execution_input",
    [
        ('return input["amount"]', {"amount": 5}),
        ('amount = input["amount"]\nreturn amount', {"amount": 5}),
        ('a = input["a"]\nb = [a, input["b"]]\na = 3\nreturn [a, b]', {"a": 1, "b": 2}),
        ('return input["x y"]', {"x y": 1}),
        ('return input["a`b"]', {"a`b": 1}),
        ('return input["m"][1][0]', {"m": [[1, 2], [3, 4]]}),
        ("return input[-1]", [1, 2, 3]),
        (
            'return {"s": "it\'s", "t": "say \\"hi\\" \\\\ ok", "u": "{% x %}", "v": "{% x"}',
            None,
        ),
        ('order = input["order"]\nreturn {"id": order["id"]}', {"order": {"id": "o"}}),
        ('return {"a": True, "b": False, "c": None, "d": 1.5}', None),
    ],
)
def test_asl_matches_python(body, execution_input):
    source = machine(body)
    definition = compile_one(source)
    assert asl.run(definition, execution_input) == python(source, execution_input)


@pytest.mark.parametrize(
    "changed, execution_input",
    [
        ('d["k"] = 1', {"d": {"a": 0}}),
        ('d["a"]["b"] = input["n"]', {"d": {"a": {"c": 1}}, "n": 2}),
        ('d["a"] += 1', {"d": {"a": 1}}),
        ('d["a"]: int = 2', {"d": {"a": 1}}),
    ],
)
def test_setting_a_key_assigns_the_dict_again(changed, execution_input):
    """d["k"] = v of a variable of the function is d = {**d, "k": v}, through
    every key written as a string."""
    source = machine(f'd = input["d"]\n{changed}\nreturn d')
    # Python changes the input it is given in place.
    expected = python(source, copy.deepcopy(execution_input))
    assert asl.run(compile_one(source), execution_input) == expected


def test_an_append_named_with_a_state_comment_keeps_its_pass():
    body = 'xs: list = input["xs"]\nxs.append(1)  # state: AddOne\nreturn xs'
    definition = compile_one(machine(body))
    assert definition["States"]["AddOne"]["Type"] == "Pass"
    assert asl.run(definition, {"xs": [0]}) == [0, 1]


def test_another_name_for_a_dict_keeps_the_one_before_a_key_is_set():
    """Setting a key assigns the dict's name again, so another name for it
    keeps the dict from before."""
    changed = machine('d = input["d"]\nother = d\nd["k"] = 1\nreturn [d, other]')
    assert asl.run(compile_one(changed), {"d": {"a": 0}}) == [
        {"a": 0, "k": 1},
        {"a": 0},
    ]


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        ('xs: list = input["xs"]\nxs.append(3)\nreturn xs', {"xs": [1, 2]}, [1, 2, 3]),
        (
            (
                'xs: list[float] = input["xs"]\nout: list = []\nfor x in xs:\n'
                "    out.append(x * 2)\nreturn out"
            ),
            {"xs": [1, 2]},
            [2, 4],
        ),
        ("xs: list = []\nxs.append([1])\nreturn xs", {}, [[1]]),
        # A loop variable is the function's own.
        (
            (
                'nested: list[list] = input["n"]\nout: list = []\nfor xs in nested:\n'
                "    xs.append(1)\n    out.append(xs)\nreturn out"
            ),
            {"n": [[0], []]},
            [[0, 1], [1]],
        ),
        (
            (
                'ds: list[dict] = input["ds"]\nout: list = []\nfor d in ds:\n'
                '    d["k"] = 1\n    out.append(d)\nreturn out'
            ),
            {"ds": [{"a": 0}]},
            [{"a": 0, "k": 1}],
        ),
    ],
)
def test_append_assigns_the_list_again(body, execution_input, expected):
    """xs.append(x) of a variable of the function is xs = xs + [x]."""
    assert asl.run(compile_one(machine(body)), execution_input) == expected


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'def setb(e):\n    e["b"] = 2\n    return e\n\nreturn setb(input["d"])',
            "e is a parameter here, and its changed copy would not reach the caller",
        ),
        (
            "def add(ys: list):\n    ys.append(9)\n    return ys\n\nreturn add([1])",
            "ys is a parameter here",
        ),
        (
            (
                'def f(item):\n    item["k"] = 1\n    return item\n\n'
                'return inline_map(f, input["ds"])'
            ),
            "item is a parameter here",
        ),
        (
            'd = {"a": 1}\ndef setb():\n    d["b"] = 2\n\nsetb()\nreturn d',
            "d is a variable around this function, which the function cannot change",
        ),
        (
            (
                "out: list = []\ndef f(x):\n    out.append(x)\n    return x\n\n"
                'rs = inline_map(f, input["xs"])\nreturn out'
            ),
            "out is a variable around this function",
        ),
        (
            (
                "out: list = []\ndef f():\n    out.append(1)\n    return 1\n\n"
                "rs = parallel(f)\nreturn out"
            ),
            "out is a variable around this function",
        ),
        (
            "xs: list = []\nys = xs.append(1)\nreturn ys",
            "it gives no value; assign the new list to a name: xs = xs + [x]",
        ),
    ],
)
def test_a_change_that_would_not_reach_where_the_name_comes_from(body, message):
    source = machine(body).replace(
        "import state_machine", "import inline_map, parallel, state_machine"
    )
    with pytest.raises(CompileError, match=re.escape(message)):
        compile_one(source)


@pytest.mark.parametrize(
    "source, message, location",
    [
        (machine("return missing"), "missing is not assigned here", "6:12"),
        (machine("return 1\nx = 2"), "never reached", "7:5"),
        (machine("x[0] = 1"), "a list or dict is not changed in place", "6:5"),
        (
            machine('input["k"] = 1'),
            "write the changed copy to another name: data = {**input, 'k': 1}",
            "6:5",
        ),
        (machine('x = {}\nx[input["k"]] = 1'), "not changed in place", "7:5"),
        (machine("x = y = 1"), "one variable per statement", "6:9"),
        (machine("return b'x'"), "only JSON values", "6:12"),
        (machine("return 1e999"), "JSON numbers are finite", "6:12"),
        (machine("return {1: 2}"), "keys are strings", "6:13"),
        (machine("return {**1}"), "1 is a number; ** unpacks dicts", "6:15"),
        (machine("return input[True]"), "True is a boolean; keys are strings", "6:18"),
        (machine("with input:\n    pass"), "with is not supported", "6:5"),
        (machine("return input.x"), 'read a key with x["key"]', "6:12"),
        (machine("x" * 81 + " = 1"), "at most 80 characters", "6:5"),
        (
            machine("x" * 80 + ' = input["w"]\n' + "x" * 80 + ' = input["x"]'),
            "longer than 80",
            "7:5",
        ),
        # The spelling is checked too: a renamed name grows, as does one
        # numbered past a name the module uses.
        (
            machine("_" + "1" * 79 + " = 1"),
            "is written value_" + "1" * 79 + " in the definition",
            "6:5",
        ),
        (
            machine("x" * 79 + " = 1\n_" + "x" * 79 + " = 2"),
            "is written " + "x" * 79 + "_2 in the definition",
            "7:5",
        ),
        (
            machine("a: list = input\nfor " + "x" * 76 + " in a:\n    pass"),
            "needs a variable " + "x" * 76 + "_index here",
            "7:5",
        ),
        (HEADER + "def pay(input):\n    return 1", "no state machine here", "1:1"),
        (
            HEADER + "@state_machine\nasync def pay(input):\n    return 1",
            "async def",
            "5:1",
        ),
        (
            HEADER + "@state_machine\n@other\ndef pay(input):\n    return 1",
            "no other decorators",
            "6:1",
        ),
        (
            HEADER + "@state_machine(300)\ndef pay(input):\n    return 1",
            "by keyword",
            "4:2",
        ),
        (
            HEADER + "@state_machine(name='x')\ndef pay(input):\n    return 1",
            "takes only timeout",
            "4:16",
        ),
        (
            HEADER + "@state_machine(timeout=0)\ndef pay(input):\n    return 1",
            "positive number",
            "4:24",
        ),
        (
            HEADER + "@state_machine(timeout=1.5)\ndef pay(input):\n    return 1",
            "positive number",
            "4:24",
        ),
        (
            HEADER + "@state_machine\ndef pay(a, b):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            HEADER + "@state_machine\ndef pay(input=1):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            HEADER + "@state_machine\ndef pay(*input):\n    return 1",
            "one parameter",
            "5:1",
        ),
        (
            machine('return context["Execution"]["Id"]'),
            "context is not imported; write from sfnx import context",
            "6:12",
        ),
        (
            machine('task("arn:aws:states:::lambda:invoke", {"FunctionName": "f"})'),
            "task is not imported; write from sfnx import task",
            "6:5",
        ),
        (machine("x = wait(1)"), "wait is not imported", "6:9"),
        (
            HEADER
            + "def task():\n    x = 1\n    return x\n\n\n@state_machine\ndef pay(input):\n"
            + "    x = task() + 1",
            "task() runs its body here, as states",
            "11:9",
        ),
        ("from sfnx import *\n", "instead of *", "1:1"),
        ("def broken(:\n", "invalid syntax", "1:12"),
        # A column counts characters, not the bytes of the UTF-8 the parser
        # reads: the caret names frozenset on each of these lines.
        (
            machine('return {"日本語": frozenset(input)}'),
            "calling frozenset() is not supported",
            "6:20",
        ),
        (
            machine('return {"🙂": frozenset(input)}'),
            "calling frozenset() is not supported",
            "6:18",
        ),
        (
            HEADER
            + '@state_machine\ndef pay(input):\n\treturn {"日": frozenset(input)}',
            "calling frozenset() is not supported",
            "6:15",
        ),
        # Python counts the column of a syntax error in characters already.
        (HEADER + 'x = {"日本語": 1}\ny = 日本語語 ? 3\n', "invalid syntax", "5:10"),
    ],
)
def test_diagnostics(source, message, location):
    with pytest.raises(CompileError) as raised:
        compile_source(source, "app.py")
    assert message in raised.value.message
    assert str(raised.value).startswith(f"app.py:{location}: ")


def test_relative_imports_are_not_sfnx():
    source = (
        "from . import state_machine\n\n\n@state_machine\ndef pay(input):\n    return 1"
    )
    with pytest.raises(CompileError, match="no state machine here"):
        compile_source(source)


def test_names_do_not_depend_on_lines():
    source = machine('amount = input["amount"]\nreturn amount')
    assert compile_source(source) == compile_source("\n\n" + source)


@pytest.mark.parametrize(
    "body, states",
    [
        # The return reads the assignment, or leaves one that cannot fail.
        ('x = input["a"]\nreturn x', ["return"]),
        ('x = input.get("a")\nreturn [x]', ["return"]),
        ("x = 1", ["return"]),
        # Nothing reads x, so it goes; one that changes on evaluation is
        # evaluated once, where the Output would evaluate it twice.
        ('x = input["a"]\nreturn 1', ["return"]),
        ("x = random.random()\nreturn [x, x]", ["x", "return"]),
    ],
)
def test_a_return_after_assignments(body, states):
    definition = compile_one("import random\n" + machine(body))
    assert list(definition["States"]) == states


PASS = {"Type": "Pass", "Next": "r"}
SUCCEED = {"Type": "Succeed"}


@pytest.mark.parametrize(
    "before, after, lines",
    [
        ({"p": PASS, "r": SUCCEED}, {"p": PASS, "r": SUCCEED}, []),
        # A state that went, one that changed, and one that came.
        (
            {"p": PASS, "r": SUCCEED},
            {"r": {"Type": "Succeed", "Output": 1}, "q": PASS},
            [
                '- p: {"Type": "Pass", "Next": "r"}',
                '- r: {"Type": "Succeed"}',
                '+ r: {"Type": "Succeed", "Output": 1}',
                '+ q: {"Type": "Pass", "Next": "r"}',
            ],
        ),
    ],
)
def test_the_states_a_pass_changed(before, after, lines):
    assert changed_states(before, after) == lines


TESTED = 'x = input["x"]\nif x is None:\n    return 0\nreturn 1'


def test_each_pass_that_changes_the_definition_is_logged(caplog):
    """The start Pass goes in the Choice after it, as fold_start does, which
    the machine now starts at, and then x goes, as nothing reads it after
    the test that reads its expression; the other passes change nothing."""
    with caplog.at_level(logging.DEBUG, logger="sfnx.passes"):
        compile_one(machine(TESTED))
    folded, dropped = caplog.records
    assert folded.getMessage().splitlines()[:2] == ["fold_start", "StartAt: x -> if"]
    assert dropped.getMessage().splitlines()[0] == "drop_dead_assignments"


def test_nothing_is_logged_where_debug_is_off(caplog):
    with caplog.at_level(logging.INFO, logger="sfnx.passes"):
        compile_one(machine(TESTED))
    assert caplog.records == []


UNOPTIMIZED = """\
from sfnx import inline_map, state_machine


@state_machine
def main(input):
    def first(item: dict):
        x = item["x"]
        if x is None:
            return 0
        return 1

    x = input["x"]
    if x is None:
        return [0]
    return inline_map(first, input["items"])
"""


@pytest.mark.parametrize(
    "optimizing, machine_states, processor_states",
    [
        # The start Pass of the machine and of the Map processor stays.
        (
            False,
            ["x", "if", "return", "return_2"],
            ["first.x_2", "first.if", "first.return", "first.return_2"],
        ),
        # fold_start takes each in the Choice after it.
        (
            True,
            ["if", "return", "return_2"],
            ["first.if", "first.return", "first.return_2"],
        ),
    ],
)
def test_the_passes_run_unless_told_not_to(
    optimizing, machine_states, processor_states
):
    (definition,) = definitions(UNOPTIMIZED, "<string>", False, optimizing).values()
    states = definition["States"]
    assert list(states) == machine_states
    assert list(states["return_2"]["ItemProcessor"]["States"]) == processor_states


RENAMED = (
    "from sfnx import context, jsonata, parallel, state_machine, task\n\n"
    'L = "arn:aws:states:::lambda:invoke"\n\n\n@state_machine\ndef main(input):\n'
)


def names_of(scope: dict) -> list:
    """The state names of a machine, with each branch's after its state."""
    found: list = []
    for name, state in scope["States"].items():
        found.append(name)
        found += [names_of(branch) for branch in state.get("Branches", [])]
    return found


@pytest.mark.parametrize(
    "body, start, names",
    [
        # The Pass of r = None goes, as nothing reads it, and the Task takes
        # the name it would have had first.
        ('r = None\nr = task(L, {"FunctionName": "f"})\nreturn r', "r", ["r"]),
        # In a branch too, whose r is spelled r_2 apart from the machine's.
        (
            (
                'def f():\n    r = None\n    r = task(L, {"FunctionName": "f"})\n'
                "    return r\n\nr = None\nr = parallel(f)\nreturn r"
            ),
            "r",
            ["r", ["f.r_2"]],
        ),
        # When the state was entered does not name it.
        (
            (
                'r = None\nr = task(L, {"FunctionName": "f"})\n'
                'return [r, context["State"]["EnteredTime"]]'
            ),
            "r",
            ["r", "return"],
        ),
        # A definition that reads the name of a state keeps every name.
        (
            (
                'r = None\nr = task(L, {"FunctionName": "f"})\n'
                'return [r, context["State"]["Name"]]'
            ),
            "r_2",
            ["r_2", "return"],
        ),
        # And so does one that calls $eval, which may build a name.
        (
            (
                'r = None\nr = task(L, {"FunctionName": "f"})\n'
                "return [r, jsonata(\"$eval('1')\")]"
            ),
            "r_2",
            ["r_2", "return"],
        ),
    ],
)
def test_the_states_that_remain_take_their_names_again(body, start, names):
    (definition,) = compile_source(RENAMED + textwrap.indent(body, "    ")).values()
    assert definition["StartAt"] == start
    assert names_of(definition) == names


def assigning(merged: bool) -> dict:
    """a = 1, then b = a, then return b, as the statements build them: a Pass
    each. merged puts the second Pass's Assign in the first's without reading
    a as the value it is assigned there, as a pass that got it wrong would."""
    written = {
        "StartAt": "a",
        "States": {
            "a": {"Type": "Pass", "Assign": {"a": 1}, "Next": "b"},
            "b": {"Type": "Pass", "Assign": {"b": "{% $a %}"}, "Next": "r"},
            "r": {"Type": "Succeed", "Output": "{% $b %}"},
        },
    }
    definition = from_asl(written)
    assert isinstance(definition, dict)
    note_reads(definition)
    states = definition["States"]
    if merged:
        states["a"]["Assign"] = {**states["a"]["Assign"], **states["b"]["Assign"]}
        states["a"]["Next"] = "r"
        del states["b"]
    return definition


def test_a_read_the_passes_leave_reading_what_it_read_is_not_reported():
    assert misread(assigning(False)) == []


def test_a_read_that_sees_the_value_from_before_the_state_is_reported():
    """In one Assign, b reads a as it was before the state, not the 1 the
    Assign gives it."""
    [found] = misread(assigning(True))
    assert found.startswith("a: $a reads a of [0]")


def excepting(merged: bool) -> dict:
    """a = 1, then b = the result of a call, which may fail, in a try whose
    except clause returns a, as the statements build them: a Pass, then a
    Task. merged puts the Pass's Assign in the Task's, where a failure of
    b's value leaves a unassigned, as a pass that got it wrong would."""
    task = {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {"FunctionName": "f"},
        "Assign": {"b": "{% $number($states.result.Payload) %}"},
        "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "caught"}],
        "Next": "r",
    }
    written = {
        "StartAt": "a",
        "States": {
            "a": {"Type": "Pass", "Assign": {"a": 1}, "Next": "b"},
            "b": task,
            "caught": {"Type": "Succeed", "Output": "{% $a %}"},
            "r": {"Type": "Succeed", "Output": "{% $b %}"},
        },
    }
    definition = from_asl(written)
    assert isinstance(definition, dict)
    note_reads(definition)
    states = definition["States"]
    if merged:
        states["b"]["Assign"] = {**states["a"]["Assign"], **states["b"]["Assign"]}
        definition["StartAt"] = "b"
        del states["a"]
    return definition


def test_an_except_clause_that_sees_what_python_sees_is_not_reported():
    assert misread(excepting(False)) == []


def test_an_except_clause_that_misses_an_assignment_before_the_failure_is_reported():
    """Python assigns a before the call fails; the Task's Assign assigns
    nothing when b's value fails, so the catcher's way reads a unassigned."""
    found = "b: the except clause reads a of [0] where $number($states.result.Payload) fails"
    assert found in misread(excepting(True))


def except_body(spread: bool) -> dict:
    """a = 1, then b = the result of a call, which may fail, in a try whose
    except clause assigns a = 2 and returns a. spread puts the except
    clause's Pass in the catcher, as spread_passes may."""
    written = {
        "StartAt": "a",
        "States": {
            "a": {"Type": "Pass", "Assign": {"a": 1}, "Next": "b"},
            "b": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Arguments": {"FunctionName": "f"},
                "Assign": {"b": "{% $number($states.result.Payload) %}"},
                "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "again"}],
                "Next": "r",
            },
            "again": {"Type": "Pass", "Assign": {"a": 2}, "Next": "caught"},
            "caught": {"Type": "Succeed", "Output": "{% $a %}"},
            "r": {"Type": "Succeed", "Output": "{% $b %}"},
        },
    }
    definition = from_asl(written)
    assert isinstance(definition, dict)
    note_reads(definition)
    states = definition["States"]
    if spread:
        [catcher] = states["b"]["Catch"]
        catcher["Assign"] = states["again"]["Assign"]
        catcher["Next"] = "caught"
        del states["again"]
    return definition


def test_what_the_except_clause_assigns_after_the_failure_is_not_compared():
    """a = 2 is on the catcher's way, so Python assigns it after the failure
    too, wherever the passes put it."""
    assert misread(except_body(False)) == []
    assert misread(except_body(True)) == []


def test_a_value_is_held_by_another_expression_as_its_syntax_tree():
    """d's expression is not in ds's, however its text is, so d, which
    nothing reads, goes; ds, read in the Output, stays."""
    body = (
        'd: dict = input["d"]\nds: list[dict] = input["ds"]\n'
        'return min(ds, key=lambda item: item["p"])'
    )
    definition = compile_one(machine(body))
    first = definition["States"][definition["StartAt"]]
    assert first["Assign"] == {"ds": "{% $states.context.Execution.Input.ds %}"}


def test_a_return_that_reads_a_value_in_text_every_time_takes_its_place():
    """An f-string evaluates each value it writes every time, so the return
    fails where the Pass would, and the Pass goes."""
    body = 'c: int = input.get("c", 0)\nc += 1\nreturn f"{c} checkpoints"'
    c = "$exists($states.context.Execution.Input.c)"
    value = f"({c} ? $states.context.Execution.Input.c : 0) + 1"
    assert compile_one(machine(body))["States"] == {
        "return": {
            "Type": "Succeed",
            "Output": f"{{% $string({value}) & ' checkpoints' %}}",
        }
    }


READ_FURTHER = (
    'd: dict[str, list[int]] = input["d"]\nys = [x * 2 for x in d["items"]]\n'
)


@pytest.mark.parametrize(
    "body",
    [
        READ_FURTHER + "return ys",
        (
            READ_FURTHER
            + 'y = task("arn:aws:states:::lambda:invoke", {"FunctionName": "f", '
            + '"Payload": ys})\nreturn y'
        ),
    ],
)
def test_a_value_a_comprehension_reads_further_in_its_place_is_not_assigned(body):
    """d is read in place as the start of a longer path, and nothing reads
    the variable, so it is not assigned: a missing d gives the empty list
    the comprehension gives for it, and the call runs once either way."""
    source = (
        "from sfnx import state_machine, task\n\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )
    definition = compile_one(source)
    assert all("d" not in s.get("Assign", {}) for s in definition["States"].values())
    calls = []
    tasks = {"y": lambda arguments: calls.append(arguments) or arguments["Payload"]}
    assert asl.run(definition, {}, tasks) == []
    assert asl.run(definition, {"d": {"items": [1, 2]}}, tasks) == [2, 4]
    assert len(calls) == (2 if "task(" in body else 0)


def test_the_examples_of_which_states_a_function_makes():
    """The examples docs/language.md gives of merged states make the states
    it says they make."""
    guide = (Path(__file__).parent.parent / "docs" / "language.md").read_text()
    section = guide.split("## Which states a function makes")[1].split("\n## ")[0]
    blocks = re.findall(r"```python\n(.*?)```", section, re.DOTALL)
    header = "from sfnx import aws, state_machine\n\n\nclass Declined(Exception):\n    pass\n\n\n"
    types = {}
    for name, definition in compile_source(header + "\n\n".join(blocks)).items():
        types[name] = [state["Type"] for state in definition["States"].values()]
    assert types == {
        "quote": ["Pass", "Succeed"],
        "charge": ["Task"],
        "book": ["Task", "Task", "Task"],
    }


def test_the_example_of_a_function_called_in_several_places():
    """docs/language.md's two ways to release held items: called per except
    clause, the release loop is written twice; called once around the steps,
    it is written once, and also releases when the receipt call fails."""
    guide = (Path(__file__).parent.parent / "docs" / "language.md").read_text()
    section = guide.split("## Functions called directly")[1].split("\n## ")[0]
    block = re.findall(r"```python\n(.*?)```", section, re.DOTALL)[-1]
    definitions = compile_source(block)
    releases = {
        name: sum(
            ":deleteItem" in str(state.get("Resource"))
            for state in d["States"].values()
        )
        for name, d in definitions.items()
    }
    assert releases == {"per_clause": 2, "once": 1}

    def tasks(call: testing.Call) -> object:
        if call.resource.endswith(":deleteItem"):
            return {}
        if call.arguments["FunctionName"] == "receipt":
            raise testing.Failure("Lambda.ServiceException", "down")
        return {"Payload": {"status": "OK"}}

    execution_input = {"skus": ["a", "b"]}
    for name, released in [("per_clause", []), ("once", ["a", "b"])]:
        execution = testing.run(definitions[name], execution_input, tasks)
        assert execution.error == "Lambda.ServiceException"
        assert [
            call.arguments["Key"]["sku"]["S"]
            for call in execution.calls
            if call.resource.endswith(":deleteItem")
        ] == released
