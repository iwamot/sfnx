from sfnx import TaskFailed, aws, state_machine

SLACK = "arn:aws:events:us-east-1:123456789012:connection/slack/0a1b2c3d"
TASK_FAILED = {"ErrorEquals": [TaskFailed], "MaxAttempts": 2, "IntervalSeconds": 60}


@state_machine
def nightly_etl(input):
    """Run the nightly Glue job, then count the rows it wrote with Athena; if the job fails for good, try to post to Slack, then fail."""
    day: str = input["day"]
    try:
        # .sync waits for the job run to finish and fails the Task if it fails.
        aws.optimized.glue.start_job_run(
            JobName="nightly-etl",
            Arguments={"--day": day},
            pattern=".sync",
            retry=[TASK_FAILED],
        )
    except Exception as e:
        aws.optimized.http.invoke(
            ApiEndpoint="https://slack.com/api/chat.postMessage",
            Method="POST",
            Authentication={"ConnectionArn": SLACK},
            RequestBody={
                "channel": "#etl",
                "text": f"nightly-etl failed for {day}: {e}",
            },
        )
        raise
    query = aws.optimized.athena.start_query_execution(
        QueryString="SELECT count(*) FROM sales WHERE day = ?",
        ExecutionParameters=[f"'{day}'"],
        WorkGroup="primary",
        pattern=".sync",
    )
    rows = aws.optimized.athena.get_query_results(
        QueryExecutionId=query["QueryExecution"]["QueryExecutionId"]
    )
    return {
        "day": day,
        "rows": int(rows["ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"]),
    }
