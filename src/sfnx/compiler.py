"""Compile @state_machine functions to Amazon States Language."""

import ast
import copy
import itertools
import json
import logging
import operator
import re
import symtable
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from importlib.util import decode_source
from pathlib import Path
from types import FunctionType
from typing import Literal, TypeGuard

from sfnx.diagnostics import CompileError
from sfnx.errors import EVERYTHING, caught, raised, retriers
from sfnx.expressions import (
    ADD,
    ATOM,
    COMPARE,
    WRITTEN,
    Expr,
    array,
    binary,
    call,
    code_of,
    expression,
    literal,
    obj,
    operand,
    spelling,
    template_of,
    variable,
    written,
)
from sfnx.expressions import field as step
from sfnx.expressions import index as index_expr
from sfnx.graph import Graph, renaming
from sfnx.jsontypes import (
    ARRAY,
    BOOLEAN,
    ERROR_OUTPUT,
    NUMBER,
    OBJECT,
    STRING,
    AnnotationError,
    Type,
    annotation,
    article,
    of,
    union,
)
from sfnx.legality import (
    EVALUATION_ERRORS,
    Differs,
    Read,
    Reject,
    assigned_value,
    captures,
    context_invariant,
    dependencies_known,
    evaluated_as_before,
    evaluated_once,
    expressions_in,
    failsafe,
    failure_escapes,
    failure_kept,
    fields_of,
    read_at_most_once,
    refused,
    resolve_reads,
    retries_evaluation,
    takes_evaluation,
)
from sfnx.locations import PREFIX, Locations, Origin
from sfnx.module import Module, holds, module, qualified
from sfnx.syntax import (
    Strictness,
    atomic,
    constant,
    lone_variable,
    looser_than_and,
    mentions,
    names_read,
    path_alone,
    reads_the_name,
    sensitivity,
    strictness,
)
from sfnx.translate import (
    StateCall,
    Translator,
    direct_call,
    text,
    unpacked,
)

# Step Functions reserves $states for its own variables.
MAX_VARIABLE = 80
# The field that marks a state a `# state:` comment names, which the passes
# keep as it is under that name, and which the definition does not show.
NAMED = "sfnx.name"
# The variables the named assignment itself assigns in its Pass, which stay
# there though nothing reads them, as the name names that assignment; what
# other statements add to the Pass goes as it would anywhere.
NAMED_ASSIGNS = "sfnx.named_assigns"
# Statements that make no state of their own, so a name on them would name
# none: the states their bodies make are named in the body.
UNNAMED_STATEMENTS = (
    ast.Try,
    ast.Pass,
    ast.FunctionDef,
    ast.Break,
    ast.Continue,
    ast.Global,
    ast.Nonlocal,
    ast.Import,
    ast.ImportFrom,
)
# How often a loop is compiled again with wider types before a type that keeps
# changing is taken as unknown.
MAX_WIDENING = 8
MAX_WAIT = 99_999_999
# RFC 3339 with an uppercase T and Z, as Wait requires.
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
# The functions whose calls are states of their own.
STATE_CALLS = ("task", "activity", "parallel", "inline_map", "distributed_map")


def machine_options(decorator: ast.expr, context: Module) -> dict[str, object]:
    """The TimeoutSeconds of a @state_machine decorator."""
    if not isinstance(decorator, ast.Call):
        return {}
    if decorator.args:
        raise CompileError(
            "pass the timeout by keyword: @state_machine(timeout=300)", decorator
        )
    options: dict[str, object] = {}
    for keyword in decorator.keywords:
        if keyword.arg != "timeout":
            raise CompileError(
                "@state_machine takes only timeout; set the rest when you deploy",
                keyword,
            )
        value = holds(keyword.value, context.constants)
        if not (
            isinstance(value, ast.Constant)
            and type(value.value) is int
            and value.value > 0
        ):
            raise CompileError(
                "timeout is a positive number of seconds: @state_machine(timeout=300)",
                value,
            )
        options["TimeoutSeconds"] = value.value
    return options


def is_machine(decorator: ast.expr, names: dict[str, str]) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return qualified(target, names) == "sfnx.state_machine"


def machines(
    tree: ast.Module, context: Module
) -> list[tuple[ast.FunctionDef, dict[str, object]]]:
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        marked = [d for d in node.decorator_list if is_machine(d, context.names)]
        if not marked:
            continue
        if any(function.name == node.name for function, _ in found):
            # Python keeps the last definition, which would drop the other.
            raise CompileError(
                f"another state machine is named {node.name}; give each its own name",
                node,
            )
        if isinstance(node, ast.AsyncFunctionDef):
            raise CompileError(
                "a state machine is a plain function; write def instead of async def",
                node,
            )
        if len(node.decorator_list) > 1:
            raise CompileError(
                "a state machine takes no other decorators; remove them", node
            )
        found.append((node, machine_options(marked[0], context)))
    if not found:
        raise CompileError(
            "no state machine here; mark the function with @state_machine "
            "(from sfnx import state_machine)",
            tree,
        )
    return found


@dataclass
class Flow:
    """Where control is at the end of a branch: the variables bound on the way,
    those bound on some paths to it only, the functions defined on it, and the
    transitions waiting for the next state."""

    bindings: dict[str, Expr]
    declared: dict[str, Type]
    tails: list[tuple[dict[str, object], str]]
    functions: dict[str, ast.FunctionDef]
    partial: set[str]


@dataclass
class Loop:
    """A loop being compiled: the types at its head, the functions defined
    before it, and the flows that leave its body early through break and
    continue."""

    head: dict[str, Type | None]
    functions: frozenset[str] = frozenset()
    breaks: list[Flow] = field(default_factory=list)
    continues: list[Flow] = field(default_factory=list)


@dataclass
class Expansion:
    """A function called directly, whose body is being compiled in place of
    the call: the statement that calls it, the call, the loops open around the
    call, which its break and continue cannot leave, and the flows its returns
    leave with."""

    name: str
    statement: ast.stmt
    call: ast.Call
    depth: int
    names: set[str]
    returns: list[Flow] = field(default_factory=list)


@dataclass
class Result:
    """A Task, Parallel or Map just added: the state, the value each variable
    its Assign gives the result takes, where in the source the state comes
    from, and its own comment. A return right after it can be its Output."""

    state: dict[str, object]
    values: dict[str, Expr]
    origins: list[Origin]
    remark: str | None


@dataclass
class Handler:
    """An except clause. Each Catch that leads to it adds the flow from the
    state that failed."""

    node: ast.ExceptHandler
    errors: list[str]
    variable: str | None
    flows: list[Flow] = field(default_factory=list)


# The try bodies each state is in, outermost first, as the translation
# records them; a pass that moves statements into a state narrows its own.
Enclosing = dict[str, tuple[list[Handler], ...]]


@dataclass
class Checkpoint:
    """Everything a loop attempt can change, to try it again with wider types."""

    states: set[str]
    tails: list[tuple[dict[str, object], str]]
    start: str | None
    bindings: dict[str, Expr]
    declared: dict[str, Type]
    partial: set[str]
    pending: dict[str, Expr]
    pending_first: str | None
    pending_node: ast.AST | None
    pending_origins: list[Origin]
    pending_remarks: list[str]
    remark: str | None
    # The state a return can end with, its fields at the checkpoint, and the
    # pending values as it reads them.
    result: Result | None
    resulted: dict[str, object]
    folded: dict[str, Expr]
    hidden: set[str]
    names: set[str]
    labels: set[str]
    functions: dict[str, ast.FunctionDef]
    returns: int
    catches: list[int]
    depth: tuple[int, int, int]


class Scope:
    """Compile the body of one function into a graph."""

    def __init__(
        self,
        graph: Graph,
        bindings: dict[str, Expr],
        module: Module,
        parameters: set[str],
        taken: set[str],
        hidden: set[str],
        assigned: set[str],
        outer: set[str],
    ):
        self.graph = graph
        self.bindings = bindings
        self.module = module
        self.parameters = parameters
        self.partial: set[str] = set()
        self.translator = Translator(
            bindings,
            module.names,
            module.spellings,
            # What the module assigns outside the machine is read where this
            # scope does not assign the name itself, as Python reads a global.
            {
                name: found
                for name, found in module.constants.items()
                if name not in assigned and name not in parameters
            },
            self.partial,
            self.compose,
            module.typed,
        )
        self.translator.is_function = lambda name: (
            name in self.functions or name in module.functions
        )
        self.translator.returned = self.returned_expression
        # Functions defined in the body, for parallel() to run.
        self.functions: dict[str, ast.FunctionDef] = {}
        # What this scope assigns, and what enclosing scopes do: Step Functions
        # lets no Parallel branch or Map assign a variable its outside assigns.
        self.assigned = assigned
        self.outer = outer
        # The types of what the scope returns, for the result of a parallel().
        self.returns: list[Type | None] = []
        # The type an annotation declared for a variable holds for its later
        # assignments too, unless their values have a type of their own.
        self.declared: dict[str, Type] = {}
        # A declaration without a value, name: type, is the type of each later
        # assignment of the name, as if written on it.
        self.announced: dict[str, Type] = {}
        # Independent assignments wait here to share one Pass.
        self.pending: dict[str, Expr] = {}
        # The name assigned first, which names their Pass: a name assigned
        # again moves to where it is written now.
        self.pending_first: str | None = None
        self.pending_node: ast.AST | None = None
        self.pending_origins: list[Origin] = []
        # The comments above the pending assignments, for the Pass they share,
        # and the comment of the statement being compiled, for the first state
        # it adds.
        self.pending_remarks: list[str] = []
        self.remark: str | None = None
        # The name a `# state:` comment gives the statement being compiled,
        # until the state it makes takes it, and the name of the Pass of the
        # pending assignments, which a named assignment starts.
        self.naming: tuple[str, ast.stmt] | None = None
        self.pending_name: str | None = None
        self.pending_named: tuple[ast.stmt, set[str]] | None = None
        # The body of a while True leads back to the first state it adds, so
        # until it adds one, what it assigns cannot go in a state before it.
        self.opening = False
        # What can hold the assignments that follow, until a state or a flush
        # does; while it lasts, control is only at its transition.
        # The Task, Parallel or Map just added, until another state or a flush
        # follows it; while it lasts, control is right after it.
        self.result: Result | None = None
        # The pending values as the Assign of that state reads them, for those
        # that can go in it.
        self.folded: dict[str, Expr] = {}
        # The pending values as the Assign of the catchers that lead to an
        # except clause reads them, with the error as $states.errorOutput.
        self.caught: dict[str, Expr] = {}
        self.loops: list[Loop] = []
        # The functions called directly whose bodies are being compiled here,
        # innermost last.
        self.expansions: list[Expansion] = []
        # Names the bodies of functions called directly assigned here, which a
        # later call may use again.
        self.expanded: set[str] = set()
        # The comparisons of a variable with a value written in the source,
        # the tests a Choice decided by known values can drop, by variable.
        self.flags: dict[str, list[ast.Compare]] = {}
        # Names in the source, and the variables loops and handlers added for
        # themselves, shared by every scope of the machine.
        self.taken = taken
        self.hidden = hidden
        # Map Run labels, unique across the machine.
        self.labels: set[str] = set()
        # The except clauses of the try statements around the current point,
        # outermost first, and the clauses being compiled, for a bare raise.
        self.tries: list[list[Handler]] = []
        # The try bodies each state is in, innermost last, for what a Task's
        # Catch may take of the state after it.
        self.enclosing: Enclosing = {}
        # States added that can report errors to except, counted.
        self.catchable = 0
        self.handling: list[Handler] = []
        # The statement being compiled, and, with --source-locations, the
        # source each state's Comment points into.
        self.current: ast.stmt | None = None
        self.locations: Locations | None = None
        # Whether the passes rewrite the states it builds, as they do but for
        # the tests that compare a definition before and after them.
        self.optimizing = True
        # Whether a read the passes made see another assignment fails the
        # compile, for the tests.
        self.checking = False

    def spelling(self, name: str) -> str:
        return spelling(name, self.module.spellings)

    def variable(self, name: str, type: Type | None, boolean: bool = False) -> Expr:
        """A variable as read, which holds a boolean for sure where every
        value assigned it that reaches the read is one."""
        return replace(variable(name, self.module.spellings, type), boolean=boolean)

    def add(
        self,
        base: str,
        state: dict[str, object],
        node: ast.AST,
        origins: list[Origin] | None = None,
    ) -> str:
        """A state of the statement being compiled, unless origins says where
        else in the source it comes from."""
        if self.remark is not None:
            state = commented(state, self.remark)
            self.remark = None
        name = self.take_name()
        if name is not None:
            return self.insert(name, state, node, origins or [self.here()], True)
        return self.insert(base, state, node, origins or [self.here()])

    def named_here(self, node: ast.stmt) -> bool:
        """Whether a `# state:` comment names the state node makes, which it
        then makes even where it would leave its work to another state."""
        return self.naming is not None and self.naming[1] is node

    def take_name(self) -> str | None:
        """The name of the first state the named statement adds."""
        if self.naming is None or self.naming[1] is not self.current:
            return None
        name = self.naming[0]
        self.naming = None
        return name

    def here(self) -> Origin:
        assert self.current is not None
        return Origin(self.current)

    def insert(
        self,
        base: str,
        state: dict[str, object],
        node: ast.AST,
        origins: list[Origin],
        named: bool = False,
    ) -> str:
        """named: base is the name a `# state:` comment gives, which the
        state keeps through the passes, as NAMED marks it."""
        self.result = None
        self.opening = False
        if self.locations is not None:
            located = self.locations.line(origins)
            remark = state.get("Comment")
            state = commented(state, f"{remark}\n{located}" if remark else located)
        if named:
            # The caller links the state it holds, such as a Choice's Default.
            state[NAMED] = base
        try:
            line = getattr(node, "lineno", None) if named else None
            added = self.graph.add(base, state, line)
        except ValueError as exc:
            raise CompileError(str(exc), node) from exc
        self.enclosing[added] = self.within()
        return added

    def flush(self) -> None:
        holding = self.holds_pending()
        result = self.following()
        self.result = None
        folded, self.folded = self.folded, {}
        caught, self.caught = self.caught, {}
        if not self.pending:
            return
        assert self.pending_node is not None and self.pending_first is not None
        pending = self.pending
        # A Pass that only moves a loop on is named after that, as its counter
        # already names the Pass that starts the loop.
        stepping = all(o.role == "loop step" for o in self.pending_origins)
        first = "next" if stepping else self.pending_first
        assign: dict[str, object] = {
            self.spelling(k): value for k, value in pending.items()
        }
        node = self.pending_node
        origins = self.pending_origins
        remarks = self.pending_remarks
        named = self.pending_name
        own = self.pending_named[1] if self.pending_named is not None else set()
        self.pending = {}
        self.pending_first = None
        self.pending_node = None
        self.pending_origins = []
        self.pending_remarks = []
        self.pending_name = None
        self.pending_named = None
        if named is not None:
            # A named assignment keeps a Pass of its own.
            state: dict[str, object] = {
                "Type": "Pass",
                "Assign": assign,
                NAMED_ASSIGNS: sorted(self.spelling(n) for n in own),
            }
            if remarks:
                state = commented(state, "\n".join(remarks))
            self.insert(named, state, node, origins, True)
            return
        if holding:
            assert result is not None
            self.result = self.fold(result, folded, origins, remarks)
            return
        if self.choosing():
            # Right after an except clause, the catchers assign the error, so
            # the assignments read it as the error output they assign.
            values = caught if caught.keys() == pending.keys() else pending
            if self.spread(values, origins, remarks):
                return
        state: dict[str, object] = {"Type": "Pass", "Assign": assign}
        if remarks:
            state = commented(state, "\n".join(remarks))
        self.insert(first, state, node, origins)

    def fold(
        self,
        result: Result,
        folded: dict[str, Expr],
        origins: list[Origin],
        remarks: list[str],
    ) -> Result:
        """Assignments right after a Task, a Parallel or a Map in its Assign,
        where control still is right after it."""
        state = result.state
        assign = state.get("Assign", {})
        assert isinstance(assign, dict)
        state["Assign"] = then(
            assign, {self.spelling(name): value for name, value in folded.items()}
        )
        remark = "\n".join(r for r in (result.remark, *remarks) if r) or None
        origins = result.origins + origins
        comment = remark
        if self.locations is not None:
            located = self.locations.line(origins)
            comment = f"{remark}\n{located}" if remark else located
        if comment:
            commented(state, comment)
        return Result(state, {**result.values, **folded}, origins, remark)

    def choosing(self) -> bool:
        """Whether control comes here along several paths, or along one that a
        Choice or a catcher takes, such as after a function called directly
        that returns from a loop: the Assign of each can hold what follows."""
        tails = self.graph.tails
        if self.opening:
            return False
        if len(tails) != 1:
            return len(tails) > 1
        [(container, key)] = tails
        return "Type" not in container or key == "Default"

    def spread(
        self, pending: dict[str, Expr], origins: list[Origin], remarks: list[str]
    ) -> bool:
        """Assignments where paths join, each in the Assign of the last state
        or Choice rule on every path, instead of a Pass after them, as a
        hand-writer copies an assignment into each branch. Only where each can
        take them and they read there what they would read after it: none
        reads or assigns a name that Assign assigns, and all read there what
        they would read after it."""
        values = list(pending.values())
        names = {self.spelling(n) for n in pending}
        reads = {
            self.spelling(v) for value in pending.values() for v in value.variables
        }
        holders: dict[int, dict[str, object]] = {}
        for container, key in self.graph.tails:
            assign = container.get("Assign", {})
            assert isinstance(assign, dict)
            if (
                not self.can_hold(container, key, values)
                or (names | reads) & assign.keys()
                or not self.holds_still(values)
            ):
                return False
            holders[id(container)] = container
        for holder in holders.values():
            assign = holder.get("Assign", {})
            assert isinstance(assign, dict)
            for name, value in pending.items():
                assign[self.spelling(name)] = value
            holder["Assign"] = assign
            self.describe(holder, origins, remarks)
        return True

    def can_hold(
        self, container: dict[str, object], key: str, values: list[Expr]
    ) -> bool:
        """Whether the Assign of what a transition belongs to runs only on the
        way along it: a Choice rule, a catcher, a Choice's own Assign for its
        Default, and a Pass, a Wait or a state that may take what follows it,
        as any state may take values that cannot fail, as failsafe says."""
        if "Type" not in container:
            return True
        kind = container["Type"]
        if kind == "Choice":
            return key == "Default"
        if kind in {"Task", "Parallel", "Map"}:
            return not refused(failure_escapes(container, values), "spread")
        return kind in {"Pass", "Wait"}

    def describe(
        self, holder: dict[str, object], origins: list[Origin], remarks: list[str]
    ) -> None:
        """Add the comments and the source of assignments to what holds them."""
        lines = (
            str(holder.get("Comment", "")).split("\n") if "Comment" in holder else []
        )
        located = None
        if self.locations is not None and lines and lines[-1].startswith(PREFIX):
            located = lines.pop()
        text = [*lines, *remarks]
        if self.locations is not None:
            text.append(self.locations.extended(located, origins))
        if text:
            commented(holder, "\n".join(text))

    def defer(
        self,
        name: str,
        value: Expr,
        node: ast.AST,
        origin: Origin,
        folded: Expr | None = None,
    ) -> None:
        """A value for the Pass the pending assignments share, and where in the
        source it comes from. Right after a Task, a Parallel or a Map, a value
        that reads nothing pending and nothing the state assigns reads the same
        in the state's Assign, so it may go there, as flush decides. An
        assignment that reads what the state assigns gives how it reads there
        as folded. What an earlier assignment of the name put there goes, as
        the new value replaces it: a, b = b, a would read a's new value in
        place of the one the state assigned."""
        if (
            self.naming is not None
            and self.naming[1] is self.current
            and isinstance(self.current, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        ):
            # A named assignment starts the Pass it names.
            self.flush()
            self.pending_name = self.naming[0]
            self.pending_named = (self.naming[1], set())
            self.naming = None
        if self.pending_named is not None and self.pending_named[0] is self.current:
            self.pending_named[1].add(name)
        self.folded.pop(name, None)
        result = self.following()
        if folded is not None:
            self.folded[name] = folded
        elif (
            result is not None
            and not value.variables & (self.pending.keys() | result.values.keys())
            and not value.volatile
        ):
            self.folded[name] = value
        # A name assigned again is evaluated where it is written now.
        if not self.pending:
            self.pending_first = name
        self.pending.pop(name, None)
        self.pending[name] = value
        self.pending_node = self.pending_node or node
        self.pending_origins.append(origin)

    def hold_remark(self) -> None:
        """The comment of an assignment waits for the Pass it will share."""
        if self.remark is not None:
            self.pending_remarks.append(self.remark)
            self.remark = None

    def materialize(
        self, statements: list[ast.stmt], node: ast.stmt, role: str
    ) -> None:
        """Assign to variables the names bound to expressions, such as a map's
        item or a list loop's variable, that the statements assign again. A path
        that does not assign them would read the expression after the others
        join it, or after a loop leads back, instead of the value.

        They share a Pass with what is pending, a map's parameters, without
        reading it: an expression here reads variables of the enclosing scope,
        and the names this scope assigns are not among them."""
        for name in sorted(assigned_names(statements) - self.parameters):
            binding = self.bindings.get(name)
            if binding is None or binding == self.variable(name, binding.type):
                continue
            self.defer(name, binding, node, Origin(node, role, header=True))
            self.bindings[name] = self.variable(name, binding.type)

    def block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if not self.graph.reachable:
                raise CompileError(
                    "this line is never reached; remove it or the return above it",
                    statement,
                )
            self.statement(statement)

    def statement(self, node: ast.stmt) -> None:
        """A statement, whose comment goes to the first state it adds; a
        compound statement that adds none first leaves it to its body."""
        own = self.module.comments.get(node.lineno)
        self.remark = "\n".join(r for r in (self.remark, own) if r) or None
        name = self.module.state_names.get(node.lineno)
        if name is not None:
            # A function called directly compiles the same statement again;
            # another statement on the line would take the name too.
            first = self.module.named.setdefault(node.lineno, node.col_offset)
            if first != node.col_offset:
                raise CompileError(
                    "# state: names the one statement of its line; put the "
                    "statements on lines of their own",
                    node,
                )
            if isinstance(node, UNNAMED_STATEMENTS) or (
                isinstance(node, ast.Expr)
                and not self.makes_state(node.value)
                and not self.called(node.value, "wait")
            ):
                raise CompileError(
                    "this statement makes no state of its own to name; name a "
                    "statement in it that does",
                    node,
                )
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            # A docstring or a string used as a comment adds no state, so the
            # comment waits for the statement after it, such as the first of
            # the body of a function called directly.
            return
        enclosing = self.current
        self.current = node
        if name is not None:
            self.naming = (name, node)
        try:
            self.compile_statement(node)
            if self.named_here(node):
                raise CompileError(
                    "this statement makes no state of its own to name", node
                )
        finally:
            self.remark = None
            self.current = enclosing

    def compile_statement(self, node: ast.stmt) -> None:
        called = self.direct_call(node)
        if called is not None:
            self.expand(node, called)
        elif isinstance(node, ast.Return) and self.expansions:
            self.give_back(node)
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise CompileError(
                    "assign one variable per statement: x = ...", node.targets[-1]
                )
            self.assign(node.targets[0], node.value, None)
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                self.declare(node)
            else:
                self.assign(node.target, node.value, node.annotation)
        elif isinstance(node, ast.AugAssign):
            self.augment(node)
        elif isinstance(node, ast.Return):
            if node.value is None:
                self.end_without_value(node, [self.here()])
            elif self.named_here(node) or not (
                self.return_pending(node.value, node)
                or self.end_with_result(node.value)
            ):
                self.finish(*self.translator.statement_value(node.value), node)
        elif (
            isinstance(node, ast.If)
            and not self.named_here(node)
            and (conditional := self.as_conditional(node)) is not None
        ):
            target, value = conditional
            self.assign(target, value, None)
        elif isinstance(node, ast.If):
            self.branch(node)
        elif isinstance(node, ast.While):
            self.while_loop(node)
        elif isinstance(node, ast.For):
            self.for_loop(node)
        elif isinstance(node, (ast.Break, ast.Continue)):
            self.leave(node)
        elif isinstance(node, ast.Raise):
            self.fail(node)
        elif isinstance(node, ast.Try):
            self.attempt(node)
        elif isinstance(node, ast.Assert):
            raise CompileError(
                "assert is not compiled; write the check as "
                "if not ...: raise OrderFailed(...)",
                node,
            )
        elif isinstance(node, ast.Expr) and self.called(node.value, "wait"):
            assert isinstance(node.value, ast.Call)
            self.wait(node.value)
        elif isinstance(node, ast.Expr) and self.makes_state(node.value):
            _, call = self.translator.statement_value(node.value)
            assert call is not None
            self.flush()
            remark = self.remark
            added = self.add_call(call.name, call, {}, node)
            self.result = Result(self.graph.states[added], {}, [self.here()], remark)
        elif isinstance(node, ast.FunctionDef):
            self.define(node)
        elif isinstance(node, ast.Pass):
            return
        else:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
            ):
                self.translator.check_import(node.value.func)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                advice = changing_call(node.value, self.parameters)
                if advice is not None:
                    raise CompileError(advice, node)
            name = type(node).__name__
            raise CompileError(
                STATEMENTS.get(name, f"{name} statements are not supported"), node
            )

    def augment(self, node: ast.AugAssign) -> None:
        """x += v as x = x + v, a list's included: xs += [x] assigns xs the
        list with x appended."""
        if isinstance(node.target, ast.Subscript):
            whole = ast.BinOp(node.target, node.op, node.value)
            raise self.changed_in_place(node.target, whole)
        if not isinstance(node.target, ast.Name):
            raise CompileError(
                "assign one variable per statement: x = ...", node.target
            )
        name = node.target.id
        reading = ast.copy_location(ast.Name(name, ast.Load()), node.target)
        value = ast.copy_location(ast.BinOp(reading, node.op, node.value), node)
        self.assign(
            ast.copy_location(ast.Name(name, ast.Store()), node.target), value, None
        )

    def as_conditional(self, node: ast.If) -> tuple[ast.Name, ast.expr] | None:
        """An if whose every branch assigns one variable and does nothing
        else, as that assignment of a conditional expression, which a
        hand-writer writes in the Assign of the state before instead of a
        Choice: `v = a if test else v`, or the else branch's value. Where there
        is no else, the variable must hold a value on every path already, and
        no value may call what makes a state. A flag keeps its Choice where a
        branch gives it a value written in the source and the function tests
        it elsewhere: that value is known on the path, which the other test
        is decided by. The comments of the branches go with it."""
        found = conditional_assignment(node)
        if found is None:
            return None
        target, value, complete = found
        if not complete and (
            target.id not in self.bindings or target.id in self.partial
        ):
            return None
        own = {id(n) for n in ast.walk(node.test)}
        tested = [c for c in self.flags.get(target.id, []) if id(c) not in own]
        if tested and any(isinstance(b, ast.Constant) for b in branches(value)):
            return None
        for made in ast.walk(value):
            if not isinstance(made, ast.Call):
                continue
            if self.makes_state(made) or self.called(made, "wait"):
                return None
            if isinstance(made.func, ast.Name) and (
                self.translator.is_function(made.func.id)
                or made.func.id in self.partial
            ):
                return None
        lines = range(node.lineno + 1, (node.end_lineno or node.lineno) + 1)
        remarks = [self.module.comments.get(line) for line in lines]
        self.remark = "\n".join(r for r in (self.remark, *remarks) if r) or None
        return target, value

    def makes_state(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and makes_state(
            qualified(node.func, self.module.names)
        )

    def called(self, node: ast.expr, name: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and qualified(node.func, self.module.names) == f"sfnx.{name}"
        )

    def direct_call(self, node: ast.stmt) -> ast.Call | None:
        """The call of a function of the module or the machine that a
        statement makes on its own, assigns or returns, read through
        subscripts: f(...), x = f(...)["k"], return f(...)."""
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            value: ast.expr | None = node.value
        elif isinstance(node, (ast.AnnAssign, ast.Return, ast.Expr)):
            value = node.value
        else:
            return None
        while isinstance(value, ast.Subscript):
            value = value.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and (
                self.translator.is_function(value.func.id)
                or value.func.id in self.partial
            )
        ):
            return value
        return None

    def expand(self, node: ast.stmt, call: ast.Call) -> None:
        """A function called directly: its body compiled here, where each
        parameter reads the argument written for it and each name the body
        assigns is renamed if this scope uses it. Each return gives the
        statement the value the call would, and the paths join after it."""
        function = self.callee(call)
        written = self.written_arguments(function, call)
        renaming = self.renaming(function)
        body = renamed(function.body, renaming)
        assigned = {renaming[n] for n in assigned_names(function.body)}
        self.assigned |= assigned
        self.expanded |= assigned
        frame = Expansion(
            function.name, node, call, len(self.loops), set(renaming.values())
        )
        self.expansions.append(frame)
        read = {
            n.id
            for n in ast.walk(ast.Module(body, []))
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        try:
            for parameter in function.args.args:
                name = renaming[parameter.arg]
                self.pass_argument(
                    name, written[parameter.arg], parameter, name in read
                )
            self.block(body)
            if self.graph.reachable:
                self.give_back(ast.copy_location(ast.Return(None), node))
        finally:
            self.expansions.pop()
            for name in renaming.values():
                self.translator.arguments.pop(name, None)
        self.join(frame.returns)
        if len(frame.returns) > 1:
            self.result = None
        for name in renaming.values():
            self.bindings.pop(name, None)
            self.declared.pop(name, None)
            self.partial.discard(name)

    def returned_expression(
        self, call: ast.Call
    ) -> tuple[ast.expr, dict[str, ast.expr]]:
        """For a call inside an expression, the value a function whose body is
        one return of a value returns, and the argument written for each
        parameter. A body of anything else makes states, which an expression
        cannot hold."""
        function = self.callee(call)
        value = only_return(function)
        if value is None:
            raise CompileError(direct_call(function.name), call)
        return value, self.written_arguments(function, call)

    def callee(self, call: ast.Call) -> ast.FunctionDef:
        """The function a direct call runs, if its body can be compiled in
        place of the call."""
        assert isinstance(call.func, ast.Name)
        name = call.func.id
        if name in self.partial and name not in self.functions:
            raise CompileError(
                f"{name} is not defined the same way on every path to here; "
                "define the function once, before the if, loop or try",
                call,
            )
        function = self.functions.get(name) or self.module.functions[name]
        if function.decorator_list:
            raise CompileError(
                f"{name}() has decorators, and a function called directly takes "
                "none; remove them",
                call,
            )
        if any(frame.name == name for frame in self.expansions):
            raise CompileError(
                f"{name}() calls itself, and its body would be written here "
                "without end; write the repetition as a loop",
                call,
            )
        arguments = function.args
        if (
            arguments.posonlyargs
            or arguments.vararg
            or arguments.kwonlyargs
            or arguments.kwarg
        ):
            raise CompileError(
                f"a function called directly takes plain parameters, with or "
                f"without defaults; {name} has others",
                function,
            )
        nested = next(
            (
                n
                for n in ast.walk(function)
                if n is not function and isinstance(n, ast.FunctionDef)
            ),
            None,
        )
        if nested is not None:
            raise CompileError(
                f"{nested.name} is defined inside {name}, which is called "
                f"directly; define it outside {name}",
                nested,
            )
        if name not in self.functions:
            # A function of the module reads the module's names, not this
            # scope's variables, and so do its defaults.
            defaults = {
                n.id
                for default in function.args.defaults
                for n in ast.walk(default)
                if isinstance(n, ast.Name)
            }
            for read in sorted(global_names(function) | defaults):
                if read in self.bindings or read in self.assigned:
                    raise CompileError(
                        f"{name}() reads {read} of the module, and {read} is a "
                        "variable here too; rename the variable",
                        call,
                    )
        return function

    def written_arguments(
        self, function: ast.FunctionDef, call: ast.Call
    ) -> dict[str, ast.expr]:
        """The argument written for each parameter, or its default."""
        name = function.name
        parameters = [p.arg for p in function.args.args]
        if len(call.args) > len(parameters):
            raise CompileError(
                f"{name}() takes {len(parameters)} arguments",
                call.args[len(parameters)],
            )
        written = dict(zip(parameters, call.args, strict=False))
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg not in parameters:
                raise CompileError(
                    f"{name}() has no parameter "
                    f"{'to unpack into' if keyword.arg is None else keyword.arg}",
                    keyword,
                )
            if keyword.arg in written:
                raise CompileError(f"{keyword.arg} is given twice to {name}()", keyword)
            written[keyword.arg] = keyword.value
        defaults = function.args.defaults
        for parameter, default in zip(
            parameters[len(parameters) - len(defaults) :], defaults, strict=True
        ):
            written.setdefault(parameter, default)
        for parameter in parameters:
            if parameter not in written:
                raise CompileError(f"{name}() needs {parameter}", call)
        for argument in written.values():
            made = next(
                (
                    n
                    for n in ast.walk(argument)
                    if self.adds_states(n) and not self.returns_only(n)
                ),
                None,
            )
            if made is not None:
                assert isinstance(made, ast.Call)
                raise CompileError(
                    f"{name}() takes values; call {ast.unparse(made.func)}() on a "
                    "line of its own first and pass its result",
                    made,
                )
        return written

    def returns_only(self, node: ast.AST) -> bool:
        """Whether a node calls a function whose body is one return of a
        value, which is written into the expression it is called in."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            return False
        name = node.func.id
        function = self.functions.get(name) or self.module.functions.get(name)
        return function is not None and only_return(function) is not None

    def adds_states(self, node: ast.AST) -> bool:
        """Whether a node is a call that adds states."""
        return isinstance(node, ast.Call) and (
            self.makes_state(node)
            or (
                isinstance(node.func, ast.Name)
                and self.translator.is_function(node.func.id)
            )
        )

    def renaming(self, function: ast.FunctionDef) -> dict[str, str]:
        """A name for each name local to a function called directly: its own,
        unless this scope or a call around this one uses it, or the definition
        gives it to another name."""
        used = (
            set(self.bindings)
            | (self.assigned - self.expanded)
            | self.outer
            | self.hidden
            | set(self.module.spellings.values())
        )
        for frame in self.expansions:
            used |= frame.names
        renaming = {}
        for name in sorted(local_names(function)):
            given = name
            serial = 1
            while given in used:
                serial += 1
                given = f"{name}_{serial}"
            used.add(given)
            renaming[name] = given
        return renaming

    def pass_argument(
        self, name: str, written: ast.expr, parameter: ast.arg, read: bool
    ) -> None:
        """A parameter reads the argument written for it. A value is read where
        the body reads it, as the same expression, except one that changes on
        evaluation, which a variable keeps from the call. What is not a value,
        such as a Retry, is read only where the body takes what is written; an
        argument the body never reads has to be a value, as Python evaluates it
        at the call."""
        self.translator.arguments[name] = written
        try:
            value = self.translator.expr(written)
        except CompileError:
            if read:
                return
            raise
        declared = annotate(parameter.annotation, self.module)
        if value.volatile:
            self.defer(name, value, written, self.here())
            value = self.variable(name, value.type)
        self.bindings[name] = replace(value, type=declared or value.type)

    def give_back(self, node: ast.Return) -> None:
        """A return in a function called directly: the statement that called
        it, with the value in place of the call, then on to after the call. A
        call on its own line evaluates only a call that adds states."""
        frame = self.expansions.pop()
        try:
            value = node.value or ast.copy_location(ast.Constant(None), node)
            statement = copy.deepcopy(frame.statement, {id(frame.call): value})
            ast.copy_location(statement, node)
            if isinstance(statement, ast.Expr):
                while isinstance(statement.value, ast.Subscript):
                    statement.value = statement.value.value
                if not self.adds_states(statement.value):
                    statement = None
            if statement is not None:
                self.compile_statement(statement)
        finally:
            self.expansions.append(frame)
        if not isinstance(frame.statement, ast.Return) and self.graph.reachable:
            # A return right after a Task leaves it the call's last state,
            # unless another return joins it.
            if self.pending:
                self.flush()
            frame.returns.append(self.save())
            self.graph.tails = []

    def claim(self, name: str, node: ast.AST) -> None:
        """A name the scope assigns: not the input or a function, a valid Step
        Functions variable, and not a variable of an enclosing scope."""
        if name in self.functions:
            raise CompileError(
                f"{name} is a function here; give the variable another name", node
            )
        if name in self.parameters:
            raise CompileError(
                f"{name} is the execution input; assign the new value to another "
                "name, such as data",
                node,
            )
        self.check_variable(name, node)
        if name in self.outer:
            # Only a parameter of a distributed map keeps its name, as args=
            # names it.
            raise CompileError(
                f"{name} is assigned outside this function too, and Step Functions "
                "keeps the variables of a branch apart from the machine's; use "
                "another name here and in args=",
                node,
            )

    def changed_in_place(self, target: ast.Subscript, value: ast.expr) -> CompileError:
        """d["k"] = v changes the dict in place, which a JSON value cannot: the
        dict is written again whole, d = {**d, "k": v}, through every key
        written as a string, d["a"]["b"] = v as d = {**d, "a": {**d["a"], "b":
        v}}. Another name for the dict keeps the old one, where in Python it
        sees the change, so the message says so."""
        whole: ast.expr = value
        held: ast.expr = target
        while (
            isinstance(held, ast.Subscript)
            and isinstance(held.slice, ast.Constant)
            and isinstance(held.slice.value, str)
        ):
            whole = ast.Dict([None, held.slice], [held.value, whole])
            held = held.value
        if whole is value or not isinstance(held, ast.Name):
            return CompileError(
                "a list or dict is not changed in place; assign the new value "
                "to a name",
                target,
            )
        if held.id in self.parameters:
            return CompileError(
                f"{held.id} is the execution input; write the changed copy to "
                f"another name: data = {ast.unparse(whole)}",
                target,
            )
        self.claim(held.id, target)
        return CompileError(
            "a dict is changed in place in Python, which other names for it see; "
            f"a JSON value is a copy, so write {held.id} = {ast.unparse(whole)}",
            target,
        )

    def define(self, node: ast.FunctionDef) -> None:
        if node.name in self.bindings:
            raise CompileError(
                f"{node.name} is a variable here; give the function another name",
                node,
            )
        if any(node.name in loop.functions for loop in self.loops):
            # Python runs the new definition from the next iteration on, and
            # after the loop; a state machine compiles each call once.
            raise CompileError(
                f"{node.name} is defined before the loop too; give the function "
                "in the loop another name",
                node,
            )
        self.functions[node.name] = node

    def declare(self, node: ast.AnnAssign) -> None:
        """name: type without a value declares the type the name's later
        assignments hold, as for a, b = ..., which takes no annotation."""
        if not isinstance(node.target, ast.Name):
            raise CompileError("declare one variable: name: type", node.target)
        self.claim(node.target.id, node.target)
        declared = annotate(node.annotation, self.module)
        assert declared is not None
        self.announced[node.target.id] = declared

    def assign(
        self, target: ast.expr, value_node: ast.expr, annotation_node: ast.expr | None
    ) -> None:
        if isinstance(target, ast.Tuple):
            self.unpack(target, value_node)
            return
        if isinstance(target, ast.Subscript):
            raise self.changed_in_place(target, value_node)
        if not isinstance(target, ast.Name):
            raise CompileError("assign one variable per statement: x = ...", target)
        name = target.id
        self.claim(name, target)
        declared = annotate(annotation_node, self.module) or self.announced.get(name)
        value, call = self.translator.statement_value(value_node)
        known = declared or value.type or self.declared.get(name)
        bound = self.bindings.get(name)
        if (
            isinstance(value_node, ast.Name)
            and value_node.id == name
            and bound is not None
            and bound.code == self.variable(name, None).code
        ):
            # A variable assigned its own value keeps it: reading a variable
            # neither fails nor is undefined, so nothing is left to do. A name
            # bound to an expression, such as the parameter of a function
            # called directly, becomes a variable as any assignment makes it
            # one.
            if declared is not None:
                self.declared[name] = declared
            self.bindings[name] = self.variable(name, known, bound.boolean)
            return
        if call is not None:
            # The state's own Assign takes its result. A Catch leaves with the
            # declarations from before, as the assignment did not happen.
            self.flush()
            assign = {self.spelling(name): value}
            remark = self.remark
            added = self.add_call(name, call, {"Assign": assign}, target)
            self.result = Result(
                self.graph.states[added], {name: value}, [self.here()], remark
            )
            if declared is not None:
                self.declared[name] = declared
            self.bindings[name] = self.variable(name, known)
            self.partial.discard(name)
            return
        if declared is not None:
            self.declared[name] = declared
        # Assign evaluates every expression with the values from before the
        # state, so one that reads a pending assignment reads its expression
        # instead, as a hand-writer spells a path out again. One that changes on
        # evaluation would give another value there, so it needs a state of its
        # own, and so does a name assigned again, whose first value is still
        # evaluated, as Python evaluates it, unless the new value evaluates it
        # every time, as the test of `v = a if test(v) else v` does, and it
        # never gives undefined, which a test reads without failing where the
        # first Assign would fail. A first value that can neither fail nor be
        # undefined, such as one written in the source, has nothing to
        # evaluate, so the new value takes its place, and so does one that
        # another pending name holds whole, which still evaluates it.
        first = self.pending.get(name)
        others = [v for n, v in self.pending.items() if n != name]
        if (
            first is not None
            and not replaceable(first, value, self.spelling(name))
            and not kept(first, others)
        ):
            self.flush()
        reads = sorted(value.variables & self.pending.keys())
        if reads and not any(self.pending[read].volatile for read in reads):
            substituted = self.read_as(value_node, {r: self.pending[r] for r in reads})
            # jsonata() reads a variable by its name, which no expression
            # replaces.
            if not substituted.variables & self.pending.keys():
                value = substituted
        if value.variables & self.pending.keys():
            self.flush()
            reads = []
        result = self.following()
        folded = None
        if result is not None and all(read in self.folded for read in reads):
            substituted = {**result.values, **{r: self.folded[r] for r in reads}}
            folded = self.read_result(Result({}, substituted, [], None), value_node)
        error = self.catching()
        if error is not None and all(read in self.caught for read in reads):
            error_output = expression("$states.errorOutput", type=ERROR_OUTPUT)
            substituted = {error: error_output, **{r: self.caught[r] for r in reads}}
            self.caught[name] = self.read_as(value_node, substituted)
        self.defer(name, value, target, self.here(), folded)
        self.hold_remark()
        self.bindings[name] = self.variable(name, known, value.boolean)
        self.partial.discard(name)

    def unpack(self, target: ast.Tuple, value_node: ast.expr) -> None:
        """a, b = ...: each name takes one element, all in one state. With a
        tuple of values, `a, b = b, a` swaps, as Assign reads the old values.
        A value that would give other items when it is evaluated again is kept
        by the state before them, as every name reads it again."""
        if not target.elts:
            raise CompileError("unpack into at least one name: a, b = ...", target)
        names = []
        for element in target.elts:
            if not isinstance(element, ast.Name):
                raise CompileError("unpack into variable names: a, b = ...", element)
            self.claim(element.id, element)
            names.append(element.id)
        if len(set(names)) != len(names):
            raise CompileError("unpack into different names", target)
        call = None
        if isinstance(value_node, (ast.Tuple, ast.List)):
            if len(value_node.elts) != len(names):
                raise CompileError(
                    f"{len(names)} names take {len(names)} values", value_node
                )
            values = [self.translator.expr(e) for e in value_node.elts]
        else:
            whole, call = self.translator.statement_value(value_node)
            if call is None and whole.volatile:
                # Each name reads the value again, and evaluating it again
                # would give other items, so the names take the value the
                # state before them kept, as Python does.
                if whole.variables & self.pending.keys():
                    self.flush()
                copy = self.fresh(f"{self.spelling(names[0])}_items", target)
                self.defer(copy, whole, target, self.here())
                whole = self.variable(copy, whole.type)
            values = [index_expr(whole, literal(i)) for i in range(len(names))]
        assign = {
            self.spelling(name): value
            for name, value in zip(names, values, strict=True)
        }
        if call is not None:
            self.flush()
            remark = self.remark
            added = self.add_call(names[0], call, {"Assign": assign}, target)
            self.result = Result(
                self.graph.states[added],
                dict(zip(names, values, strict=True)),
                [self.here()],
                remark,
            )
        else:
            shared = None
            if isinstance(value_node, (ast.Tuple, ast.List)):
                shared = self.read_pending(names, value_node.elts, values)
            if shared is not None:
                values = shared
            else:
                reads = frozenset().union(*(v.variables for v in values))
                if set(names) & self.pending.keys() or reads & self.pending.keys():
                    self.flush()
            for name, value in zip(names, values, strict=True):
                self.defer(name, value, target, self.here())
            self.hold_remark()
        for name, value in zip(names, values, strict=True):
            known = self.announced.get(name) or value.type or self.declared.get(name)
            self.bindings[name] = self.variable(name, known)
            self.partial.discard(name)

    def read_pending(
        self, names: list[str], nodes: list[ast.expr], values: list[Expr]
    ) -> list[Expr] | None:
        """The values of a, b = x, y read as a single assignment reads them, so
        that they share the pending assignments' state: each value reads a
        pending one as its expression, all of them before any name is
        assigned, and a name assigned again replaces a first value it may
        replace. None where they cannot, and need a state of their own."""
        read = []
        for node, value in zip(nodes, values, strict=True):
            reads = sorted(value.variables & self.pending.keys())
            if any(self.pending[r].volatile for r in reads):
                return None
            if reads:
                value = self.read_as(node, {r: self.pending[r] for r in reads})
                if value.variables & self.pending.keys():
                    return None
            read.append(value)
        for name, value in zip(names, values, strict=True):
            first = self.pending.get(name)
            others = [v for n, v in zip(names, read, strict=True) if n != name]
            if (
                first is not None
                and not replaceable(first, value, self.spelling(name))
                and not kept(first, others)
            ):
                return None
        return read

    def end_without_value(self, node: ast.AST, origins: list[Origin]) -> None:
        """A return without a value, written or where a body ends, which
        return None is: the state before it ends the machine or the branch
        when it can, and a Succeed does otherwise."""
        none = ast.copy_location(ast.Constant(None), node)
        if not (
            self.return_pending(none, node, origins)
            or self.end_with_result(none, origins)
        ):
            self.finish(literal(None), None, node, origins)

    def return_pending(
        self,
        value_node: ast.expr,
        node: ast.AST,
        origins: list[Origin] | None = None,
    ) -> bool:
        """A return right after assignments that wait for a Pass, with no Task,
        Parallel or Map before them to hold them, as a Succeed whose Output
        reads each as its expression, as a hand-writer returns what they
        compute: nothing reads them after the return, so the Pass goes.
        Python evaluates each even when the return does not read it, so they
        fail where Python's would, as fails_in_place says. A value that changes
        on evaluation, or that reads the state it is in, keeps the Pass. After
        a Task, a Parallel or a Map that can hold them, the return is left to
        end_with_result, which ends on that state; after one that cannot, as
        a Catch would take their failure, the Succeed reads them as well."""
        pending = self.pending
        if not pending or self.holds_pending() or self.pending_name is not None:
            return False
        if any(self.makes_state(n) for n in ast.walk(value_node)):
            return False
        if not self.fails_in_place(value_node, pending):
            return False
        values = list(pending.values())
        if any(v.volatile for v in values) or not self.holds_still(values):
            return False
        value = self.read_as(value_node, dict(pending))
        if self.read_by_name(value_node, pending):
            return False
        located = self.take_pending(origins or [self.here()])
        self.finish(value, None, node, located)
        return True

    def holds_pending(self) -> bool:
        """Whether the Task, the Parallel or the Map just added takes the
        pending assignments in its Assign when they are flushed, as flush
        decides."""
        result = self.following()
        if result is None:
            return False
        folded = list(self.folded.values())
        return (
            not self.opening
            and self.folded.keys() == self.pending.keys()
            and not refused(failure_escapes(result.state, folded), "fold")
            and self.holds_still(folded)
        )

    def fails_in_place(self, value_node: ast.expr, pending: dict[str, Expr]) -> bool:
        """Whether a return that reads the pending values as their expressions,
        in the place of the assignments, fails where Python does, which
        evaluates each even when the return does not read it: each neither
        fails nor is undefined, the return is that variable itself, whose
        Output fails where the Pass would, or the return reads each one that
        may fail every time it is evaluated, and each is never undefined,
        which a list or a dict would drop without failing. Where two may
        fail, the Output may fail on another of them first, as its message
        then says, which a hand-writer would not spend a state to keep."""
        code = self.read_with({}, value_node).code
        spelled = {self.spelling(name): value for name, value in pending.items()}
        return not refused(failure_kept(code, spelled), "return_pending")

    def read_by_name(self, value_node: ast.expr, pending: dict[str, Expr]) -> bool:
        """Whether a value reads a pending variable where no expression can
        take its place, as the text of jsonata() reads a variable by its
        name. A pending expression that reads its own name, as that of
        `n = n + 1` does, reads the value from before, which the state that
        ends the path in their place reads too, as none of them is assigned."""
        marks = {
            name: expression(f"$sfnx_pending_{i}_", type=value.type)
            for i, (name, value) in enumerate(pending.items())
        }
        return bool(self.read_as(value_node, marks).variables & pending.keys())

    def take_pending(self, origins: list[Origin]) -> list[Origin]:
        """Clear the pending assignments for a state that ends the path in
        their place, which carries their comments and where they come from,
        before origins, its own."""
        remarks = [*self.pending_remarks, self.remark]
        located = [*self.pending_origins, *origins]
        self.pending = {}
        self.pending_first = None
        self.pending_node = None
        self.pending_origins = []
        self.pending_remarks = []
        self.remark = "\n".join(r for r in remarks if r) or None
        return located

    def end_with_result(
        self, value_node: ast.expr, origins: list[Origin] | None = None
    ) -> bool:
        """A return right after a Task, a Parallel or a Map, or right after the
        assignments that went in its Assign, as the state's Output and End,
        where a person ends a machine or a branch. The Output reads the
        variables the state assigns as what they take, and the other variables
        from before the state, as the return does. origins are where the
        return is written, the statement by default."""
        # A call in the return is a state of its own, and translating it again
        # would name its states again.
        if any(self.makes_state(node) for node in ast.walk(value_node)):
            return False
        # Assignments waiting for a state may go in this one's Assign first.
        if self.pending:
            self.flush()
        result = self.following()
        if result is None:
            return False
        value = self.read_result(result, value_node)
        if value is None or not self.holds_still([value]):
            return False
        # A value that cannot fail leaves neither a Catch nor a retrier a
        # failure of the Output to take.
        if refused(failure_escapes(result.state, value), "end_with_result"):
            return False
        state = result.state
        self.graph.tails = []
        self.result = None
        self.returns.append(value.type)
        self.describe_end(
            state, result.remark, result.origins + (origins or [self.here()])
        )
        state.pop("Assign", None)
        if value.code != "$states.result":
            state["Output"] = value
        state["End"] = True
        return True

    def describe_end(
        self, state: dict[str, object], remark: str | None, origins: list[Origin]
    ) -> None:
        """Comment a state that ends a path in place of a return with what it
        was already described by, the return's own comment and, where
        locations are kept, where both come from."""
        remark = "\n".join(r for r in (remark, self.remark) if r) or None
        self.remark = None
        if self.locations is not None:
            located = self.locations.line(origins)
            remark = f"{remark}\n{located}" if remark else located
        if remark:
            commented(state, remark)

    def catching(self) -> str | None:
        """The name an except clause binds the error to, while control is
        still at the catchers that lead to it."""
        if not self.handling or self.handling[-1].variable is None:
            return None
        tails = self.graph.tails
        if tails and all("ErrorEquals" in container for container, _ in tails):
            return self.handling[-1].variable
        return None

    def following(self) -> Result | None:
        """The Task, Parallel or Map just added, while control is right after
        it: a branch or a loop moves control without adding a state."""
        result = self.result
        if result is None:
            return None
        tails = self.graph.tails
        if len(tails) != 1 or tails[0][0] is not result.state or tails[0][1] != "Next":
            return None
        return result

    def holds_still(self, values: list[Expr]) -> bool:
        """Whether values read the same in the Assign or the Output of the
        state that holds them as in a state after it. The State part of the
        context names the state it is read in, and what $eval, or code the
        parser cannot read, reads is not known. The time and a random value
        are read when the Assign runs: for a Task, a Parallel, a Map or a
        Wait, when it ends (measured), and for a Choice rule, a catcher or a
        Pass, where it is, both after what comes before them, as Python reads
        them."""
        unknown = any(v.sensitivity.dependencies_unknown for v in values)
        return not unknown and not refused(
            context_invariant([v.code for v in values], Differs.CONTEXT),
            "holds_still",
        )

    def read_result(self, result: Result, value_node: ast.expr) -> Expr | None:
        """A value as the Assign or the Output of the state just added reads
        it: the variables the state assigns as what they take, and the others
        as they were before it. None for one that may read a value that
        changes on evaluation more than once, as most_reads bounds it: each
        reading would evaluate it again, where Python reads the one value the
        variable holds. A read in the function of a comprehension is one for
        each item."""
        value = self.read_with(result.values, value_node)
        changing = [name for name, v in result.values.items() if v.volatile]
        if changing:
            # Each changing value is read as a mark, counted where it lands.
            marks = {
                name: replace(
                    result.values[name],
                    code=f"$sfnx_read_{i}_",
                    template=f"{{% $sfnx_read_{i}_ %}}",
                )
                for i, name in enumerate(changing)
            }
            marked = self.read_with({**result.values, **marks}, value_node)
            codes = expressions_in(marked.template)
            if any(
                refused(read_at_most_once(codes, mark.code[1:]), "read_result")
                for mark in marks.values()
            ):
                return None
        return value

    def read_with(self, values: dict[str, Expr], value_node: ast.expr) -> Expr:
        """A value that makes no state, with the variables in values read as
        the expressions they take, and the others as they are."""
        before = dict(self.bindings)
        # Each variable reads as what it takes, with the type it was bound
        # with, which a declaration can give.
        self.bindings.update(
            {
                name: replace(value, type=before[name].type)
                for name, value in values.items()
            }
        )
        self.reread_arguments(set(values), before)
        try:
            value, _ = self.translator.statement_value(value_node)
        finally:
            self.bindings.clear()
            self.bindings.update(before)
        return value

    def reread_arguments(self, changed: set[str], before: dict[str, Expr]) -> None:
        """A parameter of a function called directly reads the argument as it
        was translated at the call, so one whose argument reads a variable that
        now reads as another expression is translated again, outer calls
        first, as its value is where the body reads it."""
        for name, argument in self.translator.arguments.items():
            reads = {n.id for n in ast.walk(argument) if isinstance(n, ast.Name)}
            if not reads & changed:
                continue
            value = self.translator.expr(argument)
            self.bindings[name] = replace(value, type=before[name].type)
            changed.add(name)

    def read_as(self, value_node: ast.expr, values: dict[str, Expr]) -> Expr:
        """A value that makes no state, with the variables in values read as
        the expressions they take, each with the type it was bound with."""
        read = self.read_result(Result({}, values, [], None), value_node)
        assert read is not None
        return read

    def finish(
        self,
        value: Expr,
        call: StateCall | None,
        node: ast.AST,
        origins: list[Origin] | None = None,
    ) -> None:
        self.flush()
        self.returns.append(value.type)
        if call is None:
            state: dict[str, object] = {"Type": "Succeed", "Output": value}
            self.add("return", state, node, origins)
            return
        # A Task at the end ends the machine itself; its output is the result
        # unless the return makes something of it.
        ending: dict[str, object] = {}
        if value.code != "$states.result":
            ending["Output"] = value
        self.add_call("return", call, {**ending, "End": True}, node)

    def add_call(
        self, base: str, call: StateCall, fields: dict[str, object], node: ast.AST
    ) -> str:
        """A Task or Parallel, with a Catch for every except clause around it,
        innermost first. A Catch for Exception matches everything, so nothing
        follows it."""
        state = dict(call.state)
        self.catchable += 1
        if call.retry is not None:
            state["Retry"] = retriers(call.retry, self.module, self.translator.holds)
        catchers: list[dict[str, object]] = []
        for handlers in reversed(self.tries):
            for handler in handlers:
                catcher: dict[str, object] = {"ErrorEquals": handler.errors}
                if handler.variable:
                    error = self.spelling(handler.variable)
                    # The error output is always there in a catcher.
                    caught = expression("$states.errorOutput", defined=True, total=True)
                    catcher["Assign"] = {error: caught}
                catchers.append(catcher)
                # The state's own Assign does not happen when it fails.
                flow = Flow(
                    dict(self.bindings),
                    dict(self.declared),
                    [(catcher, "Next")],
                    dict(self.functions),
                    set(self.partial),
                )
                handler.flows.append(flow)
                if handler.errors == [EVERYTHING]:
                    break
            if catchers and catchers[-1]["ErrorEquals"] == [EVERYTHING]:
                break
        if catchers:
            state["Catch"] = catchers
        return self.add(base, {**state, **fields}, node)

    def within(self) -> tuple[list[Handler], ...]:
        """The try bodies being compiled, outermost first, each as the list of
        its clauses."""
        return tuple(self.tries)

    def compose(
        self, node: ast.Call, function: str
    ) -> tuple[dict[str, object], Type | None]:
        if function == "parallel":
            return self.parallel_state(node)
        if function == "inline_map":
            return self.inline_map_state(node)
        return self.distributed_map_state(node)

    def resolve(self, node: ast.expr, call: str) -> tuple[ast.FunctionDef, bool]:
        """A function passed to parallel() or a map, and whether it is defined
        in the body, where it reads the variables around it."""
        if not isinstance(node, ast.Name):
            raise CompileError(f"pass the function by name: {call}(charge, ...)", node)
        if node.id in self.partial and node.id not in self.functions:
            raise CompileError(
                f"{node.id} is not defined the same way on every path to here; "
                "define the function once, before the if, loop or try",
                node,
            )
        local = node.id in self.functions
        function = self.functions.get(node.id) or self.module.functions.get(node.id)
        if function is None:
            raise CompileError(
                f"{node.id} is not a function defined here; define it with "
                f"def {node.id}(...):",
                node,
            )
        if function.decorator_list:
            raise CompileError(
                f"a function for {call} takes no decorators; remove them", function
            )
        arguments = function.args
        if (
            arguments.posonlyargs
            or arguments.vararg
            or arguments.kwonlyargs
            or arguments.kwarg
            or arguments.defaults
        ):
            raise CompileError(
                f"a function for {call} takes plain parameters only", function
            )
        return function, local

    def own_names(
        self, function: ast.FunctionDef, kept: frozenset[str] = frozenset()
    ) -> ast.FunctionDef:
        """A function run by parallel() or a map, with each name local to it
        that this scope or one around it assigns renamed, but those in kept.
        Step Functions rejects a branch that assigns a variable of the
        machine's, where Python keeps the two apart, so the branch takes a
        name of its own, numbered as a loop's counter is."""
        outside = self.outer | self.assigned | self.hidden
        clashing = sorted((local_names(function) - kept) & outside)
        if not clashing:
            return function
        used = (
            outside
            | self.taken
            | {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
            | set(self.module.spellings.values())
        )
        renaming = {}
        for name in clashing:
            given = name
            serial = 1
            while given in used:
                serial += 1
                given = f"{name}_{serial}"
            used.add(given)
            renaming[name] = given
        return Renamer(renaming).visit(copy.deepcopy(function))

    def child(
        self,
        function: ast.FunctionDef,
        local: bool,
        bindings: dict[str, Expr],
        parameters: set[str],
    ) -> "Scope":
        """A scope for a function run by parallel() or a map: its states are
        named after it and it cannot assign what this scope assigns. A function
        defined here reads this scope, except for the names it binds itself,
        which Python makes local to it from its first line."""
        own = set()
        if local:
            own = local_names(function) - {a.arg for a in function.args.args}
            for name in own:
                bindings.pop(name, None)
        scope = Scope(
            Graph(
                f"{function.name}.",
                self.graph.names,
                self.graph.taken,
                self.graph.written,
            ),
            bindings,
            self.module,
            parameters,
            self.taken | {n.id for n in ast.walk(function) if isinstance(n, ast.Name)},
            self.hidden,
            assigned_names(function.body),
            self.outer | self.assigned | self.hidden,
        )
        scope.labels = self.labels
        scope.locations = self.locations
        scope.optimizing = self.optimizing
        scope.checking = self.checking
        scope.flags = flags(function)
        if local:
            scope.functions = dict(self.functions)
            scope.declared = {n: t for n, t in self.declared.items() if n not in own}
            scope.partial.update(self.partial - own)
            scope.translator.expired.update(self.translator.expired - own)
        return scope

    def run_child(
        self, scope: "Scope", function: ast.FunctionDef
    ) -> tuple[dict[str, object], list[Type | None]]:
        """The states of a function, and the types of what its returns give."""
        scope.materialize(function.body, function, "parameters")
        scope.block(function.body)
        if scope.graph.reachable:
            scope.end_without_value(function, [ended(function)])
        definition = scope.graph.definition()
        if scope.optimizing:
            optimize(definition, scope)
            checked(definition, scope.checking)
        docstring = ast.get_docstring(function)
        if docstring:
            definition = {"Comment": docstring, **definition}
        return definition, scope.returns

    def parallel_state(self, node: ast.Call) -> tuple[dict[str, object], Type | None]:
        """The branches of parallel(): each function's body as a scope of its
        own that reads the variables here, with states named after it."""
        if not node.args:
            raise CompileError(
                "parallel takes the functions to run: parallel(email, audit)", node
            )
        for keyword in node.keywords:
            if keyword.arg != "retry":
                raise CompileError("parallel takes only retry=", keyword)
        branches = []
        returns: list[Type | None] = []
        places: list[Type | None] = []
        for argument in node.args:
            function, local = self.resolve(argument, "parallel")
            function = self.own_names(function)
            if function.args.args:
                raise CompileError(
                    f"a branch takes no parameters; {function.name} reads the "
                    "variables around it instead",
                    function,
                )
            bindings = dict(self.bindings) if local else {}
            scope = self.child(
                function, local, bindings, self.parameters if local else set()
            )
            branch, returned = self.run_child(scope, function)
            branches.append(branch)
            returns.extend(returned)
            places.append(joined(returned))
        state = {"Type": "Parallel", "Branches": branches}
        # The result holds each branch's result at its place.
        return state, Type(frozenset({ARRAY}), joined(returns), positions=tuple(places))

    def options(self, node: ast.Call, allowed: set[str]) -> dict[str, ast.expr]:
        found = {}
        for keyword in node.keywords:
            if keyword.arg not in allowed:
                raise CompileError(
                    f"this map takes {', '.join(sorted(allowed))}=", keyword
                )
            found[keyword.arg] = keyword.value
        return found

    def count_option(
        self, node: ast.expr, name: str, high: int | None = None
    ) -> object:
        """A whole-number field that may also be an expression."""
        value = self.translator.expr(node)
        if isinstance(value.template, (int, float)) and not isinstance(
            value.template, bool
        ):
            if not (
                isinstance(value.template, int)
                and value.template >= 0
                and (high is None or value.template <= high)
            ):
                limit = f" to {high}" if high is not None else " or more"
                raise CompileError(f"{name} is a whole number from 0{limit}", node)
        elif value.type is not None and value.type.kinds != {NUMBER}:
            raise CompileError(
                f"{name} is a number, not {article(value.type.describe())}", node
            )
        return value

    def inline_map_state(self, node: ast.Call) -> tuple[dict[str, object], Type | None]:
        """inline_map(f, items): f takes the item, and its index if it has a
        second parameter. The ItemSelector passes them by parameter name.
        They are read from $states.input, which a Task or a nested Parallel or
        Map replaces, so a function that makes such states binds them first."""
        if len(node.args) != 2:
            raise CompileError(
                "inline_map takes the function and the items: "
                "inline_map(charge, orders)",
                node,
            )
        found = self.options(node, {"max_concurrency", "retry"})
        function, local = self.resolve(node.args[0], "inline_map")
        function = self.own_names(function)
        parameters = function.args.args
        if not 1 <= len(parameters) <= 2:
            raise CompileError(
                "the function of inline_map takes the item, and its index if "
                "needed: def charge(order, index):",
                function,
            )
        items = self.translator.expr(node.args[1])
        if items.type is not None and ARRAY not in items.type.kinds:
            raise CompileError(
                f"{ast.unparse(node.args[1])} is {article(items.type.describe())}; "
                "inline_map takes a list",
                node.args[1],
            )
        item_type = items.type.items if items.type else None
        sources = ["$states.context.Map.Item.Value", "$states.context.Map.Item.Index"]
        kinds = [item_type, of(NUMBER)]
        selector = {
            parameter.arg: expression(source)
            for parameter, source in zip(parameters, sources, strict=False)
        }
        declared = [
            annotate(parameter.annotation, self.module) or kind
            for parameter, kind in zip(parameters, kinds, strict=False)
        ]
        binds = makes_states(
            function, self.module.names, {**self.module.functions, **self.functions}
        )
        shared = (set(self.graph.names), set(self.labels), set(self.hidden))
        mark = marking()
        processor, returned = self.processor(
            function, local, declared, mark if binds else None
        )
        if binds and reads_at_start(processor, mark):
            processor = placed(processor, mark)
        elif binds:
            # A state after the first reads a parameter, where $states.input is
            # no longer the item, so the first state binds them after all.
            restored = (self.graph.names, self.labels, self.hidden)
            for kept, now in zip(shared, restored, strict=True):
                now.clear()
                now.update(kept)
            processor, returned = self.processor(function, local, declared, None)
        state: dict[str, object] = {"Type": "Map", "Items": items}
        state["ItemSelector"] = selector
        if "max_concurrency" in found:
            state["MaxConcurrency"] = self.count_option(
                found["max_concurrency"], "max_concurrency"
            )
        state["ItemProcessor"] = {"ProcessorConfig": {"Mode": "INLINE"}, **processor}
        return state, of(ARRAY, items=joined(returned))

    def processor(
        self,
        function: ast.FunctionDef,
        local: bool,
        declared: list[Type | None],
        mark: str | None,
    ) -> tuple[dict[str, object], list[Type | None]]:
        """The ItemProcessor of an inline map, which reads each parameter from
        $states.input. A function that makes no states reads it there. One
        that does reads it there too, marked with mark, when each read is in its
        first state, whose input is still the item (measured), or in the first
        state of a branch of a Parallel that is; otherwise its first state
        binds the parameters to variables."""
        parameters = function.args.args
        bindings = dict(self.bindings) if local else {}
        pending: dict[str, Expr] = {}
        # The ItemSelector gives each parameter the item or its index, which
        # an array always holds, so reading one is never undefined.
        given = [
            replace(step(expression("$states.input"), p.arg), defined=True, total=True)
            for p in parameters
        ]
        binds = mark is None and makes_states(
            function, self.module.names, {**self.module.functions, **self.functions}
        )
        for parameter, kind, read in zip(parameters, declared, given, strict=True):
            name = parameter.arg
            if binds:
                bindings[name] = self.variable(name, kind)
                pending[name] = read
            elif mark is not None:
                bindings[name] = replace(
                    expression(f"${mark}{name}"), type=kind, defined=True, total=True
                )
            else:
                bindings[name] = replace(read, type=kind)
        scope = self.child(
            function, local, bindings, self.parameters if local else set()
        )
        for parameter in parameters:
            if binds:
                scope.claim(parameter.arg, parameter)
            else:
                self.check_variable(parameter.arg, parameter)
        scope.pending = pending
        scope.pending_first = next(iter(pending), None)
        scope.pending_node = function if pending else None
        if pending:
            scope.pending_origins = [Origin(function, "parameters", header=True)]
        # What the first state binds is the function's to assign.
        scope.assigned |= pending.keys()
        return self.run_child(scope, function)

    def distributed_map_state(
        self, node: ast.Call
    ) -> tuple[dict[str, object], Type | None]:
        """distributed_map(f, items or source=, args=, batch=, ...): each item
        runs as a child execution, whose input is what the ItemSelector (or
        the ItemBatcher) builds. The function reads its parameters from that
        input and nothing from outside."""
        found = self.options(
            node,
            {
                "source",
                "args",
                "batch",
                "result",
                "max_concurrency",
                "tolerated_failure_count",
                "tolerated_failure_percentage",
                "label",
                "execution_type",
                "retry",
            },
        )
        if not 1 <= len(node.args) <= 2:
            raise CompileError(
                "distributed_map takes the function, and the items unless "
                "source= reads them: distributed_map(charge, orders)",
                node,
            )
        if (len(node.args) == 2) == ("source" in found):
            raise CompileError(
                "give the items either as the second argument or as source=, "
                "not both or neither",
                node,
            )
        function, _ = self.resolve(node.args[0], "distributed_map")
        # The parameters are the names in args=.
        function = self.own_names(
            function, frozenset(a.arg for a in function.args.args)
        )
        parameters = [a.arg for a in function.args.args]
        arguments = self.literal_dict(found.get("args"), "args", None, None)
        if not parameters or set(parameters[1:]) != set(arguments):
            raise CompileError(
                "the function of distributed_map takes the item first and then "
                "exactly the names in args=: def charge(order, rate): with "
                'args={"rate": rate}',
                function,
            )
        state: dict[str, object] = {"Type": "Map"}
        if "label" in found:
            text = label(self.translator.holds(found["label"]))
            if text in self.labels:
                raise CompileError(
                    f"another distributed_map is labeled {text}; give each its own "
                    "label",
                    found["label"],
                )
            self.labels.add(text)
            state["Label"] = text
        item_type: Type | None = None
        if len(node.args) == 2:
            items = self.translator.expr(node.args[1])
            if items.type is not None and not {ARRAY, OBJECT} & items.type.kinds:
                raise CompileError(
                    f"{ast.unparse(node.args[1])} is {article(items.type.describe())}; "
                    "distributed_map takes a list or a dict",
                    node.args[1],
                )
            item_type = items.type.items if items.type else None
            state["Items"] = items
        else:
            state["ItemReader"] = self.literal_dict(
                found["source"],
                "source",
                {"Resource", "ReaderConfig", "Arguments"},
                {"Resource"},
            )
        read = expression("$states.context.Execution.Input")
        bindings: dict[str, Expr] = {}
        if "batch" in found:
            batcher = self.literal_dict(
                found["batch"],
                "batch",
                {"MaxItemsPerBatch", "MaxInputBytesPerBatch"},
                None,
            )
            if not batcher:
                raise CompileError(
                    "batch sets MaxItemsPerBatch, MaxInputBytesPerBatch or both",
                    found["batch"],
                )
            if arguments:
                batcher["BatchInput"] = arguments
            state["ItemBatcher"] = batcher
            bindings[parameters[0]] = replace(
                step(read, "Items"), type=of(ARRAY, items=item_type)
            )
            for name in parameters[1:]:
                bindings[name] = step(step(read, "BatchInput"), name)
        else:
            selector: dict[str, object] = {
                parameters[0]: expression("$states.context.Map.Item.Value"),
                **arguments,
            }
            state["ItemSelector"] = selector
            # A field of the ItemSelector that is undefined fails the Map
            # (measured on an inline map, whose ItemSelector is the same
            # field), so each child's input holds every parameter.
            given = {
                name: replace(step(read, name), defined=True, total=True)
                for name in parameters
            }
            bindings[parameters[0]] = replace(given[parameters[0]], type=item_type)
            for name in parameters[1:]:
                bindings[name] = given[name]
        for parameter in function.args.args:
            self.check_variable(parameter.arg, parameter)
            declared = annotate(parameter.annotation, self.module)
            if declared is not None:
                bindings[parameter.arg] = replace(
                    bindings[parameter.arg], type=declared
                )
        for name, field_name, high in (
            ("max_concurrency", "MaxConcurrency", None),
            ("tolerated_failure_count", "ToleratedFailureCount", None),
            ("tolerated_failure_percentage", "ToleratedFailurePercentage", 100),
        ):
            if name in found:
                state[field_name] = self.count_option(found[name], name, high)
        execution = found.get("execution_type")
        execution_type = "STANDARD"
        if execution is not None:
            if not (
                isinstance(execution, ast.Constant)
                and execution.value in {"STANDARD", "EXPRESS"}
            ):
                raise CompileError(
                    'execution_type is "STANDARD" or "EXPRESS"', execution
                )
            execution_type = execution.value
        scope = self.child(function, False, bindings, set())
        scope.translator.isolated = function.name
        scope.translator.local = assigned_names(function.body)
        processor, returned = self.run_child(scope, function)
        state["ItemProcessor"] = {
            "ProcessorConfig": {"Mode": "DISTRIBUTED", "ExecutionType": execution_type},
            **processor,
        }
        if "result" in found:
            state["ResultWriter"] = self.literal_dict(
                found["result"],
                "result",
                {"Resource", "Arguments", "WriterConfig"},
                None,
            )
            return state, None
        return state, of(ARRAY, items=joined(returned))

    def literal_dict(
        self,
        node: ast.expr | None,
        name: str,
        allowed: set[str] | None,
        required: set[str] | None,
    ) -> dict[str, object]:
        """A dict written out in the call, passed through as ASL."""
        if node is None:
            return {}
        node = self.translator.holds(node)
        if not isinstance(node, ast.Dict):
            raise CompileError(f"{name} is a dict written here: {name}={{...}}", node)
        result: dict[str, object] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise CompileError(f"the keys of {name} are strings", key or value)
            if allowed is not None and key.value not in allowed:
                raise CompileError(f"{name} takes {', '.join(sorted(allowed))}", key)
            result[key.value] = self.translator.expr(value)
        missing = (required or set()) - result.keys()
        if missing:
            raise CompileError(f"{name} needs {', '.join(sorted(missing))}", node)
        return result

    def fail(self, node: ast.Raise) -> None:
        """raise as Fail: the class names the Error, the message is the Cause."""
        if node.exc is None:
            if not self.handling:
                raise CompileError(
                    'name the error to raise: raise OrderFailed("...")', node
                )
            # Raise what was caught again, unless a try around it would catch
            # that in Python.
            handling = self.handling[-1]
            for handlers in self.tries:
                for handler in handlers:
                    if (
                        EVERYTHING in handling.errors
                        or EVERYTHING in handler.errors
                        or set(handling.errors) & set(handler.errors)
                    ):
                        raise CompileError(
                            "raise ends the execution with a Fail state, which the "
                            "except around it does not catch; handle the error in "
                            "this clause instead",
                            node,
                        )
            # The Catch assigned the error under the variable's spelling.
            assert handling.variable is not None
            caught_error = self.spelling(handling.variable)
            self.flush()
            state: dict[str, object] = {
                "Type": "Fail",
                "Error": expression(f"${caught_error}.Error"),
                "Cause": expression(f"${caught_error}.Cause"),
            }
            self.add("raise", state, node)
            return
        # A Fail has no chained cause, so `from ...` changes nothing in ASL.
        exception = node.exc
        arguments: list[ast.expr] = []
        if isinstance(exception, ast.Call):
            if len(exception.args) > 1 or exception.keywords:
                raise CompileError(
                    'pass one message: raise OrderFailed("...")', exception
                )
            arguments = exception.args
            exception = exception.func
        error = raised(exception, self.module)
        handler = self.catcher_of(error)
        if handler is not None:
            self.divert(handler, error, arguments, node)
            return
        state: dict[str, object] = {"Type": "Fail", "Error": error}
        cause = self.translator.expr(arguments[0]) if arguments else None
        origins = None
        read = self.raise_pending(arguments[0] if arguments else None)
        if read is not False:
            cause, origins = read
        if cause is not None:
            if cause.type is not None and cause.type.kinds != {STRING}:
                cause = text(cause)
            state["Cause"] = cause
        self.flush()
        self.add("raise", state, node, origins)

    def raise_pending(
        self, message: ast.expr | None
    ) -> tuple[Expr | None, list[Origin]] | Literal[False]:
        """A raise right after assignments that wait for a Pass as a Fail
        alone: nothing reads them after it, and where none fails, is undefined
        or changes on evaluation, Python's evaluating them has no effect, so
        the Pass goes, and so do they where they would go in the Assign of a
        Task, a Parallel or a Map right before them. The message reads them as
        their expressions. The Fail's cause and where it comes from in the
        source, or False where the Pass stays."""
        pending = self.pending
        if not pending or self.pending_name is not None:
            return False
        if not all(v.defined and v.total and not v.volatile for v in pending.values()):
            return False
        cause = None
        if message is not None:
            cause = self.read_as(message, dict(pending))
            if self.read_by_name(message, pending):
                return False
        return cause, self.take_pending([self.here()])

    def catcher_of(self, error: str) -> Handler | None:
        """The except clause a raise of the error goes to, as Python picks it:
        the innermost try first, and its clauses in order."""
        for handlers in reversed(self.tries):
            for handler in handlers:
                if error in handler.errors or handler.errors == [EVERYTHING]:
                    return handler
        return None

    def divert(
        self, handler: Handler, error: str, arguments: list[ast.expr], node: ast.Raise
    ) -> None:
        """A raise the except clause around it catches, as a way into the
        clause instead of a Fail, which no Catch takes. The clause's variable
        holds what a catcher would assign, the Error and the Cause, which is
        an ordinary assignment, so it goes in the Choice rule or the state
        before it where it can."""
        self.catchable += 1
        if handler.variable is not None:
            made: list[ast.expr] = []
            cause: ast.expr = ast.Constant("")
            if arguments:
                cause = arguments[0]
                known = self.translator.expr(cause).type
                if known is None or known.kinds != {STRING}:
                    function = ast.Name("str", ast.Load())
                    cause = ast.Call(function, [cause], [])
                    made += [function, cause]
            else:
                made.append(cause)
            error_key, cause_key = ast.Constant("Error"), ast.Constant("Cause")
            name = ast.Constant(error)
            output = ast.Dict([error_key, cause_key], [name, cause])
            target = ast.Name(handler.variable, ast.Store())
            for new in (*made, error_key, cause_key, name, output, target):
                ast.copy_location(new, node)
            self.assign(target, output, None)
        self.flush()
        handler.flows.append(self.save())
        self.graph.tails = []

    def attempt(self, node: ast.Try) -> None:
        """try as a Catch on each Task in its body. An except clause runs from
        wherever a Task failed, with the variables bound there."""
        if node.finalbody:
            raise CompileError(
                "finally is not supported; run the cleanup after the try and "
                "in each except",
                node.finalbody[0],
            )
        handlers = []
        for position, clause in enumerate(node.handlers):
            if clause.type is None:
                raise CompileError("name what to catch: except Exception", clause)
            types = (
                clause.type.elts
                if isinstance(clause.type, ast.Tuple)
                else [clause.type]
            )
            errors = caught(types, self.module)
            if errors == [EVERYTHING] and position != len(node.handlers) - 1:
                raise CompileError(
                    "except Exception catches every error, so the clauses after "
                    "it never run; move it last",
                    clause,
                )
            variable_name = clause.name
            if variable_name is not None:
                self.claim(variable_name, clause)
            elif reraises(clause.body):
                variable_name = self.fresh("caught", clause)
            handlers.append(Handler(clause, errors, variable_name))
        self.flush()
        self.tries.append(handlers)
        catchable = self.catchable
        self.block(node.body)
        self.flush()
        self.tries.pop()
        if self.catchable == catchable:
            raise CompileError(
                "nothing in this try reports an error to except; only task(), "
                "parallel(), the maps and a raise it catches do",
                node,
            )
        # else runs after the body, outside the reach of the except clauses.
        self.block(node.orelse)
        self.flush()
        ends = [self.save()]
        for handler in handlers:
            if not handler.flows:
                # An inner except Exception catches everything first.
                continue
            self.join(handler.flows)
            if handler.variable:
                self.bindings[handler.variable] = self.variable(
                    handler.variable, ERROR_OUTPUT
                )
                self.partial.discard(handler.variable)
            self.handling.append(handler)
            self.block(handler.node.body)
            self.flush()
            self.handling.pop()
            end = self.save()
            if handler.variable:
                # Python unbinds the name at the end of the clause.
                end.bindings.pop(handler.variable, None)
            ends.append(end)
        self.join(ends)

    def branch(self, node: ast.If) -> None:
        """if / elif / else as one Choice: a rule per test, and Default for the
        else branch or for what follows the if."""
        self.flush()
        tests: list[tuple[ast.expr, list[ast.stmt]]] = []
        headers: list[ast.If] = []
        current = node
        while True:
            tests.append((current.test, current.body))
            headers.append(current)
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            otherwise = current.orelse
            break
        rules: list[dict[str, object]] = []
        bodies: list[tuple[list[ast.stmt], dict[str, Type], dict[str, object]]] = []
        # A later rule is only tested when the earlier ones did not hold.
        failed: dict[str, Type] = {}
        for test, body in tests:
            with self.translator.narrowed(failed):
                condition = self.translator.condition(test)
                when, unless = self.translator.narrowing(test)
            rule: dict[str, object] = {"Condition": condition}
            rules.append(rule)
            bodies.append((body, {**failed, **when}, rule))
            failed = {**failed, **unless}
        state: dict[str, object] = {"Type": "Choice", "Choices": rules}
        origins = [Origin(h, header=True) for h in headers]
        self.add("if", state, node, origins)
        start = self.save()
        ends = []
        for body, proven, rule in bodies:
            ends.append(self.follow(start, proven, (rule, "Next"), body))
        ends.append(self.follow(start, failed, (state, "Default"), otherwise))
        self.join(ends)

    def save(self) -> Flow:
        return Flow(
            dict(self.bindings),
            dict(self.declared),
            list(self.graph.tails),
            dict(self.functions),
            set(self.partial),
        )

    def follow(
        self,
        start: Flow,
        proven: dict[str, Type],
        entry: tuple[dict[str, object], str],
        body: list[ast.stmt],
    ) -> Flow:
        """A branch of a Choice, entered through the transition of entry."""
        self.restore(start)
        for name, declared in proven.items():
            self.bindings[name] = replace(self.bindings[name], type=declared)
        self.graph.tails = [entry]
        self.block(body)
        self.flush()
        return self.save()

    def restore(self, path: Flow) -> None:
        self.bindings.clear()
        self.bindings.update(path.bindings)
        self.declared = dict(path.declared)
        self.graph.tails = list(path.tails)
        self.functions = dict(path.functions)
        # The translator shares the set, so it is changed in place.
        self.partial.clear()
        self.partial.update(path.partial)

    def join(self, paths: list[Flow]) -> None:
        """Continue after branches. A variable is bound if every branch that
        reaches here binds it to the same code, with the types of all of them;
        the variables of two list loops read different expressions under one
        name. A declaration belongs to the name, as the types of the branches
        that declare it. A function is defined if every branch defines the same
        one."""
        reaching = [path for path in paths if path.tails]
        if not reaching:
            self.graph.tails = []
            return
        common = set.intersection(*(set(path.bindings) for path in reaching))
        everywhere = set().union(*(set(path.bindings) for path in reaching))
        bindings = {}
        for name in sorted(common):
            first = reaching[0].bindings[name]
            if any(path.bindings[name].code != first.code for path in reaching):
                continue
            declared = first.type
            for path in reaching[1:]:
                declared = union(declared, path.bindings[name].type)
            boolean = all(path.bindings[name].boolean for path in reaching)
            bindings[name] = replace(first, type=declared, boolean=boolean)
        declared_types: dict[str, Type] = {}
        for name in sorted(set().union(*(set(p.declared) for p in reaching))):
            kinds = [p.declared[name] for p in reaching if name in p.declared]
            kind: Type | None = kinds[0]
            for more in kinds[1:]:
                kind = union(kind, more)
            assert kind is not None
            declared_types[name] = kind
        functions = {}
        defined = set().union(*(set(p.functions) for p in reaching))
        for name in defined:
            candidates = [p.functions.get(name) for p in reaching]
            first_function = candidates[0]
            if first_function is not None and all(
                candidate is first_function for candidate in candidates
            ):
                functions[name] = first_function
        self.restore(
            Flow(
                bindings,
                declared_types,
                [t for p in reaching for t in p.tails],
                functions,
                set().union(*(p.partial for p in reaching)),
            )
        )
        self.partial.update(everywhere - bindings.keys(), defined - functions.keys())

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(
            set(self.graph.states),
            list(self.graph.tails),
            self.graph.start,
            dict(self.bindings),
            dict(self.declared),
            set(self.partial),
            dict(self.pending),
            self.pending_first,
            self.pending_node,
            list(self.pending_origins),
            list(self.pending_remarks),
            self.remark,
            self.result,
            dict(self.result.state) if self.result else {},
            dict(self.folded),
            set(self.hidden),
            set(self.graph.names),
            set(self.labels),
            dict(self.functions),
            len(self.returns),
            [len(h.flows) for handlers in self.tries for h in handlers],
            (len(self.loops), len(self.tries), len(self.handling)),
        )

    def rollback(self, saved: Checkpoint) -> None:
        for name in [n for n in self.graph.states if n not in saved.states]:
            del self.graph.states[name]
        self.graph.names.intersection_update(saved.names)
        self.labels.intersection_update(saved.labels)
        self.functions = dict(saved.functions)
        del self.returns[saved.returns :]
        for container, key in saved.tails:
            container.pop(key, None)
        self.graph.tails = list(saved.tails)
        self.graph.start = saved.start
        self.bindings.clear()
        self.bindings.update(saved.bindings)
        self.declared = dict(saved.declared)
        self.partial.clear()
        self.partial.update(saved.partial)
        self.pending = dict(saved.pending)
        self.pending_first = saved.pending_first
        self.pending_node = saved.pending_node
        self.pending_origins = list(saved.pending_origins)
        self.pending_remarks = list(saved.pending_remarks)
        self.remark = saved.remark
        self.result = saved.result
        self.folded = dict(saved.folded)
        if saved.result is not None:
            saved.result.state.clear()
            saved.result.state.update(saved.resulted)
        self.hidden.intersection_update(saved.hidden)
        # A failed attempt can leave loops and try statements open.
        del self.loops[saved.depth[0] :]
        del self.tries[saved.depth[1] :]
        del self.handling[saved.depth[2] :]
        catches = iter(saved.catches)
        for handlers in self.tries:
            for handler in handlers:
                del handler.flows[next(catches) :]

    def settle(
        self,
        attempt: Callable[[], tuple[Loop, dict[str, Type | None]]],
        body: list[ast.stmt],
    ) -> None:
        """Compile a loop until the types at its head hold for every way back
        to it. An attempt returns the loop and the types its head needs; wider
        ones are tried again from the same point. When a first attempt fails,
        the variables the body assigns are tried once more with no known type:
        `acc = None` before a loop that adds to acc is only null at first.
        A type that grows on every way back, as x = [x] nests a list deeper
        each time, becomes unknown after a few attempts."""
        widened: dict[str, Type | None] = {}
        first: CompileError | None = None
        attempts = 0
        while True:
            saved = self.checkpoint()
            # A way back to the head may bring another value than a boolean,
            # so what the body assigns is not one for sure there.
            for name in assigned_names(body) & self.bindings.keys():
                self.bindings[name] = replace(self.bindings[name], boolean=False)
            for name, declared in widened.items():
                # A range variable is bound inside the attempt, always a number.
                if name in self.bindings:
                    self.bindings[name] = replace(self.bindings[name], type=declared)
            try:
                loop, needed = attempt()
            except CompileError as exc:
                if first is not None:
                    # The relaxed attempt failed too. The one that got further
                    # met the line that is wrong; on the same line, the first
                    # names the types that were known.
                    if (exc.line, exc.column) > (first.line, first.column):
                        raise
                    raise first from None
                reassigned = assigned_names(body) & self.bindings.keys()
                if widened or not reassigned:
                    raise
                self.rollback(saved)
                first = exc
                widened = dict.fromkeys(sorted(reassigned))
                continue
            if all(needed[n] == loop.head[n] for n in needed):
                return
            self.rollback(saved)
            attempts += 1
            if attempts >= MAX_WIDENING:
                needed = {
                    n: t if t == loop.head[n] else None for n, t in needed.items()
                }
            widened = needed

    def enter_loop(self) -> Loop:
        """Start a loop at the current point, with the types there."""
        loop = Loop(
            {n: b.type for n, b in self.bindings.items()}, frozenset(self.functions)
        )
        self.loops.append(loop)
        return loop

    def back(self, loop: Loop, flows: list[Flow], head: str) -> dict[str, Type | None]:
        """Link the flows that return to the head of a loop, and the types the
        head needs for them."""
        needed = dict(loop.head)
        for flow in flows:
            for container, key in flow.tails:
                container[key] = head
            for name, declared in needed.items():
                if name in flow.bindings:
                    needed[name] = union(declared, flow.bindings[name].type)
        return needed

    def leave(self, node: ast.Break | ast.Continue) -> None:
        depth = self.expansions[-1].depth if self.expansions else 0
        if len(self.loops) <= depth:
            raise CompileError(
                f"{'break' if isinstance(node, ast.Break) else 'continue'} "
                "is only for loops",
                node,
            )
        self.flush()
        flow = self.save()
        loop = self.loops[-1]
        (loop.breaks if isinstance(node, ast.Break) else loop.continues).append(flow)
        self.graph.tails = []

    def no_else(self, node: ast.For | ast.While) -> None:
        if node.orelse:
            raise CompileError(
                "loops with else are not supported; set a flag before break "
                "and test it after the loop",
                node.orelse[0],
            )

    def while_loop(self, node: ast.While) -> None:
        """while as a Choice that the body leads back to. while True has no
        Choice: the body leads back to its first state."""
        self.no_else(node)
        self.flush()
        forever = isinstance(node.test, ast.Constant) and node.test.value is True

        def attempt() -> tuple[Loop, dict[str, Type | None]]:
            loop = self.enter_loop()
            before = set(self.graph.states)
            start = self.save()
            if forever:
                exits: list[Flow] = []
                self.opening = True
                self.block(node.body)
                self.flush()
                added = [n for n in self.graph.states if n not in before]
                if not added:
                    raise CompileError(
                        "this loop does nothing and never ends; give it a body "
                        "or remove it",
                        node,
                    )
                head: str = added[0]
            else:
                condition = self.translator.condition(node.test)
                when, unless = self.translator.narrowing(node.test)
                rule: dict[str, object] = {"Condition": condition}
                state: dict[str, object] = {"Type": "Choice", "Choices": [rule]}
                origins = [Origin(node, header=True)]
                head = self.add("while", state, node, origins)
                self.follow(start, when, (rule, "Next"), node.body)
                exits = [self.narrow_flow(start, unless, [(state, "Default")])]
            self.loops.pop()
            needed = self.back(loop, [self.save(), *loop.continues], head)
            self.join([*exits, *loop.breaks])
            self.partial.update(
                assigned_names(node.body) - self.bindings.keys(),
                defined_functions(node.body) - self.functions.keys(),
            )
            return loop, needed

        self.settle(attempt, node.body)

    def narrow_flow(
        self,
        start: Flow,
        proven: dict[str, Type],
        tails: list[tuple[dict[str, object], str]],
    ) -> Flow:
        bindings = dict(start.bindings)
        for name, declared in proven.items():
            bindings[name] = replace(bindings[name], type=declared)
        return Flow(
            bindings,
            dict(start.declared),
            tails,
            dict(start.functions),
            set(start.partial),
        )

    def check_variable(self, name: str, node: ast.AST) -> None:
        """A name longer than Step Functions takes, as written or as spelled
        in the definition; spellings() renames the others it would not
        accept."""
        spelled = self.spelling(name)
        if len(spelled) <= MAX_VARIABLE:
            return
        if spelled != name:
            raise CompileError(
                f"{name} is written {spelled} in the definition, and Step Functions "
                f"variable names are at most {MAX_VARIABLE} characters; use a "
                "shorter name",
                node,
            )
        raise CompileError(
            f"Step Functions variable names are at most {MAX_VARIABLE} characters; "
            "use a shorter name",
            node,
        )

    def fresh(self, base: str, node: ast.AST) -> str:
        """A variable of the loop's or the handler's own, named after what it
        holds. The callers build base on the spelling of the Python name, as
        `_x_index` for `_x` would start with `_`, and name nothing after a
        function the generated expressions call, which it would hide."""
        name = base
        serial = 1
        while (
            name in self.taken
            or name in self.hidden
            or name in self.bindings
            or name in self.module.spellings.values()
        ):
            serial += 1
            name = f"{base}_{serial}"
        if len(name) > MAX_VARIABLE:
            raise CompileError(
                f"the definition needs a variable {name} here, and Step Functions "
                f"variable names are at most {MAX_VARIABLE} characters; use a "
                "shorter name",
                node,
            )
        self.hidden.add(name)
        return name

    def for_loop(self, node: ast.For) -> None:
        """for over a list, the keys of a dict, or a range, as a counter the
        body leads back to through its increment. Two variables come from
        enumerate(), zip() or d.items()."""
        self.no_else(node)
        if unpacked(node.iter):
            self.unpacking_loop(node)
            return
        if not isinstance(node.target, ast.Name):
            raise CompileError(
                "loop over one variable: for item in items (unpack inside the loop)",
                node.target,
            )
        target = node.target.id
        counting = is_range(node)
        if counting:
            self.claim(target, node.target)
        else:
            self.loop_variable(node.target)
        assigned = assigned_names(node.body)
        if counting:
            if target in assigned:
                raise CompileError(
                    f"{target} counts the loop; assign the new value to another name",
                    node.target,
                )
            self.range_loop(node, target, assigned)
            return
        items, kind = self.iterated(node.iter)

        def attempt() -> tuple[Loop, dict[str, Type | None]]:
            source = self.listed(items, kind, target, assigned, node)
            counter = self.fresh(f"{self.spelling(target)}_index", node)
            index = self.count_from_zero(counter, node)
            limit = call("count", [source], of(NUMBER))
            return self.counted(
                node, {target: index_expr(source, index)}, counter, index, limit
            )

        self.settle(attempt, node.body)

    def loop_variable(self, target: ast.Name) -> None:
        """The variable of a list or dict loop is an expression, not a Step
        Functions variable, so only its name is checked."""
        if target.id in self.parameters:
            raise CompileError(
                f"{target.id} is the execution input; name the loop variable otherwise",
                target,
            )
        self.check_variable(target.id, target)

    def iterated(self, node: ast.expr) -> tuple[Expr, str]:
        """What a loop iterates: a list, or a dict for its keys."""
        items = self.translator.expr(node)
        kind = self.translator.known(
            node, items, "list", "for depends on what it iterates"
        )
        if kind not in {ARRAY, OBJECT}:
            raise CompileError(
                f"{ast.unparse(node)} is {article(kind)}; for iterates lists, "
                "the keys of dicts and range()",
                node,
            )
        return items, kind

    def kept(self, items: Expr, target: str, assigned: set[str], node: ast.For) -> Expr:
        """What a loop iterates, kept in a variable of the loop's own when the
        body changes it, or evaluating it again would give other items, so the
        loop keeps the value it started with, as Python does."""
        if not (items.variables & assigned or items.volatile):
            return items
        if items.variables & self.pending.keys():
            self.flush()
        copy = self.fresh(f"{self.spelling(target)}_items", node)
        self.defer(copy, items, node, starting(node))
        return self.variable(copy, items.type)

    def listed(
        self, items: Expr, kind: str, target: str, assigned: set[str], node: ast.For
    ) -> Expr:
        """The list a loop counts over: the items, or the keys of a dict."""
        source = self.kept(items, target, assigned, node)
        if kind == OBJECT:
            return call("keys", [source], of(ARRAY, items=of(STRING)))
        return source

    def count_from_zero(self, counter: str, node: ast.For) -> Expr:
        """The counter of a loop, assigned 0 before it. The i of enumerate is
        the program's own name, which may have a value pending: the 0 takes
        its place where that value leaves nothing to evaluate, and a state of
        its own keeps one that may fail, as Python evaluates it first."""
        first = self.pending.get(counter)
        if first is not None and not replaceable(
            first, literal(0), self.spelling(counter)
        ):
            self.flush()
        self.defer(counter, literal(0), node, starting(node))
        return self.variable(counter, of(NUMBER))

    def unpacking_loop(self, node: ast.For) -> None:
        """for i, item in enumerate(items), for a, b in zip(xs, ys) and
        for k, v in d.items(): one counter, and each variable an expression of
        it, as the variable of a list loop is. The i of enumerate is the counter
        itself, so the body cannot assign it."""
        assert isinstance(node.iter, ast.Call)
        form = unpacked(node.iter)
        assert form is not None
        example = UNPACKED[form]
        names = node.target.elts if isinstance(node.target, ast.Tuple) else []
        if len(names) != 2 or not all(isinstance(n, ast.Name) for n in names):
            raise CompileError(f"{form}() gives two variables: {example}", node.target)
        first, second = names
        assert isinstance(first, ast.Name) and isinstance(second, ast.Name)
        if first.id == second.id:
            raise CompileError(
                f"the two loop variables need different names: {example}", second
            )
        assigned = assigned_names(node.body)
        counted = form == "enumerate"
        if counted:
            if len(node.iter.args) != 1 or node.iter.keywords:
                raise CompileError(
                    f"enumerate() counts from 0; add the start to {first.id} in "
                    f"the body: {example}",
                    node.iter,
                )
            self.claim(first.id, first)
            if first.id in assigned:
                raise CompileError(
                    f"{first.id} is the index of enumerate and cannot be assigned; "
                    f"copy it: j = {first.id}",
                    first,
                )
        else:
            self.loop_variable(first)
        self.loop_variable(second)
        if form == "zip" and (len(node.iter.args) != 2 or node.iter.keywords):
            raise CompileError(
                f"zip() takes two lists in a loop: {example}; for more, count "
                "with range: for i in range(len(xs))",
                node.iter,
            )
        if form == "items" and (node.iter.args or node.iter.keywords):
            raise CompileError(f"items() is written {example}", node.iter)
        if form == "items":
            assert isinstance(node.iter.func, ast.Attribute)
            mapping = self.translator.operand(
                node.iter.func.value, OBJECT, f"items() is a dict method: {example}"
            )
            self.settle(
                lambda: self.items_loop(node, first.id, second.id, mapping, assigned),
                node.body,
            )
            return
        lists = [self.iterated(argument) for argument in node.iter.args]

        def attempt() -> tuple[Loop, dict[str, Type | None]]:
            if counted:
                source = self.listed(*lists[0], second.id, assigned, node)
                index = self.count_from_zero(first.id, node)
                self.bindings[first.id] = index
                self.partial.discard(first.id)
                targets = {first.id: index, second.id: index_expr(source, index)}
                limit = call("count", [source], of(NUMBER))
                return self.counted(node, targets, first.id, index, limit)
            left = self.listed(*lists[0], first.id, assigned, node)
            right = self.listed(*lists[1], second.id, assigned, node)
            counter = self.fresh(f"{self.spelling(first.id)}_index", node)
            index = self.count_from_zero(counter, node)
            targets = {
                first.id: index_expr(left, index),
                second.id: index_expr(right, index),
            }
            counts = [
                call("count", [left], of(NUMBER)),
                call("count", [right], of(NUMBER)),
            ]
            limit = call("min", [array(counts)], of(NUMBER))
            return self.counted(node, targets, counter, index, limit)

        self.settle(attempt, node.body)

    def items_loop(
        self, node: ast.For, key: str, value: str, mapping: Expr, assigned: set[str]
    ) -> tuple[Loop, dict[str, Type | None]]:
        """for k, v in d.items(): the keys counted as a dict loop counts them,
        and v read under the key."""
        source = self.kept(mapping, key, assigned, node)
        keys = call("keys", [source], of(ARRAY, items=of(STRING)))
        counter = self.fresh(f"{self.spelling(key)}_index", node)
        index = self.count_from_zero(counter, node)
        each = index_expr(keys, index)
        values = source.type.values if source.type else None
        targets = {key: each, value: call("lookup", [source, each], values)}
        limit = call("count", [keys], of(NUMBER))
        return self.counted(node, targets, counter, index, limit)

    def range_loop(self, node: ast.For, target: str, assigned: set[str]) -> None:
        assert isinstance(node.iter, ast.Call)
        start, stop, step = self.translator.range_arguments(node.iter)

        def attempt() -> tuple[Loop, dict[str, Type | None]]:
            if start.variables & self.pending.keys():
                self.flush()
            limit = stop
            # The loop assigns its variable too, so a stop that reads it is
            # kept from before the first assignment, as is one that would give
            # another number when it is evaluated again.
            if stop.variables & (assigned | {target}) or stop.volatile:
                if stop.variables & self.pending.keys():
                    self.flush()
                copy = self.fresh(f"{self.spelling(target)}_stop", node)
                self.defer(copy, stop, node, starting(node))
                limit = self.variable(copy, of(NUMBER))
            first = self.pending.get(target)
            if first is not None and not replaceable(
                first, start, self.spelling(target)
            ):
                self.flush()
            self.defer(target, start, node, starting(node))
            self.bindings[target] = self.variable(target, of(NUMBER))
            self.partial.discard(target)
            counter = self.variable(target, of(NUMBER))
            assert isinstance(step.template, int)
            return self.counted(
                node, {target: counter}, target, counter, limit, step, step.template < 0
            )

        self.settle(attempt, node.body)

    def counted(
        self,
        node: ast.For,
        targets: dict[str, Expr],
        counter: str,
        index: Expr,
        limit: Expr,
        step: Expr | None = None,
        down: bool = False,
    ) -> tuple[Loop, dict[str, Type | None]]:
        """The loop shared by lists, dicts and ranges: a Choice on the counter,
        the body with the loop variables bound to what targets gives them, and
        the increment."""
        step = literal(1) if step is None else step
        self.flush()
        loop = self.enter_loop()
        start = self.save()
        comparison = ">" if down else "<"
        condition = binary(index, comparison, limit, COMPARE, of(BOOLEAN), True)
        rule: dict[str, object] = {"Condition": condition}
        state: dict[str, object] = {"Type": "Choice", "Choices": [rule]}
        origins = [Origin(node, header=True)]
        head = self.add("for", state, node, origins)
        self.restore(start)
        self.graph.tails = [(rule, "Next")]
        self.bindings.update(targets)
        self.materialize(node.body, node, "loop variables")
        self.block(node.body)
        self.loops.pop()
        increment = binary(index, "+", step, ADD, of(NUMBER))
        if loop.continues:
            # continue goes to the increment, so it gets a state of its own.
            self.flush()
            self.join([self.save(), *loop.continues])
        if self.graph.reachable:
            # Only the loop assigns its counter, so the increment joins
            # whatever the body left pending, and after a Task it reads
            # nothing the Task assigns.
            self.defer(counter, increment, node, Origin(node, "loop step", header=True))
            self.flush()
        body_end = self.save()
        for target in targets.keys() - {counter}:
            body_end.bindings.pop(target, None)
        needed = self.back(loop, [body_end], head)
        exit_flow = Flow(
            dict(start.bindings),
            dict(start.declared),
            [(state, "Default")],
            dict(start.functions),
            set(start.partial),
        )
        # The loop variables end with the loop. A counter counts past its last
        # value, and keeps no value from before an empty range.
        for target in targets:
            exit_flow.bindings.pop(target, None)
            for flow in loop.breaks:
                flow.bindings.pop(target, None)
            self.translator.expired.add(target)
        self.join([exit_flow, *loop.breaks])
        # What only the body assigns may be unassigned after zero iterations.
        body_only = assigned_names(node.body) - self.bindings.keys() - targets.keys()
        self.partial.update(
            body_only, defined_functions(node.body) - self.functions.keys()
        )
        return loop, needed

    def wait(self, node: ast.Call) -> None:
        self.flush()
        until = [k for k in node.keywords if k.arg == "until"]
        if node.args and not node.keywords and len(node.args) == 1:
            value = self.translator.expr(node.args[0])
            seconds = value.template
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                if not (isinstance(seconds, int) and 0 <= seconds <= MAX_WAIT):
                    raise CompileError(
                        f"Wait takes whole seconds from 0 to {MAX_WAIT:,}: wait(10)",
                        node.args[0],
                    )
            elif value.type is not None and value.type.kinds != {NUMBER}:
                raise CompileError(
                    f"{ast.unparse(node.args[0])} is {article(value.type.describe())}; "
                    "wait takes seconds, or a timestamp as until=",
                    node.args[0],
                )
            field = {"Seconds": value}
        elif until and len(node.keywords) == 1 and not node.args:
            # A datetime is written as the timestamp text Timestamp takes.
            moment = self.translator.datetime_string(until[0].value)
            value = moment or self.translator.expr(until[0].value)
            literal_timestamp = until[0].value
            if isinstance(literal_timestamp, ast.Constant) and isinstance(
                literal_timestamp.value, str
            ):
                if not TIMESTAMP.fullmatch(literal_timestamp.value):
                    raise CompileError(
                        "Wait timestamps are UTC with T and Z: "
                        'wait(until="2026-09-13T01:59:00Z")',
                        until[0].value,
                    )
            elif value.type is not None and value.type.kinds != {STRING}:
                raise CompileError(
                    f"{ast.unparse(until[0].value)} is {article(value.type.describe())}; "
                    "until takes a timestamp string",
                    until[0].value,
                )
            field = {"Timestamp": value}
        else:
            raise CompileError(
                "wait takes seconds or until=: wait(10) or "
                'wait(until=input["resumeAt"])',
                node,
            )
        state: dict[str, object] = {"Type": "Wait", **field}
        self.add("wait", state, node)


# How a loop over two variables is written, by what gives them.
UNPACKED = {
    "enumerate": "for i, item in enumerate(items)",
    "zip": "for a, b in zip(xs, ys)",
    "items": "for k, v in d.items()",
}

# What to write instead of the statements the language leaves out, by node.
STATEMENTS = {
    "With": "with is not supported; Step Functions has nothing to open or close, "
    "so write the body without it",
    "AsyncWith": "async with is not supported; write the body without it",
    "AsyncFor": "async for is not supported; write for",
    "Match": "match is not supported; write if / elif / else",
    "Global": "global is not supported; return the value and assign it where the "
    "function is called",
    "Nonlocal": "nonlocal is not supported; return the value and assign it where "
    "the function is called",
    "Import": "import inside a state machine is not supported; import at the top "
    "of the module",
    "ImportFrom": "import inside a state machine is not supported; import at the "
    "top of the module",
    "Delete": "del is not supported; stop reading the variable, or assign it "
    "another value",
    "ClassDef": "a class inside a state machine is not supported; define error "
    "classes at the top of the module",
    "AsyncFunctionDef": "async def is not supported; write def",
    "TryStar": "except* is not supported; write except",
    "TypeAlias": "type aliases are not supported; annotate the values instead",
    "Expr": "a value on a line of its own does nothing in a state machine; assign "
    "it or remove the line",
}

# The methods that change a list or a dict in place, by the values that have
# them.
LIST_CHANGES = frozenset({"append", "extend", "insert", "remove", "sort", "reverse"})
DICT_CHANGES = frozenset({"update", "setdefault", "popitem"})
BOTH_CHANGES = frozenset({"pop", "clear"})


def changing_call(call: ast.Call, parameters: Iterable[str]) -> str | None:
    """What to write for a call on a line of its own that changes a list or a
    dict in place, or for print(); None for any other call. The rewrite names
    the receiver only where it is a name, and for the execution input, which
    cannot be assigned, a name of its own: the new value is then read there."""
    if isinstance(call.func, ast.Name) and call.func.id == "print":
        return "print() has nothing to write to in a state machine; remove the line"
    if not isinstance(call.func, ast.Attribute):
        return None
    method = call.func.attr
    if method in LIST_CHANGES:
        kind = "a list"
    elif method in DICT_CHANGES:
        kind = "a dict"
    elif method in BOTH_CHANGES:
        kind = "a list or a dict"
    else:
        return None
    receiver = call.func.value
    held = ast.unparse(receiver)
    shown = f"{held}.{method}() is not supported; {kind} is a value here, so"
    value = new_value(method, held, call)
    if isinstance(receiver, ast.Name) and receiver.id not in parameters:
        if value is None:
            return f"{shown} build the new value and assign it to {held}"
        return f"{shown} write {held} = {value}"
    name = "items" if kind == "a list" else "data"
    if value is None:
        return (
            f"{shown} build the new value, assign it to another name, such as "
            f"{name}, and read that name from then on"
        )
    return f"{shown} write {name} = {value} and read {name} from then on"


def new_value(method: str, held: str, call: ast.Call) -> str | None:
    """The value a change in place leaves, written as a new one, where one
    spelling writes it."""
    args = [ast.unparse(a) for a in call.args]
    if any(isinstance(a, ast.Starred) for a in call.args):
        return None
    keywords = [k for k in call.keywords if k.arg is not None]
    if len(keywords) != len(call.keywords):
        return None
    if method == "append" and len(args) == 1 and not keywords:
        return f"{held} + [{args[0]}]"
    if method == "extend" and len(args) == 1 and not keywords:
        return f"{held} + {args[0]}"
    if (
        method == "insert"
        and len(args) == 2
        and not keywords
        and isinstance(call.args[0], (ast.Constant, ast.Name))
    ):
        # The position is written twice, so only one that reads the same
        # each time.
        i, x = args
        return f"{held}[:{i}] + [{x}] + {held}[{i}:]"
    if (
        method == "sort"
        and not args
        and all(
            (k.arg == "key" and isinstance(k.value, ast.Lambda))
            or (
                k.arg == "reverse"
                and isinstance(k.value, ast.Constant)
                and isinstance(k.value.value, bool)
            )
            for k in keywords
        )
    ):
        # sorted() takes a lambda for key= and True or False for reverse=.
        return f"sorted({', '.join([held, *map(ast.unparse, keywords)])})"
    if method == "reverse" and not args and not keywords:
        return f"list(reversed({held}))"
    if method == "update" and not args and keywords:
        entries = ", ".join(f"{k.arg!r}: {ast.unparse(k.value)}" for k in keywords)
        return f"{{**{held}, {entries}}}"
    if method == "update" and len(args) == 1 and not keywords:
        given = call.args[0]
        if (
            isinstance(given, ast.Dict)
            and given.keys
            and all(
                isinstance(k, ast.Constant) and isinstance(k.value, str)
                for k in given.keys
            )
        ):
            return f"{{**{held}, {args[0][1:-1]}}}"
    return None


LABEL_FORBIDDEN = set(' ?*<>{}[]:;,\\|^~$#%&`"')


def label(node: ast.expr) -> str:
    """A Map Run label: at most 40 characters, without whitespace, wildcards,
    brackets, special or control characters."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        raise CompileError("label is a literal string", node)
    text = node.value
    if (
        not text
        or len(text) > 40
        or any(
            c in LABEL_FORBIDDEN or c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159
            for c in text
        )
    ):
        raise CompileError(
            "label is 1 to 40 characters without spaces, ? * < > { } [ ] : ; , "
            '\\ | ^ ~ $ # % & ` or "',
            node,
        )
    return text


def makes_state(target: str | None) -> bool:
    """Whether a call of target makes a state: task(), activity(), parallel(),
    a map, or an operation of aws.sdk or aws.optimized."""
    if target is None:
        return False
    segments = target.split(".")
    if segments[:2] == ["sfnx", "aws"]:
        # Everything called there is an operation but an error class, which
        # the translation of the call tells apart from a misspelled one.
        return not (len(segments) == 6 and segments[4] == "errors")
    return target in {f"sfnx.{name}" for name in STATE_CALLS}


def conditional_assignment(node: ast.If) -> tuple[ast.Name, ast.expr, bool] | None:
    """The variable an if assigns in each branch and nothing else, the
    conditional expression that gives its value, and whether every branch
    assigns it, so that an if without else keeps the value it had."""
    assigned = alone(node.body)
    if assigned is None:
        return None
    target, then = assigned
    otherwise: ast.expr
    if not node.orelse:
        otherwise = ast.copy_location(ast.Name(target.id, ast.Load()), target)
        complete = False
    elif len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        inner = conditional_assignment(node.orelse[0])
        if inner is None or inner[0].id != target.id:
            return None
        _, otherwise, complete = inner
    else:
        other = alone(node.orelse)
        if other is None or other[0].id != target.id:
            return None
        otherwise = other[1]
        complete = True
    value = ast.IfExp(node.test, then, otherwise)
    return target, ast.copy_location(value, node), complete


def replaceable(first: Expr, value: Expr, spelled: str) -> bool:
    """Whether a pending first value of a name can give way to the new value
    in node in the same state: it changes on no evaluation and is never
    undefined, and it either cannot fail, leaving nothing to evaluate, or the
    new value evaluates it every time."""
    return (
        not first.volatile
        and first.defined
        and (first.total or strictness(value.code, spelled) is Strictness.ALWAYS)
    )


def kept(first: Expr, others: list[Expr]) -> bool:
    """Whether a pending first value of a name that the new value replaces
    is still evaluated, as another name in the same Assign takes it whole,
    as n1 does in n0, n1 = n1, n0: the Assign then fails where the first
    value fails or is undefined, as Python fails evaluating it. One that
    changes on evaluation would be evaluated once there, where the two
    assignments read it apart."""
    return not first.volatile and any(o.code == first.code for o in others)


def branches(value: ast.expr) -> list[ast.expr]:
    """The values a conditional expression chooses among, nested ones included."""
    if isinstance(value, ast.IfExp):
        return [*branches(value.body), *branches(value.orelse)]
    return [value]


def flags(function: ast.FunctionDef) -> dict[str, list[ast.Compare]]:
    """The comparisons in a function of a variable with a value written in the
    source, by ==, !=, is or is not, on either side, by variable."""
    found: dict[str, list[ast.Compare]] = {}
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
        ):
            continue
        sides = [node.left, node.comparators[0]]
        if any(isinstance(side, ast.Constant) for side in sides):
            for side in sides:
                if isinstance(side, ast.Name):
                    found.setdefault(side.id, []).append(node)
    return found


def alone(statements: list[ast.stmt]) -> tuple[ast.Name, ast.expr] | None:
    """The variable and the value of a block that is one assignment, x += v
    giving x + v."""
    if len(statements) != 1:
        return None
    statement = statements[0]
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0], statement.value
    if isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name):
        target = statement.target
        reading = ast.copy_location(ast.Name(target.id, ast.Load()), target)
        value = ast.BinOp(reading, statement.op, statement.value)
        return target, ast.copy_location(value, statement)
    return None


def makes_states(
    function: ast.FunctionDef,
    names: dict[str, str],
    functions: dict[str, ast.FunctionDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether a function makes a state anywhere, with a Task, parallel() or a
    map, or calls directly a function that does."""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if makes_state(qualified(node.func, names)):
            return True
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in functions
            and node.func.id not in seen
            and makes_states(
                functions[node.func.id], names, functions, seen | {node.func.id}
            )
        ):
            return True
    return False


class Renamer(ast.NodeTransformer):
    """Rename names where they are read, assigned, or bound by a parameter or
    an except clause."""

    def __init__(self, renaming: dict[str, str]):
        self.renaming = renaming

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self.renaming.get(node.id, node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = self.renaming.get(node.arg, node.arg)
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        if node.name is not None:
            node.name = self.renaming.get(node.name, node.name)
        self.generic_visit(node)
        return node


def renamed(statements: list[ast.stmt], renaming: dict[str, str]) -> list[ast.stmt]:
    """A copy of statements with each name renamed as renaming says."""
    renamer = Renamer(renaming)
    return [renamer.visit(statement) for statement in copy.deepcopy(statements)]


def joined(types: list[Type | None]) -> Type | None:
    """What may come from any of several places; unknown from none, as a
    function whose every path raises returns nothing."""
    if not types:
        return None
    result = types[0]
    for kind in types[1:]:
        result = union(result, kind)
    return result


def local_names(function: ast.FunctionDef) -> set[str]:
    """The names Python makes local to a function, its parameters included."""
    table = symtable.symtable(ast.unparse(function), "<function>", "exec")
    namespace = table.lookup(function.name).get_namespace()
    assert isinstance(namespace, symtable.Function)
    return set(namespace.get_locals())


def only_return(function: ast.FunctionDef) -> ast.expr | None:
    """The value a function returns where its body, past a docstring, is one
    return of a value, or None where the body is anything else."""
    body = (
        function.body[1:] if ast.get_docstring(function) is not None else function.body
    )
    if len(body) == 1 and isinstance(body[0], ast.Return):
        return body[0].value
    return None


def global_names(function: ast.FunctionDef) -> set[str]:
    """The names a function and its lambdas and comprehensions read from the
    module."""
    table = symtable.symtable(ast.unparse(function), "<function>", "exec")
    pending = [table.lookup(function.name).get_namespace()]
    names: set[str] = set()
    while pending:
        scope = pending.pop()
        assert isinstance(scope, symtable.SymbolTable)
        names |= {s.get_name() for s in scope.get_symbols() if s.is_global()}
        pending.extend(scope.get_children())
    return names


def is_range(node: ast.For) -> bool:
    return (
        isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "range"
    )


def assigned_names(statements: list[ast.stmt]) -> set[str]:
    """The variables a block assigns: assignments, range loop variables and
    except names, but not the variables of list loops (expressions) or of the
    functions it defines."""
    names = set()
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        children = list(ast.iter_child_nodes(statement))
        if isinstance(statement, ast.For) and not is_range(statement):
            children = [statement.iter, *statement.body, *statement.orelse]
            if unpacked(statement.iter) == "enumerate" and isinstance(
                statement.target, ast.Tuple
            ):
                # The i of enumerate is the counter, a variable of its own.
                children.insert(0, statement.target.elts[0])
        for node in children:
            if isinstance(node, ast.stmt):
                names |= assigned_names([node])
            elif isinstance(node, ast.ExceptHandler):
                if node.name:
                    names.add(node.name)
                names |= assigned_names(node.body)
            else:
                names |= stored(node)
    return names


def stored(node: ast.AST) -> set[str]:
    """The names an expression assigns, without the variables of its
    comprehensions, which are the comprehension's own."""
    if isinstance(node, ast.Name):
        return {node.id} if isinstance(node.ctx, ast.Store) else set()
    children = ast.iter_child_nodes(node)
    if isinstance(node, ast.comprehension):
        children = iter([node.iter, *node.ifs])
    return set().union(*(stored(child) for child in children))


def defined_functions(statements: list[ast.stmt]) -> set[str]:
    """The functions a block defines, in its branches, loops and handlers too,
    but not inside the functions it defines."""
    names = set()
    for statement in statements:
        if isinstance(statement, ast.FunctionDef):
            names.add(statement.name)
            continue
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                names |= defined_functions([child])
            elif isinstance(child, ast.ExceptHandler):
                names |= defined_functions(child.body)
    return names


def reraises(statements: list[ast.stmt]) -> bool:
    """Whether an except clause raises what it caught anywhere in its body."""
    for statement in statements:
        if isinstance(statement, ast.Raise) and statement.exc is None:
            return True
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt) and reraises([child]):
                return True
    return False


def annotate(node: ast.expr | None, context: Module) -> Type | None:
    if node is None:
        return None
    try:
        return annotation(node, context.typed)
    except AnnotationError as exc:
        raise CompileError(str(exc), exc.node) from exc


def starting(loop: ast.For) -> Origin:
    """What a for loop assigns before its first iteration."""
    return Origin(loop, "loop start", header=True)


def fold_into_catching_tasks(
    definition: dict[str, object],
    enclosing: Enclosing,
) -> None:
    """The Pass or the Succeed right after a Task, a Parallel or a Map with a
    Catch, in the state's Assign or as its Output, as a hand-writer assigns
    and returns in the state whose failures the Catch takes: a failure of the
    Assign or the Output of each of the three is caught (measured), and
    inside a try, Python's except takes a failure of those statements too,
    which the separate state lets end the execution. Only the state leads to
    it, it is in the same try bodies as the state, whose Catch is theirs, or
    the Catch takes no failure of an expression, which then ends the
    execution in the state as it would after it, and the state retries
    nothing on `States.ALL` or `States.QueryEvaluationError`, which would
    run it again. A statement after the try, which only the state leads to
    when every except clause ends, is not in the try's reach, unless nothing
    in it can fail, as failsafe says, which gives the Catch nothing to take;
    a catcher of the state may lead to such a Succeed too, which stays for
    the catcher.

    A failure in the Assign loses all of it, the state's result included,
    where Python keeps what was assigned before the failing statement, so no
    way on from a catcher may read a variable assigned before a statement
    that may fail, unless nothing in the Pass or the Succeed can fail, so a
    failure of the Assign is the state's own, as in Python.
    It reads the variables the state assigns as the expressions the state
    assigns them, but for one that changes on evaluation, which the state's
    Assign evaluates already, and nothing else of `$states` than the context
    the two share."""
    # This takes away UD-PASS-NOT-CATCHABLE where it can.
    states = definition["States"]
    assert isinstance(states, dict)
    folded = True
    while folded:
        folded = False
        led = leading(states)
        for name, task in states.items():
            after = task.get("Next")
            safe = after is not None and failsafe(
                {k: v for k, v in states[after].items() if k in {"Assign", "Output"}}
            )
            if (
                task["Type"] not in {"Task", "Parallel", "Map"}
                or "Catch" not in task
                or "Output" in task
                or after is None
                or set(led[after]) != {name}
            ):
                continue
            where = "fold_into_catching_tasks"
            if refused(
                None
                if safe
                or not takes_evaluation(task, "Catch")
                or same_tries(enclosing.get(after, ()), enclosing[name])
                else Reject.OTHER_HANDLERS,
                where,
            ) or refused(
                Reject.HANDLER_TAKES_FAILURE if retries_evaluation(task) else None,
                where,
            ):
                continue
            following = states[after]
            kind = following["Type"]
            # A catcher that leads there too still needs the state; a Succeed
            # stays for it, where a Pass would have to be written twice.
            shared = led[after].count(name) > 1
            if shared and kind != "Succeed":
                continue
            fields = set(following) - {"Type", "Comment"}
            if not (
                (kind == "Pass" and fields == {"Assign", "Next"})
                or (kind == "Succeed" and fields == {"Output"})
            ):
                continue
            codes = expressions_in({k: v for k, v in following.items() if k != "Next"})
            if refused(context_invariant(codes, Differs.STATES), where):
                continue
            own = task.get("Assign", {})
            assert isinstance(own, dict)
            values = resolve_reads(own, codes, where)
            if values is None or refused(captures(following, values), where):
                continue
            # The state's Assign still evaluates what the Pass does not
            # assign again.
            kept = set(values) - set(assigns(following))
            if refused(evaluated_once(values, codes, kept), where):
                continue
            exposed = assigned_before_failures(own, following)
            if refused(
                Reject.EXPOSES_PARTIAL_ASSIGN
                if not safe and caught_reads(task, live_reads(states)) & exposed
                else None,
                where,
            ):
                continue
            moved = {
                key: value if key == "Comment" else read_through(value, values)
                for key, value in following.items()
            }
            if kind == "Pass":
                assign = moved["Assign"]
                assert isinstance(assign, dict)
                assign_before(task, "Next", then(own, assign))
                task["Next"] = moved["Next"]
            else:
                del task["Next"]
                task["Output"] = moved["Output"]
                task["End"] = True
            comment = joined_comments(task.get("Comment"), following.get("Comment"))
            if comment is not None:
                task["Comment"] = comment
            if not shared:
                del states[after]
            folded = True
            break


def assigned_before_failures(
    own: dict[str, object], following: dict[str, object]
) -> set[str]:
    """The variables Python has assigned when a statement that may fail
    fails: the state's own assignments and the Pass's, in the order they are
    written, before each that holds an expression, and all of them before a
    return that does. A value written in the source does not fail."""
    written_out = [*own.items(), *assigns(following).items()]
    if "Output" in following:
        written_out.append(("", following["Output"]))
    seen: set[str] = set()
    exposed: set[str] = set()
    for name, value in written_out:
        if not written(value):
            exposed |= seen
        seen.add(name)
    return exposed


def same_tries(
    first: tuple[list[Handler], ...], second: tuple[list[Handler], ...]
) -> bool:
    """Whether two states are in the same try bodies: the very same lists of
    clauses, which two try statements with like clauses do not share."""
    return len(first) == len(second) and all(
        a is b for a, b in zip(first, second, strict=True)
    )


def narrow(
    enclosing: Enclosing | None,
    states: dict[str, dict[str, object]],
    into: str,
    source: str,
) -> None:
    """Where a pass moves statements of source into a state, the try bodies
    the state is in: those both are in, as a Catch whose try holds only one
    of them must not take a failure of the other's statements. A Task's, a
    Parallel's or a Map's own are those of its Catch, and stay."""
    if enclosing is None or states[into]["Type"] in {"Task", "Parallel", "Map"}:
        return
    shared: list[list[Handler]] = []
    for mine, theirs in zip(
        enclosing.get(into, ()), enclosing.get(source, ()), strict=False
    ):
        if mine is not theirs:
            break
        shared.append(mine)
    enclosing[into] = tuple(shared)


def read_through(
    template: object,
    values: dict[str, Expr],
    around: Expr | None = None,
) -> object:
    """A template that reads each variable as the value a state before it
    assigns, as the Assign of that state evaluates it: a value read once, or
    a path, in its place, and a longer one read more than once bound to the
    variable's name first, so that it is written and evaluated once, as a
    hand-writer binds it. Where a value put in place or another bound one
    reads such a name, which it means from before the state, each is put in
    place instead. The expression it writes is an Expr, whose properties
    are composed from the field's and the values', as composed says; a part
    of an object or an array reads what the whole around it reads."""
    leaf = template
    template = template_of(leaf)
    around = leaf if isinstance(leaf, Expr) else around
    if isinstance(template, dict):
        return {k: read_through(v, values, around) for k, v in template.items()}
    if isinstance(template, list):
        return [read_through(v, values, around) for v in template]
    if not (isinstance(template, str) and template.startswith("{%")):
        return leaf
    code = template[2:-2].strip()
    reads = {n: len(re.findall(rf"\${n}(?!\w)", code)) for n in values}
    bound = [
        n for n, v in values.items() if reads[n] > 1 and bound_once(n, v, reads[n])
    ]
    placed = {n: v for n, v in values.items() if n not in bound and reads[n]}
    if any(v.variables & set(bound) for v in placed.values()) or any(
        values[n].variables & (set(bound) - {n}) for n in bound
    ):
        placed = {n: v for n, v in values.items() if reads[n]}
        bound = []
    if not bound and len(placed) == 1 and lone_variable(code) == next(iter(placed)):
        return in_place_of(leaf, next(iter(placed.values())))
    if placed:
        # All at once: a value put in place may read the name of another from
        # before the state, which is not to be put in place again.
        names = "|".join(map(re.escape, placed))
        code = re.sub(
            rf"\$({names})(?!\w)", lambda m: operand(placed[m[1]], ATOM), code
        )
    if bound:
        bindings = "".join(f"${n} := {values[n].code}; " for n in bound)
        code = f"({bindings}{code})"
    return composed(
        leaf, code, [v for n, v in values.items() if reads[n]], around=around
    )


def bound_once(name: str, value: Expr, reads: int) -> bool:
    """Whether a value read reads times is bound to its name first rather
    than written at each read: one the binding makes shorter, as a
    hand-writer binds a long value and spells a path out again. A value
    written in the source stays in place, where a later pass reads it as a
    constant. One that changes on evaluation is not read more than once
    here: the passes refuse that before."""
    if written(value.template):
        return False
    placed = reads * len(operand(value, ATOM))
    binding = len(f"(${name} := {value.code}; )") + reads * len(f"${name}")
    return binding < placed


def composed(
    leaf: object,
    code: str,
    values: list[Expr],
    shape: object = None,
    around: Expr | None = None,
) -> Expr:
    """The Expr of code written from a field's expression, leaf, with values
    read in place of variables it reads. The field's expression was judged
    with variables, which are never undefined and fail for nothing, in their
    place, so the code is never undefined where the field's expression is
    not and no value is, and fails for no value where the field's
    expression fails for none and each value is never undefined and fails
    for none.
    A field that holds a template, whose properties are not known, gives
    the code none of them. Where the field is written as an object or an
    array with expressions among its values, shape is that template. The
    code reads, of each variable, the assignments the field's expression
    read, or, for a part of an object or an array, what the whole around it
    read, and those each value read. So too it fails where a value that may
    fail or be undefined fails, and where the field's expression does, unless
    that is never undefined and fails for no value."""
    precedence = ATOM if path_alone(code) else WRITTEN
    read = frozenset(names_read(code))
    failing = [v.fails for v in values if not (v.defined and v.total)]
    if not isinstance(leaf, Expr):
        found = expression(code, read, precedence)
        outer = around.reads if around is not None else frozenset()
        carried = outer.union(*(v.reads for v in values))
        found = replace(
            found,
            reads=frozenset(pair for pair in carried if pair[0] in read),
            fails=frozenset().union(
                around.fails if around is not None else frozenset(), *failing
            ),
        )
    else:
        found = expression(
            code,
            read,
            precedence,
            leaf.type,
            leaf.boolean,
            defined=leaf.defined and all(v.defined for v in values),
            total=leaf.total and all(v.total and v.defined for v in values),
        )
        carried = leaf.reads.union(*(v.reads for v in values))
        found = replace(
            found,
            reads=frozenset(pair for pair in carried if pair[0] in read),
            defines=leaf.defines,
            excepts=leaf.excepts,
            fails=frozenset().union(
                frozenset() if leaf.defined and leaf.total else leaf.fails, *failing
            ),
        )
    return found if shape is None else replace(found, template=shape)


def in_place_of(leaf: object, value: object) -> object:
    """A value written whole where a field read only a variable: the value,
    as the assignment the field is the value of, if any, which fails where
    the value fails."""
    if not isinstance(value, Expr):
        return value
    if not isinstance(leaf, Expr):
        return replace(value, defines=None)
    return replace(value, defines=leaf.defines, excepts=leaf.excepts)


def as_expr(leaf: object) -> Expr:
    """A field as an Expr: the Expr it holds, or its template as an
    expression whose properties are not known."""
    if isinstance(leaf, Expr):
        return leaf
    return assigned_value(leaf) or expression(template_code(leaf))


def live_reads(states: dict[str, dict[str, object]]) -> dict[str, set[str]]:
    """The variables read on a way from each state, before a state on it
    assigns them again: what the state's own expressions read, and what is
    read after each way out that the way does not assign."""
    reads = {
        name: {r for c in expressions_in(state) for r in names_read(c)}
        for name, state in states.items()
    }
    live: dict[str, set[str]] = {name: set() for name in states}
    settled = False
    while not settled:
        settled = True
        for name, state in states.items():
            after = set()
            for holder, target in ways_out(state):
                following = live[target] if target is not None else set()
                after |= following - assigns(holder).keys()
            found = reads[name] | after
            if found != live[name]:
                live[name] = found
                settled = False
    return live


def excepted(state: dict[str, object], live: dict[str, set[str]]) -> set[str]:
    """What the ways on from a state's catchers for a failure of an
    expression read of what the state assigns: inside a try, a statement
    after the state may go in its Assign or its Output, and the except
    clause then sees what the state assigned before it failed, as Python's
    does."""
    catchers = state.get("Catch", [])
    assert isinstance(catchers, list)
    found: set[str] = set()
    for catcher in catchers:
        if set(catcher["ErrorEquals"]) & EVALUATION_ERRORS:
            found |= live[catcher["Next"]] - assigns(catcher).keys()
    return found


def caught_reads(state: dict[str, object], live: dict[str, set[str]]) -> set[str]:
    """The variables read on a way on from the catchers of a state that take
    a failure of its Assign (those for States.ALL or
    States.QueryEvaluationError; measured) before the way assigns them
    again: in the catchers' own Assign and Output, and after them. A
    catcher's own Assign hides nothing read after it, as what it assigns may
    be the value from before the state, which Python's except clause does
    not see. A catcher for other errors never runs after a failing Assign,
    so what it reads is not read after one."""
    catchers = state.get("Catch", [])
    assert isinstance(catchers, list)
    found: set[str] = set()
    for catcher in catchers:
        if set(catcher["ErrorEquals"]) & EVALUATION_ERRORS:
            own = {k: v for k, v in catcher.items() if k != "Next"}
            found |= {r for c in expressions_in(own) for r in names_read(c)}
            found |= live[catcher["Next"]]
    return found


# A JSONata literal as the compiler writes one: a string in single or double
# quotes, a number, true, false or null.
LITERAL = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null"""
# The tests a Choice rule makes of one variable that the compiler writes for
# `x is None`, `x is not None`, and `==`, `!=`, `<`, `<=`, `>` and `>=` of a
# literal.
IS_NONE = re.compile(r"\$not\(\$exists\(\$(\w+)\) and \$\1 != null\)")
IS_NOT_NONE = re.compile(r"\$exists\(\$(\w+)\) and \$\1 != null")
COMPARED = re.compile(rf"\$(\w+) (=|!=|<=|>=|<|>) ({LITERAL})")
# The value of each variable known where a transition is taken.
Known = dict[str, object]


class Rounds:
    """The first rounds of loops already taken, kept while the passes run
    again, so that a loop gives up its first round once however often they
    run: the Choices each transition went past so in thread_choices, and the
    Choices that take_in_choices took in a Choice that leads back to itself."""

    def __init__(self) -> None:
        # By the id of the transition's holder, which is kept here so that no
        # other holder takes its id.
        self.passed: dict[int, tuple[dict[str, object], set[str]]] = {}
        # The first Choice and the second, by name.
        self.taken_in: set[tuple[str, str]] = set()

    def of(self, holder: dict[str, object]) -> set[str]:
        """The Choices the transition of holder went past so."""
        return self.passed.setdefault(id(holder), (holder, set()))[1]


def thread_choices(
    definition: dict[str, object],
    rounds: Rounds | None = None,
    enclosing: Enclosing | None = None,
) -> None:
    """Each transition into a Choice whose tests are decided by values known
    along it, as the transition to where the Choice would send it: a flag that
    each path assigns a value written in the source, such as the stage a saga
    failed at, is tested where a hand-writer would have sent each path on to
    its own continuation. A Choice nothing leads to any more goes. The
    Assign of the rule or the Default the path takes goes in the transition
    that now skips it, where a failure ends the execution as it would in the
    Choice (a catcher's Assign does too; measured), and where its values
    read no part of `$states` that is the Choice's own, which is another
    state's there; the execution's input reads the same. A name that
    transition assigns, which they would read before it is assigned, they
    read as the expression the transition assigns, once for each Choice it
    goes past, as below."""
    states = definition["States"]
    assert isinstance(states, dict)
    # With rounds, a transition goes past a Choice whose Assign reads what it
    # assigns, or that leads back to the Choice it is in, once, as in the
    # first round of a loop whose first test it decides: taking each round
    # in would run the loop as the file compiles, and never end for a loop
    # that does not. Rounds records the Choices each transition went past so.
    # A path sent past a Choice no longer joins the others there, so what is
    # known where it goes may grow: follow the values again until no
    # transition moves.
    moved = True
    while moved:
        moved = decide_start(definition)
        drop_unreachable(definition)
        arriving = known_values(definition)
        for name, state in list(states.items()):
            for holder, key, known in exits(state, arriving[name]):
                target = holder[key]
                assert isinstance(target, str)
                own = holder.get("Assign", {})
                assert isinstance(own, dict)
                taken: dict[str, object] = {}
                sources: list[str] = []
                passed = rounds.of(holder) if rounds is not None else set()
                before = set(passed)
                comment = holder.get("Comment")
                seen = set()
                while (
                    states[target]["Type"] == "Choice"
                    and NAMED not in states[target]
                    and target not in seen
                ):
                    seen.add(target)
                    decided = decide(states[target], known)
                    if decided is None:
                        break
                    following, rule = decided
                    assign = rule.get("Assign", {})
                    assert isinstance(assign, dict)
                    reads = {
                        read
                        for code in expressions_in(assign)
                        for read in names_read(code)
                    }
                    current = then(own, taken)
                    # A Task's, a Parallel's or a Map's own Assign has its
                    # Catch and its retriers take a failure the Choice would
                    # not, which an Assign that cannot fail does not have.
                    where = "thread_choices"
                    if assign and (
                        (
                            holder is state
                            and refused(failure_escapes(state, assign), where)
                        )
                        or refused(
                            context_invariant(expressions_in(assign), Differs.STATES),
                            where,
                        )
                    ):
                        break
                    if assign and reads & current.keys():
                        # Choices that lead to each other would each be gone
                        # past again, round after round, where the way into
                        # them changes on each.
                        if rounds is None or target in passed:
                            break
                        assign = read_into_transition(assign, current)
                        if assign is None:
                            break
                        passed.add(target)
                    if assign:
                        taken.update(assign)
                        sources.append(target)
                        comment = joined_comments(comment, rule.get("Comment"))
                        known = assigned(known, assign)
                    target = following
                # A rule that leads back to the Choice it is in, as a loop's
                # Default does, is taken as the first round, once.
                stays = target == holder[key] and bool(taken)
                if stays and (rounds is None or target in before):
                    continue
                if target != holder[key] or stays:
                    if stays:
                        passed.add(target)
                    holder[key] = target
                    if taken:
                        holder["Assign"] = then(own, taken)
                        if comment is not None:
                            holder["Comment"] = comment
                        for source in sources:
                            narrow(enclosing, states, name, source)
                    moved = True


def read_into_transition(
    assign: dict[str, object], current: dict[str, object]
) -> dict[str, object] | None:
    """An Assign that reads what a transition assigns, as that transition's
    Assign reads it: each such name as the expression it is assigned, as
    way_assign reads them, or None where one cannot be read so."""
    where = "read_into_transition"
    codes = expressions_in(assign)
    values = resolve_reads(current, codes, where)
    # What the values read of $states they read alike: the new Assign goes in
    # the transition's, which evaluates them now. What assign itself reads
    # of $states thread_choices has weighed.
    if (
        values is None
        # The transition still evaluates what assign does not assign again.
        or refused(evaluated_once(values, codes, set(values) - set(assign)), where)
        or refused(captures({"Assign": assign}, values), where)
    ):
        return None
    return {k: written_sum(read_through(v, values)) for k, v in assign.items()}


def links(state: dict[str, object]) -> list[tuple[dict[str, object], str, str]]:
    """Each transition out of a state, as the part that holds it, its key and
    the state it leads to: the state's Next, a Choice's rules and Default,
    and each catcher's Next."""
    rules = state.get("Choices", [])
    catchers = state.get("Catch", [])
    assert isinstance(rules, list) and isinstance(catchers, list)
    found = []
    for holder in [state, *rules, *catchers]:
        assert isinstance(holder, dict)
        for key in ("Next", "Default"):
            target = holder.get(key)
            if isinstance(target, str):
                found.append((holder, key, target))
    return found


def leading(states: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    """The states each state is led to from, once for each transition."""
    found: dict[str, list[str]] = {}
    for name, state in states.items():
        for _, _, target in links(state):
            found.setdefault(target, []).append(name)
    return found


def reached(states: dict[str, dict[str, object]], starts: list[str]) -> set[str]:
    """The states the transitions lead to from the starts, the starts
    included."""
    found = set(starts)
    pending = list(starts)
    while pending:
        for _, _, target in links(states[pending.pop()]):
            if target not in found:
                found.add(target)
                pending.append(target)
    return found


def redirect(states: dict[str, dict[str, object]], renamed: dict[str, str]) -> None:
    """Each transition to a state renamed leads to the state it is renamed
    to."""
    for state in states.values():
        for holder, key, target in links(state):
            if target in renamed:
                holder[key] = renamed[target]


def ways_out(state: dict[str, object]) -> list[tuple[dict[str, object], str | None]]:
    """Each way out of a state, as the part whose Assign runs on it and the
    state it leads to, or None where it ends: a Choice's rules, and its own
    Assign for its Default; any other state's own Assign for its Next or its
    End, and each catcher's for its Next. A Succeed and a Fail end."""
    kind = state["Type"]
    if kind in {"Succeed", "Fail"}:
        return []
    if kind == "Choice":
        rules = state["Choices"]
        assert isinstance(rules, list)
        default = state.get("Default")
        assert default is None or isinstance(default, str)
        return [(r, r["Next"]) for r in rules] + [(state, default)]
    catchers = state.get("Catch", [])
    assert isinstance(catchers, list)
    following = state.get("Next")
    assert following is None or isinstance(following, str)
    return [(state, following)] + [(c, c["Next"]) for c in catchers]


def drop_dead_assignments(definition: dict[str, object]) -> None:
    """Each assignment that no state reads before the variable is assigned
    again or the execution, the branch or the processor ends, as a
    hand-writer assigns only what is read: a missing key its value reads
    fails nowhere then, which the table of differences lists. A Pass left
    with nothing to do goes, each way to it leading on to its Next. Every
    expression of a state, those of its branches or its processor included,
    reads the variables it names; a definition that calls $eval, which reads
    variables by names not written out, keeps every assignment. One whose
    expression another value reads in its place goes too, as a hand-writer
    writes no variable nothing reads, though the value read in place may not
    fail where the assignment would, as a comprehension gives [] for a
    missing key. A Task's, a Parallel's or a Map's own assignment that an
    except clause reads stays, as excepted says, even where nothing after
    the state reads it, and so does what a named assignment itself assigns
    in its Pass."""
    states = definition["States"]
    assert isinstance(states, dict)
    if any(sensitivity(c).dependencies_unknown for c in expressions_in(states)):
        return
    changed = True
    while changed:
        changed = False
        live = live_reads(states)
        for state in states.values():
            for holder, target in ways_out(state):
                own = holder.get("Assign")
                if not isinstance(own, dict):
                    continue
                # A named assignment's Pass keeps what that assignment
                # assigns, which is what the name names.
                kept = state.get(NAMED_ASSIGNS, []) if holder is state else []
                assert isinstance(kept, list)
                following = live[target] if target is not None else set()
                if holder is state:
                    following = following | excepted(state, live)
                # Allowed by AD-DEAD-FAILURE.
                dead = [k for k in own if k not in following and k not in kept]
                for k in dead:
                    del own[k]
                    changed = True
                if not own:
                    del holder["Assign"]
        for name, state in list(states.items()):
            following = state.get("Next")
            if (
                state["Type"] != "Pass"
                or set(state) - {"Type", "Comment", "Next"}
                or not isinstance(following, str)
                or following == name
            ):
                continue
            redirect(states, {name: following})
            if definition["StartAt"] == name:
                definition["StartAt"] = following
            del states[name]
            changed = True
            break


def drop_unreachable(definition: dict[str, object]) -> None:
    """Remove the states no transition leads to any more."""
    states = definition["States"]
    assert isinstance(states, dict)
    start = definition["StartAt"]
    assert isinstance(start, str)
    reachable = reached(states, [start])
    for name in [n for n in states if n not in reachable]:
        del states[name]


def known_values(definition: dict[str, object]) -> dict[str, Known]:
    """The variables known on arriving at each state: those every path to it
    assigns the same value written in the source, found by following the
    transitions until nothing changes."""
    states = definition["States"]
    assert isinstance(states, dict)
    start = definition["StartAt"]
    assert isinstance(start, str)
    arriving: dict[str, Known] = {start: {}}
    pending = [start]
    while pending:
        name = pending.pop()
        for holder, key, known in exits(states[name], arriving[name]):
            target = holder[key]
            assert isinstance(target, str)
            kept = (
                {
                    k: v
                    for k, v in arriving[target].items()
                    if k in known and same(known[k], v)
                }
                if target in arriving
                else known
            )
            if arriving.get(target) != kept:
                arriving[target] = kept
                pending.append(target)
    return arriving


def exits(
    state: dict[str, object], known: Known
) -> list[tuple[dict[str, object], str, Known]]:
    """Each transition out of a state, with the values known when it is taken:
    what the state and the transition assign on top of what was known on
    arriving. A Task's own Assign does not happen when a Catch is taken."""
    rules = state.get("Choices", [])
    catchers = state.get("Catch", [])
    assert isinstance(rules, list) and isinstance(catchers, list)
    result: list[tuple[dict[str, object], str, Known]] = [
        (holder, "Next", assigned(known, holder.get("Assign")))
        for holder in [*rules, *catchers]
    ]
    for key in ("Next", "Default"):
        if key in state:
            result.append((state, key, assigned(known, state.get("Assign"))))
    return result


def assigned(known: Known, assign: object) -> Known:
    """What is known after an Assign: a value written in the source is known,
    and any other value makes its variable unknown."""
    if not isinstance(assign, dict):
        return known
    result = dict(known)
    for name, leaf in assign.items():
        value = template_of(leaf)
        if written(value) and not isinstance(value, (dict, list)):
            result[name] = value
        else:
            result.pop(name, None)
    return result


def decide_start(definition: dict[str, object]) -> bool:
    """Whether the Choice that starts a definition, which nothing leads back
    to, is decided by tests that read no variable, as written_test says,
    such as fold_start leaves one that reads a value written in the source,
    and became a Pass under its name with the Assign of the rule it takes,
    or its own for its Default, leading where that goes, or, with nothing to
    assign, gave its place as the start to that. The start is a way into the
    Choice that assigns nothing: the Pass is entered as the Choice was, with
    the same input and name, and a test written out whole neither fails nor
    is undefined."""
    states = definition["States"]
    start = definition["StartAt"]
    assert isinstance(states, dict) and isinstance(start, str)
    choice = states[start]
    if choice["Type"] != "Choice" or NAMED in choice or start in leading(states):
        return False
    rules = choice["Choices"]
    assert isinstance(rules, list)
    taken, target = choice, choice["Default"]
    for rule in rules:
        value = written_test(rule["Condition"])
        if value is None:
            return False
        if value:
            taken, target = rule, rule["Next"]
            break
    # Where the rule leads to a Choice, the start stays as it is: not for
    # its meaning, which the Pass would keep, but so that take_in_choices
    # can take that Choice's tests into it and the start tests both in one
    # state, where a Pass before the Choice would add one on every
    # execution. Where take_in_choices takes none, the decided Choice stays,
    # as before.
    if states[target]["Type"] == "Choice":
        return False
    if not assigns(taken):
        # Nothing to assign: the definition starts where the Choice leads.
        definition["StartAt"] = target
        return True
    decided: dict[str, object] = {"Type": "Pass"}
    comment = (
        choice.get("Comment")
        if taken is choice
        else joined_comments(choice.get("Comment"), taken.get("Comment"))
    )
    if comment is not None:
        decided["Comment"] = comment
    decided["Assign"] = assigns(taken)
    decided["Next"] = target
    states[start] = decided
    return True


def written_test(condition: object) -> bool | None:
    """The value of a Condition that reads no variable: true or false written
    out, as `if False:` gives, or an expression constant says the value of.
    None for any other."""
    condition = template_of(condition)
    if isinstance(condition, bool):
        return condition
    if not (isinstance(condition, str) and condition.startswith("{%")):
        return None
    found, value = constant(condition[2:-2].strip())
    return value if found and isinstance(value, bool) else None


def decide(
    choice: dict[str, object], known: Known
) -> tuple[str, dict[str, object]] | None:
    """Where a Choice sends a path, and the rule it takes, or the Choice itself
    for its Default, when its tests are decided by what is known."""
    rules = choice["Choices"]
    assert isinstance(rules, list)
    for rule in rules:
        matched = test(rule["Condition"], known)
        if matched is None:
            return None
        if matched:
            return rule["Next"], rule
    default = choice["Default"]
    assert isinstance(default, str)
    return default, choice


def test(condition: object, known: Known) -> bool | None:
    """The result of a test of one known variable, or None for any other."""
    condition = template_of(condition)
    if not (isinstance(condition, str) and condition.startswith("{%")):
        return None
    code = condition[2:-2].strip()
    for pattern, negated in ((IS_NONE, True), (IS_NOT_NONE, False)):
        found = pattern.fullmatch(code)
        if found and found[1] in known:
            present = known[found[1]] is not None
            return not present if negated else present
    found = COMPARED.fullmatch(code)
    if found and found[1] in known:
        value, comparison, other = known[found[1]], found[2], json_literal(found[3])
        if comparison in ("=", "!="):
            equal = same(value, other)
            return equal if comparison == "=" else not equal
        # JSONata orders strings by UTF-16 units where Python orders them by
        # code points, so only numbers are ordered here.
        if number(value) and number(other):
            return ORDERS[comparison](value, other)
    return None


def number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


ORDERS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def json_literal(code: str) -> object:
    """The value of a JSONata literal as the compiler writes one."""
    if code.startswith("'"):
        return json.loads('"' + code[1:-1].replace('"', '\\"') + '"')
    return json.loads(code)


def same(first: object, second: object) -> bool:
    """Whether JSONata's = holds: the same type and value, so true is not 1."""
    return (
        type(first) is type(second)
        and first == second
        or (
            type(first) in {int, float}
            and type(second) in {int, float}
            and first == second
        )
    )


def merge_choices(
    definition: dict[str, object], enclosing: Enclosing | None = None
) -> None:
    """A Choice whose Default leads to a Choice that nothing else leads to, as
    one Choice with the rules of both, the first's before the second's, as a
    hand-writer lists the tests of `if a: ... ` and a following `if b: ...`.
    The first's own Assign runs on its Default, so it goes in each rule of the
    second too. Only where the second reads nothing the first's own Assign
    assigns, which it would read before it is assigned."""
    states = definition["States"]
    assert isinstance(states, dict)
    merged = True
    while merged:
        merged = False
        led = leading(states)
        for name, first in states.items():
            second = first.get("Default")
            if (
                first["Type"] != "Choice"
                or not isinstance(second, str)
                or states[second]["Type"] != "Choice"
                or NAMED in states[second]
                or led[second] != [name]
            ):
                continue
            following = states[second]
            own = first.get("Assign", {})
            reads = {
                read for code in expressions_in(following) for read in names_read(code)
            }
            if own.keys() & reads:
                continue
            for rule in following["Choices"] if own else []:
                rule["Assign"] = then(own, assigns(rule))
                comment = joined_comments(first.get("Comment"), rule.get("Comment"))
                if comment is not None:
                    rule["Comment"] = comment
            first["Choices"] = [*first["Choices"], *following["Choices"]]
            first["Default"] = following["Default"]
            assign = then(own, assigns(following))
            if assign:
                first["Assign"] = assign
            comment = joined_comments(first.get("Comment"), following.get("Comment"))
            if comment is not None:
                first["Comment"] = comment
            narrow(enclosing, states, name, second)
            del states[second]
            merged = True
            break


def take_in_choices(
    definition: dict[str, object], rounds: Rounds, enclosing: Enclosing | None = None
) -> None:
    """A transition of a Choice that leads straight to another Choice takes in
    that one's tests, as a hand-writer lists the tests of `if a:` and the `if
    b:` right inside it, or right after it, in one Choice: a rule `c` becomes
    a rule `c and b` for each rule `b` of the second, in its order, and a
    rule `c` to the second's Default; a Default takes the second's rules after
    the first's own. The second's tests and Assign read what the transition
    assigns as the expressions it assigns them, which the first evaluates
    with the same variables and input as its Assign, and the transition's
    Assign goes on each way out, so a value that fails still fails the
    Choice. JSONata's `and` evaluates no more once one side is false
    (measured), so a test reached only through `c` is not evaluated
    otherwise. Not where such an expression gives another value each time
    it is evaluated, where the second reads the state it is in, binds a
    name it would read, or spells one in a string, as the text of jsonata()
    may, which is not a read and stays as it is. The second stays for the
    other ways into it."""
    states = definition["States"]
    assert isinstance(states, dict)
    taken = True
    while taken:
        taken = False
        for name, first in states.items():
            if first["Type"] != "Choice":
                continue
            rules = first["Choices"]
            assert isinstance(rules, list)
            for index, rule in enumerate([*rules, first]):
                key = "Next" if rule is not first else "Default"
                target = rule[key]
                second = states[target]
                if target == name or second["Type"] != "Choice" or NAMED in second:
                    continue
                # A Choice that leads back to itself through Choices alone
                # would be taken in again each time, as a loop unrolled
                # without end, so it is taken in once into each Choice.
                if (name, target) in rounds.taken_in:
                    continue
                values = rule.get("Assign", {})
                assert isinstance(values, dict)
                read = read_as_values(second, values)
                if read is None:
                    continue
                later_rules, later_default, own = read
                narrow(enclosing, states, name, target)
                if circles(states, target):
                    rounds.taken_in.add((name, target))
                # Each way out describes the transition it takes the place
                # of, whose Assign it takes, as merge_choices does.
                leading = rule.get("Comment") if rule is not first or values else None
                if rule is not first:
                    leading = joined_comments(leading, second.get("Comment"))
                ways = [
                    commented_with(
                        {**later, "Assign": then(values, assigns(later))},
                        leading,
                    )
                    for later in later_rules
                ]
                # Each way out went past what the transition it takes the place
                # of went past, and what the second's own went past.
                before = rounds.of(rule)
                for way, later in zip(ways, second["Choices"], strict=True):
                    rounds.passed[id(way)] = (way, before | rounds.of(later))
                if rule is first:
                    first["Choices"] = [*rules, *ways]
                    first["Default"] = later_default
                    rounds.of(first).update(rounds.of(second))
                    if values or own:
                        first["Assign"] = then(values, own)
                else:
                    test = rule["Condition"]
                    tested = [
                        {**way, "Condition": both(test, way["Condition"])}
                        for way in ways
                    ]
                    for way, old in zip(tested, ways, strict=True):
                        rounds.passed[id(way)] = (way, set(rounds.of(old)))
                    ways = tested
                    rest = {**rule, "Next": later_default}
                    if values or own:
                        rest["Assign"] = then(values, own)
                    rest = commented_with(rest, second.get("Comment"), after=True)
                    rounds.passed[id(rest)] = (rest, before | rounds.of(second))
                    first["Choices"] = [
                        *rules[:index],
                        *ways,
                        rest,
                        *rules[index + 1 :],
                    ]
                for way in [*first["Choices"], first]:
                    if not way.get("Assign"):
                        way.pop("Assign", None)
                if rule is first:
                    comment = joined_comments(
                        first.get("Comment"), second.get("Comment")
                    )
                    if comment is not None:
                        first["Comment"] = comment
                taken = True
                break
            if taken:
                break
        drop_unreachable(definition)


def then(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    """The assignments of first and then of second in one Assign, each where
    it is last written: a name second assigns again goes after the rest of
    first, as Python evaluates the later assignment later and a hand-writer
    writes the keys in the order of the source."""
    return {**{k: v for k, v in first.items() if k not in second}, **second}


def assigns(holder: dict[str, object]) -> dict[str, object]:
    """What a state or a rule assigns."""
    assign = holder.get("Assign", {})
    assert isinstance(assign, dict)
    return assign


def circles(states: dict[str, dict[str, object]], start: str) -> bool:
    """Whether a Choice leads back to itself through Choices alone."""
    seen: set[str] = set()
    pending = [start]
    while pending:
        state = states[pending.pop()]
        rules = state["Choices"]
        assert isinstance(rules, list)
        for holder in [*rules, state]:
            target = holder["Next"] if holder is not state else holder["Default"]
            assert isinstance(target, str)
            if target == start:
                return True
            if target not in seen and states[target]["Type"] == "Choice":
                seen.add(target)
                pending.append(target)
    return False


def commented_with(
    holder: dict[str, object], comment: object, after: bool = False
) -> dict[str, object]:
    """A rule with another comment joined to its own, before it or after."""
    own = holder.get("Comment")
    joined = joined_comments(own, comment) if after else joined_comments(comment, own)
    if joined is None:
        return holder
    return {**holder, "Comment": joined}


def read_as_values(
    choice: dict[str, object], values: dict[str, object]
) -> tuple[list[dict[str, object]], str, dict[str, object]] | None:
    """A Choice's rules, Default and own Assign, reading the variables values
    assigns as the expressions they take, or None where that is not how the
    Choice would read them."""
    codes = expressions_in(choice)
    reads = {read for code in codes for read in names_read(code)}
    used = {n: template_code(v) for n, v in values.items() if n in reads}
    where = "take_in_choices"
    if (
        refused(context_invariant(codes, Differs.STATE), where)
        or refused(evaluated_on_each_way(choice, values, used), where)
        or refused(
            captures(choice, {n: expression(code) for n, code in used.items()}),
            where,
        )
    ):
        return None
    pattern = re.compile(r"\$(" + "|".join(map(re.escape, used)) + r")(?!\w)")

    read = [as_expr(values[n]) for n in used]

    def replaced(
        leaf: object,
        test: bool = False,
        around: Expr | None = None,
    ) -> object:
        item = template_of(leaf)
        around = leaf if isinstance(leaf, Expr) else around
        if isinstance(item, dict):
            return {
                k: v if k == "Comment" else replaced(v, k == "Condition", around)
                for k, v in item.items()
            }
        if isinstance(item, list):
            return [replaced(v, False, around) for v in item]
        if not (used and isinstance(item, str) and item.startswith("{%")):
            return leaf
        code = item[2:-2].strip()
        # An Assign value that is only the variable is the value as written;
        # a Condition stays an expression.
        whole = lone_variable(code)
        if whole in used and not test:
            return in_place_of(leaf, values[whole])
        return composed(leaf, written_in(code), read, around=around)

    def written_in(code: str) -> str:
        counts = Counter(m[1] for m in pattern.finditer(code))
        # A value read more than once is bound once in a block, as a
        # hand-writer binds a long one, where the values bound before it
        # would not hide a name it reads.
        once = [n for n in used if counts[n] > 1]
        bound = {r for n in once for r in names_read(used[n])}
        if not once or bound & set(once):
            return pattern.sub(lambda m: grouped(used[m[1]]), code)
        inline = [n for n in used if counts[n] == 1]
        if inline:
            single = re.compile(r"\$(" + "|".join(map(re.escape, inline)) + r")(?!\w)")
            code = single.sub(lambda m: grouped(used[m[1]]), code)
        bindings = "".join(f"${n} := {used[n]}; " for n in once)
        return "(" + bindings + code + ")"

    rules = replaced(choice["Choices"])
    default = choice["Default"]
    own = replaced(choice.get("Assign", {}))
    assert isinstance(rules, list) and isinstance(default, str)
    assert isinstance(own, dict)
    return rules, default, own


def evaluated_on_each_way(
    choice: dict[str, object], values: dict[str, object], used: dict[str, str]
) -> Reject | None:
    """Whether the values a transition assigns, read in place of their
    variables in a Choice it leads to, are evaluated as often as the
    transition evaluated them, as evaluated_once says, on each way out: a
    rule's tests up to its own, then the rest of the rule, whose Assign goes
    on the way with the transition's, or every test, then the Choice's own
    Assign, on the Default."""
    read = {n: as_expr(values[n]) for n in used}
    rules = choice["Choices"]
    assert isinstance(rules, list)
    tests = [expressions_in(rule["Condition"]) for rule in rules]
    rests = [{k: v for k, v in rule.items() if k != "Condition"} for rule in rules]
    ways = [(tests[: i + 1], rest) for i, rest in enumerate(rests)]
    ways.append((tests, {"Assign": assigns(choice)}))
    for tested, rest in ways:
        codes = [code for test in tested for code in test] + expressions_in(rest)
        reason = evaluated_once(read, codes, set(read) - set(assigns(rest)))
        if reason is not None:
            return reason
    return None


def template_code(template: object) -> str:
    """The JSONata of an Assign value: an expression as it is, and a value
    written out as the JSON it is, with the expressions in it."""
    template = template_of(template)
    if isinstance(template, dict):
        pairs = (f"{json.dumps(k)}: {template_code(v)}" for k, v in template.items())
        return "{" + ", ".join(pairs) + "}"
    if isinstance(template, list):
        return "[" + ", ".join(template_code(v) for v in template) + "]"
    if isinstance(template, str) and template.startswith("{%"):
        return template[2:-2].strip()
    return json.dumps(template)


def grouped(code: str) -> str:
    """Code to read in place of a variable: as it is where it reads as one
    operand, and in parentheses otherwise."""
    if atomic(code):
        return code
    return f"({code})"


def both(first: object, second: object) -> object:
    """The test of two Condition templates, the first before the second, each
    in parentheses where it has an operator that binds looser than `and`. A
    Condition written as true or false, as `if False:` is, decides without
    the second where it is first, as `and` evaluates no more once one side
    is false, and is written into the test where it is second, after the
    first, which may fail."""
    if isinstance(template_of(first), bool):
        return second if template_of(first) else False
    tests = []
    for condition in (template_of(first), template_of(second)):
        if isinstance(condition, bool):
            tests.append(json.dumps(condition))
            continue
        assert isinstance(condition, str)
        code = condition[2:-2].strip()
        tests.append(f"({code})" if looser_than_and(code) else code)
    parts = [c for c in (first, second) if isinstance(c, Expr)]
    found = composed(None, " and ".join(tests), [])
    return replace(
        found,
        reads=frozenset().union(*(c.reads for c in parts)),
        fails=frozenset().union(*(c.fails for c in parts)),
    )


def fold_start(
    definition: dict[str, object], enclosing: Enclosing | None = None
) -> None:
    """The Pass that starts a machine, a branch or a Map processor, in the
    state after it, as a hand-writer assigns what the input gives in the first
    state: that state reads each value as its expression, and its Assign
    assigns it, reading what the Pass would, as nothing between them changes.
    Only where the state after it is a Choice, whose Assign runs on every
    path out of it, a Task whose failure there ends the execution, or a
    Wait, which has no Catch, and only the Pass leads there. A value the
    input lacks then fails after the Task runs or the wait is over, where
    Python fails before calling it or waiting, as a hand-writer spends no
    state to check the input first; the Task or the Wait fails before it
    runs where it reads the value. A Task with a Catch or a retrier that
    takes such a failure, a Parallel or a Map takes them where none can fail
    or be undefined, so that none fails after the state has run, where a
    Catch would take it or the branches or the processor would have run
    for nothing, a catcher's Assign holding them where one takes the
    state's failure. The branches or the processor run before that Assign,
    so they read only values written in the source, which read the same in a
    child execution of a distributed map, whose context is its own. A
    Succeed takes them in its Output, and a Fail in its Error and Cause,
    where none can fail or be undefined, as nothing reads what they do not;
    one follows the Pass where a Choice the Pass decides was skipped, or
    where the first round of a loop that thread_choices took in the way in
    leaves it. It runs after fold_into_catching_tasks, as the
    catchers assign these values too, where that would count them among what
    Python assigned before a statement that may fail. The Pass is the start
    state as it is when the pass runs, whatever the passes before put in it,
    where nothing leads back to it, as a loop would."""
    states = definition["States"]
    start = definition["StartAt"]
    assert isinstance(states, dict) and isinstance(start, str)
    opening = states[start]
    if (
        opening["Type"] != "Pass"
        or set(opening) - {"Type", "Comment", "Assign", "Next"}
        or start in leading(states)
    ):
        return
    assign = assigns(opening)
    if not assign or not all(
        isinstance(v, Expr) or written(v) for v in assign.values()
    ):
        return
    values = {
        name: v if isinstance(v, Expr) else written_value(v)
        for name, v in assign.items()
    }
    # A value that reads what is not written out, or the State of the
    # context, which names the state it is read in, keeps the Pass.
    where = "fold_start"
    if any(v.sensitivity.dependencies_unknown for v in values.values()) or refused(
        context_invariant([v.code for v in values.values()], Differs.CONTEXT), where
    ):
        return
    following = opening["Next"]
    state = states[following]
    kind = state["Type"]
    # A Succeed or a Fail has no Assign: what it does not read ends with it,
    # which is the Pass's Python meaning only where no value can fail. A
    # Task with a Catch or a retrier for such a failure, a Parallel or a Map
    # evaluates its Assign after it has run, where only a value that cannot
    # fail fails nowhere else than Python's, neither a Catch nor a retrier
    # has a failure to take, and no branch or processor runs for nothing.
    # A value the input lacks fails after the call or the wait: allowed by
    # AD-DEFERRED-FAILURE.
    certain = failsafe(list(values.values()))
    # A value that changes on evaluation goes only where it is read after: it
    # would otherwise be evaluated in a holder's Assign as well as where it
    # is read, and the time would be read after the call or the wait.
    live = live_reads(states)
    kept = {
        name: [
            i
            for i, holder in enumerate(holders_of(state))
            if not v.volatile or name in read_after(state, holder, live)
        ]
        for name, v in values.items()
    }
    if not (
        kind in {"Choice", "Wait"}
        or (
            kind == "Task"
            and not refused(failure_escapes(state, list(values.values())), where)
        )
        or (kind in {"Succeed", "Fail", "Parallel", "Map"} and certain)
    ):
        return
    inner = [state.get("Branches", []), state.get("ItemProcessor", {})]
    if any(
        mentions(code, name)
        for name, value in values.items()
        if not written(value.template)
        for code in expressions_in(inner)
    ):
        return
    others = [name for name in leading(states).get(following, []) if name != start]
    if others or refused(captures(state, values), where):
        return
    # The Pass is right before the state, so the state's fields before its
    # call or wait have none between them and the Pass.
    reads = [Read(f.code, f.before_effect, f.repeated) for f in fields_of(state)]
    for name, value in values.items():
        # Each holder that takes it evaluates it on its way out, the Choice's
        # where the Choice is, and the others' after the call or the wait.
        taking = [Read(f"${name}", kind == "Choice") for _ in kept[name]]
        if refused(evaluated_as_before(value, name, reads + taking), where):
            return
    for key, field_value in list(state.items()):
        if key != "Comment":
            state[key] = read_through(field_value, values)
    narrow(enclosing, states, following, start)
    # The substitution wrote the rules and the catchers anew.
    for i, holder in enumerate(holders_of(state)):
        own = holder.get("Assign", {})
        assert isinstance(own, dict)
        taken = {k: v for k, v in assign.items() if i in kept[k]}
        if not (taken or own):
            continue
        holder["Assign"] = then(taken, own)
        # Each holder describes the assignments, as a Pass would.
        comment = joined_comments(opening.get("Comment"), holder.get("Comment"))
        if comment is not None:
            holder["Comment"] = comment
    del states[start]
    definition["StartAt"] = following


def read_after(
    state: dict[str, object], holder: dict[str, object], live: dict[str, set[str]]
) -> set[str]:
    """What is read on the way out of a state that holder's Assign runs on,
    before it is assigned again: a Choice's own Assign runs on its Default,
    and what the state's catchers read of what its own Assign assigns counts
    for it, as excepted says."""
    key = "Default" if holder is state and state["Type"] == "Choice" else "Next"
    target = holder.get(key)
    found = set(live[target]) if isinstance(target, str) else set()
    if holder is state:
        found |= excepted(state, live)
    return found


def written_value(template: object) -> Expr:
    """A value written out in the source as the expression the translation
    writes for it, to read in place of the variable it is assigned."""
    if isinstance(template, Expr):
        return template
    if isinstance(template, list):
        return array([written_value(item) for item in template])
    if isinstance(template, dict):
        return obj([(k, written_value(v)) for k, v in template.items()])
    return literal(template)


def holders_of(state: dict[str, object]) -> list[dict[str, object]]:
    """The parts of a state whose Assign runs on a way out of it: a Choice's
    rules and its own, which its Default applies, and a Task's, a Wait's, a
    Parallel's or a Map's own and its catchers'. A Succeed and a Fail have
    none."""
    if state["Type"] in {"Succeed", "Fail"}:
        return []
    others = state.get("Choices" if state["Type"] == "Choice" else "Catch", [])
    assert isinstance(others, list)
    return [state, *others]


def share_states(
    definition: dict[str, object], enclosing: Enclosing | None = None
) -> None:
    """States that do the same and go the same way are one state, as a
    hand-writer ends every path that returns the same at one Succeed and runs
    a clean-up the paths share once. A state behaves as its fields, its input,
    the variables and the context say, so one reached from either path does
    what each did; only the context's State names the state it is read in,
    and a state that reads it keeps its own. Sharing what two states lead to
    can make them the same, so this repeats until nothing more is shared.
    States are compared without the location line --source-locations adds,
    whose spans the one kept takes, so the option changes no state."""
    states = definition["States"]
    assert isinstance(states, dict)
    while shared := same_states(states):
        for name, kept in shared.items():
            read_both(states[kept], states[name])
            narrow(enclosing, states, kept, name)
            del states[name]
        start = definition["StartAt"]
        assert isinstance(start, str)
        definition["StartAt"] = shared.get(start, start)
        redirect(states, shared)


def read_both(kept: object, other: object) -> None:
    """Where a state takes the place of another of the same fields, each of
    its expressions reads, on the ways the other led, what the other's read:
    the reads of both, written into the state in place."""
    if isinstance(kept, dict) and isinstance(other, dict):
        for key, mine in kept.items():
            kept[key] = both_reads(mine, other.get(key))
    elif isinstance(kept, list) and isinstance(other, list):
        for index, theirs in enumerate(other[: len(kept)]):
            kept[index] = both_reads(kept[index], theirs)


def both_reads(mine: object, theirs: object) -> object:
    if isinstance(mine, Expr) and isinstance(theirs, Expr):
        return replace(mine, reads=mine.reads | theirs.reads)
    read_both(mine, theirs)
    return mine


def spread_passes(
    definition: dict[str, object], enclosing: Enclosing | None = None
) -> None:
    """A Pass every way into which can hold its assignments, in the Assign of
    each of them, as a hand-writer copies an assignment into each branch: the
    first statement of a loop's body, which the way in and the way back both
    lead to, is assigned on each instead of in a state of its own, which each
    round of the loop would pass through. A way can hold them where its
    Assign runs only on that way and a failure there ends the execution, as
    the Pass's would: a Pass, a Wait, a Choice rule, a Choice's own Assign for
    its Default, a catcher, and a Task, a Parallel or a Map whose failure
    there no Catch or retrier takes, or where none of them can fail, as
    failsafe says. The values read what the way assigns as the expressions it
    assigns them. Where the Pass assigns a name the way assigns too, the
    way's value goes, as nothing reads it after the Pass, unless the Pass
    reads it and it may be undefined, which could pass through a test such
    as $type() where the way's Assign would fail. The time and a random value go
    too: each copy is on its own way, so a run evaluates one of them once,
    as it would the Pass, and a way's Assign runs where the Pass would, a
    Task's, a Parallel's, a Map's and a Wait's when it ends (measured); the
    syntax tree says which values read them, a jsonata() expression's
    included. Values that read $states, other than the context the two
    share, or $eval, which reads variables by the names in its text, or that
    spell a name the way assigns in a string, keep the Pass, and so do those
    that read what the way assigns where that reads a value that changes on
    evaluation."""
    states = definition["States"]
    assert isinstance(states, dict)
    spread = True
    while spread:
        spread = False
        for name, state in states.items():
            if (
                state["Type"] != "Pass"
                or set(state) - {"Type", "Comment", "Assign", "Next"}
                or "Assign" not in state
                or name == definition["StartAt"]
            ):
                continue
            assign = state["Assign"]
            assert isinstance(assign, dict)
            codes = expressions_in(assign)
            if any(sensitivity(c).dependencies_unknown for c in codes) or refused(
                context_invariant(codes, Differs.STATES), "spread_passes"
            ):
                continue
            ways = [
                (holder, key, owner, owner_name)
                for owner_name, owner in states.items()
                for holder, key, target in links(owner)
                if target == name
            ]
            merged = [
                way_assign(holder, owner, assign, codes) for holder, _, owner, _ in ways
            ]
            if not ways or any(m is None for m in merged):
                continue
            for (holder, key, _, owner_name), values in zip(ways, merged, strict=True):
                assert values is not None
                narrow(enclosing, states, owner_name, name)
                assign_before(holder, key, values)
                holder[key] = state["Next"]
                comment = joined_comments(holder.get("Comment"), state.get("Comment"))
                if comment is not None:
                    holder["Comment"] = comment
            del states[name]
            spread = True
            break


def assign_before(
    holder: dict[str, object], key: str, values: dict[str, object]
) -> None:
    """The Assign of a state or a rule set to values: where it has none, just
    before the transition of key, as the fields are written where they come
    from the source."""
    if "Assign" in holder:
        holder["Assign"] = values
        return
    fields = list(holder.items())
    holder.clear()
    for field_key, value in fields:
        if field_key == key:
            holder["Assign"] = values
        holder[field_key] = value


def way_assign(
    holder: dict[str, object],
    owner: dict[str, object],
    assign: dict[str, object],
    codes: list[str],
) -> dict[str, object] | None:
    """The Assign of a way into a Pass with the Pass's assignments in it, as
    spread_passes says, or None where the way cannot hold them."""
    # Only a Pass, a Wait, a Choice, a Task, a Parallel or a Map has a Next;
    # of them, only the last three may have a Catch or a retrier.
    where = "spread_passes"
    if (
        holder is owner
        and owner["Type"] in {"Task", "Parallel", "Map"}
        and refused(failure_escapes(owner, assign), where)
    ):
        return None
    own = assigns(holder)
    read = resolve_reads(own, codes, where)
    if read is None:
        return None
    # A name both assign drops the way's value, which the Pass's new value
    # evaluates in its place where it reads it: one that may be undefined
    # could pass there through a test such as $type() where the way's
    # Assign would fail.
    if any(n in assign and not v.defined for n, v in read.items()):
        return None
    # The way still evaluates what the Pass does not assign again. What its
    # values read of $states they read alike, as the Pass's assignments go
    # in the way's own Assign, which evaluates them now; what the Pass reads
    # of $states spread_passes has weighed.
    if refused(evaluated_once(read, codes, set(read) - set(assign)), where):
        return None
    # A string that spells a name, or a binding of one, keeps the Pass.
    if refused(captures({"Assign": assign}, read), where):
        return None
    moved = {k: written_sum(read_through(v, read)) for k, v in assign.items()}
    return then(own, moved)


# An expression that is only an operation on two integers written out, as
# reading a value written in the source into `$n + 1` gives.
SUM = re.compile(r"\{% (-?\d+) ([-+*]) (-?\d+) %\}")


def written_sum(template: object) -> object:
    """A template that is only a sum, difference or product of integers
    written out, as the value, as fold writes one in the source: 1 for
    0 + 1. Anything else stays as it is."""
    text = template_of(template)
    found = SUM.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        return template
    left, right = literal(int(found[1])), literal(int(found[3]))
    return in_place_of(template, binary(left, found[2], right, ADD, of(NUMBER)))


def read_what_it_assigns(
    state: dict[str, object], output: object, codes: list[str]
) -> object | None:
    """The Output of a return for a Task, a Parallel or a Map to end with,
    or None where it cannot: only where a failure of its Output ends the
    execution, as the Succeed's would, and where the return reads nothing
    of $states, which is the Succeed's own there, nor $eval, which a
    jsonata() expression may read in ways not known here. The Output is
    evaluated once, as the Succeed's was, after the same calls and waits.
    The Output reads what the state assigns as read_assigned says. A
    return in which nothing can fail or be undefined goes in after a Catch
    or a retrier too: a variable the state assigns reads the expression its
    Assign evaluates alike, so the Output fails only where the Assign does,
    which the Catch or the retrier takes as it would without the Output."""
    where = "end_before_returns"
    if (
        refused(failure_escapes(state, output), where)
        or refused(dependencies_known(codes), where)
        or refused(context_invariant(codes, Differs.STATES), where)
    ):
        return None
    return read_assigned(state, output, codes)


def read_assigned(
    state: dict[str, object], output: object, codes: list[str]
) -> object | None:
    """An Output with what the state assigns read as the expressions it
    assigns, as the Output is evaluated with the values from before the
    state, or None where one cannot be read so: a value that is no one
    expression, one that reads the time, a random value or $eval, which the
    Output would evaluate again, or a name the Output binds itself. The
    state's Assign stays, as Python evaluates what the return does not
    read, and drop_dead_assignments keeps an assignment whose expression
    the Output reads in its place, so a value that fails or is undefined
    fails in the Assign, where Python fails, even where the Output would
    drop it."""
    where = "read_assigned"
    values = resolve_reads(assigns(state), codes, where)
    # What the state's own Assign reads of $states the Output reads alike,
    # and the Assign stays.
    if (
        values is None
        or refused(evaluated_once(values, codes, set(values)), where)
        or refused(captures({"Output": output}, values), where)
    ):
        return None
    return read_through(output, values)


def fold_constants(definition: dict[str, object]) -> None:
    """Each expression that reads no variable and gives the same value
    wherever it is evaluated, as constant says, as the value written out, as
    a hand-writer writes 0 for (2 - 2) * -1: where values written in the
    source are read in place of variables, what is left is such an
    expression. A Condition is left to thread_choices, which decides a test
    from the values known along each way, a loop's first round only; the
    Error and Cause of a Fail take only a string."""
    states = definition["States"]
    assert isinstance(states, dict)

    def folded(
        leaf: object, kinds: tuple[type, ...] | None, field: bool = False
    ) -> object:
        if isinstance(leaf, Expr) and isinstance(leaf.template, (dict, list)):
            return replace(leaf, template=folded(leaf.template, None))
        if isinstance(leaf, dict):
            return {k: folded(v, None) for k, v in leaf.items()}
        if isinstance(leaf, list):
            return [folded(v, None) for v in leaf]
        code = code_of(leaf)
        if code is None:
            return leaf
        found, value = constant(code)
        if not found or (kinds is not None and not isinstance(value, kinds)):
            return leaf
        # Written out, a string that opens or closes like a template would be
        # read as one.
        if any(
            isinstance(v, str) and (v.startswith("{%") or v.endswith("%}"))
            for v in leaves(value)
        ):
            return leaf
        # A field keeps what the check knows of the assignment it makes.
        if field and isinstance(leaf, Expr):
            return replace(
                written_value(value),
                defines=leaf.defines,
                excepts=leaf.excepts,
            )
        return value

    for state in states.values():
        holders: list[dict[str, object]] = [state, *state.get("Choices", [])]
        holders += state.get("Catch", [])
        for holder in holders:
            for key in list(holder):
                # A rule and a catcher are holders of their own, which the
                # passes know by what they are; a Condition is not folded.
                if key in UNREAD or key in {
                    "Type",
                    "Next",
                    "Default",
                    "End",
                    "Choices",
                    "Catch",
                    "Retry",
                    "Condition",
                }:
                    continue
                kinds: tuple[type, ...] | None = None
                if key in {"Error", "Cause"}:
                    kinds = (str,)
                value = holder[key]
                if key == "Assign" and isinstance(value, dict):
                    holder[key] = {
                        k: folded(v, None, field=True) for k, v in value.items()
                    }
                else:
                    holder[key] = folded(value, kinds, field=True)


def leaves(value: object) -> list[object]:
    """The scalars of a value written out."""
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in leaves(v)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in leaves(v)]
    return [value]


def return_in_place_of_passes(definition: dict[str, object]) -> None:
    """A Pass that goes on to a Succeed, as a Succeed whose Output reads what
    the Pass assigns as the expressions it assigns them, as a hand-writer
    returns what they compute: nothing reads them after the return. The
    Succeed stays for the other ways to it, and goes when none is left.
    Python evaluates each assignment even where the return does not read
    it, so each that may fail or be undefined is one the Output reads every
    time it is evaluated, as always_read says, and is never undefined, which
    a list or a dict would drop without failing; the Output that is that
    variable alone fails where the Pass would. Not where a value changes on
    evaluation, reads the context of the state it is in, or cannot be read
    in place, as reads_as says, nor where the Output reads the State of the
    context, which names the state it is read in. After a Task, a Parallel
    or a Map whose Catch takes a failure of an expression, the Pass is left
    to fold_into_catching_tasks, which weighs what the except clause reads
    of what the Pass assigns. The Succeed keeps its name where only the Pass
    leads to it."""
    states = definition["States"]
    assert isinstance(states, dict)
    changed = True
    while changed:
        changed = False
        led = leading(states)
        for name, state in states.items():
            after = state.get("Next")
            if (
                state["Type"] != "Pass"
                or set(state) - {"Type", "Comment", "Assign", "Next"}
                or not isinstance(after, str)
                or after == name
                or states[after]["Type"] != "Succeed"
                or any(
                    states[o]["Type"] in {"Task", "Parallel", "Map"}
                    and takes_evaluation(states[o], "Catch")
                    for o in led.get(name, [])
                )
            ):
                continue
            ending = states[after]
            # A named Succeed is not copied into the Pass's place for one way
            # in, though the Pass may go into it where it is the only way.
            if NAMED in ending and led[after] != [name]:
                continue
            output = ending["Output"]
            code = as_expr(output).code
            where = "return_in_place_of_passes"
            if refused(context_invariant([code], Differs.STATE), where):
                continue
            values = resolve_reads(assigns(state), None, where)
            # The Output is evaluated where the Pass would be, with no call
            # or wait between them.
            reads = [Read(c, same_interval=True) for c in expressions_in(output)]
            if (
                values is None
                or any(
                    refused(evaluated_as_before(v, n, reads), where)
                    for n, v in values.items()
                )
                or refused(
                    context_invariant(
                        [v.code for v in values.values()], Differs.CONTEXT
                    ),
                    where,
                )
            ):
                continue
            if refused(failure_kept(code, values), where):
                continue
            used = {n: v for n, v in values.items() if n in names_read(code)}
            if refused(captures(ending, used), where):
                continue
            succeed: dict[str, object] = {"Type": "Succeed"}
            comment = joined_comments(state.get("Comment"), ending.get("Comment"))
            if comment is not None:
                succeed["Comment"] = comment
            succeed["Output"] = read_through(output, used)
            if NAMED in ending:
                succeed[NAMED] = ending[NAMED]
            if led[after] == [name]:
                states[after] = succeed
                redirect(states, {name: after})
                if definition["StartAt"] == name:
                    definition["StartAt"] = after
                del states[name]
            else:
                states[name] = succeed
            changed = True
            break


def end_before_returns(definition: dict[str, object]) -> None:
    """A Task, a Parallel or a Map that goes on to a Succeed ends the machine
    or the branch itself, with the Succeed's Output as its own, as it does
    when the return follows it alone. The Succeed stays for the other ways
    to it, such as a catcher or a Choice's Default after a try or an if, and
    goes when none is left. An Output written in the source has no
    expression, so it cannot fail where the Succeed would not; one with
    expressions goes as read_what_it_assigns says. A Wait takes any Output
    so, as it evaluates it when the wait is over (measured), where the
    Succeed would, and has no Catch, unless the Output reads the State of
    the context, which names the state it is read in. It reads what the
    Wait assigns as the expressions the Wait assigns, as it would read it
    from before the Wait, and keeps the Succeed where one cannot be read so,
    as read_assigned says."""
    states = definition["States"]
    assert isinstance(states, dict)
    for state in states.values():
        after = states.get(state.get("Next"))
        if (
            state["Type"] not in {"Task", "Parallel", "Map", "Wait"}
            or "Output" in state
            or after is None
            or after["Type"] != "Succeed"
            or NAMED in after
        ):
            continue
        codes = expressions_in({"Output": after["Output"]})
        output: object = after["Output"]
        if state["Type"] != "Wait":
            if codes:
                output = read_what_it_assigns(state, output, codes)
                if output is None:
                    continue
        elif refused(context_invariant(codes, Differs.STATE), "end_before_returns"):
            continue
        else:
            output = read_assigned(state, output, codes)
            if output is None:
                continue
        del state["Next"]
        state["Output"] = output
        state["End"] = True
        comment = joined_comments(state.get("Comment"), after.get("Comment"))
        if comment is not None:
            state["Comment"] = comment
    targets = leading(states)
    for name in [n for n, s in states.items() if s["Type"] == "Succeed"]:
        if name not in targets and name != definition["StartAt"]:
            del states[name]


def same_states(states: dict[str, dict[str, object]]) -> dict[str, str]:
    """Each state that is the same as one before it, and that one, whose
    Comment takes the spans of both."""
    kept: dict[str, str] = {}
    shared: dict[str, str] = {}
    for name, state in states.items():
        own, located = split_comment(state.get("Comment"))
        key = json.dumps(
            {**{k: v for k, v in state.items() if k != "Comment"}, "Comment": own},
            sort_keys=True,
            default=template_of,
        )
        if refused(
            context_invariant(expressions_in(state), Differs.NAME), "share_states"
        ):
            continue
        if key not in kept:
            kept[key] = name
            continue
        shared[name] = kept[key]
        if located is not None:
            first = states[kept[key]]
            _, line = split_comment(first.get("Comment"))
            assert line is not None
            joined = json.loads(line.removeprefix(PREFIX))
            spans = json.loads(located.removeprefix(PREFIX))["spans"]
            joined["spans"] += [s for s in spans if s not in joined["spans"]]
            text = [own] if own else []
            located_line = PREFIX + json.dumps(joined, ensure_ascii=False)
            first["Comment"] = "\n".join([*text, located_line])
    return shared


def joined_comments(first: object, second: object) -> str | None:
    """The Comment of a state that takes the place of two: the text of each,
    and one location line holding the spans of both."""
    texts: list[str] = []
    located: dict[str, object] | None = None
    for comment in (first, second):
        own, line = split_comment(comment)
        if own:
            texts.append(own)
        if line is None:
            continue
        found = json.loads(line.removeprefix(PREFIX))
        if located is None:
            located = found
            continue
        spans = located["spans"]
        assert isinstance(spans, list)
        spans += [span for span in found["spans"] if span not in spans]
    if located is not None:
        texts.append(PREFIX + json.dumps(located, ensure_ascii=False))
    return "\n".join(texts) or None


def split_comment(comment: object) -> tuple[str | None, str | None]:
    """A Comment's own text, and the location line that ends it, if any."""
    if not isinstance(comment, str):
        return None, None
    lines = comment.split("\n")
    if lines[-1].startswith(PREFIX):
        return "\n".join(lines[:-1]) or None, lines[-1]
    return comment, None


# A parameter of an inline map's function, read where it may be read from
# $states.input, until the processor shows whether each read is. Each map
# marks its own, as a map inside it reads its parameters too.
MARKS = itertools.count()


def marking() -> str:
    return f"__sfnx_item{next(MARKS)}_"


# The fields of a first state that read $states.input as its input: measured
# for a Task and its catchers, and documented for a Choice's rules and a
# Map's Items. A branch's first state reads the Parallel's input.
AT_START = frozenset({"Arguments", "Assign", "Output", "Catch", "Choices", "Items"})


def reads_at_start(definition: dict[str, object], mark: str) -> bool:
    """Whether every marked read is in a field of the first state that reads
    the processor's input, or in the first state of a branch of a Parallel
    that is first, whose input is the same."""
    states = definition["States"]
    assert isinstance(states, dict)
    for name, state in states.items():
        assert isinstance(state, dict)
        if name != definition["StartAt"]:
            elsewhere = [state]
        else:
            fields = AT_START | {"Branches"}
            elsewhere = [v for k, v in state.items() if k not in fields]
            if not all(reads_at_start(b, mark) for b in state.get("Branches", [])):
                return False
        if any(marked(part, mark) for part in elsewhere):
            return False
    return True


def marked(value: object, mark: str) -> bool:
    return any(f"${mark}" in text for text in strings(value))


def placed(value: object, mark: str) -> dict[str, object]:
    """The processor with each marked read of a parameter as $states.input
    reads it."""
    pattern = re.compile(rf"\${mark}(\w+)")

    def read(match: re.Match[str]) -> str:
        return step(expression("$states.input"), match.group(1)).code

    def rewrite(leaf: object) -> object:
        item = template_of(leaf)
        rewritten: object
        if isinstance(item, str):
            rewritten = pattern.sub(read, item)
        elif isinstance(item, dict):
            rewritten = {k: rewrite(v) for k, v in item.items()}
        elif isinstance(item, list):
            rewritten = [rewrite(v) for v in item]
        else:
            return leaf
        if not isinstance(leaf, Expr):
            return rewritten
        plain = emitted(rewritten)
        if plain == item:
            return leaf
        # A marked read is the processor's input, which may lack the key.
        code = template_code(plain)
        return composed(leaf, code, [expression("$states.input")], plain)

    result = rewrite(value)
    assert isinstance(result, dict)
    return result


def strings(value: object) -> Iterator[str]:
    value = template_of(value)
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def ended(function: ast.FunctionDef) -> Origin:
    """The return a function makes where its body ends."""
    return Origin(function, "end of function", header=True)


def commented(state: dict[str, object], comment: str) -> dict[str, object]:
    """The state, or Choice rule, with its Comment first after any Type, where
    a person puts it. The dict itself changes, as the transitions still to be
    linked hold on to it."""
    fields = dict(state)
    fields.pop("Comment", None)
    kind = {"Type": fields.pop("Type")} if "Type" in fields else {}
    state.clear()
    state.update({**kind, "Comment": comment, **fields})
    return state


def optimize(definition: dict[str, object], scope: "Scope") -> None:
    """The states of a machine, a branch or a Map processor, rewritten by
    each pass in turn until a round of them changes nothing: one pass can
    open the way for one that ran before it, as a Choice taken in another
    leaves a start Pass that fold_start can now fold. Each pass takes what it
    can in one go, and the first round of a loop is taken once, so the
    rounds end. Where the compile checks the passes, the assignments each
    expression reads are noted first, so that misread can tell where a pass
    made one read another."""
    assert not loose_expressions(definition)
    # Each state knows the try bodies it is in, which narrow and
    # fold_into_catching_tasks read.
    states = definition["States"]
    assert isinstance(states, dict) and states.keys() <= scope.enclosing.keys()
    if scope.checking:
        note_reads(definition)
    rounds = Rounds()
    enclosing = scope.enclosing
    while True:
        before = copy.deepcopy(definition)
        traced(fold_constants, definition)
        traced(thread_choices, definition, None, enclosing)
        traced(drop_dead_assignments, definition)
        traced(fold_into_catching_tasks, definition, enclosing)
        traced(fold_start, definition, enclosing)
        traced(spread_passes, definition, enclosing)
        traced(thread_choices, definition, rounds, enclosing)
        traced(merge_choices, definition, enclosing)
        traced(take_in_choices, definition, rounds, enclosing)
        traced(share_states, definition, enclosing)
        traced(return_in_place_of_passes, definition)
        traced(end_before_returns, definition)
        # A round that writes the same definition changes nothing, though it
        # may hold an Expr where a template was.
        if emitted(definition) == emitted(before):
            return


# The assignment a variable holds where a scope starts, which no Assign of
# the scope made, and one whose Assign value a pass wrote without its number.
INITIAL = 0
UNKNOWN = -1
# The fields that hold a scope of their own, and a field that is not read.
UNREAD = frozenset({"Comment", "Branches", "ItemProcessor"})
Reaching = dict[str, frozenset[int]]


def note_reads(definition: dict[str, object]) -> None:
    """Number each Assign value of a definition as the assignment it makes,
    and note on each expression, of each variable it reads, the assignments
    that reach it where it is evaluated: the Python meaning of the read, as
    the states are built, one statement or two to a state. A value written
    out becomes an Expr, which is written out as it was."""
    states = definition["States"]
    assert isinstance(states, dict)
    # The catchers whose way reaches each state, and each catcher's own.
    ways: dict[int, set[tuple[str, int]]] = {}
    for name, state in states.items():
        catchers = state.get("Catch", [])
        assert isinstance(catchers, list)
        for index, catcher in enumerate(catchers):
            ways.setdefault(id(catcher), set()).add((name, index))
            for reached_name in reached(states, [catcher["Next"]]):
                for holder, _ in ways_out(states[reached_name]):
                    ways.setdefault(id(holder), set()).add((name, index))
    numbers = itertools.count(INITIAL + 1)
    for state in states.values():
        for holder, _ in ways_out(state):
            assign = assigns(holder)
            excepts = frozenset(ways.get(id(holder), set()))
            for key, value in assign.items():
                found = value if isinstance(value, Expr) else assigned_value(value)
                if found is not None:
                    number = next(numbers)
                    assign[key] = replace(found, defines=number, excepts=excepts)
    entry = reaching(definition)
    for name, state in states.items():
        at = entry.get(name)
        if at is None:
            continue

        def read(value: Expr, at: Reaching = at) -> Expr:
            pairs = frozenset(
                (n, d) for n in names_read(value.code) for d in reaching_of(at, n)
            )
            seen = frozenset((n, d) for n, numbers in at.items() for d in numbers)
            return replace(value, reads=pairs, fails=frozenset({seen}))

        annotated = each_expression(state, read)
        assert isinstance(annotated, dict)
        state.clear()
        state.update(annotated)


def reaching(definition: dict[str, object]) -> dict[str, Reaching]:
    """For each state reached, the assignments that may give each variable
    its value where the state is entered, by their numbers: INITIAL where no
    Assign of the scope did, UNKNOWN for a value without its number. An
    Assign is evaluated with the values from before the state, and on a
    catcher's way the state's own Assign assigns nothing (measured)."""
    states = definition["States"]
    start = definition["StartAt"]
    assert isinstance(states, dict) and isinstance(start, str)
    entry: dict[str, Reaching] = {start: {}}
    work = [start]
    while work:
        name = work.pop()
        for holder, target in ways_out(states[name]):
            if target is None:
                continue
            out = dict(entry[name])
            for key, value in assigns(holder).items():
                number = value.defines if isinstance(value, Expr) else None
                out[key] = frozenset({UNKNOWN if number is None else number})
            old = entry.get(target)
            merged = (
                out
                if old is None
                else {
                    n: reaching_of(old, n) | reaching_of(out, n)
                    for n in old.keys() | out.keys()
                }
            )
            if merged != old:
                entry[target] = merged
                work.append(target)
    return entry


def reaching_of(at: Reaching, name: str) -> frozenset[int]:
    return at.get(name, frozenset({INITIAL}))


def each_expression(node: object, change: Callable[[Expr], Expr]) -> object:
    """node with change made to each Expr in it, those inside an object or
    an array one holds included, past the Comments and the scopes of its own
    that a Parallel's branches and a Map's processor are."""
    if isinstance(node, Expr):
        if isinstance(node.template, (dict, list)):
            node = replace(node, template=each_expression(node.template, change))
        return change(node)
    if isinstance(node, dict):
        return {
            k: v if k in UNREAD else each_expression(v, change) for k, v in node.items()
        }
    if isinstance(node, list):
        return [each_expression(v, change) for v in node]
    return node


def misread(definition: dict[str, object]) -> list[str]:
    """Each read of a variable that an assignment reaches that note_reads did
    not note for it: where a pass moved an expression, or an assignment,
    so that the variable holds another value there than Python's, and each
    read on a catcher's way that miscaught finds. A read or an assignment
    the passes wrote without its note is not judged."""
    found: list[str] = []
    states = definition["States"]
    assert isinstance(states, dict)
    entry = reaching(definition)
    for name, state in states.items():
        at = entry.get(name)
        if at is None:
            continue

        def check(value: Expr, at: Reaching = at, name: str = name) -> Expr:
            noted: dict[str, set[int]] = {}
            for read_name, number in value.reads:
                noted.setdefault(read_name, set()).add(number)
            for read_name, numbers in noted.items():
                wrong = reaching_of(at, read_name) - numbers - {UNKNOWN}
                if wrong:
                    found.append(
                        f"{name}: {value.code} reads {read_name} of {sorted(wrong)}"
                    )
            return value

        each_expression(state, check)
    live = live_reads(states)
    for name, state in states.items():
        at = entry.get(name)
        if at is not None:
            found += miscaught(name, state, at, live)
    return found


def after(value: Expr, catcher: tuple[str, int]) -> bool:
    """Whether Python evaluates value after a failure the catcher takes: the
    catcher's way reached it before the passes ran, in the except clause or
    after the try."""
    return catcher in value.excepts


def miscaught(
    name: str, state: dict[str, object], at: Reaching, live: dict[str, set[str]]
) -> list[str]:
    """Each variable the way from a catcher for a failing expression reads
    that holds another value there than Python holds at a place it fails at
    where an expression of the state's Assign or Output fails: the state's
    Assign assigns nothing when it fails (measured), so the catcher's way
    reads the values from before the state, and the catcher's own, where
    Python's except clause reads what the statements before the failing one
    assigned. What the catcher assigns that Python assigns after the failure
    too, as the except clause does, is not compared."""
    found: list[str] = []
    catchers = state.get("Catch", [])
    assert isinstance(catchers, list)
    failing = [
        value
        for value in [*assigns(state).values(), state.get("Output")]
        if isinstance(value, Expr) and not (value.defined and value.total)
    ]
    for index, catcher in enumerate(catchers):
        if not set(catcher["ErrorEquals"]) & EVALUATION_ERRORS:
            continue
        caught = dict(at)
        for key, value in assigns(catcher).items():
            number = value.defines if isinstance(value, Expr) else None
            caught[key] = frozenset({UNKNOWN if number is None else number})
        own = {k: v for k, v in catcher.items() if k != "Next"}
        read = {r for c in expressions_in(own) for r in names_read(c)}
        read |= live[catcher["Next"]]
        clause = {
            key
            for key, assigned in assigns(catcher).items()
            if isinstance(assigned, Expr) and after(assigned, (name, index))
        }
        for value in failing:
            for seen in value.fails:
                held: dict[str, set[int]] = {}
                for variable_name, number in seen:
                    held.setdefault(variable_name, set()).add(number)
                for read_name in sorted(read - clause):
                    python = held.get(read_name, {INITIAL})
                    wrong = reaching_of(caught, read_name) - python - {UNKNOWN}
                    if wrong:
                        found.append(
                            f"{name}: the except clause reads {read_name} of "
                            f"{sorted(wrong)} where {value.code} fails"
                        )
    return found


def checked(definition: dict[str, object], checking: bool) -> None:
    """Where checking, fail on each read misread finds."""
    if checking:
        found = misread(definition)
        assert not found, "\n".join(found)


# Each pass's changes to a definition, at DEBUG, for whoever follows how the
# passes reach a definition: logging.getLogger("sfnx.passes").
PASSES = logging.getLogger("sfnx.passes")


def traced(step: FunctionType, definition: dict[str, object], *args: object) -> None:
    """Run a pass over a definition, logging the states it changed where
    DEBUG is on for sfnx.passes, after the start it moved, as a pass that
    folds or drops the first state does. Otherwise nothing is compared, so
    the passes run as they would without it."""
    if not PASSES.isEnabledFor(logging.DEBUG):
        step(definition, *args)
        return
    start, before = definition["StartAt"], emitted(definition["States"])
    step(definition, *args)
    changes = changed_states(before, emitted(definition["States"]))
    if definition["StartAt"] != start:
        changes.insert(0, f"StartAt: {start} -> {definition['StartAt']}")
    if changes:
        PASSES.debug("%s\n%s", step.__name__, "\n".join(changes))


def changed_states(before: object, after: object) -> list[str]:
    """The states that differ between two States fields, each as a line: -
    and the state as it was for one that went, + and the state as it is for
    one that came, and both for one that changed, in the order of before,
    then of the states that came."""
    assert isinstance(before, dict) and isinstance(after, dict)
    lines = []
    for name in [*before, *(n for n in after if n not in before)]:
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        if was is not None:
            lines.append(f"- {name}: {json.dumps(was, ensure_ascii=False)}")
        if now is not None:
            lines.append(f"+ {name}: {json.dumps(now, ensure_ascii=False)}")
    return lines


def emitted(node: object) -> object:
    """A definition as JSON: each Expr in a field as its template."""
    if isinstance(node, dict):
        return {k: emitted(v) for k, v in node.items()}
    if isinstance(node, list):
        return [emitted(v) for v in node]
    return template_of(node)


def from_asl(node: object) -> object:
    """A definition written as ASL, as the passes read one: each {% %} string
    outside a Comment as an Expr, whose properties are not known, written as
    it was."""
    if isinstance(node, dict):
        return {k: v if k == "Comment" else from_asl(v) for k, v in node.items()}
    if isinstance(node, list):
        return [from_asl(v) for v in node]
    code = code_of(node)
    return node if code is None else replace(expression(code), template=node)


def loose_expressions(node: object) -> list[str]:
    """The {% %} strings of a definition outside an Expr and a Comment, which
    the passes do not read: the compiler writes each expression as an Expr,
    and a definition written as ASL comes in through from_asl."""
    if isinstance(node, Expr):
        return []
    if isinstance(node, dict):
        found = [loose_expressions(v) for k, v in node.items() if k != "Comment"]
        return [text for texts in found for text in texts]
    if isinstance(node, list):
        return [text for item in node for text in loose_expressions(item)]
    return [] if code_of(node) is None else [str(node)]


def compile_machine(
    function: ast.FunctionDef,
    options: dict[str, object],
    context: Module,
    locations: Locations | None,
    optimizing: bool = True,
    checking: bool = False,
) -> dict[str, object]:
    arguments = function.args
    if (
        arguments.posonlyargs
        or arguments.vararg
        or arguments.kwonlyargs
        or arguments.kwarg
        or arguments.defaults
        or len(arguments.args) > 1
    ):
        raise CompileError(
            "a state machine takes one parameter, its input: def pay(input):",
            function,
        )
    # The execution input reads the same from every state, while $states.input
    # becomes the result after a Task, Parallel or Map.
    bindings = {
        # The input is there in every state, and reading it fails for nothing.
        a.arg: expression(
            "$states.context.Execution.Input",
            type=annotate(a.annotation, context),
            defined=True,
            total=True,
        )
        for a in arguments.args
    }
    graph = Graph()
    taken = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    scope = Scope(
        graph,
        bindings,
        context,
        set(bindings),
        taken,
        set(),
        assigned_names(function.body),
        set(),
    )
    scope.locations = locations
    scope.optimizing = optimizing
    scope.checking = checking
    scope.flags = flags(function)
    scope.block(function.body)
    if graph.reachable:
        scope.end_without_value(function, [ended(function)])
    docstring = ast.get_docstring(function)
    comment = {"Comment": docstring} if docstring else {}
    definition = graph.definition()
    if optimizing:
        optimize(definition, scope)
        checked(definition, checking)
    definition = emitted(definition)
    assert isinstance(definition, dict)
    rename_states(definition, graph.taken)
    for scope_definition in machines_in(definition):
        for state in scope_states(scope_definition).values():
            state.pop(NAMED, None)
            state.pop(NAMED_ASSIGNS, None)
    return {**comment, "QueryLanguage": "JSONata", **options, **definition}


def rename_states(
    definition: dict[str, object], taken: dict[str, tuple[str, str]]
) -> None:
    """The states that remain, in the machine and in each branch and Map
    processor, named again in the order their names were taken, as renaming
    says, so a state the passes took out leaves no gap in the serials. A
    definition that reads the name of a state, as the context's State may,
    or $eval, which may build one from text, keeps every name."""
    codes = expressions_in(definition)
    if any(reads_the_name(c) or sensitivity(c).dependencies_unknown for c in codes):
        return
    scopes = list(machines_in(definition))
    kept = {name for scope in scopes for name in scope_states(scope)}
    renamed = renaming(taken, kept)
    if not renamed:
        return
    for scope in scopes:
        states = scope_states(scope)
        redirect(states, renamed)
        scope["States"] = {renamed.get(n, n): state for n, state in states.items()}
        start = scope["StartAt"]
        assert isinstance(start, str)
        scope["StartAt"] = renamed.get(start, start)


def machines_in(scope: dict[str, object]) -> Iterator[dict[str, object]]:
    """A definition, and each branch and Map processor in it, however deep."""
    yield scope
    for state in scope_states(scope).values():
        branches = state.get("Branches", [])
        assert isinstance(branches, list)
        for found in [*branches, state.get("ItemProcessor")]:
            if isinstance(found, dict):
                yield from machines_in(found)


def scope_states(scope: dict[str, object]) -> dict[str, dict[str, object]]:
    states = scope["States"]
    assert isinstance(states, dict)
    return states


# The Python API, which docs/api.md describes: the compiler as the CLI runs
# it, on text or on a file, and the error it raises.
__all__ = ["CompileError", "compile_file", "compile_source"]


def compile_source(
    source: str, filename: str = "<string>"
) -> dict[str, dict[str, object]]:
    """Every state machine in a module, keyed by function name. The source is
    parsed, never imported or run; filename names it in diagnostics."""
    return definitions(source, filename, located=False)


def compile_file(path: str | Path) -> dict[str, dict[str, object]]:
    """A source file, decoded as Python decodes it: UTF-8 unless the file
    declares its encoding. Python rejects a file it cannot decode as a syntax
    error, and so does the compiler."""
    return compile_source(read(path), str(path))


def definitions(
    source: str,
    filename: str,
    located: bool,
    optimizing: bool = True,
    checking: bool = False,
) -> dict[str, dict[str, object]]:
    """The state machines of a module, each state's Comment ending with the
    lines of the file it comes from when located is set, as the CLI's
    --source-locations asks. Without optimizing, the passes leave the states
    as the statements build them, for the tests that compare the two; with
    checking, a read the passes made see another assignment than the one
    it read before them fails the compile, as misread says."""
    locations = Locations(source, filename) if located else None
    try:
        tree = ast.parse(source, filename)
        context = module(tree, source)
        compiled = {
            function.name: compile_machine(
                function, options, context, locations, optimizing, checking
            )
            for function, options in machines(tree, context)
        }
        unnamed = sorted(context.state_names.keys() - context.named)
        if unnamed:
            raise CompileError(
                "# state: names the state of a statement that starts on its "
                "line in a state machine, and no such statement starts here",
                line=unnamed[0],
            )
        return compiled
    except SyntaxError as exc:
        # Python counts the column of a syntax error in characters already.
        raise CompileError(
            exc.msg, line=exc.lineno or 1, column=exc.offset or 1, filename=filename
        ) from exc
    except CompileError as exc:
        raise exc.located(source, filename) from None


def read(path: str | Path) -> str:
    """A source file as text, or the error Python would give for its bytes."""
    data = Path(path).read_bytes()
    advice = (
        "save the file as UTF-8, or declare its encoding: # -*- coding: latin-1 -*-"
    )
    try:
        return decode_source(data)
    except SyntaxError as exc:
        raise CompileError(f"{exc.msg}; {advice}", filename=str(path)) from None
    except UnicodeDecodeError as exc:
        raise CompileError(
            f"the bytes are not {exc.encoding}: {exc.reason}; {advice}",
            line=data.count(b"\n", 0, exc.start) + 1,
            filename=str(path),
        ) from None
