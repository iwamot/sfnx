from sfnx import aws, context, state_machine, wait


class TranscriptionFailed(Exception):
    pass


class StillRunning(Exception):
    pass


POLL_SECONDS = 30
MAX_POLLS = 20


@state_machine
def transcribe(input):
    """Transcribe an audio file, polling the job until it finishes."""
    name = context["Execution"]["Name"]
    # Transcribe has no .sync integration, so the machine polls the job itself.
    aws.sdk.transcribe.start_transcription_job(
        TranscriptionJobName=name,
        Media={"MediaFileUri": input["uri"]},
        IdentifyLanguage=True,
    )
    polls = 0
    while polls < MAX_POLLS:
        wait(POLL_SECONDS)
        job = aws.sdk.transcribe.get_transcription_job(TranscriptionJobName=name)[
            "TranscriptionJob"
        ]
        status = job["TranscriptionJobStatus"]
        if status == "COMPLETED":
            return {"transcript": job["Transcript"]["TranscriptFileUri"]}
        if status == "FAILED":
            raise TranscriptionFailed(job["FailureReason"])
        polls += 1
    raise StillRunning(f"{name} is still running after {MAX_POLLS} polls")
