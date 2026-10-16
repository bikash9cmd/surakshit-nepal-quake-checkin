"""Runs the surge simulation, writes artifacts/reports/surge.json and the charts in docs/img."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import numpy as np

from surakshit import config
from surakshit.simulate import DESIGNS, SCENARIOS, arrival_times, run


def surge_report(fast: bool = False) -> list[dict]:
    scenarios = [replace(s, scale=s.scale / 5) for s in SCENARIOS] if fast else SCENARIOS
    results = []
    for sc in scenarios:
        for key, d in DESIGNS.items():
            r = asdict(run(sc, d))
            r["key"] = key
            results.append(r)
            print(f"{sc.name:<34} {d.name:<58} lost {r['lost_pct']:>5.1f}%  "
                  f"p99 visible {r['visible_p99_s']:>6.0f}s")
    out = config.ARTIFACT_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "surge.json").write_text(json.dumps(results, indent=2))
    if not fast:
        _charts(results)
    _markdown(results)
    return results


def _markdown(results: list[dict]) -> None:
    print("\n| Design | " + " | ".join(s.name for s in SCENARIOS) + " |")
    print("|---|" + "---:|" * len(SCENARIOS))
    for key, d in DESIGNS.items():
        cells = []
        for s in SCENARIOS:
            r = next(x for x in results if x["key"] == key and x["people"] == s.people)
            cells.append(f"{r['lost_pct']:.1f}% lost, p99 {r['visible_p99_s']:.0f} s")
        print(f"| {d.name} | " + " | ".join(cells) + " |")


def _charts(results: list[dict]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    img = config.ROOT / "docs" / "img"
    img.mkdir(parents=True, exist_ok=True)

    # 1. The surge itself, for the largest scenario, against the limits that bind.
    sc = SCENARIOS[-1]
    t = arrival_times(sc, np.random.default_rng(sc.seed))
    per_s = np.bincount(t, minlength=sc.horizon_s) / sc.scale
    per_min = per_s[: 120 * 60].reshape(-1, 60).mean(axis=1)
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(np.arange(len(per_min)), per_min, color="#1f6f8b", lw=2, label="check-in attempts (1-min mean)")
    ax.axhline(10_000, color="#c0392b", ls="--", lw=1.2, label="API Gateway default account quota (10k rps)")
    ax.axhline(4_000, color="#e67e22", ls=":", lw=1.4, label="New/idle DynamoDB on-demand table (4k WCU)")
    for s_after, label in ((237, "M6.1"), (2036, "M6.6")):
        ax.axvline(s_after / 60, color="grey", lw=0.8)
        ax.text(s_after / 60 + 0.5, per_min.max() * 0.6, label, fontsize=8, color="grey")
    ax.set_xlabel("minutes after the M7.8 main shock (aftershock times from the 2015 USGS catalogue)")
    ax.set_ylabel("requests per second")
    ax.set_title(f"Modelled check-in surge, {sc.name}")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(img / "surge_curve.png", dpi=130)
    plt.close(fig)

    # 2. Lost check-ins by design and surge size.
    keys = list(DESIGNS)
    fig, ax = plt.subplots(figsize=(9, 4))
    width = 0.8 / len(SCENARIOS)
    colors = ["#9ecae1", "#4292c6", "#08519c"]
    for i, s in enumerate(SCENARIOS):
        vals = [next(r for r in results if r["key"] == k and r["people"] == s.people)["lost_pct"] for k in keys]
        ax.bar(np.arange(len(keys)) + i * width, vals, width, label=s.name, color=colors[i])
    ax.set_xticks(np.arange(len(keys)) + width, [DESIGNS[k].name.split(" ", 1)[0].rstrip(".") for k in keys])
    ax.set_xlabel("design (each adds one change to the previous; see table in README)")
    ax.set_ylabel("check-ins lost (%)")
    ax.set_title("Check-ins that never reached the system")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(img / "lost_by_design.png", dpi=130)
    plt.close(fig)
