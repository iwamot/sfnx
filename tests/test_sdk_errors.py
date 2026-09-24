import re
import textwrap

import pytest

from sfnx import aws
from sfnx.compiler import compile_source
from sfnx.diagnostics import CompileError
from sfnx.integrations import java_exception_name, java_service_name

PUBLISH = 'task("arn:aws:states:::aws-sdk:sns:publish", {"Message": "m"})'


def source(body: str, imports: str) -> str:
    return (
        f"from sfnx import state_machine, task\n{imports}\n\n"
        "@state_machine\ndef pay(input):\n" + textwrap.indent(body, "    ")
    )


def caught(error: str, imports: str = "from sfnx import aws") -> list[str]:
    body = f"try:\n    {PUBLISH}\nexcept {error}:\n    return 1\nreturn 0"
    (compiled,) = compile_source(source(body, imports)).values()
    return compiled["States"]["publish"]["Catch"][0]["ErrorEquals"]


# The names Step Functions reported in TestState on 2026-09-24.
@pytest.mark.parametrize(
    "error, name",
    [
        ("dynamodb.errors.DynamoDbException", "DynamoDb.DynamoDbException"),
        ("sqs.errors.QueueDoesNotExistException", "Sqs.QueueDoesNotExistException"),
        ("sfn.errors.InvalidArnException", "Sfn.InvalidArnException"),
        ("s3.errors.NoSuchBucketException", "S3.NoSuchBucketException"),
        (
            "cloudwatchlogs.errors.AccessDeniedException",
            "CloudWatchLogs.AccessDeniedException",
        ),
        (
            "eventbridge.errors.AccessDeniedException",
            "EventBridge.AccessDeniedException",
        ),
        ("rds.errors.RdsException", "Rds.RdsException"),
        ("ec2.errors.Ec2Exception", "Ec2.Ec2Exception"),
        ("elasticache.errors.ElastiCacheException", "ElastiCache.ElastiCacheException"),
        ("apigateway.errors.ApiGatewayException", "ApiGateway.ApiGatewayException"),
        ("lambda_.errors.LambdaException", "Lambda.LambdaException"),
    ],
)
def test_measured_names(error, name):
    assert caught(f"aws.sdk.{error}") == [name]


@pytest.mark.parametrize(
    "error, name",
    [
        # Fault becomes Exception, and acronyms are cased as words.
        ("rds.errors.DbInstanceNotFoundException", "Rds.DbInstanceNotFoundException"),
        ("lambda_.errors.Ec2AccessDeniedException", "Lambda.Ec2AccessDeniedException"),
        # The AWS SDK for Java renames some shapes.
        (
            "elasticloadbalancing.errors.LoadBalancerNotFoundException",
            "ElasticLoadBalancing.LoadBalancerNotFoundException",
        ),
    ],
)
def test_names_from_the_sdk_for_java(error, name):
    assert caught(f"aws.sdk.{error}") == [name]


@pytest.mark.parametrize(
    "imports, error",
    [
        ("import sfnx", "sfnx.aws.sdk.dynamodb.errors.DynamoDbException"),
        ("from sfnx.aws import sdk", "sdk.dynamodb.errors.DynamoDbException"),
        ("from sfnx import aws as a", "a.sdk.dynamodb.errors.DynamoDbException"),
    ],
)
def test_imports(imports, error):
    assert caught(error, imports) == ["DynamoDb.DynamoDbException"]


def test_retry():
    body = (
        f"{PUBLISH[:-1]}, retry=[{{'ErrorEquals': "
        "[aws.sdk.sns.errors.ThrottledException]}])\nreturn 0"
    )
    (compiled,) = compile_source(source(body, "from sfnx import aws")).values()
    retrier = compiled["States"]["publish"]["Retry"][0]
    assert retrier == {"ErrorEquals": ["Sns.ThrottledException"]}


@pytest.mark.parametrize(
    "error, message",
    [
        (
            "aws.sdk.dynamodb.ConditionalCheckFailedException",
            "an SDK integration's error is aws.sdk.<service>.errors.<Exception>",
        ),
        (
            "aws.ConditionalCheckFailedException",
            "an SDK integration's error is aws.sdk.<service>.errors.<Exception>",
        ),
        (
            "aws.sdk.logs.errors.ResourceNotFoundException",
            (
                "SDK integrations name the service cloudwatchlogs, not logs: "
                "aws.sdk.cloudwatchlogs.errors.ResourceNotFoundException"
            ),
        ),
        (
            "aws.sdk.dynamodbb.errors.DynamoDbException",
            "no AWS SDK service is named dynamodbb; did you mean dynamodb",
        ),
        (
            "aws.sdk.sqs.errors.QueueDoesNotExist",
            "sqs has no error QueueDoesNotExist; did you mean QueueDoesNotExistException?",
        ),
        ("aws.sdk.sqs.errors.Zzz", "sqs has no error Zzz"),
    ],
)
def test_diagnostics(error, message):
    with pytest.raises(CompileError, match=re.escape(message)):
        caught(error)


@pytest.mark.parametrize(
    "service_id, name",
    [
        ("DynamoDB", "DynamoDb"),
        ("CloudWatch Logs", "CloudWatchLogs"),
        ("SFN", "Sfn"),
        ("Database Migration Service", "DatabaseMigration"),
        ("Elastic Load Balancing v2", "ElasticLoadBalancingV2"),
        ("Lex Models V2", "LexModelsV2"),
        ("AWSHealth", "Health"),
        ("Amazon Q", "Q"),
    ],
)
def test_java_service_name(service_id, name):
    assert java_service_name(service_id) == name


@pytest.mark.parametrize(
    "shape, name",
    [
        ("ConditionalCheckFailedException", "ConditionalCheckFailedException"),
        ("QueueDoesNotExist", "QueueDoesNotExistException"),
        ("DBInstanceNotFoundFault", "DbInstanceNotFoundException"),
        ("AWSOrganizationsNotInUseException", "AwsOrganizationsNotInUseException"),
        ("KMSInvalidStateException", "KmsInvalidStateException"),
        # The service's own exception keeps this name.
        ("DynamoDbException", "DefaultDynamoDbException"),
    ],
)
def test_java_exception_name(shape, name):
    assert java_exception_name(shape, "DynamoDb") == name


def test_at_run_time_a_name_is_the_same_class_each_time():
    error = aws.sdk.dynamodb.errors.ConditionalCheckFailedException
    assert issubclass(error, Exception)
    assert error.__name__ == "ConditionalCheckFailedException"
    with pytest.raises(error):
        raise aws.sdk.dynamodb.errors.ConditionalCheckFailedException


def test_at_run_time_private_names_are_not_errors():
    assert not hasattr(aws.sdk, "_services")
    assert not hasattr(aws.sdk.dynamodb.errors, "_service_name")
