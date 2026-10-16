"""Nepali mobile numbers: normalise what people actually type, and turn it into a storage key.

People type the same number many ways: 9841234567, 984-1234567, +977 9841234567,
00977-9841234567, and on a Nepali keyboard ९८४१२३४५६७. All of them must land on the same key,
or a family looking someone up will be told "no check-in" while the person is marked safe.
"""

from __future__ import annotations

import hashlib
import hmac
import re

# Devanagari digits U+0966..U+096F. Phones with a Nepali keyboard send these by default.
_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
# Anything a person might put between digits.
_SEPARATORS = re.compile(r"[\s\-().\u200b\u200c\u200d]")
# Nepal mobile numbers: 10 digits, starting 96, 97 or 98 (NTC 984-986/974-976, Ncell 980-982,
# Smart 961/962/988, UTL 972). Landlines are excluded: they can't receive SMS.
_NP_MOBILE = re.compile(r"^9[678]\d{8}$")


class InvalidPhone(ValueError):
    pass


def normalise(raw: str) -> str:
    """Return a Nepal mobile number in E.164 form (+9779XXXXXXXXX) or raise InvalidPhone."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidPhone("empty")
    s = _SEPARATORS.sub("", raw.strip().translate(_DEVANAGARI))
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    if s.startswith("977") and len(s) == 13:
        s = s[3:]
    elif s.startswith("0") and len(s) == 11:  # some people dial a trunk 0 out of habit
        s = s[1:]
    if not _NP_MOBILE.match(s):
        raise InvalidPhone(f"not a Nepal mobile number: {raw!r}")
    return "+977" + s


def phone_key(e164: str, pepper: bytes) -> str:
    """Keyed hash used as the DynamoDB partition key. The table never stores a phone number.

    HMAC with a secret pepper (from SSM Parameter Store), not a plain SHA-256: there are only
    300 million possible Nepal mobile numbers (96/97/98 + 8 digits), so an unkeyed hash can
    be reversed by hashing every one of them, which takes minutes on a laptop.
    """
    if not pepper:
        raise ValueError("pepper must not be empty")
    return hmac.new(pepper, e164.encode(), hashlib.sha256).hexdigest()[:32]


def mask(e164: str) -> str:
    """For logs and SMS replies: +977 98XXXXX567."""
    return f"{e164[:4]} {e164[4:6]}XXXXX{e164[-3:]}"
