"""Monthly cost estimate for the archiver at a given volume.

Unit prices are on-demand, ap-southeast-1, taken from the AWS Price List API
(https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<service>/current/ap-southeast-1/index.json).
Free tier is ignored since it's irrelevant at this volume.

    python scripts/cost_estimate.py --duration-ms 900 --ratio 6.4
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
    p.add_argument("--duration-ms", type=float, required=True, help="average billed duration")
    p.add_argument("--memory-mb", type=int, default=1024)
    p.add_argument("--log-bytes", type=int, default=500, help="log bytes per invocation")
    a = p.parse_args()

    n = a.files_per_hour * HOURS_PER_MONTH
    orig_gb = a.file_mb * 1024 ** 2 / GIB
    zip_gb = orig_gb / a.ratio
    gb_seconds = n * (a.memory_mb / 1024) * (a.duration_ms / 1000)

    lines = {
        "Lambda requests": n * LAMBDA_PER_REQUEST,
        "Lambda compute (x86_64)": tiered(gb_seconds, LAMBDA_TIERS_X86),
        "S3 GET (read original)": n * S3_GET,
        "S3 PUT (write zip)": n * S3_PUT,
        "S3 HEAD x2 (verify zip, check original)": 2 * n * S3_GET,
        "S3 DELETE": 0.0,
        "CloudWatch Logs ingestion": n * a.log_bytes / GIB * CW_LOGS_INGEST,
        "S3 gateway endpoint / in-region transfer": 0.0,
    }
    processing = sum(lines.values())

    print(f"objects per month:        {n:,}")
    print(f"GB-seconds per month:     {gb_seconds:,.0f}")
    print()
    print("Processing cost added by the feature")
    for name, cost in lines.items():
        print(f"  {name:45} {money(cost):>12}")
    print(f"  {'total':45} {money(processing):>12}")
    print()

    month_orig_gb = n * orig_gb
    month_zip_gb = n * zip_gb
    std_orig = tiered(month_orig_gb, S3_STANDARD_TIERS)
    std_zip = tiered(month_zip_gb, S3_STANDARD_TIERS)
    print("Storage for one month of output (S3 Standard, per month it is kept)")
    print(f"  raw JSON   {month_orig_gb / 1024 ** 2:8.2f} PiB  {money(std_orig):>12}")
    print(f"  zipped     {month_zip_gb / 1024 ** 2:8.2f} PiB  {money(std_zip):>12}")
    print(f"  saved                     {money(std_orig - std_zip):>12}")
    print(f"  net first month (saving - processing): {money(std_orig - std_zip - processing)}")
    print()

    print("Alternatives / suggestions")
    arm = tiered(gb_seconds, LAMBDA_TIERS_ARM)
    print(f"  arm64 compute instead of x86_64:           {money(arm)} (saves {money(lines['Lambda compute (x86_64)'] - arm)})")
    nat = n * (orig_gb + zip_gb) * NAT_PER_GB + 2 * HOURS_PER_MONTH * NAT_PER_HOUR
    print(f"  same design through a NAT gateway instead: +{money(nat)} per month")
    print(f"  zipped month kept in Glacier Instant Retrieval: {money(month_zip_gb * S3_GLACIER_IR)}")
    print(f"  zipped month kept in Deep Archive:              {money(month_zip_gb * S3_DEEP_ARCHIVE)}"
          f" + one-off lifecycle transitions {money(n * S3_LIFECYCLE_TO_DEEP_ARCHIVE)}")


if __name__ == "__main__":
    main()
