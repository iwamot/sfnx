"""Measure generated ASL and compiler time without running workflows on AWS.

    uv run python -m tests.optimization --out /tmp/optimization.json
        [--group corpus examples scaling] [--select ID ...] [--repeats 5]

The corpus and scaling cases run in sfnx.testing against their DSL expectations.
Examples and task-chain scaling cases are compiled without executing their Tasks.
The unoptimized definition is a cost baseline, not a semantic reference: some
spellings rely on the passes to give their documented meaning.
"""

import argparse
import json
import platform
import statistics
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import sfnx
from sfnx import testing
from sfnx.compiler import definitions
from tests import corpus


@dataclass(frozen=True)
class Workload:
    id: str
    group: str
    source: str
    case: corpus.Case | None = None


def program(body: str) -> str:
    return (
        "from sfnx import state_machine, task\n\n@state_machine\ndef main(input):\n"
        + textwrap.indent(body, "    ")
    )


def workloads() -> list[Workload]:
    found = [Workload(c.id, "corpus", c.source, c) for c in corpus.CASES]
    examples = Path(__file__).parent.parent / "examples"
    found += [
        Workload(f"example-{p.stem}", "examples", p.read_text())
        for p in sorted(examples.glob("*.py"))
    ]
    for n in (16, 64, 256):
        body = "\n".join(f'if input["x"] == {i}:\n    return {i}' for i in range(n))
        case = corpus.Case(
            f"branches-{n}",
            "scaling",
            body + "\nreturn -1",
            {"x": -1},
            corpus.Value(-1),
            "The fallback after consecutive tests.",
        )
        found.append(Workload(case.id, "scaling", case.source, case))
        body = 'n: int = input["n"]\ntotal = 0\nfor i in range(n):\n    total += i\nreturn total'
        case = corpus.Case(
            f"loop-{n}",
            "scaling",
            body,
            {"n": n},
            corpus.Value(n * (n - 1) // 2),
            "The sum of the loop's counters.",
        )
        found.append(Workload(case.id, "scaling", case.source, case))
        call = 'task("arn:aws:states:::lambda:invoke", {"FunctionName": "f", "Payload": input})'
        body = f"result = {call}\n" + (call + "\n") * n + "return result"
        found.append(Workload(f"tasks-{n}", "scaling", program(body)))
    for depth in (4, 8, 16):
        body = "return -1"
        for i in reversed(range(depth)):
            body = (
                f'if input["x"] > {i}:\n'
                + textwrap.indent(body, "    ")
                + f"\nreturn {i}"
            )
        case = corpus.Case(
            f"nested-{depth}",
            "scaling",
            body,
            {"x": depth},
            corpus.Value(-1),
            "The path through every nested test.",
        )
        found.append(Workload(case.id, "scaling", case.source, case))
        body = "return -1"
        for i in reversed(range(depth)):
            body = (
                f'if input["v{i}"] > 0:\n'
                + textwrap.indent(body, "    ")
                + f"\nreturn {i}"
            )
        case = corpus.Case(
            f"independent-{depth}",
            "scaling",
            body,
            {f"v{i}": 1 for i in range(depth)},
            corpus.Value(-1),
            "The path through independent nested tests.",
        )
        found.append(Workload(case.id, "scaling", case.source, case))
    return found


def expression_lengths(template: object) -> list[int]:
    """Read expression templates in data fields, including keys named Comment."""
    if isinstance(template, dict):
        return [n for value in template.values() for n in expression_lengths(value)]
    if isinstance(template, list):
        return [n for value in template for n in expression_lengths(value)]
    code = testing.expression(template)
    return [] if code is None else [len(code.strip())]


def footprint(machines: dict) -> dict:
    """Count all nested states; size is compact UTF-8 ASL, without outer names.
    The longest expression is an inspection aid, not a readability score."""
    states = [
        state
        for machine in machines.values()
        for _, state in testing.every_state(machine)
    ]
    lengths = []
    for state in states:
        fields = {
            k: v
            for k, v in state.items()
            if k not in {"Comment", "Branches", "ItemProcessor", "Choices", "Catch"}
        }
        for rule in [*state.get("Choices", []), *state.get("Catch", [])]:
            lengths += expression_lengths(
                {k: v for k, v in rule.items() if k != "Comment"}
            )
        lengths += expression_lengths(fields)
    return {
        "states": len(states),
        "choice_rules": sum(len(s.get("Choices", [])) for s in states),
        "asl_bytes": sum(
            len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode())
            for m in machines.values()
        ),
        "max_expression_chars": max(lengths, default=0),
    }


def execute(machines: dict, case: corpus.Case) -> dict:
    """Judge the DSL expectation and count local state entries, nested ones
    included. Retries do not add another entry for the retried parent itself;
    these counts are not a model of AWS billing or execution time."""
    (definition,) = machines.values()
    sequence = corpus.Sequence(case.random)
    execution = testing.run(definition, case.input, functions={"random": sequence})
    outcome = (
        corpus.Result(execution.output)
        if execution.error is None
        else corpus.Failed(execution.error, execution.cause or "")
    )
    verdict = corpus.judge_locally(case, corpus.LocalRun(outcome, sequence.calls))
    return {
        "entered_states": len(execution.states),
        "expected": verdict.passed,
        "detail": verdict.detail,
        "outcome": corpus.actual(outcome),
        "random_calls": sequence.calls,
    }


def measure(workload: Workload, repeats: int) -> dict:
    """Warm both compilations and alternate their order in each timing pair.
    Only compilation is timed; interpreting the definition is outside it."""
    compiled, times = {}, {False: [], True: []}
    for optimizing in (False, True):
        compiled[optimizing] = definitions(
            workload.source, workload.id, False, optimizing
        )
    for sample in range(repeats):
        for optimizing in (False, True) if sample % 2 == 0 else (True, False):
            start = perf_counter()
            compiled[optimizing] = definitions(
                workload.source, workload.id, False, optimizing
            )
            times[optimizing].append((perf_counter() - start) * 1000)
    result = {
        "id": workload.id,
        "group": workload.group,
        "source": workload.source,
        "input": None if workload.case is None else workload.case.input,
        "expected": None
        if workload.case is None
        else corpus.described(workload.case.expected),
    }
    for label, optimizing in (("before", False), ("after", True)):
        machines = compiled[optimizing]
        result[label] = {
            **footprint(machines),
            "definitions": machines,
            "compile_ms": statistics.median(times[optimizing]),
            "compile_samples_ms": times[optimizing],
            "execution": None
            if workload.case is None
            else execute(machines, workload.case),
        }
    return result


def table_line(row: dict) -> str:
    before, after = row["before"], row["after"]
    entries = "compile-only"
    status = ""
    if after["execution"] is not None:
        entries = f"{before['execution']['entered_states']}->{after['execution']['entered_states']}"
        status = "expected" if after["execution"]["expected"] else "MISMATCH"
        if not before["execution"]["expected"]:
            status += "; baseline differs"
    return (
        f"{row['id']:<59} {before['states']:>4}->{after['states']:<4} "
        f"{entries:>12} {before['asl_bytes']:>7}->{after['asl_bytes']:<7} "
        f"{before['max_expression_chars']:>5}->{after['max_expression_chars']:<5} "
        f"{before['compile_ms']:>8.3f}->{after['compile_ms']:<8.3f} {status}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path, help="JSON results file")
    parser.add_argument("--group", nargs="+", choices=("corpus", "examples", "scaling"))
    parser.add_argument("--select", nargs="+", metavar="ID")
    parser.add_argument("--repeats", type=int, default=5, help="timed pairs per source")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    selected = workloads()
    if args.select:
        unknown = set(args.select) - {w.id for w in selected}
        if unknown:
            parser.error(f"no such workload: {', '.join(sorted(unknown))}")
        selected = [w for w in selected if w.id in args.select]
    if args.group:
        selected = [w for w in selected if w.group in args.group]
    if not selected:
        parser.error("the selection contains no workloads")
    print(
        f"{'workload':<59}    states      entries              ASL bytes   longest expression           compile ms"
    )
    rows = []
    for workload in selected:
        row = measure(workload, args.repeats)
        rows.append(row)
        print(table_line(row), flush=True)
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "sfnx": sfnx.__version__,
        "repeats": args.repeats,
        "timing": "warmed in-process compilation; median milliseconds",
        "cases": rows,
    }
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return int(
        any(
            r["after"]["execution"] is not None
            and not r["after"]["execution"]["expected"]
            for r in rows
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
