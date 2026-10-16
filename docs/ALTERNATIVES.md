# Alternatives considered

Each decision is a short architecture decision record: what I chose, what I rejected and why, and what would make me change my mind. The last line of each is the part I'd expect an interviewer to push on.

---

## ADR-001: Which Nepal problem

**Chosen:** earthquake check-ins: "I'm safe / I need help" for families and responders, built for the surge.

**Considered:**

| Problem | Why it's real | Why not (for a Solutions Architect project) |
|---|---|---|
| Kathmandu traffic management | Everyone in the Valley lives it | No public traffic feed; a project on synthetic data proves nothing about architecture. |
| Waste collection tracking | Irregular KMC collection, the Sisdol landfill | A CRUD app with a map. The architecture questions (scale, failure, cost) are all easy. |
| Flood or landslide early warning | Deadly every monsoon | Mostly a data and modelling problem; my rainfall lakehouse project already covers that ground. |
| **Earthquake check-in** | Nepal sits on one of the most active plate boundaries on Earth; 2015 killed almost 9,000 people; 2023 Jajarkot killed 153 | **Every hard architecture question is real here:** zero-to-millions traffic with no warning, durability under overload, multi-region DR, cost when idle, abuse, privacy, a feature-phone channel. |

**Would change if:** a city opened a real-time traffic or waste-truck feed; that would make a good streaming project.

---

## ADR-002: Write path: storage-first queue

**Chosen:** API Gateway HTTP API with a first-class `SQS-SendMessage` integration. No Lambda before "202 Accepted". A Lambda writer drains the queue in batches.

| Option | Why not |
|---|---|
| API Gateway → Lambda → DynamoDB (synchronous) | Every limit in the chain (Lambda concurrency and cold starts, table capacity) becomes a user-visible error at the worst moment. The simulation loses 17% of check-ins at 6M people and 45% at 15M (README, rows 1-2). |
| Kinesis Data Streams | Ordered shards and replay, neither of which I need. Shards are provisioned (or on-demand at a per-hour stream charge), so it costs money while idle, and its on-demand mode also ramps from a previous peak. SQS standard has effectively unlimited throughput with nothing to provision. |
| EventBridge (API Gateway → event bus → targets) | Fine for fan-out, but its PutEvents has a per-region quota of its own and no backlog to drain at my pace: the target has to keep up. |
| SQS FIFO | Ordering per person would be nice, but FIFO throughput is capped per message group and per queue (even with high-throughput mode), and I get correct ordering anyway from the server timestamp in a conditional write. |
| AppSync / GraphQL mutations | Adds a resolver hop and a per-request cost for no gain on a single-shape write. |

**Would change if:** there were many consumers of each check-in (Red Cross, government, media). Then SNS or EventBridge fan-out after the writer, not in front of it.

---

## ADR-003: Database

**Chosen:** DynamoDB on-demand, pre-warmed to 20,000 writes/s with warm throughput, as a global table.

| Option | Why not |
|---|---|
| DynamoDB provisioned + auto scaling | Auto scaling reacts in minutes; the surge peaks in 8. Provisioning for the peak permanently costs thousands a month. |
| DynamoDB on-demand, not pre-warmed | A new or idle table serves 4,000 writes/s, and growth past double the previous peak within 30 minutes can be throttled. With the queue nothing is lost, but at 6M people the slowest 1% wait ~7 minutes to be visible (README, row 3). Pre-warming costs a one-time charge, not a monthly one. |
| Aurora Serverless v2 | Scales in seconds but within an ACU range and needs connections, which Lambda at 1,000 concurrency would exhaust without RDS Proxy. Minimum capacity isn't zero in every configuration, and multi-region means Aurora Global Database with a secondary cluster. |
| Aurora DSQL | Multi-region active-active SQL is interesting here; but my access pattern is a single key-value lookup, and I'd be paying for SQL I don't use and taking on a newer service's quotas in the one hour it matters. |
| ElastiCache / MemoryDB in front | Another always-on cluster. CloudFront already caches reads for 30 s at the edge. |

**Would change if:** responders needed ad hoc queries ("everyone who needs help within 5 km of Barpak"). That's an export to S3 and Athena, not a different primary store.

---

## ADR-004: Compute

**Chosen:** Lambda (arm64) for the writer, notifier, aggregator, read API and activator.

| Option | Why not |
|---|---|
| ECS on Fargate with target-tracking auto scaling | Tasks take tens of seconds to start and scaling reacts to minute-level metrics; the burst would arrive before the capacity. Running enough tasks all year costs money every hour. |
| EC2 Auto Scaling group behind an ALB | Same, slower; plus patching instances for years in case of one afternoon. ~$1,250/month per region sized for the peak ([COST.md](COST.md)). |
| Lambda with provisioned concurrency | The writer isn't latency-sensitive (it's behind a queue), so cold starts don't matter. The read API's cold starts are hidden by CloudFront. |

**Would change if:** the writer needed a long-lived connection (to a relational database or a partner's API); then Fargate behind the same queue.

---

## ADR-005: Multi-region

**Chosen:** warm standby in ap-southeast-1. Global tables replicate; CloudFront fails reads over automatically; writes move by a 5-minute promotion runbook.

Full comparison with RTO/RPO in [ARCHITECTURE.md](ARCHITECTURE.md#disaster-recovery). The short version: serverless makes the standby cost about $1 a month while idle, so the usual argument for pilot light (save money) doesn't apply; and active-active writes would let replication's last-writer-wins undo the "newest server timestamp wins" rule for a person who updates twice in a minute.

**Why Mumbai as primary:** the closest region to Nepal by network latency. **Why Singapore as standby, not Hyderabad (ap-south-2):** Hyderabad is closer, but two Indian regions share more correlated risk (national connectivity, regulation, the same subsea and terrestrial links into Nepal through India). Singapore is a different country and a different path.

**Would change if:** measured latency from Nepali mobile networks to Hyderabad were much better than to Mumbai; then Hyderabad primary, Mumbai or Singapore standby.

---

## ADR-006: SMS channel

**Chosen:** a Nepali SMS aggregator for inbound (webhook into the same queue) and outbound, behind a provider interface; AWS End User Messaging supported as a fallback; dry run by default.

| Option | Why not (as the primary) |
|---|---|
| AWS End User Messaging SMS (or SNS SMS) for everything | Inbound two-way SMS needs a number Nepali subscribers can text cheaply, which in practice is a local short code held by a local company. Outbound international rates are roughly 10x a local aggregator's. |
| USSD menus | Better UX on feature phones, but sessions time out on congested networks and need deeper operator integration. |
| Cell broadcast | The right tool for *warnings*, one-way and operator-controlled. A good complement, not a replacement. |
| No SMS at all | Then the service is for people with smartphones and data, which is exactly who already had Facebook in 2015. |

Every SMS goes out in **one language**, the one the person used: Devanagari forces UCS-2 encoding (70 characters per segment instead of 160), so a bilingual message costs 3-4 segments where one language costs 1-2.

**Would change if:** an operator offered zero-rated emergency SMS during declared disasters; then the budget breaker could be raised a lot.

---

## ADR-007: Who opens an event

**Chosen:** a scheduled Lambda reading the USGS "M4.5+ past hour" GeoJSON feed every minute, with a felt-radius rule backtested on 11 years of quakes ([ACTIVATION.md](ACTIVATION.md)). Manual opening is a script.

| Option | Why not (alone) |
|---|---|
| Manual only (an operator presses a button) | At 11:56 on a Saturday the operator is under a table too. |
| Nepal's National Earthquake Monitoring and Research Center | The authoritative local source, and it often reports faster with better local magnitudes, but I couldn't find a documented machine-readable feed. It's the first thing I'd add next to USGS. |
| USGS real-time push (PDL / ComCat notifications) | Lower latency than a one-minute poll, but more moving parts; the feed is a CDN-served JSON file that polls for $0.08 a month. |
| Open on *any* M4.5+ | Southern Tibet produces M5+ quakes that no one in Nepal feels; 30 of 88 M5+ quakes in the backtest were ignored, correctly. |

**Would change if:** the national centre published a feed. Then activation on either source, whichever is first.

---

## ADR-008: Identity

**Chosen:** no accounts. A check-in is keyed by the phone number the person types (web) or texts from (SMS). Lookups by number, revealing only status, district and time.

| Option | Why not |
|---|---|
| Cognito accounts | Nobody creates an account in the ten minutes after an earthquake. |
| SMS one-time password before a web check-in | The strongest protection against someone marking another person "safe", but it costs one SMS per check-in (millions) and fails exactly when SMS is congested. |
| Facebook / Google sign-in | Excludes the people this exists for, and hands the data to a third party. |

The cost of no accounts: anyone can submit a check-in for any number. Mitigations and the residual risk are in [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md). The SMS door is authenticated by the network itself: the sender's number comes from the operator, not from the message.

**Would change if:** abuse were observed in a drill; then OTP for *changing someone from "need help" to "safe"* only, which is the abuse that matters.

---

## ADR-009: Read path

**Chosen:** CloudFront caching `GET /status`, `/events`, `/summary` for 30-60 s (keyed on the `phone` query string), in front of a small Lambda; origin group fails over to the standby.

| Option | Why not |
|---|---|
| WebSockets / AppSync subscriptions for live updates | Millions of long-lived connections over congested mobile networks in exchange for news a family can get by pressing refresh. |
| DAX in front of DynamoDB | An always-on cluster, and it doesn't take load off API Gateway's quota, which reads and writes share. CloudFront does. |
| Static JSON per person in S3 | Cheapest reads, but a write per check-in to S3 and a cache invalidation story. Interesting at 10x the scale. |

**Would change if:** responders needed a live dashboard; that's a separate, authenticated app reading the Meta table, not a change to the public path.

---

## ADR-010: Build it at all?

**Considered:** Facebook Safety Check (exists, works, reaches those on Facebook), Google Person Finder (a missing-persons registry Google has opened for past disasters), the Red Cross *Restoring Family Links* service (human caseworkers, for people who stay missing).

**Why build:** none of them takes an SMS in Nepali from a feature phone, none gives Nepali responders per-district counts they control, and the question I wanted to answer is an architecture one: *what does it take to accept every check-in when everyone tries at once, while costing nothing the rest of the time?*

**Would change if:** this were a real deployment decision rather than a portfolio project. Then the first step is a conversation with the Nepal Red Cross and NDRRMA about what they'd actually use, and this code becomes a prototype for that conversation.
