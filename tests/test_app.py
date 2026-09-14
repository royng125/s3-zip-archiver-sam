import io
import json
import zipfile
from urllib.parse import quote_plus

import boto3
import pytest
from moto import mock_aws

REGION = "ap-southeast-1"
BUCKET = "archiver-test-bucket"


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield client


@pytest.fixture
def app(s3, monkeypatch):
    import app as module

    monkeypatch.setattr(module, "s3", s3)
    return module


def event_for(key):
    return {
        "Records": [
            {"s3": {"bucket": {"name": BUCKET}, "object": {"key": quote_plus(key)}}}
        ]
    }


def keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_archives_object_and_removes_original(s3, app):
    body = json.dumps({"video_id": "abc123", "frames": list(range(2000))}).encode()
    s3.put_object(Bucket=BUCKET, Key="results/abc123.json", Body=body)

    out = app.handler(event_for("results/abc123.json"), None)

    assert out["results"][0]["status"] == "archived"
    assert keys(s3) == ["results/abc123.json.zip"]

    zipped = s3.get_object(Bucket=BUCKET, Key="results/abc123.json.zip")["Body"].read()
    with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
        assert zf.namelist() == ["abc123.json"]
        assert zf.read("abc123.json") == body


def test_key_with_spaces(s3, app):
    s3.put_object(Bucket=BUCKET, Key="results/run 7/out file.json", Body=b"{}")

    app.handler(event_for("results/run 7/out file.json"), None)

    assert keys(s3) == ["results/run 7/out file.json.zip"]


@pytest.mark.parametrize("key", ["exports/run-42.ndjson", "exports/no-extension", "exports/REPORT.JSON"])
def test_any_new_object_is_archived(s3, app, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b'{"a": 1}\n{"a": 2}\n')

    out = app.handler(event_for(key), None)

    assert out["results"][0]["status"] == "archived"
    assert keys(s3) == [key + ".zip"]


def test_zip_objects_are_ignored(s3, app):
    s3.put_object(Bucket=BUCKET, Key="results/old.json.zip", Body=b"PK")

    out = app.handler(event_for("results/old.json.zip"), None)

    assert out["results"][0]["status"] == "skipped"
    assert keys(s3) == ["results/old.json.zip"]


def test_redelivered_event_is_a_noop(s3, app):
    s3.put_object(Bucket=BUCKET, Key="a.json", Body=b'{"a": 1}')

    app.handler(event_for("a.json"), None)
    out = app.handler(event_for("a.json"), None)

    assert out["results"][0]["status"] == "missing"
    assert keys(s3) == ["a.json.zip"]


def zip_content(s3, zip_key):
    data = s3.get_object(Bucket=BUCKET, Key=zip_key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(zf.namelist()[0])


def test_overwrite_right_before_delete_is_not_lost(s3, app, monkeypatch):
    # Used to be a real data loss: the ETag check passed, the producer uploaded
    # a new version, and the unconditional delete removed it.
    s3.put_object(Bucket=BUCKET, Key="d.json", Body=b'{"v": 1}')
    real_delete = s3.delete_object

    def overwrite_then_delete(**kwargs):
        s3.put_object(Bucket=BUCKET, Key="d.json", Body=b'{"v": 2}')
        return real_delete(**kwargs)

    monkeypatch.setattr(s3, "delete_object", overwrite_then_delete)
    out = app.handler(event_for("d.json"), None)
    monkeypatch.setattr(s3, "delete_object", real_delete)

    assert out["results"][0]["status"] == "kept-original"
    assert s3.get_object(Bucket=BUCKET, Key="d.json")["Body"].read() == b'{"v": 2}'

    # the new version's own event then archives it
    app.handler(event_for("d.json"), None)
    assert keys(s3) == ["d.json.zip"]
    assert zip_content(s3, "d.json.zip") == b'{"v": 2}'


def test_duplicate_invocation_deleting_first_is_not_an_error(s3, app, monkeypatch):
    # Two deliveries of the same event running at once: the other one deletes
    # the original first. Previously this surfaced as a 404 from HEAD and a
    # failed invocation that Lambda retried.
    s3.put_object(Bucket=BUCKET, Key="c.json", Body=b'{"c": 1}')
    real_delete = s3.delete_object

    def other_invocation_deletes_first(**kwargs):
        real_delete(Bucket=BUCKET, Key="c.json")
        return real_delete(**kwargs)

    monkeypatch.setattr(s3, "delete_object", other_invocation_deletes_first)

    out = app.handler(event_for("c.json"), None)

    assert out["results"][0]["status"] == "missing"
    assert keys(s3) == ["c.json.zip"]


def test_original_overwritten_mid_flight_is_not_deleted(s3, app, monkeypatch):
    s3.put_object(Bucket=BUCKET, Key="b.json", Body=b'{"v": 1}')
    real_upload = s3.upload_fileobj

    def upload_then_overwrite(*args, **kwargs):
        real_upload(*args, **kwargs)
        s3.put_object(Bucket=BUCKET, Key="b.json", Body=b'{"v": 2}')

    monkeypatch.setattr(s3, "upload_fileobj", upload_then_overwrite)

    out = app.handler(event_for("b.json"), None)

    assert out["results"][0]["status"] == "kept-original"
    assert s3.get_object(Bucket=BUCKET, Key="b.json")["Body"].read() == b'{"v": 2}'
