import json
import textwrap

import pytest

from sfnx.compiler import compile_source, definitions
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
        # The assignments before the first Task go in it, their comments too.
        "receipt": "the total\nand the count\ncharge it",
        "for": "each item",
        "status": "poll\nuntil done",
        "if": None,
        # The return after the try reads a variable, which cannot fail, so it
        # is the Task's Output on the way the Task leads.
        "invoke risky": "guarded\ndone",
        "raise": "give up",
    }
    assert list(compiled["States"]["for"])[:2] == ["Type", "Comment"]
    # The body's first assignment goes in the rule, with its comment, and the
    # if right after it is rules of the loop's Choice, which take the comments
    # of both; the one that goes on takes the increment.
    rules = compiled["States"]["for"]["Choices"]
    assert [rule["Comment"] for rule in rules] == ["add it\nleave early"] * 2
    assert rules[1]["Assign"]["item_index"] == "{% $item_index + 1 %}"


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
    assert remarks(definition(body)["States"])["return"] == "the total"


ASSIGNED_BEFORE_A_LOOP = f"""\
# about a
x = input["a"]
# the loop
while input["again"]:
    task("{LAMBDA}", {{"FunctionName": "step"}})
return 2"""


@pytest.mark.parametrize(
    "body, expected",
    [
        ('# about a\nx = input["a"]\nreturn 2', {"return": "about a"}),
        (
            '# about a\nx = input["a"]\n# about b\ny = input["b"]\nreturn 2',
            {"return": "about a\nabout b"},
        ),
        # The way back into the loop passes through the assignment first too,
        # so the loop takes its comment.
        (
            ASSIGNED_BEFORE_A_LOOP,
            {"while": "about a\nthe loop", "invoke step": None, "return": None},
        ),
    ],
)
def test_an_assignment_nothing_reads_passes_its_comment_on(body, expected):
    assert remarks(definition(body)["States"]) == expected


TRY_SUCCESS = f"""\
try:
    task("{LAMBDA}", {{"FunctionName": "risky"}})
    # about a
    x = input["a"]
except Exception:
    task("{LAMBDA}", {{"FunctionName": "recover"}})
# the answer
return 2"""


@pytest.mark.parametrize(
    "body",
    [
        TRY_SUCCESS,
        "def left():\n"
        + textwrap.indent(TRY_SUCCESS, "    ")
        + "\n\nreturn parallel(left)",
        "def each(item):\n"
        + textwrap.indent(TRY_SUCCESS.replace('input["a"]', 'item["a"]'), "    ")
        + '\n\nreturn inline_map(each, input["items"])',
    ],
)
def test_a_comment_no_state_after_it_belongs_to_alone_goes_with_it(body):
    """The recovery leads to the return without the assignment, so the
    return would describe it on that way too."""
    compiled = json.dumps(definition(body))
    assert "about a" not in compiled
    assert "the answer" in compiled


def test_comments_do_not_keep_a_pass_whose_comment_goes():
    """Source locations give every state a Comment, which keeps no more
    states than the definition has without them."""
    body = TRY_SUCCESS.replace("    # about a\n", "").replace(
        "# the answer\nreturn 2",
        f'y = task("{LAMBDA}", {{"FunctionName": "named"}})  # state: Named\nreturn 2',
    )
    source = (
        "from sfnx import state_machine, task\n\n\n@state_machine\ndef flow(input):\n"
        + textwrap.indent(body, "    ")
    )
    (plain,) = definitions(source, "flow.py", located=False).values()
    (located,) = definitions(source, "flow.py", located=True).values()
    assert list(located["States"]) == list(plain["States"])
