"""Party reports: attribute rules R5-R12, location rule R4, and the API."""

import pytest
from fastapi.testclient import TestClient

from api import main as api
from api import report_rules as rr
from api import rules


SHIPMENT = "https://example.org/logistics-objects/shipment-rp-001"
FFW = "https://forwarder.example/org"
AIR = "https://airline.example/org"
GHA = "https://handler.example/org"
UNIT = rr.MEASUREMENT_UNIT_BASE
SHC = rr.SPECIAL_HANDLING_BASE
DG = rr.DANGEROUS_GOODS_BASE
ROLE = rules.PARTICIPANT_BASE

_n = {"i": 0}


def report_doc(org=FFW, role="FFW", report_id=None, shipment=SHIPMENT, **fields):
    """A party report. Field shortcuts: weight=(value, unit),
    volume=(value, unit), pieces, origin, destination, waybill, shc=[codes],
    shipper=(name, id|None), consignee=(name, id|None)."""
    _n["i"] += 1
    doc = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": report_id or f"https://reports.example/r-{_n['i']}",
        "@type": "cargo:Shipment",
        "acp:reportFor": {"@id": shipment},
        "acp:recordingOrganization": {"@id": org},
        "acp:reporterRole": {"@id": ROLE + role},
    }
    if "weight" in fields:
        value, unit = fields["weight"]
        doc["cargo:totalGrossWeight"] = {
            "cargo:numericalValue": value, "cargo:unit": {"@id": UNIT + unit},
        }
    if "volume" in fields:
        value, unit = fields["volume"]
        doc["cargo:totalDimensions"] = {"cargo:volume": {
            "cargo:numericalValue": value, "cargo:unit": {"@id": UNIT + unit},
        }}
    if "pieces" in fields:
        doc["cargo:slac"] = fields["pieces"]
    if "origin" in fields:
        doc["cargo:departureLocation"] = {"cargo:locationCodes": [{"cargo:code": fields["origin"]}]}
    if "destination" in fields:
        doc["cargo:arrivalLocation"] = {"cargo:locationCodes": [{"cargo:code": fields["destination"]}]}
    if "waybill" in fields:
        doc["cargo:waybillNumber"] = fields["waybill"]
    if "shc" in fields:
        doc["cargo:specialHandlingCodes"] = [
            {"@id": (DG if c in rr.DANGEROUS_GOODS_CODES else SHC) + c} for c in fields["shc"]
        ]
    parties = []
    for role_code, key in (("SHP", "shipper"), ("CNE", "consignee")):
        if key in fields:
            name, org_id = fields[key]
            details = {"cargo:name": name}
            if org_id:
                details["@id"] = org_id
            parties.append({"cargo:partyRole": {"@id": ROLE + role_code},
                            "cargo:partyDetails": details})
    if parties:
        doc["cargo:involvedParties"] = parties
    return doc


def parsed(**kwargs):
    return rr.parse_report(report_doc(**kwargs))


def types(hits):
    return sorted(h.conflict_type for h in hits)


# ------------------------------- parsing ----------------------------------- #

def test_parse_full_report():
    report = parsed(weight=(1250, "KGM"), volume=(7.5, "MTQ"), pieces=10,
                    origin="bom", destination="FRA", waybill="176-1234 5675",
                    shc=["PER", "RCL"], shipper=("Tata Exports Pvt Ltd", None),
                    consignee=("Muller GmbH", "https://muller.example/org"))
    assert report.weight.normalised == 1250
    assert report.volume.normalised == 7.5
    assert report.pieces == 10
    assert (report.origin, report.destination) == ("BOM", "FRA")
    assert report.waybill == "17612345675"
    assert report.special_handling == ("PER", "RCL")
    assert report.parties["CNE"].org_id == "https://muller.example/org"
    assert report.reporter_role == "FFW"


def test_report_roundtrips_through_storage_format():
    report = parsed(weight=(10, "LBR"), shc=["PER"], shipper=("A", None))
    assert rr.ParsedReport.from_dict(report.to_dict()) == report


@pytest.mark.parametrize("bad, message", [
    ({"cargo:totalGrossWeight": {"cargo:numericalValue": 10}}, "missing cargo:unit"),
    ({"cargo:totalGrossWeight": {"cargo:numericalValue": 10, "cargo:unit": "MTQ"}}, "not supported"),
    ({"cargo:totalGrossWeight": {"cargo:numericalValue": -5, "cargo:unit": "KGM"}}, "greater than zero"),
    ({"cargo:totalGrossWeight": {"cargo:numericalValue": "heavy", "cargo:unit": "KGM"}}, "numeric"),
    ({"cargo:slac": 0}, "positive integer"),
    ({"cargo:departureLocation": "Mumbai"}, "IATA"),
    ({"cargo:specialHandlingCodes": [{"@id": "https://x.example/codes#PER"}]}, "unknown code list"),
    ({"cargo:involvedParties": [{"cargo:partyRole": "XXX", "cargo:partyDetails": {"cargo:name": "A"}}]}, "ParticipantIdentifier"),
    ({"acp:recordingOrganization": "forwarder"}, "absolute"),
    ({"@type": "cargo:Piece"}, "cargo:Shipment"),
])
def test_invalid_reports_are_rejected(bad, message):
    doc = report_doc()
    doc.update(bad)
    with pytest.raises(rules.EventValidationError, match=message):
        rr.parse_report(doc)


# ------------------------------- weight / unit ----------------------------- #

def test_same_weight_in_different_units_is_not_a_conflict():
    a = parsed(org=FFW, weight=(1250, "KGM"))
    b = parsed(org=AIR, weight=(2755.8, "LBR"))  # 1250.0 kg
    assert rr.compare_reports(b, a) == []


def test_weight_within_two_percent_is_accepted():
    a = parsed(org=FFW, weight=(1000, "KGM"))
    b = parsed(org=AIR, weight=(1019, "KGM"))
    assert rr.compare_reports(b, a) == []


def test_small_absolute_weight_difference_is_accepted():
    a = parsed(org=FFW, weight=(10, "KGM"))
    b = parsed(org=AIR, weight=(10.4, "KGM"))  # 4% but only 0.4 kg
    assert rr.compare_reports(b, a) == []


def test_weight_mismatch_medium_and_high():
    base = parsed(org=FFW, weight=(1000, "KGM"))
    medium = rr.compare_reports(parsed(org=AIR, weight=(1050, "KGM")), base)
    high = rr.compare_reports(parsed(org=AIR, weight=(1200, "KGM")), base)
    assert [(h.conflict_type, h.severity) for h in medium] == [("WEIGHT_MISMATCH", "MEDIUM")]
    assert [(h.conflict_type, h.severity) for h in high] == [("WEIGHT_MISMATCH", "HIGH")]
    assert medium[0].value_a == "1050 KGM" and medium[0].value_b == "1000 KGM"


def test_same_number_different_unit_is_unit_mismatch():
    a = parsed(org=FFW, weight=(1250, "KGM"))
    b = parsed(org=GHA, weight=(1250, "LBR"))
    (hit,) = rr.compare_reports(b, a)
    assert hit.conflict_type == "UNIT_MISMATCH"
    assert hit.severity == "HIGH"
    assert "unit entry error" in hit.description


def test_volume_unit_mismatch_and_volume_mismatch():
    a = parsed(org=FFW, volume=(8, "MTQ"))
    assert types(rr.compare_reports(parsed(org=AIR, volume=(8, "FTQ")), a)) == ["UNIT_MISMATCH"]
    assert types(rr.compare_reports(parsed(org=AIR, volume=(9, "MTQ")), a)) == ["VOLUME_MISMATCH"]
    assert rr.compare_reports(parsed(org=AIR, volume=(282.5, "FTQ")), a) == []  # 8.0 m3


# ------------------------------- other attributes -------------------------- #

def test_piece_count_mismatch():
    hits = rr.compare_reports(parsed(org=AIR, pieces=9), parsed(org=FFW, pieces=10))
    assert [(h.conflict_type, h.value_a, h.value_b) for h in hits] == [("PIECE_COUNT_MISMATCH", 9, 10)]


def test_route_mismatch_per_end():
    hits = rr.compare_reports(
        parsed(org=AIR, origin="BOM", destination="LHR"),
        parsed(org=FFW, origin="BOM", destination="FRA"),
    )
    assert [(h.conflict_type, h.field) for h in hits] == [("ROUTE_MISMATCH", "destination")]


def test_waybill_formatting_is_ignored_but_digits_are_not():
    a = parsed(org=FFW, waybill="176-12345675")
    assert rr.compare_reports(parsed(org=AIR, waybill="176 1234 5675"), a) == []
    assert types(rr.compare_reports(parsed(org=AIR, waybill="176-12345676"), a)) == ["REFERENCE_MISMATCH"]


def test_special_handling_order_is_ignored():
    a = parsed(org=FFW, shc=["PER", "COL"])
    assert rr.compare_reports(parsed(org=AIR, shc=["COL", "PER"]), a) == []


def test_missing_dangerous_goods_code_is_high():
    hits = rr.compare_reports(parsed(org=AIR, shc=["PER"]), parsed(org=FFW, shc=["PER", "RCL"]))
    assert [(h.conflict_type, h.severity) for h in hits] == [("SPECIAL_HANDLING_MISMATCH", "HIGH")]


def test_non_dangerous_handling_difference_is_medium():
    hits = rr.compare_reports(parsed(org=AIR, shc=["PER", "COL"]), parsed(org=FFW, shc=["PER"]))
    assert [(h.conflict_type, h.severity) for h in hits] == [("SPECIAL_HANDLING_MISMATCH", "MEDIUM")]


@pytest.mark.parametrize("a, b", [
    ("Tata Exports Pvt. Ltd.", "TATA EXPORTS PRIVATE LIMITED"),
    ("M/s Global Intl Logistics LLP", "Global International Logistics"),
    ("Sharma & Sons Co.", "Sharma and Sons"),
    ("Müller GmbH", "Muller"),
    ("Desert Rose Trading L.L.C.", "Desert Rose Trading LLC"),
])
def test_party_name_variants_match(a, b):
    hits = rr.compare_reports(parsed(org=AIR, shipper=(a, None)), parsed(org=FFW, shipper=(b, None)))
    assert hits == []


def test_different_party_name_is_medium():
    hits = rr.compare_reports(
        parsed(org=AIR, consignee=("Tata Impex Ltd", None)),
        parsed(org=FFW, consignee=("Tata Exports Ltd", None)),
    )
    assert [(h.conflict_type, h.severity, h.field) for h in hits] == [
        ("PARTY_MISMATCH", "MEDIUM", "party:CNE")
    ]


def test_party_ids_decide_when_both_present():
    same_id = rr.compare_reports(
        parsed(org=AIR, consignee=("Totally Different Name", "https://c.example/1")),
        parsed(org=FFW, consignee=("Muller", "https://c.example/1")),
    )
    other_id = rr.compare_reports(
        parsed(org=AIR, consignee=("Muller", "https://c.example/2")),
        parsed(org=FFW, consignee=("Muller", "https://c.example/1")),
    )
    assert same_id == []
    assert [(h.conflict_type, h.severity) for h in other_id] == [("PARTY_MISMATCH", "HIGH")]


def test_same_party_or_other_shipment_is_never_compared():
    a = parsed(org=FFW, weight=(100, "KGM"))
    assert rr.compare_reports(parsed(org=FFW, weight=(500, "KGM")), a) == []
    other = parsed(org=AIR, weight=(500, "KGM"), shipment="https://example.org/other")
    assert rr.compare_reports(other, a) == []


def test_fields_missing_on_one_side_are_not_compared():
    assert rr.compare_reports(parsed(org=AIR, pieces=4), parsed(org=FFW, weight=(1, "KGM"))) == []


def test_several_mismatches_in_one_pair():
    hits = rr.compare_reports(
        parsed(org=AIR, weight=(1500, "KGM"), pieces=8, destination="LHR"),
        parsed(org=FFW, weight=(1000, "KGM"), pieces=10, destination="FRA"),
    )
    assert types(hits) == ["PIECE_COUNT_MISMATCH", "ROUTE_MISMATCH", "WEIGHT_MISMATCH"]


# ------------------------------- location (R4) ----------------------------- #

def event(code, location, time_type="ACTUAL"):
    from datetime import datetime, timezone
    return rules.ParsedEvent(
        event_id="https://e.example/1", shipment_id=SHIPMENT, status_code=code,
        milestone_rank=rules.STATUS_CODE_TO_RANK.get(code),
        event_date=datetime(2026, 10, 27, 10, tzinfo=timezone.utc),
        time_type=time_type, recording_org=AIR, partial=False, location=location,
    )


@pytest.mark.parametrize("code, location, expected", [
    ("DEP", "BOM", None), ("DEP", "DEL", "LOCATION_MISMATCH"),
    ("RCS", "DEL", "LOCATION_MISMATCH"), ("ARR", "FRA", None),
    ("DLV", "MUC", "LOCATION_MISMATCH"), ("BKD", "XXX", None),
    ("DEP", None, None),
])
def test_event_location_rule(code, location, expected):
    report = parsed(org=FFW, origin="BOM", destination="FRA")
    hit = rr.compare_event_location(event(code, location), report)
    assert (hit.conflict_type if hit else None) == expected


def test_planned_events_are_not_location_checked():
    report = parsed(org=FFW, origin="BOM")
    assert rr.compare_event_location(event("DEP", "DEL", "PLANNED"), report) is None


# ------------------------------- API --------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "reports.db")
    with TestClient(api.app) as test_client:
        assert test_client.post("/logistics-objects", json={
            "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
            "@id": SHIPMENT, "@type": "cargo:Shipment",
        }).status_code == 201
        yield test_client


def post_report(client, doc, shipment=SHIPMENT):
    return client.post(f"/logistics-objects/{shipment}/party-reports", json=doc)


def event_doc(code, time, location, org=AIR, event_id=None):
    _n["i"] += 1
    return {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": event_id or f"https://events.example/e-{_n['i']}",
        "@type": "cargo:LogisticsEvent",
        "cargo:eventFor": {"@id": SHIPMENT},
        "cargo:eventCode": {"@id": rules.STATUS_CODE_BASE + code},
        "cargo:eventDate": f"2026-10-27T{time}:00Z",
        "cargo:eventLocation": {"cargo:locationCodes": [{"cargo:code": location}]},
        "cargo:recordingOrganization": {"@id": org},
        "acp:reporterRole": "AIR",
    }


def test_api_detects_weight_conflict_and_marks_both_reports(client):
    first = report_doc(org=FFW, weight=(1000, "KGM"))
    second = report_doc(org=AIR, role="AIR", weight=(1150, "KGM"))
    assert post_report(client, first).json()["conflictDetected"] is False

    response = post_report(client, second)

    assert response.status_code == 201
    body = response.json()
    assert body["ingestionStatus"] == "ACCEPTED_DISPUTED"
    (conflict,) = body["conflicts"]
    assert conflict["conflictType"] == "WEIGHT_MISMATCH"
    assert conflict["relatedRecordId"] == first["@id"]

    detail = client.get(f"/conflicts/{conflict['conflictId']}").json()
    assert detail["details"]["new"]["value"] == "1150 KGM"
    assert detail["details"]["stored"]["value"] == "1000 KGM"
    assert {r["record_id"] for r in detail["records"]} == {first["@id"], second["@id"]}
    assert all(r["kind"] == "report" for r in detail["records"])
    reports = client.get(f"/logistics-objects/{SHIPMENT}/party-reports").json()["items"]
    assert all(r["disputed"] for r in reports)


def test_api_three_parties_one_wrong_gives_two_conflicts(client):
    post_report(client, report_doc(org=FFW, pieces=10))
    post_report(client, report_doc(org=GHA, role="GHA", pieces=10))
    response = post_report(client, report_doc(org=AIR, role="AIR", pieces=12))
    assert [c["conflictType"] for c in response.json()["conflicts"]] == [
        "PIECE_COUNT_MISMATCH", "PIECE_COUNT_MISMATCH"
    ]


def test_api_newer_report_from_same_party_supersedes(client):
    post_report(client, report_doc(org=FFW, weight=(1000, "KGM")))
    post_report(client, report_doc(org=AIR, role="AIR", weight=(1000, "KGM")))
    corrected = post_report(client, report_doc(org=FFW, weight=(1300, "KGM")))
    body = corrected.json()
    assert body["supersededPrevious"] is True
    assert [c["conflictType"] for c in body["conflicts"]] == ["WEIGHT_MISMATCH"]
    current = client.get(
        f"/logistics-objects/{SHIPMENT}/party-reports", params={"include_superseded": False}
    ).json()
    assert current["count"] == 2


def test_api_location_conflict_is_found_whichever_arrives_first(client):
    # Event first, then the report that contradicts it.
    post_event = client.post(
        f"/logistics-objects/{SHIPMENT}/logistics-events", json=event_doc("DEP", "10:00", "DEL"),
    )
    assert post_event.json()["conflictDetected"] is False
    report = post_report(client, report_doc(org=FFW, origin="BOM", destination="FRA"))
    assert [c["conflictType"] for c in report.json()["conflicts"]] == ["LOCATION_MISMATCH"]

    # Report already stored, then a contradicting event.
    late = client.post(
        f"/logistics-objects/{SHIPMENT}/logistics-events", json=event_doc("ARR", "18:00", "LHR"),
    )
    assert [c["conflictType"] for c in late.json()["conflicts"]] == ["LOCATION_MISMATCH"]


def test_api_rejects_invalid_report_without_conflict(client):
    doc = report_doc(org=FFW)
    doc["cargo:totalGrossWeight"] = {"cargo:numericalValue": 10}
    response = post_report(client, doc)
    assert response.status_code == 422
    assert "missing cargo:unit" in response.json()["detail"]
    assert client.get("/conflicts").json()["count"] == 0
    assert client.get("/metrics").json()["rejectedByFilter"] == 1


def test_api_report_for_must_match_path(client):
    doc = report_doc(org=FFW, shipment="https://example.org/logistics-objects/other")
    assert post_report(client, doc).status_code == 422


def test_api_duplicate_report_id(client):
    doc = report_doc(org=FFW, pieces=3)
    post_report(client, doc)
    replay = post_report(client, doc)
    assert replay.status_code == 409
    assert replay.json()["severity"] == "LOW"


def test_api_conflicts_can_be_filtered(client):
    post_report(client, report_doc(org=FFW, pieces=10, weight=(100, "KGM")))
    post_report(client, report_doc(org=AIR, role="AIR", pieces=11, weight=(100, "LBR")))
    by_type = client.get("/conflicts", params={"type": "unit_mismatch"}).json()
    by_severity = client.get("/conflicts", params={"severity": "MEDIUM"}).json()
    assert [c["conflict_type"] for c in by_type["items"]] == ["UNIT_MISMATCH"]
    assert [c["conflict_type"] for c in by_severity["items"]] == ["PIECE_COUNT_MISMATCH"]
    assert by_type["items"][0]["label"] == "Unit mismatch"


def test_api_resolution_records_accepted_side(client):
    first = report_doc(org=FFW, pieces=10)
    post_report(client, first)
    conflict = post_report(client, report_doc(org=AIR, role="AIR", pieces=9)).json()["conflicts"][0]

    bad = client.patch(f"/conflicts/{conflict['conflictId']}/resolve",
                       json={"acceptedRecord": "https://nope.example/x"})
    ok = client.patch(f"/conflicts/{conflict['conflictId']}/resolve",
                      json={"acceptedRecord": first["@id"], "note": "SLAC on AWB", "resolvedBy": "ops"})

    assert bad.status_code == 422
    assert ok.status_code == 200
    stored = client.get(f"/conflicts/{conflict['conflictId']}").json()
    assert stored["accepted_record"] == first["@id"]
    assert stored["resolution_note"] == "SLAC on AWB"
    assert stored["status"] == "RESOLVED"


def test_dashboard_endpoints(client):
    post_report(client, report_doc(org=FFW, pieces=10))
    post_report(client, report_doc(org=AIR, role="AIR", pieces=9))
    summary = client.get("/dashboard/summary").json()
    assert summary["openConflicts"] == 1
    assert summary["byType"]["PIECE_COUNT_MISMATCH"]["open"] == 1
    assert summary["byCategory"] == {"ATTRIBUTE": 1}
    shipments = client.get("/dashboard/shipments").json()["items"]
    assert shipments[0]["open"] == 1 and shipments[0]["reports"] == 2
    detail = client.get("/dashboard/shipment", params={"id": SHIPMENT}).json()
    assert len(detail["reports"]) == 2 and len(detail["conflicts"]) == 1
    assert client.get("/catalog").json()["conflictTypes"]["UNIT_MISMATCH"]["category"] == "ATTRIBUTE"
    live = client.get("/metrics/live").json()
    assert live["ingestion"]["records"] == 3
    assert client.get("/dashboard").status_code == 200
