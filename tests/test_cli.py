import json
from pathlib import Path

from sfnx import state_machine
from sfnx.cli import INSTRUCTIONS, main

SOURCE = "from sfnx import state_machine\n\n\n@state_machine\ndef {name}(input):\n    return 1\n"


def test_prints_the_only_machine(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="pay"))
    assert main(["compile", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["StartAt"] == "return"


def test_writes_a_file(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="pay"))
    output = tmp_path / "pay.asl.json"
    assert main(["compile", str(source), "-o", str(output)]) == 0
    assert json.loads(output.read_text())["StartAt"] == "return"


def test_writes_a_directory(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="a") + "\n\n" + SOURCE.format(name="b"))
    assert main(["compile", str(source), "-o", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "a.asl.json",
        "b.asl.json",
    ]


def test_several_machines_need_a_directory(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="a") + "\n\n" + SOURCE.format(name="b"))
    assert main(["compile", str(source)]) == 2
    assert (
        "defines 2 state machines (a, b); pass -o out/ to write one file each"
        in capsys.readouterr().err
    )


def test_rejected_source(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_text("x = 1\n")
    assert main(["compile", str(source)]) == 1
    assert capsys.readouterr().err.startswith(f"{source}:1:1: no state machine here")


def test_missing_file(tmp_path, capsys):
    missing = tmp_path / "missing.py"
    assert main(["compile", str(missing)]) == 2
    assert capsys.readouterr().err.startswith(f"{missing}: No such file")


def test_a_file_that_python_cannot_decode(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_bytes(b"\xff")
    assert main(["compile", str(source)]) == 1
    assert capsys.readouterr().err.startswith(
        f"{source}:1:1: invalid or missing encoding declaration; save the file as UTF-8"
    )
    source.write_bytes(b'x = 1\ny = "\xff"\n')
    assert main(["compile", str(source)]) == 1
    assert capsys.readouterr().err.startswith(
        f"{source}:2:1: the bytes are not utf-8: invalid start byte; save the file"
    )


def test_text_is_utf_8_whatever_the_locale(tmp_path, capsysbinary):
    # Python reads a source as UTF-8 unless it declares an encoding, and the
    # definition keeps the text as written.
    source = tmp_path / "app.py"
    body = 'from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n    return "日本語"\n'
    source.write_bytes(body.encode())
    assert main(["compile", str(source)]) == 0
    assert '"Output": "日本語"'.encode() in capsysbinary.readouterr().out
    latin = tmp_path / "latin.py"
    latin.write_bytes(
        b"# -*- coding: latin-1 -*-\n" + body.replace("日本語", "é").encode("latin-1")
    )
    output = tmp_path / "nested" / "pay.ASL.JSON"
    assert main(["compile", str(latin), "-o", str(output)]) == 0
    assert '"Output": "é"'.encode() in output.read_bytes()


def test_machines_with_the_same_name(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="pay") + "\n\n" + SOURCE.format(name="pay"))
    assert main(["compile", str(source)]) == 1
    assert "another state machine is named pay; give each its own name" in (
        capsys.readouterr().err
    )


def test_internal_error(tmp_path, capsys):
    source = tmp_path / "app.py"
    source.write_text(SOURCE.format(name="pay").replace("1", "input" + "[0]" * 5000))
    assert main(["compile", str(source)]) == 3
    assert capsys.readouterr().err.startswith("Internal error:")


def test_no_command(capsys):
    assert main([]) == 2
    assert "usage: sfnx" in capsys.readouterr().err


def test_state_machine_leaves_the_function():
    def pay(input):
        return input

    assert state_machine(pay) is pay
    assert state_machine(timeout=300)(pay) is pay


def test_instructions(capsys):
    assert main(["--instructions"]) == 0
    assert capsys.readouterr().out == INSTRUCTIONS + "\n"


def test_the_readme_shows_the_example_and_its_whole_definition(capsys):
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text()
    example = root / "examples" / "orders.py"
    assert f"```python\n{example.read_text()}```" in readme
    assert main(["compile", str(example)]) == 0
    assert f"```json\n{capsys.readouterr().out}```" in readme
