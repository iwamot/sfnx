"""The errors of AWS SDK integrations, for except and for the ErrorEquals of a
retrier: aws.sdk.dynamodb.errors.ConditionalCheckFailedException is the
DynamoDb.ConditionalCheckFailedException a Task calling
arn:aws:states:::aws-sdk:dynamodb:... reports.

The compiler checks each name against the service's botocore model. At run
time any name gives an exception class, the same one each time.
"""

from functools import cache


class Errors:
    """The errors of one service, named as the AWS SDK for Java names them."""

    def __init__(self, service: str) -> None:
        self._service = service

    def __getattr__(self, name: str) -> type[Exception]:
        if name.startswith("_"):
            raise AttributeError(name)
        return error(self._service, name)


class Service:
    def __init__(self, name: str) -> None:
        self.errors = Errors(name)


class Sdk:
    """The services of SDK integrations, named as their resource ARNs name
    them, with a _ after a Python keyword: sdk.dynamodb, sdk.lambda_."""

    def __getattr__(self, name: str) -> Service:
        if name.startswith("_"):
            raise AttributeError(name)
        return Service(name)


@cache
def error(service: str, name: str) -> type[Exception]:
    return type(name, (Exception,), {"__module__": __name__})


sdk = Sdk()
