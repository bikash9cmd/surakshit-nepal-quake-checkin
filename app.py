"""Streamlit demo of Surakshit: check in, send an SMS and look a number up, replayed on 25 April 2015.

    streamlit run app.py

Everything runs in one process with the same writer, aggregator and SMS parser the Lambdas run
(surakshit.local.LocalSystem). The 2015 Gorkha event is opened and seeded with 2,000 synthetic
check-ins, and anything you send is stamped as if it arrived 30 minutes or more after the quake.
"""

import random
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

import brand
from surakshit import activation, reads
from surakshit.__main__ import _catalog
from surakshit.local import LocalSystem

brand.page("surakshit")

DISTRICTS = ["Kathmandu", "Lalitpur", "Bhaktapur", "Gorkha", "Sindhupalchok", "Nuwakot", "Dhading", "Rasuwa", "Kavre"]
STATUS = {"safe": "I'm safe", "need_help": "I need help", "injured": "I'm injured"}


def seed() -> dict:
    rng = random.Random(42)  # noqa: S311 - synthetic demo data
    system = LocalSystem()
    gorkha = next(q for q in _catalog() if q.usgs_id == "us20002926")
    event = activation.to_event(gorkha, activation.decide(gorkha))
    system.store.create_event(event)
    t0 = gorkha.time_ms
    weights = {"safe": 0.86, "need_help": 0.1, "injured": 0.04}
    for _ in range(2000):
        system.submit_web(
            {
                "phone": f"98{rng.randrange(10**8):08d}",
                "status": rng.choices(list(weights), list(weights.values()))[0],
                "district": rng.choice(DISTRICTS + ["Kathmandu"] * 4),
                "lang": "en",
            },
            received_at=t0 + int(rng.gammavariate(2.0, 480) * 1000),
        )
    system.drain()
    return {"system": system, "event": event, "t0": t0, "minutes": 30}


if "demo" not in st.session_state:
    st.session_state.demo = seed()
demo = st.session_state.demo
system, event = demo["system"], demo["event"]


def stamp() -> int:
    demo["minutes"] += 1
    return demo["t0"] + demo["minutes"] * 60_000


def clock() -> str:
    return datetime.fromtimestamp((demo["t0"] + demo["minutes"] * 60_000) / 1000, UTC).strftime("%d %b %Y %H:%M UTC")


st.info(
    f"Replaying **M{event['mag']} {event['place']}** (25 April 2015). Demo clock: {clock()}. "
    "2,000 synthetic people have already checked in. Phone numbers here are made up."
)

tab_web, tab_sms, tab_look, tab_sum = st.tabs(["Check in", "Send an SMS", "Look up a number", "Event summary"])

with tab_web:
    with st.form("web"):
        phone = st.text_input("Your phone number", "9851000002")
        status = st.radio("How are you?", list(STATUS), format_func=STATUS.get, horizontal=True)
        district = st.selectbox("District", DISTRICTS)
        family = st.text_input("Family number to tell (optional)", "9841000001")
        if st.form_submit_button("Check in", type="primary"):
            body = {"phone": phone, "status": status, "district": district, "lang": "en"}
            if family.strip():
                body["contacts"] = [family.strip()]
            before = len(system.outbox)
            system.submit_web(body, received_at=stamp())
            system.drain()
            st.success("Received. Your family can now look you up by your number.")
            for o in system.outbox[before:]:
                st.write(f"SMS [{o.kind}] to {o.to[:6]}...: {o.text}")

with tab_sms:
    st.write("Feature phones text a keyword, a district and an optional family number, in Nepali or English.")
    examples = [
        "SAHAYOG sindhupalchowk ghar bhatkiyo 9841000001",
        "ठीक छु काठमाडौं",
        "HELP gorkha 9841000001",
        "khoj 9851000002",
    ]
    text = st.selectbox("Message", examples, index=0, accept_new_options=True)
    sender = st.text_input("From", "+9779851000003")
    if st.button("Send SMS", type="primary"):
        before = len(system.outbox)
        system.submit_sms(sender, text, received_at=stamp())
        system.drain()
        sent = system.outbox[before:]
        if not sent:
            st.write("No reply was sent.")
        for o in sent:
            st.write(f"SMS [{o.kind}] to {o.to[:6]}...: {o.text}")

with tab_look:
    q = st.text_input("Number to look up", "9851000002")
    if st.button("Look up", type="primary"):
        code, body = reads.status_view(system.store, system.pepper, q)
        st.json(body)

with tab_sum:
    _, s = reads.summary_view(system.store, event["event_id"])
    totals = s["totals"]
    cols = st.columns(len(totals))
    for c, (k, v) in zip(cols, totals.items(), strict=True):
        c.metric(k.replace("_", " "), f"{v:,}")
    st.subheader("By district")
    st.dataframe(pd.DataFrame(s["by_district"]).T.fillna(0).astype(int), width="stretch")

brand.footer()
