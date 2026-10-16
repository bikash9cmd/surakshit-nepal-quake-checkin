# Challenges and bugs

What went wrong while building this, roughly in the order I hit it, with the evidence and the fix. Most of these were found by running the code against real data (the USGS catalogue, real Nepali text) or by a test; a few were design mistakes I caught while writing the infrastructure, before they could ship. I've marked which is which.

## Activation and data

### 1. USGS place names arrived mangled

**Symptom:** place names like `31 km SSE of Kod?ri??, Nepal`, `13 km ESE of Il?m` and `Dh?rchula`.

**Cause:** the names carry macrons and dots (Kodāri, Ilām, Dhārchula) and were mangled to `?` somewhere between the USGS service and my file. The coordinates and ids were fine; only the human-readable `place` field was affected.

**Fix:** ASCII-fold the six affected names in the stored CSV and say so in [data/README.md](../data/README.md). Nothing in the logic reads `place`, so the activation results don't depend on it. **Lesson:** I now check text columns for `?` and replacement characters as part of loading any dataset, not just numeric columns for outliers.

### 2. M6.0 would only have emailed for Jajarkot

**Symptom:** the first activation backtest paged on-call for exactly three events: Gorkha, Dolakha and southern Tibet 2025. The 2023 Jajarkot quake, which killed 153 people, only sent an email.

**Cause:** I picked "page a human at M6.0" because it's a round number that sounds serious. But USGS rates Jajarkot at M5.7, as it does the damaging 2022 Doti (6 deaths) and 2023 Bajhang quakes. In western Nepal, stone-and-mud houses fail at magnitudes that would be a news footnote elsewhere.

**Fix:** page at M5.5 (`MAJOR_MAGNITUDE` in [config.py](../surakshit/config.py)). The backtest now pages for 6 events in 11.4 years, all six of them damaging. **Test:** `test_every_damaging_quake_since_2015_opens_a_major_event`. **Lesson:** a threshold should be checked against the events you care about, not chosen by feel.

### 3. A fixed 24-hour aftershock window opened 28 events

**Symptom:** the first backtest opened 28 events in 11.4 years, including separate events for M5.0 and M5.5 aftershocks on 13 and 16 May 2015, one and four days after the M7.3.

**Cause:** the rule was "a weaker quake within 24 hours of an open event joins it". Big quakes have aftershocks for weeks. Each new event would have shown a new banner, paged someone, and split every family's check-ins across events.

**Fix:** within 24 hours any weaker quake joins; after that, up to a window that grows with the main shock (1 day for M5, 7 days for M6, 60 days for M7+), a quake joins only if it is at least 1.0 magnitude weaker. The 12 May M7.3 still opens its own event (everyone needs to check in again after a M7.3), and the backtest went from 28 events to 23 (27 after #4 below). **Test:** `test_the_2015_sequence_opens_two_events_not_thirty`.

### 4. The far west had no town on my map

**Symptom:** the backtest ignored a M5.0 on 19 November 2019, 20 km from Dipayal, the headquarters of Doti district, as "89 km from Dhangadhi, felt radius 80 km".

**Cause:** the felt-radius rule measures distance to the nearest of a list of population centres, and my list was provincial capitals and big cities. In the far-western hills the nearest one was Dhangadhi, down in the plains. Doti and Bajhang are exactly where two of the six damaging quakes in this period happened.

**Fix:** add Dipayal and Chainpur (Bajhang). The backtest now opens 27 events instead of 23; the four extra are all email-only M5.0-5.2 events in the far west, which is the right side to err on. **Lesson:** a list of "important places" built from a national view misses the places that matter for this hazard. The proper fix is district population centroids for all 77 districts, which is on the list.

## The surge simulation

### 5. My first scenario broke nothing

**Symptom:** the first simulation run, with 2 million people, showed zero lost check-ins for *every* design, including the naive synchronous one.

**Cause:** not a bug: at 2 million people the peak is ~2,800 requests/s, under every limit. But it meant my first scenario couldn't tell the designs apart, and an honest README has to say so.

**Fix:** three sizes that bracket the unknown (2M, 6M, 15M), and the README leads with "at Valley scale every design works". The interesting finding moved: the first limits to bind are soft account quotas, which are fixed by a support ticket before the event, not by code.

### 6. The simulated API Gateway was twice as strict as the real one

**Symptom:** with the quota raised to 30,000 requests/s, the 15M run still showed millions of 429s, although the peak was ~21,000 requests/s.

**Cause:** my token bucket refilled at `rps` per second but capped the bucket at `burst` *before* serving the second's requests, so per-second throughput could never exceed the burst size (5,000 by default, half the 10,000 quota). Every earlier number overstated how much the API throttled.

**Fix:** serve up to (tokens left over + refill) each second, then cap what's left at the burst. **Test:** `test_no_api_throttling_below_the_quota`. **Lesson:** a model needs a test where the answer is obvious, exactly like code does.

## SMS and Nepali text

### 7. Two phone numbers in one SMS: both lost

**Symptom:** `HELP gorkha house fell 9841000001 ९८५१०००००२` produced no contacts at all.

**Cause:** the pattern for "a number inside text" allowed 8 to 12 digits *or spaces* after the prefix. It swallowed the space and the start of the second number, the result wasn't a valid number, and the second number had lost its first digits too.

**Fix:** exactly eight more digits, each optionally preceded by one separator, and not followed by another digit. **Test:** `test_sms_numbers_become_contacts_and_leave_the_note`.

### 8. A two-line SMS got the help text back

**Symptom:** `HELP` on one line and `gorkha` on the next was answered with "Text SAFE, HELP or INJURED".

**Cause:** the note cleaner removed control characters (including `\n`) *before* collapsing whitespace, so the message became `HELPgorkha`, which isn't a keyword.

**Fix:** turn all whitespace into spaces first, then remove the remaining control characters. **Test:** `assert parse_sms("HELP\ngorkha").district == "Gorkha"`.

### 9. "ठीक छु काठमाडौं" didn't record Kathmandu

**Symptom:** in the demo, an SMS saying "I'm fine, Kathmandu" in Nepali was stored with no district, and "काठमाडौं" went into the note.

**Cause:** two things. Districts were only recognised in English spellings, and only as the first word after the keyword. In Nepali the natural order puts the place at the end.

**Fix:** Devanagari names for the districts hit hardest in 2015 and 2023, and a search through all the words. **Test:** parametrised cases in `test_sms_keywords_in_english_romanised_nepali_and_devanagari`.

### 10. "Recorded: you needs help."

**Symptom:** the demo's English SMS confirmation.

**Cause:** I built the second-person reply by lower-casing the third-person phrase used for notifications ("NEEDS HELP").

**Fix:** a separate table of second-person phrases. Small, but this message goes to someone in the worst hour of their life.

### 11. My Unicode test was wrong, not the code

**Symptom:** a test asserting that क + nukta (two code points) normalises to क़ (U+0958) failed.

**Cause:** U+0958 is a Unicode *composition exclusion*: NFC deliberately decomposes it. My expectation was wrong. The thing I actually needed to test (the same word typed on two keyboards compares equal after NFC) holds, and न + nukta does compose to ऩ.

**Fix:** test with a character that composes. **Lesson:** Devanagari normalisation has exceptions; test the property you rely on, not a character you assume behaves.

## Storage

### 12. Every redelivered message would have ended in the dead-letter queue

**Symptom:** the moto test of the writer handler crashed with `TypeError: int() argument must be ... not 'dict'` on a redelivered message.

**Cause:** when a conditional write fails, I ask DynamoDB to return the existing item (`ReturnValuesOnConditionCheckFailure=ALL_OLD`) so I can tell a duplicate from an out-of-order update without a second read. Through boto3's *resource* API everything else comes back as Python values, but that item comes back in the low-level format (`{"S": "..."}`). Comparing request ids crashed, the message was retried, crashed again, and after 8 tries would have gone to the DLQ and paged someone. SQS redelivers routinely, so in a surge this would have been thousands of pages.

**Fix:** deserialise the item explicitly. **Test:** `test_end_to_end_activation_writer_aggregator_api` redelivers a message and expects no failure. **Lesson:** this is exactly the kind of bug a unit test with a fake store can't find. The fake returned Python dicts; the real API didn't.

### 13. PutItem can't count, and a stale district lingered *(design, caught before running)*

My first `DynamoStore.put_checkin` used PutItem with a condition on an `updates` counter, which doesn't work: PutItem replaces the item and can't increment. Switching to UpdateItem (`SET ... ADD updates :one`) exposed a second problem: an update that leaves out `district` kept the *old* district, so someone who went from "need help, Gorkha" to "safe" would still show Gorkha. The update now `REMOVE`s optional fields the new check-in doesn't have. **Test:** `test_status_change_moves_the_counter_and_clears_the_old_district`.

## Infrastructure

### 14. Two regions would have counted every check-in twice *(design, caught while writing the standby stack)*

A global table writes each item into every replica, and **each replica's stream carries every write**, including the replicated ones. My first template gave both regions an aggregator on their local stream, so every check-in would have been counted once in Mumbai and once in Singapore, and since the counters are themselves in a global table, both counts would have landed in the same place. I split the stack's `Role` (which region owns the tables) from `Active` (which region runs the activator and the aggregator); only the active region's stream mapping is enabled, and promoting the standby flips it ([RUNBOOK.md](RUNBOOK.md)).

### 15. The lookup URL is a phone number *(design, caught while writing the template)*

`GET /status?phone=98...` is cacheable at CloudFront, which the read path depends on. But the default API Gateway access log includes the path and query string, and WAF's sampled requests store full URLs. Together that would have been a log of who looked up which number. The access log format now records `routeKey` (`GET /status`) instead of the path, WAF sampling is off, the Lambda logs only masked numbers (`+977 98XXXXX567`), and CloudFront standard logs aren't enabled. Trade-off recorded in [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md).
