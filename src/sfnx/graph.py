"""The states of one scope, named and linked in the order they are emitted."""

MAX_NAME = 80


class Graph:
    """A machine, a Parallel branch or a Map processor.

    Names come from the source (`amount`, `return`, `if`) and repeat as
    `amount_2`; a child scope prefixes the function that makes it (`email.return`).
    Step Functions wants names unique across the whole definition, branches
    and Map processors included, so every graph of a machine shares names.
    Transitions that wait for the next state are kept as (container, key) pairs,
    so a Choice rule and a Next field are linked the same way.
    """

    def __init__(
        self,
        prefix: str = "",
        names: set[str] | None = None,
        taken: dict[str, tuple[str, str]] | None = None,
        written: dict[str, int] | None = None,
    ):
        self.prefix = prefix
        self.names = set() if names is None else names
        # Each name a `# state:` comment gives, with the line it is written
        # on: a function called directly writes its states once per call,
        # and the same comment names each with a serial.
        self.written: dict[str, int] = {} if written is None else written
        # Each name taken, in the order taken, with the prefix and the base it
        # was taken for, which renaming takes again for the states that remain.
        self.taken: dict[str, tuple[str, str]] = {} if taken is None else taken
        self.states: dict[str, dict[str, object]] = {}
        self.start: str | None = None
        self.tails: list[tuple[dict[str, object], str]] = []

    def name(self, base: str) -> str:
        return serial_name(self.prefix, base, self.names)

    def add(self, base: str, state: dict[str, object], line: int | None = None) -> str:
        """Add a state where control currently is. It continues with Next unless
        it ends the scope or, like a Choice, links its transitions itself.
        line: base is the name a `# state:` comment on that line gives the
        state, taken as it is, or with a serial where the same comment named
        a state before."""
        named = line is not None
        name = self.name(base) if not named else self.prefix + base
        if named and name in self.names:
            if self.written.get(name) != line:
                raise ValueError(
                    f"the state name {name} is taken already; name it otherwise"
                )
            # A name the writer chose keeps its words; a serial past the
            # limit is rejected below rather than shortened.
            name = serialed(self.prefix, base, self.names)
        if named:
            self.written.setdefault(self.prefix + base, line)
        if named and len(name) > MAX_NAME:
            raise ValueError(
                f"state name {name} is longer than {MAX_NAME} characters; "
                "use a shorter name"
            )
        for container, key in self.tails:
            container[key] = name
        if self.start is None:
            self.start = name
        self.states[name] = state
        self.names.add(name)
        # A name taken again after its first state was undone is taken now.
        self.taken.pop(name, None)
        self.taken[name] = (self.prefix, base)
        if state["Type"] in {"Succeed", "Fail", "Choice"} or state.get("End"):
            self.tails = []
        else:
            self.tails = [(state, "Next")]
        return name

    @property
    def reachable(self) -> bool:
        """Whether control reaches the current point."""
        return self.start is None or bool(self.tails)

    def definition(self) -> dict[str, object]:
        return {"StartAt": self.start, "States": self.states}


def serial_name(prefix: str, base: str, names: set[str]) -> str:
    """The name for a state of a base: prefix and base, or with the least
    serial from 2 that no name in names has. A base of an action and what it
    calls, `putItem orders`, which no variable name can be as it holds a
    space, gives way to the action alone where the name would be too long."""
    name = serialed(prefix, base, names)
    if len(name) > MAX_NAME and " " in base:
        name = serialed(prefix, base.split(" ", 1)[0], names)
    if len(name) > MAX_NAME:
        raise ValueError(
            f"state name {name} is longer than {MAX_NAME} characters; "
            "use a shorter variable or function name"
        )
    return name


def serialed(prefix: str, base: str, names: set[str]) -> str:
    serial = 1
    name = prefix + base
    while name in names:
        serial += 1
        name = f"{prefix}{base}_{serial}"
    return name


def renaming(taken: dict[str, tuple[str, str]], kept: set[str]) -> dict[str, str]:
    """The new name of each state kept whose name changes when the names are
    taken again, in the order they were taken, for the kept states alone:
    the names they would have had had the others never taken one. The new
    names are all different, and none changes where no state is gone."""
    names: set[str] = set()
    found = {}
    for name, (prefix, base) in taken.items():
        if name not in kept:
            continue
        new = serial_name(prefix, base, names)
        names.add(new)
        if new != name:
            found[name] = new
    return found
