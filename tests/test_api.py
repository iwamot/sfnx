"""The Python API: the compiler on a file or on text, and its error."""

from pathlib import Path

import pytest

import sfnx.compiler
from sfnx.compiler import CompileError, compile_file, compile_source

ROOT = Path(__file__).parent.parent
SOURCE = 'from sfnx import state_machine\n\n\n@state_machine\ndef pay(input):\n    return input["a"] + input["b"]\n'


def test_the_public_names():
    assert sfnx.compiler.__all__ == ["CompileError", "compile_file", "compile_source"]
    assert issubclass(CompileError, ValueError)


def test_a_file_and_its_text_compile_the_same():
    example = ROOT / "examples" / "orders.py"
    from_file = compile_file(example)
    from_text = compile_source(example.read_text(), str(example))
    assert from_file == from_text
    assert list(from_file) == ["fulfill"]
    assert from_file["fulfill"]["QueryLanguage"] == "JSONata"


def test_every_machine_is_returned_in_order():
    source = SOURCE.replace('return input["a"] + input["b"]', "return 1")
    source += "\n\n@state_machine\ndef refund(input):\n    return 2\n"
    assert list(compile_source(source)) == ["pay", "refund"]


def test_the_error_carries_the_line_the_cli_prints():
    with pytest.raises(CompileError) as raised:
        compile_source(SOURCE, "app.py")
    error = raised.value
    assert (error.filename, error.line, error.column) == ("app.py", 6, 12)
    assert error.message.startswith("+ adds numbers, joins strings or lists")
    assert str(error) == f"app.py:6:12: {error.message}"
    with pytest.raises(CompileError) as raised:
        compile_source(SOURCE)
    assert str(raised.value).startswith("<string>:6:12: ")


@pytest.mark.parametrize(
    "source, message",
    [
        ("def pay(input:\n    return 1\n", "'(' was never closed"),
        ("def pay(input):\n    return 1\n", "no state machine here"),
    ],
)
def test_a_syntax_error_and_a_missing_machine_are_compile_errors(source, message):
    with pytest.raises(CompileError) as raised:
        compile_source(source)
    assert message in raised.value.message


def test_a_file_that_cannot_be_read_raises_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        compile_file(tmp_path / "missing.py")
