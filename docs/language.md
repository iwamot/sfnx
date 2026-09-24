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
- A function that ends without `return` returns `null`, as a `return` without a value does: both are `return None`, so a Task, a Parallel or a Map right before them ends the machine or the branch itself.
- A file may hold several machines; the compiler reads it without importing it.
- The docstring of the function is the `Comment` of the definition.

## Names of states

A state is named after what it does: the variable it assigns, `return`, `raise`, `if`, `for`, `while`, `wait`, `map`, `parallel`, or the action of a Task on its own line (`getItem`, `invoke`). Repeated names get serials (`amount`, `amount_2`). Paths that end the same way, with the same value returned or the same error raised with the same message, share one Succeed or Fail, as a hand-writer ends them at one state. States inside a Parallel branch or a Map are prefixed with the function that holds them (`email.return`).

States split only where ASL requires it: a Task, a Choice, an assignment of a name assigned just before it, and one that reads a value that changes on evaluation (the time, a random value, `jsonata()`) assigned just before it. Other assignments share one Pass, one reading what another assigns reading that assignment's expression in its place, or go in the `Assign` of what is the only way to them: a Wait right before them, the Choice rule of the `if` branch or the `while` or `for` body they start, or the Choice's own `Assign`, which only its `Default` applies, for `else`, for what follows an `if` whose branches all end (in `return`, `raise`, `break` or `continue`), and for what follows a loop left without `break`. Where paths join, or where the one path there comes from a Choice rule, a Choice's `Default` or a catcher (after a function called directly that returns from a loop, or after an `except` clause), the assignments that follow go in the `Assign` of the last state or Choice rule on each path instead, when every one of them can take them: a Pass, a Wait, a Choice rule or a catcher, a Choice for its `Default`, or a Task, a Parallel or a Map as above, whose `Assign` does not assign a name they read or assign. At the start of an `except` clause, the catchers assign the error, and the assignments read it as the error output they assign, `$states.errorOutput`, so they go in the catchers' `Assign` too. A Task's result goes in the Task's `Assign`, and a machine that returns a Task's result ends on that Task. Assignments right after a Task, a Parallel or a Map go in that state's `Assign`, reading the result it assigned as `$states.result`, and a `return` right after them, or right after the state, is its `Output`, so the state ends the machine or the branch. They keep a Pass or a Succeed of their own inside a `try`, after a state that retries on `Exception` or `QueryEvaluationError`, when one of them reads the context's `State` or a `jsonata()` expression, or the time or a random value after a Parallel or a Map, and when an assignment unpacks (`a, b = ...`). A `return` of a value written in the source, `None` included, cannot fail, so it is the `Output` even inside a `try` or after such a retrier. The time and a random value go in the `Assign` of a Task or a Wait, which Step Functions evaluates when the state ends, where Python reads them after it. The assignments that start the machine, a branch or a map's function, where nothing before them can take them, go in the first state when it is a Choice, whose rules and own `Assign` hold them on every path, or a Task that could take the assignments after it: the state reads each as its expression, and its `Assign` assigns it.

## Comments

```python
# Charge the card once the stock is reserved.
receipt = aws.optimized.lambda_.invoke(FunctionName="charge")
```

- The comment lines right above a statement, with no blank line between, are the `Comment` of the first state the statement makes: the Choice of an `if`, `for` or `while`, the Task of a Task call. Several lines are joined with line breaks.
- Assignments that share a Pass share their comments too, one after another. A statement that makes no state of its own, such as `try:` or `while True:`, passes its comment to the first state of its body, and a call of a function called directly to the first state of the function; one that makes none at all, such as `break`, drops it. A docstring or a string on a line of its own passes its comment on to the statement after it, so a function called directly that has a docstring takes the comment of its call as one without.
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
| `for x in c`, comprehensions, `any(c)`, `all(c)` | elements or keys |

One side is enough (`input["name"] + "!"` is a string join), a literal string key needs no type (`"coupon" in input`), and the key's own type is enough for `c[k]`.

Types come from:

- **Annotations** on assignments and parameters: `float` / `int`, `str`, `bool`, `list` / `list[X]`, `dict` / `dict[str, X]`, `None`, unions such as `str | None`, and the TypedDict classes of the module. An annotated variable keeps its type when reassigned with a value of unknown type. An annotation without a value, `images: list`, gives its type to each later assignment of the name, those of `a, b = ...` included, which takes no annotation of its own.
- **Literals and results**: `-` gives a number, comparisons a boolean, `len` a number, `str()` a string, `x[0]` of a `list[float]` a number.
- **AWS responses**: the botocore output shape of an SDK or optimized integration. What external code returns is unknown: a Lambda `Payload`, the `Output` of a `.sync:2` child execution, the result of an activity, the `ResponseBody` of an HTTP Task, and every result of `.sync` and `.waitForTaskToken`.

A TypedDict declares the fields of an object once, so the values under its keys need no annotation of their own, and the same class types the module for a type checker:

```python
from typing import NotRequired, TypedDict


class Item(TypedDict):
    sku: str
    quantity: int


class Order(TypedDict):
    id: str
    items: list[Item]
    coupon: NotRequired[str]


@state_machine
def fulfill(input: Order):
    for item in input["items"]:  # item is an Item, item["quantity"] a number
        ...
```

A Lambda `Payload` is typed the same way, `receipt: Receipt = task(...)["Payload"]`, and so is the whole result of a Task whose type is unknown, such as a `.waitForTaskToken` or activity result: `decision: Decision = task(...)`. The class derives from `TypedDict` directly (`typing` or `typing_extensions`) and declares every field on itself, one per line; it may name the classes written above it, in `list[Item]`, `dict[str, Item]` or a union. `NotRequired[T]` and `total=False` say a key may be left out, not that its value may be `None`: read such a key with `"coupon" in input` or `input.get("coupon")`, whose result is `str | None`, and `input["coupon"]` fails at run time when the key is missing, as any missing key does. A key the class does not declare has an unknown type. A recursive TypedDict, one that inherits from another, and the form `TypedDict("Order", {...})` are rejected.

A value that may have several types must be narrowed first. `isinstance(x, str)`, `x is None` and `x is not None` narrow in `if` / `elif` / `else`, in the right operand of `and` / `or`, in conditional expressions, in comprehension conditions and after a branch that returns. `x or default` never gives null, which is falsy, so `xs or []` of an `xs` that may be null is a list.

Annotations are not checked at run time. A wrong one fails the way hand-written JSONata fails, with `States.QueryEvaluationError`.

## Expressions

| Python | JSONata |
|---|---|
| `a - b`, `a * b`, `a / b` | the same |
| `a % b` | `$a - $b * $floor($a / $b)` (the sign follows the divisor, as in Python) |
| `a // b`, `a ** b` | `$floor($a / $b)`, `$power($a, $b)` |
| `a / b` where `b` is not written as a number | `$b = 0 ? $error('division by zero') : $a / $b`, as dividing by zero raises in Python (`//` and `%` are tested the same way) |
| `a == b`, `a != b`, `a < b` ... | `=`, `!=`, `<` ...; `a < b < c` is `$a < $b and $b < $c` |
| `x is None`, `x is not None` | `$not($exists($x) and $x != null)`, `$exists($x) and $x != null` |
| `x is True`, `x is not False` ... | `$x = true`, `$not($x = false)`: `=` compares a boolean only with a boolean, as `is` does (`0 = false` is false), and a missing `x` is not `False` |
| `if x:`, `bool(x)` | the truth of a value: `$count($x) > 0` for a list, `$boolean($x)` where it cannot be one, and `$type($x) = 'array' ? $count($x) > 0 : $boolean($x)` where the type is unknown, since `$boolean` reads `[0]` as false where Python reads it as true |
| `a and b`, `a or b` in a condition | `$a and $b`, `$a or $b`, each operand read for its truth |
| `a or b` as a value | the truth of `a`, then `a` or `b`: `$boolean($a) ? $a : $b` for a value that cannot be a list |
| `not x` | `$count($x) = 0` for a list, and `$not($x)` otherwise, with `x` read for its truth |
| `x if c else y` | `$c ? $x : $y` |
| `float(x)`, `int(x)`, `str(x)` | `$number($x)`, `($v := $number($x); $v < 0 ? $ceil($v) : $floor($v))` (towards zero, as Python truncates), `$string($x)` |
| `isinstance(x, (str, float))` | `$type($x) in ['string', 'number']` |
| `s.split(sep)`, `s.split()` | `$split($s, $sep)`, `$trim($s) = '' ? [] : $split($trim($s), ' ')` |
| `s.replace(old, new)`, `s.replace(old, new, count)` | `$replace($s, $old, $new)`, `$replace($s, $old, $new, $count)` |
| `s.lower()`, `s.upper()` | `$lowercase($s)`, `$uppercase($s)` |
| `s.strip()` | `$replace($s, /^\s+\|\s+$/, '')` |
| `sep.join(items)` | `$join($items, $sep)`, with a string split into its characters first, as Python joins those, and a value that may be one tested for it when it is evaluated |
| `sorted(xs, key=lambda x: x["k"])` | `$sort($xs, function($a, $b) { $a.k > $b.k })`, with `<` for `reverse=True` |
| `max(xs, key=lambda x: x["k"])`, `min(...)` | the same `$sort(...)`, read at `[-1]` and at `[0]` |
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
| `json.dumps(x)`, `json.dumps(x, indent=2)` | `$string($x)`, `$string($x, true)` |
| `str(uuid.uuid4())`, `f"{uuid.uuid4()}"` | `$uuid()` |
| `str(datetime.now())`, `f"{datetime.now()}"` | `$now()` |
| `time.time()`, `datetime.now().timestamp()` | `$millis() / 1000` |
| `datetime.fromisoformat(s).timestamp()` | `$toMillis($s) / 1000` |
| `datetime.fromisoformat(s) < datetime.now() - timedelta(days=7)` | `$toMillis($s) < $millis() - 604800000` |
| `str(datetime.fromtimestamp(x))`, `f"{datetime.fromtimestamp(x)}"` | `$fromMillis($x * 1000)` |
| `str(datetime.now() + timedelta(hours=1))` | `$fromMillis($millis() + 3600000)` |
| `datetime.now().strftime("%Y-%m-%d")` | `$now('[Y0001]-[M01]-[D01]')` |
| `(datetime.now() - datetime.fromisoformat(s)).total_seconds()` | `($millis() - $toMillis($s)) / 1000` |
| `timedelta(minutes=90).total_seconds()` | `5400` |
| `s.ljust(5, "0")`, `s.rjust(5, "0")` | `$pad($s, 5, '0')`, `$pad($s, -5, '0')` |
| `f"{s:<10}"`, `f"{s:>10}"`, `f"{s:*<8}"` | `$pad($s, 10)`, `$pad($s, -10)`, `$pad($s, 8, '*')` |
| `f"{x:.2f}"`, `f"{x:,.2f}"`, `f"{x:>10.2f}"` | `$formatNumber($x, '0.00')`, `$formatNumber($x, '#,##0.00')`, `$pad($formatNumber($x, '0.00'), -10)` |
| `f"{n:d}"`, `f"{n:05d}"` | `$formatNumber($n, '0')`, `$formatNumber($n, '00000;-0000')` |
| `list(set(xs))` | `$distinct($xs)` |
| `list(zip(a, b))` | `$zip($a, $b)` |
| `list(itertools.batched(xs, n))` | `[$partition($xs, $n)]` |
| `hashlib.sha256(s.encode()).hexdigest()` | `$hash($s, 'SHA-256')` |
| `base64.b64encode(s.encode()).decode()`, `base64.b64decode(s).decode()` | `$base64encode($s)`, `$base64decode($s)` |
| `urllib.parse.unquote(s)`, `urllib.parse.unquote_plus(s)` | `$decodeUrlComponent($replace($s, '+', '%2B'))`, `$decodeUrlComponent($s)`: Step Functions reads `+` as a space, as `unquote_plus` does, so `unquote` escapes it first |
| `x["key"]`, `x[0]`, `s[0]` | `$x.key`, `$x[0]`, `$substring($s, 0, 1)` |
| `s[1:3]`, `s[-3:]`, `s[1:-1]` | `$substring($s, 1, 2)`, `$substring($s, -3, 3)`, `$substring($s, 1, $length($s) - 2)` |
| `xs[1:3]`, `xs[-2:]` | `[$filter($xs, function($v, $i) { $i >= 1 and $i < 3 })]`, `[$filter($xs, function($v, $i) { $i >= $count($xs) - 2 })]` |
| `{**a, "key": v}` | `$merge([$a, {'key': $v}])`, where a later key wins; a value whose type is unknown is checked for a dict first, since Python raises for anything else |
| `[f(x) for x in xs if c]` | `[$map($filter($xs, function($x) { c }), function($x) { f })]` |
| `any(xs)`, `all(xs)` | `$reduce($xs, function($a, $x) { $a ? true : $boolean($x) }, false)` and `$reduce($xs, function($a, $x) { $a ? $boolean($x) : false }, true)`, each item read for its truth |
| `any(f(x) for x in xs if c)` | the same `$reduce` with the condition and the item in its function: `$a ? true : (c ? f : false)` |
| `{f(x): g(x) for x in xs if c}` | `$merge([$map($xs, function($x) { c ? {f: g} })])`: one pass, whose objects a later key wins in |
| `{k: v for k, v in d.items() if c}` | `$merge([$sift($d, function($v, $k) { c })])`, and `$each` in place of `$sift` where the key or the value is rewritten |
| `f"order {id}"` | `'order ' & $string($id)` |
| `[a, xs, v]` in an expression | `[$a, [$xs], $type($v) = 'array' ? [[$v]] : $v]`: an item known to be a list, or one that may be, stays one item |

- A list comprehension takes one or more `for`, each over a list or the keys of a dict, with conditions of its own: `[r["id"] for lib in libraries for r in lib["rows"]]` flattens a list of lists. A `for` after the first runs for each item the ones before it keep, in order, as `$reduce` appending what it gives. The result is a list for any number of results: `$map` and `$filter` go in brackets when the items are known not to be lists, and in `$append([], $map(...)[])` when they may be, which keeps a single list as one item. Its variable is the parameter of the JSONata function, so it cannot be named after a variable the comprehension reads through another name, such as the list a `for` loop around it iterates.
- A dict comprehension takes one `for`, and `{k: v for k, v in d.items()}` reads a dict entry by entry. Its key is written into a JSON object, so it must be known to be a string: `{x: 1 for x in xs}` needs `xs: list[str]`, and anything else is written `str(x)`, since converting a key of unknown type would write `1` and `"1"` as one entry where Python has two. A key written twice keeps the last value, as in Python. The condition, the key and the value are evaluated once per item, in that order, so an expression that gives another value on every call is read as often as Python reads it.
- A slice bound written with a minus sign (`xs[-2:]`, `xs[-n:]`) counts back from the end; any other bound is a position from the start. A slice takes no step other than `[::-1]`, and one of a list holding lists keeps them as items, as a comprehension does.
- The string and dict methods need no type: of the JSON types only strings have `split`, `replace`, `lower`, `upper`, `join`, `startswith`, `endswith`, `ljust`, `rjust` and `strip`, and only dicts `keys`, `values`, `get` and, in a `for` or a dict comprehension, `items`. `s.split(sep, maxsplit)` is rejected, as `$split` has no counterpart for the rest of the text. Written in the source, the separator of `split` and the pattern of `replace` hold one character or more, the count of `replace` and the width of `ljust` and `rjust` are whole numbers of 0 or more, and the fill of `ljust` and `rjust` is one character.
- `sum`, `max` and `min` take numbers, as their JSONata functions do, so a list known to hold anything else is rejected, as are `sum(xs, start)` and keyword arguments other than the `key=` of `max` and `min`. `sum(xs) / len(xs)` is `$average` when both read the same list.
- `sum`, `max`, `min`, `sorted` and `list` take a generator expression (`sum(x["amount"] for x in items)`) as the list comprehension it would be, since each reads every item. A generator anywhere else is rejected, as it is not a JSON value.
- `any` and `all` take a list, a dict, whose keys they read, or a generator expression with one `for`, and give a boolean: `any(r["failed"] for r in results)` holds where one item is true and `all(r["ok"] for r in results)` where none is false, each item read for its truth as `bool()` reads it. Nothing past the item that decides the result is evaluated: a generator expression's condition and item are evaluated in the function `$reduce` calls, so neither runs for a later item, as neither runs in Python. `any([f(x) for x in xs])` builds the list first, so every item is evaluated, again as in Python. An empty list is `False` for `any` and `True` for `all`, the values `$reduce` starts from.
- `sorted` orders numbers or strings, as `$sort` does without a function, so a list known to hold anything else is rejected. `key=lambda item: ...` orders by what the lambda reads, which is the comparison `$sort` takes, and `max` and `min` take the same `key=`, over a list or over the values written out, reading the ends of that order. The key is a lambda of one item, and what it reads is ordered by `>`, so it is a number or a string. `reverse=True` turns the comparison around, which leaves items with the same key in the order they came, as Python does. `range()` outside a `for` is a list; a step, when given, is a nonzero whole number written in the source.
- `set()`, `zip()` and `itertools.batched()` make lists in `list()`, `sorted()` or `reversed()`, and a digest is `hashlib.sha256(s.encode()).hexdigest()`, with `md5`, `sha1`, `sha384` or `sha512` in place of `sha256` for the others. `base64.b64encode(s.encode()).decode()` is the Base64 text of a string and `base64.b64decode(s).decode()` the string back, in UTF-8 either way, and `urllib.parse.unquote(s)` and `unquote_plus(s)` read `%XX` escapes as UTF-8. Bytes are a value nowhere else: `.encode()` and `.decode()` are taken in these spellings only. `itertools.batched` needs Python 3.12 where the module is run, and its size, when written in the source, is a whole number of 1 or more.
- `json.dumps(x)` is the JSON text of a value, and `json.dumps(x, indent=2)` the same text with each member on a line of its own. `$string` gives a string back as it is, without the quotes JSON writes around one, so a value that may be a string is written inside an object and taken out of its text again: `json.dumps(event)` is `$replace($string({'v': $event}), /^\{"v":|\}$/, '')`, and with `event: dict` it is `$string($event)`. `indent=2` takes a value known not to be a string. No other argument is taken.
- Functions of `math`, `random`, `time`, `json`, `uuid`, `datetime`, `hashlib`, `base64`, `urllib.parse` and `itertools` are recognized through the module's imports, such as `import math` or `from datetime import datetime`.
- A datetime is not a JSON value, so it is converted where it is made: `str()` or an f-string gives the timestamp text, `.timestamp()` the seconds since the epoch, and `wait(until=...)` takes one as the timestamp it waits for. `datetime.now()`, `datetime.fromisoformat(text)` and `datetime.fromtimestamp(seconds)` make one, so `str(datetime.fromisoformat(text))` reads a timestamp and writes it back in the form `$now()` returns. A datetime is compared with another by `==`, `!=`, `<`, `<=`, `>` and `>=`, which compare the milliseconds since the epoch of both, and with nothing else. It is not assigned to a variable; `.timestamp()` is the number that is.
- A `timedelta` moves a datetime, and the result is a datetime, converted in those same places: `wait(until=datetime.now() + timedelta(hours=1))` and `str(datetime.fromisoformat(text) - timedelta(days=1))`. Its units are `weeks`, `days`, `hours`, `minutes`, `seconds` and `milliseconds`, given by name. The units written in the source as numbers are added up while the file compiles, so one number of milliseconds goes into the expression, and `microseconds`, and a total of those that is a fraction of a millisecond, are rejected, as Step Functions keeps time to the millisecond. A unit given any other value is multiplied when it runs: `datetime.now() - timedelta(days=float(EXPIRATION_IN_DAYS))`, with `EXPIRATION_IN_DAYS = "${ExpirationInDays}"` outside the machine, is `$millis() - $number('${ExpirationInDays}') * 86400000`.
- One datetime taken from another is a timedelta, and its seconds are `(datetime.now() - datetime.fromisoformat(text)).total_seconds()`, a negative number where the second moment is the later one. `total_seconds()` of a timedelta written in the source is the number itself. `.days`, `.seconds` and `.microseconds` are rejected; `total_seconds()` is the one field.
- `strftime()` writes a datetime with the picture string that writes what its format writes: `datetime.now().strftime("%Y-%m-%d")` is `$now('[Y0001]-[M01]-[D01]')`, and `datetime.fromisoformat(text).strftime("%H:%M")` is `$fromMillis($toMillis($text), '[H01]:[m01]')`. The format is a string written in the source, holding `%Y`, `%y`, `%m`, `%d`, `%H`, `%M`, `%S`, `%j` and `%%`; any other text is kept as it is. Every other directive is rejected: `%f` is microseconds where Step Functions keeps time to the millisecond, `%z` and `%Z` are empty in Python for a datetime with no time zone, and the locale names and the week numbers have picture components spelled differently. Reading a format with `strptime` is rejected too; `jsonata()` reaches the picture strings `$fromMillis` and `$toMillis` take.
- `context["State"]["EnteredTime"]` has the form `$now()` returns, so the date or the year a state was entered is a slice of it: `context["State"]["EnteredTime"][:10]` and `int(context["State"]["EnteredTime"][:4])`. The seconds a state has been running are `time.time() - datetime.fromisoformat(context["State"]["EnteredTime"]).timestamp()`.
- `str()` and an f-string write a value known while the file compiles as the text itself, rather than as a call that always gives that text: with `MAX_POLLS = 20` outside the machine, `f"stopped after {MAX_POLLS} polls"` is `'stopped after 20 polls'` and `str(MAX_POLLS)` is `'20'`. A float is left to `$string`, which writes one as JavaScript does rather than as Python does.
- An f-string takes a format spec that is a width, the digits a number is written with, `d` for a whole number, or a width and one of those: `f"{code:>8}"`, `f"{code:0<8}"`, `f"{total:.2f}"`, `f"{total:,.2f}"`, `f"{total:>12,.2f}"`, `f"{n:d}"`, `f"{n:5d}"`, `f"{n:05d}"`. The value is one known to be a string or a number, and the spec is written in the source, not built from a field of its own (`f"{s:>{width}}"`).
- A width fills the text to it, with the character to fill with and `<` or `>` before it, and fills on the left for a number and on the right for a string where neither is written, as Python does.
- The digits after the decimal point, and `d`, write the number with `$formatNumber`. It rounds half to even as Python does, though on the decimal the number is written as rather than on the double it holds, so `f"{2.675:.2f}"` is `2.68` here and `2.67` in Python. `,` groups the thousands and is written with the digits, since a picture with no digits drops the fraction. A `0` before the width fills a whole number to that width with the sign inside, as Python counts it there (`f"{n:05d}"` of `-12` is `-0012`), while an alignment written out fills as it does anywhere else (`f"{n:0>5d}"` of `-12` is `00-12`, again as in Python).
- Every other spec is rejected: `^`, `%`, `e`, `x`, `b`, `#`, `n`, `,` on its own, `,` with `d` (`f"{n:08,d}"`, whose commas Python writes among the zeros), and a `0` before a width without `d` (`f"{x:05.2f}"`, `f"{s:05}"`). Conversions (`!r`, `!s`, `!a`, `{x=}`) are rejected. A spec on a datetime or a `uuid.uuid4()` is rejected too, since Python gives it to the value itself, where a datetime reads it as a `strftime` format.
- A string literal that starts with `{%` or ends with `%}` is written as a JSONata string, so Step Functions does not read it as an expression.

## Assignments and variables

- `x = value` assigns one name; `a, b = b, a` and `a, b = parallel(f, g)` assign several in one state, with the values from before it.
- `x: float = value` declares a type along with the value.
- `x += v`, `x -= v` and the others are `x = x + v` and so on. A list is the exception: `xs += [...]` extends the list in place in Python, which a JSON value cannot do, so write `xs = xs + [...]`.
- A key or a position cannot be assigned (`d["k"] = v`); build the new dict or list as a literal.

Variable names become JSONata variable names, at most 80 characters long. A variable's name in the generated ASL is the name in the Python, so choose the Python name for what you want to read there. A few names are changed on the way, as Step Functions would not take them or they would hide something: a leading `_` is dropped (`_tmp` is `$tmp`, `_` is `$value`), and `states` and the name of a JSONata function the generated code calls (`count`, `string`, `keys`, `merge`, ...) get `_val` appended (`$count_val`). A changed name the module already uses, or gives to another name, is numbered (`$tmp_2`). A variable used after a branch must be assigned on every path to it.

A `parallel` branch or a Map function assigns names of its own, as in Python. Step Functions rejects a branch that assigns a variable its enclosing function assigns, so such a name is numbered in the branch (`total` is `$total_2` there), as are the variables sfnx adds for itself (loop counters, caught errors). A parameter of a `distributed_map` function is the name in `args=`, so it keeps its name, and assigning it again where the enclosing function has a variable of that name is rejected.

## Names outside the machine

```python
TABLE = "stock"
RETRIES = [{"ErrorEquals": [Lambda.ServiceException], "MaxAttempts": 3}]


@state_machine
def pay(input):
    stock = aws.optimized.dynamodb.get_item(
        TableName=TABLE, Key={"id": {"S": input["id"]}}
    )
    return aws.optimized.lambda_.invoke(
        FunctionName="charge", Payload=stock, retry=RETRIES
    )
```

- A name assigned at the top level of the module is read as the value written there: the compiler writes that value in where the name is read, so the name itself reaches neither the definition nor Step Functions, and no state is added for it. ASL repeats a Retry state by state, so a retrier is written once here and named where it is used.
- It holds JSON data, exception classes, and other names assigned the same way. The compiler reads the module without running it, so a value it would have to run (`NOW = time.time()`) is rejected where the name is read.
- It is read where a value is written: in expressions and arguments, and in the places that take a value written out, which are the resource ARN of `task()`, `retry=`, `args=`, `label=` and `@state_machine(timeout=...)`.
- An annotation declares its type as it does inside the machine (`TOP: list[str] = [...]`), and a name assigned twice holds what the last assignment writes, while a value written before that line keeps what the names it reads held there, whether a name is the whole value or sits inside one.
- A machine that assigns the name itself reads its own variable, as a function does in Python.
- The value is written in at each place it is read, so a large one read in several places is repeated in the definition. To write it once, assign it to a variable inside the machine, which takes one Pass.
- Only the file being compiled is read: a name imported from another module is not read as its value.

## Control flow

**`if` / `elif` / `else`** is one Choice, a rule per test, with `Default` for `else` or for what follows.

**`while test:`** is a Choice named `while` that the body leads back to. `while True:` has no Choice and leads back to its first state.

**`for x in xs:`** counts with a variable of its own (`x_index`) and uses `$xs[$x_index]` for `x`, so an iteration adds no Pass state (unless the body assigns `x`, which then becomes a variable). A dict loops over its keys. `range(stop)`, `range(start, stop)` and `range(start, stop, step)` count with the loop variable itself; the step is a nonzero literal. When the body changes what the loop iterates, the loop copies it first. The loop variable is not available after the loop: a range variable would count past its last value, and an empty loop would keep the value from before it in Python.

**`for i, item in enumerate(items):`** gives the counter the name `i`, counting from 0; the body cannot assign it. **`for a, b in zip(xs, ys):`** counts one index for both lists and stops at the shorter. **`for k, v in d.items():`** counts the keys and reads `v` as `$lookup($d, $k)`. `enumerate(items, start)`, a `zip` of more than two lists, and `enumerate`, `zip` and `items()` anywhere but a `for` are rejected.

**`break`** leaves the loop; **`continue`** moves to the next iteration, through the increment of a `for`. Loops take no `else`.

## Tasks

```python
receipt = aws.optimized.lambda_.invoke(
    FunctionName="charge",
    Payload=input,
    timeout=30,
    retry=[{"ErrorEquals": [Timeout], "MaxAttempts": 3}],
)
stock = aws.sdk.dynamodb.get_item(TableName="stock", Key={"sku": {"S": sku}})
decision = aws.optimized.sqs.send_message(
    QueueUrl=QUEUE,
    MessageBody={"token": context["Task"]["Token"]},
    pattern=".waitForTaskToken",
)
review = activity("arn:aws:states:us-east-1:123456789012:activity:review", input)
done = task("${ResourceArn}", {"Id": input["id"]})
```

- **`aws.sdk.<service>.<operation>(...)`** calls `arn:aws:states:::aws-sdk:<service>:<action>`, and **`aws.optimized.<service>.<operation>(...)`** calls `arn:aws:states:::<service>:<action>` (`from sfnx import aws`). The service is named as its ARN names it, with `lambda_` for `lambda` and `_` for a hyphen (`emr_containers`). The operation is in snake_case: botocore's name for the SDK action (`list_db_instances` is `listDBInstances`), and for an optimized integration, which botocore does not model, its words joined in camelCase (`start_execution` is `startExecution`). `aws.optimized.http.invoke(...)` is an HTTP Task.
- The API parameters are keyword arguments in PascalCase, and are the `Arguments`. A dict unpacked with `**` is merged into them, and one unpacked on its own is the `Arguments` as it is. The Task's settings are lowercase: `timeout=` (`TimeoutSeconds`), `heartbeat=` (`HeartbeatSeconds`), `role=` (`Credentials.RoleArn`), `retry=`, and `pattern=` (`".sync"`, `".sync:2"` or `".waitForTaskToken"`, which ends the ARN; SDK integrations take only `".waitForTaskToken"`).
- **`activity(arn, input, timeout=, heartbeat=, retry=)`** waits for a worker of an activity, whose ARN is written out or is a `${...}` filled in at deploy time. The input is the `Arguments`, written as a dict.
- **`task(resource, arguments, timeout=, heartbeat=, role=, retry=)`** writes the resource ARN out, the pattern included, and the `Arguments` as the second argument. Any Task can be written this way; it is the spelling for a resource that is not an operation of a service, such as a `${...}` or a Lambda function's ARN, and for arguments whose keys are not identifiers. The ARN is a literal string, or a name assigned outside the machine that holds one.
- A statement holds one Task call, as an assignment (`Assign` gets `$states.result`), a `return` (the Task ends the machine) or a line of its own. It cannot sit in an `if` test, in a comprehension, or where it would run only sometimes (`a and task(...)`, `a < b < task(...)`).
- SDK integrations are checked against botocore: the service, the operation, argument names and the required arguments. Service names follow the AWS SDK for Java (`sfn`, `eventbridge`, `cloudwatchlogs`); botocore's names that differ (`logs`) are rejected with the name to write. Whether Step Functions supports a service or action that botocore has is not checked, nor are the types of argument values; ValidateStateMachineDefinition checks those it can, such as a number written as a Lambda `Payload`.
- Optimized integrations are checked for argument names and required arguments when botocore has the action, and which pattern an action supports is not checked. HTTP Tasks need `ApiEndpoint`, `Method` and a connection. Activity and Lambda function ARNs, and ARNs containing `${...}`, are passed as written.
- Arguments that unpack a dict with `**` are checked only for the argument names written out; the required arguments and what an HTTP Task needs are left to Step Functions when the Task runs.
- A `.waitForTaskToken` Task must pass `context["Task"]["Token"]` in its arguments, the only place it can be read.
- sfnx ships no type stubs for the operations of `aws`, so a type checker takes any operation and any argument; the compiler checks them. An operation's result has the type an annotation on the assignment declares, as `task()`'s does.

## Parallel and maps

```python
order = input["order"]

def email():
    return {"to": order["email"]}

def audit():
    entry = aws.sdk.sns.publish(Message=order["id"])
    return entry["MessageId"]

message, receipt = parallel(email, audit)
```

**`parallel(f, g, retry=)`** compiles each function without parameters as a branch where it is called. A function defined in the machine reads the variables around it; one defined at module level reads none of them. The result is the list of branch results, and `a, b = ...` unpacks it; each name, and each position written as a number (`results[1]`), keeps the type its branch returns. As in Python, a name the function assigns is its own from its first line, not the one around it. A function defined in a branch or a loop can be passed only where every path to that point defines the same one, and a loop cannot define again a function defined before it.

**`inline_map(f, items, max_concurrency=, retry=)`** calls `f(item)` or `f(item, index)` per item. The ItemSelector passes them by parameter name, and the function reads them from `$states.input`; when it calls `task()`, `parallel()` or a map, or assigns a parameter again, its first Pass binds them to variables, since those states replace `$states.input` and the paths that do not assign would read the item. A function that reads them only in its first state, or in the first state of a branch of a `parallel()` that is its first, reads them from `$states.input` there instead, as the item is still the input of those states in every field, the Catch included (measured), and has no such Pass.

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

## Functions called directly

```python
def invoke(function, payload, retry=RETRY):
    return task(LAMBDA, {"FunctionName": function, "Payload": payload}, retry=retry)["Payload"]


@state_machine
def pay(input):
    receipt = invoke("${ChargeFunctionArn}", input)
    return invoke("${NotifyFunctionArn}", receipt)
```

- A function of the module or of the machine can be called on a line of its own, as the value of an assignment or a `return`, and read through subscripts (`f(x)["id"]`). Its body is compiled in place of the call, so each call writes the states the function makes, as copying its body there would.
- A parameter reads the argument written for it. A value is read as the same expression wherever the body reads it, and an ARN or a `retry=` passed on is taken as if written there. An argument that changes on evaluation, such as `str(uuid.uuid4())`, is kept first by a variable named after the parameter. A default is written in the source, and an argument cannot call `task()`, `parallel()`, a map or a function whose body makes states.
- A name the body assigns is the function's own. It keeps its name in the definition unless the calling function uses it too, in which case it is numbered (`ids_2`). A function of the module reads the module's names, so calling one that reads a name the caller assigns is rejected.
- Each `return` gives the statement the value the call would, and the paths join after the call; a function that ends without one gives `None`. A `try` around the call puts its Catch on the states the body makes.
- A function that calls itself, directly or through another, a function defined inside one called directly, decorators, and parameters other than plain ones with or without defaults are rejected.
- A function whose body, past a docstring, is one `return` of a value is an expression, and is also called inside one: `task(LAMBDA, {"FunctionName": "f", "Payload": traced(input)})` writes the value `traced` returns into the Task's `Arguments`, where it is evaluated, so what it reads of the context is the Task's. Each parameter reads the value written for it, and one that changes on evaluation is bound first in a block, so the body reads the one value Python passes. Such a call can be an argument of a function called directly. A function of the module or the machine hides a built-in of its name, as it does in Python.

## Errors

```python
class Declined(Exception):
    pass


class Lambda:
    class ServiceException(Exception):
        pass


try:
    receipt = aws.optimized.lambda_.invoke(FunctionName="charge")
except (Declined, Lambda.ServiceException) as e:
    return {"declined": str(e)}
except Exception:
    raise
```

- **Error names** are exception classes. A class derives from `Exception` directly; ASL error names have no hierarchy, so list several with `except (A, B)`. A class nested in another spells a dotted name. A segment that starts with a digit is written with a leading `_`, which is dropped: `States.Http.StatusCode._416` is `States.Http.StatusCode.416`. A `_` before a name that stands on its own stays, as `_Internal` does. An imported class keeps its name from the first capitalized segment (`errors.Lambda.ServiceException` is `Lambda.ServiceException`). A class of the module that assigns `error = "..."` in its body has that name instead, for one a class name cannot spell: `class NotHelloWorld(Exception): error = "Not a Hello World Example"` raises and catches `Not a Hello World Example`. The name is a non-empty string that does not start with `States.`.
- **sfnx exports** the Step Functions errors a Retry or Catch can name: `Timeout`, `TaskFailed`, `Permissions`, `HeartbeatTimeout`, `DataLimitExceeded`, `ExceedToleratedFailureThreshold`, `ItemReaderFailed`, `ResultWriterFailed`, `QueryEvaluationError`, `HttpSocket`. `except Exception` is `States.ALL`, which does not catch `States.Runtime` and, according to the Step Functions documentation, `States.DataLimitExceeded`.
- **SDK integrations' errors** are `aws.sdk.<service>.errors.<Exception>` (`from sfnx import aws`): `except aws.sdk.dynamodb.errors.ConditionalCheckFailedException` is `DynamoDb.ConditionalCheckFailedException`, which a Task calling `arn:aws:states:::aws-sdk:dynamodb:...` reports. The service is named as its resource ARN names it (`cloudwatchlogs`, `eventbridge`, `sfn`), with `lambda_` for `lambda`. The exception is the class the AWS SDK for Java gives the error, which is the name Step Functions reports: botocore's shape name ending in `Exception` (`aws.sdk.sqs.errors.QueueDoesNotExistException`), and the service's own class for errors its model does not list (`aws.sdk.dynamodb.errors.DynamoDbException`). The compiler checks the name against the installed botocore; at run time any name is an exception class. Optimized integrations such as `lambda:invoke` report names of their own, which nested classes spell.
- **`raise Declined("message")`** is a Fail with that `Error` and `Cause`. A message known not to be a string goes through `$string()`, or is written as its text where the value is known while the file compiles. Python's built-in exceptions and the Step Functions errors cannot be raised, and `from ...` is left out, as a Fail has no chained cause. A Fail always has an `Error`; the Fail without one that ASL allows has no spelling.
- **`try` / `except`** puts a Catch on each Task, Parallel and Map in the body, innermost clauses first. A clause runs with the variables bound before the state that failed. `as e` assigns the error output, `str(e)` and `f"{e}"` read its `Cause`, `type(e).__name__` reads its `Error`, and a bare `raise` fails again with the caught `Error` and `Cause`. `else:` runs after the body without the Catch.
- Pass, Choice and Wait states cannot catch. An expression that fails in a statement of the body without a `task()`, `parallel()` or map call is not caught ([The ASL's own semantics](#the-asls-own-semantics)). A `try` whose body has no Task, Parallel or Map is rejected, and so is a `raise` in the body that its own `except` would catch, or a bare `raise` in a clause that an `except` around it would catch: a Fail ends the execution. `finally` and a bare `except:` are rejected.
- **`retry=`** takes retriers as dicts: `ErrorEquals` (a list of classes), `IntervalSeconds`, `MaxAttempts`, `BackoffRate`, `MaxDelaySeconds`, `JitterStrategy`, each checked against its range. A retrier for `Exception` comes last.

## JSONata expressions

Flow is Python: `if`, `for`, `while` and `try` become states, and the operators and built-ins on this page become expressions. A transform JSONata has and this page does not is written out, with the values it reads passed by name.

```python
from sfnx import aws, jsonata, state_machine


@state_machine
def settle(input):
    charges: list = aws.optimized.lambda_.invoke(FunctionName="load")["Payload"]
    if not charges:
        return {}
    return jsonata(
        "$merge($map($xs, function($c) { {$c.currency: $c.amount} }))", xs=charges
    )
```

- `jsonata(expression, name=value)` writes a JSONata expression as it is, for what has no Python spelling here. `jsonata("$pad($s, -$n, '0')", s=code, n=width)` compiles to `($s := $code; $n := $width; $pad($s, -$n, '0'))`: each value is bound to the variable of its name before the expression, as a Python variable does not always keep its name in the definition.
- The expression is a string written in the source, or a name assigned one outside the machine, so an expression used in several places is written once: with `UNMARSHALL = "..."` at the top of the module, `jsonata(UNMARSHALL, item=item)` writes the expression out where it is called. Its values are given by name. A value named after a JSONata function or `states` would hide it inside the expression, and a value that reads the name of one bound before it would read the new value, so both are rejected. Step Functions checks the expression when it validates the definition.
- A `$name` the call does not bind is read as the variable the definition writes that way, so an assignment the expression reads gets a state of its own, as one written in Python does. Where a name is changed on the way, the variable is the changed one: `$count_val` reads the variable `count`, and `$count` is the JSONata function.
- What the expression calls is unknown, so it is read once wherever the generated JSONata would otherwise write it twice, as `jsonata(...) % 2` does: it is bound at the start of a block, and a list a `for` iterates is saved before the loop.
- The value has no type; declare one where it matters: `padded: str = jsonata(...)`. At run time in Python, `jsonata()` raises `NotImplementedError`.

## Wait and the Context Object

`wait(10)` is `Seconds` (0 to 99,999,999) and `wait(until="2026-09-13T01:59:00Z")` is `Timestamp` (RFC 3339 with an uppercase `T` and `Z`); either may be an expression. `until=` also takes a datetime, which is written as the timestamp text: `wait(until=datetime.now() + timedelta(hours=1))`.

`context["Execution"]["Id"]` reads `$states.context.Execution.Id`, with types for the documented fields: `Execution` (`Id`, `Input`, `Name`, `RoleArn`, `StartTime`, `RedriveCount`, `RedriveTime`), `State` (`EnteredTime`, `Name`, `RetryCount`), `StateMachine` (`Id`, `Name`) and `Task` (`Token`). Unknown keys are rejected. `Map.Item` is rejected because a map passes the item and index to its function.

## What is rejected

Each of these is rejected with what to write instead:

- **Statements**: `with`, `match`, `global` / `nonlocal`, `del`, `import` and `class` inside a state machine, `async`, `finally`, a bare `except:`, `except*`, `else` on a loop, and a value on a line of its own (`print(x)`).
- **Expressions**: tuples, sets, a slice with a step other than `[::-1]`, methods other than `split`, `replace`, `lower`, `upper`, `join`, `startswith`, `endswith`, `ljust`, `rjust` and `strip` of strings and `keys`, `values`, `get` and (in a `for` or a dict comprehension) `items` of dicts, `lambda` outside `key=`, `:=`, `*` unpacking, bitwise operators, unary `+`, conversions in f-strings and every format spec but a width, the digits of a number and `d`, old-style `%` formatting, a generator expression outside `sum`, `max`, `min`, `sorted`, `list`, `any` and `all`, set comprehensions, a dict comprehension whose key is not known to be a string, a dict comprehension or a generator expression in `any` or `all` with more than one `for`, built-in functions other than `len`, `float`, `int`, `str`, `bool`, `list`, `isinstance`, `abs`, `round`, `sum`, `max`, `min`, `sorted`, `reversed`, `range`, `any` and `all`, `set` outside `list()`, `zip` outside `list()` or a `for`, `enumerate` outside a `for`, module functions other than `math.floor`, `math.ceil`, `math.sqrt`, `random.random`, `time.time`, `json.loads`, `json.dumps` (with `indent=2` as its one option), `itertools.batched` in `list()`, the `hashlib` digests, `base64.b64encode` and `base64.b64decode` in `.decode()`, and `urllib.parse.unquote` and `unquote_plus`, and a datetime outside `str()`, an f-string, `.timestamp()`, `.strftime(format)`, `wait(until=...)` or a comparison with another datetime, a `timedelta` outside a datetime it moves and `total_seconds()`, a `strftime` directive other than `%Y`, `%y`, `%m`, `%d`, `%H`, `%M`, `%S`, `%j` and `%%`, `strptime`, or `uuid.uuid4()` outside `str()` or an f-string.
- **Calls**: a function of your own called inside an expression (`f(x) + 1`, `if f(x):`) whose body is anything but one `return` of a value; call it on its own line, assign its result or return it.
- **TypedDicts**: one that names itself or a class written below it, one that inherits from another class, `TypedDict("Name", {...})`, a class argument other than `total=False`, a class of another module, and anything on the class but fields.

## Where results differ from Python

A Python spelling the compiler accepts follows Python for the values that reach it. These are where it does not, grouped under the reason each difference stays. They are the differences known so far; others may remain.

### JSON has no such value

Numbers are doubles, and JSON has no infinity, no complex number and no set. What the definition holds is what JSON can hold.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `a ** b` | a negative `a` and a fractional `b` | `States.QueryEvaluationError` | a complex number |
| `a + b`, `a - b`, `a * b`, `a / b` | a result past the range of a double, such as `1e308 * 10` | `"Infinity"` or `"-Infinity"`, a string | `inf` or `-inf` |
| a number from the input, a variable or `json.loads(s)` | an integer past 2^53, such as `10000000000000000000000001` | the nearest double (`1.0E25`) | the exact integer |
| `json.loads(s)` | `"NaN"`, `"Infinity"`, `"1e400"`, `'{"a": 1, "a": 2}'` | `States.QueryEvaluationError` | `nan`, `inf`, `inf`, `{"a": 2}` |
| `list(set(xs))` | `[2, 1, 2]`, `[True, 1]`, `[{"a": 1}, {"a": 1}]` | `[2, 1]` in the order first seen, `[true, 1]`, `[{"a": 1}]` | an order of its own, `[True]`, `TypeError` |
| `round(x, digits)`, `f"{x:.2f}"` | a number whose decimal is not the double it holds, such as `2.675` to 2 digits, or `1e308` | the decimal the number is written as, rounded half to even: `2.68`, and `1e308` as 1 and 308 zeros | the double it holds: `2.67` (from `2.67499...`), and `1e308` as `100000000000000001097...` |
| `float(x)` | `"1e400"` | `States.QueryEvaluationError` | `inf` |

### A value's text is its JSON

`str()` and an f-string write the value as the definition holds it, which is what someone writing ASL means by turning a value into text. `json.dumps()` writes it as Step Functions writes JSON.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `str(x)`, `f"{x}"` | `True`, `None`, `1.0` | `"true"`, `"null"`, `"1"` | `"True"`, `"None"`, `"1.0"` |
| `str(x)`, `f"{x}"` | `[1, 2]`, `{"a": 1}` | `"[1,2]"`, `"{\"a\":1}"` | `"[1, 2]"`, `"{'a': 1}"` |
| `json.dumps(x)` | `[1, 2]`, `{"a": 1}` | `"[1,2]"`, `"{\"a\":1}"` | `"[1, 2]"`, `"{\"a\": 1}"` |
| `json.dumps(x)`, `json.dumps(x, indent=2)` | `1.0`, `1e-07` | `"1"`, `"1e-7"` | `"1.0"`, `"1e-07"` |
| `json.dumps(x)`, `json.dumps(x, indent=2)` | `"é"`, `"\x01"` | `"\"é\""`, the control character itself between quotes | `"\"\\u00e9\""`, `"\"\\u0001\""` |

### Text is read as Step Functions reads it

Positions and lengths count UTF-16 units, not code points, and a regular expression reads `\s` as the ASCII whitespace. `$length` counts code points, so no arithmetic on positions makes the two agree.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `s.strip()` | whitespace at an end that is not ASCII, such as a non-breaking space | kept: Step Functions reads `\s` as the ASCII whitespace | removed |
| `base64.b64decode(s).decode()` | text missing its padding, such as `"YWJ"` | `"ab"` | `binascii.Error` |
| `base64.b64decode(s).decode()` | a character outside the alphabet, such as `"!!"` | `States.QueryEvaluationError` | `""`: Python discards the character |
| `urllib.parse.unquote(s)`, `unquote_plus(s)` | a malformed escape, such as `"%zz"` | `States.QueryEvaluationError` | `"%zz"`, passed through |
| `s[-1]` | a string ending in a character outside the Basic Multilingual Plane | half of that character (Step Functions counts UTF-16 units) | the character |
| `list(s)`, `sep.join(s)` | a string with characters outside the Basic Multilingual Plane | two items for each such character, neither of them the character | one item for each character |
| `s[a:b]`, `s.startswith(p)`, `s.endswith(p)` | a string with characters outside the Basic Multilingual Plane | may hold other characters or half of one, and compare accordingly | the characters between the positions |
| `sorted(xs)` | strings with characters outside the Basic Multilingual Plane | ordered by UTF-16 units (`"😀"` before `"ﬁ"`) | ordered by code points |

### Time is UTC, to the millisecond

Step Functions has no local time zone, and `$now()`, `$millis()` and `$toMillis` work in whole milliseconds.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `str(datetime.now())`, `str(datetime.fromtimestamp(x))`, `str(dt + timedelta(...))`, `dt.strftime(format)` | any time | the time in UTC, such as `"2026-09-15T13:43:06.735Z"` | the local time, such as `"2026-09-15 22:43:06.735213"` |
| `datetime.fromisoformat(s).timestamp()` | an `s` with no UTC offset, such as `"2026-09-15T13:43:06"` | the seconds counted from UTC | the seconds counted from the local time |
| `datetime.fromisoformat(s) < datetime.now()`, `(datetime.fromisoformat(s) - datetime.now()).total_seconds()` | an `s` with a UTC offset, such as `"2026-09-15T13:43:06Z"`, against `datetime.now()` or an `s` without one | the moments, both read in UTC | `TypeError` for an order or a difference, `False` for `==` |
| `datetime.fromisoformat(s).timestamp()`, `(datetime.fromisoformat(s) - dt).total_seconds()` | an `s` with more than three digits after the second, such as `"2026-09-15T13:43:06.735123Z"` | the seconds to the millisecond (`1789479786.735`) | the seconds as written (`1789479786.735123`) |
| `datetime.fromisoformat(s).timestamp()` | an `s` CPython reads that the ISO 8601 of `$toMillis` does not cover, such as `"2026-09-15 13:43:06"` with a space in place of the `T`, `"20260915T134306Z"` without the dashes, or the week date `"2026-W38-2"` | `States.QueryEvaluationError` | the seconds |
| `str(dt + timedelta(milliseconds=x))`, and the other units | an `x` from the input that is a fraction of a millisecond, such as `1.5` | the time with the fraction dropped towards zero, as `$fromMillis` drops it | the time to the microsecond |
| `time.time()` | any time | seconds to the millisecond, such as `1789479402.245` | seconds to a finer digit, such as `1789479402.8365781` |

### The value is only known when it runs

An argument written in the source that Python would refuse is rejected when the file is compiled. One read from the input or a variable cannot be, so JSONata's own reading of it stands.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `s.split(sep)` | a `sep` read at run time that is empty | the characters of `s` | `ValueError` |
| `s.replace(old, new)` | an `old` read at run time that is empty | `States.QueryEvaluationError` | `new` between every character and at both ends |
| `s.replace(old, new, count)` | a `count` read at run time that is not a whole number of 0 or more | `States.QueryEvaluationError` below `0`, `2.5` taken as `2` | every occurrence replaced for a negative `count`, `TypeError` for `2.5` |
| `s.ljust(n, fill)`, `s.rjust(n, fill)` | a `fill` of several characters read at run time | the fill repeated as far as it goes | `TypeError` |
| `s.ljust(n)`, `s.rjust(n)` | an `n` read at run time that is not a whole number, such as `6.5` | taken as `6` | `TypeError` |
| `list(itertools.batched(xs, n))` | an `n` read at run time that is not a whole number of 1 or more, such as `0` or `1.5` | `[]` for `0`, batches of one for `1.5`, `States.QueryEvaluationError` below `0` | `ValueError` or `TypeError` |
| `f"{x:d}"`, `f"{x:05d}"` | an `x` that is not a whole number, such as `1.5` | the number rounded half to even (`2`, `00002`) | `ValueError`: `d` takes an integer |

### The type is only known when it runs

These spellings need a type to pick the JSONata for them. Without one the definition fails where it reads the value, except `in`, which reads a key lookup rather than write the test for a string and a list into every `in` on an undeclared value. Declaring the type gives Python's meaning in each case, and it also shortens what reads a value's truthiness, which is otherwise written out to follow Python.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `a < b` with `a` and `b` of unknown type | `[1]` and `[2]` | `States.QueryEvaluationError` | `True` |
| `max(xs)`, `min(xs)` with items of unknown type | strings | `States.QueryEvaluationError` | the greatest or least string |
| `sorted(xs)` with items of unknown type | booleans or lists | `States.QueryEvaluationError` | a sorted list |
| `"k" in x` with `x` of unknown type | `"key"` or `["k"]` | `false` (`$exists($x.k)`, a key lookup) | `True` |

### The ASL takes more than Python does

JSONata reads these where Python raises. The intent of the source is met, so nothing is written to refuse them.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `json.loads(s)` | `"{'a': 1}"` | `{"a": 1}` | `JSONDecodeError` |
| `int(x)` | `"1.5"` | `1` | `ValueError` |
| `float(x)` | `"0x10"` | `16` | `ValueError` |

### Written this way on purpose

A minus sign written in the source counts from the end; a negative number that arrives in a variable does not, as the position would otherwise depend on a value the definition cannot see. A minus written before a variable counts that many from the end, so `s[-n:]` is the last `n` characters and an `n` of 0 is none of them, where Python reads `-0` as the start. A list comprehension is a `$filter` and then a `$map`, the two passes a hand-writer would put in an expression. A caught error is named as Step Functions names it. A function called on a line of its own evaluates the `task()` or other state-making call its `return` holds, and not a value nothing reads.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| `xs[a:b]`, or the end of `s[a:b]` | a negative number read from a variable with no minus sign written, such as `i` = -2 | not counted from the end: `xs[i:]` is the whole list, `s[:i]` is `""` | counted from the end |
| `xs[-n:]`, `xs[:-n]`, `s[-n:]`, `s[:-n]` | an `n` of 0 read from a variable | the last 0 items or characters and all but those: `[]` or `""` from `[-n:]`, the whole value from `[:-n]` | `-0` is the start: the whole value from `[-n:]`, `[]` or `""` from `[:-n]` |
| `xs[-n:]`, `xs[:-n]`, `s[-n:]`, `s[:-n]` | a negative number read from a variable with a minus sign written, such as `n` = -2 | `[]` or `""` from `[-n:]`, the whole value from `[:-n]` | counted from the start: `xs[2:]` and `xs[:2]` |
| `type(e).__name__` | `e` caught from a class that assigns `error = "..."` | the name the class declares, which is the error's name in Step Functions | the class name |
| `f(x)` on a line of its own | a function that returns a value that fails, such as `return x["missing"]` | not evaluated, as nothing reads it | `KeyError` |
| `[f(x) for x in xs if c]` | a `c` and an `f` that each give another value on every call, such as `random.random()` | `$filter` tests every item, then `$map` reads a result for each item it kept | the condition and the result of one item before the next item, so a dropped item takes no result |

### The ASL's own semantics

A Map Run reports what it tolerated instead of raising what its children raised, and only a Task, a Parallel or a Map can catch a failure.

| Source | Value | ASL result | CPython result |
|---|---|---|---|
| a statement in a `try` body without a `task()`, `parallel()` or map call, such as `data = json.loads(text)` after `text = task(...)["Body"]` | an expression in it that fails, such as `text` that is not JSON | `States.QueryEvaluationError` ends the execution: the statement is a Pass or a Choice, which cannot catch. Written in the statement of the `task()` call, as `data = json.loads(task(...)["Body"])`, the expression is evaluated by the Task, and its Catch runs the `except`; a `retry=` for `Exception` or `QueryEvaluationError` on that `task()` then calls it again first | the `except` for the error runs |
| an assignment that starts the machine, a branch or a map's function, followed by a `task()` call, such as `order = input["order"]` | an input without the key | `States.QueryEvaluationError` after the Task has run, unless the Task reads the value: the assignment is in the Task's `Assign` | `KeyError` before the call |
| `distributed_map(f, ...)` | `f` raises | within `tolerated_failure_count=` or `tolerated_failure_percentage=`, `{"Status": "FAILED", "Error": ..., "Cause": ...}` in the item's place in the list; otherwise `States.ExceedToleratedFailureThreshold`, which an `except` of the raised class does not catch | the exception `f` raised |

## At run time

Importing the module works, and the names sfnx exports behave as plain Python where they can: `state_machine` returns the function, `wait` returns at once, `parallel` and `inline_map` call their functions in turn, and `distributed_map` calls its function with each item, each value of a dict, or with `batch=` each list of up to `MaxItemsPerBatch` items. The Task calls (`task()`, `activity()` and the operations of `aws`) and `distributed_map(source=...)` raise `NotImplementedError`, and `context` is an empty dict. Functions get the arguments the definition gives them, but `retry=`, `tolerated_failure_count=`, `tolerated_failure_percentage=`, `result=` and `timeout` take effect only in Step Functions.
