"""Open an event by hand: a drill, a quake USGS hasn't published yet, or an activator outage.

    python scripts/open_event.py --id 2026-10-20T0400Z-drill --mag 0 --place "Kathmandu drill" --region ap-south-1
"""

from __future__ import annotations

import argparse
import time

import boto3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True, help="sortable event id, e.g. 2026-10-20T0400Z-drill")
    p.add_argument("--mag", type=float, required=True)
    p.add_argument("--place", required=True)
    p.add_argument("--region", default="ap-south-1")
    a = p.parse_args()
    table = boto3.resource("dynamodb", region_name=a.region).Table("surakshit-meta")
    table.put_item(Item={"pk": "EVENT", "sk": a.id, "event_id": a.id, "started_at": int(time.time() * 1000),
                         "mag": str(a.mag), "place": a.place, "severity": "watch", "opened_by": "manual",
                         "usgs_ids": []},
                   ConditionExpression="attribute_not_exists(sk)")
    print("opened", a.id)


if __name__ == "__main__":
    main()
