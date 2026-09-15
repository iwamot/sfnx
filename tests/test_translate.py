import re
import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl

INPUT = "$states.context.Execution.Input"


def source(body: str, parameter: str = "input") -> str:
    return (
        "from sfnx import state_machine\n\n\n"
        f"@state_machine\ndef pay({parameter}):\n" + textwrap.indent(body, "    ")
    )


def definition(body: str, parameter: str = "input") -> dict:
    (compiled,) = compile_source(source(body, parameter)).values()
    return compiled


def output(body: str, parameter: str = "input") -> object:
    return definition(body, parameter)["States"]["return"]["Output"]


@pytest.mark.parametrize(
    "body, code",
    [
        # + takes its spelling from whichever side has a known type.
        ('return input["a"] + 1', f"{INPUT}.a + 1"),
        ('return "x" + input["a"]', f"'x' & {INPUT}.a"),
        ('return input["a"] + [1]', f"$append({INPUT}.a, [1])"),
        ('a: str = input["a"]\nreturn a + input["b"]', f"$a & {INPUT}.b"),
        ('return input["a"] - input["b"] - 1', f"{INPUT}.a - {INPUT}.b - 1"),
        ('return input["a"] - (input["b"] - 1)', f"{INPUT}.a - ({INPUT}.b - 1)"),
        ('return (input["a"] - 1) * 2', f"({INPUT}.a - 1) * 2"),
        ('return input["a"] / 2 * 3', f"{INPUT}.a / 2 * 3"),
        ('return input["a"] % 3', f"{INPUT}.a - 3 * $floor({INPUT}.a / 3)"),
        ('return input["a"] // 3', f"$floor({INPUT}.a / 3)"),
        ('return input["a"] ** 2', f"$power({INPUT}.a, 2)"),
        ('return {**input, "a": 1}', f"$merge([{INPUT}, {{'a': 1}}])"),
        (
            'return {"a": 1, **input["b"], **input["c"], "d": 2, "e": 3}',
            f"$merge([{{'a': 1}}, {INPUT}.b, {INPUT}.c, {{'d': 2, 'e': 3}}])",
        ),
        ('return {**input["b"]}', f"{INPUT}.b"),
        (
            'return (input["a"] + 1) % 3',
            f"{INPUT}.a + 1 - 3 * $floor(({INPUT}.a + 1) / 3)",
        ),
        ('return -input["a"]', f"-{INPUT}.a"),
        ('return -(input["a"] - 1)', f"-({INPUT}.a - 1)"),
        ('return -(-input["a"])', f"-(-{INPUT}.a)"),
        ('return 2 * -input["a"]', f"2 * -{INPUT}.a"),
        (
            'items: list = input["items"]\nreturn 1 if items and input["a"] else 2',
            f"$count($items) > 0 and {INPUT}.a ? 1 : 2",
        ),
        ('return 1 if not input["a"] else 2', f"$not({INPUT}.a) ? 1 : 2"),
        (
            'return 1 if input["a"] or not input["b"] else 2',
            f"{INPUT}.a or $not({INPUT}.b) ? 1 : 2",
        ),
        ('return input["a"] == None', f"{INPUT}.a = null"),
        ('return input["a"] != "x"', f"{INPUT}.a != 'x'"),
        ('return 0 < input["a"] <= 10', f"0 < {INPUT}.a and {INPUT}.a <= 10"),
        ('return (input["a"] < 1) == True', f"({INPUT}.a < 1) = true"),
        ('return input["a"] in [1, 2]', f"{INPUT}.a in [1, 2]"),
        ('return "coupon" in input', f"$exists({INPUT}.coupon)"),
        ('return "coupon" not in input', f"$not($exists({INPUT}.coupon))"),
        (
            'return input["a"] is None',
            f"$not($exists({INPUT}.a) and {INPUT}.a != null)",
        ),
        ('return input["a"] is not None', f"$exists({INPUT}.a) and {INPUT}.a != null"),
        ('return input["a"] or "none"', f"$boolean({INPUT}.a) ? {INPUT}.a : 'none'"),
        (
            'return input["a"] and input["b"]',
            f"$boolean({INPUT}.a) ? {INPUT}.b : {INPUT}.a",
        ),
        (
            'return input["a"] or input["b"] or 0',
            f"$boolean({INPUT}.a) ? {INPUT}.a : $boolean({INPUT}.b) ? {INPUT}.b : 0",
        ),
        (
            'return input["a"] > 1 and input["b"] < 2',
            f"{INPUT}.a > 1 and {INPUT}.b < 2",
        ),
        (
            'return input["a"] > 1 or input["b"] < 2 and input["c"] == 3',
            f"{INPUT}.a > 1 or {INPUT}.b < 2 and {INPUT}.c = 3",
        ),
        (
            'return (input["a"] > 1 or input["b"] < 2) and input["c"] == 3',
            f"({INPUT}.a > 1 or {INPUT}.b < 2) and {INPUT}.c = 3",
        ),
        ('return not input["a"]', f"$not({INPUT}.a)"),
        ('return not (input["a"] or input["b"])', f"$not({INPUT}.a or {INPUT}.b)"),
        ('items: list = input["items"]\nreturn not items', "$count($items) = 0"),
        ('return 1 if input["a"] else 2', f"$boolean({INPUT}.a) ? 1 : 2"),
        ('return 1 if input["a"] > 0 else 2', f"{INPUT}.a > 0 ? 1 : 2"),
        ('return (1 if input["a"] else 2) + 1', f"($boolean({INPUT}.a) ? 1 : 2) + 1"),
        ('return bool(input["a"])', f"$boolean({INPUT}.a)"),
        ('items: list = input["items"]\nreturn bool(items)', "$count($items) > 0"),
        ('return float(input["a"])', f"$number({INPUT}.a)"),
        ('return int(input["a"])', f"$floor($number({INPUT}.a))"),
        ('return str(input["a"])', f"$string({INPUT}.a)"),
        ('items: list = input["items"]\nreturn len(items)', "$count($items)"),
        ('name: str = input["name"]\nreturn len(name)', "$length($name)"),
        ('tags: dict = input["tags"]\nreturn len(tags)', "$count($keys($tags))"),
        ('return isinstance(input["v"], str)', f"$type({INPUT}.v) = 'string'"),
        (
            'return isinstance(input["v"], (float, int, bool))',
            f"$type({INPUT}.v) in ['number', 'boolean']",
        ),
        ('tags: dict = input["tags"]\nreturn "a" in tags', "$exists($tags.a)"),
        (
            'tags: dict = input["tags"]\nreturn input["k"] in tags',
            f"$exists($lookup($tags, {INPUT}.k))",
        ),
        ('text: str = input["text"]\nreturn "ab" in text', "$contains($text, 'ab')"),
        ('items: list = input["items"]\nreturn 1 not in items', "$not(1 in $items)"),
        (
            'items: list = input["items"]\nreturn items[input["i"]]',
            f"$items[{INPUT}.i]",
        ),
        (
            'tags: dict = input["tags"]\nreturn tags[input["k"]]',
            f"$lookup($tags, {INPUT}.k)",
        ),
        ('key: str = input["k"]\nreturn input[key]', f"$lookup({INPUT}, $key)"),
        ('i: float = input["i"]\nreturn input["items"][i]', f"{INPUT}.items[$i]"),
        ('text: str = input["text"]\nreturn text[0]', "$substring($text, 0, 1)"),
        ('text: str = input["text"]\nreturn text[-1]', "$substring($text, -1, 1)"),
        ('return input["items"][-1]', f"{INPUT}.items[-1]"),
    ],
)
def test_spelling(body, code):
    assert output(body) == "{% " + code + " %}"


def test_boolean_literals_and_negative_numbers_stay_json():
    assert output("return [True, -5, -1.5, []]") == [True, -5, -1.5, []]


def test_annotated_parameter():
    assert output('return "a" in input', "input: dict") == f"{{% $exists({INPUT}.a) %}}"


def test_declared_type_holds_for_untyped_reassignment():
    body = 'total: float = input["a"]\ntotal = input["b"]\nreturn total + input["c"]'
    assert output(body) == f"{{% $total + {INPUT}.c %}}"
    body = (
        'total: float = input["a"]\ntotal, n = input["b"], 1\nreturn total + input["c"]'
    )
    assert output(body) == f"{{% $total + {INPUT}.c %}}"


def test_inferred_types_flow_through_variables():
    body = 'n = len("abc")\nwords = ["a"] + input["w"]\nreturn [n + input["x"], words + [1]]'
    assert output(body) == [f"{{% $n + {INPUT}.x %}}", "{% $append($words, [1]) %}"]


@pytest.mark.parametrize(
    "annotation, body, code",
    [
        ("list[float]", "return x[0] + input['y']", f"$x[0] + {INPUT}.y"),
        ("dict[str, str]", "return x['a'] + input['y']", f"$x.a & {INPUT}.y"),
        (
            "dict[str, list]",
            "return len(x[input['k']])",
            f"$count($lookup($x, {INPUT}.k))",
        ),
        ("list[str] | list[float]", "return x[0] + 1", None),
        ("None", "return x is None", "$not($exists($x) and $x != null)"),
        ("int", "return x + input['y']", f"$x + {INPUT}.y"),
    ],
)
def test_annotations(annotation, body, code):
    text = f"x: {annotation} = input['x']\n{body}"
    if code is None:
        with pytest.raises(CompileError, match=re.escape("may be number | string")):
            output(text)
        return
    assert output(text) == "{% " + code + " %}"


@pytest.mark.parametrize(
    "body, execution_input, expected",
    [
        ('return input["a"] + 1', {"a": 2}, 3),
        ('return input["a"] + "!"', {"a": "hi"}, "hi!"),
        ('return input["a"] + [3]', {"a": [1, 2]}, [1, 2, 3]),
        (
            'return [input["a"] + [[3]], [[1]] + input["a"]]',
            {"a": [[1]]},
            [[[1], [3]], [[1], [1]]],
        ),
        ('return input["a"] - input["b"] - 1', {"a": 10, "b": 3}, 6),
        ('return input["a"] - (input["b"] - 1)', {"a": 10, "b": 3}, 8),
        ('return [input["a"] % 3, input["a"] % -3, 5.5 % 2]', {"a": -7}, [2, -1, 1.5]),
        ('return [input["a"] // 2, input["a"] ** 2]', {"a": -7}, [-4, 49]),
        ('return 2 * -input["a"]', {"a": 3}, -6),
        ('return 0 < input["a"] <= 10', {"a": 10}, True),
        ('return input["a"] == [1, {"b": 2}]', {"a": [1, {"b": 2}]}, True),
        (
            'return [input["a"] is None, input["b"] is None, input["c"] is not None]',
            {"a": None, "c": 0, "b": 1},
            [True, False, True],
        ),
        (
            'return [input["a"] or "d", input["b"] or "d"]',
            {"a": 0, "b": "x"},
            ["d", "x"],
        ),
        (
            'return [input["a"] and "t", input["b"] and "t"]',
            {"a": [], "b": 1},
            [[], "t"],
        ),
        (
            'items: list = input["items"]\nreturn [bool(items), not items]',
            {"items": [0]},
            [True, False],
        ),
        (
            'return [not input["a"], not input["b"]]',
            {"a": "", "b": {"k": 1}},
            [True, False],
        ),
        ('return "yes" if input["a"] else "no"', {"a": {}}, "no"),
        ('return [len(input["s"]), len(input["l"]), len(input["d"])]', None, None),
        (
            'return [float(input["a"]), int(input["a"]), str(input["b"])]',
            {"a": "3.7", "b": 5},
            [3.7, 3, "5"],
        ),
        ('return isinstance(input["v"], (list, dict))', {"v": {}}, True),
        (
            'tags: dict = input["tags"]\nreturn ["a" in tags, input["k"] in tags]',
            {"tags": {"a": None}, "k": "b"},
            [True, False],
        ),
        (
            'text: str = input["text"]\nreturn ["ll" in text, text[1], text[-1]]',
            {"text": "hello"},
            [True, "e", "o"],
        ),
        (
            'items: list = input["items"]\nreturn [2 in items, 5 not in items, items[input["i"]]]',
            {"items": [1, 2], "i": -1},
            [True, True, 2],
        ),
        ('key: str = input["k"]\nreturn input[key]', {"k": "x y", "x y": 1}, 1),
        (
            'return [{**input["a"], "x": 1}, {"x": 1, **input["a"]}]',
            {"a": {"x": 2, "y": None}},
            [{"x": 1, "y": None}, {"x": 2, "y": None}],
        ),
    ],
)
def test_evaluation(body, execution_input, expected):
    if expected is None:
        body = (
            's: str = input["s"]\nl: list = input["l"]\nd: dict = input["d"]\n'
            "return [len(s), len(l), len(d)]"
        )
        execution_input, expected = (
            {"s": "héllo😀", "l": [[1, 2]], "d": {"a": 1}},
            [6, 1, 1],
        )
    assert asl.run(definition(body), execution_input) == expected


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return input["a"] + input["b"]',
            "+ adds numbers, joins strings or lists, so the type of input['a'] must be known; assign it to an annotated variable first: value: float = input['a']",
        ),
        (
            'a = input["a"]\nreturn a + input["b"]',
            "annotate it where it is assigned: a: float = ...",
        ),
        ('return 1 + "a"', "+ cannot join number and string"),
        ("return True + 1", "+ cannot join boolean and number"),
        ("return None + None", "+ takes numbers, strings or lists, not a null"),
        (
            'v: str | None = input["v"]\nreturn v + "x"',
            "v may be null | string; narrow it first",
        ),
        ('v: str | None = input["v"]\nreturn -v', "v may be null | string"),
        (
            'return "a" - 1',
            "'a' is a string, and - takes numbers; convert it with float('a')",
        ),
        ('return -"a"', "is a string, and - takes numbers"),
        ('return +input["a"]', "remove the unary +"),
        ('return ~input["a"]', "no bitwise operators"),
        ('return input["a"] | 1', "no bitwise or matrix operators"),
        ('return "a" < 1', "a number and a string cannot be ordered"),
        ("return [1] < [2]", "JSONata orders only numbers and strings"),
        ('v: str | None = input["v"]\nreturn v < "x"', "v may be null | string"),
        ('return input["a"] is 1', "is compares with None only"),
        (
            'return input["a"] in input["b"]',
            "in depends on the container, so the type of input['b'] must be known",
        ),
        ("return 1 in 2", "2 is a number; in looks into lists, dicts and strings"),
        ('return len(input["a"])', "len depends on the type"),
        ("return len(1)", "1 is a number; len takes lists, strings and dicts"),
        ("return len(1, 2)", "len() takes one argument"),
        ("return len(input)", "annotate the parameter: def ...(input: list)"),
        ("return float(x=1)", "takes no keyword arguments"),
        (
            'return list(input["a"])',
            "list() depends on the type, so the type of input['a'] must be known",
        ),
        (
            'return dict(input["a"])',
            "dict() does not convert here; declare the type instead: x: dict = ...",
        ),
        ('return abs(input["a"])', "calling abs() is not supported"),
        ('return input["a"].get("k")', "input['a'].get() is not supported"),
        ('return isinstance(input["a"])', "isinstance takes a value and a class"),
        ('return isinstance(input["a"], list[str])', "isinstance takes str, float"),
        ('return isinstance(input["a"], None)', "test None with `is None`"),
        ('return input["a"][1:2]', "a slice depends on the type"),
        ('d: dict = input["d"]\nreturn d[1:]', "d is a object; slices take lists"),
        (
            'xs: list = input["xs"]\nreturn xs["a":]',
            "'a' is a string, and a slice takes numbers",
        ),
        ('return input[input["k"]]', "a variable key depends on the container"),
        (
            'items: list = input["items"]\nreturn items["a"]',
            "items is a array; string keys look into dicts",
        ),
        (
            'k: str = input["k"]\nitems: list = input["items"]\nreturn items[k]',
            "string keys look into dicts",
        ),
        (
            'tags: dict = input["tags"]\nreturn tags[0]',
            "tags is a object; positions look into lists and strings",
        ),
        (
            'k: str | float = input["k"]\nreturn input["a"][k]',
            "k may be number | string",
        ),
        ("return (1, 2)", "JSON has no tuples"),
        (
            'v: dict | None = input["v"]\nreturn {**v}',
            "v may be null | object; narrow it first",
        ),
        ("return {x for x in input}", "JSON has lists only"),
        ('x: Any = input["x"]', "annotate with float, str, bool"),
        ('x: dict[int, str] = input["x"]', "annotate with float, str, bool"),
        ('x: "float" = input["x"]', "annotate with float, str, bool"),
        (
            "x: float",
            "an annotation declares the type of a value; assign it here: x: float = ...",
        ),
        ("input.x: float = 1", "assign one variable per statement"),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(body))
    assert message in raised.value.message


IMPORTS = "import json\nimport uuid\nfrom uuid import uuid4\n"


def imported(body: str) -> dict:
    (compiled,) = compile_source(IMPORTS + source(body)).values()
    return compiled


@pytest.mark.parametrize(
    "body, code",
    [
        ('return json.loads(input["raw"])', f"$parse({INPUT}.raw)"),
        ('raw: str = input["raw"]\nreturn json.loads(raw)["a"]', "$parse($raw).a"),
        ("return str(uuid.uuid4())", "$uuid()"),
        ('return f"order-{uuid4()}"', "'order-' & $uuid()"),
    ],
)
def test_module_functions(body, code):
    assert imported(body)["States"]["return"]["Output"] == "{% " + code + " %}"


def test_module_functions_evaluate():
    body = 'return [json.loads(input["raw"]), str(uuid.uuid4())]'
    parsed, made = asl.run(imported(body), {"raw": '{"a": [1, null], "b": "x"}'})
    assert parsed == {"a": [1, None], "b": "x"}
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", made
    )


@pytest.mark.parametrize(
    "body, message",
    [
        ("return json.loads(1)", "1 is a number; json.loads() reads a string"),
        ('return json.loads("{}", parse_float=float)', "json.loads() takes one string"),
        (
            "return uuid.uuid4()",
            "uuid.uuid4() is a UUID object, not JSON; write str(uuid.uuid4())",
        ),
        ("return str(uuid4(1))", "uuid.uuid4() takes no arguments"),
        (
            'raw: str | None = input["raw"]\nreturn json.loads(raw)',
            "raw may be null | string; narrow it first",
        ),
    ],
)
def test_module_function_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(IMPORTS + source(body))
    assert message in raised.value.message


def test_module_function_without_import():
    with pytest.raises(CompileError) as raised:
        compile_source(source('return json.loads(input["raw"])'))
    assert raised.value.message == "json is not imported; write import json"


def test_a_variable_named_after_a_function_is_renamed():
    body = (
        'count: list = input["xs"]\n'
        "merge = {**input}\n"
        "type, keys = len(count), [map * 2 for map in count]\n"
        'return [count, merge["xs"], type, keys, len(merge)]'
    )
    compiled = definition(body)
    assert compiled["States"]["count"]["Assign"] == {
        "count_val": f"{{% {INPUT}.xs %}}",
        "merge_val": f"{{% {INPUT} %}}",
    }
    assert asl.run(compiled, {"xs": [1, 2]}) == [[1, 2], [1, 2], 2, [2, 4], 1]


def test_a_renamed_variable_takes_a_name_the_module_does_not_use():
    body = (
        'count_val: list = input["xs"]\n'
        "count = len(count_val)\n"
        "count_val_2 = [count * 2 for count in count_val]\n"
        "return [count, count_val_2]"
    )
    compiled = definition(body)
    assert compiled["States"]["count"]["Assign"] == {
        "count_val_3": "{% $count($count_val) %}",
        "count_val_2": "{% [$map($count_val, function($count_val_3) { $count_val_3 * 2 })] %}",
    }
    assert asl.run(compiled, {"xs": [1, 2]}) == [2, [2, 4]]


STRINGS = 's: str = input["s"]\nparts: list = input["parts"]\n'


@pytest.mark.parametrize(
    "body, code",
    [
        ('return s.split("/")', "$split($s, '/')"),
        ("return s.split()", "$split($trim($s), ' ')"),
        ('return s.split("/")[-1]', "$split($s, '/')[-1]"),
        ('return s.replace(".", "-")', "$replace($s, '.', '-')"),
        ('return s.replace("a", "b", 2)', "$replace($s, 'a', 'b', 2)"),
        ("return s.lower()", "$lowercase($s)"),
        ('return "id-" + s.upper()', "'id-' & $uppercase($s)"),
        ('return input["s"].upper()', f"$uppercase({INPUT}.s)"),
        ('return ", ".join(parts)', "$join($parts, ', ')"),
    ],
)
def test_string_methods(body, code):
    assert output(STRINGS + body) == "{% " + code + " %}"


def test_string_methods_evaluate():
    body = (
        'return [s.split("/"), s.split(), s.replace("/", "-", 1), s.lower(), '
        '"+".join(parts), [p.upper() for p in s.split()]]'
    )
    execution_input = {"s": " a/B/c\td ", "parts": ["x", "y"]}
    assert asl.run(definition(STRINGS + body), execution_input) == [
        [" a", "B", "c\td "],
        ["a/B/c", "d"],
        " a-B/c\td ",
        " a/b/c\td ",
        "x+y",
        ["A/B/C", "D"],
    ]


@pytest.mark.parametrize(
    "body, message",
    [
        ('return parts.split("/")', "parts is a array; split() is a string method"),
        ("return s.split(1)", "1 is a number; split() splits at a string"),
        ('return s.split("/", 1)', "split() takes no maximum"),
        ('return s.split(sep="/")', "split() takes no keyword arguments here"),
        ('return s.replace("a")', "replace() is written s.replace(old, new)"),
        ('return s.replace("a", "b", "c")', "the count of replace() takes numbers"),
        ("return s.lower(1)", "lower() is written s.lower()"),
        ("return s.join(s)", "s is a string; join() takes a list of strings"),
        (
            'v: str | None = input["v"]\nreturn v.upper()',
            "v may be null | string; narrow it first",
        ),
    ],
)
def test_string_method_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(STRINGS + body))
    assert message in raised.value.message


SLICES = 's: str = input["s"]\nxs: list[float] = input["xs"]\ni: float = input["i"]\n'


@pytest.mark.parametrize(
    "body, code",
    [
        ("return s[1:3]", "$substring($s, 1, 2)"),
        ("return s[2:]", "$substring($s, 2, $length($s))"),
        ("return s[-3:]", "$substring($s, -3, 3)"),
        ("return s[1:-1]", "$substring($s, 1, $length($s) - 2)"),
        ("return s[-3:4]", "$substring($s, -3, 7 - $length($s))"),
        ("return s[i:-1]", "$substring($s, $i, $length($s) - (1 + $i))"),
        ("return s[-2:i]", "$substring($s, -2, $i + 2 - $length($s))"),
        ("return s[-i:]", "$substring($s, -$i, $i)"),
        ("return s[:-i]", "$substring($s, 0, $length($s) - $i)"),
        (
            "return xs[-i:]",
            "[$filter($xs, function($v, $i_2) { $i_2 >= $count($xs) - $i })]",
        ),
        ("return s[:]", "$s"),
        ("return xs[1:3]", "[$filter($xs, function($v, $i) { $i >= 1 and $i < 3 })]"),
        (
            "return xs[-2:]",
            "[$filter($xs, function($v, $i) { $i >= $count($xs) - 2 })]",
        ),
        ("return xs[i:]", "[$filter($xs, function($v, $i_2) { $i_2 >= $i })]"),
        (
            'nested: list = input["nested"]\nreturn nested[:1]',
            "$append([], $filter($nested, function($v, $i) { $i < 1 })[])",
        ),
        ("return xs[0:]", "$xs"),
    ],
)
def test_slices(body, code):
    assert output(SLICES + body) == "{% " + code + " %}"


def test_slices_evaluate():
    body = SLICES + (
        "return [s[1:3], s[:3], s[2:], s[-3:], s[:-1], s[1:-1], s[-3:-1], s[-3:4], "
        "s[i:], s[:i], s[i:-1], s[-2:i], s[5:2], xs[1:3], xs[:2], xs[-2:], xs[:-1], "
        "xs[i:], xs[-10:-8], xs[3:1], s[-i:], xs[-i:], s[:-i]]"
    )
    values = {"s": "hello!", "xs": [10, 20, 30, 40], "i": 2}
    assert asl.run(definition(body), values) == [
        "el",
        "hel",
        "llo!",
        "lo!",
        "hello",
        "ello",
        "lo",
        "l",
        "llo!",
        "he",
        "llo",
        "",
        "",
        [20, 30],
        [10, 20],
        [30, 40],
        [10, 20, 30],
        [30, 40],
        [],
        [],
        "o!",
        [30, 40],
        "hell",
    ]


DICTS = (
    'd: dict = input["d"]\nnested: dict[str, list] = input["nested"]\n'
    'flat: dict[str, float] = input["flat"]\n'
)


@pytest.mark.parametrize(
    "body, code",
    [
        ("return list(d)", "[$keys($d)]"),
        ("return d.keys()", "[$keys($d)]"),
        ('return input["d"].keys()', f"[$keys({INPUT}.d)]"),
        (
            "return nested.values()",
            "$append([], $each($nested, function($v) { $v })[])",
        ),
        ("return flat.values()", "[$each($flat, function($v) { $v })]"),
        ('s: str = input["s"]\nreturn list(s)', "$split($s, '')"),
        ('xs: list = input["xs"]\nreturn list(xs)', "$xs"),
    ],
)
def test_keys_and_values(body, code):
    assert output(DICTS + body) == "{% " + code + " %}"


def test_keys_and_values_evaluate():
    body = DICTS + (
        "return [list(d), d.keys(), d.values(), nested.values(), flat.values(), "
        'list(str(input["s"])), [k + "!" for k in d.keys()]]'
    )
    execution_input = {
        "d": {},
        "nested": {"a": [1, 2]},
        "flat": {"x": 1, "y": 2},
        "s": "ab",
    }
    assert asl.run(definition(body), execution_input) == [
        [],
        [],
        [],
        [[1, 2]],
        [1, 2],
        ["a", "b"],
        [],
    ]


@pytest.mark.parametrize(
    "body, message",
    [
        ('n: float = input["n"]\nreturn list(n)', "n is a number; list() takes a dict"),
        ("return list()", "list() takes one argument"),
        (
            'xs: list = input["xs"]\nreturn xs.keys()',
            "xs is a array; keys() is a dict method",
        ),
        ("return d.keys(1)", "keys() takes no arguments"),
        (
            'v: dict | None = input["v"]\nreturn v.values()',
            "v may be null | object; narrow it first",
        ),
    ],
)
def test_keys_and_values_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(DICTS + body))
    assert message in raised.value.message
