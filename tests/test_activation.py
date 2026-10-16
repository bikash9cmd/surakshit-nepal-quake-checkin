import pytest

from surakshit import activation
from surakshit.__main__ import _catalog
from surakshit.activation import Quake, decide, is_aftershock, parse_feed, run, to_event
from surakshit.store import LocalStore


@pytest.fixture(scope="module")
def catalog():
    return {q.usgs_id: q for q in _catalog()}


@pytest.mark.parametrize("usgs_id,severity", [
    ("us20002926", "major"),   # 2015 Gorkha M7.8
    ("us20002ejl", "major"),   # 2015 Dolakha M7.3, 17 days later
    ("us7000incn", "major"),   # 2022 Doti M5.7
    ("us6000lcgj", "major"),   # 2023 Bajhang M5.7
    ("us7000l8p5", "major"),   # 2023 Jajarkot M5.7, 153 deaths
    ("us6000pi9w", "major"),   # 2025 Tibet M7.1, strongly felt in Kathmandu
])
def test_every_damaging_quake_since_2015_opens_a_major_event(catalog, usgs_id, severity):
    q = catalog[usgs_id]
    d = decide(q)
    assert d.activate
    assert to_event(q, d)["severity"] == severity


@pytest.mark.parametrize("usgs_id", ["us2000iws8", "us7000pyq7", "us70008cld"])
def test_distant_tibetan_quakes_are_ignored(catalog, usgs_id):
    assert not decide(catalog[usgs_id]).activate


def test_deep_and_small_quakes_are_ignored():
    hindu_kush = Quake("x", ("x",), 0, 36.5, 70.9, 210.0, 6.4, "Hindu Kush")
    small = Quake("y", ("y",), 0, 27.7, 85.3, 10.0, 4.6, "Kathmandu")
    assert not decide(hindu_kush).activate and not decide(small).activate


def test_the_2015_sequence_opens_two_events_not_thirty(catalog):
    april_may = [q for q in catalog.values() if "2015-04-25" <= q.event_id[:10] <= "2015-05-31"]
    store = LocalStore()
    opened = run(sorted(april_may, key=lambda q: q.time_ms), store)
    assert [e["event_id"] for e in opened] == ["2015-04-25T0611Z-us20002926", "2015-05-12T0705Z-us20002ejl"]


def test_activation_is_idempotent_and_recognises_the_same_quake_under_another_id(catalog):
    store = LocalStore()
    gorkha = catalog["us20002926"]
    assert len(run([gorkha], store)) == 1
    assert run([gorkha], store) == []
    renamed = Quake("gcmt20150425061125", ("gcmt20150425061125", "us20002926"), gorkha.time_ms + 30_000,
                    gorkha.lat, gorkha.lon, gorkha.depth_km, 7.9, gorkha.place)
    assert is_aftershock(renamed, store.list_events())  # same quake, revised magnitude


def test_backtest_regression(catalog):
    store = LocalStore()
    run(sorted(catalog.values(), key=lambda q: q.time_ms), store)
    events = store.list_events(limit=100)
    assert len(catalog) == 88
    assert len(events) == 27
    assert sum(e["severity"] == "major" for e in events) == 6


def test_parse_feed_skips_blasts_and_keeps_all_ids():
    feed = {"features": [
        {"id": "us1", "geometry": {"coordinates": [85.3, 27.7, 12.0]},
         "properties": {"mag": 5.6, "time": 1000, "place": "Kathmandu", "type": "earthquake", "ids": ",us1,np2,"}},
        {"id": "us2", "geometry": {"coordinates": [85.3, 27.7, 0.0]},
         "properties": {"mag": 2.1, "time": 900, "place": "Quarry", "type": "quarry blast", "ids": ",us2,"}},
    ]}
    quakes = parse_feed(feed)
    assert [q.usgs_id for q in quakes] == ["us1"]
    assert quakes[0].ids == ("us1", "np2")


def test_felt_radius_is_a_step_function():
    assert activation.felt_radius_km(4.9) == 0
    assert activation.felt_radius_km(5.0) == 80
    assert activation.felt_radius_km(7.8) == 600
