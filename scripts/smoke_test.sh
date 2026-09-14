#!/usr/bin/env bash
# Checks a deployed stack end to end. Uses whatever AWS credentials/profile the
# shell already has and only writes test objects into the stack's own bucket.
#
#   STACK=s3-zip-archiver scripts/smoke_test.sh
set -euo pipefail

STACK=${STACK:-s3-zip-archiver}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET=$(output BucketName)
FN=$(output FunctionName)
QUEUE=$(output FailedEventsQueueUrl)
VPC=$(output VpcId)
PREFIX="smoke/$(date +%Y%m%d-%H%M%S)"

echo "== notification (expect the :live alias, no key filter)"
aws s3api get-bucket-notification-configuration --bucket "$BUCKET" \
  --query "LambdaFunctionConfigurations[].[LambdaFunctionArn,Events[0],Filter]" --output text

echo "== versions and alias"
aws lambda list-versions-by-function --function-name "$FN" \
  --query "Versions[].[Version,Environment.Variables.RELEASE_ID]" --output text
aws lambda get-alias --function-name "$FN" --name live --query FunctionVersion --output text

echo "== network (expect only local + S3 prefix list routes, no IGW, no NAT)"
aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC" \
  --query "RouteTables[].Routes[].[DestinationCidrBlock,DestinationPrefixListId]" --output text
echo "internet gateways: $(aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC" --query 'length(InternetGateways)')"
echo "nat gateways:      $(aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=$VPC" --query 'length(NatGateways)')"

echo "== uploading test objects under $PREFIX/"
python3 - "$WORK" <<'EOF'
import json, random, sys
random.seed(1)
frames = [{"frame": i, "detections": [{"label": random.choice(["person", "car", "dog"]),
           "confidence": round(random.random(), 4),
           "bbox": [random.randint(0, 1920) for _ in range(4)]} for _ in range(random.randint(0, 5))]}
          for i in range(60000)]
open(f"{sys.argv[1]}/result.json", "w").write(json.dumps({"video_id": "smoke", "frames": frames}))
open(f"{sys.argv[1]}/lines.ndjson", "w").write("\n".join(json.dumps(f) for f in frames[:1000]))
EOF
T0=$(( ($(date +%s) - 5) * 1000 ))
aws s3 cp "$WORK/result.json" "s3://$BUCKET/$PREFIX/result.json" --only-show-errors
aws s3 cp "$WORK/lines.ndjson" "s3://$BUCKET/$PREFIX/lines.ndjson" --only-show-errors
aws s3 cp "$WORK/lines.ndjson" "s3://$BUCKET/$PREFIX/no extension" --only-show-errors
printf 'PK\005\006%018d' 0 > "$WORK/producer.zip"
aws s3 cp "$WORK/producer.zip" "s3://$BUCKET/$PREFIX/producer.zip" --only-show-errors

expected="$PREFIX/lines.ndjson.zip $PREFIX/no extension.zip $PREFIX/producer.zip $PREFIX/result.json.zip"
for _ in $(seq 1 40); do
  got=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX/" --query "Contents[].Key" --output json \
        | python3 -c "import json,sys; print(' '.join(sorted(json.load(sys.stdin) or [])))")
  [ "$got" = "$expected" ] && break
  sleep 3
done
echo "objects: $got"
[ "$got" = "$expected" ] || { echo "FAIL: expected $expected"; exit 1; }

echo "== zip content matches the uploaded file"
aws s3 cp "s3://$BUCKET/$PREFIX/result.json.zip" "$WORK/back.zip" --only-show-errors
python3 - "$WORK" <<'EOF'
import hashlib, sys, zipfile
w = sys.argv[1]
inner = zipfile.ZipFile(f"{w}/back.zip").read("result.json")
same = hashlib.sha256(inner).digest() == hashlib.sha256(open(f"{w}/result.json", "rb").read()).digest()
print("sha256 match:", same)
sys.exit(0 if same else 1)
EOF

echo "== failed events queue depth (expect 0)"
aws sqs get-queue-attributes --queue-url "$QUEUE" --attribute-names ApproximateNumberOfMessages \
  --query Attributes.ApproximateNumberOfMessages --output text

echo "== REPORT lines since upload"
sleep 20
# REPORT lines are tab-separated themselves, so read them as JSON rather than text
aws logs filter-log-events --log-group-name "/aws/lambda/$FN" --start-time "$T0" \
  --filter-pattern REPORT --query "events[].message" --output json \
  | python3 -c "import json,sys; [print(m.strip().replace(chr(9), '  ')) for m in json.load(sys.stdin)]"

echo "OK"
