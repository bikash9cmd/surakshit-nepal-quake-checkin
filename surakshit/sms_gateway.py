"""Outbound SMS behind one small interface, plus the budget breaker.

Why a Nepali aggregator and not only AWS End User Messaging: inbound (two-way) SMS needs a
number that Nepali subscribers can text cheaply, i.e. a local short code from NTC and Ncell, and
those are issued to local companies. Aggregators such as Sparrow SMS or Aakash SMS hold them and
forward inbound texts to a webhook. Outbound through the same aggregator costs roughly NPR 0.5-1.4
per message, against roughly USD 0.10+ per message through an international gateway.
So the code supports both and the template picks one (docs/ALTERNATIVES.md, ADR-006).

SMS is 99% of the cost of an event (docs/COST.md), so the notifier stops sending *contact
notifications* once an event's budget is spent. Replies to people who texted us still go out:
they are the only channel a feature-phone user has.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Protocol

from surakshit.phone import mask

log = logging.getLogger(__name__)


class SmsSender(Protocol):
    def send(self, to: str, text: str) -> None: ...


class DryRun:
    """Default. Logs (masked) instead of sending, so a test deploy can't text anybody."""

    def send(self, to: str, text: str) -> None:
        log.info("SMS (dry run) to %s: %s", mask(to), text[:40])


class AggregatorHttp:
    """A Nepali SMS aggregator's HTTP API. The shape (token + to + text, form-encoded) is common to
    the ones I looked at; the field names are configuration, not code."""

    def __init__(self, url: str, token: str, sender_id: str, timeout: float = 5.0) -> None:
        if not url.startswith("https://"):
            raise ValueError("aggregator URL must be https")
        self.url, self.token, self.sender_id, self.timeout = url, token, sender_id, timeout

    def send(self, to: str, text: str) -> None:
        data = urllib.parse.urlencode({"token": self.token, "from": self.sender_id,
                                       "to": to.removeprefix("+977"), "text": text}).encode()
        req = urllib.request.Request(self.url, data=data, method="POST")  # noqa: S310 - https enforced above
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
            body = r.read(2048)
            if r.status >= 300:
                raise RuntimeError(f"aggregator HTTP {r.status}: {body[:200]!r}")


class EndUserMessaging:
    """AWS End User Messaging SMS (pinpoint-sms-voice-v2). International rates; no inbound in Nepal."""

    def __init__(self, client, origination: str, configuration_set: str | None = None) -> None:
        self.client, self.origination, self.configuration_set = client, origination, configuration_set

    def send(self, to: str, text: str) -> None:
        kwargs = {"DestinationPhoneNumber": to, "MessageBody": text, "MessageType": "TRANSACTIONAL",
                  "OriginationIdentity": self.origination}
        if self.configuration_set:
            kwargs["ConfigurationSetName"] = self.configuration_set
        self.client.send_text_message(**kwargs)


def allowed(kind: str, sent_so_far: int, budget: int) -> bool:
    """The breaker. Replies always go; notifications stop once the budget is spent."""
    return kind == "reply" or sent_so_far < budget


def parse_inbound(body: str) -> dict:
    """Webhook body from the aggregator -> {from, text, id}. Accepts JSON with either our field
    names or the common alternatives."""
    d = json.loads(body)
    return {"from": d.get("from") or d.get("sender") or d.get("msisdn") or "",
            "text": d.get("text") or d.get("message") or d.get("keyword") or "",
            "id": str(d.get("id") or d.get("message_id") or "")}
