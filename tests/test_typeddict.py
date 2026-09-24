"""TypedDict classes of the module as the types of inputs, payloads and
parameters."""

import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError

INPUT = "$states.context.Execution.Input"
LAMBDA = "arn:aws:states:::lambda:invoke"
CLASSES = """\
from typing import NotRequired, TypedDict

from sfnx import inline_map, state_machine, task


class Item(TypedDict):
    sku: str
    quantity: int


class Order(TypedDict):
    id: str
    items: list[Item]
    coupon: NotRequired[str]
"""


def source(body: str, parameter: str = "input: Order", preamble: str = CLASSES) -> str:
    return (
        preamble
        + f"\n\n@state_machine\ndef fulfill({parameter}):\n"
        + textwrap.indent(body, "    ")
    )


def states(body: str, parameter: str = "input: Order", preamble: str = CLASSES) -> dict:
    (compiled,) = compile_source(source(body, parameter, preamble)).values()
    return compiled["States"]


def output(body: str, parameter: str = "input: Order", preamble: str = CLASSES) -> str:
    return states(body, parameter, preamble)["return"]["Output"]


def rejected(
    body: str, parameter: str = "input: Order", preamble: str = CLASSES
) -> str:
    with pytest.raises(CompileError) as raised:
        compile_source(source(body, parameter, preamble))
    return raised.value.message


@pytest.mark.parametrize(
    "body, code",
    [
        # The fields type the values under them, through lists and nesting.
        ('return input["items"][0]["quantity"] + 1', f"{INPUT}.items[0].quantity + 1"),
        ('return len(input["id"])', f"$length({INPUT}.id)"),
        ('return input["id"] + "!"', f"{INPUT}.id & '!'"),
        ('return len(input["items"])', f"$count({INPUT}.items)"),
        # A key that may be left out is read with in or .get().
        ('return "coupon" in input', f"$exists({INPUT}.coupon)"),
        (
            'return input.get("coupon")',
            f"$exists({INPUT}.coupon) ? {INPUT}.coupon : null",
        ),
        # A key the class does not declare is of unknown type, as a key of an
        # AWS response the shape does not declare is.
        ('return input["note"] + 1', f"{INPUT}.note + 1"),
    ],
)
def test_the_fields_of_the_input(body, code):
    assert output(body) == "{% " + code + " %}"


def test_get_of_a_key_that_may_be_left_out_narrows():
    body = 'c = input.get("coupon")\nif c is not None:\n    return c + "!"\nreturn ""'
    assert states(body)["return"]["Output"] == "{% $c & '!' %}"


def test_a_loop_reads_the_items_as_the_class_declares_them():
    body = 'total = 0\nfor item in input["items"]:\n    total = total + item["quantity"]\nreturn total'
    assert states(body)["total_2"]["Assign"] == {
        "total": f"{{% $total + {INPUT}.items[$item_index].quantity %}}",
        "item_index": "{% $item_index + 1 %}",
    }


def test_an_assignment_a_payload_and_a_task_result_take_a_class():
    body = 'order: Order = input["order"]\nreturn order["items"][0]["quantity"] + 1'
    assert output(body, "input") == "{% $order.items[0].quantity + 1 %}"
    body = f'r: Item = task("{LAMBDA}", {{"FunctionName": "f"}})["Payload"]\nreturn r["quantity"] + 1'
    assert states(body)["r"]["Output"] == "{% $states.result.Payload.quantity + 1 %}"
    body = (
        f'r: Item = task("{LAMBDA}", {{"FunctionName": "f"}})\nreturn r["quantity"] + 1'
    )
    assert states(body)["r"]["Output"] == "{% $states.result.quantity + 1 %}"


def test_the_parameter_of_a_map_function_takes_a_class():
    after = '\n\ndef ship(item: Item):\n    return item["quantity"] + 1\n'
    compiled = states(
        'return inline_map(ship, input["items"])', preamble=CLASSES + after
    )
    processor = compiled["return"]["ItemProcessor"]["States"]["ship.return"]
    assert processor["Output"] == "{% $states.input.item.quantity + 1 %}"


def test_a_class_is_named_in_a_list_a_dict_and_a_union():
    body = 'return xs[0]["quantity"] + 1'
    assert output('xs: list[Item] = input["xs"]\n' + body, "input") == (
        "{% $xs[0].quantity + 1 %}"
    )
    body = 'return d["a"]["quantity"] + 1'
    assert output('d: dict[str, Item] = input["d"]\n' + body, "input") == (
        "{% $d.a.quantity + 1 %}"
    )
    body = 'if x is not None:\n    return x["quantity"] + 1\nreturn 0'
    assert (
        states('x: Item | None = input["x"]\n' + body, "input")["return"]["Output"]
        == "{% $x.quantity + 1 %}"
    )


@pytest.mark.parametrize(
    "preamble",
    [
        "import typing\nfrom sfnx import state_machine\n\n\nclass P(typing.TypedDict):\n    x: typing.NotRequired[int]\n",
        "from typing_extensions import NotRequired, TypedDict\nfrom sfnx import state_machine\n\n\nclass P(TypedDict):\n    x: NotRequired[int]\n",
        "from typing import Required, TypedDict\nfrom sfnx import state_machine\n\n\nclass P(TypedDict, total=False):\n    x: Required[int]\n",
        'from typing import TypedDict\nfrom sfnx import state_machine\n\n\nclass P(TypedDict):\n    """A point."""\n\n    x: int\n',
    ],
)
def test_the_spellings_of_typed_dict(preamble):
    assert (
        output('return input["x"] + 1', "input: P", preamble)
        == f"{{% {INPUT}.x + 1 %}}"
    )


def test_a_plain_class_at_the_top_of_the_module_is_not_a_typed_dict():
    assert output("return 1", preamble=CLASSES + "\n\nclass Note:\n    pass\n") == 1


TYPED = "from typing import TypedDict\nfrom sfnx import state_machine\n"


@pytest.mark.parametrize(
    "preamble, parameter, message",
    [
        (
            TYPED + "\n\nclass Node(TypedDict):\n    children: list[Node]\n",
            "input: Node",
            "recursive TypedDicts are not supported",
        ),
        (
            CLASSES + "\n\nclass Big(Order):\n    extra: str\n",
            "input: Big",
            "a TypedDict does not inherit; derive it from TypedDict directly and declare every field on the class",
        ),
        (
            CLASSES + "\n\nclass Big(Order, TypedDict):\n    extra: str\n",
            "input: Big",
            "a TypedDict does not inherit",
        ),
        (
            TYPED + '\n\nPoint = TypedDict("Point", {"x": int})\n',
            "input: Point",
            "write Point as a class: class Point(TypedDict):",
        ),
        (
            TYPED + "\n\nclass P(TypedDict, frozen=True):\n    x: int\n",
            "input: P",
            "a TypedDict takes total=False here and no other argument",
        ),
        (
            TYPED
            + "\n\nclass P(TypedDict):\n    x: int\n\n    def f(self):\n        pass\n",
            "input: P",
            "a TypedDict declares its fields only, one per line: name: type",
        ),
        (
            TYPED + "\n\nclass P(TypedDict):\n    x: int = 1\n",
            "input: P",
            "a TypedDict declares its fields only",
        ),
        (
            TYPED + "\n\nclass P(TypedDict):\n    x: Any\n",
            "input: P",
            "annotate with float, str, bool",
        ),
        # A class of another module is not read: the compiler reads one file.
        (
            "from typing import TypedDict\nfrom sfnx import state_machine\nfrom shapes import Order\n",
            "input: Order",
            "or a TypedDict class of this module",
        ),
    ],
)
def test_diagnostics(preamble, parameter, message):
    assert message in rejected("return 1", parameter, preamble)
