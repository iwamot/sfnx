# Compatibility

The version number of a release says what it can change for a project that compiles its workflows with sfnx.

This page describes 3.x. The guarantee of 2.x, that an accepted spelling follows Python apart from a table of differences, is stated in [the compatibility.md of v2.32.0](https://github.com/iwamot/sfnx/blob/v2.32.0/docs/compatibility.md).

## What a major version keeps

Within a major version, no release:

- rejects a source an earlier release of the same major version compiled, except as the table below allows;
- reads a spelling it accepts another way: what [docs/language.md](language.md) says the spelling computes, which is what its JSONata and states give in Step Functions, stays its meaning. The meaning is the values, the branches taken, the Task calls with their arguments and in their order, the failures, and the paths of Catch and Retry. A release may write the same meaning with other expressions or states, and may fix a definition that does not compute what docs/language.md says, as the table below allows;
- removes or renames a name a workflow module imports from `sfnx`, or an argument one takes, or changes what [docs/api.md](api.md) says of `CompileError`, `compile_file` and `compile_source`;
- removes or renames a name of `sfnx.testing.__all__`, an argument of `run`, or a field of `Call`, `Execution` or `Wait`, or changes what [docs/testing.md](testing.md) says of them, apart from its list of where a local run differs;
- removes a command or an option of the CLI, changes what an exit code means, or changes what the Stable column of [Output](../README.md#output) says.

A change to any of these takes the next major version.

## What a release can change

| Change | Release |
|---|---|
| A spelling, function, argument or option that was rejected is accepted | minor |
| A definition that does not compute what docs/language.md says its spelling computes is fixed to compute it | minor, listed in the release notes as changing behavior |
| The names of states, or which states a source makes (split, merged, added or removed), with the same meaning | minor, listed in the release notes |
| Support ends for a Python version past its end of life, or the lowest supported version of a dependency rises | minor |
| `sfnx.testing` runs a state or a field it raised `Unsupported` for | minor |
| `sfnx.testing` gives the result Step Functions gives where it gave another, measured | minor, listed in the release notes as changing results |
| A source is rejected whose definition Step Functions refuses, or whose definition fails on every run that reaches what is now rejected | patch |
| `sfnx.testing` raises `InvalidDefinition` for a definition Step Functions refuses, measured | patch |
| The compiler stops with an internal error (exit 3), or rejects what docs/language.md says it accepts | patch |
| The expressions in a definition, the layout of its JSON, or the text of a message change, with the same results and the same states | any |
| [Where results differ from Python](language.md#where-results-differ-from-python) gains, loses or rewords a row, without changing the meaning docs/language.md gives a spelling | any |

A definition that computes something other than what docs/language.md says its spelling computes is a bug. Fixing one changes what existing definitions compute, so it waits for a minor release and the release notes name it. The meaning docs/language.md gives a spelling is part of the language: reading an accepted spelling another way, such as testing the truth of a value of unknown type otherwise than with `$boolean`, takes the next major version.

[Where results differ from Python](language.md#where-results-differ-from-python) explains, for a reader who knows Python, where that meaning is not what CPython does. It is not part of the guarantee: its rows describe the meaning docs/language.md gives each spelling, and a row it lacks is added, or one is reworded, in any release where the meaning of the spelling stays.

State names are listed because a project may depend on them: execution histories and the mocks and tests that name a state read them, and a Standard execution is billed for each state it enters, so splitting or merging states changes the bill. This is about the same source compiling differently; editing a source still renames the states it touches, as [Output](../README.md#output) describes.

## What the version does not fix

The definition depends on more than the version of sfnx. The same source compiles to the same bytes with the same versions of sfnx, botocore, jsonata-python and Python.

- **botocore**: sfnx checks the arguments of an SDK integration against the service models of the botocore it runs with, and reads the type of a result from them. A botocore release that adds a service makes more sources compile, and one that changes a shape can change what compiles and the expressions written, whichever sfnx version reads it. Pin botocore where sfnx is pinned; [deployment.md](deployment.md#building-the-definitions-to-deploy) shows a lock file that pins both.
- **jsonata-python**: sfnx parses the JSONata it writes, and the text of `jsonata()`, with jsonata-python's parser, to tell a name an expression reads from one it binds or writes in a string. A release that parses an expression otherwise can change which states the compiler merges. The lock file that pins botocore pins it too.
- **Step Functions**: AWS evaluates the JSONata and runs the definition. [docs/verification.md](verification.md) describes the fixed corpus that measures a release against Step Functions; a change on the AWS side is outside what a version of sfnx can promise.
