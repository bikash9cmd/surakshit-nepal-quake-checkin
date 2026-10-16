from dataclasses import replace

from fastapi.testclient import TestClient

from surakshit.api import create_app
from surakshit.simulate import DESIGNS, Scenario, run


def test_local_api_flow():
    client = TestClient(create_app())
    r = client.post("/checkin", json={"phone": "9841234567", "status": "safe", "district": "Lalitpur"})
    assert r.status_code == 202
    r = client.get("/status", params={"phone": "+977 984-1234567"})
    assert r.json()["found"] and r.json()["status"] == "safe"
    assert r.headers["cache-control"] == "public, max-age=30"
    sms = client.post("/sms", json={"from": "9851234567", "text": "FIND 9841234567"}).json()
    assert "is SAFE in Lalitpur" in sms["outbound"][0]["text"]
    assert client.get("/").status_code == 200


SMALL = Scenario("test", people=1_000_000, scale=0.01, horizon_s=1800)


def test_queue_absorbs_what_a_cold_synchronous_table_drops():
    # 3,000 rps peak against a 1,000 WCU table: the synchronous design loses check-ins, the
    # queue-first design accepts all of them and writes them a little later.
    sync = run(SMALL, replace(DESIGNS["sync-naive"], warm_wcu=1_000))
    queued = run(SMALL, replace(DESIGNS["queue-cold"], warm_wcu=1_000))
    assert sync.lost_pct > 5
    assert queued.lost_pct == 0
    assert queued.visible_p99_s > sync.visible_p99_s


def test_no_api_throttling_below_the_quota():
    # Regression for the token-bucket bug: per-second throughput is the refill rate, not the burst.
    r = run(SMALL, replace(DESIGNS["queue-warm"], api_rps=5_000, api_burst=100))
    assert r.peak_arrivals_per_s < 5_000
    assert r.api_throttled == 0
