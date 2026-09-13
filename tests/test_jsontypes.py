from sfnx.jsontypes import ARRAY, NUMBER, OBJECT, STRING, of, union


def test_union_keeps_the_elements_of_the_side_that_has_them():
    numbers = of(ARRAY, items=of(NUMBER))
    assert union(numbers, of(STRING)) == union(of(STRING), numbers)
    assert union(numbers, of(STRING)).items == of(NUMBER)
    mixed = union(numbers, of(ARRAY, items=of(STRING)))
    assert mixed.items.kinds == {NUMBER, STRING}
    assert union(of(OBJECT, values=of(NUMBER)), of(OBJECT)).values is None
    assert union(numbers, None) is None
