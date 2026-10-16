# Activation: when does Surakshit turn itself on?

## The rule

Every minute the activator reads the USGS "M4.5+ in the past hour" feed and, for each quake ([activation.py](../surakshit/activation.py), constants in [config.py](../surakshit/config.py)):

1. **Ignore it** if it's below M5.0, deeper than 70 km, or farther from every place on a list of 17 Nepali population centres than a felt radius that grows with magnitude (80 km at M5.0, 120 km at M5.5, 200 at M6.0, 300 at M6.5, 450 at M7.0, 600 at M7.5).
2. **Join an open event** if it's the same quake under another network's id, or a weaker shock within 24 hours of an event's main shock, or at least 1.0 magnitude weaker within a window that grows with the main shock (1 day for M5, 7 days for M6, 60 days for M7+).
3. **Otherwise open an event**: page on-call for M5.5+, email for smaller.

The felt-radius table is a heuristic, not seismology: real felt intensity depends on depth, geology and building stock. It's tuned against the backtest below, and the obvious improvement is to use the USGS ShakeMap or "Did You Feel It?" products once they're published (minutes to an hour after a quake), to confirm or close an event.

## Backtest: every M5+ quake near Nepal, January 2015 to September 2026

88 quakes from the USGS catalogue in a box around Nepal (25.5-31.5°N, 79-89.5°E), replayed in time order through the same `activation.run` the Lambda uses. Reproduce with `make backtest`; every quake's outcome and reason is in `artifacts/reports/activation_backtest.json`.

| Outcome | Count |
|---|---:|
| Ignored | 30 |
| Joined an open event | 31 |
| Opened an event | **27** |
| of which paged on-call (M5.5+) | 6 |

### Events opened

| Opened (UTC) | M | Where (USGS) | Nearest place, km | Severity |
|---|---:|---|---|---|
| 2015-04-21 14:02 | 5.0 | 46 km SSE of Jumla | Khalanga (Jajarkot), 29 | watch |
| **2015-04-25 06:11** | **7.8** | **67 km NNE of Bharatpur (Gorkha earthquake)** | Gorkha, 27 | **major** |
| **2015-05-12 07:05** | **7.3** | **Nepal (Dolakha earthquake)** | Charikot, 16 | **major** |
| 2015-08-23 09:02 | 5.0 | 18 km SE of Kodari | Charikot, 19 | watch |
| 2015-11-19 04:15 | 5.0 | 21 km NE of Nagarkot | Kathmandu, 38 | watch |
| 2015-12-18 22:16 | 5.4 | 53 km WNW of Jumla | Chainpur (Bajhang), 47 | watch |
| 2016-02-05 16:20 | 5.2 | 19 km N of Kathmandu | Kathmandu, 18 | watch |
| 2016-02-21 18:10 | 5.1 | 40 km NE of Bharatpur | Gorkha, 8 | watch |
| 2016-11-27 23:35 | 5.4 | 17 km W of Namche Bazar | Charikot, 52 | watch |
| 2016-12-01 16:52 | 5.2 | 18 km NE of Dharchula, India | Chainpur (Bajhang), 73 | watch |
| 2017-12-08 02:51 | 5.0 | 28 km SE of Kodari | Charikot, 15 | watch |
| 2019-11-19 13:30 | 5.0 | 20 km ENE of Dipayal | Dipayal (Doti), 21 | watch |
| 2020-09-15 23:34 | 5.3 | 14 km SSW of Kodari | Charikot, 25 | watch |
| 2021-05-18 23:57 | 5.3 | 36 km E of Pokhara | Pokhara, 35 | watch |
| 2022-07-31 02:28 | 5.1 | 26 km W of Bhojpur | Dhankuta, 56 | watch |
| **2022-11-08 20:27** | **5.7** | **Nepal (Doti earthquake, 6 deaths)** | Dipayal, 22 | **major** |
| 2022-11-12 14:27 | 5.2 | 25 km ENE of Dipayal | Chainpur, 22 | watch |
| 2023-01-24 08:58 | 5.4 | 60 km NW of Jumla | Chainpur, 45 | watch |
| 2023-04-27 19:45 | 5.1 | 72 km NE of Dipayal | Chainpur, 35 | watch |
| **2023-10-03 09:21** | **5.7** | **39 km NE of Dipayal (Bajhang earthquake)** | Chainpur, 7 | **major** |
| 2023-10-07 06:00 | 5.0 | 38 km NE of Dipayal | Chainpur, 4 | watch |
| 2023-10-22 01:54 | 5.2 | 36 km NE of Bharatpur | Gorkha, 16 | watch |
| **2023-11-03 18:02** | **5.7** | **43 km E of Dailekh (Jajarkot earthquake, 153 deaths)** | Khalanga (Jajarkot), 18 | **major** |
| 2023-11-06 10:46 | 5.3 | 34 km E of Dailekh | Khalanga, 22 | watch |
| 2024-12-20 22:29 | 5.0 | 60 km WNW of Jumla | Chainpur, 40 | watch |
| **2025-01-07 01:05** | **7.1** | **Southern Tibetan Plateau (Tingri earthquake)** | Charikot, 168 | **major** |
| 2026-08-26 02:52 | 5.2 | 55 km NW of Kodari | Kathmandu, 64 | watch |

### What the rule got right

- **All six damaging quakes page on-call**, including three at M5.7 that a "round number" threshold of M6.0 would have missed ([CHALLENGES_AND_BUGS.md #2](CHALLENGES_AND_BUGS.md#2-m60-would-only-have-emailed-for-jajarkot)).
- **The 2015 sequence opens two events, not 33.** The M6.6 at +34 minutes, the M6.7 the next day (1.1 weaker than the main shock) and dozens of M5s join the Gorkha event; the M7.3 on 12 May opens its own, because after a M7.3 everyone needs to check in again.
- **Southern Tibet is mostly ignored.** 25 of the 30 ignored quakes are in Tibet, 150-340 km from the nearest Nepali town, and the January 2025 M7.1, which was strongly felt in Kathmandu, still opens a major event because its felt radius is 450 km.

### Known misses and weaknesses

- **Aftershocks of moderate quakes open their own "watch" events.** A M5.2 four days after the Doti M5.7, and a M5.0 four days after the Bajhang M5.7, each opened an email-only event because a M5.x main shock only holds aftershocks for 24 hours. People did feel them, so this errs on the right side, but it's noise for on-call.
- **The population list is still a list.** Distance to the nearest of 17 places is a crude proxy for "how many people felt this". District-level population centroids for all 77 districts would be better.
- **USGS magnitudes are not Nepal's.** Nepal's national seismological centre often reports different (local) magnitudes, and faster. The rule should take whichever source is first.
- **A deadly quake below M5.0** wouldn't open anything. None occurred in this period, but the manual path (`scripts/open_event.py`) exists for it.
