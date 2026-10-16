"""The writer: drains the check-in queue into DynamoDB. Everything after "202 Accepted" happens here.

Rules this code enforces, each with a test in tests/test_writer.py:

* At-least-once delivery. SQS may hand the same message over twice. The API Gateway request id
  travels with the message and a redelivery is recognised as a duplicate, not a second update.
* Out-of-order delivery. A standard queue does not keep order, so "safe" sent at 12:01 can be
  processed after "need help" sent at 12:05. The later *server* timestamp wins; the client's
  clock is never trusted (a phone that has been flat for a day often boots with the wrong date).
* Poison messages are dropped and counted, not retried: retrying a malformed body five times
  just delays the good messages behind it.
* Throttling is retried. If DynamoDB says "slow down", the message goes back on the queue
  through a partial batch response, and only that message: the rest of the batch is kept.
* The phone number is never stored. The partition key is a keyed hash; contacts exist only
  inside the queue message and the outbound SMS.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field

from surakshit import config, messages
from surakshit.checkin import CheckIn, InvalidCheckIn, from_api, parse_sms
from surakshit.phone import InvalidPhone, normalise, phone_key
from surakshit.store import Store

log = logging.getLogger(__name__)

NO_EVENT = "0000-no-event"  # check-ins outside any event (drills, tests) still work

@dataclass
class Envelope:
    """One queued message, whichever door it came in by."""
    message_id: str                 # SQS message id, for partial batch failure reporting
    source: str                     # web | sms
    body: str
    received_at: int                # ms since epoch, from API Gateway or the SMS gateway
    request_id: str                 # idempotency key
    sender: str | None = None       # SMS only: the originating number


@dataclass
class Outbound:
    to: str
    text: str
    kind: str                       # notify | reply
    event_id: str = NO_EVENT        # which event's SMS budget this counts against


@dataclass
class BatchResult:
    failed_ids: list[str] = field(default_factory=list)
    outbox: list[Outbound] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)
    changes: list[tuple[dict | None, dict]] = field(default_factory=list)


class Retryable(Exception):
    """DynamoDB throttled or a dependency timed out: put the message back on the queue."""


def event_for(received_at: int, events: list[dict]) -> str:
    """The event a check-in belongs to: the latest one that had started when it was received."""
    started = [e for e in events if int(e["started_at"]) <= received_at]
    return max(started, key=lambda e: int(e["started_at"]))["event_id"] if started else NO_EVENT


def to_item(c: CheckIn, pepper: bytes) -> dict:
    return {
        "pk": phone_key(c.phone, pepper), "sk": c.event_id, "status": c.status,
        "district": c.district, "note": c.note, "received_at": c.received_at,
        "checkin_id": c.checkin_id, "channel": c.channel,
        "expires_at": c.received_at // 1000 + config.RETENTION_DAYS * 86400,
    }


def process(batch: list[Envelope], store: Store, pepper: bytes, events: list[dict],
            is_throttle=lambda e: False) -> BatchResult:
    res = BatchResult()
    for env in batch:
        try:
            _one(env, store, pepper, events, res)
        except (InvalidCheckIn, InvalidPhone, json.JSONDecodeError, UnicodeDecodeError) as e:
            res.outcomes["invalid"] += 1
            log.info("dropped invalid message %s: %s", env.message_id, e)
        except Exception as e:
            if isinstance(e, Retryable) or is_throttle(e):
                res.outcomes["retry"] += 1
                res.failed_ids.append(env.message_id)
            else:
                # Unknown error: retry too. After maxReceiveCount the queue moves it to the DLQ,
                # which has an alarm. Losing a check-in silently is the one thing not allowed.
                res.outcomes["error"] += 1
                res.failed_ids.append(env.message_id)
                log.exception("unexpected error on %s", env.message_id)
    return res


def _one(env: Envelope, store: Store, pepper: bytes, events: list[dict], res: BatchResult) -> None:
    event_id = event_for(env.received_at, events)
    if env.source == "web":
        body = json.loads(env.body)
        c = from_api(body, event_id=event_id, received_at=env.received_at, checkin_id=env.request_id)
        _write(c, store, pepper, res)
        return

    # SMS
    sender = normalise(env.sender or "")
    cmd = parse_sms(env.body)
    if cmd.kind == "checkin":
        c = CheckIn(event_id=event_id, phone=sender, status=cmd.status, received_at=env.received_at,
                    checkin_id=env.request_id, channel="sms", district=cmd.district, note=cmd.note,
                    contacts=[n for n in cmd.numbers if n != sender], lang=cmd.lang)
        outcome = _write(c, store, pepper, res)
        if outcome in ("written", "duplicate"):
            res.outbox.append(Outbound(sender, messages.reply_checkin(c.status, cmd.lang), "reply", event_id))
    elif cmd.kind == "find" and cmd.numbers:
        target = cmd.numbers[0]
        found = store.latest_checkin(phone_key(target, pepper))
        res.outbox.append(Outbound(sender, messages.reply_find(target, found, cmd.lang), "reply", event_id))
        res.outcomes["find"] += 1
    else:
        res.outbox.append(Outbound(sender, messages.reply_help(cmd.lang), "reply", event_id))
        res.outcomes["unknown_sms"] += 1


def _write(c: CheckIn, store: Store, pepper: bytes, res: BatchResult) -> str:
    item = to_item(c, pepper)
    result = store.put_checkin(item)
    res.outcomes[result.outcome] += 1
    if result.outcome == "written":
        res.changes.append((result.old, item))
    # A duplicate still sends the notifications: if the first attempt wrote the item and then
    # crashed before queueing the SMS, this is the only chance. Worst case a contact gets the
    # same SMS twice, which beats never getting it.
    if result.outcome in ("written", "duplicate"):
        for contact in c.contacts:
            text = messages.notify_contact(c.phone, c.status, c.received_at, c.district, c.note, c.lang)
            res.outbox.append(Outbound(contact, text, "notify", c.event_id))
    return result.outcome
