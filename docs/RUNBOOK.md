# Runbook

## Readiness checklist (do these before there is an earthquake)

Most of the ways this system fails under load are fixed by a support ticket or a setting, weeks in advance. None of them can be done in the ten minutes that matter.

| | Item | Why | How to check |
|---|---|---|---|
| ☐ | API Gateway account throttle raised to 30,000 req/s in **both** regions | Default 10,000 is passed at ~6M people with an early peak ([SURGE_MODEL.md](SURGE_MODEL.md)) | Service Quotas → API Gateway → "Throttle rate" |
| ☐ | Stage throttle (`ApiThrottleRate`) set just under the new quota | Otherwise the stage still caps at 9,000 | Stack parameter |
| ☐ | Lambda concurrent executions ≥ 3,000 in both regions | Writer (~150 at 15M) and read API share the pool | Service Quotas → Lambda |
| ☐ | CheckIns warm throughput ≥ 20,000 writes/s | A cold table ramps from 4,000 writes/s | `aws dynamodb describe-table --query Table.WarmThroughput` |
| ☐ | Same pepper in both regions | Otherwise lookups fail after failover | Drill step 4 below |
| ☐ | SMS aggregator contract: short code, two-way, agreed TPS, account topped up | Inbound SMS can't be arranged on the day | Send a test SMS in a drill |
| ☐ | On-call email reaches a phone that wakes someone | Major events page this address | Drill |
| ☐ | Drill done in the last 6 months | Untested failover doesn't work | Below |

## Drill (twice a year)

1. Open a drill event: `python scripts/open_event.py --id 2026-10-20T0400Z-drill --mag 0 --place "Drill"`.
2. Check in from the web page, from SMS (`THIK`), and look both up.
3. Check the summary shows both, in the right districts.
4. **Promote the standby** (below), look the same numbers up through the standby API directly, check in once there, then fail back.
5. Delete the drill check-ins (they expire anyway) and note the time each step took.

## When the pager goes (major event opened)

1. **Look at the backlog.** `CheckInsBacklogAlarm` (oldest message > 5 min) is the one number that says whether families are seeing check-ins. Queue depth itself is fine; age is what matters.
2. **Throttles are expected.** `WriteThrottleAlarm` during the first minutes means the queue is doing its job. Act only if backlog age keeps growing.
3. **SMS budget.** If `SmsBudgetAlarm` fires, decide whether to raise `SmsBudgetPerEvent` (a stack parameter, safe to change mid-event) given the aggregator balance.
4. **DLQ.** Anything in `CheckInDLQ` is a check-in that failed 8 times: read one, fix the cause, redrive (`aws sqs start-message-move-task`).
5. **Tell people.** The page banner shows the event automatically; the partner organisation should share the URL and the SMS short code.

## Promote the standby region (Mumbai unavailable)

Reads have already failed over (CloudFront origin group). For writes:

1. Update the **standby** stack with `Active=true`. This enables its aggregator stream mapping and its activator schedule.
2. If Mumbai is reachable at all, update the **primary** stack with `Active=false` (never leave two active regions: counters would double).
3. Point CloudFront's `/checkin` behaviour at the `api-standby` origin **in the console**. CloudFront itself is global and keeps working, but the distribution is defined in the *primary* stack, and CloudFormation in a degraded Mumbai may not be able to update it. (This is the weakest point of the current layout: the next change I'd make is moving CloudFront into its own stack in us-east-1, next to the WAF, so the edge never depends on either workload region.) Afterwards, bring the template back in line with the console change.
4. Check: a test check-in through CloudFront appears in a lookup within seconds.
5. When Mumbai recovers, its queue drains on its own (writes land in the global table and replicate). Then fail back in reverse order.

Time in drills so far: not yet measured on AWS (the stack isn't left deployed). Target: under 5 minutes.

## Close an event

Events don't need closing: the banner disappears 14 days after the quake, check-ins expire after 180 days, and the next quake opens a new event. To stop contact notifications early, set `SmsBudgetPerEvent` to 0 (replies to people who text in continue).
