import pytest

from sfnx.syntax import (
    atomic,
    changes,
    facts,
    lone_variable,
    looser_than_and,
    mentions,
    names_read,
    path_alone,
    reads_own_context,
    reads_own_states,
    reads_state_name,
    reads_the_name,
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
    "code, name",
    [
        ("$states.context.State.Name", True),
        # The State part, the context and $states whole hold the name.
        ("$states.context.State", True),
        ("$states.context", True),
        ("$states", True),
        # Read another way, any part may be read.
        ("$states[0]", True),
        ("$states.input(", True),
        # When the state was entered and its retries do not name it.
        ("$states.context.State.EnteredTime", False),
        ("$states.context.State.RetryCount", False),
        ("$states.context.Execution.Input.a", False),
        ("$states.input.a", False),
    ],
)
def test_what_may_read_the_name_of_the_state(code, name):
    assert reads_the_name(code) == name


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
    "code, lone, path, operand",
    [
        ("$x", "x", True, True),
        (" $x ", "x", True, True),
        ("$states", "states", True, True),
        ("$a.b.c", None, True, True),
        # A filter makes more than the variable or the path.
        ("$x[0]", None, False, False),
        ("$a.b[0]", None, False, False),
        # The context and the root are no variables, but read as one operand.
        ("$", None, False, True),
        ("$count($x)", None, False, True),
        ("($v := 1; $v)", None, False, True),
        ("'s'", None, False, True),
        ("1.5", None, False, True),
        ("true", None, False, True),
        # A negative number, a built array or object, and an operator do not.
        ("-1", None, False, False),
        ("[1, 2]", None, False, False),
        ('{"k": 1}', None, False, False),
        ("$a + 1", None, False, False),
        # Parentheses in strings are text, and two groups are not one.
        ("('(') + (')')", None, False, False),
        ("$f(", None, False, False),
    ],
)
def test_shapes(code, lone, path, operand):
    assert lone_variable(code) == lone
    assert path_alone(code) == path
    assert atomic(code) == operand


@pytest.mark.parametrize(
    "code, loose",
    [
        ("$a or $b", True),
        ("$a ? 1 : 2", True),
        ("$v := 1", True),
        ("$a and $b", False),
        ("($a or $b)", False),
        ("$a = 'or'", False),
        ("$f(", True),
    ],
)
def test_looser_than_and(code, loose):
    assert looser_than_and(code) == loose


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


@pytest.mark.parametrize(
    "code, found",
    [
        ("$order.id", True),
        ("($order := 1; $order + 1)", True),
        # A string spells it, as the text of jsonata() may.
        ("$uppercase('$order')", True),
        ("$orders.id", False),
        # What code the parser cannot read does with it is not known.
        ("$x +", True),
    ],
)
def test_code_that_reads_binds_or_spells_a_name_mentions_it(code, found):
    assert mentions(code, "order") == found
