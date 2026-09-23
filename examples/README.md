# Examples

Each example is one machine in one file, with the definition it compiles to next to it. `tests/test_examples.py` checks that each definition is current and runs each machine through [`sfnx.testing`](../docs/testing.md) with stand-in Tasks, so the control flow is verified; how the AWS services behave when the Tasks run is not.

| Example | Pattern | What it shows |
|---|---|---|
| [orders.py](orders.py) → [orders.asl.json](orders.asl.json) | reserve every item of an order, then charge | `for`, `try` / `except`, `raise`, `retry=`, `@state_machine(timeout=)` |
| [poll.py](poll.py) → [poll.asl.json](poll.asl.json) | start a job and poll until it finishes | `while`, `wait()`, `context["Execution"]["Name"]`, names assigned outside the machine |
| [approval.py](approval.py) → [approval.asl.json](approval.asl.json) | wait for a person's decision | `.waitForTaskToken`, `context["Task"]["Token"]`, `timeout=`, `except Timeout`, a `TypedDict` for the result |
| [fanout.py](fanout.py) → [fanout.asl.json](fanout.asl.json) | process every item a few at a time, then do two things at once | `inline_map` with `f(item, index)`, `parallel`, nested functions reading the machine's variables |
| [settle.py](settle.py) → [settle.asl.json](settle.asl.json) | total the charges of a day per currency | `jsonata()` for an expression written out, its result typed by an annotation, `if not` on a list |

To compile one:

```bash
uvx sfnx compile examples/poll.py
```
