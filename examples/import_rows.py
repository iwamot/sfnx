from sfnx import ExceedToleratedFailureThreshold, aws, distributed_map, state_machine


@state_machine
def import_rows(input):
    """Import every row of a CSV file in S3 as a child execution, tolerating up to 5% failed rows."""
    bucket: str = input["bucket"]
    key: str = input["key"]
    topic = "arn:aws:sns:us-east-1:123456789012:imports"

    def load(row):
        reply = aws.optimized.lambda_.invoke(FunctionName="import-row", Payload=row)
        return reply["Payload"]

    try:
        run = distributed_map(
            load,
            source={
                "Resource": "arn:aws:states:::s3:getObject",
                "ReaderConfig": {"InputType": "CSV", "CSVHeaderLocation": "FIRST_ROW"},
                "Arguments": {"Bucket": bucket, "Key": key},
            },
            result={
                "Resource": "arn:aws:states:::s3:putObject",
                "Arguments": {"Bucket": bucket, "Prefix": "results"},
                "WriterConfig": {"OutputType": "JSONL", "Transformation": "COMPACT"},
            },
            max_concurrency=100,
            tolerated_failure_percentage=5,
            label="rows",
            execution_type="EXPRESS",
        )
    except ExceedToleratedFailureThreshold:
        aws.optimized.sns.publish(
            TopicArn=topic, Message=f"{key}: more than 5% of the rows failed"
        )
        raise
    counts = aws.sdk.sfn.describe_map_run(MapRunArn=run["MapRunArn"])["ItemCounts"]
    return {
        "succeeded": counts["Succeeded"],
        "failed": counts["Failed"],
        "results": run["ResultWriterDetails"]["Key"],
    }
