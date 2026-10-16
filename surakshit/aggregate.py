"""Per-district summary counters, fed by the CheckIns table's DynamoDB stream.

Why a stream and not the writer: the writer would have to update counters *after* its write,
and a crash between the two leaves the counter permanently wrong (the redelivered message is a
duplicate, so it never gets counted). The stream sees every committed change exactly once per
item, in order, with both the old and the new image, so "safe -> need help" becomes
-1 safe, +1 need help without the writer knowing counters exist.

Counters are for situational awareness ("how many need help in Sindhupalchok right now"), not
for a census. A stream batch retried after a partial failure can double-count; the nightly
export (docs/ARCHITECTURE.md) is the source of truth for exact numbers.
"""

from __future__ import annotations

from collections import defaultdict

UNKNOWN_DISTRICT = "Unknown"


def deltas(changes: list[tuple[dict | None, dict | None]]) -> dict[str, dict[tuple[str, str], int]]:
    """Fold (old, new) item pairs into counter deltas per event:
    {event_id: {(district, status): +/-n}}. Changes that net to zero are left out, so a batch
    of 100 check-ins from one district becomes a handful of counter writes, not 100."""
    out: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    for old, new in changes:
        if old:
            out[old["sk"]][(old.get("district") or UNKNOWN_DISTRICT, old["status"])] -= 1
        if new:
            out[new["sk"]][(new.get("district") or UNKNOWN_DISTRICT, new["status"])] += 1
    return {ev: {k: v for k, v in d.items() if v} for ev, d in out.items() if any(d.values())}


def apply(store, changes: list[tuple[dict | None, dict | None]]) -> int:
    """Apply a batch of changes; returns the number of counter writes made."""
    writes = 0
    for event_id, d in deltas(changes).items():
        store.add_counts(event_id, d)
        writes += len(d)
    return writes
