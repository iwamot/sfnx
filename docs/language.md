# The language

sfnx accepts the part of Python that has a counterpart in ASL and whose meaning JSONata can express. This page lists what that part is and what it compiles to. The reasons behind the decisions, and the Step Functions behavior they rely on, are in [design.md](design.md).

The generated ASL is the contract: sfnx writes what someone would write in ASL for what the source intends, rather than reproducing every detail of Python. A module is still ordinary Python that imports and runs, and where the two would disagree, [Where results differ from Python](#where-results-differ-from-python) lists what to expect.

## The machine

```python
from sfnx import state_machine


@state_machine(timeout=300)
def pay(input):
    return input["amount"]
```

- The function is the machine and its name the file name (`pay.asl.json`). `timeout` becomes `TimeoutSeconds`; the parentheses are optional.
- The parameter is the execution input. It compiles to `$states.context.Execution.Input` everywhere, because `$states.input` changes after a Task. A machine may take no parameter. Assigning to the parameter is rejected.
- A function that ends without `return` returns `null`.
- A file may hold several machines; the compiler reads it without importing it.
- The docstring of the function is the `Comment` of the definition.

## Names of states

A state is named after what it does: the variable it assigns, `return`, `raise`, `if`, `for`, `while`, `wait`, `map`, `parallel`, or the action of a Task on its own line (`getItem`, `invoke`). Repeated names get serials (`amount`, `amount_2`). States inside a Parallel branch or a Map are prefixed with the function that holds them (`email.return`).

States split only where ASL requires it: an assignment that reads another pending assignment, a Task, a Choice. Independent assignments share one Pass, a Task's result goes in the Task's `Assign`, and a machine that returns a Task's result ends on that Task.

## Comments

```python
# Charge the card once the stock is reserved.
receipt = task("arn:aws:states:::lambda:invoke", {"FunctionName": "charge"})
```

- The comment lines right above a statement, with no blank line between, are the `Comment` of the first state the statement makes: the Choice of an `if`, `for` or `while`, the Task of a `task()`. Several lines are joined with line breaks.
- Assignments that share a Pass share their comments too, one after another. A statement that makes no state of its own, such as `try:` or `while True:`, passes its comment to the first state of its body; one that makes none at all, such as `break`, drops it.
- A comment at the end of a line of code stays in the source.
- The docstring of a function run by `parallel()` or a map is the `Comment` of its branch or processor.

## Values and types

Literals are JSON: numbers, strings, `True`, `False`, `None`, lists and dicts with string keys. Tuples, sets and bytes are rejected.

Most operators have one JSONata spelling. A few depend on the type of their operands, and the compiler must know it:

| Needs a type | Why |
|---|---|
| `a + b` | `+` for numbers, `&` for strings, `$append` for lists |
| `len(x)` | `$count` for lists, `$length` for strings, `$count($keys())` for dicts |
| `x in c` | `in` for lists, `$exists` for dicts, `$contains` for strings |
| `c[k]` with a variable key | position or `$lookup` |
| `x[a:b]` | `$substring` for strings, `$filter` by position for lists |
| `list(x)`, `sorted(x)`, `reversed(x)` | `$keys` for dicts, `$split` into characters for strings, the list itself |
| `for x in c` and comprehensions | elements or keys |

One side is enough (`input["name"] + "!"` is a string join), a literal string key needs no type (`"coupon" in input`), and the key's own type is enough for `c[k]`.

Types come from:

- **Annotations** on assignments and parameters: `float` / `int`, `str`, `bool`, `list` / `list[X]`, `dict` / `dict[str, X]`, `None`, and unions such as `str | None`. An annotated variable keeps its type when reassigned with a value of unknown type.
- **Literals and results**: `-` gives a number, comparisons a boolean, `len` a number, `str()` a string, `x[0]` of a `list[float]` a number.
- **AWS responses**: the botocore output shape of an SDK or optimized integration. What external code returns is unknown: a Lambda `Payload`, the `Output` of a `.sync:2` child execution, the result of an activity, the `ResponseBody` of an HTTP Task, and every result of `.sync` and `.waitForTaskToken`.

A value that may have several types must be narrowed first. `isinstance(x, str)`, `x is None` and `x is not None` narrow in `if` / `elif` / `else`, in the right operand of `and` / `or`, in conditional expressions, in comprehension conditions and after a branch that returns.

Annotations are not checked at run time. A wrong one fails the way hand-written JSONata fails, with `States.QueryEvaluationError`.

## Expressions

| Python | JSONata |
|---|---|
| `a - b`, `a * b`, `a / b` | the same |
| `a % b` | `$a - $b * $floor($a / $b)` (the sign follows the divisor, as in Python) |
| `a // b`, `a ** b` | `$floor($a / $b)`, `$power($a, $b)` |
| `a == b`, `a != b`, `a < b` ... | `=`, `!=`, `<` ...; `a < b < c` is `$a < $b and $b < $c` |
| `x is None`, `x is not None` | `$not($exists($x) and $x != null)`, `$exists($x) and $x != null` |
| `a and b`, `a or b` in a condition | `$a and $b`, `$a or $b` |
| `a or b` as a value | `$boolean($a) ? $a : $b` |
| `not x` | `$not($x)` |
| `x if c else y` | `$c ? $x : $y` |
| `if x:` | `$boolean($x)`, or `$count($x) > 0` for a list (tested with `$type` when `x` may be a list) |
| `float(x)`, `int(x)`, `str(x)`, `bool(x)` | `$number($x)`, `$floor($number($x))`, `$string($x)`, `$boolean($x)` |
| `isinstance(x, (str, float))` | `$type($x) in ['string', 'number']` |
| `s.split(sep)`, `s.split()` | `$split($s, $sep)`, `$split($trim($s), ' ')` |
| `s.replace(old, new)`, `s.replace(old, new, count)` | `$replace($s, $old, $new)`, `$replace($s, $old, $new, $count)` |
| `s.lower()`, `s.upper()` | `$lowercase($s)`, `$uppercase($s)` |
| `s.strip()` | `$trim($s)` |
| `sep.join(items)` | `$join($items, $sep)` |
| `list(d)`, `d.keys()` | `[$keys($d)]` |
| `d.values()` | `[$each($d, function($v) { $v })]`, or `$append([], $each(...)[])` when the values may be lists |
| `list(s)` | `$split($s, '')` |
| `d.get("k")`, `d.get(k, default)` | `$exists($d.k) ? $d.k : null`, `$exists($lookup($d, $k)) ? $lookup($d, $k) : $default` |
| `abs(x)`, `round(x)`, `round(x, 2)` | `$abs($x)`, `$round($x)`, `$round($x, 2)` |
| `math.floor(x)`, `math.ceil(x)`, `math.sqrt(x)` | `$floor($x)`, `$ceil($x)`, `$sqrt($x)` |
| `sum(xs)`, `max(xs)`, `min(a, b)` | `$sum($xs)`, `$max($xs)`, `$min([$a, $b])` |
| `sum(xs) / len(xs)` | `$average($xs)` |
| `random.random()` | `$random()` |
| `sorted(xs)`, `sorted(xs, reverse=True)` | `$sort($xs)`, `$reverse($sort($xs))` |
| `list(reversed(xs))`, `xs[::-1]`, `s[::-1]` | `$reverse($xs)`, `$reverse($xs)`, `$join($reverse($split($s, '')), '')` |
| `list(range(n))`, `list(range(0, n, 3))` | `[0..$n - 1]`, `[$range(0, $n - 1, 3)]` |
| `s.startswith("arn:")`, `s.endswith(suffix)` | `$substring($s, 0, 4) = 'arn:'`, `$substring($s, $length($s) - $length($suffix), $length($suffix)) = $suffix` |
| `json.loads(s)` | `$parse($s)` |
| `str(uuid.uuid4())`, `f"{uuid.uuid4()}"` | `$uuid()` |
| `str(datetime.now())`, `f"{datetime.now()}"` | `$now()` |
| `time.time()` | `$millis() / 1000` |
| `s.ljust(5, "0")`, `s.rjust(5, "0")` | `$pad($s, 5, '0')`, `$pad($s, -5, '0')` |
| `list(set(xs))` | `$distinct($xs)` |
| `list(zip(a, b))` | `$zip($a, $b)` |
| `list(itertools.batched(xs, n))` | `[$partition($xs, $n)]` |
| `hashlib.sha256(s.encode()).hexdigest()` | `$hash($s, 'SHA-256')` |
| `x["key"]`, `x[0]`, `s[0]` | `$x.key`, `$x[0]`, `$substring($s, 0, 1)` |
| `s[1:3]`, `s[-3:]`, `s[1:-1]` | `$substring($s, 1, 2)`, `$substring($s, -3, 3)`, `$substring($s, 1, $length($s) - 2)` |
| `xs[1:3]`, `xs[-2:]` | `[$filter($xs, function($v, $i) { $i >= 1 and $i < 3 })]`, `[$filter($xs, function($v, $i) { $i >= $count($xs) - 2 })]` |
| `{**a, "key": v}` | `$merge([$a, {'key': $v}])`, where a later key wins |
| `[f(x) for x in xs if c]` | `[$map($filter($xs, function($x) { c }), function($x) { f })]` |
| `f"order {id}"` | `'order ' & $string($id)` |
| `[a, xs, v]` in an expression | `[$a, [$xs], $type($v) = 'array' ? [[$v]] : $v]`: an item known to be a list, or one that may be, stays one item |

- A comprehension takes one `for` over a list or the keys of a dict. Its result is a list for any number of results: `$map` and `$filter` go in brackets when the items are known not to be lists, and in `$append([], $map(...)[])` when they may be, which keeps a single list as one item. Its variable is the parameter of the JSONata function, so it cannot be named after a variable the comprehension reads through another name, such as the list a `for` loop around it iterates.
- A slice bound written with a minus sign (`xs[-2:]`, `xs[-n:]`) counts back from the end; any other bound is a position from the start. A slice takes no step other than `[::-1]`, and one of a list holding lists keeps them as items, as a comprehension does.
- The string and dict methods need no type: of the JSON types only strings have `split`, `replace`, `lower`, `upper`, `join`, `startswith`, `endswith`, `ljust`, `rjust` and `strip`, and only dicts `keys`, `values` and `get`. `s.split(sep, maxsplit)` is rejected, as `$split` has no counterpart for the rest of the text.
- `sum`, `max` and `min` take numbers, as their JSONata functions do, so a list known to hold anything else is rejected, as are `sum(xs, start)` and keyword arguments such as `key=`. `sum(xs) / len(xs)` is `$average` when both read the same list.
- `sorted` orders numbers or strings, as `$sort` does without a function, so a list known to hold anything else is rejected, and it takes `reverse=` but no `key=`. `range()` outside a `for` is a list; a step, when given, is a nonzero whole number written in the source.
- `set()`, `zip()` and `itertools.batched()` make lists in `list()`, `sorted()` or `reversed()`, and a digest is `hashlib.sha256(s.encode()).hexdigest()`, with `md5`, `sha1`, `sha384` or `sha512` in place of `sha256` for the others. `itertools.batched` needs Python 3.12 where the module is run.
- Functions of `math`, `random`, `time`, `json`, `uuid`, `datetime`, `hashlib` and `itertools` are recognized through the module's imports, such as `import math` or `from datetime import datetime`.
- `context["State"]["EnteredTime"]` has the form `$now()` returns, so the date or the year a state was entered is a slice of it: `context["State"]["EnteredTime"][:10]` and `int(context["State"]["EnteredTime"][:4])`.
- f-strings take no conversions (`!r`, `{x=}`) and no format specs.
- A string literal that starts with `{%` or ends with `%}` is written as a JSONata string, so Step Functions does not read it as an expression.

## Assignments and variables

- `x = value` assigns one name; `a, b = b, a` and `a, b = parallel(f, g)` assign several in one state, with the values from before it.
- `x: float = value` declares a type along with the value.
- `x += v`, `x -= v` and the others are `x = x + v` and so on. A list is the exception: `xs += [...]` extends the list in place in Python, which a JSON value cannot do, so write `xs = xs + [...]`.
- A key or a position cannot be assigned (`d["k"] = v`); build the new dict or list as a literal.

Variable names become JSONata variable names, at most 80 characters long. A variable's name in the generated ASL is the name in the Python, so choose the Python name for what you want to read there. A few names are changed on the way, as Step Functions would not take them or they would hide something: a leading `_` is dropped (`_tmp` is `$tmp`, `_` is `$value`), and `states` and the name of a JSONata function the generated code calls (`count`, `string`, `keys`, `merge`, ...) get `_val` appended (`$count_val`). A changed name the module already uses, or gives to another name, is numbered (`$tmp_2`). A variable used after a branch must be assigned on every path to it.

A `parallel` branch or a Map function cannot assign a name that its enclosing function assigns anywhere; Step Functions keeps those scopes apart. Return the value instead. Variables that sfnx adds for itself (loop counters, caught errors) never clash across scopes.

## Control flow

**`if` / `elif` / `else`** is one Choice, a rule per test, with `Default` for `else` or for what follows.

**`while test:`** is a Choice named `while` that the body leads back to. `while True:` has no Choice and leads back to its first state.

**`for x in xs:`** counts with a variable of its own (`x_index`) and uses `$xs[$x_index]` for `x`, so an iteration adds no Pass state (unless the body assigns `x`, which then becomes a variable). A dict loops over its keys. `range(stop)`, `range(start, stop)` and `range(start, stop, step)` count with the loop variable itself; the step is a nonzero literal. When the body changes what the loop iterates, the loop copies it first. The loop variable is not available after the loop: a range variable would count past its last value, and an empty loop would keep the value from before it in Python.

**`break`** leaves the loop; **`continue`** moves to the next iteration, through the increment of a `for`. Loops take no `else`, and `enumerate` / `zip` are rejected (`for i in range(len(items)):`).

## Tasks

```python
receipt = task(
    "arn:aws:states:::lambda:invoke",
    {"FunctionName": "charge", "Payload": input},
    timeout=30,
    retry=[{"ErrorEquals": [Timeout], "MaxAttempts": 3}],
)
```

- The first argument is the literal resource ARN, the second the `Arguments`. The options are `timeout=` (`TimeoutSeconds`), `heartbeat=` (`HeartbeatSeconds`), `role=` (`Credentials.RoleArn`, not for activities and HTTP Tasks) and `retry=`.
- A statement holds one `task()`, as an assignment (`Assign` gets `$states.result`), a `return` (the Task ends the machine) or a line of its own. It cannot sit in an `if` test, in a comprehension, or where it would run only sometimes (`a and task(...)`, `a < b < task(...)`).
- SDK integrations (`arn:aws:states:::aws-sdk:<service>:<action>`) are checked against botocore: the service, the action, argument names in PascalCase and the required arguments. Service names follow the AWS SDK for Java (`sfn`, `eventbridge`, `cloudwatchlogs`); botocore's names that differ (`logs`) are rejected. Whether Step Functions supports a service or action that botocore has is not checked, nor are the types of argument values; ValidateStateMachineDefinition checks those it can, such as a number written as a Lambda `Payload`.
- Optimized integrations (`arn:aws:states:::<service>:<action>`, with `.sync`, `.sync:2` or `.waitForTaskToken`) are checked for argument names and required arguments when botocore has the action. HTTP Tasks need `ApiEndpoint`, `Method` and a connection. Activity and Lambda function ARNs, and ARNs containing `${...}`, are passed as written.
- Arguments that unpack a dict with `**` are checked only for the argument names written out; the required arguments and what an HTTP Task needs are left to Step Functions when the Task runs.
- A `.waitForTaskToken` Task must pass `context["Task"]["Token"]` in its arguments, the only place it can be read.

## Parallel and maps

```python
order = input["order"]

def email():
    return {"to": order["email"]}

def audit():
    entry = task("arn:aws:states:::aws-sdk:sns:publish", {"Message": order["id"]})
    return entry["MessageId"]

message, receipt = parallel(email, audit)
```

**`parallel(f, g, retry=)`** compiles each function without parameters as a branch where it is called. A function defined in the machine reads the variables around it; one defined at module level reads none of them. The result is the list of branch results, and `a, b = ...` unpacks it. As in Python, a name the function assigns is its own from its first line, not the one around it. A function defined in a branch or a loop can be passed only where every path to that point defines the same one, and a loop cannot define again a function defined before it.

**`inline_map(f, items, max_concurrency=, retry=)`** calls `f(item)` or `f(item, index)` per item. The ItemSelector passes them by parameter name, and the function reads them from `$states.input`; when it calls `task()`, `parallel()` or a map, or assigns a parameter again, its first Pass binds them to variables, since those states replace `$states.input` and the paths that do not assign would read the item.

**`distributed_map(f, items, ...)`** runs each item as a child execution:

| Keyword | ASL |
|---|---|
| `source=` | `ItemReader` (`Resource`, `ReaderConfig`, `Arguments`), instead of `items` |
| `args=` | the values the function reads besides the item; its parameters are the item and exactly these names |
| `batch=` | `ItemBatcher` (`MaxItemsPerBatch`, `MaxInputBytesPerBatch`); the first parameter becomes the list of items and `args` the `BatchInput` |
| `result=` | `ResultWriter` (`Resource`, `Arguments`, `WriterConfig`); the result is then the writer's details |
| `max_concurrency=`, `tolerated_failure_count=`, `tolerated_failure_percentage=` | the fields of the same names |
| `label=` | `Label`, at most 40 characters without spaces or special characters, and not used by another map |
| `execution_type=` | `"STANDARD"` (the default) or `"EXPRESS"` |
| `retry=` | `Retry` |

The function reads its parameters from `$states.context.Execution.Input` and nothing from outside; a variable it would need is reported with the `args=` to add.

## Errors

```python
class Declined(Exception):
    pass


class Lambda:
    class ServiceException(Exception):
        pass


try:
    receipt = task("arn:aws:states:::lambda:invoke", {"FunctionName": "charge"})
except (Declined, Lambda.ServiceException) as e:
    return {"declined": str(e)}
except Exception:
    raise
```

- **Error names** are exception classes. A class derives from `Exception` directly; ASL error names have no hierarchy, so list several with `except (A, B)`. A class nested in another spells a dotted name. An imported class keeps its name from the first capitalized segment (`errors.Lambda.ServiceException` is `Lambda.ServiceException`).
- **sfnx exports** the Step Functions errors a Retry or Catch can name: `Timeout`, `TaskFailed`, `Permissions`, `HeartbeatTimeout`, `DataLimitExceeded`, `ExceedToleratedFailureThreshold`, `ItemReaderFailed`, `ResultWriterFailed`, `QueryEvaluationError`, `HttpSocket`. `except Exception` is `States.ALL`, which does not catch `States.Runtime` and, according to the Step Functions documentation, `States.DataLimitExceeded`.
- **`raise Declined("message")`** is a Fail with that `Error` and `Cause`. A message known not to be a string goes through `$string()`. Python's built-in exceptions and the Step Functions errors cannot be raised, and `from ...` is left out, as a Fail has no chained cause.
- **`try` / `except`** puts a Catch on each Task, Parallel and Map in the body, innermost clauses first. A clause runs with the variables bound before the state that failed. `as e` assigns the error output, `str(e)` and `f"{e}"` read its `Cause`, and a bare `raise` fails again with the caught `Error` and `Cause`. `else:` runs after the body without the Catch.
- Pass, Choice and Wait states cannot catch, so a `try` whose body has no Task, Parallel or Map is rejected, and so is a `raise` in the body that its own `except` would catch, or a bare `raise` in a clause that an `except` around it would catch: a Fail ends the execution. `finally` and a bare `except:` are rejected.
- **`retry=`** takes retriers as dicts: `ErrorEquals` (a list of classes), `IntervalSeconds`, `MaxAttempts`, `BackoffRate`, `MaxDelaySeconds`, `JitterStrategy`, each checked against its range. A retrier for `Exception` comes last.

## JSONata expressions

```python
from sfnx import jsonata

padded: str = jsonata("$pad($s, -$n, '0')", s=code, n=width)
```

- `jsonata(expression, name=value)` writes a JSONata expression as it is, for what has no Python spelling here. It compiles to `($s := $code; $n := $width; $pad($s, -$n, '0'))`: each value is bound to the variable of its name before the expression, as a Python variable does not always keep its name in the definition.
- The expression is a literal string, and its values are given by name. A value named after a JSONata function or `states` would hide it inside the expression, and a value that reads the name of one bound before it would read the new value, so both are rejected. Step Functions checks the expression when it validates the definition.
- What the expression calls is unknown, so it is read once wherever the generated JSONata would otherwise write it twice, as `jsonata(...) % 2` does: it is bound at the start of a block, and a list a `for` iterates is saved before the loop.
- The value has no type; declare one where it matters: `padded: str = jsonata(...)`. At run time in Python, `jsonata()` raises `NotImplementedError`.

## Wait and the Context Object

`wait(10)` is `Seconds` (0 to 99,999,999) and `wait(until="2026-09-13T01:59:00Z")` is `Timestamp` (RFC 3339 with an uppercase `T` and `Z`); either may be an expression.

`context["Execution"]["Id"]` reads `$states.context.Execution.Id`, with types for the documented fields: `Execution` (`Id`, `Input`, `Name`, `RoleArn`, `StartTime`, `RedriveCount`, `RedriveTime`), `State` (`EnteredTime`, `Name`, `RetryCount`), `StateMachine` (`Id`, `Name`) and `Task` (`Token`). Unknown keys are rejected. `Map.Item` is rejected because a map passes the item and index to its function.

## What is rejected

Each of these is rejected with what to write instead:

- **Statements**: `with`, `match`, `global` / `nonlocal`, `del`, `import` and `class` inside a state machine, `async`, `finally`, a bare `except:`, `except*`, `else` on a loop, and a value on a line of its own (`print(x)`).
- **Expressions**: tuples, sets, a slice with a step other than `[::-1]`, methods other than `split`, `replace`, `lower`, `upper`, `join`, `startswith`, `endswith`, `ljust`, `rjust` and `strip` of strings and `keys`, `values` and `get` of dicts, `lambda`, `:=`, `*` unpacking, bitwise operators, unary `+`, format specs and conversions in f-strings, old-style `%` formatting, generators and dict comprehensions, a comprehension with several `for`, built-in functions other than `len`, `float`, `int`, `str`, `bool`, `list`, `isinstance`, `abs`, `round`, `sum`, `max`, `min`, `sorted`, `reversed` and `range`, `set` and `zip` outside `list()`, module functions other than `math.floor`, `math.ceil`, `math.sqrt`, `random.random`, `time.time`, `json.loads`, `itertools.batched` in `list()` and the `hashlib` digests, and `uuid.uuid4()` or `datetime.now()` outside `str()` or an f-string.
- **Calls**: a function of your own called directly (`f()`); it runs as states through `parallel(f)` or a map.

## Where results differ from Python

Some values come out differently from CPython. These are the differences known so far; others may remain:

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `a / b`, `a // b` | `b` is `0` | `"Infinity"`, `"-Infinity"` or `"NaN"`, a string; arithmetic on it fails with `States.QueryEvaluationError`, a comparison does not | `ZeroDivisionError` |
| `a ** b` | a negative `a` and a fractional `b` | `States.QueryEvaluationError` | a complex number |
| `a + b`, `a - b`, `a * b`, `a / b` | a result past the range of a double, such as `1e308 * 10` | `"Infinity"` or `"-Infinity"`, a string | `inf` or `-inf` |
| a number from the input, a variable or `json.loads(s)` | an integer past 2^53, such as `10000000000000000000000001` | the nearest double (`1.0E25`) | the exact integer |
| `json.loads(s)` | `"NaN"`, `"Infinity"`, `"1e400"`, `'{"a": 1, "a": 2}'` | `States.QueryEvaluationError` | `nan`, `inf`, `inf`, `{"a": 2}` |
| `json.loads(s)` | `"{'a': 1}"` | `{"a": 1}` | `JSONDecodeError` |
| `s.split(sep)` | `sep` is `""` | the characters of `s` | `ValueError` |
| `s.split()` | `s` is empty or only whitespace | `[""]` | `[]` |
| `s.strip()` | whitespace inside the text, such as `"a  b"` | each run made one space (`"a b"`) | kept (`"a  b"`) |
| `s.replace(old, new)` | `old` is `""` | `States.QueryEvaluationError` | `new` between every character and at both ends |
| `s.replace(old, new, count)` | a negative `count` | `States.QueryEvaluationError` | every occurrence replaced |
| `sep.join(x)` with `x` of unknown type | a string, such as `"ab"` | `x` itself (`"ab"`) | the characters joined (`"a,b"` for `","`) |
| `{**x}`, `{**x, "k": v}` with `x` of unknown type | `[{"a": 1}, {"b": 2}]` | the list itself, `{"a": 1, "b": 2, "k": ...}` | `TypeError` |
| `a < b` with `a` and `b` of unknown type | `[1]` and `[2]` | `States.QueryEvaluationError` | `True` |
| `str(datetime.now())` | any time | the time in UTC, such as `"2026-09-15T13:43:06.735Z"` | the local time, such as `"2026-09-15 22:43:06.735213"` |
| `time.time()` | any time | seconds to the millisecond, such as `1789479402.245` | seconds to a finer digit, such as `1789479402.8365781` |
| `s.ljust(n, fill)`, `s.rjust(n, fill)` | a `fill` of several characters | the fill repeated as far as it goes | `TypeError` |
| `list(set(xs))` | `[2, 1, 2]`, `[True, 1]`, `[{"a": 1}, {"a": 1}]` | `[2, 1]` in the order first seen, `[true, 1]`, `[{"a": 1}]` | an order of its own, `[True]`, `TypeError` |
| `list(itertools.batched(xs, n))` | `n` is `0` | `[]` | `ValueError` |
| `round(x, digits)` | `2.675` to 2 digits | `2.68` | `2.67` |
| `max(xs)`, `min(xs)` with items of unknown type | strings | `States.QueryEvaluationError` | the greatest or least string |
| `int(x)` | `-1.5` | `-2` (`$floor`) | `-1` |
| `int(x)` | `"1.5"` | `1` | `ValueError` |
| `float(x)` | `"0x10"` | `16` | `ValueError` |
| `float(x)` | `"1e400"` | `States.QueryEvaluationError` | `inf` |
| `str(x)`, `f"{x}"` | `True`, `None`, `1.0` | `"true"`, `"null"`, `"1"` | `"True"`, `"None"`, `"1.0"` |
| `str(x)`, `f"{x}"` | `[1, 2]`, `{"a": 1}` | `"[1,2]"`, `"{\"a\":1}"` | `"[1, 2]"`, `"{'a': 1}"` |
| `bool(x)`, `if x:` with `x` of unknown type | `[0]` | `false` (`$boolean`) | `True` |
| `"k" in x` with `x` of unknown type | `"key"` or `["k"]` | `false` (`$exists($x.k)`, a key lookup) | `True` |
| `s[-1]` | a string ending in a character outside the Basic Multilingual Plane | half of that character (Step Functions counts UTF-16 units) | the character |
| `list(s)` | a string with characters outside the Basic Multilingual Plane | two items for each such character, neither of them the character | one item for each character |
| `s[a:b]`, `s.startswith(p)`, `s.endswith(p)` | a string with characters outside the Basic Multilingual Plane | may hold other characters or half of one, and compare accordingly | the characters between the positions |
| `sorted(xs)` | strings with characters outside the Basic Multilingual Plane | ordered by UTF-16 units (`"😀"` before `"ﬁ"`) | ordered by code points |
| `sorted(xs)` with items of unknown type | booleans or lists | `States.QueryEvaluationError` | a sorted list |
| `s[a:b]` | `a` written with a minus sign and past the start, such as `"hello"[-10:-8]` | counted from the start of `s` (`"he"`) | `""` |
| `xs[a:b]`, or the end of `s[a:b]` | a negative number read from a variable with no minus sign written, such as `i` = -2 | not counted from the end: `xs[i:]` is the whole list, `s[:i]` is `""` | counted from the end |
| `distributed_map(f, ...)` | `f` raises | within `tolerated_failure_count=` or `tolerated_failure_percentage=`, `{"Status": "FAILED", "Error": ..., "Cause": ...}` in the item's place in the list; otherwise `States.ExceedToleratedFailureThreshold`, which an `except` of the raised class does not catch | the exception `f` raised |

Declaring the type of a value that may be a list makes its truthiness follow Python.

## At run time

Importing the module works, and the names sfnx exports behave as plain Python where they can: `state_machine` returns the function, `wait` returns at once, `parallel` and `inline_map` call their functions in turn, and `distributed_map` calls its function with each item, each value of a dict, or with `batch=` each list of up to `MaxItemsPerBatch` items. `task()` and `distributed_map(source=...)` raise `NotImplementedError`, and `context` is an empty dict. Functions get the arguments the definition gives them, but `retry=`, `tolerated_failure_count=`, `tolerated_failure_percentage=`, `result=` and `timeout` take effect only in Step Functions.
