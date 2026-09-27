import pytest

from sfnx.syntax import facts


@pytest.mark.parametrize(
    "code, bound",
    [
        # := binds the name for the rest of its block.
        ("($v := 1; $v + 1)", {"v"}),
        # A function's parameters take over variables of their names.
        ("$map($xs, function($x, $i) { $x + $i })", {"x", "i"}),
        # Inside an object's value, which the parser keeps in pairs.
        ('{"k": ($v := 1; $v)}', {"v"}),
        # A read binds nothing.
        ("$v + 1", set()),
        # Nor does a string that looks like a binding.
        ("'$v := 1'", set()),
    ],
)
def test_bound_names(code, bound):
    found = facts(code)
    assert found is not None
    assert found.bound == bound


@pytest.mark.parametrize(
    "code, spelled",
    [
        ("'$n'", {"n"}),
        ('"a $n and $m"', {"n", "m"}),
        # A key of an object is a string too.
        ('{"$k": 1}', {"k"}),
        # A regular expression is text as well.
        ("$match($s, /a$n/)", {"n"}),
        # A read is not spelled.
        ("$n & 'x'", set()),
        # JSONata names do not start with a digit.
        ("'$1'", set()),
    ],
)
def test_spelled_names(code, spelled):
    found = facts(code)
    assert found is not None
    assert found.spelled == spelled


def test_code_that_is_not_jsonata_has_no_facts():
    # jsonata() takes any text, which Step Functions checks when it validates
    # the definition.
    assert facts("$f(") is None
