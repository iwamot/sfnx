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

    def __init__(self, prefix: str = "", names: set[str] | None = None):
        self.prefix = prefix
        self.names = set() if names is None else names
        self.states: dict[str, dict[str, object]] = {}
        self.start: str | None = None
        self.tails: list[tuple[dict[str, object], str]] = []

    def name(self, base: str) -> str:
        serial = 1
        name = self.prefix + base
        while name in self.names:
            serial += 1
            name = f"{self.prefix}{base}_{serial}"
        if len(name) > MAX_NAME:
            raise ValueError(
                f"state name {name} is longer than {MAX_NAME} characters; "
                "use a shorter variable or function name"
            )
        return name

    def add(self, base: str, state: dict[str, object]) -> str:
        """Add a state where control currently is. It continues with Next unless
        it ends the scope or, like a Choice, links its transitions itself."""
        name = self.name(base)
        for container, key in self.tails:
            container[key] = name
        if self.start is None:
            self.start = name
        self.states[name] = state
        self.names.add(name)
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
