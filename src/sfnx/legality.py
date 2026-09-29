"""Whether a pass may move, copy or drop what a state evaluates, so that
each execution evaluates the same expressions, with the same values of the
variables, under the same catchers and retriers, and before or after the same
calls, as it did before the pass, but for what docs/language.md lists as
differences. Each check gives the reason it refuses, a Reject, or None where
it finds nothing against the move; the passes still decide what to move and
how to write it.

A reason is one of two kinds. A proof is refused where a property the move
needs could not be shown, as that a failure still ends the execution. A
blanket refuses every expression of a kind, as every one that reads the
state's own input, whether or not the move changes what it reads; these are
the checks later work is to replace with proofs."""

import json
import logging
from dataclasses import dataclass, replace
from enum import Enum

from sfnx.errors import EVERYTHING
from sfnx.expressions import (
    ATOM,
    WRITTEN,
    Expr,
    code_of,
    literal,
    template_of,
    written,
)
from sfnx.syntax import (
    Strictness,
    UndefinedPropagation,
    evaluations,
    facts,
    lone_variable,
    names_read,
    occurrences,
    path_alone,
    propagation,
    reads_own_context,
    reads_own_states,
    reads_state_name,
    same_code,
    sensitivity,
    strictness,
)

# The errors a catcher or a retrier matches when a state's Assign or Output
# fails.
EVALUATION_ERRORS = frozenset({EVERYTHING, "States.QueryEvaluationError"})

# Each refusal, at DEBUG, for whoever follows why a pass left a state where it
# is: logging.getLogger("sfnx.legality").
LEGALITY = logging.getLogger("sfnx.legality")

PROOF = "proof"
BLANKET = "blanket"


class Reject(Enum):
    """Why a check refuses a move: its kind, and what it could not show."""

    HANDLER_TAKES_FAILURE = (
        PROOF,
        (
            "a Catch or a retrier of the state would take a failure of the value, "
            "which would end the execution where it was"
        ),
    )
    UNRESOLVED_READ = (
        PROOF,
        (
            "a variable the value reads is assigned something that is no one "
            "expression to read in its place"
        ),
    )
    NAME_CAPTURE = (
        PROOF,
        (
            "an expression where the value would go binds a name it reads, or "
            "spells the variable in a string"
        ),
    )
    OTHER_HANDLERS = (
        PROOF,
        (
            "the state's catchers take a failure of the value where they are not "
            "those of the try bodies the value is in"
        ),
    )
    EXPOSES_PARTIAL_ASSIGN = (
        PROOF,
        (
            "a catcher's way reads a variable assigned before a value that may "
            "fail, which a failing Assign would leave unassigned"
        ),
    )
    DEPENDENCIES_UNKNOWN = (
        PROOF,
        (
            "what the value reads is not written out, as with $eval or a "
            "jsonata() expression that is not settled"
        ),
    )
    CHANGES_EVALUATION_COUNT = (
        BLANKET,
        "the value may give another value each time it is evaluated, as $random() does",
    )
    CHANGES_EVALUATION_INSTANCE = (
        BLANKET,
        "the value reads the time, which another evaluation may read otherwise",
    )
    DROPS_FAILURE = (
        PROOF,
        (
            "a value that may fail or be undefined would be evaluated where its "
            "failure may not end what it ended, as in a branch, or where "
            "undefined may pass through"
        ),
    )
    CROSSES_EFFECT = (
        PROOF,
        (
            "the value reads the time, and would be evaluated on the other side "
            "of a call or a wait"
        ),
    )
    STATE_CONTEXT_CHANGES = (
        BLANKET,
        "the value reads a part of $states that is the state's own",
    )
    STATE_NAME_READ = (
        BLANKET,
        "the value reads the State part of the context, which names the state",
    )

    @property
    def kind(self) -> str:
        return self.value[0]


class Differs(Enum):
    """What of $states is not the same where a value would go as where it is:
    every part that is a state's own (its input, its result, its error
    output, and the parts of the context not every state shares), the parts
    of the context not every state shares, or only the State part, which
    names the state."""

    STATES = "states"
    CONTEXT = "context"
    STATE = "state"


def refused(reason: Reject | None, where: str) -> bool:
    """Whether a check refused, noting its reason where DEBUG is on."""
    if reason is None:
        return False
    LEGALITY.debug("%s: %s (%s)", where, reason.name, reason.kind)
    return True


def failure_escapes(state: dict[str, object], values: object) -> Reject | None:
    """Whether values may go in the Assign or the Output of a state and fail
    there as they would where they are: no catcher or retrier of the state
    takes a failure of an expression, as may_fold says, or nothing in them
    can fail or be undefined, as failsafe says."""
    if may_fold(state) or failsafe(values):
        return None
    return Reject.HANDLER_TAKES_FAILURE


def context_invariant(codes: list[str], differs: Differs) -> Reject | None:
    """Whether code reads the same of $states where a value would go as where
    it is, given what of $states differs between the two."""
    if differs is Differs.STATE:
        return Reject.STATE_NAME_READ if any(map(reads_state_name, codes)) else None
    reads = reads_own_states if differs is Differs.STATES else reads_own_context
    return Reject.STATE_CONTEXT_CHANGES if any(map(reads, codes)) else None


def stable(values: list[Expr] | list[str]) -> Reject | None:
    """Whether evaluating each value again gives what it gave, as its
    Sensitivity says; an Expr counts the code of a jsonata() expression that
    is not settled, and the code alone does not. Any sensitivity refuses."""
    for value in values:
        found = value.sensitivity if isinstance(value, Expr) else sensitivity(value)
        if found.dependencies_unknown:
            return Reject.DEPENDENCIES_UNKNOWN
        if found.evaluation_count:
            return Reject.CHANGES_EVALUATION_COUNT
        if found.evaluation_instance:
            return Reject.CHANGES_EVALUATION_INSTANCE
    return None


def failure_kept(code: str, values: dict[str, Expr]) -> Reject | None:
    """Whether code read in place of the assignments of values, by the names
    it reads them as, fails wherever one of them would have failed: each
    that may fail or be undefined is the code itself, whose undefined fails
    as the Assign would, or is never undefined and read every time the code
    is evaluated."""
    # Where two may fail, the other's failure may come first: allowed by
    # AD-FAILURE-ORDER.
    for name, value in values.items():
        if failsafe(value):
            continue
        if propagation(code, name) is UndefinedPropagation.PROPAGATES:
            continue
        if value.defined and strictness(code, name) is Strictness.ALWAYS:
            continue
        return Reject.DROPS_FAILURE
    return None


@dataclass(frozen=True)
class Field:
    """The code of a field a value would be read in: whether it is evaluated
    before the state's call or wait, or in a state that makes none, and
    whether the state may evaluate it more than once, as a retrier runs a
    state again and a Map's ItemSelector runs for each item."""

    code: str
    before: bool
    repeated: bool = False


# The fields of the states that call or wait, evaluated before the call or
# the wait; their other fields are evaluated after it. A Map evaluates its
# ItemSelector for each item.
BEFORE = {
    "Task": frozenset(
        {"Arguments", "Credentials", "TimeoutSeconds", "HeartbeatSeconds"}
    ),
    "Parallel": frozenset({"Arguments"}),
    "Map": frozenset({"Items", "MaxConcurrency", "ToleratedFailureCount"}),
    "Wait": frozenset({"Seconds", "Timestamp"}),
}
EACH_ITEM = {"Map": frozenset({"ItemSelector"})}
# The fields that hold no expression evaluated where the state is.
NOT_EVALUATED = frozenset(
    {"Type", "Comment", "Next", "End", "Default", "Retry", "Branches", "ItemProcessor"}
)


def fields_of(state: dict[str, object]) -> list[Field]:
    """The code of each expression a state evaluates, with where it is
    evaluated: a Choice's rules, a catcher's Assign and Output among them. A
    state with a retrier may evaluate any of them again."""
    kind = state["Type"]
    assert isinstance(kind, str)
    retried = kind in {"Task", "Parallel", "Map"} and "Retry" in state
    found = []
    for key, value in state.items():
        if key in NOT_EVALUATED:
            continue
        before = kind not in BEFORE or key in BEFORE[kind]
        repeated = retried or key in EACH_ITEM.get(kind, frozenset())
        found += [Field(code, before, repeated) for code in expressions_in(value)]
    return found


def evaluated_as_before(value: Expr, name: str, fields: list[Field]) -> Reject | None:
    """Whether a value that changes on evaluation, read in place of the
    variable of a name in fields, gives what the one evaluation of its
    assignment gave: one that may give another value each time is read at
    most once, and the time is read in one field, which one evaluation reads
    once however often it reads it (measured), on the same side of the
    state's call or wait. What it reads that is not written out refuses. A
    field the state evaluates more than once may read neither."""
    found = value.sensitivity
    if not found.varies:
        return None
    if found.dependencies_unknown:
        return Reject.DEPENDENCIES_UNKNOWN
    reading = [f for f in fields if name in names_read(f.code)]
    if not reading:
        return None
    if any(f.repeated for f in reading):
        return (
            Reject.CHANGES_EVALUATION_COUNT
            if found.evaluation_count
            else Reject.CHANGES_EVALUATION_INSTANCE
        )
    if found.evaluation_count:
        counts = [evaluations(f.code, name).maximum for f in reading]
        if None in counts or sum(c or 0 for c in counts) > 1:
            return Reject.CHANGES_EVALUATION_COUNT
    # Read in another state between the same calls and waits: allowed by
    # AD-TIMING-WITHIN-EFFECT-INTERVAL.
    if found.evaluation_instance:
        if len(reading) > 1:
            return Reject.CHANGES_EVALUATION_INSTANCE
        if not all(f.before for f in reading):
            return Reject.CROSSES_EFFECT
    return None


def failure_seen_before(
    value: object, holder: dict[str, object], state: dict[str, object]
) -> bool:
    """Whether a failure of a value in holder's Assign is one the state has
    already met: the same state, so the same catchers and retriers, and the
    same values of the variables, as every field of a state reads those from
    before it, evaluates the same code as a whole field before its call or
    wait, whose failure fails the state there, so the value fails nowhere
    that does not. Only for a value that is never undefined: what a state
    does with a field that is undefined is not measured for every field."""
    if holder is not state or not isinstance(value, Expr) or not value.defined:
        return False
    return any(
        f.before and not f.repeated and same_code(f.code, value.code)
        for f in fields_of(state)
        if state["Type"] in BEFORE
    )


def read_at_most_once(codes: list[str], name: str) -> Reject | None:
    """Whether the codes, each evaluated once, evaluate a read of the variable
    of a name at most once between them, as evaluations bounds it: a read in
    the function a comprehension runs for each item may be one of many."""
    counts = [evaluations(code, name).maximum for code in codes]
    if None in counts or sum(c or 0 for c in counts) > 1:
        return Reject.CHANGES_EVALUATION_COUNT
    return None


def held_elsewhere(template: object, codes: list[str]) -> bool:
    """Whether another expression holds the expression of a value that may
    fail, as reading the variable in its place writes it: a value written
    out or a variable alone has nothing to fail, whatever holds it, nor has
    one that can neither fail nor be undefined. The syntax trees are
    compared where the text holds it, so $a.b is not held by $a.bc; one the
    parser cannot read may hold anything."""
    if failsafe(template):
        return False
    found = [code.strip() for code in expressions_in(template)]
    return any(
        not (lone_variable(code) is not None or code in NEVER_FAILS)
        and sum(code in other and occurrences(other, code) != 0 for other in codes) > 1
        for code in found
    )


# What a state reads of its own that is always there: the result of a Task,
# a Parallel or a Map, and the error output of a catcher.
NEVER_FAILS = frozenset({"$states.result", "$states.errorOutput"})


def resolve_reads(
    assign: dict[str, object], codes: list[str] | None, where: str
) -> dict[str, Expr] | None:
    """The values an Assign gives the variables code reads, each as the
    expression to read in its place, as assigned_value says; of every name
    it assigns without codes. None, noted as refused, where one is no one
    expression."""
    reads = None if codes is None else {r for c in codes for r in names_read(c)}
    found = {
        n: assigned_value(v) for n, v in assign.items() if reads is None or n in reads
    }
    values = {n: v for n, v in found.items() if v is not None}
    if len(values) < len(found):
        refused(Reject.UNRESOLVED_READ, where)
        return None
    return values


def captures(holder: dict[str, object], values: dict[str, Expr]) -> Reject | None:
    """Whether writing values in place of their variables in holder would
    change what they read, as reads_as says of each."""
    if all(reads_as(holder, n, v) for n, v in values.items()):
        return None
    return Reject.NAME_CAPTURE


def may_fold(state: dict[str, object]) -> bool:
    """Whether what follows a Task, a Parallel or a Map can go in its Assign
    or its Output: its failure there ends the execution, as the failure of a
    state after it would."""
    return not takes_evaluation(state, "Catch") and not retries_evaluation(state)


def retries_evaluation(state: dict[str, object]) -> bool:
    """Whether a retrier of a state takes a failure of its Assign or its
    Output, which would call the state again."""
    return takes_evaluation(state, "Retry")


def takes_evaluation(state: dict[str, object], field: str) -> bool:
    """Whether a catcher or a retrier of a state takes a failure of its
    Assign or its Output: only States.ALL and States.QueryEvaluationError
    do (measured)."""
    handlers = state.get(field, [])
    assert isinstance(handlers, list)
    errors = {e for handler in handlers for e in handler["ErrorEquals"]}
    return bool(errors & EVALUATION_ERRORS)


def failsafe(field: object) -> bool:
    """Whether nothing in a field fails or is undefined, so neither a Catch
    nor a retrier of the state that holds it has a failure of it to take: a
    value written out, and expressions that never are undefined and fail for
    no value. Where one reads a variable the state assigns as the expression
    the state assigns it, it fails only where the state's Assign fails."""
    if isinstance(field, Expr):
        return field.defined and field.total
    if isinstance(field, dict):
        return all(failsafe(v) for v in field.values())
    if isinstance(field, list):
        return all(failsafe(v) for v in field)
    return not (isinstance(field, str) and field.startswith("{%"))


def assigned_value(template: object) -> Expr | None:
    """What an Assign writes for a variable, as an expression to read in its
    place: an expression, or a value written out, whose JSON is JSONata too.
    An object or an array with expressions among its values is no one
    expression. An expression keeps what the field's Expr knows of it, as
    whether it may fail or be undefined; the names it reads are those of
    its code, $states and functions among them, and it binds as tightly as
    an atom only where it is a path alone, which is what may be written in
    place of a variable read more than once."""
    if isinstance(template, Expr) and code_of(template) is not None:
        code = template.code
        precedence = ATOM if path_alone(code) else WRITTEN
        return replace(
            template, variables=frozenset(names_read(code)), precedence=precedence
        )
    template = template_of(template)
    if isinstance(template, (dict, list)):
        if not written(template):
            return None
        code = json.dumps(template, ensure_ascii=False, default=template_of)
        return Expr(code, template, defined=True, total=True)
    return literal(template)


def reads_as(state: dict[str, object], name: str, value: Expr) -> bool:
    """Whether a value can be written where a state reads the variable of a
    name: no expression in the state binds that name, or a name the value
    reads, which would take them over, no string in one spells the name, as
    the text of jsonata() may, which is not a read and stays as it is, and
    the parser reads each, as what one it cannot read binds is not known."""
    names = {name, *(read for read in names_read(value.code))}
    found = [facts(code) for code in expressions_in(state)]
    return all(
        f is not None and name not in f.spelled and not f.bound & names for f in found
    )


def expressions_in(node: object) -> list[str]:
    """The JSONata of the {% %} strings in a state, past its Comment."""
    node = template_of(node)
    if isinstance(node, dict):
        return [c for k, v in node.items() if k != "Comment" for c in expressions_in(v)]
    if isinstance(node, list):
        return [c for item in node for c in expressions_in(item)]
    if isinstance(node, str) and node.startswith("{%") and node.endswith("%}"):
        return [node[2:-2]]
    return []
