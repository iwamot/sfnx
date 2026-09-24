import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

LAMBDA = "arn:aws:states:::lambda:invoke"
HEADER = (
    "import uuid\n\n"
    "from sfnx import Timeout, inline_map, parallel, state_machine, task\n\n"
    f'LAMBDA = "{LAMBDA}"\n'
    'RETRY = [{"ErrorEquals": [Timeout], "MaxAttempts": 2}]\n\n\n'
    "class Declined(Exception):\n    pass\n\n\n"
    "def invoke(function, payload, retry=RETRY):\n"
    '    return task(LAMBDA, {"FunctionName": function, "Payload": payload}, '
    'retry=retry)["Payload"]\n\n\n'
    "def classify(n: float):\n"
    "    if n > 10:\n"
    '        return "big"\n'
    '    return "small"\n'
)


def source(body: str, module: str = "") -> str:
    return (
        HEADER
        + module
        + "\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def definition(body: str, module: str = "") -> dict:
    (compiled,) = compile_source(source(body, module)).values()
    return compiled


def states(body: str, module: str = "") -> dict:
    return definition(body, module)["States"]


def run(
    body: str, execution_input: object, tasks: dict | None = None, module: str = ""
) -> object:
    return asl.run(definition(body, module), execution_input, tasks)


def rejected(body: str, module: str = "") -> str:
    with pytest.raises(CompileError) as raised:
        definition(body, module)
    return raised.value.message


def test_a_call_is_its_body_written_in_place():
    """Each call writes the Task the function makes, with the arguments in
    place of the parameters, a Retry and an ARN passed as written included."""
    compiled = states(
        'a = invoke("${A}", {"x": input["x"]})\n'
        'invoke("${B}", a, retry=[{"ErrorEquals": [Declined], "MaxAttempts": 1}])\n'
        'return invoke("${C}", a["y"])'
    )
    assert list(compiled) == ["a", "invoke", "return"]
    retry = [{"ErrorEquals": ["States.Timeout"], "MaxAttempts": 2}]
    assert compiled["a"] == {
        "Type": "Task",
        "Resource": LAMBDA,
        "Arguments": {
            "FunctionName": "${A}",
            "Payload": {"x": "{% $states.context.Execution.Input.x %}"},
        },
        "Retry": retry,
        "Assign": {"a": "{% $states.result.Payload %}"},
        "Next": "invoke",
    }
    assert compiled["invoke"]["Arguments"] == {
        "FunctionName": "${B}",
        "Payload": "{% $a %}",
    }
    assert compiled["invoke"]["Retry"] == [
        {"ErrorEquals": ["Declined"], "MaxAttempts": 1}
    ]
    assert compiled["return"]["Arguments"]["Payload"] == "{% $a.y %}"
    assert compiled["return"]["Output"] == "{% $states.result.Payload %}"
    assert compiled["return"]["End"] is True


def test_each_return_gives_the_statement_its_value():
    body = 'size = classify(input["n"])\nreturn size'
    compiled = states(body)
    # The branch returns, so what follows the if goes in the Choice's own
    # Assign, which only the Default applies.
    assert compiled["if"]["Choices"][0]["Assign"] == {"size": "big"}
    assert compiled["if"]["Assign"] == {"size": "small"}
    assert list(compiled) == ["if", "return"]
    assert run(body, {"n": 11}) == "big"
    assert run(body, {"n": 1}) == "small"


def test_a_return_ends_the_function_inside_a_loop():
    module = (
        "\n\ndef first(xs: list):\n"
        "    for x in xs:\n"
        "        if x > 1:\n"
        "            return x\n"
        "    return None\n"
    )
    body = 'found = first(input["xs"])\nreturn [found, found]'
    assert run(body, {"xs": [1, 3, 5]}, module=module) == [3, 3]
    assert run(body, {"xs": [1]}, module=module) == [None, None]


def test_the_result_can_be_read_through_subscripts_and_unpacked():
    module = '\n\ndef pair():\n    return [{"k": 1}, 2]\n'
    assert run("a = pair()[0]['k']\nreturn a", {}, module=module) == 1
    assert run("a, b = pair()\nreturn [b, a]", {}, module=module) == [2, {"k": 1}]
    assert run("a: dict = pair()[0]\nreturn a", {}, module=module) == {"k": 1}


def test_a_function_that_ends_without_return_gives_none():
    module = '\n\ndef note(x):\n    task(LAMBDA, {"FunctionName": "f", "Payload": x})\n'
    body = "r = note(input)\nreturn [r]"
    tasks = {"invoke": lambda arguments: {}}
    assert run(body, {}, tasks, module=module) == [None]


def test_a_value_the_call_does_not_use_is_not_evaluated():
    """A call on its own line evaluates the Task a return makes, and not an
    expression it would drop."""
    module = '\n\ndef lookup(x):\n    return x["missing"]\n'
    compiled = states('lookup(input)\ninvoke("f", 1)\nreturn 1', module)
    assert list(compiled) == ["invoke"]
    assert (compiled["invoke"]["Output"], compiled["invoke"]["End"]) == (1, True)


def test_the_names_of_the_function_are_its_own():
    """A name the function assigns is renamed where the machine uses it too,
    so the machine's variable keeps its value, as Python keeps it."""
    module = "\n\ndef bump(x: float):\n    ids = x + 1\n    return ids\n"
    body = 'ids = input["n"]\nr = bump(ids)\nreturn [ids, r]'
    compiled = states(body, module)
    assert compiled["ids_2"]["Assign"]["ids_2"] == "{% $ids + 1 %}"
    assert run(body, {"n": 1}, module=module) == [1, 2]
    # A name the machine does not use stays as written.
    assert "ids" in states('r = bump(input["n"])\nreturn r', module)


def test_calls_inside_calls_keep_their_names_apart():
    module = (
        "\n\ndef inner(x: float):\n    y = x * 2\n    return y\n"
        "\n\ndef outer(x: float):\n    y = inner(x)\n    z = inner(y)\n    return [y, z]\n"
    )
    assert run('return outer(input["n"])', {"n": 3}, module=module) == [6, 12]


def test_a_lambda_and_an_except_clause_bind_the_function_s_names():
    module = (
        "\n\ndef best(xs: list):\n    return sorted(xs, key=lambda x: -x)[0]\n"
        "\n\ndef safe(name):\n"
        "    try:\n"
        "        r = invoke(name, 1)\n"
        "    except Declined as e:\n"
        "        return str(e)\n"
        "    except Timeout:\n"
        '        return "late"\n'
        "    return r\n"
    )
    body = 'e = input["e"]\nx = best(input["xs"])\ny = safe("f")\nreturn [x, y, e]'

    def declined(arguments: object) -> object:
        raise asl.Failure("Declined", "no")

    assert run(body, {"xs": [1, 3], "e": "mine"}, {"r": declined}, module=module) == [
        3,
        "no",
        "mine",
    ]


def test_a_value_that_changes_on_evaluation_is_passed_once():
    module = "\n\ndef twice(v):\n    return [v, v]\n"
    compiled = states("return twice(str(uuid.uuid4()))", module)
    assert compiled["v"]["Assign"] == {"v": "{% $uuid() %}"}
    first, second = run("return twice(str(uuid.uuid4()))", {}, module=module)
    assert first == second


def test_a_parameter_takes_its_annotation():
    module = "\n\ndef total(xs: list):\n    n = 0\n    for x in xs:\n        n = n + x\n    return n\n"
    assert run('return total(input["xs"])', {"xs": [1, 2]}, module=module) == 3


def test_a_function_of_the_machine_reads_its_variables():
    body = (
        'rate: float = input["rate"]\n\n'
        "def charge(amount: float):\n"
        "    return amount * rate\n\n"
        'return charge(input["amount"])'
    )
    assert run(body, {"rate": 2, "amount": 3}) == 6


def test_the_try_around_a_call_catches_what_its_body_raises():
    body = (
        "try:\n"
        '    r = invoke("f", 1)\n'
        "except Declined:\n"
        '    return "declined"\n'
        "return r"
    )
    compiled = states(body)
    assert compiled["r"]["Catch"][0]["ErrorEquals"] == ["Declined"]

    def declined(arguments: object) -> object:
        raise asl.Failure("Declined", "no")

    assert run(body, {}, {"r": declined}) == "declined"


def test_a_call_runs_in_a_branch_and_a_map():
    body = (
        'def one():\n    return invoke("a", 1)\n\n'
        'def each(x):\n    return invoke("b", x)\n\n'
        'a = parallel(one)\nb = inline_map(each, input["xs"])\nreturn [a, b]'
    )
    compiled = states(body)
    processor = compiled["b"]["ItemProcessor"]["States"]
    # The map's item is bound first, as the Task would replace $states.input.
    assert processor["each.x"]["Assign"] == {"x": "{% $states.input.x %}"}
    tasks = {
        "one.return": lambda arguments: {"Payload": arguments["Payload"]},
        "each.return": lambda arguments: {"Payload": arguments["Payload"] * 10},
    }
    assert run(body, {"xs": [1, 2]}, tasks) == [[1], [10, 20]]


@pytest.mark.parametrize(
    "body, module, message",
    [
        (
            "return again(1)",
            "\n\ndef again(n):\n    return again(n)\n",
            "again() calls itself, and its body would be written here without end",
        ),
        (
            "return ping(1)",
            "\n\ndef ping(n):\n    return pong(n)\n\n\ndef pong(n):\n    return ping(n)\n",
            "ping() calls itself",
        ),
        (
            "return outer()",
            "\n\ndef outer():\n    def inner():\n        return 1\n    return parallel(inner)\n",
            "inner is defined inside outer, which is called directly; define it outside outer",
        ),
        (
            "return pay(1)",
            "",
            "pay() has decorators, and a function called directly takes none",
        ),
        (
            "return rest(1)",
            "\n\ndef rest(*xs):\n    return xs\n",
            "a function called directly takes plain parameters, with or without defaults",
        ),
        ("return classify(1, 2)", "", "classify() takes 1 arguments"),
        ("return classify(m=1)", "", "classify() has no parameter m"),
        ("return classify(**input)", "", "classify() has no parameter to unpack into"),
        ("return classify(1, n=2)", "", "n is given twice to classify()"),
        ("return classify()", "", "classify() needs n"),
        (
            'return classify(task(LAMBDA, {"FunctionName": "f"}))',
            "",
            "classify() takes values; call task() on a line of its own first",
        ),
        (
            'return invoke("f", classify(1))',
            "",
            "invoke() takes values; call classify() on a line of its own first",
        ),
        (
            "RETRY = 1\nreturn invoke('f', 1)",
            "",
            "invoke() reads RETRY of the module, and RETRY is a variable here too",
        ),
        (
            "xs: list = input['xs']\nfor x in xs:\n    stop()",
            "\n\ndef stop():\n    break\n",
            "break is only for loops",
        ),
        (
            'if input["a"]:\n    def f():\n        return 1\nreturn f()',
            "",
            "f is not defined the same way on every path to here",
        ),
        # An argument is evaluated at the call, read or not.
        (
            "return classify_default()",
            "\n\ndef classify_default(n=missing):\n    return 1\n",
            "missing is not assigned here",
        ),
        # What is not a value where the call is written is not one inside.
        (
            "return twice(RETRY)",
            "\n\ndef twice(v):\n    return [v, v]\n",
            "Timeout is not assigned here",
        ),
    ],
)
def test_what_a_call_cannot_do(body, module, message):
    assert message in rejected(body, module)
