# The surge model

[`surakshit/simulate.py`](../surakshit/simulate.py) is an agent-based simulation: each agent is a person trying to check in, each step is one second, and each AWS service is reduced to the documented limit that binds first. It exists because the alternative ways to answer "does this design survive?" are worse: a real load test at 20,000 requests/s for three hours would cost real money and still not reproduce a cold table or an account quota, and "it's serverless, it scales" is not an answer.

Run it with `make simulate` (about 90 seconds); results go to `artifacts/reports/surge.json` and the two charts in `docs/img`.

## Assumptions and where they come from

| Assumption | Value | Source / reasoning |
|---|---|---|
| People who try within 3 hours | 2M / 6M / 15M scenarios | Unknowable in advance. Nepal's 2021 census counted 29.2 million people; Facebook said "millions" were marked safe in 2015. 2M is roughly a Valley-scale event, 6M central Nepal, 15M national reach plus diaspora checking in on behalf of family. |
| Arrival curve | Gamma(k=2), peak 8 min after the shock | People take cover, then reach for phones. Nothing published gives this curve for Nepal; 8 minutes is my assumption and is the most important one (see sensitivity). |
| Aftershock waves | 30% of people check in again after each M6+ aftershock, at their real times | M6.1 at +3 min 57 s and M6.6 at +33 min 56 s after the main shock: USGS events us20002bi4 and us2000292y. |
| API Gateway | Token bucket, 10,000 req/s steady, 5,000 burst (account default per region) | [API Gateway quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html). Raised to 30,000 / 15,000 in design 6. |
| DynamoDB on-demand | 4,000 writes/s for a new or idle table; can serve up to double the previous peak instantly; growth beyond that within 30 minutes may be throttled | [DynamoDB on-demand: initial throughput and scaling](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html). Modelled conservatively: capacity fixed at max(warm throughput, 2 × prior peak) for 30 minutes, then may double every 30 minutes. |
| Pre-warming | 20,000 writes/s warm throughput | [Warm throughput](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/warm-throughput.html). |
| Lambda on SQS | Starts at 5 concurrent batches, adds up to 300 per minute, at most 1,250 per event source (I cap at 1,000) | [Configuring scaling for SQS event source mappings](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html). |
| Writer throughput | 150 check-ins/s per concurrent Lambda | 100 conditional UpdateItems one after another at ~6 ms each, plus overhead. An assumption; a load test would replace it. |
| Client, naive | Retries every ~1 s, gives up after 6 attempts | What a `fetch` in a loop does. |
| Client, backoff | Exponential backoff with full jitter, cap 60 s, 6 attempts | [AWS Architecture Blog: Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/). |
| Client, offline-first | Same backoff, cap 300 s, never gives up while the page is open | `web/index.html`. |
| Scale | Agents = people × 0.1 (2M), 0.05 (6M), 0.02 (15M); capacities scaled by the same factor | Keeps runtime to seconds per run. Checked below: halving the sample changes the loss by 0.04 points. |

## What is not modelled

- **Nepal's mobile network.** In 2015 the bottleneck was the network, not servers. The model assumes every attempt reaches API Gateway. The SMS door and the offline-first page are the answer to the network; the model only tests the AWS side.
- **Cold starts and the read path.** The writer sits behind a queue, so its cold starts only add seconds of queue time. Reads are served mostly by CloudFront and don't share the table's write capacity.
- **Per-partition limits.** Each phone number is its own partition key, so writes spread across partitions; the one hot-key risk (counters) is handled by batching and sharding (ARCHITECTURE.md, "How it scales").

## Sensitivity: what would change the answer

Each row is a re-run of the simulation with one assumption changed (6M scenario unless stated).

| If... | Measured |
|---|---|
| The peak comes at 4 minutes instead of 8 | Peak rises from ~8,400 to ~15,000 req/s, past the default API quota. Design 4 (no quota increase) now loses **6.5%**; design 6 loses none, p99 visible 23 s. The quota increase stops being optional. |
| Twice as many people re-check after each aftershock (60%) | The M6.6 wave at +34 min becomes taller than the main one (~11,300 req/s). Design 6: no losses, p99 11 s instead of 8 s. |
| Writer throughput is 50/s per Lambda, not 150 (15M) | Lambda's +300 pollers/minute ramp becomes the slow step. No losses, p99 11 s instead of 10 s. |
| Agents per person halved (6M, sampling check) | Loss for design 1: 16.74% vs 16.78%. The scaling factor doesn't drive the results. |
| The account's Lambda concurrency is still the default 1,000 | Not simulated: the writer needs ~140 concurrent at 15M and shares the pool with the read API. Fine, but on the readiness checklist. |
