"""Liveness agrees with finding a read along a path before any assignment.

The path search is independent of the compiler's fixed-point analysis. The
same property covers branches, catchers, loops and disconnected states.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from sfnx.compiler import live_reads

VARIABLES = ("a", "b", "c")
NAMES = st.sets(st.sampled_from(VARIABLES))


@st.composite
def graphs(draw):
    names = [f"s{i}" for i in range(draw(st.integers(1, 10)))]
    target = st.sampled_from(names)
    states, reads, edges = {}, {}, {}
    for name in names:
        kind = draw(st.sampled_from(("Wait", "Choice", "Task", "Succeed")))
        reads[name] = draw(NAMES)
        own = dict.fromkeys(sorted(draw(NAMES)), 0)
        output = {v: "{% $" + v + " %}" for v in sorted(reads[name])}
        if kind == "Succeed":
            states[name] = {"Type": kind, "Output": output}
            edges[name] = []
            continue
        default = draw(target)
        state = {"Type": kind, "Assign": own}
        edges[name] = [(default, set(own))]
        if kind == "Choice":
            state["Default"] = default
            rules = []
            condition = (
                "{% " + " or ".join(f"${v}" for v in sorted(reads[name])) + " %}"
            )
            for _ in range(draw(st.integers(1, 3))):
                next_state, assigned = draw(target), draw(NAMES)
                rules.append(
                    {
                        "Condition": condition if reads[name] else True,
                        "Assign": dict.fromkeys(sorted(assigned), 0),
                        "Next": next_state,
                    }
                )
                edges[name].append((next_state, assigned))
            state["Choices"] = rules
        else:
            state.update({"Next": default, "Output": output})
            if kind == "Wait":
                state["Seconds"] = 0
            else:
                state["Resource"] = "arn:aws:states:::lambda:invoke"
                caught, assigned = draw(target), draw(NAMES)
                state["Catch"] = [
                    {
                        "ErrorEquals": ["States.ALL"],
                        "Assign": dict.fromkeys(sorted(assigned), 0),
                        "Next": caught,
                    }
                ]
                edges[name].append((caught, assigned))
        states[name] = state
    return states, reads, edges


def reaches_read(start, variable, reads, edges):
    pending, visited = [start], set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if variable in reads[name]:
            return True
        pending.extend(
            target for target, assigned in edges[name] if variable not in assigned
        )
    return False


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(graphs())
def test_liveness_finds_exactly_the_reads_reachable_before_assignment(graph):
    states, reads, edges = graph
    expected = {
        name: {v for v in VARIABLES if reaches_read(name, v, reads, edges)}
        for name in states
    }
    assert live_reads(states) == expected
