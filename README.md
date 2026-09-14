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
2. It reads exactly the version the event describes (`If-Match` on the
   event's eTag) and streams it into a ZIP in `/tmp`.
3. It checks what is already at `<key>.zip` (see "Concurrency and safe
   delete"), then uploads the zip with a conditional PUT and a SHA-256
   checksum, recording the source ETag and the event's sequencer.
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
producer's own `.zip` uploads are skipped the same way. If a producer uploads
both `report` and `report.zip`, that zip is left alone (see below).

**Private subnets without NAT.** The function only needs S3, so the VPC has no
internet gateway and no NAT gateway. S3 traffic goes through a gateway
endpoint, which is free, and the endpoint policy only allows this stack's
bucket. The bucket policy rejects any request not made over TLS. Logs don't
need a network path: Lambda ships them to CloudWatch outside the VPC. See the
cost section for why NAT would be a very expensive choice here.

**Concurrency and safe delete.** S3 notifications are delivered at least once
and in no guaranteed order, and a producer may upload the same key again while
an earlier version is still being archived. The interleavings that matter,
each reproduced by a test in `tests/test_app.py`:

```
situation                                  how it's handled
─────────────────────────────────────────  ─────────────────────────────────────────────────
same event delivered twice                 second DELETE If-Match gets 404 -> "missing"
new upload between our read and delete     DELETE If-Match: etag we read -> 412, new version kept
slow invocation for an older version       zip already made from a newer event -> write nothing
late event, key overwritten since          GET If-Match: event eTag -> 412 -> "superseded"
zip labelled older than its content        replace a zip only while our source is still live
  (backfill zip, identical re-upload)
```

The rules, in the order the handler applies them:

| situation | action |
|---|---|
| the event's version was overwritten before it was read | skip (`superseded`); the newer version has its own event |
| original is gone | nothing to do (`missing`) |
| `<key>.zip` exists without `source-etag` metadata | not written by the archiver: leave both (`zip-key-taken`) |
| zip exists, made from this exact source ETag | skip the upload, run the conditional delete (how a retry after failing between upload and delete finishes) |
| zip is from a newer event, or our source is no longer the live object | stop, keep everything (`stale`) |
| otherwise | PUT with `If-None-Match: *` / `If-Match: <zip etag>`, then conditional delete |
| original changed before the delete | leave it (`kept-original`) |

**Known limitations.**

- With each result key written once (for example one key per video), the only
  concurrency is duplicate delivery, which is fully handled. With repeated
  writes to one key, a write landing in the milliseconds between the source
  check and the zip PUT can still be overwritten.
- Events without a sequencer (manual invoke, batch job) never replace a zip
  made from another version; the original stays in the bucket.
- A `<key>.zip` the archiver didn't write is never overwritten, so `<key>`
  stays uncompressed next to it.
- Keys over 1,020 bytes: `<key>.zip` passes S3's 1,024-byte key limit, the PUT
  fails and the event ends in the failed-events queue with the original kept.
- A DELETE answered with 409 counts as `kept-original`; if the conflicting
  write then failed, no new event arrives and the original stays.
- An archive is a single PUT, so at most 5 GB.

Bucket versioning is deliberately off; with it on, "deleting" the original
would only add a delete marker and save nothing.

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
  10 MB each.
- Every archived file means two invocations: the archive run, and a short one
  when its own zip triggers the function.
- Durations and log sizes are the measured values from the second deployment:
  743 ms and 464 bytes per archive run, 2 ms and 258 bytes per zip-triggered
  run, at 1024 MB. That's twenty runs on synthetic data, one at a time; real
  files under real concurrency could be slower.
- Compression ratio 6.44x, measured on synthetic JSON. Real video-analysis
  output could compress better or worse, and the storage numbers scale directly
  with this ratio.
- Files arrive evenly through the month and are kept, so month N pays for
  N - 0.5 months of data on average. An earlier version of this section charged
  a full month of storage in the first month and overstated the first-month
  saving by roughly 2x.
- On-demand prices for `ap-southeast-1` from the AWS Price List API, free tier
  ignored.

`scripts/cost_estimate.py` reproduces every number below:

```bash
python scripts/cost_estimate.py --duration-ms 743 --ratio 6.44 --log-bytes 464 \
  --skip-duration-ms 2 --skip-log-bytes 258
```

### What the feature costs to run

The same every month:

| item | volume per month | USD / month |
|---|---|---|
| Lambda requests | 1,460M (archive + zip-triggered) x $0.20 per 1M | 292 |
| Lambda compute, archive runs, x86_64 | 542.4M GB-s x $0.0000166667 | 9,040 |
| Lambda compute, zip-triggered runs | 1.46M GB-s | 24 |
| S3 GET, read original | 730M x $0.0004 per 1K | 292 |
| S3 HEAD, check for an existing zip | 730M x $0.0004 per 1K | 292 |
| S3 PUT, write zip | 730M x $0.005 per 1K | 3,650 |
| S3 DELETE | free | 0 |
| CloudWatch Logs ingestion | ~491 GB x $0.70 | 344 |
| CloudWatch alarms | 4 x $0.10 | < 1 |
| S3 gateway endpoint, in-region transfer | free | 0 |
| ECR image (207 MB), SQS and SNS (failures only) | | < 1 |
| **total** | | **~13,934** |

That is about $0.000019 per file, or $19 per million files. Cold starts add a
little on top: the image's 1.9 s init is billed, but at ~200 warm environments
running continuously they are a very small share of invocations. Replacing an
existing zip costs one more HEAD, which only happens when a key is rewritten.

### Monthly bill with and without the feature

Both columns include the producer's 730M uploads ($3,650). "Without" keeps the
raw JSON in S3 Standard; "with" adds the processing above and keeps only the
zips.

| month | without feature | with feature |
|---|---|---|
| 1 | 86,196 | **30,878** |
| 2 | 250,160 | 56,338 |
| 3 | 414,125 | 81,798 |
| 6 | 906,020 | 158,179 |
| 12 | 1,889,809 | 310,942 |
| each further month | +163,965 | +25,460 |

### Final monthly figure

**US$30,878 for the first month, then about US$25,460 more each month for as
long as the archives are kept.** US$13,934 of that is the processing this
feature adds; the rest is the producer's uploads and the zip storage. Without
the feature the first month costs US$86,196 and grows by US$163,965 a month.

The bill has no single steady-state value while storage keeps growing. With a
retention or lifecycle rule that caps what is kept, it levels off: with twelve
months kept in S3 Standard, about **US$323,672 a month with the feature against
US$1,971,791 without**.

### Ways to save more

Roughly in order of impact:

1. **Keep S3 traffic off NAT.** Already done here. The same design through a
   NAT gateway would add about **US$486,000 per month** in data processing,
   more than everything else combined.
2. **Compress before uploading.** If the on-prem exporter writes ZIP (or
   gzip/zstd) itself, the ~US$13.9K of processing disappears and upload
   bandwidth drops about 6x. This Lambda is the right tool when the producer
   can't be changed.
3. **Move older archives to a colder class.** One month of zips costs
   ~US$5,500 a month in Glacier Instant Retrieval instead of ~US$26,000 in
   Standard. Deep Archive is ~US$2,200, but lifecycle transitions are charged
   per object (US$43,800 for a month's 730M objects), there's a 180-day minimum
   and retrieval takes hours, so it only pays off for data that is almost
   never read.
4. **Fewer, bigger objects.** Lambda requests (US$292), PUTs (US$3,650),
   GET/HEAD (US$584) and especially lifecycle transitions are charged per
   object. Bundling files into one archive per minute or per video
   (S3 -> SQS -> batch consumer) divides those line items by the bundle size.
5. **arm64.** Graviton compute is 20% cheaper, about US$1,813 a month. Needs
   the image built for arm64, which the CI job could do with a multi-arch
   build.
6. **Right-size memory.** Max memory used was 114 MB out of 1024 MB. Lowering
   memory also lowers CPU, so duration will go up; the cheapest setting has to
   be measured (e.g. with AWS Lambda Power Tuning). Not measured here.
7. **Filter the trigger if every object isn't really needed.** Restricting the
   notification to known source keys removes the zip-triggered runs, about
   US$293 a month.
8. **Smaller wins.** Log successful archives at DEBUG in production and buy a
   Compute Savings Plan for the Lambda spend.

## Scalability and bottlenecks

**Short answer:** the design scales to this volume, but not on the account it
was tested on as-is, and one invocation per object is not the cheapest shape at
this size.

At 1,000,000 files per hour there are ~278 source events and ~278 zip events
per second. At 743 ms per archive run that is **about 206 concurrent
executions** for archiving and under one for the zip-triggered runs, before any
burst from the producer.

1. **Lambda concurrency quota, the first hard limit.** The account this was
   deployed to has a limit of 10 concurrent executions
   (`aws lambda get-account-settings`). At 743 ms that is about 48,000 files an
   hour, under 5% of the target; the rest would be throttled, retried for up to
   6 hours and then land in the failed-events queue. The quota needs raising
   to a few hundred plus burst headroom before production. Reserved
   concurrency, to stop this function starving others, can't be set until
   then because Lambda always keeps 100 units unreserved. The `Throttles`
   alarm fires as soon as throttling starts.
2. **The async backlog is hard to see.** S3 -> Lambda is an asynchronous
   invoke with an internal queue, and `AsyncEventAge` is the only signal; there
   is an alarm at 15 minutes. Putting SQS between S3 and Lambda gives a visible
   queue depth, batching and a maximum concurrency on the event source
   mapping. At about **US$700 a month** (source and zip events through the
   queue, receives and deletes at batch size 10) I'd do that for production.
3. **S3 request rates per prefix.** S3 supports 3,500 PUT/COPY/POST/DELETE and
   5,500 GET/HEAD requests per second per partitioned prefix. Steady state here
   is ~833 write requests/s (producer PUT + zip PUT + DELETE) and ~556
   GET/HEAD/s. That's fine on average, but a burst into a single prefix can
   return `503 SlowDown` while S3 re-partitions. Spreading keys across
   prefixes (date/hour or a hash) avoids it.
4. **VPC limits.** Lambda uses shared Hyperplane ENIs per subnet and security
   group combination, so concurrency doesn't use one IP per execution. The
   `/20` subnets have ~4,091 addresses each and there's a per-VPC Hyperplane
   ENI quota. The S3 gateway endpoint has no bandwidth limit. Losing one AZ
   leaves the other subnet running.
5. **Originals pile up when processing falls behind.** The raw object is only
   deleted after its zip exists, so throttling or errors leave raw data in
   Standard at full price. The `AsyncEventAge` and failed-events alarms cover
   this. Each message in the failed-events queue carries the original S3 event
   (`requestPayload`), so it can be replayed to the `live` alias with
   `aws lambda invoke`; the replay keeps its sequencer, so the rules in
   "Concurrency and safe delete" make it safe.
6. **Object size.** The handler streams into `/tmp` (512 MB by default, up to
   10 GB) with a 120 s timeout, and the zip is written with a single PUT, so an
   archive can be at most 5 GB. 10 MB takes about 0.7 s. Multi-GB outputs would
   need more ephemeral storage, a longer timeout and a multipart upload, and
   anything near Lambda's 15-minute limit belongs on Fargate or Batch.
7. **Concurrent and duplicate events.** Notifications are at-least-once and
   unordered. A duplicate delivery costs one extra invocation and is handled;
   with each key written once, that is the only concurrency there is. Repeated
   writes to the same key are handled too, except for the millisecond window
   under "Known limitations".
8. **Cold starts.** The container image takes ~1.9 s to initialise. That's
   irrelevant for an async pipeline and rare at steady state; provisioned
   concurrency isn't worth paying for here.
9. **Per-object pricing.** Every per-object charge (invocations, PUT, GET,
   HEAD, lifecycle transitions) grows with file count, not with bytes. That's
   the main cost-efficiency concern at this scale; see "Fewer, bigger objects".
10. **Rollback scope.** Moving the alias only rolls back the function; changes
    to the VPC, policies or alarms need the older commit redeployed, and every
    version depends on its image staying in ECR (see "Rolling back").

## Existing objects (backfill)

The introduction says the buckets are already large. The bucket notification
only sees objects uploaded after the stack exists, so the data already there
needs a one-off job. Not implemented here; the plan:

```
S3 Inventory manifest ──► drop *.zip and keys that already have <key>.zip ──► S3 Batch Operations
                                                                              "Invoke AWS Lambda" -> ArchiverFunction:live
                                                                              one job per prefix, run off-peak
```

1. Turn on S3 Inventory for the bucket, or let Batch Operations generate the
   manifest from a prefix.
2. Drop keys that already end in `.zip` and keys whose `<key>.zip` already
   exists (Athena over the inventory works well for this).
3. Run a Batch Operations job that invokes the `live` alias for each key.
   Batch Operations sends its own payload (`tasks[].s3Key`) and expects a result
   per task, so the handler needs a small adapter for that format, which isn't
   written yet. It can run while live traffic continues: a backfill invocation
   never replaces a zip made from another version, and a live event only
   replaces a zip while its own source is still the live object.
4. Split jobs by prefix and run them when the producer is quiet. Batch
   Operations uses the same account concurrency as live traffic: with this
   account's limit of 10, 100 million objects would take about 86 days; at 250
   concurrent executions, about 3.4 days.

Example for **100 million existing 10 MB objects** (954 TiB), with the same
per-file cost as above:

| item | USD |
|---|---|
| processing, $19.09 per million files | 1,909 |
| Batch Operations, $1.00 per million objects + $0.25 per job | ~100 |
| S3 Inventory ($0.0028 per million listed) or a generated manifest ($0.015 per million) | < 2 |
| **one-off total** | **~2,010** |

Afterwards those objects take 148 TiB instead of 954 TiB, and their storage
drops from **US$23,024 to US$3,691 a month, saving US$19,334 a month**. The
backfill pays for itself in about three days.
