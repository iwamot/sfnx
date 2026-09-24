from sfnx import aws, inline_map, parallel, state_machine


@state_machine
def publish(input):
    """Resize every photo of an album, a few at a time, then notify the owner and index the album at once."""
    album: str = input["album"]
    photos: list = input["photos"]

    def resize(photo, index):
        # The index names the output, so two photos never overwrite each other.
        return aws.optimized.lambda_.invoke(
            FunctionName="resize",
            Payload={"source": photo, "target": f"{album}/{index}.jpg"},
        )["Payload"]

    resized = inline_map(resize, photos, max_concurrency=4)

    def notify():
        return aws.optimized.sns.publish(
            TopicArn="arn:aws:sns:us-east-1:123456789012:albums",
            Message=f"{len(resized)} photos of {album} are ready",
        )["MessageId"]

    def index():
        return aws.optimized.lambda_.invoke(
            FunctionName="index", Payload={"album": album, "photos": resized}
        )["Payload"]

    notice, entry = parallel(notify, index)
    return {"album": album, "photos": resized, "index": entry, "notice": notice}
