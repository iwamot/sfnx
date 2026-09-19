"""What more than one test file needs to say about generated expressions."""


def truthiness(name: str = "$v") -> str:
    """Python's truthiness of a value whose type is unknown: an array is
    truthy when it holds anything, and $boolean reads the rest."""
    return f"$type({name}) = 'array' ? $count({name}) > 0 : $boolean({name})"


def truthy(code: str, name: str = "$v") -> str:
    """That test on a value bound to a name first, as the compiler binds
    anything longer than a variable."""
    return f"({name} := {code}; {truthiness(name)})"


def unpacked(code: str, name: str = "$v") -> str:
    """A ** of a value whose type is unknown: Python raises for anything but
    a dict, so the check is written out."""
    return (
        f"({name} := {code}; $type({name}) = 'object' "
        f"? {name} : $error('** unpacks dicts'))"
    )
