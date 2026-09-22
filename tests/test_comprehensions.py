import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl, corpus

INPUT = "$states.context.Execution.Input"


def source(body: str) -> str:
    return (
        "from sfnx import state_machine, task\n\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def randomized(body: str) -> dict[str, object]:
    """A machine that calls random.random(), compiled with the module imported."""
    (compiled,) = compile_source("import random\n" + source(body)).values()
    return compiled


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
        # A format spec fills the text to its width, where $pad takes a
        # negative width to fill on the left.
        ('s: str = input["s"]\nreturn f"{s:>10}"', "$pad($s, -10)"),
        ('s: str = input["s"]\nreturn f"{s:<10}"', "$pad($s, 10)"),
        ('s: str = input["s"]\nreturn f"{s:10}"', "$pad($s, 10)"),
        ('s: str = input["s"]\nreturn f"{s:*>8}"', "$pad($s, -8, '*')"),
        # A 0 fills as any other character does where the alignment is written;
        # a width of its own (f"{s:08}") is number formatting and is rejected.
        ('s: str = input["s"]\nreturn f"{s:0<8}"', "$pad($s, 8, '0')"),
        ('s: str = input["s"]\nreturn f"[{s:->4}]"', "'[' & $pad($s, -4, '-') & ']'"),
        # An empty spec is what Python gives str() for, which is the value.
        ('s: str = input["s"]\nreturn f"{s:}"', "$s"),
        # The digits after the decimal point are the picture $formatNumber
        # takes, and a width pads what it writes.
        ('x: float = input["x"]\nreturn f"{x:.2f}"', "$formatNumber($x, '0.00')"),
        ('x: float = input["x"]\nreturn f"{x:.0f}"', "$formatNumber($x, '0')"),
        (
            'x: float = input["x"]\nreturn f"{x:,.2f}"',
            "$formatNumber($x, '#,##0.00')",
        ),
        (
            'x: float = input["x"]\nreturn f"{x:10.2f}"',
            "$pad($formatNumber($x, '0.00'), -10)",
        ),
        (
            'x: float = input["x"]\nreturn f"{x:<10.2f}"',
            "$pad($formatNumber($x, '0.00'), 10)",
        ),
        (
            'x: float = input["x"]\nreturn f"{x:*>12,.2f}"',
            "$pad($formatNumber($x, '#,##0.00'), -12, '*')",
        ),
        # d writes a whole number, and the 0 before a width fills it with the
        # sign inside, which the picture says with a negative one of its own.
        ('x: float = input["x"]\nreturn f"{x:d}"', "$formatNumber($x, '0')"),
        (
            'x: float = input["x"]\nreturn f"{x:5d}"',
            "$pad($formatNumber($x, '0'), -5)",
        ),
        (
            'x: float = input["x"]\nreturn f"{x:05d}"',
            "$formatNumber($x, '00000;-0000')",
        ),
        # An alignment written out fills as any other, as it does in Python.
        (
            'x: float = input["x"]\nreturn f"{x:0>5d}"',
            "$pad($formatNumber($x, '0'), -5, '0')",
        ),
        (
            'x: float = input["x"]\nreturn f"{x:*>05d}"',
            "$pad($formatNumber($x, '0'), -5, '*')",
        ),
        ('name: str = input["name"]\nreturn f"{name}"', "$name"),
        ("return f\"{input['n'] + 1} items\"", f"$string({INPUT}.n + 1) & ' items'"),
        # A dict comprehension is one pass whose objects are merged.
        (
            'xs: list[str] = input["xs"]\nreturn {x: 1 for x in xs}',
            "$merge([$map($xs, function($x) { {$x: 1} })])",
        ),
        (
            'xs: list[float] = input["xs"]\nreturn {str(x): x * 2 for x in xs if x > 1}',
            "$merge([$map($xs, function($x) { $x > 1 ? {$string($x): $x * 2} })])",
        ),
        (
            'd: dict = input["d"]\nreturn {k: d[k] for k in d}',
            "$merge([$map($keys($d), function($k) { {$k: $lookup($d, $k)} })])",
        ),
        # $sift keeps the entries that pass through as they are.
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 1}',
            "$merge([$sift($d, function($v, $k) { $v > 1 })])",
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v + 1 for k, v in d.items() if v > 0}',
            "$merge([$each($d, function($v, $k) { $v > 0 ? {$k: $v + 1} })])",
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k.upper(): v for k, v in d.items()}',
            "$merge([$each($d, function($v, $k) { {$uppercase($k): $v} })])",
        ),
        # Every key of a JSON object is a string, so this is the dict itself.
        ('d: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items()}', "$d"),
    ],
)
def test_spelling(body, code):
    assert output(body) == "{% " + code + " %}"


SPECS = ["<6", ">6", "6", ".<6", ".>6", "*>3", "->2"]


@pytest.mark.parametrize("text", ["", "ab", "abcdefgh", "日本", "a😀b"])
def test_a_format_spec_fills_the_text_as_python_does(text):
    """$pad counts the width in code points, as Python counts characters
    (measured on Step Functions)."""
    body = (
        's: str = input["s"]\nreturn ['
        + ", ".join(f'f"{{s:{spec}}}"' for spec in SPECS)
        + "]"
    )
    assert run(body, {"s": text}) == [format(text, spec) for spec in SPECS]


NUMBER_SPECS = [".2f", ".0f", ",.2f", "10.2f", "<10.2f", "*>12,.2f"]


@pytest.mark.parametrize(
    "number",
    # Values the decimal and the double round the same way at these digits:
    # halves and quarters, which a double holds exactly, and values that are
    # nowhere near a halfway digit. The one that differs (2.675) is a case of
    # the corpus.
    [0, 5, -7.5, 0.125, 0.375, 2.5, 3.5, -0.125, 1234.5678, 1234567],
)
def test_a_number_spec_writes_what_cpython_writes(number):
    body = (
        'x: float = input["x"]\nreturn ['
        + ", ".join(f'f"{{x:{spec}}}"' for spec in NUMBER_SPECS)
        + "]"
    )
    assert run(body, {"x": number}) == [format(number, spec) for spec in NUMBER_SPECS]


WHOLE_SPECS = ["d", "5d", "05d", "010d", "<6d", "*>6d", "0>6d", "02d"]


@pytest.mark.parametrize("number", [0, 7, -12, -123456, 1234567])
def test_a_whole_number_spec_writes_what_cpython_writes(number):
    """The sign counts inside the zeros of a width, which the picture writes
    with a negative sub-picture one digit shorter."""
    body = (
        'x: float = input["x"]\nreturn ['
        + ", ".join(f'f"{{x:{spec}}}"' for spec in WHOLE_SPECS)
        + "]"
    )
    assert run(body, {"x": number}) == [format(number, spec) for spec in WHOLE_SPECS]


def test_a_format_spec_on_a_datetime_is_its_own():
    """Python gives the spec to the value, and a datetime reads it as a
    strftime format rather than a width."""
    imports = "import uuid\nfrom datetime import datetime\n"
    with pytest.raises(CompileError) as raised:
        compile_source(imports + source('return f"{datetime.now():>10}"'))
    assert raised.value.message == (
        "a format spec here is a width or the digits of a number, and Python "
        "gives this one to the value itself; write the datetime with strftime: "
        'datetime.now().strftime("%Y-%m-%d")'
    )
    with pytest.raises(CompileError) as raised:
        compile_source(imports + source('return f"{uuid.uuid4():>10}"'))
    assert raised.value.message == (
        "a format spec here is a width or the digits of a number, and Python "
        "gives this one to the value itself"
    )


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


def test_a_second_for_is_pointed_at_its_variable():
    body = 'xs: list = input["xs"]\nys: list = input["ys"]\nreturn [x for x in xs for y in ys]'
    with pytest.raises(CompileError) as raised:
        output(body)
    assert (raised.value.line, raised.value.column) == (8, 31)
    with pytest.raises(CompileError) as raised:
        output(
            body.replace(
                "[x for x in xs for y in ys]", "sum(x for x in xs for y in ys)"
            )
        )
    assert (raised.value.line, raised.value.column) == (8, 34)


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
    body = 'xs: list[str] = input["xs"]\nd = {x: 1 for x in xs}\nreturn d["a"] + 1'
    assert output(body) == "{% $d.a + 1 %}"
    body = 'e: dict[str, str] = input["e"]\nd = {k: v for k, v in e.items() if v != ""}\nreturn d["a"] + "!"'
    assert output(body) == "{% $d.a & '!' %}"


def test_a_dict_comprehension_evaluates_the_condition_the_key_and_the_value_once():
    # Python takes the condition, then the key, then the value, once per item.
    # A $filter before a $map would test every item before it read any value,
    # and give {"1": 0.8} after three calls.
    values = iter([0.9, 0.1, 0.8, 0.2])
    assert {str(x): next(values) for x in [1, 2] if next(values) > 0.5} == {
        "1": 0.1,
        "2": 0.2,
    }
    compiled = randomized(
        "return {str(x): random.random() for x in [1, 2] if random.random() > 0.5}"
    )
    sequence = corpus.Sequence([0.9, 0.1, 0.8, 0.2])
    with asl.replaced(random=sequence):
        assert asl.run(compiled, {}) == {"1": 0.1, "2": 0.2}
    assert sequence.calls == 4


def test_the_entries_of_a_dict_are_read_once_each():
    values = iter([0.9, 2.0, 0.1, 0.8, 4.0])
    entries = {"a": 1, "b": 2, "c": 3}
    assert {k: v * next(values) for k, v in entries.items() if next(values) > 0.5} == {
        "a": 2.0,
        "c": 12.0,
    }
    compiled = randomized(
        'd: dict[str, float] = input["d"]\n'
        "return {k: v * random.random() for k, v in d.items() if random.random() > 0.5}"
    )
    sequence = corpus.Sequence([0.9, 2.0, 0.1, 0.8, 4.0])
    with asl.replaced(random=sequence):
        assert asl.run(compiled, {"d": {"a": 1, "b": 2, "c": 3}}) == {
            "a": 2.0,
            "c": 12.0,
        }
    assert sequence.calls == 5


def test_a_dict_comprehension_variable_cannot_hide_what_another_name_reads():
    # item is $items[$item_index], which a function parameter $items would hide.
    body = 'items: list[str] = input["items"]\nzs: list = input["zs"]\nfor item in items:\n    ys = {item: 1 for items in zs}'
    with pytest.raises(
        CompileError, match="items is a variable that this comprehension reads"
    ):
        compile_source(source(body))
    body = 'items: list[str] = input["items"]\nd: dict = input["d"]\nfor item in items:\n    ys = {k: item for k, items in d.items()}'
    with pytest.raises(
        CompileError, match="items is a variable that this comprehension reads"
    ):
        compile_source(source(body))


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
        # A dict comprehension over a list: nothing to merge, several items, a
        # condition that drops every item, and a key written twice, where the
        # last value wins as it does in Python.
        ('xs: list[str] = input["xs"]\nreturn {x: 1 for x in xs}', {"xs": []}, {}),
        (
            'xs: list[str] = input["xs"]\nreturn {x: 1 for x in xs}',
            {"xs": ["a", "b"]},
            {"a": 1, "b": 1},
        ),
        (
            'xs: list[float] = input["xs"]\nreturn {str(x): x for x in xs if x > 5}',
            {"xs": [1, 2]},
            {},
        ),
        (
            'ps: list[dict[str, str]] = input["ps"]\nreturn {p["k"]: p["v"] for p in ps}',
            {"ps": [{"k": "a", "v": "1"}, {"k": "a", "v": "2"}]},
            {"a": "2"},
        ),
        # A value that is a list stays one value of the object.
        (
            'xs: list[str] = input["xs"]\nys: list = input["ys"]\nreturn {x: ys for x in xs}',
            {"xs": ["a"], "ys": [1, 2]},
            {"a": [1, 2]},
        ),
        (
            'xs: list[str] = input["xs"]\nys: list = input["ys"]\nreturn {x: [y for y in ys] for x in xs}',
            {"xs": ["a"], "ys": []},
            {"a": []},
        ),
        # The entries of a dict, kept as they are and rewritten.
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 1}',
            {"d": {"a": 1, "b": 2}},
            {"b": 2},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 5}',
            {"d": {"a": 1}},
            {},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items() if v > 5}',
            {"d": {}},
            {},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v + 1 for k, v in d.items() if v > 0}',
            {"d": {"a": 0, "b": 2}},
            {"b": 3},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v + 1 for k, v in d.items() if v > 0}',
            {"d": {}},
            {},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k + "!": v for k, v in d.items()}',
            {"d": {"a": 1}},
            {"a!": 1},
        ),
        (
            'd: dict[str, float] = input["d"]\nreturn {k: v for k, v in d.items()}',
            {"d": {"a": 1}},
            {"a": 1},
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
        # enumerate() says where it is taken before the message about
        # unpacking.
        (
            'xs: list = input["xs"]\nreturn [i for i, x in enumerate(xs)]',
            "enumerate() is only for a for loop: for i, item in enumerate(items)",
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
            "JSON object keys are strings, and the type of x is not known here; write str(x)",
        ),
        (
            'xs: list[float] = input["xs"]\nreturn {x: 1 for x in xs}',
            "JSON object keys are strings, and x is number; write str(x)",
        ),
        (
            'xs: list[str | None] = input["xs"]\nreturn {x: 1 for x in xs}',
            "JSON object keys are strings, and x is null | string; write str(x)",
        ),
        (
            'xs: list = input["xs"]\nys: list = input["ys"]\nreturn {str(x): 1 for x in xs for y in ys}',
            "a comprehension takes one for",
        ),
        (
            'xs: list = input["xs"]\nreturn {a: b for a, b in xs}',
            (
                "a dict comprehension iterates one variable, or the key and the "
                "value of d.items(): {k: v for k, v in d.items()}"
            ),
        ),
        (
            'xs: list = input["xs"]\nreturn {str(i): x for i, x in enumerate(xs)}',
            "enumerate() is only for a for loop: for i, item in enumerate(items)",
        ),
        (
            'd: dict = input["d"]\nreturn {k: v for k in d.items()}',
            "items() gives two variables: {k: v for k, v in d.items()}",
        ),
        (
            'd: dict = input["d"]\nreturn {k: k for k, k in d.items()}',
            "the two variables need different names: {k: v for k, v in d.items()}",
        ),
        (
            'd: dict = input["d"]\nreturn {k: v for k, v in d.items(1)}',
            "items() is written {k: v for k, v in d.items()}",
        ),
        (
            'xs: list = input["xs"]\nreturn {k: v for k, v in xs.items()}',
            "xs is an array; items() is a dict method: {k: v for k, v in d.items()}",
        ),
        (
            'xs: list[str] = input["xs"]\nreturn {x: task("arn:aws:states:::aws-sdk:sns:publish", {"Message": x}) for x in xs}',
            "task() in a comprehension would need a state per item",
        ),
        (
            'xs: list[str] = input["xs"]\nreturn {x for x in xs}',
            "JSON has lists only; write a list comprehension: [x for x in xs]",
        ),
        (
            'xs: list = input["xs"]\nreturn [task("arn:aws:states:::aws-sdk:sns:publish", {"Message": x}) for x in xs]',
            "task() in a comprehension would need a state per item; use inline_map or a for loop",
        ),
        ('x = 1\nreturn f"{x!r}"', "conversions such as !r and = are not supported"),
        ('x = 1\nreturn f"{x=}"', "conversions such as !r and = are not supported"),
        # The format specs outside a width and the digits of a number, and a
        # width the spec does not hold itself.
        ('s: str = input["s"]\nreturn f"{s:^10}"', "a format spec here is a width"),
        ('x: float = input["x"]\nreturn f"{x:,}"', "a format spec here is a width"),
        ('x: float = input["x"]\nreturn f"{x:.2%}"', "a format spec here is a width"),
        ('x: float = input["x"]\nreturn f"{x:.2e}"', "a format spec here is a width"),
        ('x: float = input["x"]\nreturn f"{x:05.2f}"', "a format spec here is a width"),
        ('x: float = input["x"]\nreturn f"{x:08,d}"', "a format spec here is a width"),
        ('s: str = input["s"]\nreturn f"{s:05}"', "a format spec here is a width"),
        (
            's: str = input["s"]\nreturn f"{s:5d}"',
            "s is a string, and d writes a whole number here",
        ),
        (
            's: str = input["s"]\nreturn f"{s:.2f}"',
            "s is a string, and the digits format a number here",
        ),
        ('s: str = input["s"]\nreturn f"{s:010}"', "a format spec here is a width"),
        ('s: str = input["s"]\nreturn f"{s:>}"', "a format spec here is a width"),
        ('s: str = input["s"]\nreturn f"{s:>0}"', "a format spec here is a width"),
        (
            's: str = input["s"]\nw = 4\nreturn f"{s:>{w}}"',
            "the width of a format spec is written in the source:",
        ),
        # A format spec pads a string, and the value must be known to be one.
        (
            'x: float = input["x"]\nreturn f"{x:>10}"',
            (
                "x is a number, and a width pads a string here; write a "
                "number with .2f or d, or build the text from it"
            ),
        ),
        (
            "return f\"{input['s']:>10}\"",
            "a width pads a string, so the type of input['s'] must be known",
        ),
        (
            "return f\"{input['x']:.2f}\"",
            "the digits format a number, so the type of input['x'] must be known",
        ),
        (
            'v: str | None = input["v"]\nreturn f"{v:>10}"',
            "v may be null | string; narrow it first",
        ),
        ("return lambda: 1", "lambda is not supported; define the function with def"),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        output(body)
    assert message in raised.value.message
