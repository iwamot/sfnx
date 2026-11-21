"""ASL snapshots are independent of mutable compiler expression templates."""

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from sfnx.compiler import emitted, snapshot
from sfnx.expressions import expression

JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda values: (
        st.lists(values, max_size=4) | st.dictionaries(st.text(), values, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(JSON)
def test_a_snapshot_survives_later_changes_inside_an_expression(value):
    template = {"payload": [value]}
    leaf = replace(expression("$x"), template=template)
    definition = {"States": {"done": {"Type": "Succeed", "Output": leaf}}}
    before = snapshot(definition)
    expected = {"States": {"done": {"Type": "Succeed", "Output": {"payload": [value]}}}}
    assert before == expected
    template["payload"].append("later")
    assert before == expected
    assert emitted(definition) != before


def test_expression_analysis_is_not_part_of_a_snapshot():
    leaf = expression("$x")
    changed = replace(leaf, reads=frozenset({("x", 7)}), defined=True, total=True)
    assert leaf != changed
    assert snapshot({"Output": leaf}) == snapshot({"Output": changed})
