import logging
import os
import tempfile
import zipfile
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")

ZIP_SUFFIX = ".zip"
CHUNK_SIZE = 8 * 1024 * 1024
PUT_ATTEMPTS = 3


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # keys arrive url-encoded in the event ("my file.json" -> "my+file.json")
        key = unquote_plus(record["s3"]["object"]["key"])
        # S3 orders events for the same key by this hex value
        sequencer = record["s3"]["object"].get("sequencer")
        # ETag of the version this event is about
        event_etag = record["s3"]["object"].get("eTag")
        results.append(archive_object(bucket, key, sequencer, event_etag))
    return {"results": results}


def archive_object(bucket, key, sequencer=None, event_etag=None):
    if key.endswith(ZIP_SUFFIX):
        # Every new object triggers us, including the zips we write back.
        # This check is what stops the loop, so it has to stay first.
        # debug level: at full volume this path runs once per archived object.
        logger.debug("skipping %s, already a zip", key)
        return {"key": key, "status": "skipped"}

    # Read exactly the version the event describes. If the key was overwritten
    # after the event, the current object has its own event; archiving it here
    # would label newer content with this event's older sequencer.
    condition = {"IfMatch": '"%s"' % event_etag.strip('"')} if event_etag else {}
    try:
        obj = s3.get_object(Bucket=bucket, Key=key, **condition)
    except ClientError as err:
        if err.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
            logger.info("%s was overwritten after this event, skipping", key)
            return {"key": key, "status": "superseded"}
        if err.response["Error"]["Code"] == "NoSuchKey":
            # S3 notifications are at-least-once. A redelivered event can show up
            # after the first invocation already deleted the original.
            logger.info("%s no longer exists, nothing to do", key)
            return {"key": key, "status": "missing"}
        raise

    etag = obj["ETag"]
    zip_key = key + ZIP_SUFFIX

    with tempfile.TemporaryFile() as buf:
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # stream into the archive instead of reading the whole object into memory
            with zf.open(os.path.basename(key), "w", force_zip64=True) as entry:
                for chunk in obj["Body"].iter_chunks(CHUNK_SIZE):
                    entry.write(chunk)
        zip_size = buf.tell()
        outcome = write_zip(bucket, zip_key, buf, etag, sequencer)

    if outcome == "stale":
        # A zip made from a newer upload of this key is already in place, so
        # this version was overwritten and is not ours to delete.
        logger.warning("%s: newer version already archived, leaving this one", key)
        return {"key": key, "zip_key": zip_key, "status": "stale"}

    # Conditional delete: S3 removes the object only if it is still the version
    # we compressed. The previous HEAD-then-DELETE left a gap where a new upload
    # landing in between was deleted without ever being zipped.
    try:
        s3.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
    except ClientError as err:
        status = err.response["ResponseMetadata"]["HTTPStatusCode"]
        if status in (409, 412):
            # 412: replaced since we read it. 409: a concurrent write won.
            # Either way the newer object has its own event coming.
            logger.warning("%s changed while archiving, leaving it in place", key)
            return {"key": key, "zip_key": zip_key, "status": "kept-original"}
        if status == 404:
            # a duplicate delivery of the same event got there first
            logger.info("%s already removed by another invocation", key)
            return {"key": key, "zip_key": zip_key, "status": "missing"}
        raise

    logger.info(
        "archived s3://%s/%s -> %s (%d -> %d bytes)",
        bucket, key, zip_key, obj["ContentLength"], zip_size,
    )
    return {
        "key": key,
        "zip_key": zip_key,
        "status": "archived",
        "original_bytes": obj["ContentLength"],
        "zip_bytes": zip_size,
    }


def write_zip(bucket, zip_key, body, source_etag, sequencer):
    """Upload the zip without ever replacing one made from a newer upload.

    Without this, a slow invocation for an old version could finish after the
    invocation for the new version and put the old content back on top.

    Returns "written", "already-there" when the zip for this exact source
    version exists (e.g. a retry after failing between upload and delete),
    or "stale" when the existing zip comes from a newer upload.
    """
    source_etag = source_etag.strip('"')
    metadata = {"source-etag": source_etag}
    if sequencer:
        metadata["source-sequencer"] = sequencer

    for _ in range(PUT_ATTEMPTS):
        try:
            existing = s3.head_object(Bucket=bucket, Key=zip_key)
        except ClientError as err:
            if err.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            existing = None

        if existing is None:
            condition = {"IfNoneMatch": "*"}
        else:
            theirs = existing.get("Metadata", {})
            if theirs.get("source-etag") == source_etag:
                return "already-there"
            if not is_newer(sequencer, theirs.get("source-sequencer")):
                return "stale"
            condition = {"IfMatch": existing["ETag"]}

        body.seek(0)
        try:
            # Single PUT, fine up to 5 GB of zip. S3 rejects the upload if the
            # bytes it received don't match the SHA-256 the SDK sends along.
            s3.put_object(
                Bucket=bucket,
                Key=zip_key,
                Body=body,
                ContentType="application/zip",
                ChecksumAlgorithm="SHA256",
                Metadata=metadata,
                **condition,
            )
            return "written"
        except ClientError as err:
            if err.response["ResponseMetadata"]["HTTPStatusCode"] not in (409, 412):
                raise
            # someone else wrote the zip between our HEAD and PUT, look again

    raise RuntimeError(f"{zip_key} kept changing while writing it")


def is_newer(ours, theirs):
    # Sequencers of events for the same key compare as hex numbers. Without one
    # of our own (manual invoke, batch job) we can't claim to be newer; a zip
    # without one was written before sequencers were recorded.
    if not ours:
        return False
    if not theirs:
        return True
    return int(ours, 16) > int(theirs, 16)
