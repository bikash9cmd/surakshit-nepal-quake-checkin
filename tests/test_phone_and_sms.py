import pytest

from surakshit.checkin import InvalidCheckIn, clean_note, from_api, parse_sms
from surakshit.phone import InvalidPhone, mask, normalise, phone_key


@pytest.mark.parametrize("raw", [
    "9841234567", "984-1234567", "+977 9841234567", "00977-9841234567", "977 984 123 4567",
    "(984) 123-4567", "09841234567", "९८४१२३४५६७", "+९७७ ९८४१२३४५६७", "98412\u200b34567",
])
def test_every_way_people_type_a_number_lands_on_the_same_key(raw):
    assert normalise(raw) == "+9779841234567"


@pytest.mark.parametrize("raw", ["", "014412345", "+919841234567", "984123456", "98412345678", "abc", "9541234567"])
def test_landlines_foreign_and_wrong_length_are_rejected(raw):
    with pytest.raises(InvalidPhone):
        normalise(raw)


def test_phone_key_is_keyed_and_stable():
    a = phone_key("+9779841234567", b"pepper-1")
    assert a == phone_key("+9779841234567", b"pepper-1")
    assert a != phone_key("+9779841234567", b"pepper-2")
    assert "9841234567" not in a
    with pytest.raises(ValueError):
        phone_key("+9779841234567", b"")


def test_mask_hides_the_middle():
    assert mask("+9779841234567") == "+977 98XXXXX567"


@pytest.mark.parametrize("text,status,district,lang", [
    ("SAFE", "safe", None, "en"),
    ("safe kavre", "safe", "Kavrepalanchok", "en"),
    ("ठीक छु काठमाडौं", "safe", "Kathmandu", "ne"),
    ("thik chu", "safe", None, "ne"),
    ("HELP Sindhupalchowk ghar bhatkiyo", "need_help", "Sindhupalchok", "en"),
    ("सहयोग गोरखा", "need_help", "Gorkha", "ne"),
    ("ghaite", "injured", None, "ne"),
])
def test_sms_keywords_in_english_romanised_nepali_and_devanagari(text, status, district, lang):
    cmd = parse_sms(text)
    assert (cmd.kind, cmd.status, cmd.district, cmd.lang) == ("checkin", status, district, lang)


def test_sms_numbers_become_contacts_and_leave_the_note():
    cmd = parse_sms("HELP gorkha house fell 9841000001 ९८५१०००००२")
    assert cmd.numbers == ["+9779841000001", "+9779851000002"]
    assert cmd.note == "house fell"


def test_sms_find_and_unknown():
    assert parse_sms("khoj 9841000001").numbers == ["+9779841000001"]
    assert parse_sms("FIND +977-9841000001").kind == "find"
    assert parse_sms("hello?").kind == "unknown"
    assert parse_sms("नमस्ते").lang == "ne"


def test_api_body_validation():
    c = from_api({"phone": "9841234567", "status": "SAFE", "district": "ktm",
                  "contacts": ["9841234567", "9851111111", "9851111111", "bad", "9861111111", "9801111111"]},
                 event_id="e", received_at=1, checkin_id="r")
    assert c.status == "safe" and c.district == "Kathmandu"
    # own number and duplicates dropped, bad number skipped, capped at MAX_CONTACTS input entries
    assert c.contacts == ["+9779851111111"]
    for bad in ({"phone": "9841234567", "status": "dead"}, {"phone": "12", "status": "safe"}, []):
        with pytest.raises(InvalidCheckIn):
            from_api(bad, event_id="e", received_at=1, checkin_id="r")


def test_note_is_normalised_and_cut_by_characters_not_bytes():
    # NA + NUKTA typed as two code points composes to NNNA under NFC, so the same word typed on two
    # keyboards compares equal. (KA + NUKTA does not: U+0958 is a composition exclusion.)
    assert clean_note("\u0928\u093c") == "\u0929"
    assert len(clean_note("घ" * 500)) == 140
    assert clean_note("a\x00b\n\nc") == "ab c"
    assert parse_sms("HELP\ngorkha").district == "Gorkha"
