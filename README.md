# s3-zip-archiver-sam

A Lambda function, packaged as a container image and deployed with AWS SAM,
that compresses every new JSON object uploaded to an S3 bucket into a ZIP,
writes the ZIP back to the same bucket and deletes the original.

Everything (VPC, bucket, function, failure queue) is in one stack defined in
[`template.yaml`](template.yaml).

## How it works

```
 on-prem export                     VPC 10.20.0.0/16 (no IGW, no NAT)
 ─────────────                     ┌──────────────────────────────────────────┐
   PUT results/abc.json  ──►  S3   │  private subnet A      private subnet B   │
                           bucket  │   ┌───────────────────────────────────┐  │
                             │     │   │ ArchiverFunction:live (container) │  │
       s3:ObjectCreated:*    │     │   └───────────────┬───────────────────┘  │
       suffix = .json  ──────┴────►│                   │ GET / PUT / DELETE   │
                                   │           S3 gateway endpoint            │
                                   └───────────────────┼──────────────────────┘
                                                       ▼
                                   results/abc.json.zip written, results/abc.json deleted

   failed after retries ──► SQS FailedEventsQueue
```

1. An object ending in `.json` lands in the bucket and S3 invokes the `live`
   alias of the function asynchronously.
2. The function streams the object into a ZIP in `/tmp` and uploads it as
   `<original key>.zip`.
3. It checks the ZIP in S3 has the size it wrote, checks the original hasn't
   been replaced in the meantime (ETag), and only then deletes the original.
4. If an invocation keeps failing, Lambda retries twice and then sends the
   event to the SQS queue.

## Repository layout

```
archiver/app.py           handler
archiver/Dockerfile       image based on public.ecr.aws/lambda/python:3.12
archiver/requirements.txt runtime deps (pinned boto3)
tests/test_app.py         unit tests against moto
template.yaml             VPC, bucket, function, queue, log group
samconfig.toml            stack name / region / deploy defaults
Makefile                  test, build, deploy, rollback
```

## Design notes

**No trigger loop.** The ZIP is written to the same bucket, so a notification
on every object would invoke the function on its own output forever. The
notification is filtered on the `SourceSuffix` parameter (`.json` by default),
a template rule refuses `.zip` as a value, and the handler also ignores `.zip`
keys in case the filter is ever widened by hand.

**Private subnets without NAT.** The function only needs S3, so the VPC has no
internet gateway and no NAT gateway. S3 traffic goes through a gateway
endpoint, which is free, and the endpoint policy only allows this stack's
bucket. Logs don't need a network path: Lambda ships them to CloudWatch
outside the VPC. See the cost section for why NAT would be a very expensive
choice here.

**Safe delete.** S3 delivers notifications at least once and a producer might
re-upload the same key. So the handler treats a missing source object as
"already done", verifies the uploaded ZIP before deleting, and does not delete
an original whose ETag changed while it was being archived. Bucket versioning
is deliberately off; with it on, "deleting" the original would only add a
delete marker and save nothing.

**Versions and rollback.** `AutoPublishAlias: live` publishes an immutable
version on each deploy and S3 always invokes the alias. The Makefile passes
the git commit as `ReleaseId`, which is set as an environment variable, so a
deploy of a new commit always produces a new version (together with
`AutoPublishAliasAllProperties`), even when the image is byte-for-byte the
same. Rolling back means moving the alias to a previous version.

**Circular dependency.** A bucket notification that targets a function whose
IAM policy `!Ref`s the same bucket is a cycle CloudFormation refuses to
create. The bucket has a deterministic name
(`<stack>-<account>-<region>`) and the policy is built from that string.

## Deploying

Requirements: AWS CLI, SAM CLI, Docker, credentials for the target account.

```bash
make test      # unit tests (pip install -r requirements-dev.txt first)
make deploy    # sam build + sam deploy, ReleaseId = current git commit
```

`samconfig.toml` uses `resolve_s3` / `resolve_image_repos`, so SAM creates the
artifact bucket and the ECR repository on first deploy and no account-specific
values live in the repo.

### Trying it

```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name s3-zip-archiver \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)

aws s3 cp sample.json s3://$BUCKET/results/sample.json
sleep 10
aws s3 ls s3://$BUCKET/results/
# results/sample.json.zip
```

### Deployment check (free tier, personal account)

Deployed to `ap-southeast-1` on 2026-09-14 from commit `14d9ff4`. Stack
creation took about 5 minutes. Account id replaced with `<account>`.

What was checked after the deploy:

```
bucket notification   arn:aws:lambda:ap-southeast-1:<account>:function:s3-zip-archiver-ArchiverFunction-…:live
                      s3:ObjectCreated:*  suffix=.json
versions              $LATEST 14d9ff4 / 1 14d9ff4, alias live -> 1
function              PackageType Image, 1024 MB, x86_64, 2 private subnets + 1 security group
VPC routes            10.20.0.0/16 local, pl-6fa54006 (S3) -> vpce-…
internet gateways     0
NAT gateways          0
```

Uploaded five copies of a 10.4 MB JSON file (synthetic video-analysis output:
per-frame detections, bounding boxes, audio stats), one file with spaces in
its key and one `.zip`:

```
results/2026/09/14/vid-000121.json.zip   1700604
results/2026/09/14/vid-000122.json.zip   1700604
results/2026/09/14/vid-000123.json.zip   1700604
results/2026/09/14/vid-000124.json.zip   1700604
results/2026/09/14/vid-000125.json.zip   1700604
results/already.zip                           20   <- not processed
results/run 7/out file.json.zip              172
```

All `.json` originals were gone within a few seconds, the unzipped content's
sha256 matched the source file, and the failed-events queue stayed at 0.

From the function's `REPORT` lines:

| | duration | billed | max memory |
|---|---|---|---|
| cold start (init 1900 ms) | 803 ms | 2704 ms | 105 MB |
| warm, 10.4 MB file (4 runs) | 698–733 ms, avg 715 ms | same | 111 MB |

Compression ratio on this data was 6.44x (10,952,097 -> 1,700,604 bytes) and
each invocation wrote about 507 bytes of logs. These are the numbers used in
the cost section.

### Rolling back

```bash
aws lambda list-versions-by-function --function-name <FunctionName> \
  --query "Versions[].[Version,Environment.Variables.RELEASE_ID]" --output table

make rollback VERSION=3
```

Note that the next `make deploy` moves the alias forward again; to stay on an
old release, redeploy that commit.

### Removing the stack

```bash
aws s3 rm s3://$BUCKET --recursive
sam delete --stack-name s3-zip-archiver
```

The bucket has to be empty before CloudFormation can delete it. Deleting a
VPC-attached function can take a while because Lambda releases its network
interfaces asynchronously.

## Assumptions

- "Every time a new object is added" is read as every new *source* object.
  The ZIPs the function writes are objects too, and processing them would
  loop, so the trigger is limited to the configured suffix.
- One ZIP per source object, stored next to it as `<key>.zip`.
- Region is `ap-southeast-1`; prices in the cost section are for that region.
