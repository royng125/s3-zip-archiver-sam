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


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # keys arrive url-encoded in the event ("my file.json" -> "my+file.json")
        key = unquote_plus(record["s3"]["object"]["key"])
        results.append(archive_object(bucket, key))
    return {"results": results}


def archive_object(bucket, key):
    if key.endswith(ZIP_SUFFIX):
        # Every new object triggers us, including the zips we write back.
        # This check is what stops the loop, so it has to stay first.
        # debug level: at full volume this path runs once per archived object.
        logger.debug("skipping %s, already a zip", key)
        return {"key": key, "status": "skipped"}

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as err:
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
        buf.seek(0)
        s3.upload_fileobj(
            buf,
            bucket,
            zip_key,
            ExtraArgs={
                "ContentType": "application/zip",
                "Metadata": {"source-etag": etag.strip('"')},
            },
        )

    uploaded = s3.head_object(Bucket=bucket, Key=zip_key)
    if uploaded["ContentLength"] != zip_size:
        raise RuntimeError(
            f"size mismatch for {zip_key}: wrote {zip_size}, S3 has {uploaded['ContentLength']}"
        )

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
