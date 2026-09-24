"""AWS service integrations, each an operation of a service:
aws.sdk.dynamodb.update_item(TableName=..., Key=...) is a Task calling
arn:aws:states:::aws-sdk:dynamodb:updateItem, and
aws.optimized.lambda_.invoke(FunctionName=...) one calling
arn:aws:states:::lambda:invoke. aws.sdk.dynamodb.errors.
ConditionalCheckFailedException is the DynamoDb.ConditionalCheckFailedException
such a Task reports, for except and for the ErrorEquals of a retrier.

The compiler checks the names against botocore's models. At run time an
operation raises NotImplementedError, as task() does, and any error name gives
an exception class, the same one each time.
"""

from collections.abc import Mapping
from functools import cache
from typing import TypeVar

# A result has the type its annotation declares, as task()'s does.
R = TypeVar("R", bound=Mapping[str, object])


class Errors:
    """The errors of one service, named as the AWS SDK for Java names them."""

    def __init__(self, service: str) -> None:
        self._service = service

    def __getattr__(self, name: str) -> type[Exception]:
        if name.startswith("_"):
            raise AttributeError(name)
        return error(self._service, name)


class Operation:
    """An operation of a service, called with its API parameters in
    PascalCase and the Task's timeout=, heartbeat=, role=, retry= and
    pattern=. It only runs in Step Functions."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, **arguments: object) -> R:
        raise NotImplementedError(f"aws.{self._name}() runs in Step Functions")


class Service:
    """A service's operations, in snake_case, and its errors."""

    def __init__(self, kind: str, name: str) -> None:
        self._name = f"{kind}.{name}"
        self.errors = Errors(name)

    def __getattr__(self, operation: str) -> Operation:
        if operation.startswith("_"):
            raise AttributeError(operation)
        return Operation(f"{self._name}.{operation}")


class Services:
    """The services of one kind of integration, named as their resource ARNs
    name them, with a _ after a Python keyword and for a hyphen: sdk.dynamodb,
    optimized.lambda_, optimized.emr_containers."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def __getattr__(self, name: str) -> Service:
        if name.startswith("_"):
            raise AttributeError(name)
        return Service(self._kind, name)


@cache
def error(service: str, name: str) -> type[Exception]:
    return type(name, (Exception,), {"__module__": __name__})


sdk = Services("sdk")
optimized = Services("optimized")
