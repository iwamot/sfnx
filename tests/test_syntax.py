import pytest

from sfnx.syntax import (
    changes,
    facts,
    names_read,
    reads_own_context,
    reads_own_states,
    reads_state_name,
)


@pytest.mark.parametrize(
    "code, own, context, name",
    [
        # The execution's input reads alike in every state.
        ("$states.context.Execution.Input.a", False, False, False),
        (
            "$states.context.StateMachine.Id & $states.context.Map.Item.Index",
            *[False] * 3,
        ),
        # A state's input, result and error output are its own.
        ("$states.input.a", True, False, False),
        ("$count($states.result)", True, False, False),
        # The other parts of the context are the state's own too.
        ("$states.context.Task.Token", True, True, False),
        ("$states.context.State.Name", True, True, True),
        # Read whole, or another way, any part may be read.
        ("$states.context", True, True, True),
        ("$states", True, True, True),
        ("$states[0]", True, True, True),
        ("$states[0].input", True, True, True),
        # The names stop where a step is not a name, as a block is not.
        ("$states.context.(Execution)", True, True, True),
        # A string that spells it reads nothing.
        ("'$states.input'", False, False, False),
        # What the parser cannot read may read any of it.
        ("$states.input(", True, True, True),
    ],
)
def test_states_read(code, own, context, name):
    assert reads_own_states(code) == own
    assert reads_own_context(code) == context
    assert reads_state_name(code) == name


@pytest.mark.parametrize(
    "code, changing",
    [
        ("$random() * 2", True),
        ("$now()", True),
        # $eval may call either, and reads variables by names not written.
        ("$eval('1')", True),
        # A function passed on is called too.
        ("$map([1, 2], $random)", True),
        # A string that spells one calls nothing.
        ("'$random()'", False),
        # A function of that name the expression defines is its own.
        ("($random := function() { 1 }; $random())", False),
        ("$count($xs)", False),
        # What the parser cannot read may call anything its text names.
        ("$random(", True),
    ],
)
def test_changes(code, changing):
    assert changes(code) == changing


@pytest.mark.parametrize(
    "code, reads",
    [
        ("$a + $b", {"a", "b"}),
        # $states is a variable too, and so is a function called by name.
        ("$count($states.input)", {"count", "states"}),
        # The context and the root are not variables.
        ("$count($ys[$ != 1]) + $$.a", {"count", "ys"}),
        # A block binds a name for the expressions after the binding, and the
        # binding reads the name from before it.
        ("($x := $x + 1; $x * $y)", {"x", "y"}),
        ("($v := 1; $v)", set()),
        # A filter on the block reads what is bound around it.
        ("($v := 1; $v)[$v]", {"v"}),
        ("([1, 2])[$i]", {"i"}),
        # A function's parameters are its own; what else its body reads is
        # read.
        ("function($i) { $i + $k }", {"k"}),
        # @ and # bind for the step's filters and the steps after it.
        ("$xs#$i[$i > $j].($i)", {"xs", "j"}),
        ("$xs@$e.($e + $f)", {"xs", "f"}),
        # A binding anywhere but right in a block binds nothing for certain,
        # so the name still counts as read.
        ("$f(($v := 1)) + $v", {"f", "v"}),
        # A string spells a name, which is not a read.
        ("'$n' & $m", {"m"}),
        # Inside an object.
        ('{"k": $v}', {"v"}),
    ],
)
def test_names_read(code, reads):
    assert names_read(code) == reads


def test_what_the_parser_cannot_read_reads_every_name_it_spells():
    assert names_read("$f($x") == {"f", "x"}


@pytest.mark.parametrize(
    "code, bound",
    [
        # := binds the name for the rest of its block.
        ("($v := 1; $v + 1)", {"v"}),
        # A function's parameters take over variables of their names.
        ("$map($xs, function($x, $i) { $x + $i })", {"x", "i"}),
        # Inside an object's value, which the parser keeps in pairs.
        ('{"k": ($v := 1; $v)}', {"v"}),
        # @ and # in a path.
        ("$xs@$e#$i.($e + $i)", {"e", "i"}),
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
