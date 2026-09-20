"""Source-located compiler diagnostics."""

import ast


class CompileError(ValueError):
    """A line the compiler does not accept, with what to write instead.

    The column counts characters from 1, whatever a character is worth on
    screen or in UTF-8 or UTF-16. An ast node carries a UTF-8 byte offset
    instead, and `located` reads it as characters once the source is at hand.
    """

    def __init__(
        self,
        message: str,
        node: ast.AST | None = None,
        *,
        line: int = 1,
        column: int = 1,
        filename: str = "<string>",
    ):
        self.message = message
        self.line = getattr(node, "lineno", line)
        self.column = getattr(node, "col_offset", column - 1) + 1
        self.utf8 = hasattr(node, "col_offset")
        self.filename = filename
        super().__init__(f"{filename}:{self.line}:{self.column}: {message}")

    def located(self, source: str, filename: str) -> "CompileError":
        """The same problem, named after the file the source was read from,
        with a byte offset of that source read as a character column."""
        column = self.column
        if self.utf8:
            column = characters(source.split("\n")[self.line - 1], self.column)
        return CompileError(
            self.message, line=self.line, column=column, filename=filename
        )


def characters(line: str, column: int) -> int:
    """A 1-based UTF-8 byte column of a line as a 1-based character column:
    the character that byte belongs to."""
    return len(line.encode()[: column - 1].decode(errors="ignore")) + 1
