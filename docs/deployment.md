# Deployment

sfnx produces the state machine definition and nothing else. Roles, tables, queues, connections and the state machine resource belong to the deployment tool; this page says how to hand the definition over and what the tool has to provide.

## The definition

```bash
sfnx compile app.py -o fulfill.asl.json   # the only machine in the file
sfnx compile app.py -o out/               # one <function>.asl.json per machine
```

The file is a JSONata-mode definition. Values that depend on the deployment are written as `${Name}` in the source, where Step Functions definition substitutions fill them in:

```python
task("arn:aws:states:::lambda:invoke", {"FunctionName": "${ChargeFunctionArn}", "Payload": input})
task("arn:aws:states:::aws-sdk:dynamodb:getItem", {"TableName": "${Table}", "Key": key})
```

A placeholder inside a string argument is an ordinary string to the compiler. A resource ARN may be a placeholder too (`task("${ApproveActivityArn}")`), and is then passed through unchecked.

## Building the definitions to deploy

`uvx sfnx` is for trying a file. A project that deploys the definitions compiles them from a locked environment, since the definition depends on the versions of sfnx, botocore and Python ([compatibility.md](compatibility.md#what-the-version-does-not-fix)). botocore comes in as a dependency of sfnx, so the lock file pins it along with sfnx:

```bash
uv add sfnx          # records sfnx and botocore in uv.lock
uv python pin 3.13   # records the Python version in .python-version
```

Compile into an empty directory each time, and pass on what it holds only when the compiler exits 0:

```bash
set -euo pipefail
rm -rf build/asl
uv run --locked sfnx compile app.py -o build/asl/
```

`-o` leaves the files already in the directory, so a machine that was deleted or renamed would keep its old `.asl.json` there. A source that does not compile writes nothing, but a failure to write, such as a full disk, can leave some of the files written and the rest missing, which only the exit status shows.

## Tracing a state back to the source

`--source-locations` ends the `Comment` of every state, in Parallel branches and Map processors too, with a line that says where in the source the state comes from:

```bash
sfnx compile examples/orders.py --source-locations
```

```json
    "raise": {
      "Type": "Fail",
      "Comment": "sfnx-source: {\"file\": \"examples/orders.py\", \"spans\": [{\"at\": \"43:13-43:73\"}]}",
```

After `sfnx-source: ` comes a JSON object:

- **`file`** is the path as the command was given it, not made absolute.
- **`spans`** lists the source of the state, each as `line:column-line:column`. Lines and columns count from 1, a column counts characters as the diagnostics do, and the end is the column after the last character.
- A state made by a statement spans the statement. The Choice of an `if` spans the header of the `if` and of each `elif`, from the keyword through the colon, and the Choice of a `for` or a `while` spans its header. A Pass that several assignments share spans each of them.
- A span with a **`role`** stands for what the source does not spell, and spans the header of the statement that makes it: `loop start` for what a `for` assigns before its first iteration, `loop step` for moving to the next item, `loop variables` for the loop variables a body assigns again, `parameters` for binding the parameters of a function a map runs, and `end of function` for the return where a body ends without one.

The comment lines written above a statement stay above the location line. The state names, the transitions and what the definition computes are the same with the option and without it.

The spans point into the source as it was when it was compiled. To read them, open that version of the file, such as the commit the definition was built from (`git show <commit>:app.py`), rather than the file as it is now. To have the locations in the definitions a project deploys, add the option to the command that builds them.

## CDK

```python
from aws_cdk import aws_stepfunctions as sfn

machine = sfn.StateMachine(
    self,
    "Fulfill",
    definition_body=sfn.DefinitionBody.from_file("fulfill.asl.json"),
    definition_substitutions={
        "Table": table.table_name,
        "ChargeFunctionArn": charge.function_arn,
    },
)
table.grant_read_write_data(machine)
charge.grant_invoke(machine)
```

## SAM and CloudFormation

```yaml
Resources:
  Fulfill:
    Type: AWS::Serverless::StateMachine
    Properties:
      DefinitionUri: fulfill.asl.json
      DefinitionSubstitutions:
        Table: !Ref Stock
        ChargeFunctionArn: !GetAtt Charge.Arn
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref Stock
        - LambdaInvokePolicy:
            FunctionName: !Ref Charge
```

Plain CloudFormation takes the same file through `AWS::StepFunctions::StateMachine` with `DefinitionS3Location` (or `DefinitionString`) and `DefinitionSubstitutions`.

## Checking a definition before deploying it

`ValidateStateMachineDefinition` checks a definition without creating anything. Run it on the definition after the placeholders are filled (as `envsubst` does below), since a resource that is still `${Name}` is not a valid ARN, and with the type of the machine it will be (`STANDARD` or `EXPRESS`):

```bash
set -euo pipefail
result=$(aws stepfunctions validate-state-machine-definition --type STANDARD \
  --definition file://deploy.asl.json --query result --output text)
test "$result" = OK
```

The call succeeds for a definition that fails validation too: its `result` is `OK` or `FAIL`, and the `diagnostics` say why. Test the result rather than the call's exit status or the text of the diagnostics. A call that fails, such as one without credentials, stops the script as well.

`OK` says Step Functions accepts the definition. It does not say an execution succeeds, that the role has the permissions the tasks need, or what the services answer.

`TestState` runs one state with a given input and role, which is the quickest way to see a Task's real result or an expression's value.

## The AWS CLI

For a quick try, fill the placeholders and create the machine with a role that may call the services. Name the placeholders for `envsubst`: without the list it would also rewrite `$states`, `$count` and the other JSONata variables in the file.

```bash
Table=stock ChargeFunctionArn=arn:aws:lambda:... envsubst '${Table} ${ChargeFunctionArn}' < fulfill.asl.json > deploy.asl.json
aws stepfunctions create-state-machine --name fulfill --role-arn arn:aws:iam::...:role/... --definition file://deploy.asl.json
aws stepfunctions start-execution --state-machine-arn arn:aws:states:... --input '{"id": "o-1", "items": []}'
```

## What the role needs

| The workflow uses | The role needs |
|---|---|
| `task("arn:aws:states:::aws-sdk:<service>:<action>", ...)` | the action on the resource, as with any SDK call (`dynamodb:UpdateItem`, `sns:Publish`, ...) |
| an optimized integration (`lambda:invoke`, `sqs:sendMessage`, ...) | the action of that API (`lambda:InvokeFunction`, `sqs:SendMessage`, ...) |
| `.sync` / `.sync:2` | the action, plus what the integration polls: `events:PutTargets`, `events:PutRule`, `events:DescribeRule` on the managed rule, and the describe action of the job (`states:DescribeExecution`, `batch:DescribeJobs`, ...); and the action that stops the job (`states:StopExecution`, `batch:TerminateJob`, ...), which Step Functions calls when the execution is stopped |
| `.waitForTaskToken` | the action; whoever receives the token calls `SendTaskSuccess` / `SendTaskFailure` with its own credentials |
| an activity ARN | nothing on the role; the worker polls `GetActivityTask` and answers under its own credentials |
| `arn:aws:states:::http:invoke` | `states:InvokeHTTPEndpoint`, `events:RetrieveConnectionCredentials` on the connection, and `secretsmanager:GetSecretValue` / `secretsmanager:DescribeSecret` on its secret |
| `distributed_map` | `states:StartExecution` on the machine itself and `states:DescribeExecution` on its executions (`states:RedriveExecution` to redrive), the S3 actions of `source=` (`s3:GetObject`, `s3:ListBucket`) and of `result=` (`s3:PutObject`, `s3:GetObject`, `s3:ListMultipartUploadParts`, `s3:AbortMultipartUpload`, plus `kms:Decrypt` / `kms:Encrypt` / `kms:GenerateDataKey` on a KMS-encrypted bucket) |
| `role=` on a task | `sts:AssumeRole` on that role, which then needs the API's action |

## Standard and Express

The definition is the same; the type is a property of the state machine resource. Express executions are limited to five minutes and do not support `.waitForTaskToken`, `.sync` or activities, and a `distributed_map` needs a Standard parent.

## Activities and task tokens

An activity worker is a process of yours that polls `GetActivityTask` for the activity's ARN, does the work with the payload, and calls `SendTaskSuccess` with the result (or `SendTaskFailure`). A `.waitForTaskToken` task works the same way from the other side: its arguments send `context["Task"]["Token"]` somewhere (a queue, an event, a Lambda function), and whoever receives it calls `SendTaskSuccess` with that token and the value the workflow continues with. `timeout=` and `heartbeat=` bound how long the machine waits.
