import math

import pytest

from sfnx.syntax import (
    EvaluationCount,
    Strictness,
    UndefinedPropagation,
    always_read,
    atomic,
    changes,
    constant,
    evaluates,
    evaluations,
    facts,
    lone_variable,
    looser_than_and,
    mentions,
    most_reads,
    names_read,
    path_alone,
    propagation,
    reads_context,
    reads_own_context,
    reads_own_states,
    reads_state_name,
    reads_the_name,
    same_code,
    sensitivity,
    strictness,
    unsupported_reference,
)


@pytest.mark.parametrize(
    "code, reads",
    [
        # Called without the argument the context gives.
        ("$string()", True),
        ("$exists($string()) ? 1 : 0", True),
        # The arguments written may take the second place and on, as a
        # number first does for $substring, and 'b' and 2 do for $split.
        ("$substring($s, 1)", True),
        ("$substring(1, 2)", True),
        ("$split('b', 2)", True),
        ("$fromMillis($n)", True),
        # -$n is a number or undefined, and undefined as the width leaves
        # the first place to the context.
        ("$pad($s, -$n)", True),
        # Called with ? or passed or bound as a value, its arguments are not
        # all written in the call.
        ("$pad(?, -10)", True),
        ("$map($xs, $string)", True),
        ("($f := $string; $f())", True),
        ("$map($xs, function($v) { $string() })", True),
        # What the parser cannot read.
        ("$foo(", True),
        # Every argument written, or ones that can only take their own
        # places: a string for $split's separator, a number for $pad's width.
        ("$string($x)", False),
        ("$substring($s, 1, 2)", False),
        ("$split($v, '')", False),
        ("$pad($s, -10)", False),
        ("$s ~> $pad(-10)", False),
        ("$formatNumber($x, '0.00')", False),
        ("$fromMillis($millis() + 3600000)", False),
        ("$match($text, /a/)", False),
        # A later step or a filter gives the item it reads.
        ("$xs.$string()", False),
        ("$xs[$string() = 'a']", False),
        ("$a + 1", False),
    ],
)
def test_what_may_read_the_context(code, reads):
    assert reads_context(code) is reads


@pytest.mark.parametrize(
    "code, reference",
    [
        # Where nothing gives an item: the top of the expression, a
        # function's body there, the first step of a path there.
        ("foo", "foo"),
        ("$", "$"),
        ("$.foo", "$"),
        ("foo[0]", "foo"),
        ("{'a': foo}", "foo"),
        ("$exists(foo) ? 1 : 0", "foo"),
        ("(function() { $exists(foo) })()", "foo"),
        ("$map($x, function($v) { foo })", "foo"),
        # $$ is rejected everywhere.
        ("$$", "$$"),
        ("$x[$$.a = 1]", "$$"),
        # A later step, a filter, a grouping and a sort give an item.
        ("$x.foo", None),
        ("$x.(foo)", None),
        ("$x[foo]", None),
        ("$x[$ > 1]", None),
        ("$x{$string(a): foo}", None),
        ("$x^(a)", None),
        # A function called without its argument reads the input, which is
        # undefined there, and is accepted; so is a $ in a string.
        ("$string()", None),
        ("'$ foo'", None),
        ("$a + 1", None),
        # What the parser cannot read.
        ("$foo(", None),
    ],
)
def test_what_step_functions_rejects_for_want_of_an_input(code, reference):
    assert unsupported_reference(code) == reference


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


@pytest.mark.parametrize(
    "code, read",
    [
        ("$a and $b", {"a"}),
        ("$c ? $a : $b", {"c"}),
        ("-($t + $t)", {"t"}),
        ('[$a, {"k": $b}]', {"a", "b"}),
        ("$f($a)", {"f", "a"}),
        ("($x := 1; $x + $a)", {"a"}),
        ("$a.b[$c = 1]", {"a"}),
        ("function($v) { $v + $a }", set()),
        ("$string($n) & '$m'", {"string", "n"}),
        # What code the parser cannot read reads is not known.
        ("$x +", set()),
    ],
)
def test_what_code_reads_every_time_it_is_evaluated(code, read):
    assert always_read(code) == read


@pytest.mark.parametrize(
    "code, most",
    [
        ("$y + 1", 0),
        ("$x", 1),
        ("[$x, $x]", 2),
        ("$f($x)", 1),
        ("$c ? $x : 1", 1),
        ("$c ? $x", 1),
        ("$x ? $x : $x", 2),
        ("($v := $x; $v + $v)", 1),
        ("($x := 1; $x + $x)", 0),
        ("$x.a", 1),
        # Each item reads it again: in a function, a filter, a grouping, a
        # sort term, a step of a path after the first.
        ("$map($r, function($i) { $x + $i })", math.inf),
        ("$r[$x > 0]", math.inf),
        ("$r{$x: 1}", math.inf),
        ("$r^($x)", math.inf),
        ("$r.($x)", math.inf),
        # A parameter of the name is not the variable.
        ("$map($r, function($x) { $x })", 0),
        # What code the parser cannot read reads is not known.
        ("$y +", math.inf),
    ],
)
def test_how_often_code_may_read_a_variable(code, most):
    assert most_reads(code, "x") == most


@pytest.mark.parametrize(
    "code, value",
    [
        ("(2 - 2) * -1", 0),
        ("$count([1, 2])", 2),
        ("{'a': [1, 2]}.a[1]", 2),
        ("false ? 1 : 2", 2),
        ("1.5 * 2", 3),
        ("2 > 1", True),
        ("$append([1], [2])", [1, 2]),
    ],
)
def test_code_that_reads_nothing_is_its_value(code, value):
    assert constant(code) == (True, value)


@pytest.mark.parametrize(
    "code",
    [
        "$x + 1",
        "$states.input",
        "a",
        "$random()",
        # JSONata and jsonata-python may write or order these apart.
        "$string(1)",
        "'a' & 'b'",
        "'a' < 'b'",
        "1 / 3",
        "9007199254740993 - 1",
        # The deployment writes another text in the place of a placeholder.
        "'${Name}' = 'a'",
        # Undefined, and a failure, are left to happen where they are.
        "{'a': 1}.b",
        "1 / 0",
    ],
)
def test_code_that_may_differ_or_reads_something_has_no_value_here(code):
    assert constant(code) == (False, None)


@pytest.mark.parametrize(
    "code, count, instance, unknown",
    [
        ("$a + 1", False, False, False),
        ("$random()", True, False, False),
        ("$uuid() & $now()", True, True, False),
        ("$millis()", False, True, False),
        ("$map($xs, $random)", True, False, False),
        ("$eval('1')", False, False, True),
    ],
)
def test_what_moving_code_must_keep(code, count, instance, unknown):
    found = sensitivity(code)
    assert (
        found.evaluation_count,
        found.evaluation_instance,
        found.dependencies_unknown,
    ) == (count, instance, unknown)
    assert found.varies == (count or instance or unknown)


@pytest.mark.parametrize(
    "code, how",
    [
        ("$x", Strictness.ALWAYS),
        ("$y", Strictness.NEVER),
        ("$x ? $a : $b", Strictness.ALWAYS),
        ("$c ? $x : $b", Strictness.CONDITIONAL),
        ("$x or $a", Strictness.ALWAYS),
        ("$a or $x", Strictness.CONDITIONAL),
        ("$not($x)", Strictness.ALWAYS),
        ("$x < $a and $a < $b", Strictness.ALWAYS),
        ("$a < $b and $b < $x", Strictness.CONDITIONAL),
        ("$x + 1", Strictness.ALWAYS),
        ("$x.k", Strictness.ALWAYS),
        ("$count($x)", Strictness.ALWAYS),
        ("$max([$x, 1])", Strictness.ALWAYS),
        # A divisor written as a variable is tested first; the dividend is
        # read only where it is not zero.
        ("$b = 0 ? $error('division by zero') : $x / $b", Strictness.CONDITIONAL),
        ("$map($b, function($a) { $x })", Strictness.CONDITIONAL),
        ("[$a, $x]", Strictness.ALWAYS),
        ('{"k": $x}', Strictness.ALWAYS),
        ("'n: ' & $string($x)", Strictness.ALWAYS),
        ("[$a, $b]", Strictness.NEVER),
    ],
)
def test_how_code_reads_a_variable(code, how):
    assert strictness(code, "x") is how


@pytest.mark.parametrize(
    "code, how",
    [
        ("$x", UndefinedPropagation.PROPAGATES),
        ("$x + 1", UndefinedPropagation.UNKNOWN),
        ("$type($x)", UndefinedPropagation.UNKNOWN),
        ("$y", UndefinedPropagation.UNKNOWN),
    ],
)
def test_how_undefined_passes_through_code(code, how):
    assert propagation(code, "x") is how


@pytest.mark.parametrize(
    "code, count",
    [
        ("$x", EvaluationCount(1, 1)),
        ("$y", EvaluationCount(0, 0)),
        ("$c ? $x : 1", EvaluationCount(0, 1)),
        ("[$x, $x]", EvaluationCount(1, 2)),
        ("$map($r, function($i) { $x })", EvaluationCount(0, None)),
    ],
)
def test_how_often_code_evaluates_a_read(code, count):
    assert evaluations(code, "x") == count


@pytest.mark.parametrize(
    "code, part, found",
    [
        ("$a.b + 1", "$a.b", True),
        # The text holds it; the syntax tree does not.
        ("$a.bc + 1", "$a.b", False),
        # The syntax tree holds it; the text does not.
        ("$x * ($a + 1)", "$a+1", True),
        ("[$map($a.b, function($v) { $v })]", "($a.b)", True),
        ("($v := $a + 1; $v)", "$a + 1", True),
        # A path read further evaluates its start first, filters and all.
        ("$count($a.b.c)", "$a.b", True),
        ("$count($a.b[0].c)", "$a.b", True),
        ("$a.c.b", "$a.b", False),
        # A part that filters its last step is there only with the filter.
        ("$count($a.b[0].c)", "$a.b[0]", True),
        ("$count($a.b) + $a.b[1]", "$a.b[0]", False),
        ("$a +", "$a", None),
    ],
)
def test_what_evaluating_code_evaluates(code, part, found):
    assert evaluates(code, part) is found


def test_the_same_code_however_it_is_written():
    assert same_code("$a+1", " ($a + 1) ")
    assert not same_code("$a + 1", "1 + $a")
    assert not same_code("$a +", "$a +")
