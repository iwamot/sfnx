"""What a JSONata expression does with names, read from its syntax tree
rather than from its text, so a name written in a string is told apart from
one the expression reads or binds."""

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from functools import cache

import jsonata
from jsonata.jexception import JException
from jsonata.parser import Parser

# A variable as JSONata spells one, found in the text of a string or a
# regular expression.
SPELLED = re.compile(r"\$([^\W\d]\w*)")


@dataclass(frozen=True)
class Facts:
    """reads holds the variables the expression reads, $states among them,
    that it does not bind itself for certain before it reads them: a
    function it calls by name counts, as JSONata's functions are variables.
    bound holds the names the expression binds, with :=, as the parameters
    of a function it defines, or with @ and # in a path, which take over a
    variable of the name. spelled holds the names a string or a regular
    expression in it writes as $name, as the text of jsonata() may: that is
    not a read, and a value put in the variable's place must leave it as it
    is. states holds how it reads $states, each read as the names of the
    fields that follow it for as long as they are plain names: ("input",)
    for $states.input, and () for $states itself or one read otherwise, as
    $states[0] is, which may be any part."""

    reads: frozenset[str]
    bound: frozenset[str]
    spelled: frozenset[str]
    states: frozenset[tuple[str, ...]]


@cache
def facts(code: str) -> Facts | None:
    """The facts of the code, or None where the parser cannot read it: the
    text of jsonata() may not be JSONata, which Step Functions rejects when
    it validates the definition, or may be JSONata newer than the parser."""
    tree = tree_of(code)
    if tree is None:
        return None
    bound: set[str] = set()
    spelled: set[str] = set()
    states: set[tuple[str, ...]] = set()
    # The $states that start a path, which the path reads.
    heads: set[int] = set()
    for node in nodes(tree):
        kind = node.type
        bound.update(binds(node))
        if kind == "path":
            assert node.steps is not None
            head, *steps = node.steps
            if head.type == "variable" and head.value == "states":
                heads.add(id(head))
                states.add(fields(head, steps))
        elif kind == "variable" and node.value == "states" and id(node) not in heads:
            states.add(())
        if kind == "bind":
            bound.add(text(node.lhs))
        elif kind == "lambda":
            bound.update(parameters(node))
        elif kind == "string":
            spelled.update(SPELLED.findall(text(node)))
        elif kind == "regex":
            assert isinstance(node.value, re.Pattern)
            pattern = node.value.pattern
            assert isinstance(pattern, str)
            spelled.update(SPELLED.findall(pattern))
    return Facts(
        frozenset(free(tree, frozenset())),
        frozenset(bound),
        frozenset(spelled),
        frozenset(states),
    )


def fields(head: Parser.Symbol, steps: list[Parser.Symbol]) -> tuple[str, ...]:
    """The plain names that follow $states in a path, none where the path
    filters $states itself."""
    if filtered(head):
        return ()
    names: list[str] = []
    for step in steps:
        if step.type != "name":
            break
        names.append(text(step))
    return tuple(names)


@cache
def tree_of(code: str) -> Parser.Symbol | None:
    """The syntax tree of the code, or None where the parser cannot read it."""
    try:
        return Parser().parse(code)
    except JException:
        return None


def filtered(node: Parser.Symbol) -> bool:
    """Whether a node carries filters or @ and # bindings of its own."""
    return bool(node.stages or node.predicate or binds(node))


def lone_variable(code: str) -> str | None:
    """The name of the variable the code is, where it is one alone."""
    tree = tree_of(code)
    return text(tree) if tree is not None and named(tree) else None


def path_alone(code: str) -> bool:
    """Whether the code is a variable, or a path of plain names from one,
    with no filters: what a hand-writer reads again where it is needed
    rather than binding it first."""
    tree = tree_of(code)
    if tree is None or tree.type != "path":
        return tree is not None and named(tree)
    assert tree.steps is not None
    head, *steps = tree.steps
    return named(head) and all(s.type == "name" and not filtered(s) for s in steps)


def named(node: Parser.Symbol) -> bool:
    """Whether a node is a variable with a name, unfiltered: not the context
    $ or the root $$."""
    return (
        node.type == "variable" and not filtered(node) and node.value not in {"", "$"}
    )


def atomic(code: str) -> bool:
    """Whether the code reads as one operand wherever it is written, needing
    no parentheses: a path alone, a string, a number that is not negative,
    true, false or null, a function call or a block, with no filters of its
    own. An array or an object it builds is put in parentheses, as one
    followed by a step reads otherwise."""
    if path_alone(code):
        return True
    tree = tree_of(code)
    if tree is None or filtered(tree):
        return False
    if tree.type == "number":
        return not code.lstrip().startswith("-")
    return tree.type in {"variable", "string", "value", "function", "block"}


def looser_than_and(code: str) -> bool:
    """Whether the code needs parentheses beside `and`: its outermost
    operator binds looser, as `or`, a conditional and := do, or the parser
    cannot read it."""
    tree = tree_of(code)
    return (
        tree is None
        or tree.type in {"condition", "bind"}
        or (tree.type == "binary" and tree.value == "or")
    )


def names_read(code: str) -> frozenset[str]:
    """The variables the code reads, or, where the parser cannot read it,
    every name its text spells, the most it can read."""
    found = facts(code)
    return found.reads if found is not None else frozenset(SPELLED.findall(code))


def always_read(code: str) -> frozenset[str]:
    """The variables the code reads every time it is evaluated, or none where
    the parser cannot read it: through the operands of an operator, but for
    the right of and and or, the test of a conditional, the arguments of a
    call, each expression of a block, the items of an array and the keys and
    values of an object written out, and what a path starts from. A branch
    of a conditional, the body of a function, a filter and the steps of a
    path after the first, which an empty sequence skips, may not be."""
    tree = tree_of(code)
    return frozenset() if tree is None else eager(tree, frozenset())


class Strictness(Enum):
    """Whether evaluating code evaluates a read of a variable: every time, only
    on some evaluations, as in a branch of a conditional, or never."""

    ALWAYS = "always"
    CONDITIONAL = "conditional"
    NEVER = "never"


def strictness(code: str, name: str) -> Strictness:
    """How the code reads the variable of a name, as always_read and
    names_read say."""
    if name in always_read(code):
        return Strictness.ALWAYS
    if name in names_read(code):
        return Strictness.CONDITIONAL
    return Strictness.NEVER


class UndefinedPropagation(Enum):
    """Whether code gives undefined, which fails an Assign or an Output, where
    a variable it reads is undefined: certainly, where the code is the
    variable itself, and not known otherwise, as $type() and & give a value
    for undefined."""

    PROPAGATES = "propagates"
    UNKNOWN = "unknown"


def propagation(code: str, name: str) -> UndefinedPropagation:
    """How an undefined variable of a name passes through the code."""
    if lone_variable(code) == name:
        return UndefinedPropagation.PROPAGATES
    return UndefinedPropagation.UNKNOWN


def eager(node: Parser.Symbol, bound: frozenset[str]) -> frozenset[str]:
    """What always_read says of node, with the names bound around it, which
    are not the variables of those names."""
    kind = node.type
    if kind == "variable":
        if node.value in bound or node.value in {"", "$"}:
            return frozenset()
        return frozenset({text(node)})
    if kind == "binary":
        assert node.lhs is not None and node.rhs is not None
        left = eager(node.lhs, bound)
        return left if node.value in {"and", "or"} else left | eager(node.rhs, bound)
    if kind == "condition":
        assert node.condition is not None
        return eager(node.condition, bound)
    if kind == "bind":
        assert node.rhs is not None
        return eager(node.rhs, bound)
    if kind == "block":
        assert node.expressions is not None
        found: frozenset[str] = frozenset()
        for expression in node.expressions:
            found |= eager(expression, bound)
            if expression.type == "bind":
                bound = bound | {text(expression.lhs)}
        return found
    if kind == "path":
        assert node.steps is not None
        return eager(node.steps[0], bound)
    if kind == "unary" and node.value == "-":
        assert node.expression is not None
        return eager(node.expression, bound)
    if kind == "unary" or kind == "function":
        parts = [
            *(node.expressions or []),
            *([node.procedure] if node.procedure is not None else []),
            *(node.arguments or []),
            *(part for pair in node.lhs_object or [] for part in pair),
        ]
        return frozenset().union(*(eager(part, bound) for part in parts))
    return frozenset()


@dataclass(frozen=True)
class EvaluationCount:
    """How often each evaluation of code evaluates a read of a variable: at
    least minimum times, and at most maximum, None where no bound is shown,
    as for a read in a function a comprehension runs for each item. Both are
    bounds on the safe side, not counts."""

    minimum: int
    maximum: int | None


def evaluations(code: str, name: str) -> EvaluationCount:
    """How often the code evaluates the variable of a name: at least once
    where it reads it every time, as always_read says, and at most as
    most_reads bounds it."""
    most = most_reads(code, name)
    return EvaluationCount(
        1 if name in always_read(code) else 0,
        None if most == math.inf else int(most),
    )


def shape(node: Parser.Symbol, bare: bool = False) -> object:
    """A node as what it means, apart from where it is written: its type, its
    value and the shapes of the nodes under it, field by field. Parentheses
    around one expression, with no filter of their own, are that
    expression. Bare, the node's own filters, grouping and sort terms, and
    how it keeps an array, are left out, which evaluating it before them
    evaluates anyway."""
    if wraps(node):
        assert node.expressions is not None
        return shape(node.expressions[0], bare)
    parts = []
    for key, value in sorted(vars(node).items()):
        if key in POSITIONAL or (bare and key in UNFILTERED):
            continue
        found = list(within(value))
        if found:
            parts.append((key, tuple(shape(n) for n in found)))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            parts.append((key, value))
    return (node.type, tuple(parts))


def wraps(node: Parser.Symbol) -> bool:
    """Whether a node is parentheses around one expression, with no filter
    of their own."""
    return (
        node.type == "block"
        and len(node.expressions or []) == 1
        and not (filtered(node))
    )


# What a bare shape leaves out: what a node does with its value once it has
# evaluated it.
UNFILTERED = frozenset(
    {"predicate", "stages", "group", "terms", "keep_array", "keep_singleton_array"}
)
# The fields of a node that say where it is written, or how the parser went
# about it, rather than what it means.
POSITIONAL = frozenset(
    {"_outer_instance", "position", "id", "lbp", "bp", "level", "_jsonata_lambda"}
)


def steps_of(node: Parser.Symbol) -> list[Parser.Symbol]:
    """The steps of a path, or the node itself as the one step of an
    expression that is no path."""
    if wraps(node):
        assert node.expressions is not None
        return steps_of(node.expressions[0])
    if node.type == "path":
        assert node.steps is not None
        return list(node.steps)
    return [node]


def evaluated_key(steps: list[Parser.Symbol], bare: bool) -> object:
    """What evaluating the first steps of a path evaluates: the steps before
    the last as they are, and the last as it is or bare, as a path read
    further evaluates a step before its filters and the steps after it."""
    *before, last = steps
    kept = shape(last, bare=True) if bare else ("filtered", shape(last))
    return (tuple(shape(step) for step in before), kept)


def unfiltered(node: Parser.Symbol) -> bool:
    """Whether a node has none of what a bare shape leaves out."""
    return not any(getattr(node, key, None) for key in UNFILTERED)


@cache
def evaluated_parts(code: str) -> frozenset[object] | None:
    """What evaluating the code evaluates: each node of its syntax tree, and
    each path read up to each of its steps, or None where the parser cannot
    read it."""
    tree = tree_of(code)
    if tree is None:
        return None
    found = set()
    for node in nodes(tree):
        if wraps(node):
            continue
        steps = steps_of(node)
        for k in range(1, len(steps) + 1):
            found.add(evaluated_key(steps[:k], bare=True))
            found.add(evaluated_key(steps[:k], bare=False))
    return frozenset(found)


def evaluates(code: str, part: str) -> bool | None:
    """Whether evaluating the code evaluates part: part's syntax tree is in
    the code's, as a node or as the start of a path, however it is spaced or
    parenthesized, as `$a.b` is in `$count($a.b[0].c)`; a part that filters
    its last step is in the code only with that filter. None where the
    parser cannot read either."""
    found, wanted = evaluated_parts(code), part_key(part)
    if found is None or wanted is None:
        return None
    return wanted in found


@cache
def part_key(part: str) -> object:
    """What evaluates says a code must evaluate to evaluate part, or None
    where the parser cannot read it."""
    tree = tree_of(part)
    if tree is None:
        return None
    steps = steps_of(tree)
    return evaluated_key(steps, bare=unfiltered(steps[-1]))


def same_code(first: str, second: str) -> bool:
    """Whether two codes have the same syntax tree, however they are spaced
    or parenthesized; not where the parser cannot read either."""
    one, other = tree_of(first), tree_of(second)
    return one is not None and other is not None and shape(one) == shape(other)


# The fields of a node that are evaluated once for each item: the filters and
# the grouping of a step, and the terms of a sort.
PER_ITEM = ("predicate", "stages", "group", "terms")


def most_reads(code: str, name: str) -> float:
    """How often, at most, the code reads the variable of a name each time
    it is evaluated: a bound on the safe side, not a count. A read in a
    function's body, a filter, a grouping, a sort term or a step of a path
    after the first may be evaluated once for each item, so it counts as
    unbounded, however many items there turn out to be. A conditional
    counts the branch that reads more. Where the parser cannot read the
    code, what it reads is not known, so it is unbounded."""
    tree = tree_of(code)
    if tree is None:
        return math.inf
    return most(tree, name, frozenset())


def most(node: Parser.Symbol, name: str, bound: frozenset[str]) -> float:
    """What most_reads says of node, with the names bound around it, which
    are not the variable of that name."""
    kind = node.type
    rest = bound | binds(node)
    if any(repeats(child, name, rest) for child in per_item(node)):
        return math.inf
    if kind == "variable":
        return 1 if node.value == name and name not in bound else 0
    if kind == "bind":
        assert node.rhs is not None
        return most(node.rhs, name, bound)
    if kind == "lambda":
        assert node.body is not None
        return math.inf if repeats(node.body, name, bound | parameters(node)) else 0
    if kind == "condition":
        assert node.condition is not None and node.then is not None
        otherwise = getattr(node, "_else", None)
        branches = [node.then, *([otherwise] if otherwise is not None else [])]
        return most(node.condition, name, bound) + max(
            most(branch, name, bound) for branch in branches
        )
    if kind == "block":
        assert node.expressions is not None
        found: float = 0
        inner = bound
        for expression in node.expressions:
            found += most(expression, name, inner)
            if expression.type == "bind":
                inner = inner | {text(expression.lhs)}
        return found
    if kind == "path":
        assert node.steps is not None
        head, *steps = node.steps
        found = most(head, name, rest)
        rest = rest | binds(head)
        for step in steps:
            if repeats(step, name, rest):
                return math.inf
            rest = rest | binds(step)
        return found
    return sum(most(child, name, rest) for child in children(node, *PER_ITEM))


def per_item(node: Parser.Symbol) -> Iterator[Parser.Symbol]:
    for key in PER_ITEM:
        yield from within(getattr(node, key, None))


def repeats(node: Parser.Symbol, name: str, bound: frozenset[str]) -> bool:
    """Whether node reads the variable of a name, where each reading may be
    one of many."""
    return name in set(free(node, bound))


# The integers a double holds exactly, which JSONata computes with as Python
# does.
EXACT = 2**53
# The functions a constant may call, which give the same value in JSONata
# and in jsonata-python for any value written out.
CONSTANT_FUNCTIONS = frozenset({"count", "exists", "not", "boolean", "append", "type"})
ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
ORDER = frozenset({"<", ">", "<=", ">="})


def constant(code: str) -> tuple[bool, object]:
    """The value of code that reads no variable and gives the same value
    wherever it is evaluated, as (True, value), or (False, None): numbers,
    strings, true, false and null written out, arithmetic, = and !=, the
    order of numbers, and, or, conditionals, arrays and objects written out,
    a key or an index written out, and the functions of CONSTANT_FUNCTIONS.
    Not where JSONata and jsonata-python may disagree, as on the order of
    strings, a number written as text, a number a double does not hold or
    one that is not whole, which is left for JSONata to write, nor on a
    placeholder the deployment replaces, nor where the value is undefined,
    which an Assign fails on, or fails to evaluate, as it fails there too."""
    found, written = evaluated(code)
    return (True, json.loads(written)) if found else (False, None)


@cache
def evaluated(code: str) -> tuple[bool, str]:
    """What constant says of code, with the value as JSON text, so that each
    caller gets a value of its own."""
    tree = tree_of(code)
    if tree is None or not closed(
        tree, strings=any(n.type == "string" for n in nodes(tree))
    ):
        return False, ""
    try:
        value = jsonata.Jsonata(code).evaluate(None)
    except (JException, ArithmeticError, TypeError, ValueError):
        return False, ""
    if not representable(value):
        return False, ""
    return True, json.dumps(plain(value), ensure_ascii=False)


def closed(node: Parser.Symbol, strings: bool) -> bool:
    """Whether node is made only of what constant evaluates, where strings
    says whether the code writes any string out, which the order would then
    compare."""
    kind = node.type
    if kind == "number":
        # A number the parser rounded to 2**53 may have been written larger.
        assert isinstance(node.value, (int, float))
        return abs(node.value) < EXACT
    if kind == "string":
        # A placeholder, which the deployment writes another text in place of.
        assert isinstance(node.value, str)
        return "${" not in node.value
    if kind == "value":
        return True
    if kind == "binary":
        if node.value not in ARITHMETIC | ORDER | {"=", "!=", "and", "or"}:
            return False
        if node.value in ORDER and strings:
            return False
    elif kind == "unary":
        if node.value not in {"-", "[", "{"}:
            return False
    elif kind == "function":
        procedure = node.procedure
        if procedure is None or procedure.type != "variable":
            return False
        if procedure.value not in CONSTANT_FUNCTIONS:
            return False
        return all(closed(a, strings) for a in node.arguments or [])
    elif kind == "path":
        # A path that starts from a name reads the input of the expression.
        assert node.steps is not None
        if node.steps[0].type == "name":
            return False
    elif kind == "name":
        # A key after the start of a path, which node.stages may filter.
        return all(closed(stage, strings) for stage in node.stages or [])
    elif kind not in {"condition", "block", "filter"}:
        return False
    return all(closed(child, strings) for child in children(node))


def representable(value: object) -> bool:
    """Whether a value is JSON that a double holds as jsonata-python gives it."""
    if isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return abs(value) < EXACT
    if isinstance(value, float):
        # A number that is not whole is left for JSONata to write.
        return value.is_integer() and abs(value) < EXACT
    if isinstance(value, list):
        return all(representable(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and representable(v) for k, v in value.items())
    return False


def plain(value: object) -> object:
    """A value as JSON: lists and dicts of the parser's kinds as plain ones,
    and a float that is whole as an int, as JSON writes both alike."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, float) and value.is_integer() and abs(value) < EXACT:
        return int(value)
    return value


def mentions(code: str, name: str) -> bool:
    """Whether code reads, binds or spells the variable of a name, as the
    text of jsonata() may, or the parser cannot read it, as what it does with
    the name is then not known."""
    found = facts(code)
    return found is None or name in found.reads | found.bound | found.spelled


# The parts of the context every state of an execution reads alike; the rest,
# as the State part, which names the state and says when it was entered,
# differs from state to state.
SHARED = frozenset({"Execution", "StateMachine", "Map"})


def states_read(code: str) -> frozenset[tuple[str, ...]]:
    """How the code reads $states, or, where the parser cannot read it and
    its text spells $states, as a whole."""
    found = facts(code)
    if found is not None:
        return found.states
    return frozenset({()}) if "states" in SPELLED.findall(code) else frozenset()


def shared(path: tuple[str, ...]) -> bool:
    return len(path) >= 2 and path[0] == "context" and path[1] in SHARED


def reads_own_states(code: str) -> bool:
    """Whether the code reads a part of $states that is the state's own, as
    its input, its result or its error output, or a part of the context
    that is not shared."""
    return any(not shared(path) for path in states_read(code))


def reads_own_context(code: str) -> bool:
    """Whether the code reads a part of the context that is not shared."""
    return any(
        not shared(path) and path[:1] in {(), ("context",)}
        for path in states_read(code)
    )


# Where the context holds the name of the state it is read in.
NAME = ("context", "State", "Name")


def reads_the_name(code: str) -> bool:
    """Whether the code may read the name of the state it is read in: $states,
    its context or the State part as a whole, or the Name in it. When the
    state was entered and how often it was retried do not name it."""
    return any(
        path == NAME[: len(path)] or path[:3] == NAME for path in states_read(code)
    )


def reads_state_name(code: str) -> bool:
    """Whether the code reads the State part of the context, which names the
    state it is read in."""
    return any(
        path in {(), ("context",)} or path[:2] == ("context", "State")
        for path in states_read(code)
    )


# What gives another value each time it is called: a random value, which
# every call gives anew; the time, which one evaluation of an expression reads
# once, however many calls it makes (measured); and $eval, which may call
# either and reads variables by names not written out.
COUNTED = frozenset({"random", "uuid"})
TIMED = frozenset({"millis", "now"})
UNRESOLVED = frozenset({"eval"})
CHANGING = COUNTED | TIMED | UNRESOLVED


@dataclass(frozen=True)
class Sensitivity:
    """What a move of code must keep for it to give what it gave.
    evaluation_count: each evaluation may give another value, as $random()
    and $uuid() do, so the move must keep how often it is evaluated.
    evaluation_instance: one evaluation of a {% %} gives one value however
    often it reads it, and another evaluation may give another, as $now()
    and $millis() do, so the move must keep it within the same evaluation,
    or between the same calls and waits. dependencies_unknown: what it reads
    is not written out, as with $eval, which reads variables by name and may
    do either of the others."""

    evaluation_count: bool = False
    evaluation_instance: bool = False
    dependencies_unknown: bool = False

    @property
    def varies(self) -> bool:
        """Whether evaluating it again may give another value."""
        return (
            self.evaluation_count
            or self.evaluation_instance
            or (self.dependencies_unknown)
        )


def sensitivity(code: str) -> Sensitivity:
    """What moving the code must keep, from the functions of CHANGING it
    reads, to call them or to pass them on, as to $map."""
    found = names_read(code)
    return Sensitivity(
        evaluation_count=bool(found & COUNTED),
        evaluation_instance=bool(found & TIMED),
        dependencies_unknown=bool(found & UNRESOLVED),
    )


def changes(code: str) -> bool:
    """Whether the code may give another value when evaluated again, as
    sensitivity says."""
    return sensitivity(code).varies


def free(node: Parser.Symbol, bound: frozenset[str]) -> Iterator[str]:
    """The variables node reads that bound does not hold. A block binds a
    name for the expressions after the one that binds it, a function its
    parameters for its body, and a step of a path its @ and # names for its
    own filters and the steps after it. A name bound anywhere else, as inside
    a function's arguments, still counts as read, which is the safe side:
    what reads a variable keeps its assignment and has no value written in."""
    kind = node.type
    if kind == "variable" and node.value not in bound and node.value not in {"", "$"}:
        yield text(node)
    # The fields read in their own way; the others, such as the filters of
    # a block or of a step, read what is bound around the node.
    handled: tuple[str, ...] = ()
    rest = bound | binds(node)
    if kind == "bind":
        assert node.rhs is not None
        yield from free(node.rhs, bound)
        handled = ("lhs", "rhs")
    elif kind == "lambda":
        assert node.body is not None
        yield from free(node.body, bound | parameters(node))
        handled = ("arguments", "body")
    elif kind == "block":
        assert node.expressions is not None
        inner = bound
        for expression in node.expressions:
            yield from free(expression, inner)
            if expression.type == "bind":
                inner = inner | {text(expression.lhs)}
        handled = ("expressions",)
    elif kind == "path":
        assert node.steps is not None
        for step in node.steps:
            yield from free(step, rest)
            rest = rest | binds(step)
        handled = ("steps",)
    for child in children(node, *handled):
        yield from free(child, rest)


def binds(node: Parser.Symbol) -> frozenset[str]:
    """The names a step of a path binds with @ and #."""
    return frozenset(n for n in (node.focus, node.index) if isinstance(n, str))


def parameters(node: Parser.Symbol) -> frozenset[str]:
    assert node.arguments is not None
    return frozenset(text(argument) for argument in node.arguments)


def text(node: Parser.Symbol | None) -> str:
    """The name of a variable node, or the text of a string node."""
    assert node is not None and isinstance(node.value, str)
    return node.value


def nodes(node: Parser.Symbol) -> Iterator[Parser.Symbol]:
    """A node and every node under it."""
    yield node
    for child in children(node):
        yield from nodes(child)


def children(node: Parser.Symbol, *skipped: str) -> Iterator[Parser.Symbol]:
    """The nodes right under a node, but for those in the fields skipped: the
    parser keeps them in its fields, alone, in lists, or in the key and
    value pairs of an object."""
    for key, value in vars(node).items():
        if key != "_outer_instance" and key not in skipped:
            yield from within(value)


def within(value: object) -> Iterator[Parser.Symbol]:
    if isinstance(value, Parser.Symbol):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from within(item)
