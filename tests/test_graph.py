from hypothesis import example, given, settings
from hypothesis import strategies as st

from sfnx.graph import Graph, renaming


def test_a_name_taken_again_moves_to_the_end_of_what_is_taken():
    graph = Graph()
    graph.add("x", {"Type": "Pass"})
    graph.add("y", {"Type": "Pass"})
    # The x state is undone, as a checkpoint does, and taken again.
    del graph.states["x"]
    graph.names.discard("x")
    graph.add("x", {"Type": "Pass"})
    assert list(graph.taken) == ["y", "x"]


@settings(max_examples=500, derandomize=True, database=None, deadline=None)
@example([("", "send " + "a" * 73, i not in {0, 2}) for i in range(12)])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(("", "f.", "f", "g.", "入力.")),
            st.sampled_from(
                ("x", "X", "x_2", "x_3", "fx", "f.x", "return", "send " + "a" * 80)
            ),
            st.booleans(),
        ),
        max_size=100,
    )
)
def test_renaming_matches_rebuilding_only_the_surviving_states(operations):
    names, taken, kept = set(), {}, set()
    for prefix, base, keep in operations:
        graph = Graph(prefix, names, taken)
        name = graph.add(base, {"Type": "Succeed"})
        if keep:
            kept.add(name)
    # Rebuild with Graph's ordinary allocation, which can also handle
    # rollbacks, using only the surviving states in their original order.
    fresh_names, expected = set(), {}
    for old, (prefix, base) in taken.items():
        if old in kept:
            new = Graph(prefix, fresh_names).add(base, {"Type": "Succeed"})
            if new != old:
                expected[old] = new
    assert renaming(taken, kept) == expected


def test_shortened_actions_share_serials_across_their_long_bases():
    names, taken = set(), {}
    for base in ("send " + "a" * 80, "send " + "b" * 80, "send", "send_2"):
        Graph("", names, taken).add(base, {"Type": "Succeed"})
    assert renaming(taken, names) == {}
    kept = names - {"send"}
    assert renaming(taken, kept) == {
        "send_2": "send",
        "send_3": "send_2",
    }
