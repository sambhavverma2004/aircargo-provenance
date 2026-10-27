"""Blockchain anchoring through the API, in simulated mode.

The real Algorand path is covered by tests/test_algorand_backend.py against
a fake algod HTTP server, and by evaluation/benchmark_chain.py on LocalNet.
"""

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api import chain
from api import main as api
from api import rules


SHIPMENT = "https://example.org/logistics-objects/shipment-ch-001"
FFW = "https://forwarder.example/org"
AIR = "https://airline.example/org"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAIN_MODE", "simulated")
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "chain.db")
    monkeypatch.setattr(api, "EVIDENCE_DIR", tmp_path / "evidence")
    with TestClient(api.app) as test_client:
        test_client.post("/logistics-objects", json={
            "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
            "@id": SHIPMENT, "@type": "cargo:Shipment",
        })
        yield test_client


def drain():
    assert api.CHAIN.drain(10), "chain worker did not finish"


def report(org, pieces, n):
    return {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": f"https://reports.example/{n}",
        "@type": "cargo:Shipment",
        "acp:reportFor": {"@id": SHIPMENT},
        "acp:recordingOrganization": {"@id": org},
        "acp:reporterRole": "FFW" if org == FFW else "AIR",
        "cargo:slac": pieces,
    }


def event(code, time, org, n):
    return {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": f"https://events.example/{n}",
        "@type": "cargo:LogisticsEvent",
        "cargo:eventFor": {"@id": SHIPMENT},
        "cargo:eventCode": {"@id": rules.STATUS_CODE_BASE + code},
        "cargo:eventDate": f"2026-10-27T{time}:00Z",
        "cargo:recordingOrganization": {"@id": org},
    }


def make_conflict(client):
    client.post(f"/logistics-objects/{SHIPMENT}/party-reports", json=report(FFW, 10, "a"))
    response = client.post(f"/logistics-objects/{SHIPMENT}/party-reports", json=report(AIR, 9, "b"))
    return response.json()["conflicts"][0]["conflictId"]


def test_conflict_is_anchored_and_verified(client):
    conflict_id = make_conflict(client)
    drain()

    detail = client.get(f"/conflicts/{conflict_id}").json()
    assert detail["chain_status"] == "CONFIRMED"
    assert detail["chain_tx_id"].startswith("SIM")
    (tx,) = [t for t in detail["chain_transactions"] if t["kind"] == "conflict"]
    assert tx["status"] == "CONFIRMED" and tx["verified"] == 1

    check = client.get(f"/conflicts/{conflict_id}/verify").json()
    assert check["databaseIntact"] is True
    assert check["onChainMatch"] is True
    assert check["mode"] == "simulated"


def test_tampering_with_a_record_is_detected(client):
    conflict_id = make_conflict(client)
    drain()
    with sqlite3.connect(api.DB_PATH) as conn:
        conn.execute(
            "UPDATE party_reports SET payload = ? WHERE report_id = ?",
            (json.dumps(report(AIR, 10, "b")), "https://reports.example/b"),
        )
    check = client.get(f"/conflicts/{conflict_id}/verify").json()
    assert check["databaseIntact"] is False
    assert check["onChainMatch"] is False


def test_events_are_anchored(client):
    response = client.post(
        f"/logistics-objects/{SHIPMENT}/logistics-events", json=event("DEP", "10:00", AIR, 1)
    )
    assert response.json()["blockchainStatus"] == "QUEUED"
    drain()
    items = client.get(f"/logistics-objects/{SHIPMENT}/logistics-events").json()["items"]
    assert items[0]["chain_status"] == "CONFIRMED"


def test_events_can_be_excluded_from_anchoring(client, monkeypatch):
    monkeypatch.setattr(api.CHAIN, "anchor_events", False)
    client.post(f"/logistics-objects/{SHIPMENT}/logistics-events", json=event("DEP", "10:00", AIR, 2))
    drain()
    items = client.get(f"/logistics-objects/{SHIPMENT}/logistics-events").json()["items"]
    assert items[0]["chain_status"] == "NOT_SUBMITTED"


def test_resolution_is_anchored_and_cannot_repeat_on_chain(client):
    conflict_id = make_conflict(client)
    drain()
    resolved = client.patch(f"/conflicts/{conflict_id}/resolve",
                            json={"acceptedRecord": "https://reports.example/a"})
    assert resolved.json()["blockchainStatus"] == "QUEUED"
    drain()
    detail = client.get(f"/conflicts/{conflict_id}").json()
    assert detail["chain_status"] == "RESOLVED_ON_CHAIN"
    kinds = [t["kind"] for t in detail["chain_transactions"]]
    assert kinds == ["conflict", "resolve"]

    # The contract rule "a conflict can be resolved once" holds on the chain.
    with pytest.raises(RuntimeError, match="not open"):
        api.CHAIN.backend.call(chain.resolve_call(
            conflict_id, SHIPMENT, "https://reports.example/b", "https://reports.example/a",
            "PIECE_COUNT_MISMATCH", bytes(32), "again", "x"))


def test_evidence_upload_hash_anchor_and_verify(client):
    conflict_id = make_conflict(client)
    content = b"%PDF-1.4 weighbridge slip 10 pieces"
    uploaded = client.post(f"/conflicts/{conflict_id}/evidence", json={
        "partyOrg": FFW, "filename": "slip.pdf", "contentType": "application/pdf",
        "contentBase64": base64.b64encode(content).decode(), "note": "Weighbridge slip",
    })
    assert uploaded.status_code == 201
    evidence_id = uploaded.json()["evidenceId"]
    drain()

    listed = client.get(f"/conflicts/{conflict_id}/evidence").json()["items"]
    assert listed[0]["chain_status"] == "CONFIRMED"
    assert listed[0]["party_role"] == "FFW"
    assert client.get(f"/evidence/{evidence_id}/file").content == content

    good = client.post(f"/evidence/{evidence_id}/verify",
                       json={"contentBase64": base64.b64encode(content).decode()}).json()
    bad = client.post(f"/evidence/{evidence_id}/verify",
                      json={"contentBase64": base64.b64encode(content + b"x").decode()}).json()
    assert good["matchesStored"] and good["matchesOnChain"]
    assert not bad["matchesStored"] and not bad["matchesOnChain"]


def test_evidence_only_from_parties_in_the_conflict(client):
    conflict_id = make_conflict(client)
    response = client.post(f"/conflicts/{conflict_id}/evidence", json={
        "partyOrg": "https://stranger.example/org", "filename": "x.txt",
        "contentBase64": base64.b64encode(b"x").decode(),
    })
    assert response.status_code == 422


def test_evidence_validation(client):
    conflict_id = make_conflict(client)
    for body, status in (
        ({"partyOrg": FFW, "filename": "x", "contentBase64": "!!!"}, 422),
        ({"partyOrg": FFW, "filename": "x"}, 422),
        ({"filename": "x", "contentBase64": "eA=="}, 422),
    ):
        assert client.post(f"/conflicts/{conflict_id}/evidence", json=body).status_code == status
    assert client.post("/conflicts/missing/evidence", json={
        "partyOrg": FFW, "filename": "x", "contentBase64": "eA=="}).status_code == 404


def test_failed_transactions_are_recorded_not_fatal(client, monkeypatch):
    def broken(call):
        raise RuntimeError("node unreachable")
    monkeypatch.setattr(api.CHAIN.backend, "call", broken)
    conflict_id = make_conflict(client)
    drain()
    detail = client.get(f"/conflicts/{conflict_id}").json()
    assert detail["chain_status"] == "FAILED"
    assert "node unreachable" in detail["chain_transactions"][0]["error"]
    assert client.get("/chain/status").json()["transactions"]["FAILED"] >= 1


def test_chain_metrics_and_transaction_list(client):
    make_conflict(client)
    drain()
    live = client.get("/metrics/live").json()
    assert live["chain"]["simulated"]["confirmed"] >= 1
    assert live["chain"]["simulated"]["totalBoxMbrMicroAlgos"] > 0
    txs = client.get("/chain/transactions").json()["items"]
    assert txs and txs[0]["mode"] == "simulated"
    assert client.get("/chain/status").json()["mode"] == "simulated"


def test_box_layout_matches_contract_and_cost_formula():
    call = chain.conflict_call("c", "s", "a", "b", "WEIGHT_MISMATCH", bytes(range(32)))
    assert call.expected.endswith(b"|OPEN|" + bytes(range(32)))
    assert chain.hash_in_conflict_box(call.expected) == bytes(range(32))
    assert len(call.main_box) == 3 + 32  # "cfl" + 32-char key, under the 64-byte limit
    assert chain.box_mbr(35, 100) == 2500 + 400 * 135
    roundtrip = chain.ChainCall.from_json(call.to_json())
    assert roundtrip == call
