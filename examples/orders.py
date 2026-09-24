from typing import TypedDict

from sfnx import Timeout, aws, state_machine


class Item(TypedDict):
    sku: str
    quantity: int


class Order(TypedDict):
    id: str
    items: list[Item]


class OutOfStock(Exception):
    pass


@state_machine(timeout=300)
def fulfill(input: Order):
    """Reserve every item of an order, then charge for it."""
    items = input["items"]
    for item in items:
        try:
            aws.sdk.dynamodb.update_item(
                TableName="stock",
                Key={"sku": {"S": item["sku"]}},
                UpdateExpression="SET quantity = quantity - :n",
                ConditionExpression="quantity >= :n",
                ExpressionAttributeValues={":n": {"N": str(item["quantity"])}},
                retry=[{"ErrorEquals": [Timeout], "MaxAttempts": 3}],
            )
        except aws.sdk.dynamodb.errors.ConditionalCheckFailedException:
            raise OutOfStock(f"{item['sku']} is out of stock") from None
    receipt = aws.optimized.lambda_.invoke(FunctionName="charge", Payload=input)
    return {"order": input["id"], "receipt": receipt["Payload"]}
