# Design

Why the language in [language.md](language.md) looks the way it does, and the Step Functions and JSONata behavior the compiler relies on.

## Principles

1. **The product is the ASL.** sfnx is for people who find ASL tedious or hard to write. It stops at the definition: running, mocking and deploying belong to other tools.
2. **The Python should run, but the contract is with the ASL.** The output is what someone writing ASL by hand would write for the intent of the source, not a reproduction of Python: `str(True)` is `"true"`, while a comprehension is a list for any number of results, as its author means. Known differences from CPython are listed in [Where results differ from Python](language.md#where-results-differ-from-python); others may remain.
3. **No library of functions.** The names sfnx exports make states (`state_machine`, `task`, `wait`, `parallel`, `inline_map`, `distributed_map`) or name what ASL names (`context`, error classes, and `jsonata` for an expression written out). ASL settings such as `Arguments`, `ItemReader` and retriers are passed through as the structures they are, with no builders.
4. **As few control structures, operators and built-ins as the flows need.**

A construct is accepted when ASL or JSONata has a counterpart for it and its meaning can be written as a JSONata expression. The meaning is mapped, not the operator: JSONata has conditionals and arithmetic, so `a or b` is `$boolean($a) ? $a : $b` and `a % b` is `$a - $b * $floor($a / $b)`.

## Types

**Why types at all.** Someone writing JSONata by hand knows the type of each value and picks the spelling from it: `$a + $b` for numbers, `$a & $b` for strings, `$append($a, $b)` for arrays. Python's `a + b` can be any of the three, and the compiler never sees a value; the input arrives with `StartExecution`. So the source states what the hand-writer already knows, where it matters.

**Only where an operation depends on it.** `+`, `len`, `in`, a variable key and `for` need a type. `-`, `*`, `/`, comparisons, `and` / `or` / `not` and literal keys have one spelling and need none. One known side is enough, since Python's `+` requires both sides to agree (`input["name"] + "!"` is a string join). Only the leaf that is operated on needs an annotation (`total: float = input["order"]["total"]`).

**An annotation is a claim.** It generates nothing and is not checked at run time: Step Functions has no input schema, and a wrong claim fails with `States.QueryEvaluationError`, as hand-written JSONata would. `float(x)` is the conversion. There is no `Any` or `object`; a value passed through untouched needs no type.

**Narrowing follows type checkers.** `isinstance`, `is None` and `is not None` narrow in branches, in the right side of `and` / `or`, in conditional expressions and after a branch that returns. A declaration belongs to the name, as in PEP 526, so after branches it holds as the types of the branches that declare it. A test trusts the run time over a declaration: `isinstance(x, list)` on a value declared `str` types the branch as a list.

**Task results.** botocore's output shapes type SDK and optimized integration results (structures with PascalCase keys, lists, maps, strings, numbers and booleans; blobs and timestamps are unknown). What external code returns is unknown: a Lambda `Payload`, a `.sync:2` child's `Output`, an activity's result, an HTTP `ResponseBody`, and every result of `.sync` and `.waitForTaskToken`.

## Decisions

**A decorator marks the machine.** A rule such as "every top-level function is a machine" is shorter, but invisible: a shared helper would silently become a machine. The decorator returns the function, so the module still imports. It takes only `timeout`, the one top-level ASL field a user decides; the workflow type, logging, tracing and the role belong to the deployment.

**The parameter reads `$states.context.Execution.Input`.** `$states.input` is the input of the current state and becomes the result after a Task, Parallel or Map, so spelling the parameter by where it is read would make one `input["amount"]` look two ways. Assigning to the parameter is rejected: after a branch it would be unclear which of the two it names. The name `input` in examples follows `StartExecution` and `$states.input`.

**State names come from what a state does.** The variable, `return`, `if`, the action of a Task. Line numbers would rename every state when a line is added above, breaking mocks keyed by state name and turning every change of the generated ASL into a change of every line. Serials count across the whole definition, since Step Functions wants names unique there, and states in a branch or a Map carry the name of the function that holds them, which `Branches` has nowhere else to keep.

**States split only where ASL requires it.** `Assign` evaluates every expression with the values from before the state, so an assignment that reads a pending one needs a new state; a Task is one state per call; a Choice is one state. Everything else shares a Pass, a Task's result goes in its own `Assign`, and a machine that returns a Task's result ends on it.

**A name outside the machine is written in.** ASL has no name for a value: a Retry is repeated state by state, and every Task writes its own Arguments. Python has the name, so a name assigned at the top level of the module is read as the value written there and put in at each place it is read, and the definition reads as it would if the value had been written out. The compiler reads the module without running it, so such a name holds data and exception classes; `NOW = time.time()` is rejected where it is read, because writing it in would evaluate it state by state instead of once at import. A machine that assigns the name has a variable of its own, as Python does, and a variable inside the machine is what writes a value into the definition once.

**A name ASL cannot use as a variable is renamed.** Step Functions variable names do not start with `_`, and `$states` is reserved. A variable also hides the JSONata function of its name, and a definition that calls the function then fails only when it runs. Someone writing ASL by hand would pick another name, so sfnx does the same instead of rejecting the Python name: the leading `_` is dropped (`$tmp`), and `states` and a name the generated expressions call as a function get `_val` (`$count_val`). The new names are chosen for the whole module at once, numbered past every name it uses and every name already given, and state names keep the Python name. Only those names change, so the rest of the definition reads as written.

**`jsonata()` covers what Python does not.** The functions and methods with a Python spelling are those a workflow reaches for often; the rest of JSONata stays reachable through one name instead of a Python spelling each. The values go in by name and are bound at the start of a block, because a Python variable does not always keep its name in the definition (`count` is `$count_val`, a loop's item is `$items[$item_index]`), and the text of the expression stays as it is written.

**Dividing by zero is tested where Python would raise.** JSONata has no error for it: `$a / 0` gives the string `"Infinity"`, which fails only in a later state that does arithmetic on it, and compares as a string without failing, so a Choice takes a branch on a value Python would never have produced (measured). A divisor that is not written as a number is therefore tested first, `$b = 0 ? $error('division by zero') : $a / $b`, so the execution fails where it divides, with `States.QueryEvaluationError` and `division by zero` as the cause. A divisor written as a number needs no test, so `$a / 1000` stays as it is, and one written as zero is rejected when the file is compiled. `$error` is a function the generated expressions call, so a variable of that name is renamed as the other function names are.

**A value that changes on evaluation is evaluated once.** Some Python operations have no single JSONata spelling and write an operand twice: `a % b` is `$a - $b * $floor($a / $b)`, `a < b < c` is `$a < $b and $b < $c`, `a or b` as a value is `$boolean($a) ? $a : $b`, `a is not None` is `$exists($a) and $a != null`, and `d.get(k, x)` tests the value before reading it. For a variable or a field that is what a hand-writer does. For `$random()`, `$uuid()`, `$now()` and `$millis()`, and an expression built on one, the two readings would differ, so such an operand is bound first in a block, `($v := $random(); $v - 3 * $floor($v / 3))`, under a name that hides nothing the block reads. A `for` over such a list, or a `range()` to such a stop, copies it before the loop for the same reason, as does `a, b = ...`, where every name reads the value again. An expression written in `jsonata()` is not parsed, so it always counts as changing: a call can be written `$random ()`, or made under a name the expression binds the function to, and no search of the text finds either. One that holds still is bound too, and a `for` over one copies it before the loop, as an unpacked one does, so `jsonata()` in these places changes the generated ASL whether or not the expression reads anything volatile. The name bound avoids every `$name` the code holds, since an expression written by hand names variables the program never declared.

**Comments come from the source.** `Comment` is the only documentation ASL keeps, and a docstring or a comment above a line is where a Python author already writes it. Taking what is there needs no new syntax, and a comment that should stay out of the definition goes where it is not taken, after the code or above a blank line.

**Control flow is statements, not functions.** One ASL scope is a flat graph with jumps; `if`, `for` and `while` can produce any of it, while functions and closures only produce trees.

**One `task()` for every integration.** A Task varies only in its `Resource` and `Arguments`, so there is no `aws.Service.api` library, which would contradict the third principle, and no separate `http` or `activity` names. The ARN still lets the compiler look up the botocore model. `Arguments` is the second positional argument and the state's own settings are keywords: AWS API parameter names collide with keywords (5,229 lowercase parameter names across 431 services, `name` in 679 places, `timeout` in 3), and a positional argument also takes keys that are not identifiers and non-object arguments.

**A statement holds one state-making call, where it always runs.** Not in a condition, a comprehension, a branch of a conditional expression, the right side of `and` / `or` or a later operand of a chained comparison, since the Task would run whether or not that part is taken.

**Branches and map functions are nested functions.** An ASL branch reads the variables around it and cannot write them, and has its own locals and `return`; a Python nested function matches on all four. Step Functions rejects an inner scope that assigns a name its outer scope assigns, so that Python form is diagnosed. As in Python, a name the function assigns is local to it from its first line. `Map.Item` is readable only in the `ItemSelector`, so an inline map passes the item by parameter name and binds it in the first Pass when a later state would replace `$states.input` or the function assigns the parameter again. A distributed map runs child executions that read nothing outside, so values reach them through `args=`, which the `ItemSelector` or `BatchInput` evaluates in the parent.

**`try` puts a Catch on each state that can fail.** Wrapping the body in a one-branch Parallel would hide the variables assigned before the failure and need detours for `break`, `continue` and `return`. Only Task, Parallel and Map can catch, so a `try` with none of them is rejected. A `raise` is a Fail, which no Catch catches, so a `raise` that Python would catch in an enclosing `except` is rejected rather than compiled to something else.

**Errors are classes, with no hierarchy.** `except` takes classes, so retriers take the same classes. ASL error names are flat, so a class derives from `Exception` directly and `except (A, B)` lists several. A nested class spells a dotted name (`Lambda.ServiceException`), which Python class names cannot hold otherwise. A segment that starts with a digit, which a Python class name cannot, is written with a leading `_` that is dropped, so `_416` spells `416`; a `_` before a name that stands on its own stays, since ASL takes `_Internal` as it is. Built-in exceptions have no ASL name and are rejected. `Exception` stands for `States.ALL` in an `except` and a retrier, and cannot be raised, as it names no single error.

**Retries are per state.** A retrier is a JSON object, written as a dict. There is no machine-wide default, and no new syntax for retrying several steps: `parallel(f, retry=[...])` produces the one-branch Parallel ASL offers for it.

**A `for` loop counts.** It keeps an index and reads `$items[$item_index]` instead of a Pass per iteration, since in a Standard workflow every state transition is billed and counts toward the history limit. When the body changes what the loop iterates, the loop copies it first, as Python iterates the original. A loop is compiled again with wider types until the types at its head hold for every way back to it; a type that grows on every way back (`x = [x]`) becomes unknown after a few attempts. The loop variable ends with the loop, as a counter would otherwise leave a value Python does not.

**Comprehensions are `$map` and `$filter`, kept as lists.** Without brackets one result would be a value and no result undefined. Brackets would merge a single result that is a list, so when the items may be lists the result is `$append([], $map(...)[])`: `[]` keeps a single result as an array, and appending it to an empty array turns no result into `[]`. A `$filter` whose items may be lists gets `[]` too before `$map` iterates it.

**Lists keep their shape.** JSONata's array constructor merges the items of an array value unless the item is itself written as an array constructor, so `[xs]` of a list would lose a level. An item known to be a list is wrapped once more (`[[$xs]]`), and one that may be a list is tested when it runs. `xs += [...]` is rejected because Python extends the list in place, which other names for it see, and a JSON value is a copy.

**The Context Object is read by subscript**, like the input, with the keys ASL uses. The fields readable only in some places are diagnosed: `Task.Token` outside a `.waitForTaskToken` Task's arguments, and `Map.Item` everywhere.

## What the compiler relies on

From the Step Functions and JSONata documentation, from [jsonata-python](https://github.com/rayokota/jsonata-python), and from Step Functions itself where noted (TestState, ValidateStateMachineDefinition and executions, measured on 2026-09-13, 2026-09-14 and 2026-09-18).

### JSONata in Step Functions

- Step Functions implements JSONata 2.0.6 without `$eval`, and adds `$partition`, `$range`, `$hash`, `$random`, `$uuid` and `$parse`. An expression has a one-second limit and a memory limit; every failure is `States.QueryEvaluationError`.
- An expression that returns undefined fails, in any field and inside objects, arrays and `Assign` (measured).
- `Assign` evaluates all its expressions with the values from before the state, then assigns. `Assign` and `Output` of a state are evaluated in parallel.
- Binding a variable hides the built-in function of the same name (`count` hides `$count`), and ValidateStateMachineDefinition does not report it: calling the function fails when it runs (`T1006: Attempted to invoke a non-function`; measured).
- `and`, `or` and `$not` convert operands as `$boolean` does and return booleans. `$boolean` matches Python's `bool()` except on a non-empty array whose items are all falsy (`[0]`, `[[]]`), which is false (measured).
- With undefined, both `=` and `!=` are false.
- `$count` counts a non-array as one item, and null or a missing value as zero.
- `$s[0]` of a string returns the whole string; `$substring` and `$length` work on strings. A fractional position is truncated (`$x[1.5]` is `$x[1]`).
- `$toMillis` reads the ISO 8601 form `$now()` returns: it counts a timestamp with no UTC offset from UTC and keeps whole milliseconds, while a space in place of the `T`, a form without the dashes and a week date fail. `$fromMillis` returns the form `$now()` does, and `$toMillis($now())` is `$millis()`, so the two spellings of the current moment agree (measured).
- A position right after a variable position has no effect (`$x[$i][0]` returns `$x[$i]`; `($x[$i])[0]` works; measured).
- `&` joins non-strings as strings (`'a' & 1` is `'a1'`). A string literal has no `\'` escape.
- An array constructor merges the items of an array value that is not itself a constructor: `$count([$xs])` of `[1, 2]` is 2, and `[[$xs]]` and `$type($x) = 'array' ? [[$x]] : $x` keep one item for arrays, scalars, objects, one-item arrays, nested arrays and null (measured).
- `$map` and `$filter` return a value for one result and undefined for none; in brackets they are `[2]` and `[]`, but a single array result loses a level. `$map(...)[]` keeps a single result as an array (but not in parentheses, `($map(...))[]`), and `$append([], $map(...)[])` is `[]` for none; for 0, 1 and several items, lists and nested lists, with and without `$filter`, CPython, jsonata-python and Step Functions agreed (measured).
- `a % b` as `$a - $b * $floor($a / $b)` matches Python for negative operands; `$power` matches Python's `**` except for a negative base with a fractional exponent, where JSONata fails (measured).
- Dividing by zero does not fail: `1 / 0`, `-1 / 0` and `0 / 0` are the strings `"Infinity"`, `"-Infinity"` and `"NaN"`, and so is `$floor` of them. Arithmetic on them fails, but a comparison does not (`1 / 0 > 5` is true; measured).
- Numbers are doubles. A literal number in the definition keeps its digits, but read in an expression, from a variable or the input, an integer past 2^53 is rounded (`10000000000000000000000001` is `1.0E25`; measured). `+`, `-`, `*` and `/` past the range of a double give `"Infinity"` or `"-Infinity"` as division by zero does, and `$number` of a string past it fails (`"1e400"`; measured).
- `? :` binds looser than `and` and `or`.
- `$merge` of an array of objects gives a later object's key precedence over an earlier one's, null values included. An array among the items is merged as its objects, as the array constructor merges it (measured).
- `$parse` fails on text JSON does not allow (`NaN`, `Infinity`), on a number past the range of a double (`1e400`) and on a repeated key, but reads single-quoted strings (`{'a': 1}`; measured).
- `$uuid()` returns a new lowercase version 4 UUID on every call (measured).
- `$now()` returns the time in UTC to the millisecond with a `Z` (`"2026-09-15T13:36:42.245Z"`), `$now('[Y0001]-[M01]-[D01]')` formats it with a picture string, and `$millis()` returns the milliseconds since 1970 (measured). Every `$now()` and `$millis()` in one evaluation of an expression returns the same time.
- `$pad` fills on the right for a positive width and on the left for a negative one, repeating a fill of several characters and taking a width of 6.5 as 6 (measured).
- `$distinct` keeps the first of each value in order, compares objects and arrays by value, and keeps `true` apart from `1`; `$zip` stops at the shortest array (measured).
- `$hash` gives the lowercase hex digest of the UTF-8 text for `MD5`, `SHA-1`, `SHA-256`, `SHA-384` and `SHA-512`; `$partition` returns nothing for an empty array and for a size of 0, makes batches of one item for a size of 1.5, and fails below 0 (measured).
- `$split` with a string separator splits at that exact text (`.` and `*` are not patterns), keeps empty parts, and splits into characters at `''` (measured).
- `$trim` turns every run of whitespace into one space and removes it from both ends: `$split($trim('  a  b\t\nc '), ' ')` is `["a", "b", "c"]`, and `[""]` for blank text (measured).
- `$replace` with a string pattern takes `$0` in the replacement literally, fails on an empty pattern and on a negative limit, replaces nothing with a limit of 0, and takes a limit of 2.5 as 2 (measured).
- `$replace` also takes a regular expression written between slashes, and Step Functions reads `\s` in one as the ASCII whitespace: `$replace($s, /^\s+|\s+$/, '')` leaves a non-breaking space or an ideographic space at an end, which jsonata-python and Python's `strip()` remove (measured).
- `$join` of `[]` is `""`, of a string is that string, and of an array holding a non-string fails (measured).
- `$lowercase` and `$uppercase` changed `İ`, `ẞ`, `Σ`, `ß`, `ǆ` and `ﬁ` as Python's `lower()` and `upper()` do (measured).
- `$substring` counts a negative start in UTF-16 units in Step Functions but in code points in jsonata-python (`$substring('héllo😀', -1, 1)`); `$length` counts code points in both (measured). On text with characters outside the Basic Multilingual Plane, Step Functions also returned other characters or half of one for some positive positions and lengths (`$substring('a😀b', 1, 1)`), and without a length it failed at 30 of 35 starts, where with a length it failed at none (measured).
- `$substring` with a negative start past the beginning starts at the beginning and takes the length from there (`$substring('hello', -10, 2)` is `"he"`); a negative length gives `""` (measured).
- `$round` rounds half to even at the decimal digits of the number (`$round(2.5)` is `2`, `$round(2.675, 2)` is `2.68`), and a negative number of digits rounds to tens and beyond (`$round(25, -1)` is `20`). `$ceil(-1.5)` is `-1` and `$floor(-1.5)` is `-2`, and `$sqrt` fails on a negative number (measured).
- `$sum([])` is `0`; `$max([])`, `$min([])` and `$average([])` return nothing. `$sum` and `$max` failed on an array holding a string and took a number outside an array as a one-item array (measured).
- `$lookup` of a missing key returns nothing, and Step Functions rejects `??` and `?:` when it validates a definition; `$exists($d.k) ? $d.k : null` gives null for a missing key and the value, null included, for a present one (measured).
- `$keys` returns the one key of an object as itself and nothing for an empty object; `$each` does the same with the results of its function, and returns a single result that is an array as that array (measured).
- `$split($s, '')` splits a character outside the Basic Multilingual Plane into two items, neither of them the character (measured).
- `$sort` without a function orders an array of numbers or of strings and fails on booleans, arrays and mixed items; it orders strings by UTF-16 units. `$reverse` and `$sort` return an array for any number of items (measured).
- `$sort` with a function of two parameters orders by it, and in Step Functions it leaves items the function calls equal in the order they came, as Python's `sorted` does; jsonata-python reorders them, so a test cannot tell the two apart. `$max` and `$min` take no function of their own, and `$sort(...)[-1]` and `$sort(...)[0]` are what read the greatest and the least (measured).
- `[a..b]` is an array for any number of items, empty when `b` is less than `a`. `$range(a, b, step)` includes `b` when a step reaches it and returns one number as itself and none as nothing (measured).
- `$xs[[a..b]]` returns the item itself for a range of one position and undefined for an empty range, while `$filter` passes each item's position as the second parameter of its function (measured).
- A block binds variables for the expressions after them: `($s := $states.input.code; $pad($s, -5, "0"))` works in Step Functions (measured).
- `Comment` is accepted at the top level, on every state type, and on Choice rules, retriers, catchers, Parallel branches and Map processors (measured).
- Variable names are Unicode identifiers (ID_Start, then ID_Continue), at most 80 characters; `$states` is reserved. Non-ASCII names work (measured).
- A string is evaluated when it starts with `{%` and ends with `%}`, including strings inside objects and arrays; a half-open one fails validation.

### States

- State names must be unique across the whole definition, branches and Map processors included (`DUPLICATE_STATE_NAME`; measured). A distributed Map `Label` must be unique too (`DUPLICATE_LABEL_NAME`; measured).
- Only Parallel and Map create scopes. An inner scope can read outer variables but cannot assign a name the outer scope assigns (`DUPLICATE_VARIABLE_NAME`; measured). A Catch's `Assign` writes to the outer scope. A distributed Map reads no outer variables.
- Choice evaluates its rules in order and fails if none holds and there is no `Default`. A Choice has no `End`, and its state-level `Assign` runs only when no rule matched.
- Only Task, Parallel and Map have `Retry` and `Catch`. A Fail has `Error` and `Cause`, which may be expressions, and no `Assign`.
- `Output` takes any JSON value, null included.
- JSONata states have no `InputPath`, `Parameters`, `ResultSelector`, `ResultPath`, `OutputPath` or `*Path` fields (`SecondsPath`, `ItemsPath`), and no intrinsic functions; `Arguments`, `Output` and expressions take their place.
- Wait `Seconds` is a whole number from 0 to 99,999,999; `Timestamp` is RFC 3339 with an uppercase `T` and `Z`.
- An inline Map's `Items` is an array; a distributed Map's may be an object. A distributed Map needs `ProcessorConfig.ExecutionType`; its `Label` is at most 40 characters without whitespace, wildcards, brackets or special characters. With `ItemBatcher`, a child's input is `{"Items": [...], "BatchInput": {...}}`.
- For an object of items, `Map.Item.Key` is an entry's key and `Map.Item.Value` its value; without an `ItemSelector`, a child's input is `{"Key": ..., "Value": ...}` (measured).
- With a `ResultWriter`, whose `Arguments` need `Prefix`, a distributed Map's result is `{"MapRunArn": ..., "ResultWriterDetails": {"Bucket": ..., "Key": "<Prefix>/<run>/manifest.json"}}`, with or without `WriterConfig` (measured).
- A failed child execution of a distributed Map fails the Map with `States.ExceedToleratedFailureThreshold`, not with the child's error, unless the failed items are within `ToleratedFailureCount` (at most the count) or `ToleratedFailurePercentage` (at most that share of the items). A tolerated child takes its place in the result as `{"Status": "FAILED", "Error": ..., "Cause": ...}` (measured). With both thresholds, exceeding either fails the Map, as documented, but a threshold of 0 counts as not set: a count of 0 with a percentage that held did not fail (measured). With `ItemBatcher`, a failed batch counts each of its items and takes one place in the result (measured).
- The Context Object has `Execution` (`Id`, `Input`, `Name`, `RoleArn`, `StartTime`, `RedriveCount`, `RedriveTime`), `State` (`EnteredTime`, `Name`, `RetryCount`), `StateMachine` (`Id`, `Name`) and `Task` (`Token`). `RedriveCount` is 0 in an execution never redriven, which has no `RedriveTime` (measured). `State.RetryCount` exists in a Task and a Map, not in a Pass or a Succeed; it is the number of retries before the current attempt, and in a Catch's `Assign` the number of retries made (measured). A child execution of a distributed Map has its own `Execution.Id` and `Name`, and its `StateMachine.Id` ends in `/<map run>` (measured). `Map.Item` is readable only in the `ItemSelector`.
- A Standard execution's history holds 25,000 events, and every state transition is billed. Express executions are billed by their number, duration and memory instead.

### Errors

- Error names starting with `States.` are reserved. `States.TaskFailed` matches every error except `States.Timeout`. `States.ALL` stands alone in the last retrier or catcher.
- `States.Runtime` cannot be retried or caught.
- A retrier's attempts add up over all the errors it matched in the state. Only the first retrier that matches an error counts it: once that one has no attempts left, the error is not retried even if a later retrier matches (TestState; measured). A failure in evaluating `Arguments` or `Output` is retried as a failure of the Task (TestState; measured).
- The documentation says `States.ALL` does not catch `States.DataLimitExceeded`, but one raised by an SDK integration result over the quota (`ec2:describeImages` with `Owners: ["amazon"]`) was caught by both `States.ALL` and `States.TaskFailed`, in TestState and in Standard and Express executions (measured). Other causes were not measured.
- A Lambda function's error name is its exception type, and its `Cause` is JSON written by the runtime (measured).

### Tasks

- An SDK integration's resource is `arn:aws:states:::aws-sdk:<service>:<action>`, where the service follows the AWS SDK for Java (`sfn`, `eventbridge`, `cloudwatchlogs`) and the action is camelCase. Its arguments are PascalCase even for camelCase APIs, and so is its result, nested fields included (`logGroups[].arn` is `LogGroups[].Arn`); timestamps arrive as strings (measured).
- An SDK integration's error name is `<ServiceName>.<ErrorName>`.
- ValidateStateMachineDefinition rejects an SDK integration without `Arguments` (`{}` is needed), a resource that still contains `${...}`, and unknown keys and missing idempotency tokens of SDK integrations (measured). `Arguments` written as one expression, such as `$merge`, passes for SDK integrations and HTTP Tasks (measured). For the 16 optimized actions whose botocore model the compiler checks against, it rejected an unknown key, rejected some keys botocore has, and required some that botocore makes optional (measured).
- ValidateStateMachineDefinition checks the types of literal argument values: a Lambda `Payload` takes a string, object, array or null, and rejects a number or boolean literal. Expressions pass (measured).
- `TimeoutSeconds` is a whole number from 1, `HeartbeatSeconds` a smaller one. `Credentials` applies only to Lambda functions and AWS service integrations. An HTTP Task needs `ApiEndpoint`, `Method` and a connection, and stops after 60 seconds.
- botocore has no service model whose service ID disagrees with the AWS SDK for Java name; loading every model takes about four seconds.

### Programs as a whole

Programs from the generator in `tests/test_differential.py` were run in Step Functions and compared with CPython (errors by name): 40 without tasks as Express executions, and 40 with Lambda tasks, 10 of them with distributed maps as Standard executions, each with three inputs. All 240 runs agreed (measured).
