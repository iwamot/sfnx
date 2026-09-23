# The Hello World template of the Step Functions console ("Run Hello World"),
# written with sfnx. The definition it compiles to runs the same way as the
# template's.
import time
from datetime import datetime

from sfnx import QueryEvaluationError, context, parallel, state_machine, wait


class NotHelloWorld(Exception):
    # The template's error name has spaces, which a class name cannot spell.
    error = "Not a Hello World Example"


@state_machine
def hello_world():
    """A Hello World example that demonstrates various state types."""
    is_hello_world_example = True
    execution_wait_time_in_seconds = 3
    checkpoint_count = 0
    if not is_hello_world_example:
        raise NotHelloWorld()
    wait(execution_wait_time_in_seconds)
    checkpoint_count += 1

    def format_execution_start_date():
        return datetime.fromisoformat(context["State"]["EnteredTime"]).strftime("%m/%d")

    def snapshot_execution_elapsed_time():
        started = datetime.fromisoformat(context["Execution"]["StartTime"]).timestamp()
        return time.time() - started

    try:
        start_date, elapsed = parallel(
            format_execution_start_date, snapshot_execution_elapsed_time
        )
    except QueryEvaluationError:
        start_date, elapsed = "Failed to format", "Failed to calculate"
    checkpoint_count += 1
    return {
        "Summary": f"This Hello World execution began on {start_date}. The state"
        f" machine ran for {elapsed} seconds before the snapshot was taken, passing"
        f" through {checkpoint_count} checkpoints, and has successfully completed."
    }
