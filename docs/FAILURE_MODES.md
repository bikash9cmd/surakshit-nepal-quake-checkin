# What breaks, and what happens when it does

The question I designed around: *a person in Sindhupalchok presses "Send" twelve minutes after a M7.8. What has to go right for their daughter in Doha to see "safe" a minute later, and what happens when each piece doesn't?*

## Measured: what each missing safeguard costs

From the design ladder in the surge simulation ([SURGE_MODEL.md](SURGE_MODEL.md); rows refer to the README table), 6M-person scenario unless stated:

| Without | Check-ins lost | Slowest 1% visible after | Why |
|---|---:|---:|---|
| None (Surakshit) | 0% | 8 s | |
| Table pre-warming (row 3) | 0% | 407 s | Queue absorbs it; the table ramps from 4,000 writes/s |
| The queue: synchronous writes, clients that try 6 times (row 1) | 16.8% | 5 s* | Throttled writes become errors; clients give up after 6 tries |
| API quota increase, 15M scenario (row 5) | 0% | 1,358 s | Offline-first page keeps retrying through the 429s |
| API quota increase *and* the offline-first page, 15M (row 4) | 16.6% | 41 s* | People give up |

\* For check-ins that made it. The fast p99 of the synchronous design is survivorship: the slow ones were lost.

## Every failure mode I could think of

| What fails | How it shows up | How it's detected | What the system does | Residual risk |
|---|---|---|---|---|
| Surge beyond table capacity | Writer gets throttled | `WriteThrottleAlarm`; queue age rises | Throttled messages go back to the queue via partial batch response; nothing lost | Check-ins visible later; `CheckInsBacklogAlarm` pages at 5 min |
| Surge beyond API Gateway quota | 429s to clients | API Gateway 4xx metrics | Page keeps the check-in locally and retries with jittered backoff up to 5 min | Delay; quota increase is a readiness item ([RUNBOOK.md](RUNBOOK.md)) |
| Writer Lambda bug (every message fails) | Nothing gets written | Writer `Errors`; backlog alarm | Messages retried 8 times, then parked in the DLQ (`CheckInDLQAlarm` pages); queue keeps 14 days | Fix and redrive the DLQ; nothing lost, everything late |
| One malformed message | Parse error | `CheckIn_invalid` metric | Dropped and counted, *not* retried, so it can't block the batch | A real check-in with a typo in the number is dropped; the page validates the number first |
| Duplicate delivery (SQS at-least-once) | Same message twice | `CheckIn_duplicate` metric | Recognised by request id; not counted twice; notification re-sent | A contact may get the same SMS twice (deliberate) |
| Messages out of order | "safe" (12:01) processed after "need help" (12:05) | `CheckIn_stale` metric | Conditional write: newer server time wins | None |
| Phone clock wrong | Client time years off | n/a | Ignored: order uses API Gateway's request time | None |
| Someone scripts 1,000 updates for one number | Repeated writes | `CheckIn_rate_limited` | First 20 updates per person per event count, the rest are rejected by the condition | The 20th update could be malicious (see [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md)) |
| Aggregator fails | Counters stop moving | Iterator age; `StreamDLQAlarm` | Bisects the batch to isolate a bad record; after 5 retries the record goes to the stream DLQ | Counters too low; check-ins and lookups unaffected |
| Counter double-count after a retried stream batch | Counts slightly high | Not detected live | Documented as approximate in the API response | Exact numbers come from an export of CheckIns, not counters |
| SMS provider down or slow | Notifications back up | Notify queue age; `NotifyDLQAlarm` | Throttles and timeouts are retried; check-ins unaffected (separate queue) | Families learn by lookup instead of SMS |
| SMS budget exhausted | Notifications skipped | `SmsBudgetAlarm` pages | Replies to SMS users still go out; contact notifications stop | A family that depended on the SMS notification has to look up; the operator decides whether to raise the budget |
| Someone floods the SMS webhook | Fake inbound messages | Authorizer denials in API logs | Token authorizer rejects requests without the aggregator's token | A leaked token: rotate in SSM, the authorizer cache expires within 5 min |
| USGS feed down or changed | Activator errors | `ActivatorFailingAlarm` (15 min) | Nothing opens automatically; operator opens by hand (`scripts/open_event.py`) | Minutes of delay on the banner. **Check-ins still work without an event** (they file under "no event") |
| Scheduler disabled or deleted | Nothing runs, so nothing errors | `ActivatorNotRunningAlarm` on *zero invocations*, missing data = breaching | n/a | This is the failure an error-only alarm misses |
| Quake USGS rates below threshold but deadly | No event opens | Humans notice | Manual open | See [ACTIVATION.md](ACTIVATION.md), "Known misses" |
| Two activator runs overlap | Same event created twice | Prevented | Reserved concurrency 1 + conditional put on the event id | None |
| Mumbai region degraded | API errors | CloudFront origin 5xx; AWS Health | Reads fail over to Singapore automatically; writes by promoting the standby (~5 min, [RUNBOOK.md](RUNBOOK.md)) | Check-ins accepted but unwritten in Mumbai's queue wait for recovery (below) |
| Pepper differs between regions | After failover, every lookup says "no check-in" | Drill (lookup a known number in each region) | `scripts/bootstrap_params.sh` writes the same value to both | Silent until tested: why the drill exists |
| Cost runaway (bot traffic, SMS) | Bill grows | AWS Budget at 80% actual and 100% forecast; SMS breaker | WAF rate limits outside Nepal; SMS budget | Budget alerts lag by hours; SMS is the only line item that can get large fast |

## A region failure in the middle of a surge

The worst coincidence: Mumbai has a regional problem 10 minutes after a M7.8, at peak.

| Data | Where it is | What happens |
|---|---|---|
| Written check-ins | Both regions (replicated within ~1 s) | Lookups keep working: CloudFront fails `GET`s over to Singapore on 5xx |
| Accepted, not yet written | Mumbai's SQS queue (durable across 3 AZs) | Not lost, but unreachable until Mumbai recovers. In the 15M scenario the queue peaks at **~176,000 messages** for a few seconds; in steady surge it's a few seconds of traffic |
| New check-ins | Clients | Get errors, keep retrying (offline-first). After promotion, they succeed in Singapore |
| Counters | Meta global table | Stop moving until the standby's aggregator is enabled at promotion |

**The honest RPO** is therefore "zero for anything written, and up to a few seconds of accepted check-ins delayed until the region recovers". The fix, if a partner required it, is for the page to keep its local copy until a lookup confirms the write, which roughly doubles read traffic during the surge. I'd rather spend that on the read cache.

## Known gaps (not built, deliberately listed)

1. **No load test against real AWS.** The surge numbers are a model. The next step is a 15-minute test with a load generator in two regions at 5,000 req/s, which would cost tens of dollars and would replace the model's assumptions about writer throughput with measurements.
2. **Spoofed "safe" check-ins.** Anyone can submit for any number on the web. See ADR-008 for why there's no OTP, and SECURITY_AND_PRIVACY.md for what would change that.
3. **No automatic write failover.** Promotion is a runbook step by design (ARCHITECTURE.md, "Why writes don't fail over automatically").
4. **The edge lives in the primary stack.** CloudFront is global, but it's defined in the Mumbai stack, so changing it during a Mumbai outage means the console (RUNBOOK.md). It belongs in its own us-east-1 stack with the WAF.
5. **Export for responders.** An S3 export of CheckIns (point-in-time export, no table load) for Athena queries is the obvious next feature and isn't built.
