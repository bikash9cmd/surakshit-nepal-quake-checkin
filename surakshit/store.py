"""Storage behind one interface: LocalStore (in memory, for tests and `make demo`) and DynamoStore.

Two tables (see docs/ARCHITECTURE.md, "Data model"):

  CheckIns   pk = phone_key (HMAC of the number), sk = event_id
             One item per person per event. Spreading the partition key over phone numbers is
             what lets the table absorb a surge: every person is their own partition key.
  Meta       pk = "EVENT" | "COUNT#<event_id>",  sk = <event_id> | "<district>#<status>#<shard>"
             Events and the per-district summary counters.

Event ids start with the quake's UTC time ("2015-04-25T0611Z-us20002926") so that sorting by
sk sorts by time, and "this person's latest check-in" is one Query with Limit=1.
"""

from __future__ import annotations

import random
import threading
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from surakshit import config

# Attributes a later check-in may leave out; if it does, the old value must not linger
# (someone who moved from "need_help, Gorkha" to "safe" should not still show Gorkha).
_OPTIONAL_FIELDS = ("district", "note")


@dataclass
class PutResult:
    outcome: str                    # written | stale | duplicate | rate_limited
    old: dict | None = None         # the previous item, when there was one


class Store(Protocol):
    def put_checkin(self, item: dict) -> PutResult: ...
    def latest_checkin(self, phone_key: str) -> dict | None: ...
    def get_checkin(self, phone_key: str, event_id: str) -> dict | None: ...
    def add_counts(self, event_id: str, deltas: dict[tuple[str, str], int]) -> None: ...
    def summary(self, event_id: str) -> dict[str, dict[str, int]]: ...
    def create_event(self, event: dict) -> bool: ...
    def list_events(self, limit: int = 10) -> list[dict]: ...
    def add_sms_sent(self, event_id: str, n: int) -> int: ...


def _classify(old: dict | None, item: dict) -> str:
    """Why a conditional put failed. Order matters: a redelivered message is a duplicate even if
    the person has since hit the update cap."""
    if old is None:
        return "written"
    if old.get("checkin_id") == item["checkin_id"]:
        return "duplicate"
    if int(old["received_at"]) >= int(item["received_at"]):
        return "stale"
    return "rate_limited"


def _summarise(rows: list[tuple[str, int]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(config.STATUSES, 0))
    for sk, n in rows:
        district, status, _shard = sk.rsplit("#", 2)
        out[district][status] += int(n)
    return {d: dict(v) for d, v in sorted(out.items())}


class LocalStore:
    """In-memory store with the same semantics as DynamoStore, including conditional writes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.checkins: dict[tuple[str, str], dict] = {}
        self.counts: dict[tuple[str, str], int] = defaultdict(int)
        self.events: dict[str, dict] = {}
        self.sms_sent: dict[str, int] = defaultdict(int)

    def put_checkin(self, item: dict) -> PutResult:
        key = (item["pk"], item["sk"])
        with self._lock:
            old = self.checkins.get(key)
            ok = old is None or (
                int(old["received_at"]) < int(item["received_at"])
                and int(old.get("updates", 1)) < config.MAX_UPDATES_PER_PERSON
            )
            if not ok:
                return PutResult(_classify(old, item), old)
            new = dict(item, updates=int(old.get("updates", 1)) + 1 if old else 1)
            self.checkins[key] = new
            return PutResult("written", old)

    def latest_checkin(self, phone_key: str) -> dict | None:
        rows = [v for (pk, _sk), v in self.checkins.items() if pk == phone_key]
        return max(rows, key=lambda r: r["sk"]) if rows else None

    def get_checkin(self, phone_key: str, event_id: str) -> dict | None:
        return self.checkins.get((phone_key, event_id))

    def add_counts(self, event_id: str, deltas: dict[tuple[str, str], int]) -> None:
        with self._lock:
            for (district, status), n in deltas.items():
                if n:
                    shard = random.randrange(config.COUNTER_SHARDS)  # noqa: S311 - load spreading, not security
                    self.counts[(f"COUNT#{event_id}", f"{district}#{status}#{shard}")] += n

    def summary(self, event_id: str) -> dict[str, dict[str, int]]:
        pk = f"COUNT#{event_id}"
        return _summarise([(sk, n) for (p, sk), n in self.counts.items() if p == pk])

    def create_event(self, event: dict) -> bool:
        with self._lock:
            if event["event_id"] in self.events:
                return False
            self.events[event["event_id"]] = dict(event)
            return True

    def list_events(self, limit: int = 10) -> list[dict]:
        return sorted(self.events.values(), key=lambda e: e["event_id"], reverse=True)[:limit]

    def add_sms_sent(self, event_id: str, n: int) -> int:
        with self._lock:
            self.sms_sent[event_id] += n
            return self.sms_sent[event_id]


class DynamoStore:
    """DynamoDB implementation. Table names come from the Lambda environment (infra/template.yaml)."""

    def __init__(self, checkins_table: str, meta_table: str, resource=None) -> None:
        import boto3

        ddb = resource or boto3.resource("dynamodb")
        self.checkins = ddb.Table(checkins_table)
        self.meta = ddb.Table(meta_table)
        self._client = self.checkins.meta.client

    def put_checkin(self, item: dict) -> PutResult:
        """One UpdateItem does the write, the ordering check, the per-person cap and the counter
        increment, and hands back the previous item so the caller knows the old status."""
        from botocore.exceptions import ClientError

        fields = {k: v for k, v in _to_ddb(item).items() if k not in ("pk", "sk", "updates")}
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        removable = [k for k in _OPTIONAL_FIELDS if k not in fields]
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields) + " ADD updates :one"
        if removable:
            expr += " REMOVE " + ", ".join(removable)
        try:
            resp = self.checkins.update_item(
                Key={"pk": item["pk"], "sk": item["sk"]},
                UpdateExpression=expr,
                ConditionExpression=(
                    "attribute_not_exists(pk) OR (#received_at < :received_at AND updates < :max)"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**values, ":one": 1, ":max": config.MAX_UPDATES_PER_PERSON},
                ReturnValues="ALL_OLD",
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # The item on a failed condition comes back in the low-level format ({"S": "..."}), even
            # through the resource API, which deserialises everything else. The first version
            # passed it straight on and crashed on every redelivered message.
            raw = e.response.get("Item")
            old = _from_ddb(_deserialise(raw)) if raw else self.get_checkin(item["pk"], item["sk"])
            return PutResult(_classify(old, item), old)
        old = resp.get("Attributes")
        return PutResult("written", _from_ddb(old) if old else None)

    def latest_checkin(self, phone_key: str) -> dict | None:
        from boto3.dynamodb.conditions import Key

        resp = self.checkins.query(KeyConditionExpression=Key("pk").eq(phone_key),
                                   ScanIndexForward=False, Limit=1)
        items = resp.get("Items", [])
        return _from_ddb(items[0]) if items else None

    def get_checkin(self, phone_key: str, event_id: str) -> dict | None:
        item = self.checkins.get_item(Key={"pk": phone_key, "sk": event_id}).get("Item")
        return _from_ddb(item) if item else None

    def add_counts(self, event_id: str, deltas: dict[tuple[str, str], int]) -> None:
        for (district, status), n in deltas.items():
            if not n:
                continue
            shard = random.randrange(config.COUNTER_SHARDS)  # noqa: S311
            self.meta.update_item(
                Key={"pk": f"COUNT#{event_id}", "sk": f"{district}#{status}#{shard}"},
                UpdateExpression="ADD n :n", ExpressionAttributeValues={":n": n},
            )

    def summary(self, event_id: str) -> dict[str, dict[str, int]]:
        from boto3.dynamodb.conditions import Key

        rows, kwargs = [], {"KeyConditionExpression": Key("pk").eq(f"COUNT#{event_id}")}
        while True:
            resp = self.meta.query(**kwargs)
            rows += [(i["sk"], int(i["n"])) for i in resp["Items"]]
            if "LastEvaluatedKey" not in resp:
                return _summarise(rows)
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def create_event(self, event: dict) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.meta.put_item(Item=_to_ddb({"pk": "EVENT", "sk": event["event_id"], **event}),
                               ConditionExpression="attribute_not_exists(sk)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def list_events(self, limit: int = 10) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        resp = self.meta.query(KeyConditionExpression=Key("pk").eq("EVENT"),
                               ScanIndexForward=False, Limit=limit)
        return [_from_ddb(i) for i in resp["Items"]]

    def add_sms_sent(self, event_id: str, n: int) -> int:
        """Per-event SMS counter for the budget breaker. One write per notifier batch, not per SMS."""
        resp = self.meta.update_item(Key={"pk": "SMS", "sk": event_id}, UpdateExpression="ADD n :n",
                                     ExpressionAttributeValues={":n": n}, ReturnValues="ALL_NEW")
        return int(resp["Attributes"]["n"])


def _deserialise(raw: dict) -> dict:
    from boto3.dynamodb.types import TypeDeserializer

    td = TypeDeserializer()
    return {k: td.deserialize(v) for k, v in raw.items()}


def _to_ddb(d: dict) -> dict:
    """DynamoDB rejects floats and empty strings in some positions; convert and drop Nones."""
    out = {}
    for k, v in d.items():
        if v is None or v == "" or v == []:
            continue
        out[k] = Decimal(str(v)) if isinstance(v, float) else v
    return out


def _from_ddb(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v == v.to_integral_value() else float(v)
        else:
            out[k] = v
    return out
