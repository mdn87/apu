from limit import accepts


def test_upper_boundary_is_exclusive() -> None:
    assert not accepts(10)
