import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from tests import asl, corpus

# The lists the quantifiers are read over: the items $boolean and Python can
# disagree on, and the pairs that decide a quantifier at the first item, at the
# second, or at neither.
LISTS = (
    [],
    [0],
    [1],
    [[]],
    [[0]],
    [[], []],
    [None],
    [""],
    ["0"],
    [{}],
    [{"a": 1}],
    [0, 1],
    [1, 0],
    [0, 0],
    [1, 1],
    [None, ""],
    [[], [0]],
)


def source(body: str) -> str:
    return (
        "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def definition(body: str) -> dict[str, object]:
    (compiled,) = compile_source(source(body)).values()
    return compiled


def randomized(body: str) -> dict[str, object]:
    """A machine that calls random.random(), compiled with the module imported."""
    (compiled,) = compile_source("import random\n" + source(body)).values()
    return compiled


def output(body: str) -> object:
    return definition(body)["States"]["return"]["Output"]


def run(body: str, execution_input: object) -> object:
    return asl.run(definition(body), execution_input)


def in_python(body: str, execution_input: object) -> object:
    namespace: dict[str, object] = {}
    exec(source(body), namespace)
    pay = namespace["pay"]
    assert callable(pay)
    return pay(execution_input)


@pytest.mark.parametrize(
    "body, code",
    [
        (
            'xs: list[bool] = input["xs"]\nreturn any(xs)',
            "$reduce($xs, function($a, $x) { $a ? true : $boolean($x) }, false)",
        ),
        (
            'xs: list[bool] = input["xs"]\nreturn all(xs)',
            "$reduce($xs, function($a, $x) { $a ? $boolean($x) : false }, true)",
        ),
        # An item of an unknown type is read as bool() reads it: a list is true
        # when it holds anything, whatever the items are.
        (
            'xs: list = input["xs"]\nreturn any(xs)',
            (
                "$reduce($xs, function($a, $x) { $a ? true : ($type($x) = 'array' ? "
                "$count($x) > 0 : $boolean($x)) }, false)"
            ),
        ),
        (
            'rs: list[dict] = input["rs"]\nreturn any(r["failed"] for r in rs)',
            (
                "$reduce($rs, function($a, $r) { $a ? true : ($v := $r.failed; "
                "$type($v) = 'array' ? $count($v) > 0 : $boolean($v)) }, false)"
            ),
        ),
        (
            'xs: list[float] = input["xs"]\nreturn any(x > 0 for x in xs)',
            "$reduce($xs, function($a, $x) { $a ? true : $x > 0 }, false)",
        ),
        (
            'xs: list[float] = input["xs"]\nreturn all(x > 0 for x in xs)',
            "$reduce($xs, function($a, $x) { $a ? $x > 0 : false }, true)",
        ),
        # An item the condition drops leaves the result as it is, which is the
        # value the quantifier starts from.
        (
            'xs: list[float] = input["xs"]\nreturn any(x > 0 for x in xs if x != 1)',
            (
                "$reduce($xs, function($a, $x) { $a ? true : ($x != 1 ? $x > 0 : false) "
                "}, false)"
            ),
        ),
        (
            'xs: list[float] = input["xs"]\nreturn all(x > 0 for x in xs if x != 1)',
            (
                "$reduce($xs, function($a, $x) { $a ? ($x != 1 ? $x > 0 : true) : false "
                "}, true)"
            ),
        ),
        (
            'xs: list[float] = input["xs"]\nreturn any(x > 0 for x in xs if x != 1 if x != 2)',
            (
                "$reduce($xs, function($a, $x) { $a ? true : ($x != 1 and $x != 2 ? "
                "$x > 0 : false) }, false)"
            ),
        ),
        # A dict gives its keys as a list: $keys of an empty one is nothing,
        # which $reduce would give back in the place of its initial value.
        (
            'd: dict = input["d"]\nreturn any(d)',
            "$reduce([$keys($d)], function($a, $x) { $a ? true : $boolean($x) }, false)",
        ),
        # A list comprehension is a list: it is built before it is read.
        (
            'xs: list[float] = input["xs"]\nreturn any([x > 0 for x in xs])',
            (
                "$reduce([$map($xs, function($x) { $x > 0 })], function($a, $x_2) { "
                "$a ? true : $boolean($x_2) }, false)"
            ),
        ),
        # The accumulator takes a name the function does not read otherwise.
        (
            'xs: list[bool] = input["xs"]\nreturn any(a for a in xs)',
            "$reduce($xs, function($a_2, $a) { $a_2 ? true : $boolean($a) }, false)",
        ),
        (
            'xs: list[bool] = input["xs"]\nreturn any(True for a in xs)',
            "$reduce($xs, function($a_2, $a) { $a_2 ? true : true }, false)",
        ),
    ],
)
def test_spelling(body, code):
    assert output(body) == "{% " + code + " %}"


@pytest.mark.parametrize("items", LISTS)
@pytest.mark.parametrize("name", ["any", "all"])
def test_the_result_is_the_one_python_gives(name, items):
    body = f'xs: list = input["xs"]\nreturn {name}(xs)'
    assert run(body, {"xs": items}) == in_python(body, {"xs": items})


@pytest.mark.parametrize("items", LISTS)
@pytest.mark.parametrize("name", ["any", "all"])
def test_a_generator_gives_what_python_gives(name, items):
    body = f'xs: list = input["xs"]\nreturn {name}(x for x in xs)'
    assert run(body, {"xs": items}) == in_python(body, {"xs": items})


@pytest.mark.parametrize("items", LISTS)
@pytest.mark.parametrize("name", ["any", "all"])
def test_a_condition_drops_items_as_python_drops_them(name, items):
    body = f'xs: list = input["xs"]\nreturn {name}(x for x in xs if x != 0)'
    assert run(body, {"xs": items}) == in_python(body, {"xs": items})


def test_the_keys_of_a_dict_are_what_is_read():
    """The value of an entry decides nothing, and several keys are several
    items: the brackets around $keys make a list of the keys, not of the one
    list it returns."""
    body = 'd: dict = input["d"]\nreturn any(d)'
    every = 'd: dict = input["d"]\nreturn all(d)'
    assert run(body, {"d": {"": 1}}) is False
    assert run(body, {"d": {"a": 0}}) is True
    assert run(body, {"d": {}}) is False
    assert run(every, {"d": {"a": 1, "": 1}}) is False
    assert run(every, {"d": {"a": 1, "b": 2}}) is True
    for value in ({"": 1}, {"a": 0}, {}, {"a": 1, "": 1}, {"a": 1, "b": 2}):
        assert run(body, {"d": value}) == in_python(body, {"d": value})
        assert run(every, {"d": value}) == in_python(every, {"d": value})


def test_any_stops_at_the_first_item_that_is_true():
    values = iter([0.1, 0.9, 0.4])
    assert any(next(values) > 0.5 for x in [1, 2, 3]) is True
    assert list(values) == [0.4]
    body = "return any(random.random() > 0.5 for x in [1, 2, 3])"
    sequence = corpus.Sequence([0.1, 0.9, 0.4])
    with asl.replaced(random=sequence):
        assert asl.run(randomized(body), {}) is True
    assert sequence.calls == 2


def test_all_stops_at_the_first_item_that_is_false():
    values = iter([0.9, 0.1, 0.4])
    assert all(next(values) > 0.5 for x in [1, 2, 3]) is False
    assert list(values) == [0.4]
    body = "return all(random.random() > 0.5 for x in [1, 2, 3])"
    sequence = corpus.Sequence([0.9, 0.1, 0.4])
    with asl.replaced(random=sequence):
        assert asl.run(randomized(body), {}) is False
    assert sequence.calls == 2


def test_an_item_that_decides_nothing_is_read_to_the_end():
    values = iter([0.1, 0.2, 0.3])
    assert any(next(values) > 0.5 for x in [1, 2, 3]) is False
    assert list(values) == []
    body = "return any(random.random() > 0.5 for x in [1, 2, 3])"
    sequence = corpus.Sequence([0.1, 0.2, 0.3])
    with asl.replaced(random=sequence):
        assert asl.run(randomized(body), {}) is False
    assert sequence.calls == 3


def test_a_condition_is_evaluated_until_the_result_is_decided():
    # The condition drops the first item, the second passes it and decides the
    # result, and the third is neither tested nor read: two conditions and the
    # one item between them.
    values = iter([0.1, 0.9, 0.9, 0.4])
    assert any(next(values) > 0.5 for x in [1, 2, 3] if next(values) > 0.5) is True
    assert list(values) == [0.4]
    body = (
        "return any(random.random() > 0.5 for x in [1, 2, 3] if random.random() > 0.5)"
    )
    sequence = corpus.Sequence([0.1, 0.9, 0.9, 0.4])
    with asl.replaced(random=sequence):
        assert asl.run(randomized(body), {}) is True
    assert sequence.calls == 3


def test_a_list_comprehension_evaluates_every_item():
    """Python builds the whole list before any() reads it, and so does the
    definition: the short circuit is the generator expression's."""
    values = iter([0.9, 0.1, 0.4])
    items = [next(values) > 0.5 for x in [1, 2, 3]]
    assert any(items) is True
    assert list(values) == []
    body = "return any([random.random() > 0.5 for x in [1, 2, 3]])"
    sequence = corpus.Sequence([0.9, 0.1, 0.4])
    with asl.replaced(random=sequence):
        assert asl.run(randomized(body), {}) is True
    assert sequence.calls == 3


def test_an_item_past_the_one_that_decides_is_never_evaluated():
    """The result is decided by the first item, so the second, which would
    fail, is not read; a list comprehension of the same items fails."""
    body = 'xs: list[float] = input["xs"]\nreturn any(10 / x > 1 for x in xs)'
    assert run(body, {"xs": [1, 0]}) is True
    assert in_python(body, {"xs": [1, 0]}) is True
    listed = 'xs: list[float] = input["xs"]\nreturn any([10 / x > 1 for x in xs])'
    with pytest.raises(asl.Failure) as raised:
        run(listed, {"xs": [1, 0]})
    assert raised.value.error == "States.QueryEvaluationError"
    assert raised.value.cause == "division by zero"
    with pytest.raises(ZeroDivisionError):
        in_python(listed, {"xs": [1, 0]})


def test_a_condition_past_the_one_that_decides_is_never_evaluated():
    body = 'xs: list[float] = input["xs"]\nreturn any(x > 0 for x in xs if 10 / x > 1)'
    assert run(body, {"xs": [2, 0]}) is True
    assert in_python(body, {"xs": [2, 0]}) is True


def test_all_stops_before_the_item_that_would_fail():
    body = 'xs: list[float] = input["xs"]\nreturn all(10 / x > 1 for x in xs)'
    assert run(body, {"xs": [-1, 0]}) is False
    assert in_python(body, {"xs": [-1, 0]}) is False


def test_the_variable_does_not_leak():
    body = 'xs: list[bool] = input["xs"]\nb = any(x for x in xs)\nreturn x'
    with pytest.raises(CompileError, match="x is not assigned here"):
        compile_source(source(body))


def test_the_variable_cannot_hide_what_another_name_reads():
    # item is $items[$item_index], which a function parameter $items would hide.
    body = (
        'items: list[str] = input["items"]\nzs: list = input["zs"]\n'
        "for item in items:\n    b = any(item for items in zs)"
    )
    with pytest.raises(
        CompileError, match="items is a variable that this comprehension reads"
    ):
        compile_source(source(body))


def test_types():
    body = 'xs: list[bool] = input["xs"]\nb = any(xs)\nreturn not b'
    assert output(body) == "{% $not($b) %}"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'xs: list = input["xs"]\nreturn any()',
            'any() takes one argument: any(x["ok"] for x in xs)',
        ),
        (
            'xs: list = input["xs"]\nreturn all(xs, xs)',
            'all() takes one argument: all(x["ok"] for x in xs)',
        ),
        (
            'return any(input["xs"])',
            (
                "any() depends on what it iterates, so the type of input['xs'] must be "
                "known"
            ),
        ),
        (
            's: str = input["s"]\nreturn all(c for c in s)',
            "s is a string; all() iterates lists and the keys of dicts",
        ),
        (
            'xs: list = input["xs"]\nys: list = input["ys"]\nreturn any(x for x in xs for y in ys)',
            "a comprehension takes one for",
        ),
        (
            'xs: list = input["xs"]\nreturn any(a for a, b in xs)',
            'any() iterates one variable: any(x["ok"] for x in xs)',
        ),
        # enumerate() says where it is taken before the message about
        # unpacking.
        (
            'xs: list = input["xs"]\nreturn all(i for i, x in enumerate(xs))',
            "enumerate() is only for a for loop: for i, item in enumerate(items)",
        ),
    ],
)
def test_diagnostics(body, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source(body))
    assert message in raised.value.message
