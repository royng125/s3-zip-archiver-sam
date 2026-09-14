"""Monthly cost estimate for the archiver at a given volume.

Unit prices are on-demand, ap-southeast-1, taken from the AWS Price List API
(https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<service>/current/ap-southeast-1/index.json).
Free tier is ignored since it's irrelevant at this volume.

    python scripts/cost_estimate.py --duration-ms 715 --ratio 6.44 --log-bytes 507

Storage is modelled with objects arriving evenly through the month and kept
indefinitely, so month N stores (N - 0.5) months of data on average.
"""
import argparse

GIB = 1024 ** 3
HOURS_PER_MONTH = 730

# Lambda (x86_64 / arm64), per GB-second, tiered on monthly GB-seconds
LAMBDA_TIERS_X86 = [(6e9, 0.0000166667), (9e9, 0.0000150000), (float("inf"), 0.0000133334)]
LAMBDA_TIERS_ARM = [(7.5e9, 0.0000133334), (11.25e9, 0.0000120001), (float("inf"), 0.0000106667)]
LAMBDA_PER_REQUEST = 0.20 / 1e6

S3_PUT = 0.005 / 1000           # PUT, COPY, POST, LIST
S3_GET = 0.0004 / 1000          # GET, HEAD and other
S3_STANDARD_TIERS = [(50 * 1024, 0.025), (450 * 1024, 0.024), (float("inf"), 0.023)]  # per GB-month
S3_GLACIER_IR = 0.005           # per GB-month
S3_DEEP_ARCHIVE = 0.002         # per GB-month
S3_LIFECYCLE_TO_DEEP_ARCHIVE = 0.06 / 1000

CW_LOGS_INGEST = 0.70           # per GB
CW_ALARM = 0.10                 # per standard alarm per month
ALARMS = 4
SQS_PER_REQUEST = 0.40 / 1e6
NAT_PER_GB = 0.059
NAT_PER_HOUR = 0.059


def tiered(amount, tiers):
    total, remaining = 0.0, amount
    for size, price in tiers:
        used = min(remaining, size)
        total += used * price
        remaining -= used
        if remaining <= 0:
            break
    return total


def money(x):
    return f"${x:,.0f}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--files-per-hour", type=int, default=1_000_000)
    p.add_argument("--file-mb", type=float, default=10)
    p.add_argument("--ratio", type=float, required=True, help="original size / zip size")
    p.add_argument("--duration-ms", type=float, required=True, help="average billed duration of an archive run")
    p.add_argument("--memory-mb", type=int, default=1024)
    p.add_argument("--log-bytes", type=int, default=500, help="log bytes per archive run")
    p.add_argument("--skip-duration-ms", type=float, default=3, help="billed duration when the event is our own zip")
    p.add_argument("--skip-log-bytes", type=int, default=350, help="log bytes per skipped run")
    p.add_argument("--retention-months", type=int, default=12, help="months of archives kept once capped")
    a = p.parse_args()

    n = a.files_per_hour * HOURS_PER_MONTH
    gb = a.memory_mb / 1024
    orig_gb = a.file_mb * 1024 ** 2 / GIB
    zip_gb = orig_gb / a.ratio
    archive_gbs = n * gb * a.duration_ms / 1000
    skip_gbs = n * gb * a.skip_duration_ms / 1000

    lines = {
        "Lambda requests (archive + zip-triggered skip)": 2 * n * LAMBDA_PER_REQUEST,
        "Lambda compute, archive runs (x86_64)": tiered(archive_gbs, LAMBDA_TIERS_X86),
        "Lambda compute, zip-triggered skips": tiered(archive_gbs + skip_gbs, LAMBDA_TIERS_X86)
                                               - tiered(archive_gbs, LAMBDA_TIERS_X86),
        "S3 GET (read original)": n * S3_GET,
        "S3 HEAD (existing zip check)": n * S3_GET,
        "S3 PUT (write zip)": n * S3_PUT,
        "S3 DELETE": 0.0,
        "CloudWatch Logs ingestion": n * (a.log_bytes + a.skip_log_bytes) / GIB * CW_LOGS_INGEST,
        f"CloudWatch alarms ({ALARMS})": ALARMS * CW_ALARM,
        "S3 gateway endpoint / in-region transfer": 0.0,
    }
    processing = sum(lines.values())

    print(f"objects per month:        {n:,}")
    print(f"GB-seconds per month:     {archive_gbs + skip_gbs:,.0f}")
    print()
    print("Processing cost added by the feature (flat every month)")
    for name, cost in lines.items():
        print(f"  {name:48} {money(cost):>10}")
    print(f"  {'total':48} {money(processing):>10}")
    print()

    month_orig_gb = n * orig_gb
    month_zip_gb = n * zip_gb
    producer_puts = n * S3_PUT

    def bill(month, with_feature):
        stored_months = month - 0.5
        if with_feature:
            return producer_puts + processing + tiered(month_zip_gb * stored_months, S3_STANDARD_TIERS)
        return producer_puts + tiered(month_orig_gb * stored_months, S3_STANDARD_TIERS)

    print(f"One month of output: {month_orig_gb / 1024 ** 2:.2f} PiB raw, {month_zip_gb / 1024 ** 2:.2f} PiB zipped")
    print("Monthly bill for this pipeline (producer PUTs + processing + S3 Standard storage)")
    print(f"  {'month':>5}  {'without feature':>16}  {'with feature':>14}")
    for m in (1, 2, 3, 6, 12):
        print(f"  {m:>5}  {money(bill(m, False)):>16}  {money(bill(m, True)):>14}")
    growth_without = bill(3, False) - bill(2, False)
    growth_with = bill(3, True) - bill(2, True)
    print(f"  each further month adds about {money(growth_without)} without / {money(growth_with)} with the feature")
    print()
    print(f"Final monthly figure: {money(bill(1, True))} in month 1, then about +{money(growth_with)} per month"
          f" while archives are kept")
    # once a lifecycle rule caps retention the bucket always holds exactly that many months
    r = a.retention_months
    print(f"Steady state with {r} months retained: {money(producer_puts + processing + tiered(month_zip_gb * r, S3_STANDARD_TIERS))}"
          f" with / {money(producer_puts + tiered(month_orig_gb * r, S3_STANDARD_TIERS))} without the feature")
    print()

    print("Alternatives / suggestions")
    arm = tiered(archive_gbs + skip_gbs, LAMBDA_TIERS_ARM)
    x86 = tiered(archive_gbs + skip_gbs, LAMBDA_TIERS_X86)
    print(f"  arm64 compute instead of x86_64:              {money(arm)} (saves {money(x86 - arm)})")
    skip_cost = (n * LAMBDA_PER_REQUEST + lines["Lambda compute, zip-triggered skips"]
                 + n * a.skip_log_bytes / GIB * CW_LOGS_INGEST)
    print(f"  suffix filter instead of every object:        saves {money(skip_cost)}")
    nat = n * (orig_gb + zip_gb) * NAT_PER_GB + 2 * HOURS_PER_MONTH * NAT_PER_HOUR
    print(f"  same design through a NAT gateway instead:    +{money(nat)} per month")
    print(f"  one month of zips in Glacier Instant Retrieval: {money(month_zip_gb * S3_GLACIER_IR)}"
          f" (vs {money(tiered(month_zip_gb, S3_STANDARD_TIERS))} in Standard)")
    print(f"  one month of zips in Deep Archive:              {money(month_zip_gb * S3_DEEP_ARCHIVE)}"
          f" + one-off lifecycle transitions {money(n * S3_LIFECYCLE_TO_DEEP_ARCHIVE)}")
    # S3 -> SQS for both source and zip events, plus Lambda's receive and delete
    # calls at a batch size of 10
    sqs = 2 * n * 1.2 * SQS_PER_REQUEST
    print(f"  SQS buffer between S3 and Lambda:             about +{money(sqs)} per month")


if __name__ == "__main__":
    main()
