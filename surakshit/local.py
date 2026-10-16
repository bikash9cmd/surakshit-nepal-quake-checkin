"""The whole system in one process: queue, writer, stream aggregator and SMS outbox, with the
same code the Lambdas run. Used by the local API (`make serve`), the demo and the tests."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter

from surakshit import aggregate, writer
from surakshit.store import LocalStore
from surakshit.writer import Envelope, Outbound

DEMO_PEPPER = b"local-demo-pepper-not-a-secret"


class LocalSystem:
    def __init__(self, pepper: bytes | None = None) -> None:
        self.store = LocalStore()
        self.pepper = pepper or os.environ.get("SURAKSHIT_PEPPER", "").encode() or DEMO_PEPPER
        self.queue: list[Envelope] = []
        self.outbox: list[Outbound] = []
        self.outcomes: Counter = Counter()

    # -- the two doors in --

    def submit_web(self, body: dict | str, received_at: int | None = None) -> str:
        """What API Gateway's SQS integration does: stamp time and request id, enqueue, return."""
        rid = uuid.uuid4().hex
        self.queue.append(Envelope(
            message_id=rid, source="web", body=body if isinstance(body, str) else json.dumps(body),
            received_at=received_at or int(time.time() * 1000), request_id=rid))
        return rid

    def submit_sms(self, sender: str, text: str, received_at: int | None = None) -> str:
        rid = uuid.uuid4().hex
        self.queue.append(Envelope(message_id=rid, source="sms", body=text, sender=sender,
                                   received_at=received_at or int(time.time() * 1000), request_id=rid))
        return rid

    # -- what the Lambdas do --

    def drain(self, batch_size: int = 100) -> Counter:
        """Writer (SQS batches of `batch_size`), then the stream aggregator on what was written."""
        while self.queue:
            batch, self.queue = self.queue[:batch_size], self.queue[batch_size:]
            res = writer.process(batch, self.store, self.pepper, self.store.list_events(limit=50))
            failed = set(res.failed_ids)
            self.queue.extend(e for e in batch if e.message_id in failed)
            aggregate.apply(self.store, res.changes)
            self.outbox.extend(res.outbox)
            self.outcomes.update(res.outcomes)
            if failed and len(failed) == len(batch):
                break  # nothing progressing; leave the rest for the next drain
        return self.outcomes
