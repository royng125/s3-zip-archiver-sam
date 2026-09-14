# s3-zip-archiver-sam

A Lambda function, packaged as a container image and deployed with AWS SAM,
that compresses every new object uploaded to an S3 bucket into a ZIP, writes
the ZIP back to the same bucket and deletes the original.

Everything (VPC, bucket, function, failure queue, alarms) is in one stack
defined in [`template.yaml`](template.yaml).

## How it works

```
 on-prem export                     VPC 10.20.0.0/16 (no IGW, no NAT)
 ─────────────                     ┌──────────────────────────────────────────┐
   PUT results/abc.json  ──►  S3   │  private subnet A      private subnet B   │
                           bucket  │   ┌───────────────────────────────────┐  │
                             │     │   │ ArchiverFunction:live (container) │  │
       s3:ObjectCreated:*    │     │   └───────────────┬───────────────────┘  │
       every new object ─────┴────►│                   │ GET/HEAD/PUT/DELETE  │
                                   │           S3 gateway endpoint            │
                                   └───────────────────┼──────────────────────┘
                                                       ▼
                             results/abc.json.zip written, results/abc.json deleted
                             (the new .zip triggers the function too; it returns at once)

   failed after retries ──► SQS FailedEventsQueue ──► CloudWatch alarm ──► SNS
```

1. Any new object lands in the bucket and S3 invokes the `live` alias of the
   function asynchronously. Keys ending in `.zip` return immediately.
2. The function streams the object into a ZIP in `/tmp`.
3. It looks at the existing `<key>.zip`, if any. A zip made from this exact
   source version means the work was already done; a zip made from a newer
   upload means this event is stale and the function stops. Otherwise it
   uploads the zip with a conditional PUT and a SHA-256 checksum, recording the
   source ETag and the event's sequencer in the zip's metadata.
4. It deletes the original with a conditional delete (`If-Match` on the ETag it
   read), so a version uploaded in the meantime is never removed.
5. If an invocation keeps failing, Lambda retries twice and then sends the
   event to the SQS queue, which raises an alarm.

## Repository layout

```
archiver/app.py             handler
archiver/Dockerfile         image based on public.ecr.aws/lambda/python:3.12
archiver/requirements.txt   runtime deps (pinned boto3)
tests/test_app.py           unit tests against moto, including the race conditions below
template.yaml               VPC, bucket, function, queue, alarms, log group
samconfig.toml              stack name / region / deploy defaults
Makefile                    test, build, deploy, smoke, rollback
scripts/smoke_test.sh       end-to-end check of a deployed stack
scripts/cost_estimate.py    reproduces the cost analysis
.github/workflows/ci.yml    tests, cfn-lint and image build on every push
```

## Design notes

**No trigger loop.** The task asks for the function to run every time a new
object is added, and the ZIPs it writes back are new objects too. The
notification has no key filter; the first thing the handler does is return for
keys ending in `.zip`, before any S3 call. If that check were ever broken,
Lambda's recursive loop detection for S3 stops the chain after roughly 16
invocations, but that's a backstop, not the design. The price is one very short
extra invocation per archived object, included in the cost section. A
producer's own `.zip` uploads are skipped the same way, which is fine since
they're already compressed.

**Private subnets without NAT.** The function only needs S3, so the VPC has no
internet gateway and no NAT gateway. S3 traffic goes through a gateway
endpoint, which is free, and the endpoint policy only allows this stack's
bucket. The bucket policy rejects any request not made over TLS. Logs don't
need a network path: Lambda ships them to CloudWatch outside the VPC. See the
cost section for why NAT would be a very expensive choice here.

**Concurrency and safe delete.** S3 notifications are delivered at least once
and in no guaranteed order, and a producer can upload a key again while the
previous version is still being archived. An earlier version of the handler
got three interleavings wrong. Each one is now reproduced by a test in
`tests/test_app.py`:

```
1. Overwrite between the ETag check and the delete  -> the new version was deleted, never zipped
   A (v1)    GET v1 ── zip ── PUT zip ── HEAD: etag ok ─────────── DELETE  ✗ deletes v2
   producer                                          PUT v2 ──┘
   now       DELETE If-Match: etag(v1)  -> 412, v2 stays, its own event archives it

2. Two deliveries of the same event racing          -> spurious errors and retries
   A1        ... DELETE ─ 204
   A2        ... HEAD original ─ 404, unhandled
   now       DELETE If-Match ─ 404 -> "missing", no error

3. A slow invocation for an older version           -> old content put back over the new zip
   A (v1)    GET v1 ──────────────── slow ─────────────────── PUT zip(v1)  ✗ overwrites zip(v2)
   B (v2)           GET v2 ── PUT zip(v2) ── DELETE v2
   now       A sees a zip made from a newer sequencer: writes nothing, deletes nothing
```

The rules the handler follows:

| situation | action |
|---|---|
| original is gone | nothing to do (`missing`) |
| zip exists, made from this exact source ETag | skip the upload, run the conditional delete; this is how a retry after failing between upload and delete finishes |
| zip exists, made from a newer event sequencer | stop, keep everything (`stale`) |
| no zip, or zip from an older event | PUT with `If-None-Match: *` / `If-Match: <zip etag>` so two writers can't both win, then conditional delete |
| original changed before the delete | leave it (`kept-original`); its own event handles it |

Limits of this: an event without a sequencer (a manual invoke, a batch job)
never replaces a zip made from a different version; and the zip is a single
PUT, so one archive can be at most 5 GB. Bucket versioning is deliberately off;
with it on, "deleting" the original would only add a delete marker and save
nothing.

**Versions and rollback.** `AutoPublishAlias: live` publishes an immutable
version and S3 always invokes the alias, never `$LATEST`. The Makefile passes
the git commit as `ReleaseId`. On its own, putting that value in an
environment variable was not enough: SAM decides whether to publish from a
hash of the template taken before parameters are resolved, so the second
deploy updated `$LATEST` without creating a version. `ReleaseId` is now also
passed as `AutoPublishCodeSha256`, which makes every release publish a new
version even when the image is byte-for-byte the same. Old versions are
retained, so rolling back means moving the alias to one of them.

**Circular dependency.** A bucket notification that targets a function whose
IAM policy `!Ref`s the same bucket is a cycle CloudFormation refuses to
create. The bucket has a deterministic name
(`<stack>-<account>-<region>`) and the policy is built from that string.

## Deploying

Requirements: AWS CLI, SAM CLI, Docker, credentials for the target account.

```bash
pip install -r requirements-dev.txt
make test      # unit tests
make deploy    # refuses uncommitted changes, then sam build + sam deploy with ReleaseId = git commit
make smoke     # end-to-end check of the deployed stack (scripts/smoke_test.sh)
```

Deploy through `make deploy`. Two guards keep "every deployment is a new,
traceable version" true:

- `ReleaseId` has no default, so a bare `sam deploy` fails instead of reusing
  the previous code hash and quietly publishing nothing.
- `make deploy` refuses to run with uncommitted changes, so the `RELEASE_ID`
  on a version is always the commit that was actually deployed. Version 2
  below predates this guard: it was deployed from the working tree while the
  version fix was still uncommitted, so its `RELEASE_ID` says `3470e19` but it
  ran the template that was committed a minute later as `23048e7`.

To get alarm emails, add `AlarmEmail=you@example.com` to the parameter
overrides (and confirm the subscription email), or subscribe anything else to
the `AlarmTopicArn` output.

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

### Deployment checks (free tier, personal account)

Both deployments below went to `ap-southeast-1` in a personal account.
Account id replaced with `<account>`. `make smoke` repeats the checks.

#### First deployment, commit `14d9ff4`

Stack creation took about 5 minutes. What was checked after the deploy:

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

#### After the hardening changes, commit `b978ca7` (version 4)

Stack update took about a minute and a half including the image build.

```
bucket notification   arn:aws:lambda:ap-southeast-1:<account>:function:s3-zip-archiver-ArchiverFunction-…:live
                      s3:ObjectCreated:*  no key filter
versions              1 14d9ff4 / 2 3470e19 / 3 23048e7 / 4 b978ca7, alias live -> 4
alarms                Errors, Throttles, AsyncEventAge, failed-events queue: all OK
bucket policy         DenyPlainHttp (aws:SecureTransport = false)
VPC routes            10.20.0.0/16 local, pl-6fa54006 (S3); internet gateways 0; NAT gateways 0
account concurrency   limit 10 (see Scalability)
```

The handler relies on how S3 answers conditional requests, and the unit tests
use moto for that. The same requests against the real bucket, on `.zip` keys so
the function ignores them:

```
PUT    If-None-Match: *   on an existing key   -> 412 PreconditionFailed
PUT    If-Match: <wrong etag>                   -> 412 PreconditionFailed
DELETE If-Match: <wrong etag>                   -> 412 PreconditionFailed
DELETE If-Match: <right etag>                   -> 204
DELETE If-Match: <etag>  on a missing key       -> 404 NoSuchKey
```

Same answers as moto.

`make smoke` uploaded a JSON file, an NDJSON file, a key without an extension
and a producer `.zip`:

```
smoke/…/lines.ndjson.zip
smoke/…/no extension.zip
smoke/…/producer.zip          <- left alone
smoke/…/result.json.zip       unzipped entry has the same sha256 as the upload
failed-events queue           0
```

Then twenty 10.4 MB uploads, one at a time because of the concurrency limit,
with the `REPORT` lines grouped by whether the invocation archived something:

| invocation | runs | billed duration | max memory | log bytes |
|---|---|---|---|---|
| archive, 10.4 MB JSON | 20 | avg 743 ms (median 699, min 676, max 1,539) | 114 MB | 464 |
| triggered by its own zip | 20 | avg 2 ms | 114 MB | 258 |

Each zip carries `source-etag` and `source-sequencer` metadata and a
`ChecksumSHA256` verified by S3. The compression ratio was 6.44x again
(10,952,097 -> 1,700,596 bytes). These are the inputs of the cost section.

### Rolling back

```bash
aws lambda list-versions-by-function --function-name <FunctionName> \
  --query "Versions[].[Version,Environment.Variables.RELEASE_ID]" --output table

make rollback VERSION=3
```

Checked on the deployed stack after three deploys:

```
Version  RELEASE_ID
1        14d9ff4
2        3470e19     (see the note under Deploying)
3        23048e7     <- live

make rollback VERSION=2   -> live = 2
upload results/rollback-check.json
log stream 2026/09/13/[2]280a3cf1…  archived …/rollback-check.json
make rollback VERSION=3   -> live = 3
```

The `[2]` in the log stream name is the version that handled the event.

What a rollback does and doesn't cover:

```
make rollback VERSION=n moves the alias. A version is a snapshot of the function:

  rolled back with the version                 not part of the version (stays as currently deployed)
  ┌────────────────────────────────────────┐   ┌───────────────────────────────────────────┐
  │ image digest (the code)                │   │ security group rules, route table, VPC     │
  │ environment variables, memory, timeout │   │ endpoint policy, bucket policy             │
  │ subnet ids + security group ids        │   │ contents of the IAM role's policies        │
  │ IAM role ARN, architecture             │   │ bucket notification, SQS queue, alarms     │
  └────────────────────────────────────────┘   └───────────────────────────────────────────┘
```

- To undo an infrastructure change, redeploy the older commit with
  `make deploy` instead of moving the alias.
- A rollback happens outside CloudFormation. The stack still records the newer
  version as the alias target, so the next `make deploy` moves the alias
  forward again; to stay on an old release, redeploy that commit.
- A version runs the image digest it was published with, pulled from the ECR
  repository SAM created. If that image is deleted, for example by an ECR
  lifecycle rule added later, the version goes into a failed state and can no
  longer be rolled back to. The repository created here has no lifecycle
  policy; any cleanup rule should keep the images of every version you might
  still want.

### Removing the stack

```bash
aws s3 rm s3://$BUCKET --recursive
sam delete --stack-name s3-zip-archiver
```

The bucket has to be empty before CloudFormation can delete it. Deleting a
VPC-attached function can take a while because Lambda releases its network
interfaces asynchronously. `sam delete` also offers to remove the ECR
repository and artifact bucket SAM created for the stack.

## Assumptions

- "Every time a new object is added" is taken literally: every new object
  triggers the function. The only objects that are not compressed are ones
  whose key already ends in `.zip`, which includes the function's own output.
  The first version only triggered on `.json` keys, reading the task's
  "processing result is produced in JSON" as a filter; it was changed because
  that silently left `.ndjson`, extension-less or upper-case `.JSON` objects
  uncompressed.
- One ZIP per source object, stored next to it as `<key>.zip`, holding a single
  entry named after the last segment of the key.
- Region is `ap-southeast-1`; prices in the cost section are for that region.

## Cost analysis

### Assumptions

- 1,000,000 files per hour x 730 hours = **730 million files a month**,
  10 MB each, one invocation per file.
- Duration and log volume are the measured values from the deployment above:
  715 ms average at 1024 MB, ~507 bytes of logs per invocation.
- Compression ratio 6.44x, measured on synthetic JSON. Real video-analysis
  output could compress better or worse, and the storage numbers scale directly
  with this ratio.
- On-demand prices for `ap-southeast-1` from the AWS Price List API, free tier
  ignored. The producer's own uploads (and their PUT requests) already exist
  today, so they're not counted as part of this feature.

`scripts/cost_estimate.py` reproduces every number below:

```bash
python scripts/cost_estimate.py --duration-ms 715 --ratio 6.44 --log-bytes 507
```

### What the feature costs to run

| item | volume per month | USD / month |
|---|---|---|
| Lambda requests | 730M x $0.20 per 1M | 146 |
| Lambda compute, x86_64 | 521.95M GB-s x $0.0000166667 | 8,699 |
| S3 GET, read original | 730M x $0.0004 per 1K | 292 |
| S3 PUT, write zip | 730M x $0.005 per 1K | 3,650 |
| S3 HEAD x2, verify zip + check original | 1,460M x $0.0004 per 1K | 584 |
| S3 DELETE | free | 0 |
| CloudWatch Logs ingestion | ~345 GB x $0.70 | 241 |
| S3 gateway endpoint, in-region transfer | free | 0 |
| ECR image (207 MB) and SQS (failures only) | | < 1 |
| **total** | | **~13,600** |

Cold starts add a little on top: the image's 1.9 s init is billed, but at
~200 warm environments running continuously they are a very small share of
invocations.

### What it saves

One month of output is 6.80 PiB of raw JSON or 1.06 PiB zipped. Kept in
S3 Standard, that month of data costs:

| | size | USD per month it is stored |
|---|---|---|
| raw JSON (today) | 6.80 PiB | 164,528 |
| zipped | 1.06 PiB | 26,024 |
| **difference** | | **138,504** |

### Monthly figure

- **The feature itself costs about US$13,600 per month.**
- In the first month it removes about US$138,500 of storage, so the bill is
  roughly **US$124,900 lower** than without it.
- The gap widens every month because stored data accumulates while the
  processing cost stays flat. After a year of retention, storage would be
  around US$312K/month zipped against about US$1.97M/month raw.

### Ways to save more

Roughly in order of impact:

1. **Keep S3 traffic off NAT.** Already done here. The same design through a
   NAT gateway would add about **US$486,000 per month** in data processing,
   more than everything else combined.
2. **Compress before uploading.** If the on-prem exporter writes ZIP (or
   gzip/zstd) itself, the whole ~US$13.6K of processing disappears and upload
   bandwidth drops about 6x. This Lambda is the right tool when the producer
   can't be changed.
3. **Move old archives to a colder class.** Zipped data in Glacier Instant
   Retrieval costs ~US$5,500 per month of data instead of ~US$26,000 in
   Standard. Deep Archive is ~US$2,200, but lifecycle transitions are charged
   per object (US$43,800 for 730M objects), there's a 180-day minimum and
   retrieval takes hours, so it only pays off for data that is almost never
   read.
4. **Fewer, bigger objects.** PUT, HEAD and especially lifecycle transition
   costs are per object. Bundling files into one archive per minute or per
   video (S3 -> SQS -> batch consumer) divides those line items by the
   bundle size.
5. **arm64.** Graviton compute is 20% cheaper here, about US$1,740/month
   saved. Needs the image built for arm64.
6. **Right-size memory.** Max memory used was 111 MB out of 1024 MB. Lowering
   memory also lowers CPU, so duration will go up; the cheapest setting has to
   be measured (e.g. with AWS Lambda Power Tuning). Not measured here.
7. **Smaller wins.** Log successful archives at DEBUG in production, buy a
   Compute Savings Plan for the Lambda spend, and drop the verification HEAD
   calls (US$584) if the PUT response is considered enough.

## Scalability and bottlenecks

**Short answer:** it handles this volume without a redesign, but one Lambda
invocation per object is not the most cost-efficient shape at this scale, and
a few limits need attention before production.

At 1,000,000 files per hour the steady load is ~278 events per second. At
715 ms each that's **about 200 concurrent executions** on average, plus
whatever the producer's burstiness adds.

1. **Lambda concurrency quota.** The default regional limit is 1,000
   concurrent executions shared by every function in the account. A 5x burst
   reaches it. Throttled async events are retried for up to 6 hours
   (`MaximumEventAgeInSeconds`) and then land in the failed-events queue.
   Before production: request a quota increase, set reserved concurrency so
   this function can't starve others, and alarm on `Throttles`.
2. **The backlog is hard to see.** S3 -> Lambda is an async invoke with an
   internal queue; the only signal is `AsyncEventAge`. Putting SQS between S3
   and Lambda gives a visible queue depth, batching and a max concurrency
   setting on the event source mapping. I'd do that for production.
3. **S3 request rates per prefix.** S3 supports 3,500 PUT/COPY/POST/DELETE and
   5,500 GET/HEAD requests per second per partitioned prefix. Steady state
   here is ~834 write requests/s (producer PUT + zip PUT + DELETE) and ~834
   GET/HEAD/s. That's fine on average, but a burst into a single prefix can
   return `503 SlowDown` while S3 re-partitions. Spreading keys across
   prefixes (date/hour or a hash) avoids it.
4. **VPC limits.** Lambda uses shared Hyperplane ENIs per subnet and security
   group combination, so concurrency doesn't use one IP per execution. The
   `/20` subnets have ~4,091 addresses each and there's a per-VPC Hyperplane
   ENI quota. The S3 gateway endpoint has no bandwidth limit. Losing one AZ
   leaves the other subnet running.
5. **Originals pile up when it falls behind.** The raw object is deleted only
   after its ZIP exists. If throttling or errors build a backlog, raw data sits
   in Standard at full price, so backlog age and failed-events queue depth need
   alarms, and the queue needs a replay procedure.
6. **Object size.** The handler streams into `/tmp` (512 MB by default, up to
   10 GB) with a 120 s timeout. 10 MB takes under a second. Multi-GB outputs
   would need more ephemeral storage and a longer timeout, and anything near
   Lambda's 15-minute limit belongs on Fargate or Batch.
7. **Duplicates and ordering.** Notifications are at-least-once and
   unordered. The handler is idempotent (a missing original is a no-op, a
   replaced original isn't deleted), so duplicates only cost an extra
   invocation.
8. **Cold starts.** The container image takes ~1.9 s to initialise. That's
   irrelevant for an async pipeline and rare at steady state; provisioned
   concurrency isn't worth paying for here.
9. **Request-priced design.** Every per-object charge (PUT, GET, HEAD,
   lifecycle transitions) grows linearly with file count, not with bytes.
   That's the main cost-efficiency concern at this scale; see "Fewer, bigger
   objects" above.
