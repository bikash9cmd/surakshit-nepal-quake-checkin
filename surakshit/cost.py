"""Monthly cost: idle (the normal state) and during a 15-million-person event. Prints markdown.

    python -m surakshit cost

Prices are on-demand list prices (USD) as I understood them in October 2026, rounded up, free
tier ignored. Check them in the AWS Pricing Calculator for ap-south-1 before quoting. The point of
the model is the shape (what dominates), not the third digit.
"""

from __future__ import annotations

PRICE = {
    "httpapi_req": 1.00 / 1e6,
    "sqs_req": 0.40 / 1e6,
    "lambda_gb_s_arm": 0.0000133334,
    "lambda_req": 0.20 / 1e6,
    "ddb_wru": 0.625 / 1e6,          # also the replicated write price for on-demand global tables
    "ddb_rru": 0.125 / 1e6,
    "cf_req": 0.012 / 10_000,        # HTTPS requests, India edge locations
    "cf_gb": 0.17,                   # data transfer out, India
    "waf_acl": 5.00, "waf_rule": 1.00, "waf_req": 0.60 / 1e6,
    "alarm": 0.10,
    "logs_gb": 0.50,
    "sms_local_np": 0.01,            # Nepali aggregator, ~NPR 0.5-1.4 per SMS
    "sms_international": 0.10,       # international gateway to Nepal, lower bound
    "ec2_c7g_large_hr": 0.0725, "alb_hr": 0.0225, "rds_r6g_xl_multiaz_hr": 0.96,
}
HOURS = 730

IDLE = {
    "regions": 2,
    "alarms": {"primary": 10, "standby": 7},
    "waf_rules": 4,
    # Lambda keeps long-polling each SQS event source even when the queue is empty. Assumed 5
    # pollers x 3 empty receives a minute (20 s long poll) per queue; 2 queues per region.
    "sqs_idle_receives": 5 * 3 * 60 * HOURS * 2,
    "activator_runs": 60 * HOURS, "activator_s": 0.5, "activator_gb": 0.25,
    "logs_gb": 0.05,
}

EVENT = {
    "people": 15_000_000,
    "updates_per_person": 1.3,       # main shock, plus re-check-ins after the M6+ aftershocks
    "lookups_per_checkin": 4,
    "cache_hit": 0.6,                # family members re-checking the same number within 30 s
    "page_kb": 6, "api_kb": 1,
    "writer_batch": 100, "writer_s": 1.5, "writer_gb": 0.5,
    "api_s": 0.03, "api_gb": 0.25,
    "counter_writes_per_batch": 3,
    "sms_share": 0.10, "finds_per_sms_user": 0.7,   # people who check in by SMS, and FIND texts
    "contact_share": 0.20, "contacts_each": 1.5,    # web check-ins that list family numbers
    "sms_budget": 500_000,
}


def idle() -> dict[str, float]:
    p, a = PRICE, IDLE
    act_gbs = a["activator_runs"] * a["activator_s"] * a["activator_gb"]
    return {
        "WAF web ACL + 4 rules (us-east-1)": p["waf_acl"] + a["waf_rules"] * p["waf_rule"],
        "CloudWatch alarms (17 across both regions)": sum(a["alarms"].values()) * p["alarm"],
        "SQS: Lambda long-polling 4 empty queues": a["sqs_idle_receives"] * a["regions"] * p["sqs_req"],
        "Activator Lambda (every minute, primary only)": act_gbs * p["lambda_gb_s_arm"]
        + a["activator_runs"] * p["lambda_req"],
        "DynamoDB (activator reads, storage, PITR on ~0 GB)": a["activator_runs"] * 0.5 * p["ddb_rru"] + 0.02,
        "CloudWatch Logs": a["logs_gb"] * p["logs_gb"],
        "API Gateway, CloudFront, S3, SNS, SSM, custom metrics": 0.05,
    }


def event() -> tuple[dict[str, float], dict[str, float]]:
    p, e = PRICE, EVENT
    writes = e["people"] * e["updates_per_person"]
    lookups = writes * e["lookups_per_checkin"]
    origin_reads = lookups * (1 - e["cache_hit"]) + e["people"] * 0.05  # + /events misses
    viewer = lookups + 2 * e["people"]                                   # + page and /events
    batches = writes / e["writer_batch"]
    platform = {
        "CloudFront requests + transfer": viewer * p["cf_req"]
        + (e["people"] * e["page_kb"] + (viewer - e["people"]) * e["api_kb"]) / 1e6 * p["cf_gb"],
        "WAF requests": viewer * p["waf_req"],
        "API Gateway (check-ins + uncached reads)": (writes + origin_reads) * p["httpapi_req"],
        "SQS (send, receive, delete)": writes * 1.2 * p["sqs_req"],
        "Lambda (writer, read API, aggregator)": (batches * e["writer_s"] * e["writer_gb"]
                                                  + origin_reads * e["api_s"] * e["api_gb"]) * p["lambda_gb_s_arm"]
        + (batches * 2 + origin_reads) * p["lambda_req"],
        "DynamoDB writes, replicated to the standby": writes * 2 * p["ddb_wru"]
        + batches * e["counter_writes_per_batch"] * 2 * p["ddb_wru"],
        "DynamoDB reads (uncached lookups)": origin_reads * 0.5 * p["ddb_rru"],
    }
    sms_users = e["people"] * e["sms_share"]
    replies = sms_users * (1 + e["finds_per_sms_user"])
    notify_wanted = e["people"] * e["contact_share"] * e["contacts_each"]
    notify = min(notify_wanted, e["sms_budget"])
    sms = {
        "replies (SMS check-ins and FIND)": replies,
        "contact notifications, capped by budget": notify,
        "contact notifications wanted (uncapped)": notify_wanted,
    }
    return platform, sms


def always_on_alternative() -> dict[str, float]:
    """What it costs to be ready for 20k writes/s with servers instead (per month, per region)."""
    p = PRICE
    return {
        "10 x c7g.large behind an ALB, running 24/7": 10 * p["ec2_c7g_large_hr"] * HOURS + p["alb_hr"] * HOURS,
        "RDS db.r6g.xlarge Multi-AZ": p["rds_r6g_xl_multiaz_hr"] * HOURS,
    }


def report() -> None:
    rows = idle()
    total = sum(rows.values())
    print("## Idle month (no earthquake)\n\n| Item | USD / month |\n|---|---:|")
    for k, v in rows.items():
        print(f"| {k} | {v:.2f} |")
    print(f"| **Total** | **{total:.2f}** |\n")

    platform, sms = event()
    ptotal = sum(platform.values())
    e = EVENT
    print(f"## One event: {e['people'] / 1e6:.0f} million people, "
          f"{e['people'] * e['updates_per_person'] / 1e6:.1f} million check-ins\n")
    print("| Item | USD |\n|---|---:|")
    for k, v in platform.items():
        print(f"| {k} | {v:,.0f} |")
    print(f"| **Platform total** | **{ptotal:,.0f}** |\n")
    sent = sms["replies (SMS check-ins and FIND)"] + sms["contact notifications, capped by budget"]
    uncapped = sms["replies (SMS check-ins and FIND)"] + sms["contact notifications wanted (uncapped)"]
    print("| SMS | Messages | Local aggregator | International gateway |\n|---|---:|---:|---:|")
    for label, n in (("With the budget breaker", sent), ("Without it", uncapped)):
        print(f"| {label} | {n / 1e6:.1f} M | ${n * PRICE['sms_local_np']:,.0f} | "
              f"${n * PRICE['sms_international']:,.0f} |")
    print(f"\nSMS is {sent * PRICE['sms_local_np'] / (sent * PRICE['sms_local_np'] + ptotal):.0%} of the "
          "event's cost even through a local aggregator.\n")

    alt = always_on_alternative()
    atot = sum(alt.values())
    print("## Always-on alternative, sized for the same peak\n\n| Item | USD / month / region |\n|---|---:|")
    for k, v in alt.items():
        print(f"| {k} | {v:,.0f} |")
    print(f"| **Total, two regions** | **{2 * atot:,.0f}** |")
    print(f"\nServerless idle: ${total * 12:,.0f} a year. Always-on, two regions: ${2 * atot * 12:,.0f} a year.")
