"""What more than one test file needs to say about generated expressions."""


def unpacked(code: str, name: str = "$v") -> str:
    """A ** of a value whose type is unknown: Python raises for anything but
    a dict, so the check is written out."""
    return (
        f"({name} := {code}; $type({name}) = 'object' "
        f"? {name} : $error('** unpacks dicts'))"
    )
