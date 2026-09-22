# sfnx

[![pypi](https://img.shields.io/pypi/v/sfnx.svg)](https://pypi.org/project/sfnx/)
[![python](https://img.shields.io/pypi/pyversions/sfnx.svg)](https://pypi.org/project/sfnx/)

Write the intent of an AWS Step Functions workflow in Python, and sfnx compiles it to the Amazon States Language (ASL) you might write by hand, in JSONata mode. It aims to express what you want the workflow to do in natural, readable states and JSONata expressions, not to reproduce every detail of Python execution. When a construct cannot be translated sensibly, the compiler says what to write instead.

Save this as `app.py` (it is also [examples/orders.py](https://github.com/iwamot/sfnx/blob/main/examples/orders.py)):

```python
from sfnx import Timeout, state_machine, task


class OutOfStock(Exception):
    pass


class DynamoDb:
    class ConditionalCheckFailedException(Exception):
        pass


@state_machine(timeout=300)
def fulfill(input):
    """Reserve every item of an order, then charge for it."""
    items: list = input["items"]
    for item in items:
        try:
            task(
                "arn:aws:states:::aws-sdk:dynamodb:updateItem",
                {
                    "TableName": "stock",
                    "Key": {"sku": {"S": item["sku"]}},
                    "UpdateExpression": "SET quantity = quantity - :n",
                    "ConditionExpression": "quantity >= :n",
                    "ExpressionAttributeValues": {":n": {"N": str(item["quantity"])}},
                },
                retry=[{"ErrorEquals": [Timeout], "MaxAttempts": 3}],
            )
        except DynamoDb.ConditionalCheckFailedException:
            raise OutOfStock(f"{item['sku']} is out of stock") from None
    receipt = task(
        "arn:aws:states:::lambda:invoke",
        {"FunctionName": "charge", "Payload": input},
    )
    return {"order": input["id"], "receipt": receipt["Payload"]}
```

Compile it; `uvx` runs sfnx without adding it to the project:

```bash
uvx sfnx compile app.py -o fulfill.asl.json
```

The definition has the states a person would write by hand, named after what they do. It begins:

```json
{
  "Comment": "Reserve every item of an order, then charge for it.",
  "QueryLanguage": "JSONata",
  "TimeoutSeconds": 300,
  "StartAt": "items",
  "States": {
    "items": {
      "Type": "Pass",
      "Assign": {
        "items": "{% $states.context.Execution.Input.items %}",
        "item_index": 0
      },
      "Next": "for"
    },
    "for": {
      "Type": "Choice",
      "Choices": [
        {
          "Condition": "{% $item_index < $count($items) %}",
          "Next": "updateItem"
        }
      ],
      "Default": "receipt"
    },
```

<details>
<summary>The whole definition</summary>

```json
{
  "Comment": "Reserve every item of an order, then charge for it.",
  "QueryLanguage": "JSONata",
  "TimeoutSeconds": 300,
  "StartAt": "items",
  "States": {
    "items": {
      "Type": "Pass",
      "Assign": {
        "items": "{% $states.context.Execution.Input.items %}",
        "item_index": 0
      },
      "Next": "for"
    },
    "for": {
      "Type": "Choice",
      "Choices": [
        {
          "Condition": "{% $item_index < $count($items) %}",
          "Next": "updateItem"
        }
      ],
      "Default": "receipt"
    },
    "updateItem": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:dynamodb:updateItem",
      "Arguments": {
        "TableName": "stock",
        "Key": {
          "sku": {
            "S": "{% $items[$item_index].sku %}"
          }
        },
        "UpdateExpression": "SET quantity = quantity - :n",
        "ConditionExpression": "quantity >= :n",
        "ExpressionAttributeValues": {
          ":n": {
            "N": "{% $string($items[$item_index].quantity) %}"
          }
        }
      },
      "Retry": [
        {
          "ErrorEquals": [
            "States.Timeout"
          ],
          "MaxAttempts": 3
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "DynamoDb.ConditionalCheckFailedException"
          ],
          "Next": "raise"
        }
      ],
      "Next": "item_index"
    },
    "raise": {
      "Type": "Fail",
      "Error": "OutOfStock",
      "Cause": "{% $string($items[$item_index].sku) & ' is out of stock' %}"
    },
    "item_index": {
      "Type": "Pass",
      "Assign": {
        "item_index": "{% $item_index + 1 %}"
      },
      "Next": "for"
    },
    "receipt": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Arguments": {
        "FunctionName": "charge",
        "Payload": "{% $states.context.Execution.Input %}"
      },
      "Assign": {
        "receipt": "{% $states.result %}"
      },
      "Next": "return"
    },
    "return": {
      "Type": "Succeed",
      "Output": {
        "order": "{% $states.context.Execution.Input.id %}",
        "receipt": "{% $receipt.Payload %}"
      }
    }
  }
}
```

</details>

[examples/](https://github.com/iwamot/sfnx/blob/main/examples/README.md) has more patterns, each with the definition it compiles to: polling a job, waiting for a person's approval, fanning out over items, and an expression written out in JSONata.

## Why

ASL is a JSON document of states that name each other, with the logic in JSONata strings. Writing it means choosing the right spelling for every operation (`+`, `&` or `$append`), wiring `Next` by hand, and repeating Retry and Catch on every Task. sfnx lets you write the flow as Python and does that part.

- **The output is ASL you can read.** States split only where ASL needs them, independent assignments share one Pass, and each state is named after its variable, `return`, `if`, `for` or the API it calls, so execution histories and the console read like the source.
- **Mistakes surface at compile time.** Every rejected line comes with what to write instead. SDK integration ARNs and their argument names are checked against the botocore service models (whether Step Functions integrates the action is not checked).
- **Python control flow with a few workflow primitives.** The names sfnx exports make states (`task`, `wait`, `parallel`, `inline_map`, `distributed_map`) or name what ASL names (`context`, error classes, `jsonata` for an expression written out). Everything else is Python syntax, compiled to the JSONata you would write for it. [Where results differ from Python](https://github.com/iwamot/sfnx/blob/main/docs/language.md#where-results-differ-from-python) lists the values known to come out otherwise.

sfnx compiles; it does not run workflows or mock tasks, and it does not deploy. The Python module stays importable, but the definition is the contract, not what CPython computes.

## Setup

`uvx sfnx compile app.py` compiles a file without adding sfnx to the project, since `compile` parses the file and never imports it. The module itself imports `sfnx`, so to run it as Python, in tests or from a CDK app, add sfnx to the project:

```bash
uv add sfnx
```

`uv run sfnx compile app.py` then runs the compiler from the project.

## What you write

- **The machine** is a function marked `@state_machine` or `@state_machine(timeout=300)`. Its parameter is the execution input, read as `$states.context.Execution.Input`; its return value is the output.
- **Assignments, `if` / `elif` / `else`, `for`, `while`, `break`, `continue`, `return`, `raise`, `try` / `except`** become Pass, Choice, loops through Choice, Succeed, Fail and Catch. `for` iterates a list, the keys of a dict, `range()`, `enumerate()`, `zip()` or `d.items()`.
- **`task(resource, arguments, timeout=, heartbeat=, role=, retry=)`** is a Task for any integration: SDK (`arn:aws:states:::aws-sdk:dynamodb:getItem`), optimized (`arn:aws:states:::lambda:invoke`, with `.sync` or `.waitForTaskToken`), HTTP, activities, or a `${Placeholder}` filled in at deploy time.
- **`parallel(f, g)`** runs functions without parameters as branches. **`inline_map(f, items)`** and **`distributed_map(f, items or source=, args=, batch=, result=)`** run a function per item.
- **`wait(10)`** and **`wait(until=timestamp)`** are Wait states. **`context["Execution"]["Id"]`** reads the Context Object.
- **`jsonata("$pad($s, -5, '0')", s=code)`** writes a JSONata expression out, for what has no Python spelling, with each value bound to the variable of its name.
- **Exceptions** are your own classes derived from `Exception`, nested classes for dotted names (`Lambda.ServiceException`), or the Step Functions errors sfnx exports (`Timeout`, `TaskFailed`, ...). `except Exception` is `States.ALL`.
- **Names assigned outside the machine** (`RETRIES = [{"ErrorEquals": [Timeout], "MaxAttempts": 3}]`) hold JSON data and exception classes, and are written into the definition where they are read, so what ASL repeats state by state is written once.
- **Expressions** are Python operators, conditional expressions, list and dict comprehensions, f-strings, slices and dicts with `**`, and the functions and methods JSONata has a counterpart for:
  - built-in functions `len`, `float`, `int`, `str`, `bool`, `list`, `isinstance`, `abs`, `round`, `sum`, `max`, `min`, `sorted`, `reversed`, `range`, `any` and `all`, and `set` and `zip` in `list()` (`sum(xs) / len(xs)` is `$average`, `sorted`, `max` and `min` take `key=lambda item: ...`, and `sum`, `max`, `min`, `sorted`, `list`, `any` and `all` take a generator expression: `any(r["failed"] for r in results)`, which `any` and `all` stop reading once the result is decided)
  - `math.floor`, `math.ceil`, `math.sqrt`, `random.random`, `time.time`, `json.loads`, `itertools.batched` in `list()`, `str(uuid.uuid4())`, `hashlib.sha256(s.encode()).hexdigest()`, `base64.b64encode(s.encode()).decode()`, `base64.b64decode(s).decode()`, `urllib.parse.unquote(s)` and `unquote_plus(s)`
  - a datetime from `datetime.now()`, `datetime.fromisoformat(text)` or `datetime.fromtimestamp(seconds)`, converted where it is made: `str()` or an f-string for the timestamp text, `.timestamp()` for the seconds
  - the string methods `split`, `replace`, `lower`, `upper`, `join`, `startswith`, `endswith`, `ljust`, `rjust` and `strip`, and the dict methods `keys`, `values` and `get` (and `items` in a `for` or a dict comprehension: `{k: v for k, v in d.items() if v > 0}`)
- **Types** are written where an operator depends on them, as annotations: `+` is `+`, `&` or `$append` depending on the operands, and `len` is `$count`, `$length` or `$count($keys(...))`. A `TypedDict` class of the module declares the fields of an input, a Lambda `Payload` or a Task result once, for the compiler and the type checker alike. Literals, operator results and AWS API responses carry their types already.
- **Comments** go into the definition: a function's docstring is the `Comment` of the machine, a Parallel branch or a Map processor, and the comment lines right above a statement are the `Comment` of the first state it makes.
- **Anything else** (`with`, other methods, a `lambda` outside `key=`, ...) is rejected with what to write instead; [the reference](https://github.com/iwamot/sfnx/blob/main/docs/language.md#what-is-rejected) lists it.

[docs/language.md](https://github.com/iwamot/sfnx/blob/main/docs/language.md) is the reference, and [docs/design.md](https://github.com/iwamot/sfnx/blob/main/docs/design.md) explains the design and the Step Functions behavior it relies on.

## Rejected lines

Success prints the definition on stdout and exits 0. A rejected line exits 1 with the location and what to write instead:

```
$ sfnx compile app.py
app.py:6:12: + adds numbers, joins strings or lists, so the type of input['price'] must be known; assign it to an annotated variable first: value: float = input['price']

$ sfnx compile app.py
app.py:6:63: getItem has no argument Tablename; did you mean TableName?

$ sfnx compile app.py
app.py:6:12: calling print() is not supported; write it with operators or jsonata(), or compute it in a Lambda task

$ sfnx compile app.py
app.py:6:9: loop over one variable: for item in items (unpack inside the loop)
```

A call that cannot proceed exits 2:

```
$ sfnx compile missing.py
missing.py: No such file or directory

$ sfnx compile app.py
app.py defines 2 state machines (pay, refund); pass -o out/ to write one file each
```

## Reference

```
usage: sfnx [-h] [--version] [--instructions] command ...

sfnx - write Step Functions workflows as Python functions and compile them to Amazon States Language.

positional arguments:
  command
    compile       compile the @state_machine functions of a file to ASL

options:
  -h, --help      show this help message and exit
  --version       show program's version number and exit
  --instructions  print the paragraph for an agent instruction file and exit

Examples:
  sfnx compile app.py            print the ASL of the only @state_machine
  sfnx compile app.py -o out/    write one <function>.asl.json per @state_machine

Exit codes:
  0  success
  1  the source is not accepted; the message names the line and what to write instead
  2  the call is wrong or a file cannot be read or written
  3  internal error; report it with the source that caused it
```

- `compile` parses the file and never imports or runs it.
- The compiler is a Python function too: [docs/api.md](https://github.com/iwamot/sfnx/blob/main/docs/api.md) describes `compile_file` and `compile_source`.
- `-o` ending in `.json` writes the only machine to that file; any other path is a directory that receives `<function>.asl.json` per machine. Missing directories are created.
- The first error stops the compilation, so one run reports one line.

## Output

| Stream | Shape | Stable |
|---|---|---|
| stdout (exit 0) | the definition as indented JSON in UTF-8, with text as written | valid JSON of a definition |
| stderr (exit 1) | `<path>:<line>:<column>: <message>` | the location before the message |
| stderr (exit 2) | `<path>: <reason>`, or a message naming the path | the path |

The line and the column count from 1, and the column counts characters: a tab is one column, and so is a character outside ASCII, whatever it takes on screen or in UTF-8 or UTF-16.

The message text, including `; <what to write instead>`, is prose and may change between releases. So may state names when the source changes above them in the same scope (serial numbers such as `amount_2`).

Before 1.0, the definition compiled from the same source, and what the language accepts, may change between releases; the release notes say so.

## Deploying the definition

sfnx stops at the definition. Write `${Name}` where a value comes from the deployment, as a resource ARN or inside an argument string, and fill it with CDK `definition_substitutions`, SAM or CloudFormation `DefinitionSubstitutions`. [docs/deployment.md](https://github.com/iwamot/sfnx/blob/main/docs/deployment.md) has the snippets, how to check a definition before deploying it, and the IAM actions each kind of task needs.

## Development

```bash
env -u VIRTUAL_ENV ./validate.sh
```

`validate.sh` runs lint, formatting, type checking, the tests and a build. The tests evaluate the generated JSONata with jsonata-python and run whole definitions through a small interpreter, including random programs whose results must match CPython's. [docs/verification.md](https://github.com/iwamot/sfnx/blob/main/docs/verification.md) describes what those checks guarantee and how to run the fixed corpus in Step Functions itself.

## License

MIT
