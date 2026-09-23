"""Where in the source each state comes from, as a line ending its Comment."""

import ast
import json
import tokenize
from dataclasses import dataclass

from sfnx.diagnostics import characters

PREFIX = "sfnx-source: "


@dataclass(frozen=True)
class Origin:
    """A piece of source a state comes from: a whole statement, or only the
    header of a compound one (`if ...:`, `for ...:`, `def ...:`). A state the
    source does not spell, such as a loop's counter, names its role."""

    node: ast.stmt
    role: str | None = None
    header: bool = False


class Locations:
    """The source being compiled, and the file name it goes by."""

    def __init__(self, source: str, filename: str):
        self.lines = source.split("\n")
        self.filename = filename

    def line(self, origins: list[Origin]) -> str:
        """`sfnx-source: ` and a JSON object: the file, and each span as
        `line:column-line:column`, counted from 1 in characters, the end
        exclusive."""
        spans = []
        for origin in dict.fromkeys(origins):
            span: dict[str, str] = {"at": self.span(origin)}
            if origin.role is not None:
                span["role"] = origin.role
            spans.append(span)
        found = {"file": self.filename, "spans": spans}
        return PREFIX + json.dumps(found, ensure_ascii=False)

    def span(self, origin: Origin) -> str:
        node = origin.node
        start = characters(self.lines[node.lineno - 1], node.col_offset + 1)
        if origin.header:
            end_line, end = self.colon(node.lineno)
        else:
            assert node.end_lineno is not None and node.end_col_offset is not None
            end_line = node.end_lineno
            end = characters(self.lines[end_line - 1], node.end_col_offset + 1)
        return f"{node.lineno}:{start}-{end_line}:{end}"

    def colon(self, first: int) -> tuple[int, int]:
        """The line and the character column right after the colon that ends
        the header of a compound statement starting on a line: the first one
        outside brackets, as those of a slice, a dict or an annotation are
        inside them."""
        rest = iter(line + "\n" for line in self.lines[first - 1 :])
        tokens = (
            token
            for token in tokenize.generate_tokens(lambda: next(rest, ""))
            if token.type == tokenize.OP
        )
        depth = 0
        token = next(tokens)
        while token.string != ":" or depth:
            depth += (token.string in "([{") - (token.string in ")]}")
            token = next(tokens)
        return first + token.end[0] - 1, token.end[1] + 1
