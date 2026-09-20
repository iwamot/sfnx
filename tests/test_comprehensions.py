import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"


def source(body: str) -> str:
    return (
        "from sfnx import state_machine, task\n\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def output(body: str) -> object:
    (compiled,) = compile_source(source(body)).values()
    return compiled["States"]["return"]["Output"]


def run(body: str, execution_input: object) -> object:
    (compiled,) = compile_source(source(body)).values()
    return asl.run(compiled, execution_input)


@pytest.mark.parametrize(
    "body, code",
    [
        (
            'xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]',
            "[$map($xs, function($x) { $x * 2 })]",
        ),
        (
            'xs: list[float] = input["xs"]\nreturn [x for x in xs if x > 0]',
            "[$filter($xs, function($x) { $x > 0 })]",
        ),
        (
            'xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs if x > 0 if x < 9]',
            "[$map($filter($xs, function($x) { $x > 0 and $x < 9 }), function($x) { $x * 2 })]",
        ),
        (
            'prices: dict = input["prices"]\nreturn [k for k in prices if prices[k] > 1]',
            "[$filter($keys($prices), function($k) { $lookup($prices, $k) > 1 })]",
        ),
        (
            'xs: list[str | None] = input["xs"]\nreturn [x + "!" for x in xs if x is not None]',
            "[$map($filter($xs, function($x) { $exists($x) and $x != null }), function($x) { $x & '!' })]",
        ),
        (
            'x = 5\nxs: list = input["xs"]\nreturn [[x, y] for y in xs]',
            "$append([], $map($xs, function($y) { [$x, $type($y) = 'array' ? [[$y]] : $y] })[])",
        ),
        (
            'order_id = input["id"]\nreturn f"order {order_id}"',
            "'order ' & $string($order_id)",
        ),
        ('name: str = input["name"]\nreturn f"{name}!"', "$name & '!'"),
        ('name: str = input["name"]\nreturn f"{name}"', "$name"),
        ("return f\"{input['n'] + 1} items\"", f"$string({INPUT}.n + 1) & ' items'"),
    ],
)
def test_spelling(body, code):
    assert output(body) == "{% " + code + " %}"


def test_constant_f_strings_are_json():
    assert output('return f"plain"') == "plain"
    assert output('return f""') == ""


# The functions that read every item take a generator expression as the list
# comprehension it would be, and compile it to the same definition.
@pytest.mark.parametrize(
    "generator, comprehension",
    [
        ("sum(x * 2 for x in xs)", "sum([x * 2 for x in xs])"),
        ("max(x for x in xs if x > 0)", "max([x for x in xs if x > 0])"),
        ("min(x for x in xs)", "min([x for x in xs])"),
        ("sorted(x for x in xs)", "sorted([x for x in xs])"),
        ("list(x for x in xs if x)", "list([x for x in xs if x])"),
        (
            "max((x for x in xs), key=lambda x: -x)",
            "max([x for x in xs], key=lambda x: -x)",
        ),
        (
            "min((x for x in xs), key=lambda x: -x)",
            "min([x for x in xs], key=lambda x: -x)",
        ),
        (
            "sorted((x for x in xs), key=lambda x: -x, reverse=True)",
            "sorted([x for x in xs], key=lambda x: -x, reverse=True)",
        ),
    ],
)
def test_a_generator_is_the_list_comprehension_it_would_be(generator, comprehension):
    declared = 'xs: list[float] = input["xs"]\n'
    assert output(declared + "return " + generator) == output(
        declared + "return " + comprehension
    )


def test_the_comprehension_variable_does_not_leak():
    body = 'x = "outer"\nxs: list = input["xs"]\ny = [x for x in xs]\nreturn [x, y]'
    assert output(body) == ["{% $x %}", "{% $y %}"]


def test_the_comprehension_variable_cannot_hide_what_another_name_reads():
    # item is $items[$item_index], which a function parameter $items would hide.
    body = 'items: list = input["items"]\nzs: list = input["zs"]\nfor item in items:\n    ys = [item for items in zs]'
    with pytest.raises(
        CompileError, match="items is a variable that this comprehension reads"
    ):
        compile_source(source(body))
    body = 'items: list = input["items"]\nzs: list = input["zs"]\nfor item in items:\n    ys = [zs for items in zs if item]'
    with pytest.raises(
        CompileError, match="items is a variable that this comprehension reads"
    ):
        compile_source(source(body))


def test_a_slice_position_does_not_hide_the_comprehension_variable():
    # i is the function's parameter, not a variable, and the slice counts
    # positions under a name of its own.
    body = 'idx: list[float] = input["idx"]\nxs: list = input["xs"]\nreturn [xs[i:] for i in idx]'
    assert output(body) == (
        "{% $append([], $map($idx, function($i) { $append([], $filter($xs, "
        "function($v, $i_2) { $i_2 >= $i })[]) })[]) %}"
    )
    assert run(body, {"idx": [1, 2], "xs": [1, 2, 3]}) == [[2, 3], [3]]


def test_types():
    body = (
        'xs: list[float] = input["xs"]\nys = [str(x) for x in xs]\nreturn ys[0] + "!"'
    )
    assert output(body) == "{% $ys[0] & '!' %}"
    body = 'xs: list[str] = input["xs"]\nys = [x for x in xs if x != ""]\nreturn len(ys[0])'
    assert output(body) == "{% $length($ys[0]) %}"


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        (
            'xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]',
            {"xs": [1, 2]},
            [2, 4],
        ),
        ('xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]', {"xs": [1]}, [2]),
        ('xs: list[float] = input["xs"]\nreturn [x * 2 for x in xs]', {"xs": []}, []),
        (
            'xs: list[float] = input["xs"]\nreturn [x for x in xs if x > 5]',
            {"xs": [1, 2]},
            [],
        ),
        (
            'xs: list[float] = input["xs"]\nreturn [x for x in xs if x > 1]',
            {"xs": [1, 2]},
            [2],
        ),
        (
            'prices: dict = input["prices"]\nreturn [k for k in prices if prices[k] > 1]',
            {"prices": {"a": 1, "b": 2}},
            ["b"],
        ),
        (
            'xs: list = input["xs"]\nreturn [{"v": x} for x in xs]',
            {"xs": [1]},
            [{"v": 1}],
        ),
        (
            'n = input["n"]\nname: str = input["name"]\nreturn f"{name} has {n} items, {n > 1}"',
            {"n": 2, "name": "cart"},
            "cart has 2 items, true",
        ),
        # The comprehension variable hides the list only inside the function.
        ("xs = [1, 2]\nys = [xs * 2 for xs in xs]\nreturn ys", {}, [2, 4]),
        # A list known to be a list stays one element of the list around it.
        ("xs = [1, 2]\nreturn len([xs])", {}, 1),
        ("xs = [1, 2]\nreturn [xs][0]", {}, [1, 2]),
        ("xs = [1, 2]\nys = []\nreturn ys + [xs]", {}, [[1, 2]]),
        ("xs = [1, 2]\nreturn len([[xs], [1, 2]])", {}, 2),
        ("return len([x for x in [2, 3]])", {}, 2),
        # A dict literal is typed by its values.
        (
            'd: dict[str, int] = input["d"]\nd = {"a": 1, "b": d["a"]}\nreturn d["b"] + d["a"]',
            {"d": {"a": 2}},
            3,
        ),
        # An empty list joins without making the items unknown.
        ('ws = ["ab"]\nws = [] + ws\nreturn [len(w) for w in ws]', {}, [2]),
        (
            'ws = []\nif input["a"]:\n    ws = ["ab"]\nreturn [len(w) for w in ws]',
            {"a": True},
            [2],
        ),
        ("return [x for x in [[2]]]", {}, [[2]]),
        # A result that may hold lists keeps its shape for one item or none.
        *(
            (
                f'xs: list[list] = input["xs"]\nreturn {comprehension}',
                {"xs": xs},
                expected,
            )
            for comprehension, python in (
                ("[[x] for x in xs]", lambda xs: [[x] for x in xs]),
                (
                    "[x for x in xs if len(x) != 1]",
                    lambda xs: [x for x in xs if len(x) != 1],
                ),
                (
                    "[[x] for x in xs if len(x) != 1]",
                    lambda xs: [[x] for x in xs if len(x) != 1],
                ),
            )
            for xs in ([], [[1]], [[]], [[1, 2]], [[1], [2]], [[], [3, 4]])
            for expected in [python(xs)]
        ),
        *(
            (
                'xs: list = input["xs"]\nreturn [x["v"] for x in xs]',
                {"xs": xs},
                [x["v"] for x in xs],
            )
            for xs in ([], [{"v": [1]}], [{"v": []}], [{"v": 1}, {"v": [2]}])
        ),
        # An item of unknown type is tested when it is evaluated.
        ('x = input["x"]\nreturn len([x])', {"x": [1, 2]}, 1),
        ('x = input["x"]\nreturn [x, 3][0]', {"x": [1, 2]}, [1, 2]),
        ('x = input["x"]\nreturn [x, 3][0]', {"x": 5}, 5),
        ('x = input["x"]\nreturn [x, 3][0]', {"x": [7]}, [7]),
        ('x = input["x"]\nreturn [x, 3][0]', {"x": {"a": 1}}, {"a": 1}),
        ('x: list | None = input["x"]\nys = []\nreturn ys + [x]', {"x": [1]}, [[1]]),
        ('x: list | None = input["x"]\nys = []\nreturn ys + [x]', {"x": None}, [None]),
        ('xs: list = input["xs"]\nreturn len([[x] for x in xs])', {"xs": [1, 2]}, 2),
        ('return [input["true"], input["null"]]', {"true": 1, "null": 2}, [1, 2]),
        (
            'xs: list[float] = input["xs"]\nreturn sum(x for x in xs if x > 1)',
            {"xs": [1, 2, 3]},
            5,
        ),
        (
            'xs: list[float] = input["xs"]\nreturn sorted((x for x in xs), key=lambda x: -x)',
            {"xs": [1, 3, 2]},
            [3, 2, 1],
        ),
    ],
)
def test_evaluation(body, execution_input, expected):
    assert run(body, execution_input) == expected


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'xs: list = input["xs"]\nys: list = input["ys"]\nreturn [x for x in xs for y in ys]',
            "a comprehension takes one for",
        ),
        (
            'xs: list = input["xs"]\nreturn [a for a, b in xs]',
            "a comprehension iterates one variable",
        ),
        # enumerate() says what to count with before the message about
        # unpacking, as it does in a loop.
        (
            'xs: list = input["xs"]\nreturn [i for i, x in enumerate(xs)]',
            "enumerate() is not supported; count with range",
        ),
        (
            'return [x for x in input["xs"]]',
            "a comprehension depends on what it iterates",
        ),
        (
            's: str = input["s"]\nreturn [c for c in s]',
            "s is a string; a comprehension iterates lists and the keys of dicts",
        ),
        ('xs: list = input["xs"]\nreturn (x for x in xs)', "JSON has lists only"),
        # A generator is a value only where every item is read.
        ('xs: list = input["xs"]\nreturn len(x for x in xs)', "JSON has lists only"),
        (
            'xs: list = input["xs"]\nreturn reversed(x for x in xs)',
            "JSON has lists only",
        ),
        (
            'xs: list = input["xs"]\nys: list = input["ys"]\nreturn sum(x for x in xs for y in ys)',
            "a comprehension takes one for",
        ),
        (
            'xs: list = input["xs"]\nreturn sum(a for a, b in xs)',
            "a comprehension iterates one variable",
        ),
        (
            'xs: list[str] = input["xs"]\nreturn sum(x for x in xs)',
            "the items of [x for x in xs] are string; sum() takes numbers here",
        ),
        (
            'xs: list = input["xs"]\nreturn {x: 1 for x in xs}',
            'dict comprehensions are not supported; write the dict with its keys, such as {"id": x}, or build it in a Lambda task',
        ),
        (
            'xs: list = input["xs"]\nreturn [task("arn:aws:states:::aws-sdk:sns:publish", {"Message": x}) for x in xs]',
            "task() in a comprehension would need a state per item; use inline_map or a for loop",
        ),
        ('x = 1\nreturn f"{x!r}"', "conversions such as !r and = are not supported"),
        ('x = 1\nreturn f"{x=}"', "conversions such as !r and = are not supported"),
        ('x = 1.5\nreturn f"{x:.2f}"', "format specs are not supported"),
        ("return lambda: 1", "lambda is not supported; define the function with def"),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        output(body)
    assert message in raised.value.message
