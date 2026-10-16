"""The check-in record and the two ways one arrives: a JSON body from the web page, or an SMS.

Validation happens in the writer (after the queue), not in front of it: the API's only job in
a surge is to accept the request and return 202. A malformed message costs one SQS message
and one log line; a validation Lambda in front of the queue would cost a cold start for every
caller during the busiest minutes the system will ever see.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from surakshit import config
from surakshit.phone import InvalidPhone, normalise

_DISTRICT_LOOKUP = {d.lower().replace(" ", ""): d for d in config.DISTRICTS}
# Common alternative spellings seen on signs and in news reports.
_DISTRICT_ALIASES = {
    "kavre": "Kavrepalanchok", "kabhre": "Kavrepalanchok", "kabhrepalanchok": "Kavrepalanchok",
    "sindhupalchowk": "Sindhupalchok", "sindhupalchok": "Sindhupalchok",
    "makawanpur": "Makwanpur", "nuwakot": "Nuwakot", "kathmandu": "Kathmandu", "ktm": "Kathmandu",
    "lalitpur": "Lalitpur", "patan": "Lalitpur", "bhaktapur": "Bhaktapur", "chitawan": "Chitwan",
    "tanahu": "Tanahun", "dhanusa": "Dhanusha", "kapilbastu": "Kapilvastu", "rukumeast": "Eastern Rukum",
    "rukumwest": "Western Rukum", "solu": "Solukhumbu", "terathum": "Terhathum",
    # Devanagari, for the districts hit hardest in 2015 and 2023 and the largest cities.
    "काठमाडौं": "Kathmandu", "काठमाडौँ": "Kathmandu", "ललितपुर": "Lalitpur", "भक्तपुर": "Bhaktapur",
    "गोरखा": "Gorkha", "सिन्धुपाल्चोक": "Sindhupalchok", "दोलखा": "Dolakha", "नुवाकोट": "Nuwakot",
    "धादिङ": "Dhading", "रसुवा": "Rasuwa", "काभ्रे": "Kavrepalanchok", "काभ्रेपलाञ्चोक": "Kavrepalanchok",
    "मकवानपुर": "Makwanpur", "चितवन": "Chitwan", "कास्की": "Kaski", "रामेछाप": "Ramechhap",
    "जाजरकोट": "Jajarkot", "बझाङ": "Bajhang", "डोटी": "Doti", "मोरङ": "Morang", "झापा": "Jhapa",
}


class InvalidCheckIn(ValueError):
    pass


def district_from(text: str | None) -> str | None:
    if not text:
        return None
    key = re.sub(r"[\s\-_]", "", text.strip().lower())
    return _DISTRICT_ALIASES.get(key) or _DISTRICT_LOOKUP.get(key)


def clean_note(text: str | None) -> str:
    """NFC-normalise (so the same Nepali word typed on two keyboards compares equal), strip
    control characters, and cut to MAX_NOTE_CHARS *characters*, not bytes."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    # Whitespace first, then strip controls. The other way round, "HELP\ngorkha" (an SMS typed on
    # two lines) became "HELPgorkha" and was answered with the help text.
    text = "".join(" " if ch.isspace() else ch for ch in text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\u200c\u200d")
    text = " ".join(text.split())
    return text[: config.MAX_NOTE_CHARS]


@dataclass
class CheckIn:
    event_id: str
    phone: str                      # E.164, only in memory: stored as a keyed hash
    status: str
    received_at: int                # ms since epoch, stamped by API Gateway, never by the client
    checkin_id: str                 # API Gateway request id (or SMS message id): the idempotency key
    channel: str = "web"            # web | sms
    district: str | None = None
    note: str = ""
    contacts: list[str] = field(default_factory=list)
    lang: str = "en"                # language of the SMS sent to contacts

    def to_dict(self) -> dict:
        return asdict(self)


def from_api(body: dict, *, event_id: str, received_at: int, checkin_id: str) -> CheckIn:
    """Build a CheckIn from the web page's JSON body. Raises InvalidCheckIn with a reason."""
    if not isinstance(body, dict):
        raise InvalidCheckIn("body is not a JSON object")
    try:
        phone = normalise(str(body.get("phone", "")))
    except InvalidPhone as e:
        raise InvalidCheckIn(str(e)) from e
    status = str(body.get("status", "")).strip().lower()
    if status not in config.STATUSES:
        raise InvalidCheckIn(f"status must be one of {config.STATUSES}")
    contacts = []
    for raw in (body.get("contacts") or [])[: config.MAX_CONTACTS]:
        try:
            c = normalise(str(raw))
        except InvalidPhone:
            continue  # a bad contact number should not lose the person's own check-in
        if c != phone and c not in contacts:
            contacts.append(c)
    return CheckIn(
        event_id=event_id, phone=phone, status=status, received_at=int(received_at),
        checkin_id=checkin_id, channel="web", district=district_from(body.get("district")),
        note=clean_note(body.get("note")), contacts=contacts,
        lang="ne" if body.get("lang") == "ne" else "en",
    )


# ---------- SMS ----------

# Keywords in English, romanised Nepali, and Devanagari. Matched on the first word.
_SMS_STATUS = {
    "safe": "safe", "ok": "safe", "thik": "safe", "theek": "safe", "surakshit": "safe",
    "ठीक": "safe", "ठिक": "safe", "सुरक्षित": "safe",
    "help": "need_help", "sahayog": "need_help", "maddat": "need_help", "bachau": "need_help",
    "सहयोग": "need_help", "मद्दत": "need_help", "बचाउ": "need_help",
    "injured": "injured", "ghaite": "injured", "घाइते": "injured",
}
_SMS_FIND = {"find", "khoj", "khoja", "खोज", "खोज्नुहोस्"}
_ENGLISH_KEYWORDS = {"safe", "ok", "help", "injured", "find"}
# A Nepal mobile number inside free text, in either script, with optional +977.
# Exactly eight more digits, each optionally preceded by one separator. The first version
# allowed "8 to 12 digits or spaces", which swallowed the start of the *next* number in
# "9841000001 ९८५१०००००२" and lost both. \d matches Devanagari digits in Python 3.
_NUMBER_IN_TEXT = re.compile(r"(?:\+?977[\s\-]?)?[9९][6-8६-८](?:[\s\-]?\d){8}(?!\d)")


@dataclass
class SmsCommand:
    kind: str                       # checkin | find | unknown
    status: str | None = None
    district: str | None = None
    note: str = ""
    numbers: list[str] = field(default_factory=list)
    lang: str = "en"                # reply language: follows the keyword the person used


def parse_sms(text: str) -> SmsCommand:
    """Parse an inbound SMS.

    Examples:  "SAFE"   "safe kavre"   "HELP Sindhupalchok house collapsed 9841000001"
               "ठीक छु"   "khoj 9841234567"
    Numbers found in a check-in are family contacts to notify; in FIND, the number to look up.
    """
    text = clean_note(text)
    numbers: list[str] = []
    for m in _NUMBER_IN_TEXT.finditer(text):
        try:
            n = normalise(m.group(0))
        except InvalidPhone:
            continue
        if n not in numbers:
            numbers.append(n)
    rest = _NUMBER_IN_TEXT.sub(" ", text).split()
    if not rest:
        return SmsCommand(kind="unknown", lang=_guess_lang(text))
    first = rest[0].lower().strip(".,!।")
    lang = "en" if first in _ENGLISH_KEYWORDS else "ne"
    if first in _SMS_FIND:
        return SmsCommand(kind="find", numbers=numbers[:1], lang=lang)
    status = _SMS_STATUS.get(first)
    if status is None:
        return SmsCommand(kind="unknown", lang=_guess_lang(text))
    words = rest[1:]
    district = None
    # The district can be anywhere: "safe kavre", but also "ठीक छु काठमाडौं" ("I'm fine, Kathmandu").
    for i, w in enumerate(words):
        district = district_from(w.strip(".,!।"))
        if district:
            words = words[:i] + words[i + 1:]
            break
    return SmsCommand(kind="checkin", status=status, district=district, note=" ".join(words),
                      numbers=numbers[: config.MAX_CONTACTS], lang=lang)


def _guess_lang(text: str) -> str:
    return "ne" if any("\u0900" <= ch <= "\u097f" for ch in text) else "en"
