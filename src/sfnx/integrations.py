"""What a Task resource ARN calls, and what botocore knows about it."""

import difflib
import keyword
import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

import botocore.session
from botocore import xform_name
from botocore.model import (
    ListShape,
    MapShape,
    OperationModel,
    ServiceModel,
    Shape,
    StructureShape,
)

from sfnx.jsontypes import ARRAY, BOOLEAN, NUMBER, OBJECT, STRING, Type, of

PARTITION = r"arn:aws(?:-cn|-us-gov)?"
SDK = re.compile(PARTITION + r":states:::aws-sdk:([a-z0-9]+):(\w+)(\.\w+(?::\d+)?)?")
OPTIMIZED = re.compile(PARTITION + r":states:::([a-z0-9-]+):(\w+)(\.\w+(?::\d+)?)?")
ACTIVITY = re.compile(PARTITION + r":states:[a-z0-9-]+:\d{12}:activity:([\w-]+)")
HTTP = re.compile(PARTITION + r":states:::http:invoke")
FUNCTION = re.compile(
    PARTITION + r":lambda:[a-z0-9-]+:\d{12}:function:([\w-]+)(:[\w-]+)?"
)
PATTERNS = {"", ".sync", ".sync:2", ".waitForTaskToken"}

# Step Functions names SDK services after the AWS SDK for Java. These differ
# from botocore's names by more than hyphens.
SERVICES = {
    "applicationdiscovery": "discovery",
    "cloudwatchlogs": "logs",
    "cognitoidentityprovider": "cognito-idp",
    "costandusagereport": "cur",
    "costexplorer": "ce",
    "databasemigration": "dms",
    "directory": "ds",
    "directoryservicedata": "ds-data",
    "elasticloadbalancing": "elb",
    "elasticloadbalancingv2": "elbv2",
    "elasticsearch": "es",
    "eventbridge": "events",
    "iotjobsdataplane": "iot-jobs-data",
    "lexmodelsv2": "lexv2-models",
    "lexruntimev2": "lexv2-runtime",
    "marketplacemetering": "meteringmarketplace",
    "migrationhub": "mgh",
    "serverlessapplicationrepository": "serverlessrepo",
    "sfn": "stepfunctions",
    # An optimized integration.
    "states": "stepfunctions",
}

# botocore's names, without hyphens, that SDK integrations spell otherwise,
# and the name of the optimized Step Functions integration.
RENAMED = {
    **{
        botocore.replace("-", ""): java
        for java, botocore in SERVICES.items()
        if java != "states"
    },
    "states": "sfn",
}

HTTP_REQUIRED = frozenset({"ApiEndpoint", "Method"})
HTTP_ARGUMENTS = HTTP_REQUIRED | {
    "Authentication",
    "InvocationConfig",
    "Headers",
    "QueryParameters",
    "RequestBody",
    "Transform",
}
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
HTTP_RESULT = Type(
    frozenset({OBJECT}),
    fields=(
        ("Headers", of(OBJECT)),
        ("ResponseBody", None),
        ("StatusCode", of(NUMBER)),
        ("StatusText", of(STRING)),
    ),
)


class ResourceError(ValueError):
    pass


@dataclass(frozen=True)
class Integration:
    """name is the base of the state name for a task() on a line of its own.
    required and allowed are Arguments keys; allowed is None when the keys
    cannot be checked. arguments is the type of the Arguments, whose fields
    are the keys at each level, and result the type of $states.result."""

    kind: str
    name: str
    pattern: str = ""
    required: frozenset[str] = frozenset()
    allowed: frozenset[str] | None = None
    result: Type | None = None
    arguments: Type | None = None


def integration(resource: str) -> Integration:
    if "${" in resource:
        # A definition substitution, filled in when the machine is deployed.
        return Integration("substituted", "task")
    if match := SDK.fullmatch(resource):
        service, action, pattern = match.group(1), match.group(2), match.group(3) or ""
        if pattern not in {"", ".waitForTaskToken"}:
            raise ResourceError(
                f"SDK integrations support only .waitForTaskToken, not {pattern}"
            )
        model = sdk_service(service, f"arn:aws:states:::aws-sdk:{{}}:{action}")
        operation = find_operation(service, model, action)
        return Integration(
            "sdk",
            action,
            pattern,
            required(operation, sdk_member),
            arguments(operation, sdk_member),
            shape_type(operation.output_shape, 0, sdk_member) if not pattern else None,
            shape_type(operation.input_shape, 0, sdk_member),
        )
    if HTTP.fullmatch(resource):
        return Integration(
            "http", "invoke", "", HTTP_REQUIRED, HTTP_ARGUMENTS, HTTP_RESULT
        )
    if match := OPTIMIZED.fullmatch(resource):
        service, action, pattern = match.group(1), match.group(2), match.group(3) or ""
        if pattern not in PATTERNS:
            raise ResourceError(
                f"{pattern} is not an integration pattern; use .sync, .sync:2 "
                "or .waitForTaskToken"
            )
        model = service_model(service)
        operations = operation_names(model) if model else {}
        if model is None or action not in operations:
            # Optimized integrations with actions of their own, such as
            # apigateway:invoke, have nothing in botocore to check against.
            return Integration("optimized", action, pattern)
        operation = model.operation_model(operations[action])
        result = shape_type(operation.output_shape, 0, pascal) if not pattern else None
        return Integration(
            "optimized",
            action,
            pattern,
            required(operation, pascal),
            arguments(operation, pascal),
            result,
            shape_type(operation.input_shape, 0, pascal),
        )
    if match := ACTIVITY.fullmatch(resource):
        return Integration("activity", match.group(1))
    if match := FUNCTION.fullmatch(resource):
        return Integration("function", match.group(1))
    raise ResourceError(
        "the resource is not a Task ARN; write one such as "
        '"arn:aws:states:::aws-sdk:dynamodb:getItem" or '
        '"arn:aws:states:::lambda:invoke"'
    )


def sdk_service(service: str, spelling: str) -> ServiceModel:
    """The model of an SDK integration's service, named as its ARN names it.
    spelling is how the name is written, with {} for the service."""
    if service in RENAMED:
        raise ResourceError(
            f"SDK integrations name the service {RENAMED[service]}, not "
            f"{service}: {spelling.format(RENAMED[service])}"
        )
    model = service_model(service)
    if model is None:
        close = difflib.get_close_matches(service, sorted(services()), n=3)
        hint = f"; did you mean {' or '.join(close)}?" if close else ""
        raise ResourceError(
            f"no AWS SDK service is named {service}{hint} (the name is "
            "lowercase without hyphens, such as dynamodb; a service newer "
            "than the installed botocore needs an update)"
        )
    return model


def arn_service(name: str) -> str:
    """A service as Python spells it after aws.sdk. or aws.optimized.: a _
    after a Python keyword, lambda_ for lambda, is dropped."""
    if keyword.iskeyword(name.removesuffix("_")):
        return name.removesuffix("_")
    return name


def operation_resource(kind: str, service: str, operation: str) -> str:
    """The resource ARN of aws.sdk.<service>.<operation>(...) or
    aws.optimized.<service>.<operation>(...). An SDK integration's operation
    is botocore's snake_case name for its action; an optimized integration has
    no model to look one up in, so its words are joined in camelCase."""
    service = arn_service(service)
    if kind == "optimized":
        if not (
            re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", service)
            and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", operation)
        ):
            raise ResourceError(
                "an optimized integration is aws.optimized.<service>.<operation> "
                "in lowercase, with _ between words and for a hyphen: "
                "aws.optimized.states.start_execution"
            )
        head, *rest = operation.split("_")
        action = head + "".join(word.capitalize() for word in rest)
        return f"arn:aws:states:::{service.replace('_', '-')}:{action}"
    model = sdk_service(service, f"aws.sdk.{{}}.{operation}(...)")
    operations = {xform_name(name): name for name in model.operation_names}
    if operation not in operations:
        close = difflib.get_close_matches(operation, sorted(operations), n=3)
        hint = f"; did you mean {' or '.join(close)}?" if close else ""
        raise ResourceError(f"{service} has no operation {operation}{hint}")
    action = operations[operation]
    return f"arn:aws:states:::aws-sdk:{service}:{action[0].lower()}{action[1:]}"


def sdk_error(service: str, name: str) -> str:
    """The error name an SDK integration reports for an exception of its
    service, such as DynamoDb.ConditionalCheckFailedException: the class the
    AWS SDK for Java gives it, after the service's name in that SDK. An error
    the model does not list is the service's own, DynamoDb.DynamoDbException."""
    service = arn_service(service)
    model = sdk_service(service, f"aws.sdk.{{}}.errors.{name}")
    prefix = java_service_name(model.service_id)
    renamed = JAVA_RENAMED_ERRORS.get(model.service_name, {})
    names = {
        java_exception_name(renamed.get(shape.name, shape.name), prefix)
        for shape in model.error_shapes
    } | {prefix + "Exception"}
    if name not in names:
        close = difflib.get_close_matches(name, sorted(names), n=3)
        hint = f"; did you mean {' or '.join(close)}?" if close else ""
        raise ResourceError(f"{service} has no error {name}{hint}")
    return f"{prefix}.{name}"


# Error shapes the AWS SDK for Java renames, by botocore's service name.
JAVA_RENAMED_ERRORS = {
    "elb": {
        "AccessPointNotFoundException": "LoadBalancerNotFoundException",
        "DuplicateAccessPointNameException": "DuplicateLoadBalancerNameException",
        "InvalidEndPointException": "InvalidInstanceException",
        "TooManyAccessPointsException": "TooManyLoadBalancersException",
    },
    "marketplacecommerceanalytics": {
        "MarketplaceCommerceAnalyticsException": (
            "MarketplaceCommerceAnalyticsServiceException"
        ),
    },
}


def java_words(name: str) -> list[str]:
    """The words of a name as the AWS SDK for Java splits them to case it."""
    name = re.sub(r"[^A-Za-z0-9]+", " ", name)
    name = re.sub(r"([^a-z]{2,})v([0-9]+)", r"\1 v\2 ", name)
    name = re.sub(r"([^A-Z]{2,})V([0-9]+)", r"\1 V\2 ", name)
    name = " ".join(re.split(r"(?<=[a-z])(?=[A-Z](?:[a-zA-Z]|[0-9]))", name))
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    name = re.sub(r"([0-9])([a-zA-Z])", r"\1 \2", name)
    return name.split()


def java_pascal(name: str) -> str:
    """PascalCase as the AWS SDK for Java writes it: DynamoDB is DynamoDb."""
    return "".join(word.lower().capitalize() for word in java_words(name))


def java_service_name(service_id: str) -> str:
    name = java_pascal(service_id)
    for prefix in ("amazon", "aws"):
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
    if name.lower().endswith("service"):
        name = name[: -len("service")]
    return name


def java_exception_name(shape: str, service: str) -> str:
    if shape.endswith("Fault"):
        name = java_pascal(shape.removesuffix("Fault")) + "Exception"
    elif shape.endswith("Exception"):
        name = java_pascal(shape)
    else:
        name = java_pascal(shape) + "Exception"
    # The service's own exception has this name.
    return "Default" + name if name == service + "Exception" else name


@cache
def session() -> botocore.session.Session:
    return botocore.session.get_session()


@cache
def services() -> dict[str, str]:
    names = session().get_available_services()
    index = {name.replace("-", ""): name for name in names}
    return {**index, **SERVICES}


@cache
def service_ids() -> dict[str, str]:
    """Services by their service ID, the name the SDK for Java derives its own
    from. Loading every model is slow, so this is only for names not known
    otherwise."""
    index = {}
    for name in session().get_available_services():
        service_id = session().get_service_model(name).service_id
        key = re.sub(r"[^a-z0-9]", "", service_id.lower())
        index[key] = name
        index[key.removesuffix("service")] = name
    return index


def service_model(service: str) -> ServiceModel | None:
    name = services().get(service) or service_ids().get(service)
    return session().get_service_model(name) if name else None


def operation_names(model: ServiceModel) -> dict[str, str]:
    return {name[0].lower() + name[1:]: name for name in model.operation_names}


def find_operation(service: str, model: ServiceModel, action: str) -> OperationModel:
    names = operation_names(model)
    if action not in names:
        close = difflib.get_close_matches(action, names, n=3)
        hint = f"; did you mean {' or '.join(close)}?" if close else ""
        raise ResourceError(f"{service} has no API action {action}{hint}")
    return model.operation_model(names[action])


def pascal(member: str) -> str:
    """A member's name in an optimized integration: botocore's, with its first
    letter capitalized (awsvpcConfiguration is AwsvpcConfiguration, and BOOL
    stays BOOL; measured)."""
    return member[0].upper() + member[1:]


def sdk_member(member: str) -> str:
    """A member's name in an SDK integration, arguments and results alike: the
    AWS SDK for Java v2's, which lowercases the capitals the name starts with,
    all but the last of them when a lowercase letter follows, with its first
    letter capitalized. DBInstanceIdentifier is DbInstanceIdentifier, ACL is
    Acl, BOOL is Bool, and MultiAZ stays MultiAZ (measured)."""
    run = len(member) - len(member.lstrip(string.ascii_uppercase))
    if run == len(member):
        lowered = member.lower()
    elif run > 1 and member[run].islower():
        lowered = member[: run - 1].lower() + member[run - 1 :]
    else:
        lowered = member[:run].lower() + member[run:]
    return lowered[0].upper() + lowered[1:]


def required(operation: OperationModel, spell: Callable[[str], str]) -> frozenset[str]:
    """Required Arguments keys, idempotency tokens included: Step Functions
    does not fill them in as the SDKs do."""
    shape = operation.input_shape
    if shape is None:
        return frozenset()
    return frozenset(spell(name) for name in shape.required_members)


def arguments(operation: OperationModel, spell: Callable[[str], str]) -> frozenset[str]:
    """Arguments keys, which Step Functions writes in PascalCase."""
    shape = operation.input_shape
    return frozenset(spell(name) for name in shape.members) if shape else frozenset()


def shape_type(
    shape: Shape | None, depth: int, spell: Callable[[str], str]
) -> Type | None:
    """The JSON type of a botocore shape, its members spelled as the
    integration spells them. Blobs and timestamps are left unknown, and
    recursive shapes stop after a few levels."""
    if shape is None or depth > 6:
        return None
    if isinstance(shape, StructureShape):
        return Type(
            frozenset({OBJECT}),
            fields=tuple(
                (spell(name), shape_type(member, depth + 1, spell))
                for name, member in shape.members.items()
            ),
        )
    if isinstance(shape, ListShape):
        return of(ARRAY, items=shape_type(shape.member, depth + 1, spell))
    if isinstance(shape, MapShape):
        return of(OBJECT, values=shape_type(shape.value, depth + 1, spell))
    kind = {
        "string": STRING,
        "integer": NUMBER,
        "long": NUMBER,
        "float": NUMBER,
        "double": NUMBER,
        "boolean": BOOLEAN,
    }.get(shape.type_name)
    return of(kind) if kind else None
