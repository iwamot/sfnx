# Python API

The compiler is a Python function as well as a command. A build script, a test or a tool of your own can hand it a file or the text of one and get the definitions back as dicts, with the same contract as the CLI: the source is parsed, never imported or run.

## Compiling

```python
import json

from sfnx.compiler import CompileError, compile_file, compile_source

definitions = compile_file("app.py")
definitions = compile_source(text)
```

Both return a dict with one entry per `@state_machine` function, keyed by the function's name, in the order the functions are written. Each value is the definition as a dict of JSON values, with any `${Name}` placeholders as written in the source. What the CLI writes is that dict as JSON:

```python
(definition,) = definitions.values()
json.dumps(definition, indent=2, ensure_ascii=False)
```

`compile_source` takes the text of a module. `compile_file` reads the file as Python does, in UTF-8 unless the file declares another encoding.

## Errors

A line the compiler does not accept raises `CompileError`, a `ValueError`. Its `str()` is the line the CLI prints, `<filename>:<line>:<column>: <message>`, and its `message`, `line`, `column` and `filename` are the parts: the line and column count from 1, the column in characters, and the filename is the path given to `compile_file`, or `<string>` for text. A syntax error, an encoding the file does not decode in, and a file without a `@state_machine` function are `CompileError` too, since each is something to fix in the source. One error is raised for the whole file, the first one found, as the CLI reports one line per run.

`compile_file` raises `OSError` when the file cannot be read, as `open` would. Any other exception is a bug in the compiler; report it with the source that caused it.

## What is public

`CompileError`, `compile_file` and `compile_source` are the Python API, and `sfnx.compiler.__all__` lists them; [compatibility.md](compatibility.md) says what a release can change of them. The other names of `sfnx.compiler` and its neighbours are the compiler's own and change without notice.

`sfnx.compiler` imports botocore for the service models it checks arguments against, so import it where the compiling happens. The names a workflow module imports from `sfnx` itself (`state_machine`, `task`, ...) stay light, and importing `sfnx` does not import the compiler.
