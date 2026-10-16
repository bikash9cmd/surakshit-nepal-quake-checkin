"""Automatic activation: open an event when USGS reports a quake that people in Nepal will have felt.

The service is dormant almost all the time. Nobody should have to wake up, log in to AWS and
press a button at 12:00 on a Saturday while the building is shaking: a scheduled Lambda reads the
USGS feed every minute and opens an event on its own. An operator can still open or close one
by hand (scripts/open_event.py), and a drill uses the same path.

The rule is deliberately simple and is backtested against every M5+ quake near Nepal since 2015
(`python -m surakshit activation-backtest`, results in docs/ACTIVATION.md).
"""

from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from surakshit import config


@dataclass
class Quake:
    usgs_id: str
    ids: tuple[str, ...]            # every id networks have given this quake (USGS merges them)
    time_ms: int
    lat: float
    lon: float
    depth_km: float
    mag: float
    place: str

    @property
    def event_id(self) -> str:
        t = datetime.fromtimestamp(self.time_ms / 1000, UTC)
        return f"{t:%Y-%m-%dT%H%MZ}-{self.usgs_id}"


@dataclass
class Decision:
    activate: bool
    reason: str
    nearest_city: str | None = None
    distance_km: float | None = None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def felt_radius_km(mag: float) -> float:
    """Step function over config.FELT_RADIUS_KM: the radius for the largest threshold <= mag."""
    radius = 0.0
    for m, r in sorted(config.FELT_RADIUS_KM.items()):
        if mag >= m:
            radius = r
    return radius


def nearest_city(lat: float, lon: float) -> tuple[str, float]:
    return min(((name, haversine_km(lat, lon, *ll)) for name, ll in config.NEPAL_CITIES.items()),
               key=lambda t: t[1])


def decide(q: Quake) -> Decision:
    if q.mag < config.ACTIVATION_MIN_MAGNITUDE:
        return Decision(False, f"M{q.mag} below M{config.ACTIVATION_MIN_MAGNITUDE}")
    if q.depth_km > config.ACTIVATION_MAX_DEPTH_KM:
        return Decision(False, f"deep ({q.depth_km:.0f} km)")
    city, d = nearest_city(q.lat, q.lon)
    radius = felt_radius_km(q.mag)
    if d > radius:
        return Decision(False, f"{d:.0f} km from {city}, felt radius {radius:.0f} km", city, round(d))
    return Decision(True, f"M{q.mag} {d:.0f} km from {city}", city, round(d))


def aftershock_window_hours(main_mag: float) -> float:
    hours = 0.0
    for m, h in sorted(config.AFTERSHOCK_WINDOW_HOURS.items()):
        if main_mag >= m:
            hours = h
    return hours


def is_aftershock(q: Quake, events: list[dict]) -> dict | None:
    """The open event this quake belongs to, if any (see config.AFTERSHOCK_* for the rule)."""
    for e in events:
        if set(q.ids) & set(e.get("usgs_ids", [])):
            return e  # the same quake, re-reported (USGS revises location and magnitude)
        hours = (q.time_ms - int(e["started_at"])) / 3_600_000
        main = float(e["mag"])
        if 0 <= hours <= config.AFTERSHOCK_FIRST_HOURS and q.mag < main:
            return e
        if 0 <= hours <= aftershock_window_hours(main) and q.mag <= main - config.AFTERSHOCK_MARGIN:
            return e
    return None


def to_event(q: Quake, d: Decision) -> dict:
    return {
        "event_id": q.event_id, "started_at": q.time_ms, "mag": q.mag, "place": q.place,
        "lat": q.lat, "lon": q.lon, "depth_km": q.depth_km, "usgs_ids": list(q.ids),
        "nearest_city": d.nearest_city, "distance_km": d.distance_km, "opened_by": "usgs-feed",
        "severity": "major" if q.mag >= config.MAJOR_MAGNITUDE else "watch",
    }


def parse_feed(geojson: dict) -> list[Quake]:
    """USGS GeoJSON summary feed -> quakes. Skips non-earthquakes (quarry blasts, explosions)."""
    out = []
    for f in geojson.get("features", []):
        p, (lon, lat, depth) = f["properties"], f["geometry"]["coordinates"][:3]
        if p.get("type", "earthquake") != "earthquake" or p.get("mag") is None:
            continue
        ids = tuple(i for i in (p.get("ids") or f["id"]).split(",") if i)
        out.append(Quake(usgs_id=f["id"], ids=ids, time_ms=int(p["time"]), lat=float(lat),
                         lon=float(lon), depth_km=float(depth or 0), mag=float(p["mag"]),
                         place=p.get("place") or ""))
    return sorted(out, key=lambda q: q.time_ms)


def fetch_feed(url: str = config.USGS_FEED_URL, timeout: float = 5.0) -> dict:
    if not url.startswith("https://"):
        raise ValueError("feed URL must be https")
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https URL
        return json.load(r)


def run(quakes: list[Quake], store) -> list[dict]:
    """One activation pass: open an event for each new qualifying quake. Idempotent: the
    scheduler fires every minute and the feed repeats the same quakes for an hour."""
    opened = []
    for q in quakes:
        d = decide(q)
        if not d.activate:
            continue
        if is_aftershock(q, store.list_events(limit=20)):
            continue
        event = to_event(q, d)
        if store.create_event(event):
            opened.append(event)
    return opened
