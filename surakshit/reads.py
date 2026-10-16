"""Read-side responses, shared by the local API and the API Lambda.

The status lookup shows *only* status, district, time and which quake. The free-text note is
sent to the contacts the person chose, never shown to whoever types their number in: anyone can
look up any number, so the lookup must not reveal more than "this person checked in".
"""

from __future__ import annotations

from surakshit.phone import InvalidPhone, mask, normalise, phone_key
from surakshit.writer import NO_EVENT

# Seconds CloudFront may cache each response. A family refreshing the same number every few
# seconds is served from the edge; a new check-in shows up within this long.
CACHE_SECONDS = {"status": 30, "summary": 60, "events": 60}


def status_view(store, pepper: bytes, raw_phone: str) -> tuple[int, dict]:
    try:
        phone = normalise(raw_phone)
    except InvalidPhone:
        return 400, {"error": "Enter a Nepal mobile number, e.g. 98XXXXXXXX"}
    item = store.latest_checkin(phone_key(phone, pepper))
    if not item:
        return 200, {"phone": mask(phone), "found": False,
                     "message": "No check-in yet. That is not bad news: networks may be down."}
    return 200, {
        "phone": mask(phone), "found": True, "status": item["status"],
        "district": item.get("district"), "received_at": int(item["received_at"]),
        "event_id": None if item["sk"] == NO_EVENT else item["sk"], "channel": item.get("channel"),
    }


def summary_view(store, event_id: str) -> tuple[int, dict]:
    by_district = store.summary(event_id)
    totals = {s: sum(d[s] for d in by_district.values()) for s in ("safe", "need_help", "injured")}
    return 200, {"event_id": event_id, "totals": totals, "by_district": by_district,
                 "note": "Live counts for situational awareness; approximate under retries."}


def events_view(store, limit: int = 10) -> tuple[int, dict]:
    keep = ("event_id", "started_at", "mag", "place", "nearest_city", "severity")
    return 200, {"events": [{k: e.get(k) for k in keep} for e in store.list_events(limit=limit)]}
