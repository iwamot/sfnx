import pytest

from sfnx.graph import Graph, renaming


@pytest.mark.parametrize(
    "taken, kept, renamed",
    [
        # Where no state is gone, no name changes.
        ({"x": ("", "x"), "x_2": ("", "x")}, {"x", "x_2"}, {}),
        # A serial after one that is gone takes its place.
        (
            {"x": ("", "x"), "x_2": ("", "x"), "x_3": ("", "x")},
            {"x", "x_3"},
            {"x_3": "x_2"},
        ),
        ({"x": ("", "x"), "x_2": ("", "x")}, {"x_2"}, {"x_2": "x"}),
        # A branch's names carry the function's, and count with the machine's.
        (
            {"f.r": ("f.", "r"), "f.r_2": ("f.", "r"), "r": ("", "r")},
            {"f.r_2", "r"},
            {"f.r_2": "f.r"},
        ),
        # A variable named x_2 is a base of its own, not x's second serial.
        (
            {"x": ("", "x"), "x_2": ("", "x_2"), "x_3": ("", "x")},
            {"x_2", "x_3"},
            {"x_3": "x"},
        ),
        # A name taken again after its first state was undone counts from
        # where it was taken again.
        ({"y": ("", "y"), "x": ("", "x")}, {"x", "y"}, {}),
    ],
)
def test_the_states_that_remain_are_named_again(taken, kept, renamed):
    assert renaming(taken, kept) == renamed


def test_a_name_taken_again_moves_to_the_end_of_what_is_taken():
    graph = Graph()
    graph.add("x", {"Type": "Pass"})
    graph.add("y", {"Type": "Pass"})
    # The x state is undone, as a checkpoint does, and taken again.
    del graph.states["x"]
    graph.names.discard("x")
    graph.add("x", {"Type": "Pass"})
    assert list(graph.taken) == ["y", "x"]
