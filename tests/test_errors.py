import textwrap

import pytest

import sfnx
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.errors import STATES
from tests import asl

INPUT = "$states.context.Execution.Input"


def source(body: str, preamble: str = "") -> str:
    return (
        "from sfnx import state_machine\n"
        + preamble
        + "\n\n@state_machine\ndef pay(input):\n"
        + textwrap.indent(body, "    ")
    )


def definition(
    body: str, preamble: str = "class TooLarge(Exception):\n    pass\n"
) -> dict:
    (compiled,) = compile_source(source(body, preamble)).values()
    return compiled


def test_fail_example():
    body = 'if input["amount"] > 1000:\n    raise TooLarge("amount exceeds the limit")\nreturn input'
    assert definition(body) == {
        "QueryLanguage": "JSONata",
        "StartAt": "if",
        "States": {
            "if": {
                "Type": "Choice",
                "Choices": [
                    {"Condition": f"{{% {INPUT}.amount > 1000 %}}", "Next": "raise"}
                ],
                "Default": "return",
            },
            "raise": {
                "Type": "Fail",
                "Error": "TooLarge",
                "Cause": "amount exceeds the limit",
            },
            "return": {"Type": "Succeed", "Output": f"{{% {INPUT} %}}"},
        },
    }


@pytest.mark.parametrize(
    "body, state",
    [
        ("raise TooLarge()", {"Type": "Fail", "Error": "TooLarge"}),
        ("raise TooLarge", {"Type": "Fail", "Error": "TooLarge"}),
        (
            'raise TooLarge(input["reason"])',
            {"Type": "Fail", "Error": "TooLarge", "Cause": f"{{% {INPUT}.reason %}}"},
        ),
        (
            'raise TooLarge("amount " + str(input["amount"]))',
            {
                "Type": "Fail",
                "Error": "TooLarge",
                "Cause": f"{{% 'amount ' & $string({INPUT}.amount) %}}",
            },
        ),
        (
            'raise TooLarge(input["amount"] * 2)',
            {
                "Type": "Fail",
                "Error": "TooLarge",
                "Cause": f"{{% $string({INPUT}.amount * 2) %}}",
            },
        ),
    ],
)
def test_raise(body, state):
    assert definition(body)["States"]["raise"] == state


def test_from_is_left_out():
    assert definition("raise TooLarge() from None")["States"]["raise"] == {
        "Type": "Fail",
        "Error": "TooLarge",
    }


def test_pending_assignments_come_before_the_fail():
    states = definition('reason = input["reason"]\nraise TooLarge(reason)')["States"]
    assert states["reason"]["Next"] == "raise"
    assert states["raise"]["Cause"] == "{% $reason %}"


def test_a_raise_ends_the_path():
    body = 'if input["a"]:\n    raise TooLarge()\nelse:\n    x = 1\nreturn x'
    assert definition(body)["States"]["return"]["Output"] == "{% $x %}"
    with pytest.raises(CompileError, match="never reached"):
        definition("raise TooLarge()\nx = 1")


@pytest.mark.parametrize(
    "preamble, error, name",
    [
        ("from errors import TooLarge", "TooLarge", "TooLarge"),
        ("from errors import TooLarge as Big", "Big", "TooLarge"),
        ("import errors", "errors.TooLarge", "TooLarge"),
        ("import app.errors as e", "e.TooLarge", "TooLarge"),
        (
            "class Declined(Exception):\n    '''The card was declined.'''",
            "Declined",
            "Declined",
        ),
    ],
)
def test_where_classes_come_from(preamble, error, name):
    states = definition(f"raise {error}()", preamble)["States"]
    assert states["raise"]["Error"] == name


@pytest.mark.parametrize(
    "error, name",
    [("_416", "416"), ("_Internal", "_Internal"), ("_", "_")],
)
def test_a_leading_underscore_spells_a_name_python_cannot(error, name):
    preamble = f"class {error}(Exception):\n    pass"
    states = definition(f"raise {error}()", preamble)["States"]
    assert states["raise"]["Error"] == name


def test_asl_run():
    body = 'if input["amount"] > 1000:\n    raise TooLarge("too large: " + str(input["amount"]))\nreturn "ok"'
    with pytest.raises(asl.Failure) as failure:
        asl.run(definition(body), {"amount": 5000})
    assert (failure.value.error, failure.value.cause) == ("TooLarge", "too large: 5000")
    assert asl.run(definition(body), {"amount": 5}) == "ok"


def test_sfnx_error_classes_match_the_table():
    for name, error in STATES.items():
        cls = getattr(sfnx, name)
        assert issubclass(cls, Exception)
        assert cls.__doc__ == error
        assert name in sfnx.__all__


@pytest.mark.parametrize(
    "body, preamble, message",
    [
        ("raise", "", "name the error to raise"),
        (
            'raise TooLarge("a", "b")',
            "class TooLarge(Exception):\n    pass",
            "pass one message",
        ),
        (
            'raise TooLarge(message="a")',
            "class TooLarge(Exception):\n    pass",
            "pass one message",
        ),
        (
            "raise ValueError('bad')",
            "",
            "ValueError is a Python exception and has no ASL error name; define your own: class OrderFailed(Exception): pass",
        ),
        ("raise Exception('bad')", "", "raise names one error"),
        ("raise Missing()", "", "Missing is not defined; define your own"),
        ("raise errors()[0]", "", "name an exception class here"),
        (
            "raise Timeout()",
            "from sfnx import Timeout",
            "error names starting with States. are reserved",
        ),
        ("raise sfnx.Nope()", "import sfnx", "sfnx has no error named Nope"),
        (
            "raise Small()",
            "class Base(Exception):\n    pass\nclass Small(Base):\n    pass",
            "ASL error names have no hierarchy; derive Small from Exception directly",
        ),
        ("raise Plain()", "class Plain:\n    pass", "derive Plain from Exception"),
        (
            "raise Meta()",
            "class Meta(Exception, metaclass=type):\n    pass",
            "derive Meta from Exception",
        ),
        (
            "raise Shadow()",
            "class Exception(BaseException):\n    pass\nclass Shadow(Exception):\n    pass",
            "derive Shadow from Exception",
        ),
        (
            'assert input["a"]',
            "",
            "assert is not compiled; write the check as if not ...: raise OrderFailed(...)",
        ),
    ],
)
def test_diagnostics(body, preamble, message):
    with pytest.raises(CompileError) as raised:
        definition(body, preamble)
    assert message in raised.value.message
