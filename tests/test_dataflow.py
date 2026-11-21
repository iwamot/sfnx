"""Dataflow agrees with finding reads along paths before reassignment.

Independent path searches check liveness and which assignments the compiler
keeps across branches, catchers, loops, nested scopes and disconnected states.
"""

import copy
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from sfnx.compiler import drop_dead_assignments, emitted, from_asl, live_reads

VARIABLES = ("a", "b", "c")
NAMES = st.sets(st.sampled_from(VARIABLES))


@st.composite
def graphs(draw):
    names = [f"s{i}" for i in range(draw(st.integers(1, 10)))]
    target = st.sampled_from(names)
    states, reads, edges = {}, {}, {}
    for name in names:
        kind = draw(st.sampled_from(("Wait", "Choice", "Task", "Parallel", "Succeed")))
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
            state["Next"] = default
            if kind == "Parallel":
                state["Branches"] = [
                    {
                        "StartAt": "read",
                        "States": {"read": {"Type": "Succeed", "Output": output}},
                    }
                ]
            else:
                state["Output"] = output
            if kind == "Wait":
                state["Seconds"] = 0
            else:
                if kind == "Task":
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


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(graphs(), st.booleans())
def test_dead_assignments_and_their_passes_are_removed(graph, at_start):
    """Only values read before reassignment remain. A Task or Parallel also
    keeps values its evaluation-error catcher reads, unless that catcher
    replaces them. The graph's assignments hold literals, so deleting one
    does not make another assignment dead."""
    states, reads, edges = graph
    expected = copy.deepcopy(states)
    for name, state in expected.items():
        holders = [state, *state.get("Choices", []), *state.get("Catch", [])]
        for index, (target, _) in enumerate(edges[name]):
            holder = holders[index]
            kept = {
                v for v in holder["Assign"] if reaches_read(target, v, reads, edges)
            }
            if index == 0 and state["Type"] in {"Task", "Parallel"}:
                kept |= {
                    v
                    for v in holder["Assign"]
                    if any(
                        v not in assigned and reaches_read(caught, v, reads, edges)
                        for caught, assigned in edges[name][1:]
                    )
                }
            holder["Assign"] = {
                v: value for v, value in holder["Assign"].items() if v in kept
            }
            if not holder["Assign"]:
                del holder["Assign"]
    start = next(iter(states))
    prefix = {
        "unused": {
            "Type": "Pass",
            "Assign": {"unused": "{% $states.input.a %}"},
            "Next": start,
        }
    }
    if not at_start:
        prefix["entry"] = {"Type": "Wait", "Seconds": 0, "Next": "unused"}
        expected["entry"] = {"Type": "Wait", "Seconds": 0, "Next": start}
    definition = from_asl(
        {
            "StartAt": "unused" if at_start else "entry",
            "States": {**prefix, **states},
        }
    )
    drop_dead_assignments(definition)
    assert emitted(definition) == {
        "StartAt": start if at_start else "entry",
        "States": expected,
    }


VALUES = st.one_of(
    st.sampled_from(
        [
            (0, None),
            ("{% $states.input.a %}", None),
            ("{% [$map($states.input.a, function($v) { $v })] %}", None),
        ]
    ),
    st.sampled_from(
        [
            ("{% $" + v + suffix + " %}", v)
            for v in VARIABLES
            for suffix in ("", ".field")
        ]
    ),
)


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(VARIABLES), VALUES), max_size=20), NAMES)
def test_only_assignments_contributing_to_the_return_remain(steps, returned):
    """Track the origins of values forward through a straight-line program.
    Only origins contributing to the return survive, including through
    copies, self-assignment and field reads. The input path or comprehension
    may spell a variable's name without reading that variable."""
    assignments = {"init": dict.fromkeys(VARIABLES, 0)}
    origins = {v: {("init", v)} for v in VARIABLES}
    for i, (variable, (value, read)) in enumerate(steps):
        name = f"s{i}"
        assignments[name] = {variable: value}
        origins[variable] = {(name, variable)} | (origins[read] if read else set())
    names = [*assignments, "return"]
    states = {
        name: {"Type": "Pass", "Assign": values, "Next": names[i + 1]}
        for i, (name, values) in enumerate(assignments.items())
    }
    states["return"] = {
        "Type": "Succeed",
        "Output": {v: "{% $" + v + " %}" for v in sorted(returned)},
    }
    definition = from_asl({"StartAt": "init", "States": states})
    needed = set().union(*(origins[v] for v in returned))
    expected = {}
    for name, values in assignments.items():
        kept = {v: value for v, value in values.items() if (name, v) in needed}
        if kept:
            expected[name] = {"Type": "Pass", "Assign": kept}
    expected["return"] = states["return"]
    remaining = list(expected)
    for name, following in pairwise(remaining):
        expected[name]["Next"] = following
    drop_dead_assignments(definition)
    assert emitted(definition) == {"StartAt": remaining[0], "States": expected}
    drop_dead_assignments(definition)
    assert emitted(definition) == {"StartAt": remaining[0], "States": expected}
