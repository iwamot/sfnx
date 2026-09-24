"""--source-locations: each state's Comment ends with the spans of the source
it comes from, and nothing else about the definition changes."""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from sfnx.cli import document, main
from sfnx.compiler import compile_source, definitions
from sfnx.locations import PREFIX

ROOT = Path(__file__).parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))

SOURCE = '''\
from sfnx import inline_map, parallel, state_machine, task

LAMBDA = "arn:aws:states:::lambda:invoke"


class Declined(Exception):
    pass


@state_machine
def pay(input):
    """Charge each order."""
    # 合計と件数 🧾
    total = 0
    count = 0
    note = "注文 🧾"
    orders: list = input["orders"]
    for order in orders:
        if order["amount"] > 100:
            total = total + order["amount"]
        elif order["amount"] < 0:
            break
        count = count + 1
    while total > 1000:
        total = total - 1000
    try:
        receipt = task(LAMBDA, {"FunctionName": "charge", "Payload": total})
    except Declined:
        raise Declined("declined")

    def email():
        return {"to": input["email"], "note": note}

    def audit():
        task(LAMBDA, {"FunctionName": "audit"})

    sent, _ = parallel(email, audit)
    return {"total": total, "count": count, "receipt": receipt, "sent": sent}


def ship(order: dict, index: int) -> None:
    """Ship one order."""
    task(LAMBDA, {"FunctionName": "ship", "Payload": order})


@state_machine
def fan(input):
    inline_map(ship, input["orders"])
'''


def states(definition: dict) -> Iterator[tuple[str, dict]]:
    """Every state, in branches and Map processors too, and each Choice rule
    that holds assignments, as `if[0]`."""
    for name, state in definition["States"].items():
        yield name, state
        for index, rule in enumerate(state.get("Choices", [])):
            if "Assign" in rule:
                yield f"{name}[{index}]", rule
        for branch in state.get("Branches", []):
            yield from states(branch)
        if "ItemProcessor" in state:
            yield from states(state["ItemProcessor"])


def text(source: str, at: str) -> str:
    """The source a span covers, lines joined with newlines."""
    lines = source.split("\n")
    first, last = at.split("-")
    start_line, start = map(int, first.split(":"))
    end_line, end = map(int, last.split(":"))
    if start_line == end_line:
        return lines[start_line - 1][start - 1 : end - 1]
    middle = lines[start_line : end_line - 1]
    return "\n".join(
        [lines[start_line - 1][start - 1 :], *middle, lines[end_line - 1][: end - 1]]
    )


def located(source: str, definition: dict) -> dict[str, tuple[list[str], list]]:
    """Each state's own comment lines, and the source text and role of each
    span its last line names."""
    found = {}
    for name, state in states(definition):
        *remarks, last = state["Comment"].split("\n")
        assert last.startswith(PREFIX)
        spans = json.loads(last.removeprefix(PREFIX))["spans"]
        found[name] = (
            remarks,
            [(text(source, s["at"]), s.get("role")) for s in spans],
        )
    return found


def test_each_state_names_the_source_it_comes_from():
    pay, fan = definitions(SOURCE, "app.py", located=True).values()
    header = "for order in orders:"
    assert located(SOURCE, pay) == {
        "total": (
            ["合計と件数 🧾"],
            [
                ("total = 0", None),
                ("count = 0", None),
                ('note = "注文 🧾"', None),
                ('orders: list = input["orders"]', None),
                (header, "loop start"),
            ],
        ),
        "for": ([], [(header, None)]),
        # The two paths that go on join at count, so each takes it.
        "if": (
            [],
            [
                ('if order["amount"] > 100:', None),
                ('elif order["amount"] < 0:', None),
                ("count = count + 1", None),
                (header, "loop step"),
            ],
        ),
        "if[0]": (
            [],
            [
                ('total = total + order["amount"]', None),
                ("count = count + 1", None),
                (header, "loop step"),
            ],
        ),
        "while": ([], [("while total > 1000:", None)]),
        "while[0]": ([], [("total = total - 1000", None)]),
        "receipt": (
            [],
            [
                (
                    'receipt = task(LAMBDA, {"FunctionName": "charge", "Payload": total})',
                    None,
                )
            ],
        ),
        "raise": ([], [('raise Declined("declined")', None)]),
        # The return right after the Parallel is its Output.
        "sent": (
            [],
            [
                ("sent, _ = parallel(email, audit)", None),
                (
                    'return {"total": total, "count": count, "receipt": receipt, "sent": sent}',
                    None,
                ),
            ],
        ),
        "email.return": (
            [],
            [('return {"to": input["email"], "note": note}', None)],
        ),
        # The end of the function is where the Task ends the branch.
        "audit.invoke": (
            [],
            [
                ('task(LAMBDA, {"FunctionName": "audit"})', None),
                ("def audit():", "end of function"),
            ],
        ),
    }
    ship = "def ship(order: dict, index: int) -> None:"
    assert located(SOURCE, fan) == {
        "map": (
            [],
            [
                ('inline_map(ship, input["orders"])', None),
                ("def fan(input):", "end of function"),
            ],
        ),
        "ship.order": ([], [(ship, "parameters")]),
        "ship.invoke": (
            [],
            [
                ('task(LAMBDA, {"FunctionName": "ship", "Payload": order})', None),
                (ship, "end of function"),
            ],
        ),
    }
    # The docstrings of the machine and the processor stay as they are.
    assert pay["Comment"] == "Charge each order."
    processor = fan["States"]["map"]["ItemProcessor"]
    assert processor["Comment"] == "Ship one order."


def test_headers_over_lines_and_loops_the_body_leads_back_to():
    source = """\
from sfnx import state_machine


@state_machine
def count(input):
    rows: list = input["rows"]
    kept = []
    for row in rows[1:]:
        if (
            row["n"] > 0
            and row == {"a": 1}
        ):
            row = row["next"]
        kept = kept + [row]
    for i in range(len(kept)):
        kept = kept + [i]
    while True:
        kept = kept[:-1]
        if len(kept) < 3:
            break
    return kept
"""
    (definition,) = definitions(source, "app.py", located=True).values()
    header = "for row in rows[1:]:"
    counting = "for i in range(len(kept)):"
    assert located(source, definition) == {
        "rows": (
            [],
            [
                ('rows: list = input["rows"]', None),
                ("kept = []", None),
                (header, "loop start"),
            ],
        ),
        # The second loop starts in the first one's Choice, which only its
        # Default leads on from, and each body's first assignments go in the
        # rule that leads there.
        "for": ([], [(header, None), (counting, "loop start")]),
        "for[0]": ([], [(header, "loop variables")]),
        "if": (
            [],
            [
                (
                    'if (\n            row["n"] > 0\n            and row == {"a": 1}\n        ):',
                    None,
                )
            ],
        ),
        "if[0]": ([], [('row = row["next"]', None)]),
        "kept": ([], [("kept = kept + [row]", None), (header, "loop step")]),
        "for_2": ([], [(counting, None)]),
        "for_2[0]": ([], [("kept = kept + [i]", None), (counting, "loop step")]),
        "kept_2": ([], [("kept = kept[:-1]", None)]),
        "if_2": ([], [("if len(kept) < 3:", None)]),
        "return": ([], [("return kept", None)]),
    }


def test_columns_count_characters():
    source = (
        "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
        '    a = "日本語 🧾"; b = 2\n'
        "    return [a, b]\n"
    )
    (definition,) = definitions(source, "app.py", located=True).values()
    last = definition["States"]["a"]["Comment"]
    spans = json.loads(last.removeprefix(PREFIX))["spans"]
    assert spans == [{"at": "6:5-6:16"}, {"at": "6:18-6:23"}]


def test_a_wait_spans_the_assignments_it_takes():
    source = (
        "from sfnx import state_machine, wait\n\n\n@state_machine\ndef pay(input):\n"
        "    n = 0\n    wait(1)\n    n = n + 1\n    return n\n"
    )
    (definition,) = definitions(source, "app.py", located=True).values()
    comment = definition["States"]["wait"]["Comment"]
    spans = json.loads(comment.removeprefix(PREFIX))["spans"]
    assert spans == [{"at": "7:5-7:12"}, {"at": "8:5-8:14"}]


def test_each_path_that_takes_an_assignment_spans_it():
    """The rule holds nothing else and has no location yet; the Choice adds
    the span to its own."""
    source = (
        "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
        '    if input["a"]:\n        pass\n    x = 1\n    return x\n'
    )
    (definition,) = definitions(source, "app.py", located=True).values()
    choice = definition["States"]["if"]
    for holder in [choice["Choices"][0], choice]:
        spans = json.loads(holder["Comment"].split("\n")[-1].removeprefix(PREFIX))
        assert {"at": "8:5-8:10"} in spans["spans"]
    assert choice["Assign"] == choice["Choices"][0]["Assign"] == {"x": 1}


def test_a_choice_spans_what_its_rules_and_default_assign():
    source = (
        "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
        '    if input["a"]:\n        x = 1\n    else:\n        x = 2\n    return x\n'
    )
    (definition,) = definitions(source, "app.py", located=True).values()
    found = located(source, definition)
    assert found["if"] == ([], [('if input["a"]:', None), ("x = 2", None)])
    assert found["if[0]"] == ([], [("x = 1", None)])


@pytest.mark.parametrize("filename", ['a "b".py', "a\nb.py", "dir/日本語.py"])
def test_the_file_is_named_as_given_on_one_line(filename):
    source = "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n    return 1\n"
    (definition,) = definitions(source, filename, located=True).values()
    comment = definition["States"]["return"]["Comment"]
    assert "\n" not in comment
    assert json.loads(comment.removeprefix(PREFIX))["file"] == filename


def without_locations(value: object) -> object:
    """A definition with the location line taken out of every Comment."""
    if isinstance(value, list):
        return [without_locations(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "Comment" and isinstance(item, str):
            kept = "\n".join(
                line for line in item.split("\n") if not line.startswith(PREFIX)
            )
            if kept:
                result[key] = kept
            continue
        result[key] = without_locations(item)
    return result


@pytest.mark.parametrize(
    "filename, source",
    [
        pytest.param("app.py", SOURCE, id="fixture"),
        *(pytest.param(p.name, p.read_text(), id=p.stem) for p in EXAMPLES),
    ],
)
def test_the_option_only_adds_a_line_to_each_comment(filename, source):
    plain = compile_source(source, filename)
    assert definitions(source, filename, located=False) == plain
    first = definitions(source, filename, located=True)
    assert without_locations(first) == plain
    # Every state is located, and the same input gives the same bytes.
    for machine in first.values():
        for _, state in states(machine):
            assert state["Comment"].split("\n")[-1].startswith(PREFIX)
    again = definitions(source, filename, located=True)
    assert [document(m) for m in again.values()] == [
        document(m) for m in first.values()
    ]


def test_the_cli_names_the_path_it_was_given(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(
        "from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n"
        "    # done\n    return 1\n"
    )
    assert main(["compile", "app.py", "--source-locations"]) == 0
    definition = json.loads(capsys.readouterr().out)
    assert definition["States"]["return"]["Comment"] == (
        'done\nsfnx-source: {"file": "app.py", "spans": [{"at": "7:5-7:13"}]}'
    )
    assert main(["compile", "app.py"]) == 0
    assert "sfnx-source" not in capsys.readouterr().out


def test_the_deployment_guide_shows_what_the_compiler_writes(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    guide = (ROOT / "docs" / "deployment.md").read_text()
    assert main(["compile", "examples/orders.py", "--source-locations"]) == 0
    written = capsys.readouterr().out
    shown = re.findall(r"^ *\"Comment\": \"sfnx-source: .*$", guide, re.MULTILINE)
    assert shown
    assert all(line.strip() in written for line in shown)
