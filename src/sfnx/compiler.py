"""Compile @state_machine functions to Amazon States Language."""

import ast
import copy
import re
import symtable
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from importlib.util import decode_source
from pathlib import Path

from sfnx.diagnostics import CompileError
from sfnx.errors import EVERYTHING, caught, raised, retriers
from sfnx.expressions import (
    ADD,
    COMPARE,
    Expr,
    array,
    binary,
    call,
    expression,
    literal,
    spelling,
    variable,
)
from sfnx.expressions import field as step
from sfnx.expressions import index as index_expr
from sfnx.graph import Graph
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
from sfnx.locations import Locations, Origin
from sfnx.module import Module, holds, module, qualified
from sfnx.translate import StateCall, Translator, text, unpacked

# Step Functions reserves $states for its own variables.
MAX_VARIABLE = 80
# How often a loop is compiled again with wider types before a type that keeps
# changing is taken as unknown.
MAX_WIDENING = 8
MAX_WAIT = 99_999_999
# RFC 3339 with an uppercase T and Z, as Wait requires.
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
# The functions whose calls are states of their own.
STATE_CALLS = ("task", "parallel", "inline_map", "distributed_map")
# The errors of a retrier that runs a state again when its Output fails.
RETRIED = frozenset({EVERYTHING, "States.QueryEvaluationError"})
# Context a state and the Pass after it read alike. The State part, its name
# and when it was entered, differs, and so does the whole object.
SHARED_CONTEXT = re.compile(r"\$states\.context(?!\.(Execution|StateMachine|Map)\b)")


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
class Carrier:
    """What can hold the assignments at a point that one transition alone
    leads to: the Wait just added, or a Choice rule or the Choice's Default
    that leads into a branch. With the transition, where in the source the
    holder comes from, and its own comment."""

    holder: dict[str, object]
    key: str
    origins: list[Origin] = field(default_factory=list)
    remark: str | None = None


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
    pending_node: ast.AST | None
    pending_origins: list[Origin]
    pending_remarks: list[str]
    remark: str | None
    # Where pending assignments can go, and its fields at the checkpoint.
    carrier: Carrier | None
    carried: dict[str, object]
    # The state a return can end with, and its fields at the checkpoint.
    result: Result | None
    resulted: dict[str, object]
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
        self.pending_node: ast.AST | None = None
        self.pending_origins: list[Origin] = []
        # The comments above the pending assignments, for the Pass they share,
        # and the comment of the statement being compiled, for the first state
        # it adds.
        self.pending_remarks: list[str] = []
        self.remark: str | None = None
        # What can hold the assignments that follow, until a state or a flush
        # does; while it lasts, control is only at its transition.
        self.carrier: Carrier | None = None
        # The Task, Parallel or Map just added, until another state or a flush
        # follows it; while it lasts, control is right after it.
        self.result: Result | None = None
        self.loops: list[Loop] = []
        # The functions called directly whose bodies are being compiled here,
        # innermost last.
        self.expansions: list[Expansion] = []
        # Names the bodies of functions called directly assigned here, which a
        # later call may use again.
        self.expanded: set[str] = set()
        # Names in the source, and the variables loops and handlers added for
        # themselves, shared by every scope of the machine.
        self.taken = taken
        self.hidden = hidden
        # Map Run labels, unique across the machine.
        self.labels: set[str] = set()
        # The except clauses of the try statements around the current point,
        # outermost first, and the clauses being compiled, for a bare raise.
        self.tries: list[list[Handler]] = []
        # States added that can report errors to except, counted.
        self.catchable = 0
        self.handling: list[Handler] = []
        # The statement being compiled, and, with --source-locations, the
        # source each state's Comment points into.
        self.current: ast.stmt | None = None
        self.locations: Locations | None = None

    def spelling(self, name: str) -> str:
        return spelling(name, self.module.spellings)

    def variable(self, name: str, type: Type | None) -> Expr:
        return variable(name, self.module.spellings, type)

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
        return self.insert(base, state, node, origins or [self.here()])

    def here(self) -> Origin:
        assert self.current is not None
        return Origin(self.current)

    def insert(
        self, base: str, state: dict[str, object], node: ast.AST, origins: list[Origin]
    ) -> str:
        self.carrier = None
        self.result = None
        if self.locations is not None:
            located = self.locations.line(origins)
            remark = state.get("Comment")
            state = commented(state, f"{remark}\n{located}" if remark else located)
        try:
            return self.graph.add(base, state)
        except ValueError as exc:
            raise CompileError(str(exc), node) from exc

    def flush(self) -> None:
        carrier, self.carrier = self.carrier, None
        self.result = None
        if not self.pending:
            return
        assert self.pending_node is not None
        pending = self.pending
        first = next(iter(pending))
        assign = {self.spelling(k): value.template for k, value in pending.items()}
        node = self.pending_node
        origins = self.pending_origins
        remarks = self.pending_remarks
        self.pending = {}
        self.pending_node = None
        self.pending_origins = []
        self.pending_remarks = []
        if carrier is not None and self.joins(carrier, list(pending.values())):
            self.hold(carrier, assign, origins, remarks)
            return
        state: dict[str, object] = {"Type": "Pass", "Assign": assign}
        if remarks:
            state = commented(state, "\n".join(remarks))
        self.insert(first, state, node, origins)

    def joins(self, carrier: Carrier, values: list[Expr]) -> bool:
        """Whether assignments can be the Assign of what holds them. The
        carrier lasts until a state or a flush, and every join, branch and loop
        flushes first, so control reaches them only through its transition.
        What is left is that no value reads what could differ there, the time,
        a random value or the State part of the context. A Wait and a Choice
        have no Catch, so a value that fails ends the execution either way."""
        [(tail, key)] = self.graph.tails
        assert tail is carrier.holder and key == carrier.key
        return not any(
            value.volatile or SHARED_CONTEXT.search(value.code) for value in values
        )

    def hold(
        self,
        carrier: Carrier,
        assign: dict[str, object],
        origins: list[Origin],
        remarks: list[str],
    ) -> None:
        holder = carrier.holder
        remark = "\n".join(r for r in (carrier.remark, *remarks) if r) or None
        if self.locations is not None:
            located = self.locations.line(carrier.origins + origins)
            remark = f"{remark}\n{located}" if remark else located
        if remark:
            commented(holder, remark)
        holder["Assign"] = assign

    def defer(self, name: str, value: Expr, node: ast.AST, origin: Origin) -> None:
        """A value for the Pass the pending assignments share, and where in the
        source it comes from."""
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
        enclosing = self.current
        self.current = node
        try:
            self.compile_statement(node)
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
                self.finish(literal(None), None, node)
            elif not self.end_with_result(node.value):
                self.finish(*self.translator.statement_value(node.value), node)
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
        elif isinstance(node, ast.Expr) and any(
            self.called(node.value, name) for name in STATE_CALLS
        ):
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
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            # A docstring or a string used as a comment.
            return
        else:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
            ):
                self.translator.check_import(node.value.func)
            name = type(node).__name__
            raise CompileError(
                STATEMENTS.get(name, f"{name} statements are not supported"), node
            )

    def augment(self, node: ast.AugAssign) -> None:
        """x += v as x = x + v. A list is extended in place in Python, which
        other names for it see; a JSON value is a copy, so it is written out."""
        if not isinstance(node.target, ast.Name):
            raise CompileError(
                "assign one variable per statement: x = ...", node.target
            )
        name = node.target.id
        reading = ast.copy_location(ast.Name(name, ast.Load()), node.target)
        current = self.translator.expr(reading)
        if (
            isinstance(node.op, ast.Add)
            and current.type is not None
            and ARRAY in current.type.kinds
        ):
            symbol = ast.unparse(node.value)
            raise CompileError(
                f"{name} += extends the list in place, which other names for it "
                f"see in Python; write {name} = {name} + {symbol}",
                node,
            )
        value = ast.copy_location(ast.BinOp(reading, node.op, node.value), node)
        self.assign(
            ast.copy_location(ast.Name(name, ast.Store()), node.target), value, None
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
            self.carrier = None
            self.result = None
        for name in renaming.values():
            self.bindings.pop(name, None)
            self.declared.pop(name, None)
            self.partial.discard(name)

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
            made = next((n for n in ast.walk(argument) if self.adds_states(n)), None)
            if made is not None:
                assert isinstance(made, ast.Call)
                raise CompileError(
                    f"{name}() takes values; call {ast.unparse(made.func)}() on a "
                    "line of its own first and pass its result",
                    made,
                )
        return written

    def adds_states(self, node: ast.AST) -> bool:
        """Whether a node is a call that adds states."""
        return isinstance(node, ast.Call) and (
            any(self.called(node, name) for name in STATE_CALLS)
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
        if not isinstance(target, ast.Name):
            raise CompileError("assign one variable per statement: x = ...", target)
        name = target.id
        self.claim(name, target)
        declared = annotate(annotation_node, self.module) or self.announced.get(name)
        value, call = self.translator.statement_value(value_node)
        known = declared or value.type or self.declared.get(name)
        if call is not None:
            # The state's own Assign takes its result. A Catch leaves with the
            # declarations from before, as the assignment did not happen.
            self.flush()
            assign = {self.spelling(name): value.template}
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
        # state, so one that reads a pending assignment needs a state of its own.
        if name in self.pending or value.variables & self.pending.keys():
            self.flush()
        self.defer(name, value, target, self.here())
        self.hold_remark()
        self.bindings[name] = self.variable(name, known)
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
            self.spelling(name): value.template
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

    def end_with_result(self, value_node: ast.expr) -> bool:
        """A return right after a Task, a Parallel or a Map, as the state's
        Output and End, where a person ends a machine or a branch. The Output
        reads the result where the return reads the variables the state
        assigns, and the other variables from before the state, as the return
        does. The state keeps its Next where the Output could fail otherwise
        than the return: a Catch would take the failure, and a retrier for
        States.ALL or States.QueryEvaluationError would run the state again
        (measured). So does a value that reads the time, a random value or the
        State part of the context."""
        result = self.result
        if result is None or self.pending:
            return False
        # A call in the return is a state of its own, and translating it again
        # would name its states again.
        if any(
            self.called(node, name)
            for node in ast.walk(value_node)
            if isinstance(node, ast.Call)
            for name in STATE_CALLS
        ):
            return False
        state = result.state
        retriers = state.get("Retry", [])
        assert isinstance(retriers, list)
        retried = {error for retrier in retriers for error in retrier["ErrorEquals"]}
        if "Catch" in state or retried & RETRIED:
            return False
        before = dict(self.bindings)
        # Each variable reads as the result, with the type it was bound with,
        # which a declaration can give.
        self.bindings.update(
            {
                name: replace(value, type=before[name].type)
                for name, value in result.values.items()
            }
        )
        try:
            value, call = self.translator.statement_value(value_node)
        finally:
            self.bindings.clear()
            self.bindings.update(before)
        if call is not None or value.volatile or SHARED_CONTEXT.search(value.code):
            return False
        [(tail, key)] = self.graph.tails
        assert tail is state and key == "Next"
        self.graph.tails = []
        self.result = None
        self.returns.append(value.type)
        remark = "\n".join(r for r in (result.remark, self.remark) if r) or None
        self.remark = None
        if self.locations is not None:
            located = self.locations.line(result.origins + [self.here()])
            remark = f"{remark}\n{located}" if remark else located
        if remark:
            commented(state, remark)
        state.pop("Assign", None)
        if value.code != "$states.result":
            state["Output"] = value.template
        state["End"] = True
        return True

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
            state = {"Type": "Succeed", "Output": value.template}
            self.add("return", state, node, origins)
            return
        # A Task at the end ends the machine itself; its output is the result
        # unless the return makes something of it.
        ending: dict[str, object] = {}
        if value.code != "$states.result":
            ending["Output"] = value.template
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
                    catcher["Assign"] = {error: "{% $states.errorOutput %}"}
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
            Graph(f"{function.name}.", self.graph.names),
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
            scope.finish(literal(None), None, function, [ended(function)])
        definition = scope.graph.definition()
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
        return value.template

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
        selector: dict[str, object] = {}
        bindings = dict(self.bindings) if local else {}
        pending: dict[str, Expr] = {}
        binds = makes_states(
            function, self.module.names, {**self.module.functions, **self.functions}
        )
        for parameter, source, kind in zip(parameters, sources, kinds, strict=False):
            name = parameter.arg
            selector[name] = "{% " + source + " %}"
            declared = annotate(parameter.annotation, self.module) or kind
            if binds:
                bindings[name] = self.variable(name, declared)
                pending[name] = step(expression("$states.input"), name)
            else:
                bindings[name] = replace(
                    step(expression("$states.input"), name), type=declared
                )
        scope = self.child(
            function, local, bindings, self.parameters if local else set()
        )
        for parameter in parameters:
            if binds:
                scope.claim(parameter.arg, parameter)
            else:
                self.check_variable(parameter.arg, parameter)
        scope.pending = pending
        scope.pending_node = function if pending else None
        if pending:
            scope.pending_origins = [Origin(function, "parameters", header=True)]
        # What the first state binds is the function's to assign.
        scope.assigned |= pending.keys()
        processor, returned = self.run_child(scope, function)
        state: dict[str, object] = {"Type": "Map", "Items": items.template}
        state["ItemSelector"] = selector
        if "max_concurrency" in found:
            state["MaxConcurrency"] = self.count_option(
                found["max_concurrency"], "max_concurrency"
            )
        state["ItemProcessor"] = {"ProcessorConfig": {"Mode": "INLINE"}, **processor}
        return state, of(ARRAY, items=joined(returned))

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
            state["Items"] = items.template
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
                parameters[0]: "{% $states.context.Map.Item.Value %}",
                **arguments,
            }
            state["ItemSelector"] = selector
            bindings[parameters[0]] = replace(step(read, parameters[0]), type=item_type)
            for name in parameters[1:]:
                bindings[name] = step(read, name)
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
            result[key.value] = self.translator.expr(value).template
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
                "Error": f"{{% ${caught_error}.Error %}}",
                "Cause": f"{{% ${caught_error}.Cause %}}",
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
        for handlers in self.tries:
            for handler in handlers:
                if error in handler.errors or handler.errors == [EVERYTHING]:
                    raise CompileError(
                        "a raise ends the execution with a Fail state, which except "
                        "does not catch; handle the case with if instead",
                        node,
                    )
        state: dict[str, object] = {"Type": "Fail", "Error": error}
        if arguments:
            cause = self.translator.expr(arguments[0])
            if cause.type is not None and cause.type.kinds != {STRING}:
                cause = text(cause)
            state["Cause"] = cause.template
        self.flush()
        self.add("raise", state, node)

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
                "parallel() and the maps do",
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
            rule: dict[str, object] = {"Condition": condition.template}
            rules.append(rule)
            bodies.append((body, {**failed, **when}, rule))
            failed = {**failed, **unless}
        state: dict[str, object] = {"Type": "Choice", "Choices": rules}
        origins = [Origin(h, header=True) for h in headers]
        remark = self.remark
        self.add("if", state, node, origins)
        start = self.save()
        ends = []
        for body, proven, rule in bodies:
            ends.append(self.follow(start, proven, Carrier(rule, "Next"), body))
        default = Carrier(state, "Default", origins, remark)
        ends.append(self.follow(start, failed, default, otherwise))
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
        carrier: Carrier,
        body: list[ast.stmt],
    ) -> Flow:
        """A branch of a Choice, entered through the carrier's transition,
        which can hold the branch's first assignments."""
        self.restore(start)
        for name, declared in proven.items():
            self.bindings[name] = replace(self.bindings[name], type=declared)
        self.graph.tails = [(carrier.holder, carrier.key)]
        self.carrier = carrier
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
            bindings[name] = replace(first, type=declared)
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
            self.pending_node,
            list(self.pending_origins),
            list(self.pending_remarks),
            self.remark,
            self.carrier,
            dict(self.carrier.holder) if self.carrier else {},
            self.result,
            dict(self.result.state) if self.result else {},
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
        self.pending_node = saved.pending_node
        self.pending_origins = list(saved.pending_origins)
        self.pending_remarks = list(saved.pending_remarks)
        self.remark = saved.remark
        self.carrier = saved.carrier
        if saved.carrier is not None:
            saved.carrier.holder.clear()
            saved.carrier.holder.update(saved.carried)
        self.result = saved.result
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
                rule: dict[str, object] = {"Condition": condition.template}
                state: dict[str, object] = {"Type": "Choice", "Choices": [rule]}
                head = self.add("while", state, node, [Origin(node, header=True)])
                self.follow(start, when, Carrier(rule, "Next"), node.body)
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
        """The counter of a loop, assigned 0 before it. A pending value of the
        same name is written over: the loop assigns the counter first, and it
        ends with the loop."""
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
            if target in self.pending:
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
        rule: dict[str, object] = {"Condition": condition.template}
        state: dict[str, object] = {"Type": "Choice", "Choices": [rule]}
        head = self.add("for", state, node, [Origin(node, header=True)])
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
            # whatever the body left pending.
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
            field = {"Seconds": seconds}
        elif until and len(node.keywords) == 1 and not node.args:
            # A datetime is written as the timestamp text Timestamp takes.
            moment = self.translator.datetime_string(until[0].value)
            value = moment or self.translator.expr(until[0].value)
            timestamp = value.template
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
            field = {"Timestamp": timestamp}
        else:
            raise CompileError(
                "wait takes seconds or until=: wait(10) or "
                'wait(until=input["resumeAt"])',
                node,
            )
        state: dict[str, object] = {"Type": "Wait", **field}
        remark = self.remark
        self.add("wait", state, node)
        self.carrier = Carrier(state, "Next", [self.here()], remark)


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


def makes_states(
    function: ast.FunctionDef,
    names: dict[str, str],
    functions: dict[str, ast.FunctionDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether a function calls task(), parallel() or a map anywhere, or calls
    directly a function that does."""
    made = {f"sfnx.{name}" for name in STATE_CALLS}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if qualified(node.func, names) in made:
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


def compile_machine(
    function: ast.FunctionDef,
    options: dict[str, object],
    context: Module,
    locations: Locations | None,
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
        a.arg: expression(
            "$states.context.Execution.Input", type=annotate(a.annotation, context)
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
    scope.block(function.body)
    if graph.reachable:
        scope.finish(literal(None), None, function, [ended(function)])
    docstring = ast.get_docstring(function)
    comment = {"Comment": docstring} if docstring else {}
    return {**comment, "QueryLanguage": "JSONata", **options, **graph.definition()}


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
    source: str, filename: str, located: bool
) -> dict[str, dict[str, object]]:
    """The state machines of a module, each state's Comment ending with the
    lines of the file it comes from when located is set, as the CLI's
    --source-locations asks."""
    locations = Locations(source, filename) if located else None
    try:
        tree = ast.parse(source, filename)
        context = module(tree, source)
        return {
            function.name: compile_machine(function, options, context, locations)
            for function, options in machines(tree, context)
        }
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
