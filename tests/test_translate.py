import base64
import re
import textwrap
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.expressions import spellings
from tests import asl, truthiness, truthy, unpacked

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
        ('return input["a"] / -2', f"{INPUT}.a / -2"),
        # A divisor not written as a number is tested, as dividing by zero
        # raises in Python and gives the string "Infinity" in JSONata.
        (
            'b: float = input["b"]\nreturn input["a"] / b',
            f"$b = 0 ? $error('division by zero') : {INPUT}.a / $b",
        ),
        (
            'b: float = input["b"]\nreturn input["a"] // b',
            f"$b = 0 ? $error('division by zero') : $floor({INPUT}.a / $b)",
        ),
        (
            'b: float = input["b"]\nreturn input["a"] % b',
            (
                f"$b = 0 ? $error('division by zero') : "
                f"{INPUT}.a - $b * $floor({INPUT}.a / $b)"
            ),
        ),
        (
            'b: float = input["b"]\nc: float = input["c"]\nreturn 1 / (b + c)',
            "$b + $c = 0 ? $error('division by zero') : 1 / ($b + $c)",
        ),
        # Nothing is folded, so a divisor that is zero when it runs is tested.
        ("return 1 / (2 - 2)", "2 - 2 = 0 ? $error('division by zero') : 1 / (2 - 2)"),
        ('return input["a"] ** 2', f"$power({INPUT}.a, 2)"),
        (
            'return {**input, "a": 1}',
            f"$merge([{unpacked(INPUT)}, {{'a': 1}}])",
        ),
        (
            'return {"a": 1, **input["b"], **input["c"], "d": 2, "e": 3}',
            (
                f"$merge([{{'a': 1}}, {unpacked(f'{INPUT}.b')}, "
                f"{unpacked(f'{INPUT}.c')}, {{'d': 2, 'e': 3}}])"
            ),
        ),
        ('return {**input["b"]}', unpacked(f"{INPUT}.b")),
        (
            'd: dict = input["d"]\nreturn {**d, "k": 1}',
            "$merge([$d, {'k': 1}])",
        ),
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
            f"$count($items) > 0 and {truthy(f'{INPUT}.a')} ? 1 : 2",
        ),
        (
            'return 1 if not input["a"] else 2',
            f"$not({truthy(f'{INPUT}.a')}) ? 1 : 2",
        ),
        (
            'return 1 if input["a"] or not input["b"] else 2',
            f"{truthy(f'{INPUT}.a')} or $not({truthy(f'{INPUT}.b')}) ? 1 : 2",
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
        (
            'return input["a"] or "none"',
            f"($v := {INPUT}.a; ({truthiness()}) ? $v : 'none')",
        ),
        (
            'return input["a"] and input["b"]',
            f"($v := {INPUT}.a; ({truthiness()}) ? {INPUT}.b : $v)",
        ),
        (
            'return input["a"] or input["b"] or 0',
            (
                f"($v_2 := {INPUT}.a; ({truthiness('$v_2')}) ? $v_2 : "
                f"($v := {INPUT}.b; ({truthiness()}) ? $v : 0))"
            ),
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
        ('return not input["a"]', f"$not({truthy(f'{INPUT}.a')})"),
        (
            'return not (input["a"] or input["b"])',
            f"$not({truthy(f'{INPUT}.a')} or {truthy(f'{INPUT}.b')})",
        ),
        ('items: list = input["items"]\nreturn not items', "$count($items) = 0"),
        ('return 1 if input["a"] else 2', f"{truthy(f'{INPUT}.a')} ? 1 : 2"),
        ('return 1 if input["a"] > 0 else 2', f"{INPUT}.a > 0 ? 1 : 2"),
        (
            'return (1 if input["a"] else 2) + 1',
            f"({truthy(f'{INPUT}.a')} ? 1 : 2) + 1",
        ),
        ('return bool(input["a"])', truthy(f"{INPUT}.a")),
        ('items: list = input["items"]\nreturn bool(items)', "$count($items) > 0"),
        ('return float(input["a"])', f"$number({INPUT}.a)"),
        (
            'return int(input["a"])',
            f"($v := $number({INPUT}.a); $v < 0 ? $ceil($v) : $floor($v))",
        ),
        (
            'return ",".join(input["xs"])',
            (
                f"($v := {INPUT}.xs; $join($type($v) = 'string' "
                f"? $split($v, '') : $v, ','))"
            ),
        ),
        (
            's: str = input["s"]\nreturn ",".join(s)',
            "$join($split($s, ''), ',')",
        ),
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


def test_a_declaration_without_a_value_types_the_later_assignments():
    body = 'x: list\nx = input["x"]\nreturn x + [1]'
    (compiled,) = compile_source(source(body)).values()
    assert asl.run(compiled, {"x": [0]}) == [0, 1]


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
        (
            'b: float = input["b"]\nreturn [input["a"] / b, input["a"] // b]',
            {"a": 7, "b": 2},
            [3.5, 3],
        ),
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
        (
            (
                'return [int(input["a"]), int(input["b"]), int(input["c"]),'
                ' int(input["d"]), int(input["e"])]'
            ),
            {"a": -1.5, "b": -0.5, "c": 0, "d": 1.5, "e": -2},
            [-1, 0, 0, 1, -2],
        ),
        ('return ",".join(input["xs"])', {"xs": ["a", "b"]}, "a,b"),
        ('s: str = input["s"]\nreturn ",".join(s)', {"s": "ab"}, "a,b"),
        ('return ",".join(input["xs"])', {"xs": "ab"}, "a,b"),
        ('return {**input["d"], "k": 1}', {"d": {"a": 2}}, {"a": 2, "k": 1}),
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


def test_unpacking_a_value_that_is_not_a_dict_fails_where_it_unpacks():
    """Python raises there, and $merge would take a list of dicts as the
    dicts themselves."""
    body = 'return {**input["d"], "k": 1}'
    with pytest.raises(asl.Failure) as raised:
        asl.run(definition(body), {"d": [{"a": 1}, {"b": 2}]})
    assert raised.value.error == "States.QueryEvaluationError"
    assert raised.value.cause == "** unpacks dicts"
    namespace: dict[str, object] = {}
    exec(source(body), namespace)
    pay = namespace["pay"]
    assert callable(pay)
    with pytest.raises(TypeError):
        pay({"d": [{"a": 1}, {"b": 2}]})


def test_dividing_by_zero_fails_where_it_divides():
    """The module raises there, and the definition fails where it divides."""
    body = 'b: float = input["b"]\nreturn input["a"] / b'
    with pytest.raises(asl.Failure) as raised:
        asl.run(definition(body), {"a": 10, "b": 0})
    assert raised.value.error == "States.QueryEvaluationError"
    assert raised.value.cause == "division by zero"
    namespace: dict[str, object] = {}
    exec(source(body), namespace)
    pay = namespace["pay"]
    assert callable(pay)
    with pytest.raises(ZeroDivisionError):
        pay({"a": 10, "b": 0})


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
        (
            'return input["a"] / 0',
            "dividing by 0 fails every time; divide by a value that is not zero",
        ),
        ('return input["a"] % 0.0', "dividing by 0.0 fails every time"),
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
        ('return divmod(input["a"], 2)', "calling divmod() is not supported"),
        (
            'return input["a"].title()',
            "input['a'].title() is not supported; write the operation with operators, supported functions or jsonata(), or compute it in a Lambda task (the methods sfnx compiles are split, replace, lower, upper, join, startswith, endswith, ljust, rjust and strip of strings and keys, values and get of dicts)",
        ),
        # Rejected spellings whose messages name what to write instead.
        (
            'return map(str, input["a"])',
            "map() is not supported; write a comprehension: [str(x) for x in xs]",
        ),
        (
            'return filter(None, input["a"])',
            "filter() is not supported; write a comprehension: [x for x in xs if x]",
        ),
        (
            'return input["a"].append("k")',
            (
                "input['a'].append() is not supported; a list is a value here, "
                "so write xs = xs + [x]"
            ),
        ),
        (
            'return input["a"].extend(input["b"])',
            (
                "input['a'].extend() is not supported; a list is a value here, "
                "so write xs = xs + ys"
            ),
        ),
        (
            'return input["a"].insert(0, "k")',
            (
                "input['a'].insert() is not supported; a list is a value here, "
                "so write xs = xs[:i] + [x] + xs[i:]"
            ),
        ),
        (
            'return input["a"].format(1)',
            (
                "input['a'].format() is not supported; write an f-string, "
                'such as f"{n} items"'
            ),
        ),
        # % on a string is formatting, not the remainder. The message writes
        # out the f-string when every conversion carries over, and gives an
        # example of one when it does not.
        (
            'n: float = input["n"]\nreturn "%d items" % n',
            "old-style % formatting is not supported; write an f-string: f'{n} items'",
        ),
        (
            'a: str = input["a"]\nb: str = input["b"]\nreturn "%s: %s" % (a, b)',
            "old-style % formatting is not supported; write an f-string: f'{a}: {b}'",
        ),
        (
            'n: float = input["n"]\nreturn "%.2f" % n',
            (
                "old-style % formatting is not supported; write an f-string, "
                'such as f"{n} items"'
            ),
        ),
        (
            'd: dict = input["d"]\nreturn "%(name)s" % d',
            (
                "old-style % formatting is not supported; write an f-string, "
                'such as f"{n} items"'
            ),
        ),
        (
            'n: float = input["n"]\nreturn "%d%% done" % n',
            "old-style % formatting is not supported; write an f-string: f'{n}% done'",
        ),
        (
            'a: str = input["a"]\nreturn "%s %s" % (a,)',
            (
                "old-style % formatting is not supported; write an f-string, "
                'such as f"{n} items"'
            ),
        ),
        (
            'a: str = input["a"]\nb: str = input["b"]\nreturn "%s" % (a, b)',
            (
                "old-style % formatting is not supported; write an f-string, "
                'such as f"{n} items"'
            ),
        ),
        ('return isinstance(input["a"])', "isinstance takes a value and a class"),
        ('return isinstance(input["a"], list[str])', "isinstance takes str, float"),
        ('return isinstance(input["a"], None)', "test None with `is None`"),
        ('return input["a"][1:2]', "a slice depends on the type"),
        ('d: dict = input["d"]\nreturn d[1:]', "d is an object; slices take lists"),
        (
            'xs: list = input["xs"]\nreturn xs["a":]',
            "'a' is a string, and a slice takes numbers",
        ),
        ('return input[input["k"]]', "a variable key depends on the container"),
        (
            'items: list = input["items"]\nreturn items["a"]',
            "items is an array; string keys look into dicts",
        ),
        (
            'k: str = input["k"]\nitems: list = input["items"]\nreturn items[k]',
            "string keys look into dicts",
        ),
        (
            'tags: dict = input["tags"]\nreturn tags[0]',
            "tags is an object; positions look into lists and strings",
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
        ("input.x: float", "declare one variable: name: type"),
        ("input.x: float = 1", "assign one variable per statement"),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(body))
    assert message in raised.value.message


IMPORTS = (
    "import json\nimport math\nimport os\nimport time\nimport uuid\n"
    "from datetime import datetime, timedelta\nfrom uuid import uuid4\n"
)


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
        ("return str(datetime.now())", "$now()"),
        ('return f"at {datetime.now()}"', "'at ' & $now()"),
        ("return time.time()", "$millis() / 1000"),
        ("return datetime.now().timestamp()", "$millis() / 1000"),
        (
            'return datetime.fromisoformat(input["at"]).timestamp()',
            f"$toMillis({INPUT}.at) / 1000",
        ),
        (
            'return str(datetime.fromtimestamp(input["t"]))',
            f"$fromMillis({INPUT}.t * 1000)",
        ),
        (
            't: float = input["t"]\nreturn f"at {datetime.fromtimestamp(t)}"',
            "'at ' & $fromMillis($t * 1000)",
        ),
        (
            'return str(datetime.fromisoformat(input["at"]))',
            f"$fromMillis($toMillis({INPUT}.at))",
        ),
        ('return datetime.fromtimestamp(input["t"]).timestamp()', f"{INPUT}.t"),
        # A timedelta moves a datetime by the milliseconds it spans, added up
        # where it is written.
        (
            "return str(datetime.now() + timedelta(hours=1))",
            "$fromMillis($millis() + 3600000)",
        ),
        (
            "return str(timedelta(hours=1) + datetime.now())",
            "$fromMillis($millis() + 3600000)",
        ),
        (
            "return str(datetime.now() - timedelta(minutes=30))",
            "$fromMillis($millis() - 1800000)",
        ),
        (
            "return str(datetime.now() + timedelta(hours=-1))",
            "$fromMillis($millis() - 3600000)",
        ),
        (
            "return str(datetime.now() + timedelta(days=1, minutes=-30))",
            "$fromMillis($millis() + 84600000)",
        ),
        (
            'return str(datetime.fromisoformat(input["at"]) - timedelta(weeks=1))',
            f"$fromMillis($toMillis({INPUT}.at) - 604800000)",
        ),
        (
            'return f"at {datetime.now() + timedelta(milliseconds=1)}"',
            "'at ' & $fromMillis($millis() + 1)",
        ),
        # Nothing to move leaves the moment as it is written.
        ("return str(datetime.now() + timedelta())", "$now()"),
        (
            "return (datetime.now() + timedelta(seconds=1)).timestamp()",
            "($millis() + 1000) / 1000",
        ),
        (
            'return (datetime.fromtimestamp(input["t"]) - timedelta(days=1)).timestamp()',
            f"({INPUT}.t * 1000 - 86400000) / 1000",
        ),
        # strftime writes the datetime with the picture string that writes
        # what its format writes.
        (
            'return datetime.now().strftime("%Y-%m-%d")',
            "$now('[Y0001]-[M01]-[D01]')",
        ),
        (
            'return datetime.fromisoformat(input["at"]).strftime("%H:%M:%S")',
            f"$fromMillis($toMillis({INPUT}.at), '[H01]:[m01]:[s01]')",
        ),
        (
            'return (datetime.now() + timedelta(days=1)).strftime("%j")',
            "$fromMillis($millis() + 86400000, '[d001]')",
        ),
        (
            'return datetime.now().strftime("100%% [ok] %y")',
            "$now('100% [[ok]] [Y01]')",
        ),
        (
            "return f\"key-{datetime.now().strftime('%Y%m%d')}\"",
            "'key-' & $now('[Y0001][M01][D01]')",
        ),
        # The seconds between two datetimes are the milliseconds divided.
        (
            'return (datetime.now() - datetime.fromisoformat(input["at"])).total_seconds()',
            f"($millis() - $toMillis({INPUT}.at)) / 1000",
        ),
    ],
)
def test_module_functions(body, code):
    assert imported(body)["States"]["return"]["Output"] == "{% " + code + " %}"


def test_module_functions_evaluate():
    body = (
        'return [json.loads(input["raw"]), str(uuid.uuid4()), str(datetime.now()), '
        "time.time() > 1e9]"
    )
    parsed, made, moment, later = asl.run(
        imported(body), {"raw": '{"a": [1, null], "b": "x"}'}
    )
    assert parsed == {"a": [1, None], "b": "x"}
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", made
    )
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", moment)
    assert later is True


def test_datetimes_evaluate():
    body = (
        'at: str = input["at"]\n'
        "return [datetime.fromisoformat(at).timestamp(), "
        'str(datetime.fromtimestamp(input["t"])), datetime.now().timestamp() > 1e9]'
    )
    moment = "2026-09-15T13:43:06.735Z"
    seconds, text, later = asl.run(
        imported(body), {"at": moment, "t": datetime.fromisoformat(moment).timestamp()}
    )
    assert seconds == datetime.fromisoformat(moment).timestamp()
    assert text == moment
    assert later is True


@pytest.mark.parametrize(
    "body, value",
    [
        ("return timedelta(minutes=90).total_seconds()", 5400),
        ("return timedelta(seconds=1.5).total_seconds()", 1.5),
        ("return timedelta(minutes=-1).total_seconds()", -60),
        ("return timedelta().total_seconds()", 0),
    ],
)
def test_a_written_timedelta_is_seconds_of_its_own(body, value):
    """total_seconds() of a timedelta written in the source is the number
    itself: the units are added up while it compiles."""
    assert imported(body)["States"]["return"]["Output"] == value


def test_a_moment_moved_by_a_timedelta_is_read_once():
    """$millis() gives another value on every call, so a moment built on it is
    bound once where the code would write it twice."""
    body = "return (datetime.now() + timedelta(seconds=1)).timestamp() % 60"
    assert imported(body)["States"]["return"]["Output"] == (
        "{% ($v := (($millis() + 1000) / 1000); $v - 60 * $floor($v / 60)) %}"
    )


def test_datetime_arithmetic_evaluates():
    """A moment moved by a timedelta, and the seconds between two moments,
    hold the values CPython computes for them."""
    body = (
        'at: str = input["at"]\n'
        'other: str = input["other"]\n'
        "return [str(datetime.fromisoformat(at) + timedelta(days=1, hours=-2)), "
        "(datetime.fromisoformat(at) - datetime.fromisoformat(other)).total_seconds(), "
        "(datetime.fromisoformat(other) - datetime.fromisoformat(at)).total_seconds()]"
    )
    moment, other = "2026-09-15T13:43:06.735Z", "2026-09-16T01:00:00.500Z"
    at, later = datetime.fromisoformat(moment), datetime.fromisoformat(other)
    text, back, forward = asl.run(imported(body), {"at": moment, "other": other})
    moved = at + timedelta(days=1, hours=-2)
    assert text == moved.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert back == (at - later).total_seconds()
    assert forward == (later - at).total_seconds()
    # A moment taken from a later one is a negative number of seconds.
    assert back < 0


FORMAT = "%Y-%y-%m-%d %H:%M:%S %j %%"


@pytest.mark.parametrize("seconds", [1789479786.735, 0, 946684800])
def test_strftime_writes_what_cpython_writes(seconds):
    """Each directive the picture string covers, on a time written in the
    source so both sides read the same moment."""
    body = f'return datetime.fromtimestamp({seconds}).strftime("{FORMAT}")'
    written = asl.run(imported(body), {})
    assert written == datetime.fromtimestamp(seconds, UTC).strftime(FORMAT)


def test_strftime_of_the_moment_runs():
    """$now() takes the picture too, so the moment needs no milliseconds of
    its own."""
    written = asl.run(imported('return datetime.now().strftime("%Y")'), {})
    assert re.fullmatch(r"\d{4}", written)


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "return datetime.now() + timedelta(hours=1)",
            (
                "datetime.now() + timedelta(hours=1) is a datetime object, not "
                "JSON; write str(dt), dt.timestamp() or wait(until=dt)"
            ),
        ),
        (
            "return datetime.now() - timedelta(hours=1) < datetime.now()",
            (
                "datetime.now() - timedelta(hours=1) is a datetime object, not "
                "JSON; write str(dt), dt.timestamp() or wait(until=dt)"
            ),
        ),
        (
            'return str(datetime.now() + timedelta(hours=input["h"]))',
            (
                "the units of timedelta are numbers written here: "
                "timedelta(hours=1); for a span the input carries, write "
                "datetime.fromtimestamp(dt.timestamp() + seconds)"
            ),
        ),
        (
            "return str(datetime.now() + timedelta(microseconds=5))",
            (
                "Step Functions keeps time to the millisecond, so timedelta "
                "takes no microseconds here"
            ),
        ),
        (
            "return str(datetime.now() + timedelta(seconds=0.0005))",
            (
                "Step Functions keeps time to the millisecond, and "
                "timedelta(seconds=0.0005) is a fraction of one"
            ),
        ),
        (
            "return str(datetime.now() + timedelta(1))",
            "timedelta takes its units by name here: timedelta(hours=1)",
        ),
        (
            "return str(datetime.now() + timedelta(fortnights=1))",
            (
                "timedelta takes weeks, days, hours, minutes, seconds and "
                "milliseconds here: timedelta(hours=1)"
            ),
        ),
        (
            "return str(datetime.now() + timedelta(days=1e9))",
            "timedelta(days=1000000000.0) is longer than a timedelta holds",
        ),
        (
            "return timedelta(hours=1)",
            (
                "timedelta(hours=1) is a timedelta object, not JSON; add it to a "
                "datetime or take it from one, or write "
                "timedelta(hours=1).total_seconds()"
            ),
        ),
        (
            'return (datetime.now() - datetime.fromisoformat(input["at"])).days',
            (
                "a timedelta is seconds through total_seconds() here: "
                "(datetime.now() - datetime.fromisoformat(input['at']))"
                ".total_seconds()"
            ),
        ),
        (
            "return timedelta(hours=1).seconds",
            (
                "a timedelta is seconds through total_seconds() here: "
                "timedelta(hours=1).total_seconds()"
            ),
        ),
        (
            'return input["span"].total_seconds()',
            (
                "total_seconds() is written (dt - dt2).total_seconds() or "
                "timedelta(hours=1).total_seconds()"
            ),
        ),
        (
            "return (datetime.now() - 1).total_seconds()",
            (
                "total_seconds() is written (dt - dt2).total_seconds() or "
                "timedelta(hours=1).total_seconds()"
            ),
        ),
        (
            "return timedelta(hours=1).total_seconds(2)",
            (
                "total_seconds() is written (dt - dt2).total_seconds() or "
                "timedelta(hours=1).total_seconds()"
            ),
        ),
        (
            "return str(datetime.now() * timedelta(hours=1))",
            (
                "datetime.now() is a datetime object, not JSON; write "
                "str(datetime.now()) or datetime.now().timestamp()"
            ),
        ),
    ],
)
def test_timedelta_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(IMPORTS + source(body))
    assert raised.value.message == message


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return datetime.now().strftime("%A %d")',
            (
                "strftime() takes %Y, %y, %m, %d, %H, %M, %S, %j and %% here, not %A; jsonata() reaches "
                "the picture strings $fromMillis takes"
            ),
        ),
        (
            'return datetime.now().strftime("%f")',
            (
                "strftime() takes %Y, %y, %m, %d, %H, %M, %S, %j and %% here, not %f, which is "
                "microseconds where Step Functions keeps time to the "
                "millisecond; jsonata() reaches the picture strings "
                "$fromMillis takes"
            ),
        ),
        (
            'return datetime.now().strftime("%z")',
            (
                "strftime() takes %Y, %y, %m, %d, %H, %M, %S, %j and %% here, not %z, whose offset is "
                "empty in Python for a datetime with no time zone; jsonata() "
                "reaches the picture strings $fromMillis takes"
            ),
        ),
        (
            'return datetime.now().strftime("%Z")',
            (
                "strftime() takes %Y, %y, %m, %d, %H, %M, %S, %j and %% here, not %Z, whose name is "
                "empty in Python for a datetime with no time zone; jsonata() "
                "reaches the picture strings $fromMillis takes"
            ),
        ),
        (
            'return datetime.now().strftime("%Y %")',
            (
                "strftime() takes %Y, %y, %m, %d, %H, %M, %S, %j and %% here, not the % at the end; "
                "jsonata() reaches the picture strings $fromMillis takes"
            ),
        ),
        (
            'return datetime.now().strftime(input["fmt"])',
            (
                "the format of strftime() is a literal string, as the picture "
                "string it becomes is built here: "
                'datetime.now().strftime("%Y-%m-%d")'
            ),
        ),
        (
            'return datetime.now().strftime("%Y", "x")',
            'strftime() is written datetime.now().strftime("%Y-%m-%d")',
        ),
        (
            "return datetime.now().strftime()",
            'strftime() is written datetime.now().strftime("%Y-%m-%d")',
        ),
        (
            'return input["at"].strftime("%Y")',
            'strftime() is written datetime.now().strftime("%Y-%m-%d")',
        ),
    ],
)
def test_strftime_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(IMPORTS + source(body))
    assert raised.value.message == message


def test_timedelta_without_its_import():
    body = "return str(datetime.now() + timedelta(hours=1))"
    with pytest.raises(CompileError) as raised:
        compile_source("from datetime import datetime\n" + source(body))
    assert raised.value.message == (
        "timedelta is not imported; write from datetime import timedelta"
    )


def test_a_function_named_timedelta_is_not_the_datetime_one():
    """A name the module defines is the function it defines, not an import it
    does not have."""
    body = "return str(datetime.now() + timedelta(hours=1))"
    defined = "\n\ndef timedelta(hours):\n    return hours\n"
    with pytest.raises(CompileError) as raised:
        compile_source("from datetime import datetime\n" + source(body) + defined)
    assert raised.value.message == (
        "datetime.now() is a datetime object, not JSON; write "
        "str(datetime.now()) or datetime.now().timestamp()"
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
            "return datetime.now()",
            "datetime.now() is a datetime object, not JSON; write str(datetime.now())",
        ),
        ("return str(datetime.now(None))", "datetime.now() takes no arguments here"),
        ("return time.time(1)", "time.time() takes no arguments"),
        (
            "return datetime.fromisoformat(1).timestamp()",
            "1 is a number; datetime.fromisoformat() reads a timestamp string",
        ),
        (
            'at: str = input["at"]\nreturn str(datetime.fromtimestamp(at))',
            "at is a string, and datetime.fromtimestamp() takes numbers",
        ),
        (
            'return datetime.fromisoformat(input["at"])',
            (
                "write str(datetime.fromisoformat(text)) or "
                "datetime.fromisoformat(text).timestamp()"
            ),
        ),
        (
            "return str(datetime.fromisoformat())",
            "datetime.fromisoformat(text) takes one argument here",
        ),
        (
            'return input["at"].timestamp()',
            "timestamp() is written datetime.fromisoformat(text).timestamp()",
        ),
        (
            "return uuid.uuid4().timestamp()",
            "timestamp() is written datetime.fromisoformat(text).timestamp()",
        ),
        (
            "return datetime.now().timestamp(1)",
            "timestamp() is written datetime.fromisoformat(text).timestamp()",
        ),
        (
            "return datetime.fromisoformat().timestamp()",
            "write datetime.fromisoformat(text)",
        ),
        (
            'raw: str | None = input["raw"]\nreturn json.loads(raw)',
            "raw may be null | string; narrow it first",
        ),
        # Rejected spellings whose messages name what to write instead.
        (
            'return json.dumps(input["a"])',
            (
                "json.dumps() is not supported; write str(x), the JSON text of a "
                "dict or a list"
            ),
        ),
        (
            'return math.pow(input["n"], 2)',
            "math.pow() is not supported; write x ** y",
        ),
        (
            'return os.path.basename(input["key"])',
            'os.path.basename() is not supported; write path.split("/")[-1]',
        ),
    ],
)
def test_module_function_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(IMPORTS + source(body))
    assert message in raised.value.message


@pytest.mark.parametrize(
    "body, message",
    [
        ('return json.loads(input["raw"])', "json is not imported; write import json"),
        (
            'return base64.b64encode(input["s"].encode()).decode()',
            "base64 is not imported; write import base64",
        ),
        (
            'return urllib.parse.unquote(input["s"])',
            "urllib.parse is not imported; write import urllib.parse",
        ),
        (
            "return str(datetime.now())",
            "datetime is not imported; write from datetime import datetime",
        ),
        ("return time.time()", "time is not imported; write import time"),
        (
            'return datetime.fromisoformat(input["at"]).timestamp()',
            "datetime is not imported; write from datetime import datetime",
        ),
    ],
)
def test_module_function_without_import(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(body))
    assert raised.value.message == message


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
        "merge_val": f"{{% {unpacked(INPUT)} %}}",
    }
    assert asl.run(compiled, {"xs": [1, 2]}) == [[1, 2], [1, 2], 2, [2, 4], 1]


def test_a_variable_named_error_does_not_hide_the_error_function():
    body = 'error = input["error"]\nb: float = input["b"]\nreturn [error, 10 / b]'
    compiled = definition(body)
    assert (
        compiled["States"]["error"]["Assign"]["error_val"] == f"{{% {INPUT}.error %}}"
    )
    assert compiled["States"]["return"]["Output"][1] == (
        "{% $b = 0 ? $error('division by zero') : 10 / $b %}"
    )
    assert asl.run(compiled, {"error": "e", "b": 2}) == ["e", 5]


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
        ("return s.split()", "$trim($s) = '' ? [] : $split($trim($s), ' ')"),
        ('return s.split("/")[-1]', "$split($s, '/')[-1]"),
        ('return s.replace(".", "-")', "$replace($s, '.', '-')"),
        ('return s.replace("a", "b", 2)', "$replace($s, 'a', 'b', 2)"),
        ("return s.lower()", "$lowercase($s)"),
        ("return s.strip()", "$replace($s, /^\\s+|\\s+$/, '')"),
        ('return "id-" + s.upper()', "'id-' & $uppercase($s)"),
        ('return input["s"].upper()', f"$uppercase({INPUT}.s)"),
        ('return ", ".join(parts)', "$join($parts, ', ')"),
    ],
)
def test_string_methods(body, code):
    assert output(STRINGS + body) == "{% " + code + " %}"


@pytest.mark.parametrize("text", ["  a  b\tc  ", "abc", "", "   ", "\n x \n"])
def test_strip_removes_the_whitespace_at_the_ends_only(text):
    """$trim would also make every run of whitespace inside the text one space."""
    compiled = definition(STRINGS + "return s.strip()")
    assert asl.run(compiled, {"s": text, "parts": []}) == text.strip()


@pytest.mark.parametrize("text", ["a b  c", " a ", "", "   ", "\n\t"])
def test_split_at_whitespace_gives_the_same_parts_as_python(text):
    """$split reads text that trims to "" as one empty part, Python as none."""
    compiled = definition(STRINGS + "return s.split()")
    assert asl.run(compiled, {"s": text, "parts": []}) == text.split()


def test_no_parts_stay_a_list_where_they_are_read():
    """An empty array is a value of its own, not a sequence that disappears."""
    body = 'return [s.split(), {"p": s.split()}, len(s.split())]'
    compiled = definition(STRINGS + body)
    assert asl.run(compiled, {"s": "   ", "parts": []}) == [[], {"p": []}, 0]


def test_a_fill_read_at_run_time_is_passed_through():
    """Only what is written in the source is known when the file is compiled."""
    body = 'n: float = input["n"]\nf: str = input["f"]\nreturn s.ljust(n, f)'
    assert output(STRINGS + body) == "{% $pad($s, $max([$n, 0]), $f) %}"


@pytest.mark.parametrize("method", ["ljust", "rjust"])
def test_a_width_below_zero_read_at_run_time_leaves_the_text(method):
    body = f'n: float = input["n"]\nreturn [s.{method}(n), s.{method}(n, "0")]'
    compiled = definition(STRINGS + body)
    assert asl.run(compiled, {"s": "ab", "n": -3, "parts": []}) == ["ab", "ab"]
    assert asl.run(compiled, {"s": "ab", "n": 4, "parts": []}) == [
        getattr("ab", method)(4),
        getattr("ab", method)(4, "0"),
    ]


def test_string_methods_evaluate():
    body = (
        'return [s.split("/"), s.split(), s.replace("/", "-", 1), s.lower(), '
        '"+".join(parts), [p.upper() for p in s.split()], s.strip()]'
    )
    execution_input = {"s": " a/B/c\td ", "parts": ["x", "y"]}
    assert asl.run(definition(STRINGS + body), execution_input) == [
        [" a", "B", "c\td "],
        ["a/B/c", "d"],
        " a-B/c\td ",
        " a/b/c\td ",
        "x+y",
        ["A/B/C", "D"],
        "a/B/c\td",
    ]


@pytest.mark.parametrize(
    "body, message",
    [
        ('return parts.split("/")', "parts is an array; split() is a string method"),
        ("return s.split(1)", "1 is a number; split() splits at a string"),
        ('return s.split("/", 1)', "split() takes no maximum"),
        ('return s.split(sep="/")', "split() takes no keyword arguments here"),
        ('return s.replace("a")', "replace() is written s.replace(old, new)"),
        ('return s.replace("a", "b", "c")', "the count of replace() takes numbers"),
        (
            'return s.split("")',
            (
                "split() splits at one character or more; list(s) reads the "
                "text as its characters"
            ),
        ),
        (
            'return s.replace("", "x")',
            (
                "replace() replaces one character or more; Step Functions fails "
                "on an empty pattern"
            ),
        ),
        (
            'return s.replace("a", "b", -1)',
            (
                "the count of replace() is a whole number of 0 or more; leave "
                "it out to replace every occurrence"
            ),
        ),
        ('return s.replace("a", "b", 2.5)', "the count of replace() is a whole number"),
        ("return s.ljust(-5)", "the width of ljust() is a whole number of 0 or more"),
        ("return s.rjust(6.5)", "the width of rjust() is a whole number of 0 or more"),
        ('return s.ljust(6, "ab")', "ljust() fills with one character"),
        ('return s.rjust(6, "")', "rjust() fills with one character"),
        ("return s.lower(1)", "lower() is written s.lower()"),
        ('return s.strip("x")', "strip() is written s.strip()"),
        (
            'n: float = input["n"]\nreturn s.join(n)',
            "n is a number; join() takes a list of strings",
        ),
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
        ("return s[-3:4]", "$substring($s, -3, $min([7 - $length($s), 4]))"),
        ("return s[-3:-1]", "$substring($s, -3, $min([2, $length($s) - 1]))"),
        ("return s[-1:-3]", "$substring($s, -1, -2)"),
        ("return s[i:-1]", "$substring($s, $i, $length($s) - (1 + $i))"),
        ("return s[-2:i]", "$substring($s, -2, $min([$i + 2 - $length($s), $i]))"),
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


def test_a_start_back_past_the_beginning_takes_what_python_takes():
    body = SLICES + "return [s[-10:-8], s[-10:2], s[-i:-1], s[-10:i], s[-3:-5]]"
    compiled = definition(body)
    for text in ["", "a", "héllo", "abcdefghi", "abcdefghij", "abcdefghijkl"]:
        for i in [1, 4, 12]:
            python = [text[-10:-8], text[-10:2], text[-i:-1], text[-10:i], text[-3:-5]]
            values = {"s": text, "xs": [], "i": i}
            assert asl.run(compiled, values) == python, (text, i)


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
            "xs is an array; keys() is a dict method",
        ),
        ("return d.keys(1)", "keys() is written d.keys()"),
        ("return d.get()", "get() is written d.get(key) or d.get(key, default)"),
        ('return d.get("a", default=1)', "get() is written d.get(key)"),
        ("return d.get(1)", "1 is a number; keys are strings"),
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


NUMBERS = 'xs: list[float] = input["xs"]\na: float = input["a"]\n'
MATH = "import math\nimport random\n"


def numbers_definition(body: str) -> dict:
    (compiled,) = compile_source(MATH + source(NUMBERS + body)).values()
    return compiled


@pytest.mark.parametrize(
    "body, code",
    [
        ("return abs(a)", "$abs($a)"),
        ("return round(a)", "$round($a)"),
        ("return round(a, 2)", "$round($a, 2)"),
        ("return math.floor(a)", "$floor($a)"),
        ("return math.ceil(a)", "$ceil($a)"),
        ("return math.sqrt(a)", "$sqrt($a)"),
        ("return random.random()", "$random()"),
        ("return sum(xs)", "$sum($xs)"),
        ('return max(input["xs"])', f"$max({INPUT}.xs)"),
        ("return min(a, xs[0], 3)", "$min([$a, $xs[0], 3])"),
        ("return sum(xs) / len(xs)", "$average($xs)"),
        (
            'ys: list = input["ys"]\nreturn sum(xs) / len(ys)',
            "$count($ys) = 0 ? $error('division by zero') : $sum($xs) / $count($ys)",
        ),
    ],
)
def test_number_functions(body, code):
    output = numbers_definition(body)["States"]["return"]["Output"]
    assert output == "{% " + code + " %}"


def test_number_functions_evaluate():
    body = (
        "return [abs(a), round(a), round(a, 1), math.floor(a), math.ceil(a), "
        "math.sqrt(xs[0]), sum(xs), max(xs), min(a, xs[0]), sum(xs) / len(xs)]"
    )
    execution_input = {"xs": [4, 1, 2.5], "a": -2.25}
    assert asl.run(numbers_definition(body), execution_input) == [
        2.25,
        -2,
        -2.2,
        -3,
        -2,
        2,
        7.5,
        4,
        -2.25,
        2.5,
    ]


CHANGING = "import random\nimport uuid\nfrom sfnx import jsonata\n"


def changing_definition(body: str) -> dict:
    (compiled,) = compile_source(CHANGING + source(NUMBERS + body)).values()
    return compiled


@pytest.mark.parametrize(
    "body, code",
    [
        # An operand written twice is bound once when it changes on evaluation.
        (
            "return random.random() % 0.5",
            "($v := $random(); $v - 0.5 * $floor($v / 0.5))",
        ),
        (
            "return a % random.random()",
            (
                "($v := $random(); $v = 0 ? $error('division by zero') : "
                "$a - $v * $floor($a / $v))"
            ),
        ),
        (
            "return (random.random() + 1) % 2",
            "($v := ($random() + 1); $v - 2 * $floor($v / 2))",
        ),
        # The expression jsonata() takes is not parsed, so it counts as
        # changing however the call is written, or bound to a name first, and
        # even when it calls nothing.
        (
            "return jsonata('$random()') % 2",
            "($v := ($random()); $v - 2 * $floor($v / 2))",
        ),
        (
            "return jsonata('$random ()') % 2",
            "($v := ($random ()); $v - 2 * $floor($v / 2))",
        ),
        (
            "return jsonata('($f := $random; $f())') % 2",
            "($v := (($f := $random; $f())); $v - 2 * $floor($v / 2))",
        ),
        ("return jsonata('1 + 1') % 2", "($v := (1 + 1); $v - 2 * $floor($v / 2))"),
        (
            "return jsonata('$random() + $n', n=a) % 2",
            "($v := ($n := $a; $random() + $n); $v - 2 * $floor($v / 2))",
        ),
        # The operand written once is left where it is, so it is evaluated
        # only when the first one is falsy.
        ("return a or jsonata('$random ()')", "$boolean($a) ? $a : $random ()"),
        (
            "return 0.2 < random.random() < 0.8",
            "($v := $random(); 0.2 < $v and $v < 0.8)",
        ),
        # The comparisons before it stay as they are, and c is evaluated only
        # when a < b holds.
        (
            "return 0 <= a < random.random() < 1",
            "0 <= $a and ($v := $random(); $a < $v and $v < 1)",
        ),
        (
            "return 0 <= random.random() < random.random() + 1 < 9",
            "($v := $random(); 0 <= $v and ($v_2 := ($random() + 1); $v < $v_2 and $v_2 < 9))",
        ),
        ("return random.random() or a", "($v := $random(); $boolean($v) ? $v : $a)"),
        ("return random.random() and a", "($v := $random(); $boolean($v) ? $a : $v)"),
        (
            "return str(uuid.uuid4()) is not None",
            "($v := $uuid(); $exists($v) and $v != null)",
        ),
        (
            "return str(uuid.uuid4()) is None",
            "$not(($v := $uuid(); $exists($v) and $v != null))",
        ),
        (
            'd: dict = input["d"]\nreturn d.get(str(uuid.uuid4()), a)',
            "($v := $lookup($d, $uuid()); $exists($v) ? $v : $a)",
        ),
        # The variable hides nothing the block reads: not a variable, and not
        # the parameter of a comprehension, which is not counted as one.
        (
            'v: float = input["v"]\nreturn random.random() % v',
            (
                "($v_2 := $random(); $v = 0 ? $error('division by zero') : "
                "$v_2 - $v * $floor($v_2 / $v))"
            ),
        ),
        (
            'v: float = input["v"]\nreturn random.random() or v',
            "($v_2 := $random(); $boolean($v_2) ? $v_2 : $v)",
        ),
        # A name the expression written in jsonata() reads is one the variable
        # must not hide, though the program never declared it.
        (
            'v: float = input["v"]\nreturn jsonata("$v + 1") % 2',
            "($v_2 := ($v + 1); $v_2 - 2 * $floor($v_2 / 2))",
        ),
        (
            "return [x % random.random() for x in xs]",
            (
                "[$map($xs, function($x) { ($v := $random(); $v = 0 ? "
                "$error('division by zero') : $x - $v * $floor($x / $v)) })]"
            ),
        ),
        (
            "return [random.random() % 2 for v in xs]",
            "[$map($xs, function($v) { ($v_2 := $random(); $v_2 - 2 * $floor($v_2 / 2)) })]",
        ),
        # A value that holds still is written as it is.
        ("return a % 3", "$a - 3 * $floor($a / 3)"),
        ("return 0 <= a < xs[0] < 1", "0 <= $a and $a < $xs[0] and $xs[0] < 1"),
    ],
)
def test_a_value_that_changes_on_evaluation_is_bound_once(body, code):
    output = changing_definition(body)["States"]["return"]["Output"]
    assert output == "{% " + code + " %}"


def test_a_written_expression_is_read_once():
    """The expression jsonata() takes is read once wherever the code would
    write it twice, and the variable holding it hides no name it reads."""
    body = (
        'v: float = input["v"]\n'
        'return [jsonata("$v + 10") % jsonata("$v - 4"), '
        'jsonata("$uuid ()") != jsonata("$uuid ()")]'
    )
    remainder, apart = asl.run(
        changing_definition(body), {"xs": [], "a": 0.0, "v": 7.0}
    )
    assert remainder == (7 + 10) % (7 - 4)
    # Two expressions written apart are read apart.
    assert apart is True


def test_bound_values_evaluate():
    body = (
        "return [random.random() % 0.5, 0 <= random.random() < 1, "
        'str(uuid.uuid4()) or "none", str(uuid.uuid4()) is None, '
        '{"a": random.random()}.get("a", 2) < 1, [str(uuid.uuid4())][0] == a]'
    )
    remainder, inside, chosen, missing, present, same = asl.run(
        changing_definition(body), {"xs": [], "a": "x"}
    )
    # Evaluated twice, a random number is in [0, 0.5) only by chance.
    assert 0 <= remainder < 0.5
    assert inside is True
    assert len(chosen) == 36
    assert (missing, present, same) == (False, True, False)


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'return max(["a"])',
            "the items of ['a'] are string; max() takes numbers here",
        ),
        ("return sum(xs, 1)", "sum() is written sum(xs)"),
        ("return sum(1)", "1 is a number; sum() takes a list of numbers"),
        ("return round()", "round() is written round(x) or round(x, digits)"),
        ('return abs("x")', "'x' is a string, and abs() takes numbers"),
        ("return math.sqrt()", "math.sqrt() takes one number"),
        ("return random.random(1)", "random.random() takes no arguments"),
    ],
)
def test_number_function_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        numbers_definition(body)
    assert message in raised.value.message


def test_math_without_import():
    with pytest.raises(CompileError) as raised:
        compile_source(source("return math.ceil(1.5)"))
    assert raised.value.message == "math is not imported; write import math"


LISTS = (
    's: str = input["s"]\nxs: list[float] = input["xs"]\nd: dict = input["d"]\n'
    'n: float = input["n"]\np: str = input["p"]\nds: list[dict] = input["ds"]\n'
)


@pytest.mark.parametrize(
    "body, code",
    [
        ("return sorted(xs)", "$sort($xs)"),
        ("return sorted(xs, reverse=True)", "$reverse($sort($xs))"),
        (
            'return sorted(ds, key=lambda item: item["p"])',
            "$sort($ds, function($a, $b) { $a.p > $b.p })",
        ),
        (
            'return sorted(ds, key=lambda item: item["p"], reverse=True)',
            "$sort($ds, function($a, $b) { $a.p < $b.p })",
        ),
        (
            'return max(ds, key=lambda item: item["p"])',
            "$sort($ds, function($a, $b) { $a.p > $b.p })[-1]",
        ),
        (
            'return min(ds, key=lambda item: item["p"])',
            "$sort($ds, function($a, $b) { $a.p > $b.p })[0]",
        ),
        (
            'return sorted(ds, key=lambda item: item["p"] * n)',
            "$sort($ds, function($a, $b) { $a.p * $n > $b.p * $n })",
        ),
        (
            'return max(ds[0], ds[1], key=lambda item: item["p"])',
            "$sort([$ds[0], $ds[1]], function($a, $b) { $a.p > $b.p })[-1]",
        ),
        ("return sorted(d)", "$sort([$keys($d)])"),
        ("return list(reversed(xs))", "$reverse($xs)"),
        ("return xs[::-1]", "$reverse($xs)"),
        ("return s[::-1]", "$join($reverse($split($s, '')), '')"),
        ("return list(range(n))", "[0..$n - 1]"),
        ("return list(range(2, 5))", "[2..4]"),
        ("return list(range(0, n, 3))", "[$range(0, $n - 1, 3)]"),
        ("return list(range(n, 0, -2))", "[$range($n, 1, -2)]"),
        (
            "return [i * 2 for i in range(n)]",
            "[$map([0..$n - 1], function($i) { $i * 2 })]",
        ),
        ('return s.startswith("arn:")', "$substring($s, 0, 4) = 'arn:'"),
        ('return s.endswith(".json")', "$substring($s, -5, 5) = '.json'"),
        ("return s.startswith(p)", "$substring($s, 0, $length($p)) = $p"),
        (
            "return s.endswith(p)",
            "$substring($s, $length($s) - $length($p), $length($p)) = $p",
        ),
    ],
)
def test_list_and_string_functions(body, code):
    assert output(LISTS + body) == "{% " + code + " %}"


def test_list_and_string_functions_evaluate():
    body = LISTS + (
        "return [sorted(xs), sorted(d, reverse=True), list(reversed(xs)), s[::-1], "
        "list(range(n)), list(range(0)), list(range(0, 10, 3)), list(range(5, 0, -2)), "
        "list(range(3, 4, 5)), s.startswith(p), s.endswith(p), s.endswith(''), "
        '"lo".endswith("hello")]'
    )
    execution_input = {
        "s": "hello",
        "xs": [3, 1, 2],
        "d": {"b": 1, "a": 2},
        "n": 3,
        "p": "llo",
        "ds": [],
    }
    assert asl.run(definition(body), execution_input) == [
        [1, 2, 3],
        ["b", "a"],
        [2, 1, 3],
        "olleh",
        [0, 1, 2],
        [],
        [0, 3, 6, 9],
        [5, 3, 1],
        [3],
        False,
        True,
        True,
        False,
    ]


def test_a_key_orders_by_what_it_reads():
    """The items have keys of their own here: Step Functions leaves items with
    the same key in order, as Python does, and jsonata-python does not."""
    items = [{"p": 2, "id": "a"}, {"p": 1, "id": "b"}, {"p": 3, "id": "c"}]
    body = LISTS + (
        'return [sorted(ds, key=lambda x: x["p"]), '
        'sorted(ds, key=lambda x: x["p"], reverse=True), '
        'max(ds, key=lambda x: x["p"])["id"], min(ds, key=lambda x: x["p"])["id"]]'
    )
    execution_input = {"s": "", "xs": [], "d": {}, "n": 0, "p": "", "ds": items}
    assert asl.run(definition(body), execution_input) == [
        sorted(items, key=lambda x: x["p"]),
        sorted(items, key=lambda x: x["p"], reverse=True),
        max(items, key=lambda x: x["p"])["id"],
        min(items, key=lambda x: x["p"])["id"],
    ]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "return sorted(xs, key=abs)",
            "the key of sorted() is a lambda of one item",
        ),
        (
            "return sorted(xs, reverse=1)",
            "sorted() takes key= and reverse=True or reverse=False",
        ),
        (
            'return sorted(xs, key=lambda x, y: x["p"])',
            "the key of sorted() is a lambda of one item",
        ),
        (
            'ds: list[dict] = input["ds"]\nreturn sorted(ds, key=lambda d: d)',
            "the key of sorted() is object; JSONata orders numbers and strings",
        ),
        (
            'return max(xs, key=lambda x: x["p"], default=0)',
            'max() takes key=: max(xs, key=lambda x: x["price"])',
        ),
        (
            'ls: list[list] = input["ls"]\nreturn sorted(ls)',
            "the items of ls are array; sorted() orders numbers or strings",
        ),
        ("return sorted()", "sorted() takes one argument"),
        (
            "return sorted(n)",
            "n is a number; sorted() takes a dict, a list or a string",
        ),
        ("return reversed(n)", "n is a number; reversed() takes a dict"),
        ("return reversed(xs, s)", "reversed() takes one argument"),
        ("return d[::-1]", "d is an object; slices take lists and strings"),
        ("return xs[::2]", "a slice takes no step other than xs[::-1]"),
        (
            "return s.startswith(1)",
            "1 is a number; startswith() compares with a string",
        ),
        ('return s.endswith("a", 1)', "endswith() is written s.endswith(suffix)"),
    ],
)
def test_list_and_string_function_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(LISTS + body))
    assert message in raised.value.message


@pytest.mark.parametrize(
    "body, code",
    [
        ('return d.get("a")', "$exists($d.a) ? $d.a : null"),
        ('return d.get("a", 0)', "$exists($d.a) ? $d.a : 0"),
        (
            'k: str = input["k"]\nreturn d.get(k, "none")',
            "$exists($lookup($d, $k)) ? $lookup($d, $k) : 'none'",
        ),
        (
            'return input.get("coupon")',
            f"$exists({INPUT}.coupon) ? {INPUT}.coupon : null",
        ),
        ('return flat.get("x", 0) + 1', "($exists($flat.x) ? $flat.x : 0) + 1"),
    ],
)
def test_get(body, code):
    assert output(DICTS + body) == "{% " + code + " %}"


def test_get_evaluates():
    body = DICTS + (
        'k: str = input["k"]\n'
        'return [d.get("a"), d.get("b", 0), d.get(k, "none"), d.get("n", 5), '
        'input.get("coupon"), flat.get("x", 0) + 1]'
    )
    execution_input = {
        "d": {"a": 1, "n": None},
        "k": "a",
        "flat": {"x": 2},
        "nested": {},
    }
    assert asl.run(definition(body), execution_input) == [1, 0, 1, None, None, 3]


MORE = (
    "import base64\nimport hashlib\nimport itertools\nimport urllib.parse\n",
    's: str = input["s"]\nxs: list[float] = input["xs"]\nys: list = input["ys"]\n',
)


def more_definition(body: str) -> dict:
    (compiled,) = compile_source(MORE[0] + source(MORE[1] + body)).values()
    return compiled


@pytest.mark.parametrize(
    "body, code",
    [
        ('return s.ljust(5, "0")', "$pad($s, 5, '0')"),
        ("return s.rjust(4)", "$pad($s, -4)"),
        (
            'n: float = input["n"]\nreturn s.rjust(n, "0")',
            "$pad($s, -$max([$n, 0]), '0')",
        ),
        ("return list(set(xs))", "$distinct($xs)"),
        ("return sorted(set(xs))", "$sort($distinct($xs))"),
        ("return list(zip(xs, ys))", "$zip($xs, $ys)"),
        ("return hashlib.sha256(s.encode()).hexdigest()", "$hash($s, 'SHA-256')"),
        ("return hashlib.md5(s.encode()).hexdigest()", "$hash($s, 'MD5')"),
        ("return base64.b64encode(s.encode()).decode()", "$base64encode($s)"),
        ("return base64.b64decode(s).decode()", "$base64decode($s)"),
        (
            "return urllib.parse.unquote(s)",
            "$decodeUrlComponent($replace($s, '+', '%2B'))",
        ),
        ("return urllib.parse.unquote_plus(s)", "$decodeUrlComponent($s)"),
        ("return list(itertools.batched(xs, 2))", "[$partition($xs, 2)]"),
    ],
)
def test_more_functions(body, code):
    assert more_definition(body)["States"]["return"]["Output"] == "{% " + code + " %}"


def test_unquote_imported_by_name():
    preamble = "from urllib.parse import unquote\n"
    body = 's: str = input["s"]\nreturn unquote(s)'
    (compiled,) = compile_source(preamble + source(body)).values()
    assert compiled["States"]["return"]["Output"] == (
        "{% $decodeUrlComponent($replace($s, '+', '%2B')) %}"
    )


@pytest.mark.parametrize("text", ["ab", "日本 a/b", "", "a+b"])
def test_base64_and_unquote_agree_with_python(text):
    """The Base64 text is of the UTF-8 bytes, as .encode() gives them, and
    %XX escapes are read as UTF-8; unquote() keeps a + and unquote_plus()
    reads it as a space, as in Python."""
    body = (
        "return [base64.b64encode(s.encode()).decode(), "
        'base64.b64decode(input["b"]).decode(), urllib.parse.unquote(input["q"]), '
        'urllib.parse.unquote_plus(input["q"])]'
    )
    encoded = base64.b64encode(text.encode()).decode()
    quoted = urllib.parse.quote(text) + "+%2B"
    execution_input = {"s": text, "b": encoded, "q": quoted, "xs": [], "ys": []}
    assert asl.run(more_definition(body), execution_input) == [
        encoded,
        text,
        urllib.parse.unquote(quoted),
        urllib.parse.unquote_plus(quoted),
    ]


def test_more_functions_evaluate():
    body = (
        'return [s.ljust(5, "0"), s.rjust(5, "0"), list(set(xs)), list(zip(xs, ys)), '
        "hashlib.sha1(s.encode()).hexdigest(), list(itertools.batched(xs, 3)), "
        "list(itertools.batched(ys, 5)), list(itertools.batched([], 2))]"
    )
    execution_input = {"s": "ab", "xs": [3, 1, 3, 2], "ys": [[1], [2]]}
    assert asl.run(more_definition(body), execution_input) == [
        "ab000",
        "000ab",
        [3, 1, 2],
        [[3, [1]], [1, [2]]],
        "da23614e02469a0d7c7bd1bdab5c9c474b1904dc",
        [[3, 1, 3], [2]],
        [[[1], [2]]],
        [],
    ]


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "return set(xs)",
            "JSON has lists only; keep each item once with list(set(items))",
        ),
        ("return zip(xs, ys)", "zip() is a list here only as list(zip(a, b))"),
        ("return list(zip(1, xs))", "1 is a number; zip() takes lists"),
        ("return list(zip(xs, ys, strict=True))", "zip() takes no keyword arguments"),
        (
            "return itertools.batched(xs, 2)",
            "itertools.batched() is a list here only as list(itertools.batched(items, n))",
        ),
        (
            "return hashlib.sha256(s.encode())",
            "hashlib.sha256() is a hash object, not JSON; write hashlib.sha256(s.encode()).hexdigest()",
        ),
        (
            "return hashlib.sha256(s).hexdigest()",
            "hexdigest() is written hashlib.sha256(s.encode()).hexdigest()",
        ),
        ("return s.hexdigest(1)", "hexdigest() is written hashlib.sha256"),
        (
            "return base64.b64encode(s.encode())",
            "base64.b64encode() is bytes, not JSON; write base64.b64encode(s.encode()).decode()",
        ),
        (
            "return base64.b64decode(s)",
            "base64.b64decode() is bytes, not JSON; write base64.b64decode(s).decode()",
        ),
        (
            "return base64.b64encode(s).decode()",
            "b64encode() is written base64.b64encode(s.encode()).decode()",
        ),
        (
            "return base64.b64encode(s.encode(), altchars=b'-_').decode()",
            "decode() is written base64.b64encode(s.encode()).decode() or base64.b64decode(s).decode()",
        ),
        (
            "return base64.b64decode(1).decode()",
            "1 is a number; b64decode() reads a string",
        ),
        (
            "return base64.b64encode((1).encode()).decode()",
            "1 is a number; encode() is a string method",
        ),
        (
            "return s.decode()",
            "decode() is written base64.b64encode(s.encode()).decode() or",
        ),
        (
            "return urllib.parse.unquote(s, encoding='latin-1')",
            "unquote() takes one string: unquote(s)",
        ),
        ("return urllib.parse.unquote(1)", "1 is a number; unquote() reads a string"),
        (
            "return urllib.parse.unquote_plus(s, 'x')",
            "unquote_plus() takes one string: unquote_plus(s)",
        ),
        ("return urllib.parse.quote(s)", "urllib.parse.quote() is not supported"),
        ('return s.ljust("a")', "the width of ljust() takes numbers"),
        ("return s.ljust(5, 1)", "1 is a number; ljust() fills with a string"),
    ],
)
def test_more_function_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        more_definition(body)
    assert message in raised.value.message


# 0 makes no batches at all, 1.5 batches of one item, and Python takes no float.
@pytest.mark.parametrize("size", ["0", "1.5", "-1", "2.0"])
def test_a_batch_size_that_makes_no_batches_is_rejected(size):
    with pytest.raises(CompileError) as raised:
        more_definition(f"return list(itertools.batched(xs, {size}))")
    assert raised.value.message == (
        "the size of itertools.batched() is a whole number of 1 or more, such as 10"
    )


def test_hashlib_without_import():
    with pytest.raises(CompileError) as raised:
        compile_source(source('return hashlib.sha256(input["s"].encode()).hexdigest()'))
    assert raised.value.message == "hashlib is not imported; write import hashlib"


@pytest.mark.parametrize(
    "identifiers, found",
    [
        (
            ["_tmp", "states", "count", "x"],
            {"_tmp": "tmp", "states": "states_val", "count": "count_val"},
        ),
        (["_tmp", "tmp", "__tmp"], {"__tmp": "tmp_2", "_tmp": "tmp_3"}),
        (["__", "_1"], {"__": "value", "_1": "value_1"}),
        (["_count", "count_val"], {"_count": "count_val_2"}),
    ],
)
def test_names_step_functions_would_not_take_are_renamed(identifiers, found):
    assert spellings(frozenset(identifiers)) == found


def test_renamed_variables_run():
    body = (
        '_tmp = input["a"]\nstates = 2\n_ = 3\n'
        "return [_tmp, states, _, [_x * 2 for _x in [_tmp]]]"
    )
    compiled = definition(body)
    assert compiled["States"]["_tmp"]["Assign"] == {
        "tmp": f"{{% {INPUT}.a %}}",
        "states_val": 2,
        "value": 3,
    }
    assert asl.run(compiled, {"a": 1}) == [1, 2, 3, [2]]
