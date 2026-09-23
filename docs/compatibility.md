# Compatibility

From 1.0, the version number of a release says what it can change for a project that compiles its workflows with sfnx. Before 1.0, a release may change anything below, and the release notes say so.

## What 1.x keeps

Within 1.x, no release:

- rejects a source an earlier 1.x release compiled, except as the table below allows;
- changes what a definition computes, except as the table below allows. For every value, the result is the one [docs/language.md](language.md) gives: Python's, or the row that covers it in [Where results differ from Python](language.md#where-results-differ-from-python), the table of differences;
- removes or renames a name a workflow module imports from `sfnx`, or an argument one takes, or changes what [docs/api.md](api.md) says of `CompileError`, `compile_file` and `compile_source`;
- removes a command or an option of the CLI, changes what an exit code means, or changes what the Stable column of [Output](../README.md#output) says.

A change to any of these is a 2.0.

## What a release can change

| Change | Release |
|---|---|
| A spelling, function, argument or option that was rejected is accepted | minor |
| Where the ASL gives another result than Python without failing, and the table of differences does not list it, the ASL gets Python's result | minor, listed in the release notes as changing results |
| A row of the table of differences whose ASL result is an error gets Python's result | minor, listed in the release notes as changing results |
| The names of states, or which states a source makes (split, merged, added or removed) | minor, listed in the release notes |
| Support ends for a Python version past its end of life, or the lowest supported version of a dependency rises | minor |
| A source is rejected whose definition Step Functions refuses, or whose definition fails on every run that reaches what is now rejected | patch |
| The compiler stops with an internal error (exit 3), or rejects what docs/language.md says it accepts | patch |
| The expressions in a definition, the layout of its JSON, or the text of a message change, with the same results and the same states | any |

A difference the table does not list is a bug: docs/language.md says an accepted spelling follows Python for the values that reach it, apart from the rows of the table. Fixing one changes what existing definitions compute, so it waits for a minor release and the release notes name it. A row of the table is part of the language, and changing one that gives a value is a 2.0.

State names are listed because a project may depend on them: execution histories and the mocks and tests that name a state read them, and a Standard execution is billed for each state it enters, so splitting or merging states changes the bill. This is about the same source compiling differently; editing a source still renames the states it touches, as [Output](../README.md#output) describes.

## What the version does not fix

The definition depends on more than the version of sfnx. The same source compiles to the same bytes with the same versions of sfnx, botocore and Python.

- **botocore**: sfnx checks the arguments of an SDK integration against the service models of the botocore it runs with, and reads the type of a result from them. A botocore release that adds a service makes more sources compile, and one that changes a shape can change what compiles and the expressions written, whichever sfnx version reads it. Pin botocore where sfnx is pinned; [deployment.md](deployment.md#building-the-definitions-to-deploy) shows a lock file that pins both.
- **Step Functions**: AWS evaluates the JSONata and runs the definition. [docs/verification.md](verification.md) describes the fixed corpus that measures a release against Step Functions; a change on the AWS side is outside what a version of sfnx can promise.
