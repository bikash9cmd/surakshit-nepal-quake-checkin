"""Command line: python -m surakshit <command>

  demo                  replay the first hours of 25 April 2015 through the real writer and aggregator
  activation-backtest   run the activation rule over every M5+ quake near Nepal since 2015
  simulate              surge simulation: six designs x three surge sizes (about 3 minutes)
  cost                  monthly cost: idle, and during a 15-million-person event
  serve                 local API on http://localhost:8000
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import UTC, datetime

from surakshit import config


def _catalog() -> list:
    from surakshit.activation import Quake

    out = []
    with open(config.ROOT / "data" / "usgs_m5_2015_2026.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp() * 1000)
            out.append(Quake(r["id"], (r["id"],), t, float(r["latitude"]), float(r["longitude"]),
                             float(r["depth"]), float(r["mag"]), r["place"]))
    return out


def cmd_activation_backtest(_args) -> None:
    from surakshit.activation import decide, run
    from surakshit.store import LocalStore

    quakes = _catalog()
    store = LocalStore()
    opened = {e["event_id"] for e in run(quakes, store)}
    rows = []
    for q in quakes:
        d = decide(q)
        outcome = "opened event" if q.event_id in opened else ("joined event" if d.activate else "ignored")
        rows.append({"time_utc": datetime.fromtimestamp(q.time_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M"),
                     "mag": q.mag, "depth_km": q.depth_km, "place": q.place, "outcome": outcome,
                     "reason": d.reason, "usgs_id": q.usgs_id})
    events = store.list_events(limit=1000)
    summary = {
        "quakes": len(quakes), "qualifying": sum(r["outcome"] != "ignored" for r in rows),
        "events_opened": len(events), "major": sum(e["severity"] == "major" for e in events),
        "years": round((quakes[-1].time_ms - quakes[0].time_ms) / 3.156e10, 1),
    }
    out = config.ARTIFACT_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "activation_backtest.json").write_text(json.dumps({"summary": summary, "quakes": rows,
                                                              "events": events}, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    print("\n| Opened (UTC) | M | Place | Severity |\n|---|---:|---|---|")
    for e in sorted(events, key=lambda e: e["event_id"]):
        print(f"| {e['event_id'][:10]} {e['event_id'][11:13]}:{e['event_id'][13:15]} | {e['mag']} | "
              f"{e['place']} | {e['severity']} |")


def cmd_demo(args) -> None:
    from surakshit import activation, reads
    from surakshit.local import LocalSystem

    rng = random.Random(42)  # noqa: S311 - synthetic demo data
    system = LocalSystem()
    gorkha = next(q for q in _catalog() if q.usgs_id == "us20002926")
    event = activation.to_event(gorkha, activation.decide(gorkha))
    system.store.create_event(event)
    t0 = gorkha.time_ms
    print(f"Event opened: {event['event_id']}  M{event['mag']}  {event['place']}  ({event['severity']})\n")

    districts = ["Kathmandu"] * 5 + ["Lalitpur", "Bhaktapur", "Gorkha", "Sindhupalchok", "Nuwakot",
                                     "Dhading", "Rasuwa", "Kavre", None]
    weights = {"safe": 0.86, "need_help": 0.1, "injured": 0.04}
    phones = [f"98{rng.randrange(10**8):08d}" for _ in range(args.people)]
    for i, p in enumerate(phones):
        at = t0 + int(rng.gammavariate(2.0, 480) * 1000)
        status = rng.choices(list(weights), list(weights.values()))[0]
        body = {"phone": p, "status": status, "district": rng.choice(districts), "lang": "en"}
        if i % 7 == 0:
            body["contacts"] = [rng.choice(phones)]
        system.submit_web(body, received_at=at)
    # SMS from feature phones, in Nepali and English, plus a redelivery and an out-of-order pair.
    system.submit_sms("+9779841000001", "ठीक छु काठमाडौं", received_at=t0 + 300_000)
    system.submit_sms("9851000002", "HELP sindhupalchowk ghar bhatkiyo 9841000001", received_at=t0 + 420_000)
    system.submit_sms("९८६१०००००३", "khoj 9841000001", received_at=t0 + 900_000)
    late = system.submit_web({"phone": "9801000004", "status": "need_help"}, received_at=t0 + 600_000)
    system.submit_web({"phone": "9801000004", "status": "safe"}, received_at=t0 + 400_000)  # arrives after
    system.queue.append(next(e for e in system.queue if e.request_id == late))                # redelivery
    rng.shuffle(system.queue)  # standard SQS: no ordering
    outcomes = system.drain()

    print("Writer outcomes:", dict(outcomes))
    print("\nSummary for the event:")
    s = reads.summary_view(system.store, event["event_id"])[1]
    print(" totals:", s["totals"])
    for d, c in list(s["by_district"].items())[:6]:
        print(f"  {d:<14} {c}")
    print("\nLookups:")
    for p in ("9801000004", "9841000001", "+977-985-1000002", "9800000000"):
        print(" ", p, "->", json.dumps(reads.status_view(system.store, system.pepper, p)[1], ensure_ascii=False))
    print("\nSome outbound SMS:")
    for o in [o for o in system.outbox if o.kind == "reply"][:3] + [o for o in system.outbox if o.kind == "notify"][:2]:
        print(f"  [{o.kind}] {o.to[:6]}...: {o.text}")


def cmd_simulate(args) -> None:
    from surakshit.report import surge_report

    surge_report(fast=args.fast)


def cmd_cost(_args) -> None:
    from surakshit.cost import report

    report()


def cmd_serve(args) -> None:
    import uvicorn

    from surakshit.api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)


def main() -> None:
    p = argparse.ArgumentParser(prog="surakshit")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--people", type=int, default=5000)
    sub.add_parser("activation-backtest")
    s = sub.add_parser("simulate")
    s.add_argument("--fast", action="store_true", help="smaller agent sample (for CI)")
    sub.add_parser("cost")
    v = sub.add_parser("serve")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    {"demo": cmd_demo, "activation-backtest": cmd_activation_backtest, "simulate": cmd_simulate,
     "cost": cmd_cost, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    main()
