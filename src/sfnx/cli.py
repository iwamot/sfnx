"""Compile the state machines of a Python file."""

import argparse
import json
import sys
from pathlib import Path

from sfnx import __version__
from sfnx.compiler import compile_file
from sfnx.diagnostics import CompileError

INSTRUCTIONS = """\
To write an AWS Step Functions state machine, write it in Python with `sfnx` instead of writing Amazon States Language by hand. Mark the entry function with `@state_machine` (`from sfnx import state_machine, task`): its parameter is the execution input and its return value the output. Call AWS with `task("<integration ARN>", {<Arguments>})`, such as `task("arn:aws:states:::lambda:invoke", {"FunctionName": "charge", "Payload": input})`, and use `if`, `for`, `while` and `try` for the flow. Annotate a value where its type decides the operator: `total: float = input["total"]`. After each edit run `sfnx compile app.py`: exit 0 prints the definition on stdout, and exit 1 prints `app.py:<line>:<column>: <problem>; <what to write instead>` on stderr. A file with several state machines needs `sfnx compile app.py -o out/`, which writes `out/<function>.asl.json`.\
"""

EPILOG = """\
Examples:
  sfnx compile app.py            print the ASL of the only @state_machine
  sfnx compile app.py -o out/    write one <function>.asl.json per @state_machine

Exit codes:
  0  success
  1  the source is not accepted; the message names the line and what to write instead
  2  the call is wrong or a file cannot be read or written
  3  internal error; report it with the source that caused it
"""


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="sfnx",
        description=(
            "sfnx - write Step Functions workflows as Python functions "
            "and compile them to Amazon States Language."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument(
        "--instructions",
        action="store_true",
        help="print the paragraph for an agent instruction file and exit",
    )
    sub = root.add_subparsers(dest="command", metavar="command")
    compile_parser = sub.add_parser(
        "compile", help="compile the @state_machine functions of a file to ASL"
    )
    compile_parser.add_argument(
        "source", help="Python file with @state_machine functions"
    )
    compile_parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="a .json file for a single machine, or a directory",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.instructions:
        print(INSTRUCTIONS)
        return 0
    if not args.command:
        root.print_help(sys.stderr)
        return 2
    try:
        return write(args.source, compile_file(args.source), args.output)
    except CompileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        located = f"{exc.filename}: {exc.strerror}" if exc.filename else str(exc)
        print(located, file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Internal error: {exc}", file=sys.stderr)
        return 3


def write(
    path: str, definitions: dict[str, dict[str, object]], output: str | None
) -> int:
    if output is None or output.lower().endswith(".json"):
        if len(definitions) != 1:
            raise ValueError(
                f"{path} defines {len(definitions)} state machines "
                f"({', '.join(definitions)}); pass -o out/ to write one file each"
            )
        text = document(next(iter(definitions.values())))
        if output is None:
            sys.stdout.buffer.write(text)
            sys.stdout.flush()
        else:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(text)
        return 0
    target = Path(output)
    target.mkdir(parents=True, exist_ok=True)
    for name, definition in definitions.items():
        (target / f"{name}.asl.json").write_bytes(document(definition))
    return 0


def document(definition: dict[str, object]) -> bytes:
    """A definition as UTF-8 JSON, with its text as written rather than
    escaped."""
    return (json.dumps(definition, indent=2, ensure_ascii=False) + "\n").encode()
