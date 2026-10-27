"""API tests for logistics events: the 19 worked cases in
docs/CONFLICT_RULES.md section 8, plus storage and migration checks."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api import main as api
from api import rules


SHIPMENT = "https://example.org/logistics-objects/shipment-ev-001"
ORG_A = "https://airline.example/org"
ORG_B = "https://handler.example/org"
ORG_C = "https://forwarder.example/org"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "test_events.db")
    with TestClient(api.app) as test_client:
        response = test_client.post(
            "/logistics-objects",
            json={
                "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
                "@id": SHIPMENT,
                "@type": "cargo:Shipment",
            },
        )
        assert response.status_code == 201
        yield test_client


_counter = {"n": 0}


def make_event(code, time, org=ORG_A, time_type="ACTUAL", partial=None,
               shipment=SHIPMENT, event_id=None):
    """Build a ONE Record-style LogisticsEvent. `time` is "HH:MM" UTC."""
    _counter["n"] += 1
    doc = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": event_id or f"https://events.example/evt-{_counter['n']}",
        "@type": "cargo:LogisticsEvent",
        "cargo:eventFor": {"@id": shipment},
        "cargo:eventCode": {"@id": rules.STATUS_CODE_BASE + code},
        "cargo:eventDate": {
            "@type": "http://www.w3.org/2001/XMLSchema#dateTime",
            "@value": f"2026-10-27T{time}:00Z",
        },
        "cargo:eventTimeType": {"@id": f"cargo:{time_type}"},
        "cargo:recordingOrganization": {"@id": org},
    }
    if partial is not None:
        doc["cargo:partialEventIndicator"] = partial
    return doc


def post_event(client, document, shipment=SHIPMENT):
    return client.post(
        f"/logistics-objects/{shipment}/logistics-events", json=document
    )


def conflict_types(response):
    return sorted(c["conflictType"] for c in response.json()["conflicts"])


# --------------------------------------------------------------------------- #
# Cases 1-10 and 13-15: stored events, one new event, expected conflicts.
# --------------------------------------------------------------------------- #

RULE_CASES = [
    # id, stored [(code, time, org)], new (code, time, org, extra), expected
    ("01-first-event", [], ("DEP", "10:00", ORG_A, {}), []),
    ("02-gap-allowed", [("RCS", "08:00", ORG_A)],
     ("DEP", "10:00", ORG_B, {}), []),
    ("03-in-transit-then-delivered", [("DEP", "10:00", ORG_A)],
     ("DLV", "18:00", ORG_B, {}), []),
    ("04-departed-after-delivered", [("DLV", "18:00", ORG_A)],
     ("DEP", "19:00", ORG_B, {}), ["INVALID_TRANSITION"]),
    ("05-received-after-departed", [("DEP", "10:00", ORG_A)],
     ("RCS", "12:00", ORG_B, {}), ["INVALID_TRANSITION"]),
    ("06-within-clock-skew", [("DEP", "10:00", ORG_A)],
     ("ARR", "09:58", ORG_B, {}), []),
    ("07-corroborating-report", [("DEP", "10:00", ORG_A)],
     ("DEP", "10:20", ORG_B, {}), []),
    ("08-same-milestone-disagrees", [("DEP", "10:00", ORG_A)],
     ("DEP", "14:00", ORG_B, {}), ["STATUS_MISMATCH"]),
    ("09-mismatch-with-one-corroborates-other",
     [("DEP", "10:00", ORG_A), ("DEP", "14:00", ORG_B)],
     ("DEP", "14:05", ORG_C, {}), ["STATUS_MISMATCH"]),
    ("10-violates-two-events",
     [("DEP", "10:00", ORG_A), ("ARR", "12:00", ORG_A)],
     ("RCS", "13:00", ORG_B, {}),
     ["INVALID_TRANSITION", "INVALID_TRANSITION"]),
    ("13-planned-not-compared", [("DEP", "10:00", ORG_A)],
     ("DEP", "15:00", ORG_B, {"time_type": "PLANNED"}), []),
    ("14-partial-not-compared", [("DEP", "10:00", ORG_A)],
     ("DEP", "15:00", ORG_B, {"partial": True}), []),
    ("15-unmapped-code-out-of-scope", [("DEP", "10:00", ORG_A)],
     ("FOH", "11:00", ORG_B, {}), []),
]


@pytest.mark.parametrize(
    "stored, new, expected",
    [case[1:] for case in RULE_CASES],
    ids=[case[0] for case in RULE_CASES],
)
def test_rule_cases(client, stored, new, expected):
    for code, time, org in stored:
        setup = post_event(client, make_event(code, time, org))
        assert setup.status_code == 201

    code, time, org, extra = new
    response = post_event(client, make_event(code, time, org, **extra))

    assert response.status_code == 201
    body = response.json()
    assert conflict_types(response) == expected
    assert body["conflictDetected"] is bool(expected)
    assert body["ingestionStatus"] == (
        "ACCEPTED_DISPUTED" if expected else "ACCEPTED"
    )


def test_case_09_conflict_points_at_the_disagreeing_event(client):
    first = make_event("DEP", "10:00", ORG_A)
    post_event(client, first)
    post_event(client, make_event("DEP", "14:00", ORG_B))

    response = post_event(client, make_event("DEP", "14:05", ORG_C))

    (conflict,) = response.json()["conflicts"]
    assert conflict["relatedEventId"] == first["@id"]
    assert conflict["severity"] == "MEDIUM"


def test_case_15_reports_out_of_scope(client):
    response = post_event(client, make_event("FOH", "11:00"))
    assert response.status_code == 201
    assert response.json()["inScope"] is False
    assert response.json()["compared"] is False


# --------------------------------------------------------------------------- #
# Cases 11-12: duplicate identifiers (R1).
# --------------------------------------------------------------------------- #

def test_case_11_identical_replay_is_low_duplicate(client):
    original = make_event("DEP", "10:00")
    assert post_event(client, original).status_code == 201

    replay = post_event(client, original)

    assert replay.status_code == 409
    assert replay.json()["conflictType"] == "DUPLICATE_IDENTIFIER"
    assert replay.json()["severity"] == "LOW"
    events = client.get(f"/logistics-objects/{SHIPMENT}/logistics-events")
    assert events.json()["count"] == 1


def test_case_12_same_id_different_content_is_high_duplicate(client):
    original = make_event("DEP", "10:00", event_id="https://events.example/x")
    changed = make_event("DEP", "11:00", event_id="https://events.example/x")
    assert post_event(client, original).status_code == 201

    response = post_event(client, changed)

    assert response.status_code == 409
    assert response.json()["conflictType"] == "DUPLICATE_IDENTIFIER"
    assert response.json()["severity"] == "HIGH"
    events = client.get(f"/logistics-objects/{SHIPMENT}/logistics-events")
    assert events.json()["items"][0]["event_date"].startswith(
        "2026-10-27T10:00"
    )


def test_duplicate_does_not_also_run_r2_r3(client):
    original = make_event("DEP", "10:00", event_id="https://events.example/y")
    post_event(client, original)
    post_event(client, make_event("DEP", "16:00", ORG_B))

    changed = make_event("DEP", "20:00", event_id="https://events.example/y")
    response = post_event(client, changed)

    assert response.status_code == 409
    conflicts = client.get("/conflicts").json()["items"]
    assert [c["conflict_type"] for c in conflicts].count(
        "DUPLICATE_IDENTIFIER"
    ) == 1


def test_event_id_colliding_with_shipment_id_is_duplicate(client):
    response = post_event(client, make_event("DEP", "10:00", event_id=SHIPMENT))
    assert response.status_code == 409
    assert response.json()["severity"] == "HIGH"


# --------------------------------------------------------------------------- #
# Cases 16-19: validation errors create no conflict.
# --------------------------------------------------------------------------- #

def _no_conflicts(client):
    return client.get("/conflicts").json()["count"] == 0


def test_case_16_unknown_shipment_returns_404(client):
    unknown = "https://example.org/logistics-objects/unknown"
    response = post_event(
        client, make_event("DEP", "10:00", shipment=unknown), shipment=unknown
    )
    assert response.status_code == 404
    assert _no_conflicts(client)


def test_case_17_event_for_must_match_path(client):
    other = "https://example.org/logistics-objects/other"
    response = post_event(client, make_event("DEP", "10:00", shipment=other))
    assert response.status_code == 422
    assert _no_conflicts(client)


def test_case_18_event_date_needs_timezone(client):
    document = make_event("DEP", "10:00")
    document["cargo:eventDate"] = "2026-10-27T10:00:00"
    response = post_event(client, document)
    assert response.status_code == 422
    assert _no_conflicts(client)


@pytest.mark.parametrize("code", [None, "IN_TRANSIT"])
def test_case_19_missing_or_invalid_event_code(client, code):
    document = make_event("DEP", "10:00")
    if code is None:
        del document["cargo:eventCode"]
    else:
        document["cargo:eventCode"] = code
    response = post_event(client, document)
    assert response.status_code == 422
    assert _no_conflicts(client)


# --------------------------------------------------------------------------- #
# Storage, listing and routing.
# --------------------------------------------------------------------------- #

def test_both_events_in_a_conflict_are_marked_disputed(client):
    first = make_event("DEP", "10:00", ORG_A)
    second = make_event("DEP", "14:00", ORG_B)
    third = make_event("ARR", "18:00", ORG_A)
    for document in (first, second, third):
        post_event(client, document)

    items = client.get(
        f"/logistics-objects/{SHIPMENT}/logistics-events"
    ).json()["items"]
    disputed = {item["event_id"]: item["disputed"] for item in items}

    assert disputed == {
        first["@id"]: True,
        second["@id"]: True,
        third["@id"]: False,
    }


def test_conflict_record_links_both_events_and_shipment(client):
    first = make_event("DLV", "18:00", ORG_A)
    second = make_event("DEP", "19:00", ORG_B)
    post_event(client, first)
    conflict_id = post_event(client, second).json()["conflicts"][0]["conflictId"]

    stored = client.get(f"/conflicts/{conflict_id}").json()

    assert stored["object_id"] == second["@id"]
    assert stored["related_object_id"] == first["@id"]
    assert stored["shipment_id"] == SHIPMENT
    assert stored["conflict_type"] == "INVALID_TRANSITION"
    assert stored["severity"] == "HIGH"
    assert stored["status"] == "OPEN"


def test_event_conflicts_can_be_resolved(client):
    post_event(client, make_event("DEP", "10:00", ORG_A))
    response = post_event(client, make_event("DEP", "14:00", ORG_B))
    conflict_id = response.json()["conflicts"][0]["conflictId"]

    resolved = client.patch(f"/conflicts/{conflict_id}/resolve")

    assert resolved.status_code == 200
    assert client.get(f"/conflicts/{conflict_id}").json()["status"] == "RESOLVED"


def test_events_are_listed_in_time_order(client):
    post_event(client, make_event("ARR", "16:00"))
    post_event(client, make_event("RCS", "08:00"))
    post_event(client, make_event("DEP", "10:00"))

    items = client.get(
        f"/logistics-objects/{SHIPMENT}/logistics-events"
    ).json()["items"]

    assert [item["milestone"] for item in items] == [
        "RECEIVED", "DEPARTED", "ARRIVED"
    ]


def test_listing_events_for_unknown_shipment_returns_404(client):
    response = client.get(
        "/logistics-objects/https://example.org/x/logistics-events"
    )
    assert response.status_code == 404


def test_shipment_retrieval_still_works_with_event_routes(client):
    response = client.get(f"/logistics-objects/{SHIPMENT}")
    assert response.status_code == 200
    assert response.json()["object_id"] == SHIPMENT


def test_events_cannot_be_posted_as_logistics_objects(client):
    response = client.post("/logistics-objects", json=make_event("DEP", "10:00"))
    assert response.status_code == 422
    assert "logistics-events" in response.json()["detail"]


def test_metrics_count_events_and_conflict_types(client):
    post_event(client, make_event("DEP", "10:00", ORG_A))
    post_event(client, make_event("DEP", "14:00", ORG_B))

    data = client.get("/metrics").json()

    assert data["logisticsEventsStored"] == 2
    assert data["disputedEvents"] == 2
    assert data["conflictsByType"] == {"STATUS_MISMATCH": 1}


# --------------------------------------------------------------------------- #
# Migration of a database created by the previous version.
# --------------------------------------------------------------------------- #

def test_old_database_is_migrated_on_startup(tmp_path, monkeypatch):
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.execute("""
        CREATE TABLE conflicts (
            conflict_id TEXT PRIMARY KEY, object_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL, severity TEXT NOT NULL,
            description TEXT NOT NULL, status TEXT NOT NULL,
            detected_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO conflicts VALUES "
        "('old-1', 'https://e.org/a', 'DUPLICATE_IDENTIFIER', 'MEDIUM', "
        "'old row', 'OPEN', '2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(api, "DB_PATH", old_db)
    with TestClient(api.app) as client:
        old_row = client.get("/conflicts/old-1")
        assert old_row.status_code == 200
        assert old_row.json()["related_object_id"] is None
        assert old_row.json()["shipment_id"] is None
        assert client.get("/metrics").json()["logisticsEventsStored"] == 0
