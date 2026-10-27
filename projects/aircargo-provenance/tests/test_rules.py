"""Unit tests for api/rules.py (no HTTP, no database)."""

from datetime import datetime, timedelta, timezone

import pytest

from api import rules


SHIPMENT = "https://example.org/logistics-objects/shipment-001"
ORG_A = "https://airline.example/org"
ORG_B = "https://handler.example/org"
BASE = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def event(code, at, org=ORG_A, time_type="ACTUAL", partial=False, n=1):
    return rules.ParsedEvent(
        event_id=f"https://example.org/events/{n}",
        shipment_id=SHIPMENT,
        status_code=code,
        milestone_rank=rules.STATUS_CODE_TO_RANK.get(code),
        event_date=at,
        time_type=time_type,
        recording_org=org,
        partial=partial,
    )


def document(**overrides):
    doc = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": "https://airline.example/events/evt-1",
        "@type": "cargo:LogisticsEvent",
        "cargo:eventFor": {"@id": SHIPMENT},
        "cargo:eventCode": {"@id": rules.STATUS_CODE_BASE + "DEP"},
        "cargo:eventDate": {
            "@type": "http://www.w3.org/2001/XMLSchema#dateTime",
            "@value": "2026-10-27T10:00:00Z",
        },
        "cargo:eventTimeType": {"@id": "cargo:ACTUAL"},
        "cargo:recordingOrganization": {"@id": ORG_A},
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not None}


# ------------------------------ parsing ------------------------------------ #

def test_parse_full_event():
    parsed = rules.parse_event(document())
    assert parsed.shipment_id == SHIPMENT
    assert parsed.status_code == "DEP"
    assert parsed.milestone == "DEPARTED"
    assert parsed.event_date == BASE
    assert parsed.time_type == "ACTUAL"
    assert parsed.recording_org == ORG_A
    assert parsed.partial is False
    assert parsed.comparable is True


def test_parse_accepts_plain_string_forms():
    parsed = rules.parse_event(
        document(
            **{
                "cargo:eventFor": SHIPMENT,
                "cargo:eventCode": "ARR",
                "cargo:eventDate": "2026-10-27T15:30:00+05:30",
                "cargo:recordingOrganization": ORG_B,
            }
        )
    )
    assert parsed.milestone == "ARRIVED"
    assert parsed.event_date == BASE  # 15:30 IST == 10:00 UTC


def test_time_type_defaults_to_actual():
    parsed = rules.parse_event(document(**{"cargo:eventTimeType": None}))
    assert parsed.time_type == "ACTUAL"


def test_full_iri_type_is_accepted():
    parsed = rules.parse_event(
        document(**{"@type": rules.CARGO_NS + "LogisticsEvent"})
    )
    assert parsed.status_code == "DEP"


def test_unmapped_status_code_is_out_of_scope():
    parsed = rules.parse_event(
        document(**{"cargo:eventCode": {"@id": rules.STATUS_CODE_BASE + "FOH"}})
    )
    assert parsed.in_scope is False
    assert parsed.comparable is False


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"@type": "cargo:Shipment"}, "@type"),
        ({"cargo:eventFor": None}, "cargo:eventFor"),
        ({"cargo:eventCode": None}, "cargo:eventCode"),
        ({"cargo:eventCode": "IN_TRANSIT"}, "not a valid status code"),
        (
            {"cargo:eventCode": {"@id": "https://other.example/codes#DEP"}},
            "StatusCode",
        ),
        ({"cargo:eventDate": None}, "cargo:eventDate"),
        ({"cargo:eventDate": "2026-10-27T10:00:00"}, "timezone"),
        ({"cargo:eventDate": "yesterday"}, "ISO 8601"),
        ({"cargo:recordingOrganization": None}, "recordingOrganization"),
        ({"cargo:recordingOrganization": "airline"}, "absolute"),
        ({"cargo:partialEventIndicator": "maybe"}, "boolean"),
    ],
)
def test_invalid_events_are_rejected(overrides, message):
    with pytest.raises(rules.EventValidationError, match=message):
        rules.parse_event(document(**overrides))


# ------------------------------ R1 severity -------------------------------- #

def test_identical_replay_is_low_severity():
    assert rules.duplicate_severity(document(), document()) == "LOW"


def test_key_order_does_not_matter_for_replay():
    reordered = dict(reversed(list(document().items())))
    assert rules.duplicate_severity(document(), reordered) == "LOW"


def test_changed_content_is_high_severity():
    changed = document(**{"cargo:eventDate": "2026-10-27T11:00:00Z"})
    assert rules.duplicate_severity(document(), changed) == "HIGH"


# ------------------------------ R2 boundaries ------------------------------ #

def test_same_milestone_exactly_at_delta_corroborates():
    stored = event("DEP", BASE)
    new = event("DEP", BASE + rules.SAME_MILESTONE_TOLERANCE, ORG_B, n=2)
    assert rules.compare(new, stored) is None


def test_same_milestone_just_over_delta_is_mismatch():
    stored = event("DEP", BASE)
    new = event(
        "DEP",
        BASE + rules.SAME_MILESTONE_TOLERANCE + timedelta(seconds=1),
        ORG_B,
        n=2,
    )
    hit = rules.compare(new, stored)
    assert hit.conflict_type == rules.STATUS_MISMATCH
    assert hit.severity == "MEDIUM"


def test_mismatch_is_symmetric_in_time():
    stored = event("DEP", BASE)
    new = event("DEP", BASE - timedelta(hours=4), ORG_B, n=2)
    assert rules.compare(new, stored).conflict_type == rules.STATUS_MISMATCH


# ------------------------------ R3 boundaries ------------------------------ #

def test_order_violation_exactly_at_epsilon_is_accepted():
    stored = event("DEP", BASE)
    new = event("RCS", BASE + rules.CLOCK_SKEW_TOLERANCE, ORG_B, n=2)
    assert rules.compare(new, stored) is None


def test_order_violation_just_over_epsilon_is_invalid():
    stored = event("DEP", BASE)
    new = event(
        "RCS",
        BASE + rules.CLOCK_SKEW_TOLERANCE + timedelta(seconds=1),
        ORG_B,
        n=2,
    )
    hit = rules.compare(new, stored)
    assert hit.conflict_type == rules.INVALID_TRANSITION
    assert hit.severity == "HIGH"


def test_later_milestone_reported_before_earlier_is_invalid():
    stored = event("DEP", BASE)
    new = event("ARR", BASE - timedelta(hours=1), ORG_B, n=2)
    assert rules.compare(new, stored).conflict_type == rules.INVALID_TRANSITION


def test_gaps_in_sequence_are_allowed():
    stored = event("RCS", BASE)
    new = event("DLV", BASE + timedelta(hours=8), ORG_B, n=2)
    assert rules.compare(new, stored) is None


def test_same_time_different_milestones_is_accepted():
    stored = event("DEP", BASE)
    new = event("DLV", BASE, ORG_B, n=2)
    assert rules.compare(new, stored) is None


# ------------------------------ filters ------------------------------------ #

@pytest.mark.parametrize(
    "new",
    [
        event("DEP", BASE + timedelta(hours=5), ORG_B, time_type="PLANNED", n=2),
        event("DEP", BASE + timedelta(hours=5), ORG_B, partial=True, n=2),
        event("FOH", BASE + timedelta(hours=5), ORG_B, n=2),
    ],
    ids=["planned", "partial", "unmapped"],
)
def test_non_comparable_events_never_conflict(new):
    assert rules.compare(new, event("DEP", BASE)) is None


def test_different_shipments_never_conflict():
    stored = event("DEP", BASE)
    other = rules.ParsedEvent(
        **{
            **event("DEP", BASE + timedelta(hours=5), ORG_B, n=2).__dict__,
            "shipment_id": "https://example.org/logistics-objects/other",
        }
    )
    assert rules.compare(other, stored) is None
