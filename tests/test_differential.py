"""Random programs run by CPython and, compiled, by the ASL interpreter, which
must agree on the result or on the error. The programs keep to what both mean
the same way: whole numbers without / and **, ASCII strings and str() of
numbers only, the edges where docs/language.md notes a difference. task() is a
Lambda that doubles its payload and fails with Declined on a negative one, in
both. A distributed_map whose function raises fails with
States.ExceedToleratedFailureThreshold in Step Functions, so the CPython side
raises that error for it too."""

import textwrap
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sfnx import ExceedToleratedFailureThreshold, distributed_map
from sfnx.compiler import compile_source
from tests import asl
from tests.corpus import same

NUMBER, STRING, BOOLEAN, NUMBERS, STRINGS, MAPPING, OPTIONAL, UNION, KEY = (
    "number",
    "string",
    "boolean",
    "numbers",
    "strings",
    "mapping",
    "optional",
    "union",
    "key",
)

HEADER = """\
from sfnx import distributed_map, inline_map, parallel, state_machine, task, wait


class Declined(Exception):
    pass


@state_machine
def main(input):
"""
PRELUDE = """\
n0: int = input["n0"]
n1 = 2
s0: str = input["s0"]
s1 = "b"
b0 = True
l0: list[int] = input["l0"]
l1 = [1, 2]
m0 = ["x", ""]
d0: dict[str, int] = input["d0"]
o0: int | None = None
u0: int | str = input["u0"]
"""
RESULT = "return [n0, n1, s0, s1, b0, l0, l1, m0, d0, o0, u0]"
DECLARED = {
    NUMBER: ("n0", "n1"),
    STRING: ("s0", "s1"),
    BOOLEAN: ("b0",),
    NUMBERS: ("l0", "l1"),
    STRINGS: ("m0",),
    MAPPING: ("d0",),
    OPTIONAL: ("o0",),
    UNION: ("u0",),
    # The keys a dict loop reads, which every dict here has.
    KEY: (),
}
LAMBDA = "arn:aws:states:::lambda:invoke"


@dataclass(frozen=True)
class Env:
    """What a statement can read and assign, by type, and what leaves: the
    return of the function, and break and continue in a loop. A raise cannot be
    in a try whose except would catch it."""

    readable: dict[str, tuple[str, ...]]
    assignable: dict[str, tuple[str, ...]]
    result: str
    loop: bool = False
    tried: bool = False
    # Whether control can leave from here. A branch that tests a type does
    # not, as the test would type what follows by the branch left standing.
    leaving: bool = True
    depth: int = 0

    def reading(self, kind: str, name: str) -> "Env":
        return replace(self, readable=add(self.readable, kind, name))

    def narrowing(self, name: str) -> "Env":
        """An optional number shown not to be None, a number from then on. It
        leaves the optionals, whose tests the compiler takes over the type:
        `is None` on a known number would type the branch as null."""
        assignable = self.assignable
        if name in assignable[OPTIONAL]:
            assignable = add(assignable, NUMBER, name)
        return replace(
            self,
            readable=add(without(self.readable, name), NUMBER, name),
            assignable=without(assignable, name) | {NUMBER: assignable[NUMBER]},
        )

    def settling(self, name: str, kind: str) -> "Env":
        """A union shown to be one kind, read as that kind and not assigned,
        so that it stays so."""
        return replace(
            self,
            readable=add(without(self.readable, name), kind, name),
            assignable=without(self.assignable, name),
        )

    def owning(self, kind: str, name: str) -> "Env":
        return replace(
            self,
            readable=add(self.readable, kind, name),
            assignable=add(self.assignable, kind, name),
        )


def add(
    names: dict[str, tuple[str, ...]], kind: str, name: str
) -> dict[str, tuple[str, ...]]:
    return {**names, kind: (*names[kind], name)}


def without(names: dict[str, tuple[str, ...]], name: str) -> dict[str, tuple[str, ...]]:
    return {kind: tuple(n for n in found if n != name) for kind, found in names.items()}


@dataclass
class Program:
    data: st.DataObject
    nesting: int = 2
    # Whether a caught error's message is read, which a Lambda function in
    # Step Functions writes as JSON of its own.
    messages: bool = True
    serial: int = 0
    lines: list[str] = field(default_factory=list)

    def pick(self, options: list[str] | tuple[str, ...]) -> str:
        return self.data.draw(st.sampled_from(options))

    def number_in(self, low: int, high: int) -> int:
        return self.data.draw(st.integers(low, high))

    def fresh(self, base: str) -> str:
        self.serial += 1
        return f"{base}{self.serial}"

    def emit(self, indent: int, line: str) -> None:
        self.lines.append("    " * indent + line)

    # Expressions

    def expr(self, env: Env, kind: str, depth: int) -> str:
        return getattr(self, kind)(env, depth)

    def variable_or(self, env: Env, kind: str, literal: str) -> str:
        names = env.readable[kind]
        return self.pick(names) if names and self.number_in(0, 2) else literal

    def number(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, NUMBER, str(self.number_in(-3, 5)))
        if depth <= 0:
            return leaf
        d = depth - 1
        form = self.pick(
            ["leaf", "add", "sub", "mul", "div", "neg", "len", "first", "key"]
            + ["last", "either", "union", "if", "optional"]
        )
        if form == "add":
            return f"({self.number(env, d)} + {self.number(env, d)})"
        if form == "sub":
            return f"({self.number(env, d)} - {self.number(env, d)})"
        if form == "mul":
            return f"({self.number(env, d)} * {self.number_in(-2, 2)})"
        if form == "div":
            operator = self.pick(["//", "%"])
            divisor = self.pick(["1", "2", "3", "-2"])
            return f"({self.number(env, d)} {operator} {divisor})"
        if form == "neg":
            return f"-({self.number(env, d)})"
        if form == "len":
            kind = self.pick([STRING, NUMBERS, STRINGS, MAPPING])
            return f"len({self.expr(env, kind, d)})"
        if form == "first":
            items = self.numbers(env, d)
            return f"({items}[0] if {items} else {self.number(env, d)})"
        if form == "key":
            if env.readable[KEY] and self.number_in(0, 1):
                return f"{self.mapping(env, d)}[{self.pick(env.readable[KEY])}]"
            return f'{self.mapping(env, d)}["{self.pick(["a", "b"])}"]'
        if form == "last":
            items = self.numbers(env, d)
            return f"({items}[-1] if {items} else {self.number(env, d)})"
        if form == "either":
            operator = self.pick(["or", "and"])
            return f"({self.number(env, d)} {operator} {self.number(env, d)})"
        if form == "union" and env.readable[UNION]:
            name = self.pick(env.readable[UNION])
            return f"({name} * 2 if isinstance({name}, int) else len({name}))"
        if form == "if":
            return self.conditional(env, NUMBER, d)
        if form == "optional" and env.readable[OPTIONAL]:
            name = self.pick(env.readable[OPTIONAL])
            # The else side knows the name is None, so it tests it no more.
            otherwise = self.number(
                replace(env, readable=without(env.readable, name)), d
            )
            return f"({name} if {name} is not None else {otherwise})"
        return leaf

    def string(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, STRING, repr(self.pick(["", "a", "b", "ab"])))
        if depth <= 0:
            return leaf
        d = depth - 1
        form = self.pick(
            ["leaf", "add", "format", "str", "first", "either", "union", "if"]
        )
        if form == "add":
            return f"({self.string(env, d)} + {self.string(env, d)})"
        if form == "either":
            operator = self.pick(["or", "and"])
            return f"({self.string(env, d)} {operator} {self.string(env, d)})"
        if form == "union" and env.readable[UNION]:
            name = self.pick(env.readable[UNION])
            return f"({name} if isinstance({name}, str) else str({name}))"
        if form == "format":
            # Python before 3.12 takes no quotes inside an f-string, so the
            # values are names.
            text = self.variable_or(env, STRING, "")
            number = self.variable_or(env, NUMBER, str(self.number_in(0, 5)))
            return f'f"{{{text}}}-{{{number}}}"' if text else f'f"-{{{number}}}"'
        if form == "str":
            return f"str({self.number(env, d)})"
        if form == "first":
            text = self.string(env, d)
            return f'({text}[0] if {text} else "z")'
        if form == "if":
            return self.conditional(env, STRING, d)
        return leaf

    def boolean(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, BOOLEAN, self.pick(["True", "False"]))
        if depth <= 0:
            return leaf
        d = depth - 1
        form = self.pick(
            ["leaf", "order", "chain", "equal", "text", "member", "word"]
            + ["substring", "key", "not", "and", "or", "truth", "lists", "empty"]
        )
        if form == "empty":
            return f"(not {self.pick([self.numbers(env, d), self.strings(env, d)])})"
        if form == "order":
            operator = self.pick(["<", "<=", ">", ">="])
            return f"({self.number(env, d)} {operator} {self.number(env, d)})"
        if form == "chain":
            numbers = [self.number(env, d) for _ in range(3)]
            return f"({numbers[0]} < {numbers[1]} <= {numbers[2]})"
        if form == "equal":
            operator = self.pick(["==", "!="])
            return f"({self.number(env, d)} {operator} {self.number(env, d)})"
        if form == "text":
            operator = self.pick(["==", "<"])
            return f"({self.string(env, d)} {operator} {self.string(env, d)})"
        if form == "member":
            return f"({self.number(env, d)} in {self.numbers(env, d)})"
        if form == "word":
            return f"({self.string(env, d)} in {self.strings(env, d)})"
        if form == "substring":
            return f"({self.string(env, d)} in {self.string(env, d)})"
        if form == "key":
            key = f'"{self.pick(["a", "c"])}"'
            if env.readable[KEY] and self.number_in(0, 1):
                key = self.pick(env.readable[KEY])
            return f"({key} in {self.mapping(env, d)})"
        if form == "not":
            return f"(not {self.boolean(env, d)})"
        if form in {"and", "or"}:
            return f"({self.boolean(env, d)} {form} {self.boolean(env, d)})"
        if form == "truth":
            kind = self.pick([NUMBER, STRING, NUMBERS, STRINGS])
            return f"bool({self.expr(env, kind, d)})"
        if form == "lists":
            return f"({self.numbers(env, d)} == {self.numbers(env, d)})"
        return leaf

    def numbers(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, NUMBERS, "[1, 2]")
        if depth <= 0:
            return leaf
        d = depth - 1
        form = self.pick(["leaf", "literal", "add", "comprehension", "if"])
        if form == "literal":
            count = self.number_in(1, 2)
            return "[" + ", ".join(self.number(env, d) for _ in range(count)) + "]"
        if form == "add":
            return f"({self.numbers(env, d)} + {self.numbers(env, d)})"
        if form == "comprehension":
            name = self.fresh("x")
            source = self.numbers(env, d)
            inner = env.reading(NUMBER, name)
            element = self.number(inner, d)
            test = f" if {self.boolean(inner, d)}" if self.number_in(0, 1) else ""
            return f"[{element} for {name} in {source}{test}]"
        if form == "if":
            return self.conditional(env, NUMBERS, d)
        return leaf

    def strings(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, STRINGS, '["x"]')
        if depth <= 0:
            return leaf
        d = depth - 1
        form = self.pick(["leaf", "literal", "add", "comprehension"])
        if form == "literal":
            count = self.number_in(1, 2)
            return "[" + ", ".join(self.string(env, d) for _ in range(count)) + "]"
        if form == "add":
            return f"({self.strings(env, d)} + {self.strings(env, d)})"
        if form == "comprehension":
            name = self.fresh("w")
            source = self.strings(env, d)
            inner = env.reading(STRING, name)
            test = f" if {self.boolean(inner, d)}" if self.number_in(0, 1) else ""
            return f"[{self.string(inner, d)} for {name} in {source}{test}]"
        return leaf

    def mapping(self, env: Env, depth: int) -> str:
        leaf = self.variable_or(env, MAPPING, '{"a": 1, "b": 2}')
        if depth <= 0 or not self.number_in(0, 1):
            return leaf
        d = depth - 1
        form = self.pick(["literal", "comprehension", "items"])
        if form == "literal":
            return f'{{"a": {self.number(env, d)}, "b": {self.number(env, d)}}}'
        # A subscript reads a key a dict loop bound, so every dict here has the
        # keys a and b and no others, and a comprehension keeps that key set: a
        # condition that dropped one would leave the subscript with no value.
        if form == "comprehension":
            name = self.fresh("w")
            keys = self.pick(['["a", "b"]', '["b", "a"]', '["a", "b", "a"]'])
            inner = env.reading(STRING, name)
            return f"{{{name}: {self.number(inner, d)} for {name} in {keys}}}"
        key, value = self.fresh("k"), self.fresh("v")
        inner = env.reading(STRING, key).reading(NUMBER, value)
        return (
            f"{{{key}: {self.number(inner, d)} "
            f"for {key}, {value} in {self.mapping(env, d)}.items()}}"
        )

    def optional(self, env: Env, depth: int) -> str:
        return "None" if self.number_in(0, 2) == 0 else self.number(env, depth)

    def union(self, env: Env, depth: int) -> str:
        return self.expr(env, self.pick([NUMBER, STRING]), depth)

    def conditional(self, env: Env, kind: str, depth: int) -> str:
        then = self.expr(env, kind, depth)
        test = self.boolean(env, depth)
        return f"({then} if {test} else {self.expr(env, kind, depth)})"

    # Statements

    def block(self, env: Env, indent: int) -> bool:
        """One or more statements, and whether control leaves at the end: no
        statement follows a return, break, continue or raise, as the compiler
        rejects a line that never runs."""
        for _ in range(self.number_in(1, 2)):
            if self.statement(env, indent):
                return True
        leaving = ["return"]
        if env.loop:
            leaving += ["break", "continue"]
        if not env.tried:
            leaving.append("raise")
        if not env.leaving or self.number_in(0, 5):
            return False
        form = self.pick(leaving)
        if form == "return":
            self.emit(indent, env.result)
        elif form == "raise":
            self.emit(indent, f'raise Declined("{self.pick(["r", "s"])}")')
        else:
            self.emit(indent, form)
        return True

    def statement(self, env: Env, indent: int) -> bool:
        forms = ["assign", "assign", "augment", "swap", "task"]
        if env.depth < self.nesting:
            forms += ["if", "none", "isinstance", "range", "for", "while"]
            forms += ["enumerate", "zip", "items"]
            forms += ["forever", "try", "wait"]
            forms += ["parallel", "map", "distributed"]
        form = self.pick(forms)
        targets = [kind for kind, names in env.assignable.items() if names]
        inner = replace(env, depth=env.depth + 1)
        looping = replace(inner, loop=True)
        if form == "assign" and targets:
            kind = self.pick(targets)
            name = self.pick(env.assignable[kind])
            # An optional keeps its declaration, as a None of no annotation
            # would type it null, whose `is not None` branch the compiler types.
            declaration = {OPTIONAL: ": int | None", UNION: ": int | str"}.get(kind, "")
            self.emit(indent, f"{name}{declaration} = {self.expr(env, kind, 2)}")
        elif form == "augment" and (env.assignable[NUMBER] or env.assignable[STRING]):
            if env.assignable[NUMBER] and (
                not env.assignable[STRING] or self.number_in(0, 1)
            ):
                name = self.pick(env.assignable[NUMBER])
                operator = self.pick(["+=", "-=", "//=", "*="])
                value = self.number(env, 1)
                if operator == "*=":
                    value = str(self.number_in(-2, 2))
                elif operator == "//=":
                    value = self.pick(["2", "-3"])
                self.emit(indent, f"{name} {operator} {value}")
            else:
                name = self.pick(env.assignable[STRING])
                self.emit(indent, f"{name} += {self.string(env, 1)}")
        elif form == "swap" and len(env.assignable[NUMBER]) > 1:
            first, second = env.assignable[NUMBER][:2]
            self.emit(indent, f"{first}, {second} = {second}, {first}")
        elif form == "task" and env.assignable[NUMBER]:
            name = self.pick(env.assignable[NUMBER])
            payload = self.number(env, 1)
            call = f'task("{LAMBDA}", {{"FunctionName": "f", "Payload": {{"n": {payload}}}}})'
            if self.number_in(0, 3):
                self.emit(indent, f'{name}: int = {call}["Payload"]')
            else:
                self.emit(indent, call)
        elif form == "if":
            return self.branch(env, indent)
        elif form == "none" and env.assignable[OPTIONAL]:
            name = self.pick(env.assignable[OPTIONAL])
            self.emit(indent, f"if {name} is None:")
            self.emit(indent + 1, f"{name} = {self.number(env, 1)}")
            self.emit(indent, "else:")
            self.block(replace(inner.narrowing(name), leaving=False), indent + 1)
        elif form == "range":
            name = self.fresh("i")
            stop = self.pick(["0", "1", "3", f"len({self.numbers(env, 0)})"])
            self.emit(indent, f"for {name} in range({stop}):")
            self.block(looping.reading(NUMBER, name), indent + 1)
        elif form == "enumerate":
            index, name = self.fresh("i"), self.fresh("x")
            self.emit(
                indent, f"for {index}, {name} in enumerate({self.numbers(env, 1)}):"
            )
            self.block(looping.reading(NUMBER, index).owning(NUMBER, name), indent + 1)
        elif form == "zip":
            first, second = self.fresh("x"), self.fresh("w")
            lists = f"{self.numbers(env, 1)}, {self.strings(env, 1)}"
            self.emit(indent, f"for {first}, {second} in zip({lists}):")
            zipped = looping.owning(NUMBER, first).owning(STRING, second)
            self.block(zipped, indent + 1)
        elif form == "items":
            key, name = self.fresh("w"), self.fresh("x")
            self.emit(indent, f"for {key}, {name} in ({self.mapping(env, 1)}).items():")
            keyed = looping.reading(KEY, key).reading(STRING, key).owning(NUMBER, name)
            self.block(keyed, indent + 1)
        elif form == "for":
            kind, element = self.pick(
                [(NUMBERS, NUMBER), (STRINGS, STRING), (MAPPING, KEY)]
            )
            name = self.fresh("x" if element == NUMBER else "w")
            self.emit(indent, f"for {name} in {self.expr(env, kind, 1)}:")
            if element == KEY:
                # A key stays one the dicts have, so it is not assigned.
                keyed = looping.reading(KEY, name).reading(STRING, name)
                self.block(keyed, indent + 1)
            else:
                # A list loop's variable is the body's to assign again.
                self.block(looping.owning(element, name), indent + 1)
        elif form == "isinstance" and env.readable[UNION]:
            name = self.pick(env.readable[UNION])
            self.emit(indent, f"if isinstance({name}, str):")
            staying = replace(inner, leaving=False)
            self.block(staying.settling(name, STRING), indent + 1)
            self.emit(indent, "else:")
            self.block(staying.settling(name, NUMBER), indent + 1)
        elif form == "wait":
            self.emit(indent, "wait(0)")
        elif form == "while":
            counter = self.fresh("c")
            self.emit(indent, f"{counter} = 0")
            self.emit(indent, f"while {counter} < {self.number_in(0, 3)}:")
            self.emit(indent + 1, f"{counter} = {counter} + 1")
            self.block(looping.reading(NUMBER, counter), indent + 1)
        elif form == "forever":
            counter = self.fresh("c")
            self.emit(indent, f"{counter} = 0")
            self.emit(indent, "while True:")
            self.emit(indent + 1, f"{counter} = {counter} + 1")
            self.emit(indent + 1, f"if {counter} > {self.number_in(0, 3)}:")
            self.emit(indent + 2, "break")
            self.block(looping.reading(NUMBER, counter), indent + 1)
        elif form == "try":
            return self.attempt(env, indent)
        elif form == "parallel" and env.assignable[NUMBERS]:
            count = self.number_in(1, 2)
            functions = [self.function(env, indent, []) for _ in range(count)]
            call = f"parallel({', '.join(functions)})"
            if count == 2 and len(env.assignable[NUMBER]) > 1 and self.number_in(0, 1):
                first, second = env.assignable[NUMBER][:2]
                self.emit(indent, f"{first}, {second} = {call}")
            else:
                self.emit(indent, f"{self.pick(env.assignable[NUMBERS])} = {call}")
        elif form == "map" and env.assignable[NUMBERS]:
            parameters = [self.fresh("p")]
            if self.number_in(0, 1):
                parameters.append(self.fresh("i"))
            function = self.function(env, indent, parameters)
            target = self.pick(env.assignable[NUMBERS])
            items = self.numbers(env, 1)
            self.emit(indent, f"{target} = inline_map({function}, {items})")
        elif form == "distributed" and env.assignable[NUMBERS]:
            item, rate = self.fresh("p"), self.fresh("k")
            function = self.function(env, indent, [item, rate], isolated=True)
            target = self.pick(env.assignable[NUMBERS])
            items = self.numbers(env, 1)
            arguments = f'args={{"{rate}": {self.number(env, 1)}}}'
            self.emit(
                indent, f"{target} = distributed_map({function}, {items}, {arguments})"
            )
        else:
            self.emit(indent, "pass")
        return False

    def branch(self, env: Env, indent: int) -> bool:
        inner = replace(env, depth=env.depth + 1)
        self.emit(indent, f"if {self.boolean(env, 2)}:")
        ends = [self.block(inner, indent + 1)]
        if self.number_in(0, 1):
            self.emit(indent, f"elif {self.boolean(env, 2)}:")
            ends.append(self.block(inner, indent + 1))
        if not self.number_in(0, 1):
            return False
        self.emit(indent, "else:")
        ends.append(self.block(inner, indent + 1))
        return all(ends)

    def attempt(self, env: Env, indent: int) -> bool:
        """A try whose body starts with a task(), which a clause catches."""
        inner = replace(env, depth=env.depth + 1)
        self.emit(indent, "try:")
        payload = self.number(env, 1)
        call = (
            f'task("{LAMBDA}", {{"FunctionName": "f", "Payload": {{"n": {payload}}}}})'
        )
        if env.assignable[NUMBER]:
            name = self.pick(env.assignable[NUMBER])
            self.emit(indent + 1, f'{name}: int = {call}["Payload"]')
        else:
            self.emit(indent + 1, call)
        body_ends = self.block(replace(inner, tried=True), indent + 1)
        clauses = self.pick(["Declined", "Exception", "Declined Exception"]).split()
        ends = []
        for clause in clauses:
            error = self.fresh("e") if self.number_in(0, 2) else None
            self.emit(indent, f"except {clause}{f' as {error}' if error else ''}:")
            if error and env.assignable[STRING] and self.messages:
                target = self.pick(env.assignable[STRING])
                self.emit(indent + 1, f"{target} = str({error})")
            # Raising again inside another try would be caught there in Python
            # but not in ASL, which the compiler rejects.
            if env.tried or self.number_in(0, 3):
                ends.append(self.block(inner, indent + 1))
            else:
                self.emit(indent + 1, "raise")
                ends.append(True)
        if not body_ends and self.number_in(0, 2) == 0:
            self.emit(indent, "else:")
            body_ends = self.block(inner, indent + 1)
        return body_ends and all(ends)

    def function(
        self, env: Env, indent: int, parameters: list[str], isolated: bool = False
    ) -> str:
        """A function for parallel() or a map, with number parameters. It reads
        what is around it, unless distributed_map runs it, and assigns only
        names of its own, as a branch or a Map iteration cannot assign the
        variables of its enclosing scope."""
        name = self.fresh("f")
        local = self.fresh("t")
        signature = ", ".join(f"{p}: int" for p in parameters)
        self.emit(indent, f"def {name}({signature}):")
        around = replace(
            env, loop=False, tried=False, leaving=True, depth=env.depth + 1
        )
        nothing: dict[str, tuple[str, ...]] = dict.fromkeys(DECLARED, ())
        around = replace(around, assignable=nothing)
        if isolated:
            around = replace(around, readable=nothing)
        for parameter in parameters:
            around = around.owning(NUMBER, parameter)
        self.emit(indent + 1, f"{local} = {self.number(around, 1)}")
        inner = around.owning(NUMBER, local)
        inner = replace(inner, result=f"return {self.number(inner, 1)}")
        if not self.block(inner, indent + 1):
            self.emit(indent + 1, f"return {self.number(inner, 2)}")
        return name

    def source(self) -> str:
        env = Env(dict(DECLARED), dict(DECLARED), RESULT)
        ends = self.block(env, 0)
        body = PRELUDE + "\n".join(self.lines) + "\n" + ("" if ends else RESULT)
        return HEADER + textwrap.indent(body + "\n", "    ")


def lambda_payload(arguments: object) -> int:
    """The number sent, in an object: Step Functions takes no number literal
    as a Lambda Payload."""
    assert isinstance(arguments, dict)
    payload = arguments["Payload"]
    assert isinstance(payload, dict) and isinstance(payload["n"], int)
    return payload["n"]


class Lambda(Mapping[str, object]):
    """The task of every state: the payload doubled, or Declined."""

    def __getitem__(self, name: str) -> object:
        return self.invoke

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    @staticmethod
    def invoke(arguments: object) -> object:
        payload = lambda_payload(arguments)
        if payload < 0:
            raise asl.Failure("Declined", str(payload))
        return {"Payload": payload * 2}


INPUTS = st.fixed_dictionaries(
    {
        "n0": st.integers(-3, 5),
        "s0": st.sampled_from(["", "a", "ab"]),
        "l0": st.lists(st.integers(-2, 3), max_size=3),
        "d0": st.fixed_dictionaries({"a": st.integers(-2, 3), "b": st.integers(-2, 3)}),
        "u0": st.one_of(st.integers(-2, 3), st.sampled_from(["", "a"])),
    }
)


def in_cpython(program: str, execution_input: object) -> tuple[str, object]:
    """The result, or the error and cause of a Declined that nothing caught."""
    namespace: dict[str, object] = {}
    exec(compile(program, "<program>", "exec"), namespace)
    declined = namespace["Declined"]
    assert isinstance(declined, type)

    def task(resource: str, arguments: object) -> object:
        payload = lambda_payload(arguments)
        if payload < 0:
            raise declined(str(payload))
        return {"Payload": payload * 2}

    def child_executions(
        function: Callable[..., object], items: list, /, **options: object
    ) -> list:
        """A function that raises fails the map, as the generated maps set no
        failure threshold."""
        try:
            return distributed_map(function, items, **options)
        except declined as exc:
            raise ExceedToleratedFailureThreshold(asl.EXCEEDED) from exc

    namespace["task"] = task
    namespace["distributed_map"] = child_executions
    main = namespace["main"]
    assert callable(main)
    try:
        return "result", main(execution_input)
    except declined as exc:
        return "error", ["Declined", str(exc)]
    except ExceedToleratedFailureThreshold as exc:
        return "error", ["States.ExceedToleratedFailureThreshold", str(exc)]


def in_asl(program: str, execution_input: object) -> tuple[str, object]:
    (definition,) = compile_source(program).values()
    try:
        return "result", asl.run(definition, execution_input, Lambda())
    except asl.Failure as failure:
        return "error", [failure.error, failure.cause]


@settings(
    max_examples=200,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(st.data())
def test_cpython_and_the_compiled_definition_agree(data):
    program = Program(data).source()
    execution_input = data.draw(INPUTS)
    expected = in_cpython(program, execution_input)
    actual = in_asl(program, execution_input)
    assert expected[0] == actual[0] and same(expected[1], actual[1]), (
        f"{program}\ninput: {execution_input}\nCPython: {expected}\nASL: {actual}"
    )
