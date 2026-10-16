# Data

## usgs_m5_2015_2026.csv

Every earthquake of magnitude 5.0 or more in a box around Nepal from 1 January 2015 to 1 October 2026, from the U.S. Geological Survey's ANSS Comprehensive Catalog (ComCat), retrieved on 13 October 2026 with:

```text
https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime=2015-01-01&endtime=2026-10-01
  &minlatitude=25.5&maxlatitude=31.5&minlongitude=79&maxlongitude=89.5&minmagnitude=5&orderby=time-asc
```

| Column | Meaning |
|---|---|
| `time` | Origin time, UTC (ISO 8601) |
| `latitude`, `longitude` | Epicentre, degrees |
| `depth` | km |
| `mag`, `magType` | Magnitude as USGS reports it (`mww` moment magnitude, `mb` body-wave, ...) |
| `place` | USGS's description, e.g. "67 km NNE of Bharatpur, Nepal" |
| `id` | USGS event id, e.g. `us20002926` (the 2015 Gorkha earthquake) |

88 rows. The service's own columns were reduced to these eight. Six place names had accented characters mangled to `?` in transit (Kodāri, Ilām, Dhārchula, Pīpalkoti, Panauti, Joshīmath) and are ASCII-folded here; nothing in the code reads `place` ([CHALLENGES_AND_BUGS.md #1](../docs/CHALLENGES_AND_BUGS.md#1-usgs-place-names-arrived-mangled)).

USGS catalogue data is in the public domain. Magnitudes and locations are revised over time; a fresh query may differ slightly.

## Live data

In production the activator reads the USGS GeoJSON summary feed (`4.5_hour.geojson`), which is regenerated every minute. Its `ids` field lists every network's id for the same quake, which is how a re-reported quake is recognised as the same event.
