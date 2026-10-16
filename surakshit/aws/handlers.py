"""AWS Lambda entry points. The same core code as local mode; only the I/O differs.

  writer_handler      SQS check-in queue -> DynamoDB CheckIns (+ notification jobs to the notify queue)
  aggregator_handler  CheckIns DynamoDB stream -> per-district counters in Meta
  notifier_handler    notify queue -> SMS through the configured provider (dry run by default)
  activator_handler   every minute (EventBridge Scheduler): USGS feed -> open events -> SNS
  api_handler         API Gateway HTTP API: GET /status, /events, /summary/{event_id}, /health

POST /checkin has no handler on purpose: API Gateway sends it straight to SQS.
Environment variables are set by infra/template.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from surakshit import activation, aggregate, reads, sms_gateway, writer
from surakshit.phone import mask
from surakshit.store import DynamoStore
from surakshit.writer import Envelope

log = logging.getLogger()
log.setLevel(logging.INFO)

_THROTTLE_CODES = {"ProvisionedThroughputExceededException", "ThrottlingException",
                   "RequestLimitExceeded", "TooManyRequestsException"}
_cache: dict = {}
_deser = TypeDeserializer()


def _client(name: str):
    if name not in _cache:
        _cache[name] = boto3.client(name)
    return _cache[name]


def _store() -> DynamoStore:
    if "store" not in _cache:
        _cache["store"] = DynamoStore(os.environ["CHECKINS_TABLE"], os.environ["META_TABLE"])
    return _cache["store"]


def _pepper() -> bytes:
    """HMAC key for phone numbers, from an SSM SecureString. Cached for the container's life."""
    if "pepper" not in _cache:
        resp = _client("ssm").get_parameter(Name=os.environ["PEPPER_PARAM"], WithDecryption=True)
        _cache["pepper"] = resp["Parameter"]["Value"].encode()
    return _cache["pepper"]


def _events(store) -> list[dict]:
    """Open events, cached for 30 s: the writer asks for every batch, and during a surge that
    would be thousands of identical queries a minute against one partition key."""
    hit = _cache.get("events")
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    events = store.list_events(limit=20)
    _cache["events"] = (time.time(), events)
    return events


def _metric(values: dict[str, float], **props: str) -> None:
    """CloudWatch Embedded Metric Format: one log line becomes metrics, no PutMetricData call.
    No dimensions: each dimension value would be a separately billed custom metric. Zeros are
    not emitted: custom metrics are billed for the hours they are published, and the activator
    runs every minute, so emitting "0 events opened" would bill metrics all year for nothing."""
    values = {k: v for k, v in values.items() if v}
    if not values:
        return
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "Surakshit", "Dimensions": [[]],
            "Metrics": [{"Name": k, "Unit": "Count"} for k in values]}]},
        **values, **props}))


def _is_throttle(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response["Error"]["Code"] in _THROTTLE_CODES


# ---------- writer ----------

def to_envelope(record: dict) -> Envelope:
    """An SQS record from either door. Both come through API Gateway's SQS integration, which
    adds the source, the request id and the request time as message attributes:
      POST /checkin       web page JSON
      POST /sms/inbound   the SMS aggregator's webhook (behind a token authorizer)
    """
    attrs = record.get("messageAttributes") or {}
    sent = int(record["attributes"]["SentTimestamp"])
    source = attrs.get("source", {}).get("stringValue", "web")
    received_at = int(attrs.get("requestTime", {}).get("stringValue") or sent)
    rid = attrs.get("requestId", {}).get("stringValue") or record["messageId"]
    if source == "sms":
        sms = sms_gateway.parse_inbound(record["body"])
        return Envelope(message_id=record["messageId"], source="sms", body=sms["text"],
                        received_at=received_at, request_id=sms["id"] or rid, sender=sms["from"])
    return Envelope(message_id=record["messageId"], source="web", body=record["body"],
                    received_at=received_at, request_id=rid)


def writer_handler(event, context=None):
    store = _store()
    envelopes, unreadable = [], 0
    for r in event["Records"]:
        try:
            envelopes.append(to_envelope(r))
        except (KeyError, ValueError):
            unreadable += 1  # not retried: a body we can't parse now we can't parse later
    res = writer.process(envelopes, store, _pepper(), _events(store), is_throttle=_is_throttle)
    _send_notifications(res.outbox)
    _metric({**{f"CheckIn_{k}": v for k, v in res.outcomes.items()}, "Unreadable": unreadable,
             "Notifications": len(res.outbox)})
    return {"batchItemFailures": [{"itemIdentifier": i} for i in res.failed_ids]}


def _send_notifications(outbox) -> None:
    if not outbox:
        return
    sqs, url = _client("sqs"), os.environ["NOTIFY_QUEUE_URL"]
    for i in range(0, len(outbox), 10):
        chunk = outbox[i:i + 10]
        sqs.send_message_batch(QueueUrl=url, Entries=[
            {"Id": str(j), "MessageBody": json.dumps(o.__dict__, ensure_ascii=False)}
            for j, o in enumerate(chunk)])


# ---------- aggregator ----------

def _image(d: dict | None) -> dict | None:
    return {k: _deser.deserialize(v) for k, v in d.items()} if d else None


def aggregator_handler(event, context=None):
    changes = []
    for r in event["Records"]:
        # TTL expiry is a REMOVE by the DynamoDB service, 180 days later. It must not
        # decrement the counts of an event that ended long ago.
        if r.get("userIdentity", {}).get("principalId") == "dynamodb.amazonaws.com":
            continue
        ddb = r["dynamodb"]
        changes.append((_image(ddb.get("OldImage")), _image(ddb.get("NewImage"))))
    writes = aggregate.apply(_store(), changes)
    _metric({"CounterWrites": writes, "StreamRecords": len(changes)})
    return {"batchItemFailures": []}


# ---------- notifier ----------

def _sender() -> sms_gateway.SmsSender:
    if "sms" not in _cache:
        provider = os.environ.get("SMS_PROVIDER", "dryrun")
        if provider == "aggregator":
            token = _client("ssm").get_parameter(Name=os.environ["SMS_TOKEN_PARAM"],
                                                 WithDecryption=True)["Parameter"]["Value"]
            _cache["sms"] = sms_gateway.AggregatorHttp(os.environ["SMS_API_URL"], token,
                                                       os.environ.get("SMS_SENDER_ID", "SURAKSHIT"))
        elif provider == "aws":
            _cache["sms"] = sms_gateway.EndUserMessaging(_client("pinpoint-sms-voice-v2"),
                                                         os.environ["SMS_ORIGINATION"])
        else:
            _cache["sms"] = sms_gateway.DryRun()
    return _cache["sms"]


def notifier_handler(event, context=None):
    """Notify queue -> SMS. MaximumConcurrency on the event source keeps us under the provider's
    messages-per-second limit; the budget breaker keeps us under the money limit."""
    store, sender = _store(), _sender()
    budget = int(os.environ.get("SMS_BUDGET_PER_EVENT", "500000"))
    msgs = [(r["messageId"], json.loads(r["body"])) for r in event["Records"]]
    by_event: dict[str, int] = {}
    for _, m in msgs:
        by_event[m.get("event_id", "")] = by_event.get(m.get("event_id", ""), 0) + 1
    # Reserve the whole batch up front: one counter write per batch, not one per SMS.
    totals = {ev: store.add_sms_sent(ev, n) for ev, n in by_event.items()}
    failures, sent, skipped = [], 0, 0
    for mid, m in msgs:
        if not sms_gateway.allowed(m["kind"], totals[m.get("event_id", "")] - by_event[m.get("event_id", "")],
                                   budget):
            skipped += 1
            continue
        try:
            sender.send(m["to"], m["text"])
            sent += 1
        except ClientError as e:
            if _is_throttle(e):
                failures.append({"itemIdentifier": mid})
            else:
                log.warning("SMS to %s failed: %s", mask(m["to"]), e.response["Error"]["Code"])
        except OSError as e:  # aggregator timeout or HTTP error: retry later
            log.warning("SMS to %s failed: %s", mask(m["to"]), e)
            failures.append({"itemIdentifier": mid})
    _metric({"SmsSent": sent, "SmsSkippedBudget": skipped, "SmsRetried": len(failures)})
    return {"batchItemFailures": failures}


# ---------- activator ----------

def activator_handler(event, context=None):
    quakes = activation.parse_feed(event["feed"]) if "feed" in event else \
        activation.parse_feed(activation.fetch_feed(os.environ.get("USGS_FEED_URL") or activation.config.USGS_FEED_URL))
    store = _store()
    opened = activation.run(quakes, store)
    sns = _client("sns") if opened else None
    for e in opened:
        topic = os.environ["PAGE_TOPIC_ARN"] if e["severity"] == "major" else os.environ["EMAIL_TOPIC_ARN"]
        sns.publish(TopicArn=topic, Subject=f"Surakshit: event opened, M{e['mag']} {e['place']}"[:100],
                    Message=json.dumps(e, default=str, indent=2))
    _metric({"FeedQuakes": len(quakes), "EventsOpened": len(opened)})
    return {"quakes": len(quakes), "opened": [e["event_id"] for e in opened]}


# ---------- read API ----------

def _response(status: int, body: dict, kind: str | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if kind and status == 200:
        headers["cache-control"] = f"public, max-age={reads.CACHE_SECONDS[kind]}"
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, ensure_ascii=False)}


def api_handler(event, context=None):
    route = event.get("routeKey", "")
    store = _store()
    if route == "GET /status":
        phone = (event.get("queryStringParameters") or {}).get("phone", "")
        return _response(*reads.status_view(store, _pepper(), phone), "status")
    if route == "GET /events":
        return _response(*reads.events_view(store), "events")
    if route == "GET /summary/{event_id}":
        return _response(*reads.summary_view(store, event["pathParameters"]["event_id"]), "summary")
    if route == "GET /health":
        return _response(200, {"ok": True, "region": os.environ.get("AWS_REGION")})
    return _response(404, {"error": "not found"})
