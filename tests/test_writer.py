import json

from surakshit import aggregate, config, writer
from surakshit.phone import phone_key
from surakshit.store import LocalStore
from surakshit.writer import Envelope

PEPPER = b"test-pepper"
EVENT = {"event_id": "2015-04-25T0611Z-us20002926", "started_at": 1_429_942_285_950, "mag": 7.8}
T0 = EVENT["started_at"]


def web(body: dict, at: int, rid: str) -> Envelope:
    return Envelope(message_id=f"m-{rid}", source="web", body=json.dumps(body), received_at=at, request_id=rid)


def run(store, *envs):
    res = writer.process(list(envs), store, PEPPER, [EVENT])
    aggregate.apply(store, res.changes)
    return res


def item(store, phone="+9779841234567"):
    return store.get_checkin(phone_key(phone, PEPPER), EVENT["event_id"])


def test_redelivered_message_is_a_duplicate_not_a_second_update():
    s = LocalStore()
    e = web({"phone": "9841234567", "status": "safe", "contacts": ["9851111111"]}, T0 + 1000, "r1")
    first, again = run(s, e), run(s, e)
    assert first.outcomes["written"] == 1 and again.outcomes["duplicate"] == 1
    assert item(s)["updates"] == 1
    assert s.summary(EVENT["event_id"])["Unknown"]["safe"] == 1
    # The duplicate re-sends the notification: the first attempt may have crashed before queueing it.
    assert [o.kind for o in again.outbox] == ["notify"]


def test_later_server_time_wins_whatever_order_messages_arrive_in():
    s = LocalStore()
    later = web({"phone": "9841234567", "status": "need_help", "district": "Gorkha"}, T0 + 600_000, "r2")
    earlier = web({"phone": "9841234567", "status": "safe"}, T0 + 300_000, "r1")
    res = run(s, later, earlier)
    assert res.outcomes == {"written": 1, "stale": 1}
    assert item(s)["status"] == "need_help"
    assert s.summary(EVENT["event_id"]) == {"Gorkha": {"safe": 0, "need_help": 1, "injured": 0}}


def test_status_change_moves_the_counter_and_clears_the_old_district():
    s = LocalStore()
    run(s, web({"phone": "9841234567", "status": "need_help", "district": "Gorkha"}, T0 + 1, "a"))
    run(s, web({"phone": "9841234567", "status": "safe"}, T0 + 2, "b"))
    assert item(s).get("district") is None
    summary = s.summary(EVENT["event_id"])
    assert summary["Gorkha"]["need_help"] == 0 and summary["Unknown"]["safe"] == 1


def test_updates_are_capped_per_person():
    s = LocalStore()
    envs = [web({"phone": "9841234567", "status": "safe"}, T0 + i, f"r{i}") for i in range(30)]
    res = run(s, *envs)
    assert res.outcomes["written"] == config.MAX_UPDATES_PER_PERSON
    assert res.outcomes["rate_limited"] == 30 - config.MAX_UPDATES_PER_PERSON


def test_invalid_messages_are_dropped_not_retried():
    s = LocalStore()
    bad = [Envelope("m1", "web", "{not json", T0, "r1"), web({"phone": "12", "status": "safe"}, T0, "r2"),
           web({"phone": "9841234567", "status": "maybe"}, T0, "r3")]
    res = run(s, *bad)
    assert res.outcomes["invalid"] == 3 and res.failed_ids == []


def test_wrongly_typed_optional_fields_are_stored_not_retried_to_the_dlq():
    s = LocalStore()
    res = run(s, web({"phone": "9841234567", "status": "injured", "note": 5, "district": 7,
                      "contacts": {"a": 1}}, T0, "r1"))
    assert res.failed_ids == [] and res.outcomes["written"] == 1
    assert item(s)["status"] == "injured"


def test_throttled_messages_are_returned_for_retry_and_only_those():
    class Flaky(LocalStore):
        def put_checkin(self, it):
            if it["checkin_id"] == "throttle-me":
                raise RuntimeError("ProvisionedThroughputExceededException")
            return super().put_checkin(it)

    s = Flaky()
    res = writer.process([web({"phone": "9841234567", "status": "safe"}, T0, "throttle-me"),
                          web({"phone": "9851234567", "status": "safe"}, T0, "fine")], s, PEPPER, [EVENT])
    assert res.failed_ids == ["m-throttle-me"]
    assert res.outcomes["written"] == 1


def test_phone_numbers_are_never_stored():
    s = LocalStore()
    run(s, web({"phone": "9841234567", "status": "safe", "contacts": ["9851111111"], "note": "at home"},
               T0, "r1"))
    stored = json.dumps(list(s.checkins.values()))
    for digits in ("9841234567", "9851111111"):
        assert digits not in stored


def test_check_in_before_any_event_still_works():
    s = LocalStore()
    res = writer.process([web({"phone": "9841234567", "status": "safe"}, T0 - 10_000, "r")], s, PEPPER, [EVENT])
    assert res.outcomes["written"] == 1
    assert s.latest_checkin(phone_key("+9779841234567", PEPPER))["sk"] == writer.NO_EVENT


def test_sms_checkin_find_and_help_replies():
    s = LocalStore()
    sms = [Envelope("s1", "sms", "SAHAYOG gorkha 9851111111", T0 + 5, "i1", sender="9841234567"),
           Envelope("s2", "sms", "FIND 9841234567", T0 + 9, "i2", sender="9861234567"),
           Envelope("s3", "sms", "what is this", T0 + 9, "i3", sender="9801234567")]
    res = run(s, *sms)
    kinds = [(o.to, o.kind) for o in res.outbox]
    assert ("+9779851111111", "notify") in kinds
    assert ("+9779841234567", "reply") in kinds
    find_reply = next(o.text for o in res.outbox if o.to == "+9779861234567")
    assert "NEEDS HELP" in find_reply and "Gorkha" in find_reply
    assert "SAFE, HELP or INJURED" in next(o.text for o in res.outbox if o.to == "+9779801234567")


def test_counter_deltas_fold_a_batch_into_few_writes():
    changes = [(None, {"sk": "e", "district": "Kathmandu", "status": "safe"}) for _ in range(100)]
    changes.append(({"sk": "e", "district": "Kathmandu", "status": "safe"},
                    {"sk": "e", "district": "Kathmandu", "status": "safe"}))  # no-op update
    assert aggregate.deltas(changes) == {"e": {("Kathmandu", "safe"): 100}}
