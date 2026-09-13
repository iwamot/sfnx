"""Source-located compiler diagnostics."""

import ast


class CompileError(ValueError):
    """A line the compiler does not accept, with what to write instead."""

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
        self.filename = filename
        super().__init__(f"{filename}:{self.line}:{self.column}: {message}")

    def located(self, filename: str) -> "CompileError":
        return CompileError(
            self.message, line=self.line, column=self.column, filename=filename
        )
