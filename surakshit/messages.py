"""Outbound SMS text in English or Nepali, never both.

Every Devanagari character forces an SMS into UCS-2 encoding: 70 characters per segment
instead of 160. A bilingual message is 3-4 billed segments; the same message in one language
is 1-2. So the reply language follows the sender: a Devanagari or romanised-Nepali keyword gets
a Nepali reply, an English one gets English. See docs/COST.md, "SMS is the expensive part".
"""

from __future__ import annotations

from datetime import UTC, datetime

from surakshit.config import NPT
from surakshit.phone import mask

_STATUS = {
    "en": {"safe": "is SAFE", "need_help": "NEEDS HELP", "injured": "is INJURED"},
    "ne": {"safe": "सुरक्षित हुनुहुन्छ", "need_help": "लाई सहयोग चाहिएको छ", "injured": "घाइते हुनुहुन्छ"},
}


def _hhmm(received_at_ms: int) -> str:
    t = datetime.fromtimestamp(received_at_ms / 1000, UTC).replace(tzinfo=None) + NPT
    return t.strftime("%d %b %H:%M")


def segments(text: str) -> int:
    """Billed SMS segments: GSM-7 is 160 chars (153 when split), UCS-2 is 70 (67 when split).
    Approximates GSM-7 as ASCII, which is close enough for cost estimates."""
    if all(ord(c) < 128 for c in text):
        single, multi = 160, 153
    else:
        single, multi = 70, 67
    n = len(text)
    return 1 if n <= single else -(-n // multi)


def notify_contact(phone: str, status: str, received_at: int, district: str | None, note: str,
                   lang: str = "en") -> str:
    who, when = mask(phone), _hhmm(received_at)
    if lang == "ne":
        where = f" ({district})" if district else ""
        msg = f"सुरक्षित: {who}{where} {_STATUS['ne'][status]}। {when}"
    else:
        where = f" in {district}" if district else ""
        msg = f"Surakshit: {who} {_STATUS['en'][status]}{where} ({when} NPT)."
    if note:
        msg += f' "{note}"'
    return msg


# Second person for the confirmation. Built from the third-person phrases above, the first
# version said "Recorded: you needs help".
_YOU = {"en": {"safe": "you are SAFE", "need_help": "you NEED HELP", "injured": "you are INJURED"},
        "ne": {"safe": "तपाईं सुरक्षित", "need_help": "तपाईंलाई सहयोग चाहिएको", "injured": "तपाईं घाइते"}}


def reply_checkin(status: str, lang: str = "en") -> str:
    if lang == "ne":
        return f"दर्ता भयो: {_YOU['ne'][status]}। परिवारले KHOJ <तपाईंको नम्बर> पठाएर हेर्न सक्छन्।"
    return f"Recorded: {_YOU['en'][status]}. Family can text FIND <your number> to check."


def reply_find(phone: str, found: dict | None, lang: str = "en") -> str:
    who = mask(phone)
    if not found:
        if lang == "ne":
            return f"{who} को कुनै जानकारी छैन। यसको अर्थ खतरा होइन: सञ्जाल बन्द हुन सक्छ।"
        return f"No check-in from {who} yet. That is not bad news: networks may be down."
    when = _hhmm(int(found["received_at"]))
    district = found.get("district")
    if lang == "ne":
        where = f" ({district})" if district else ""
        return f"{who}{where} {_STATUS['ne'][found['status']]}। {when}"
    where = f" in {district}" if district else ""
    return f"{who} {_STATUS['en'][found['status']]}{where}, checked in {when} NPT."


def reply_help(lang: str = "en") -> str:
    if lang == "ne":
        return "पठाउनुहोस्: THIK (सुरक्षित), SAHAYOG (सहयोग चाहियो), GHAITE (घाइते), वा KHOJ <नम्बर>।"
    return "Text SAFE, HELP or INJURED (optionally your district), or FIND <number>."
