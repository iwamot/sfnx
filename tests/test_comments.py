import textwrap

import pytest

from sfnx.compiler import compile_source
from sfnx.module import comments

LAMBDA = "arn:aws:states:::lambda:invoke"


@pytest.mark.parametrize(
    "source, found",
    [
        ("# one\nx = 1\n", {2: "one"}),
        ("# one\n#two\n    # three\nx = 1\n", {4: "one\ntwo\nthree"}),
        ("# gone\n\nx = 1\n", {}),
        ("x = 1  # trailing\ny = 2\n", {}),
        ('x = """\n# in a string\n"""\ny = 2\n', {}),
        ("# end of file\n", {}),
    ],
)
def test_comment_lines_right_above_code(source, found):
    assert comments(source) == found


def definition(body: str) -> dict:
    source = (
        "from sfnx import inline_map, parallel, state_machine, task\n\n\n"
        "class Failed(Exception):\n    pass\n\n\n"
        "@state_machine\ndef flow(input):\n" + textwrap.indent(body, "    ")
    )
    (compiled,) = compile_source(source).values()
    return compiled


def remarks(states: dict) -> dict:
    return {name: state.get("Comment") for name, state in states.items()}


def test_a_docstring_describes_the_machine():
    compiled = definition('"""Charge an order."""\nreturn 1')
    assert list(compiled)[:2] == ["Comment", "QueryLanguage"]
    assert compiled["Comment"] == "Charge an order."


def test_a_comment_describes_the_first_state_of_its_statement():
    body = f"""
# the total
total = 0
# and the count
count = 1

# not this one, with a blank line below

items: list = input["items"]
# charge it
receipt = task("{LAMBDA}", {{"FunctionName": "charge"}})
# each item
for item in items:
    # add it
    total = total + item["n"]
    # leave early
    if total > 10:
        # stop
        break
# poll
while True:
    # until done
    status = task("{LAMBDA}", {{"FunctionName": "poll"}})
    if status["Payload"] == "done":
        break
# guarded
try:
    task("{LAMBDA}", {{"FunctionName": "risky"}})
except Exception:
    # give up
    raise Failed("no")
# done
return total
"""
    compiled = definition(body)
    assert remarks(compiled["States"]) == {
        "total": "the total\nand the count",
        "receipt": "charge it",
        "item_index": None,
        "for": "each item",
        "if": "leave early",
        "status": "poll\nuntil done",
        "if_2": None,
        "invoke": "guarded",
        "raise": "give up",
        "return": "done",
    }
    assert list(compiled["States"]["for"])[:2] == ["Type", "Comment"]
    # The body's first assignment goes in the rule, with its comment, and the
    # increment in the Choice of the if, whose Default alone leads on.
    assert compiled["States"]["for"]["Choices"][0]["Comment"] == "add it"
    assert compiled["States"]["if"]["Default"] == "for"
    assert compiled["States"]["if"]["Assign"] == {"item_index": "{% $item_index + 1 %}"}


def test_docstrings_describe_branches_and_processors():
    body = '''
def left():
    """The left branch."""
    # from the left
    return 1

def each(x):
    """Per item."""
    return x

both = parallel(left)
return inline_map(each, input["items"])
'''
    states = definition(body)["States"]
    branch = states["both"]["Branches"][0]
    assert branch["Comment"] == "The left branch."
    assert branch["States"]["left.return"]["Comment"] == "from the left"
    assert states["return"]["ItemProcessor"]["Comment"] == "Per item."


def test_a_loop_compiled_again_keeps_its_comment():
    body = """
x = 1
# widen
for i in range(3):
    x = [x]
return x
"""
    assert definition(body)["States"]["for"]["Comment"] == "widen"


def test_a_function_called_directly_takes_the_comment_of_its_call():
    body = f'''
def charge(amount):
    """Charge the card."""
    return task("{LAMBDA}", {{"FunctionName": "charge", "Payload": amount}})

# charge it
receipt = charge(input["amount"])
return receipt
'''
    assert definition(body)["States"]["receipt"]["Comment"] == "charge it"


def test_a_string_on_its_own_line_passes_its_comment_on():
    body = (
        '# the total\n"a string used as a note"\ntotal = input["a"] + 1\nreturn total'
    )
    assert remarks(definition(body)["States"])["total"] == "the total"
