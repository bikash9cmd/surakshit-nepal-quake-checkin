"""Project-wide constants. Anything a reviewer might question lives here, with the reason next to it."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = Path(os.environ.get("SURAKSHIT_ARTIFACT_DIR", ROOT / "artifacts"))

NPT = timedelta(hours=5, minutes=45)  # Nepal time, UTC+05:45

# ---------- check-ins ----------

# What a person can report. Kept to three on purpose: under stress, a fourth choice is a
# question people get wrong. "need_help" is what responders filter on.
STATUSES = ("safe", "need_help", "injured")

# Free-text note limit. 140 characters fits one Unicode SMS segment (70 chars) twice and
# keeps a DynamoDB item well under 1 KB, i.e. one write request unit per check-in.
MAX_NOTE_CHARS = 140

# Each person may list family numbers to be told they checked in. Capped because every
# contact is an outbound SMS we pay for, and because an uncapped list is a spam cannon.
MAX_CONTACTS = 3

# A person who checks in more than this many times per event is almost certainly a script.
# The writer drops further updates (the first ones still count).
MAX_UPDATES_PER_PERSON = 20

# Check-ins expire through DynamoDB TTL 180 days after they were made. Long enough for relief
# agencies to reconcile lists through the reconstruction phase; short enough not to become a
# permanent register of where every phone number in Nepal was on a given day.
RETENTION_DAYS = 180

# ---------- activation ----------

# An event opens automatically when a quake is strong enough to be felt widely in Nepal.
# M5.0 at shallow depth is felt across a district; below that, the service stays dormant.
ACTIVATION_MIN_MAGNITUDE = 5.0
# Ignore deep events: intermediate-depth quakes under the Hindu Kush are often M5+ but are
# barely felt in Nepal.
ACTIVATION_MAX_DEPTH_KM = 70.0

# Rough "felt radius" by magnitude, in km, used to decide whether a quake outside Nepal
# (Tibet, Sikkim, Bihar) is close enough to matter. A heuristic, not seismology: tuned so
# that the 2015 Gorkha (M7.8), 2015 Dolakha (M7.3) and 2023 Jajarkot (M5.7) quakes activate
# and a M5.0 in central Tibet does not. See tests/test_activation.py.
FELT_RADIUS_KM = {5.0: 80, 5.5: 120, 6.0: 200, 6.5: 300, 7.0: 450, 7.5: 600}

# Population centres used for the felt-radius check: the seven provincial capitals, the largest
# cities, and the district headquarters nearest the faults that produced damaging quakes since
# 2015. (lat, lon), rounded to 0.01 degree.
NEPAL_CITIES = {
    "Kathmandu": (27.72, 85.32),
    "Pokhara": (28.21, 83.99),
    "Biratnagar": (26.45, 87.27),
    "Janakpur": (26.73, 85.93),
    "Hetauda": (27.43, 85.03),
    "Butwal": (27.70, 83.45),
    "Deukhuri (Lumbini)": (27.98, 82.48),
    "Birendranagar": (28.60, 81.63),
    "Nepalgunj": (28.05, 81.62),
    "Dhangadhi": (28.69, 80.59),
    "Dhankuta": (26.98, 87.32),
    "Jumla": (29.27, 82.18),
    "Gorkha": (28.00, 84.63),
    "Charikot (Dolakha)": (27.67, 86.03),
    "Khalanga (Jajarkot)": (28.70, 82.20),
    # Added after the backtest: the far-western hills had no point closer than Dhangadhi, so a
    # M5.0 next to Dipayal in 2019 was "89 km away" and ignored.
    "Dipayal (Doti)": (29.26, 80.94),
    "Chainpur (Bajhang)": (29.55, 81.20),
}

# When does a new quake join an open event instead of opening its own?
#  * Within 24 hours of the main shock, any weaker quake joins. Otherwise the 25 April 2015
#    sequence (19 shocks of M5+ that day) would open 19 events and scatter every family's
#    check-ins across them.
#  * After that, and up to a window that grows with the main shock's size, a quake joins only if
#    it is at least AFTERSHOCK_MARGIN weaker. The 12 May 2015 M7.3, 17 days after the M7.8,
#    therefore opens a new event (everyone needs to check in again), and a M5.0 a day later
#    does not.
# Within an event a person can update their status at any time, so joining costs nothing.
AFTERSHOCK_FIRST_HOURS = 24
AFTERSHOCK_WINDOW_HOURS = {5.0: 24, 6.0: 7 * 24, 7.0: 60 * 24}  # by main-shock magnitude
AFTERSHOCK_MARGIN = 1.0

# Events at or above this magnitude page the on-call phone; smaller ones send an email.
# Not 6.0: the 2023 Jajarkot quake that killed 153 people was M5.7 (USGS), as were the damaging
# 2022 Doti and 2023 Bajhang quakes. A threshold of 6.0 would only have emailed for all three.
MAJOR_MAGNITUDE = 5.5

# USGS publishes this feed every minute. Public, no key, served from a CDN.
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_hour.geojson"

# ---------- districts ----------

# The 77 districts of Nepal (after the 2015 constitution; Nawalparasi and Rukum are split
# East/West). A check-in may name one so responders can see where help is needed.
DISTRICTS = (
    "Achham", "Arghakhanchi", "Baglung", "Baitadi", "Bajhang", "Bajura", "Banke", "Bara",
    "Bardiya", "Bhaktapur", "Bhojpur", "Chitwan", "Dadeldhura", "Dailekh", "Dang", "Darchula",
    "Dhading", "Dhankuta", "Dhanusha", "Dolakha", "Dolpa", "Doti", "Eastern Rukum", "Gorkha",
    "Gulmi", "Humla", "Ilam", "Jajarkot", "Jhapa", "Jumla", "Kailali", "Kalikot", "Kanchanpur",
    "Kapilvastu", "Kaski", "Kathmandu", "Kavrepalanchok", "Khotang", "Lalitpur", "Lamjung",
    "Mahottari", "Makwanpur", "Manang", "Morang", "Mugu", "Mustang", "Myagdi",
    "Nawalparasi East", "Nawalparasi West", "Nuwakot", "Okhaldhunga", "Palpa", "Panchthar",
    "Parbat", "Parsa", "Pyuthan", "Ramechhap", "Rasuwa", "Rautahat", "Rolpa", "Rupandehi",
    "Salyan", "Sankhuwasabha", "Saptari", "Sarlahi", "Sindhuli", "Sindhupalchok", "Siraha",
    "Solukhumbu", "Sunsari", "Surkhet", "Syangja", "Tanahun", "Taplejung", "Terhathum",
    "Udayapur", "Western Rukum",
)

# Summary counters are sharded per district so that a surge from one district (Kathmandu
# holds the most phones) cannot exceed DynamoDB's 1,000 write units per second per partition
# key. 10 shards gives 10,000 WCU of headroom per district. See docs/CHALLENGES_AND_BUGS.md.
COUNTER_SHARDS = 10

# ---------- AWS ----------

REGION_PRIMARY = os.environ.get("SURAKSHIT_PRIMARY_REGION", "ap-south-1")   # Mumbai
REGION_STANDBY = os.environ.get("SURAKSHIT_STANDBY_REGION", "ap-southeast-1")  # Singapore
