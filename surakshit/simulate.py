"""Surge simulator: what happens to each design in the three hours after a large quake.

You can't load-test an earthquake, and the AWS limits that matter (API Gateway's account
throttle, DynamoDB's on-demand ramp, Lambda's SQS scaling rate) only show up at volumes that
would cost real money to generate. So this is a model, and it says so: an agent-based
simulation, one second per step, where each agent is a person trying to check in, and each
AWS service is reduced to the documented limit that binds first.

What is modelled (sources in docs/SURGE_MODEL.md):
  * Arrivals: a burst after the main shock and a smaller wave after each M6+ aftershock, timed
    from the real 25 April 2015 USGS catalogue (M6.1 at +4 min, M6.6 at +34 min).
  * API Gateway: a token bucket (default account quota 10,000 requests/s, burst 5,000).
  * DynamoDB on-demand: a new or idle table sustains 4,000 writes/s; it can serve up to double
    its previous peak instantly, but growth beyond that within 30 minutes may be throttled.
    Modelled conservatively: capacity is fixed at max(warm throughput, 2 x prior peak) for the
    first 30 minutes, then doubles relative to the peak reached so far, every 30 minutes.
  * Lambda on SQS: starts with 5 concurrent batches and adds up to 300 per minute.
  * Clients: either retry immediately (what a naive `fetch` loop does) or back off
    exponentially with full jitter. Either way they give up after 6 attempts.

What is not modelled: Nepal's mobile network itself (in 2015 it was congested, not the
servers), cold starts, and reads (handled by CloudFront, see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np

# 25 April 2015 sequence, seconds after the M7.8 main shock (USGS us20002926 at 06:11:25 UTC),
# aftershocks of M6+ in the first three hours: us20002bi4 M6.1 06:15:22, us2000292y M6.6 06:45:21.
GORKHA_M6_AFTERSHOCKS_S = (237, 2036)


@dataclass
class Scenario:
    name: str = "base"
    people: int = 2_000_000          # people who try to check in within three hours
    peak_minute: float = 8.0         # arrivals peak this many minutes after the shock
    aftershock_fraction: float = 0.3  # share who update again after each M6+ aftershock
    horizon_s: int = 3 * 3600
    scale: float = 0.1               # agents per person; capacities are scaled to match
    seed: int = 7


# Who tries to check in within three hours. No one knows this number; these bracket it.
# Facebook reported "millions" marked safe after the 2015 quake. Nepal's 2021 census counted
# 29.2 million people. See docs/SURGE_MODEL.md for the reasoning.
SCENARIOS = [
    Scenario("2M people (Valley-scale)", people=2_000_000, scale=0.1),
    Scenario("6M people (central Nepal)", people=6_000_000, scale=0.05),
    Scenario("15M people (national + diaspora)", people=15_000_000, scale=0.02),
]


@dataclass
class Design:
    name: str
    queue: bool                      # storage-first (API -> SQS) or synchronous (API -> Lambda -> table)
    backoff: bool                    # client: exponential backoff with full jitter, or immediate retry
    warm_wcu: int = 4_000            # DynamoDB write capacity available at t=0
    prior_peak_wcu: int = 0          # highest write rate the table has seen before the event
    api_rps: int = 10_000
    api_burst: int = 5_000
    writer_items_per_s_per_invoke: int = 150   # one Lambda doing 100 conditional writes one by one (~6 ms each)
    writer_max_concurrency: int = 1_000        # SQS event source mapping cap
    max_attempts: int = 6
    backoff_cap_s: float = 60.0


# Each design adds one change to the one before, so each row of the results table isolates
# the effect of one decision.
DESIGNS = {
    "sync-naive": Design("1. Synchronous, clients retry immediately", queue=False, backoff=False),
    "sync-backoff": Design("2. + clients back off with jitter", queue=False, backoff=True),
    "queue-cold": Design("3. + storage-first queue (table not pre-warmed)", queue=True, backoff=True),
    "queue-warm": Design("4. + table pre-warmed to 20k WCU", queue=True, backoff=True, warm_wcu=20_000),
    "offline-first": Design("5. + offline-first page: keeps retrying for hours", queue=True, backoff=True,
                            warm_wcu=20_000, max_attempts=10_000, backoff_cap_s=300.0),
    "surakshit": Design("6. + API Gateway quota raised to 30k rps (Surakshit)", queue=True, backoff=True,
                        warm_wcu=20_000, max_attempts=10_000, backoff_cap_s=300.0,
                        api_rps=30_000, api_burst=15_000),
}


@dataclass
class Result:
    scenario: str
    design: str
    people: int
    peak_arrivals_per_s: int
    accepted_first_try_pct: float
    lost_pct: float                 # gave up: the check-in never reached the system
    unsent_pct: float               # still retrying when the simulation ended (4 h)
    requests_per_person: float
    api_throttled: int              # requests rejected with 429 (each retry counts again)
    table_limited_s: int            # seconds in which DynamoDB capacity was the binding limit
    visible_p50_s: float
    visible_p95_s: float
    visible_p99_s: float
    max_queue_depth: int
    all_visible_by_s: float


def arrival_times(sc: Scenario, rng: np.random.Generator) -> np.ndarray:
    """Second at which each agent first tries. Gamma(k=2) kernel: zero at the shock (people are
    taking cover), peaking at peak_minute, long tail."""
    n_main = int(sc.people * sc.scale)
    theta = sc.peak_minute * 60  # Gamma(2, theta) has its mode at theta
    times = [rng.gamma(2.0, theta, n_main)]
    for t_after in GORKHA_M6_AFTERSHOCKS_S:
        n = int(n_main * sc.aftershock_fraction)
        times.append(t_after + rng.gamma(2.0, theta / 2, n))
    t = np.concatenate(times)
    return np.floor(t[t < sc.horizon_s]).astype(np.int64)


def _retry_delay(attempt: np.ndarray, backoff: bool, cap_s: float, rng: np.random.Generator) -> np.ndarray:
    if not backoff:
        return np.ones_like(attempt)  # a tight loop with a ~1 s round trip
    cap = np.minimum(cap_s, 1.0 * 2.0 ** np.minimum(attempt, 30))  # 2, 4, 8, ... capped
    return np.maximum(1, np.ceil(rng.uniform(0, cap))).astype(np.int64)  # full jitter


def run(sc: Scenario, d: Design) -> Result:
    rng = np.random.default_rng(sc.seed)
    first = arrival_times(sc, rng)
    n = len(first)
    horizon = sc.horizon_s + 3600  # let the tail drain
    s = sc.scale

    next_try = first.copy()
    attempts = np.zeros(n, dtype=np.int64)
    state = np.zeros(n, dtype=np.int8)        # 0 trying, 1 accepted, 2 gave up
    accepted_at = np.full(n, -1, dtype=np.int64)
    visible_at = np.full(n, -1, dtype=np.int64)

    tokens = d.api_burst * s
    queue: list[np.ndarray] = []              # FIFO of agent-index arrays (standard SQS is roughly FIFO)
    queue_len = 0
    max_queue = 0
    writer_conc = 5
    api_throttled = table_throttled = 0
    window_peak = 0.0
    capacity = max(d.warm_wcu, 2 * d.prior_peak_wcu) * s

    for t in range(horizon):
        if t and t % 1800 == 0:               # every 30 minutes the on-demand ceiling may double
            capacity = max(capacity, 2 * window_peak)
        # Token bucket: the bucket holds at most `burst` tokens and refills at `rps` per second,
        # so within one second up to (tokens left over + rps) requests can pass.
        available = tokens + d.api_rps * s

        due = np.flatnonzero((next_try == t) & (state == 0))
        if len(due):
            rng.shuffle(due)
            attempts[due] += 1
            k = int(min(len(due), available))
            available -= k
            ok, rejected = due[:k], due[k:]
            api_throttled += len(rejected)

            if d.queue:
                state[ok] = 1
                accepted_at[ok] = t
                if len(ok):
                    queue.append(ok)
                    queue_len += len(ok)
            else:
                w = int(min(len(ok), capacity))
                window_peak = max(window_peak, w)
                written, throttled = ok[:w], ok[w:]
                state[written] = 1
                accepted_at[written] = t
                visible_at[written] = t
                table_throttled += int(len(throttled) > 0)
                rejected = np.concatenate([rejected, throttled])

            if len(rejected):
                give_up = attempts[rejected] >= d.max_attempts
                state[rejected[give_up]] = 2
                retry = rejected[~give_up]
                next_try[retry] = t + _retry_delay(attempts[retry], d.backoff, d.backoff_cap_s, rng)

        tokens = min(d.api_burst * s, available)

        if d.queue and queue_len:
            if t % 60 == 0:                   # Lambda adds up to 300 concurrent pollers per minute
                wanted = int(np.ceil(queue_len / (d.writer_items_per_s_per_invoke * s)))
                writer_conc = int(min(d.writer_max_concurrency, max(5, min(wanted, writer_conc + 300))))
            drain = int(min(queue_len, writer_conc * d.writer_items_per_s_per_invoke * s, capacity))
            table_throttled += int(min(queue_len, writer_conc * d.writer_items_per_s_per_invoke * s) > capacity)
            window_peak = max(window_peak, drain)
            taken = 0
            while taken < drain and queue:
                head = queue[0]
                take = min(len(head), drain - taken)
                visible_at[head[:take]] = t
                if take == len(head):
                    queue.pop(0)
                else:
                    queue[0] = head[take:]
                taken += take
            queue_len -= taken
        max_queue = max(max_queue, queue_len)

    seen = visible_at >= 0
    latency = (visible_at[seen] - first[seen]).astype(float)
    hist = np.bincount(first)
    return Result(
        scenario=sc.name, design=d.name, people=sc.people,
        peak_arrivals_per_s=int(hist.max() / s),
        accepted_first_try_pct=round(100 * float(np.mean((attempts == 1) & (state == 1))), 2),
        lost_pct=round(100 * float(np.mean(state == 2)), 2),
        unsent_pct=round(100 * float(np.mean(state == 0)), 2),
        requests_per_person=round(float(attempts.sum()) / n, 2),
        api_throttled=int(api_throttled / s), table_limited_s=int(table_throttled),
        visible_p50_s=float(np.percentile(latency, 50)) if len(latency) else float("nan"),
        visible_p95_s=float(np.percentile(latency, 95)) if len(latency) else float("nan"),
        visible_p99_s=float(np.percentile(latency, 99)) if len(latency) else float("nan"),
        max_queue_depth=int(max_queue / s),
        all_visible_by_s=float(visible_at[seen].max() - first.min()) if seen.any() else float("nan"),
    )


def run_all(scenarios: list[Scenario] | None = None) -> list[dict]:
    scenarios = scenarios or SCENARIOS
    return [asdict(run(sc, d)) for sc in scenarios for d in DESIGNS.values()]


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2))
