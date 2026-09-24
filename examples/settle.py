from sfnx import aws, jsonata, state_machine


@state_machine
def settle(input):
    """Load the charges of a day and total them per currency."""
    charges: list = aws.optimized.lambda_.invoke(
        FunctionName="load-charges", Payload={"date": input["date"]}
    )["Payload"]
    if not charges:
        return {"date": input["date"], "charges": 0, "totals": {}}
    # Totals per currency are a grouping, which is written out in JSONata.
    totals: dict = jsonata(
        "$merge($map($distinct($xs.currency), function($c) {"
        " {$c: $sum($xs[currency = $c].amount)} }))",
        xs=charges,
    )
    return {"date": input["date"], "charges": len(charges), "totals": totals}
