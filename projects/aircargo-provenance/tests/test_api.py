import pytest
from fastapi.testclient import TestClient

from api import main as api


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Use an isolated database for every test.
    test_db = tmp_path / "test_aircargo.db"
    monkeypatch.setattr(api, "DB_PATH", test_db)

    # TestClient runs FastAPI's lifespan, which creates the tables.
    with TestClient(api.app) as test_client:
        yield test_client


def sample_shipment(reference="shipment-test-001"):
    return {
        "@context": {
            "cargo": "https://onerecord.iata.org/ns/cargo#"
        },
        "@id": f"https://example.org/logistics-objects/{reference}",
        "@type": "cargo:Shipment",
        "testReference": reference,
    }


def test_health_returns_success(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_home_lists_api_endpoints(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "/logistics-objects" in response.json()["endpoints"]


def test_create_shipment(client):
    response = client.post(
        "/logistics-objects",
        json=sample_shipment(),
    )
    assert response.status_code == 201
    data = response.json()
    assert data["ingestionStatus"] == "ACCEPTED"
    assert data["conflictDetected"] is False
    assert data["blockchainStatus"] == "NOT_SUBMITTED"


def test_duplicate_shipment_opens_conflict(client):
    payload = sample_shipment()
    first = client.post("/logistics-objects", json=payload)
    second = client.post("/logistics-objects", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    conflict = second.json()
    assert conflict["conflictType"] == "DUPLICATE_IDENTIFIER"
    assert conflict["status"] == "OPEN"
    assert conflict["conflictId"]

def test_shipment_can_be_listed(client):
    payload = sample_shipment()
    created = client.post("/logistics-objects", json=payload)
    assert created.status_code == 201

    response = client.get("/logistics-objects")
    assert response.status_code == 200
    assert payload["@id"] in response.text


def test_get_conflict_by_id(client):
    shipment = sample_shipment()
    client.post("/logistics-objects", json=shipment)
    duplicate = client.post("/logistics-objects", json=shipment)
    conflict_id = duplicate.json()["conflictId"]

    response = client.get(f"/conflicts/{conflict_id}")

    assert response.status_code == 200
    assert response.json()["conflict_id"] == conflict_id
    assert response.json()["conflict_type"] == "DUPLICATE_IDENTIFIER"
    assert response.json()["status"] == "OPEN"


def test_get_missing_conflict_returns_404(client):
    response = client.get("/conflicts/nonexistent-conflict-id")

    assert response.status_code == 404
    assert response.json()["detail"] == "Conflict not found"




def test_resolve_conflict(client):
    shipment = sample_shipment()
    client.post("/logistics-objects", json=shipment)
    duplicate = client.post("/logistics-objects", json=shipment)
    conflict_id = duplicate.json()["conflictId"]

    response = client.patch(f"/conflicts/{conflict_id}/resolve")

    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"

    details = client.get(f"/conflicts/{conflict_id}")
    assert details.status_code == 200
    assert details.json()["status"] == "RESOLVED"

    # Verify that the resolution was recorded in the audit table.
    with api.connect_db() as conn:
        rows = conn.execute(
            """
            SELECT conflict_id, action, previous_status,
                   new_status, recorded_at
            FROM conflict_audit
            WHERE conflict_id = ?
            """,
            (conflict_id,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["conflict_id"] == conflict_id
    assert rows[0]["action"] == "CONFLICT_RESOLVED"
    assert rows[0]["previous_status"] == "OPEN"
    assert rows[0]["new_status"] == "RESOLVED"
    assert rows[0]["recorded_at"]


def test_resolving_conflict_twice_is_safe(client):
    shipment = sample_shipment()
    client.post("/logistics-objects", json=shipment)
    duplicate = client.post("/logistics-objects", json=shipment)
    conflict_id = duplicate.json()["conflictId"]

    first = client.patch(f"/conflicts/{conflict_id}/resolve")
    second = client.patch(f"/conflicts/{conflict_id}/resolve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "RESOLVED"


def test_resolve_missing_conflict_returns_404(client):
    response = client.patch("/conflicts/nonexistent-id/resolve")

    assert response.status_code == 404




def test_conflicts_can_be_listed(client):
    payload = sample_shipment()
    client.post("/logistics-objects", json=payload)
    duplicate = client.post("/logistics-objects", json=payload)
    assert duplicate.status_code == 409

    response = client.get("/conflicts")
    assert response.status_code == 200
    assert "DUPLICATE_IDENTIFIER" in response.text


def test_open_conflicts_can_be_filtered(client):
    payload = sample_shipment()
    client.post("/logistics-objects", json=payload)
    client.post("/logistics-objects", json=payload)

    response = client.get("/conflicts", params={"status": "OPEN"})
    assert response.status_code == 200
    assert "DUPLICATE_IDENTIFIER" in response.text


def test_metrics_endpoint_returns_success(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.json()


def test_rejects_missing_jsonld_context(client):
    payload = {
        "@id": "https://example.org/logistics-objects/missing-context",
        "@type": "cargo:Shipment",
    }
    response = client.post("/logistics-objects", json=payload)
    assert response.status_code == 422


def test_rejects_invalid_object_id(client):
    payload = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": "not-a-valid-absolute-url",
        "@type": "cargo:Shipment",
    }
    response = client.post("/logistics-objects", json=payload)
    assert response.status_code == 422


def test_rejects_missing_object_type(client):
    payload = {
        "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
        "@id": "https://example.org/logistics-objects/missing-type",
    }
    response = client.post("/logistics-objects", json=payload)
    assert response.status_code == 422


def test_database_records_survive_reconnection(client):
    payload = sample_shipment("persistence-check")
    created = client.post("/logistics-objects", json=payload)
    assert created.status_code == 201

    # Close the current database connection and open a fresh one
    # through the application's existing connection function.
    with api.connect_db() as conn:
        row = conn.execute(
            "SELECT object_id FROM logistics_objects WHERE object_id = ?",
            (payload["@id"],),
        ).fetchone()

    assert row is not None
    assert row["object_id"] == payload["@id"]


def test_rejects_malformed_json(client):
    response = client.post(
        "/logistics-objects",
        content='{"broken":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON request body"


def test_rejects_empty_json_object(client):
    response = client.post("/logistics-objects", json={})
    assert response.status_code == 422


def test_conflict_record_survives_reconnection(client):
    import api.main as api

    shipment = {
        "@context": {"@vocab": "https://example.org/aircargo#"},
        "@id": "https://example.org/shipments/persistence-test",
        "@type": "Shipment",
    }

    assert client.post("/logistics-objects", json=shipment).status_code == 201
    duplicate = client.post("/logistics-objects", json=shipment)
    assert duplicate.status_code == 409

    conflict_id = duplicate.json()["conflictId"]

    with api.connect_db() as conn:
        row = conn.execute(
            "SELECT conflict_id, status FROM conflicts WHERE conflict_id = ?",
            (conflict_id,),
        ).fetchone()

    assert row is not None
    assert row["conflict_id"] == conflict_id
    assert row["status"] == "OPEN"
def test_database_stores_correct_object_type(client):
    import api.main as api

    shipment = {
        "@context": {"@vocab": "https://example.org/aircargo#"},
        "@id": "https://example.org/shipments/type-test",
        "@type": "Shipment",
    }

    response = client.post("/logistics-objects", json=shipment)
    assert response.status_code == 201

    with api.connect_db() as conn:
        row = conn.execute(
            "SELECT object_type FROM logistics_objects WHERE object_id = ?",
            (shipment["@id"],),
        ).fetchone()

    assert row is not None
    assert row["object_type"] == '"Shipment"'
def test_get_logistics_object_by_id(client):
    payload = sample_shipment("get-by-id-test")
    created = client.post("/logistics-objects", json=payload)
    assert created.status_code == 201

    response = client.get(
        "/logistics-objects/"
        "https://example.org/logistics-objects/get-by-id-test"
    )

    assert response.status_code == 200
    assert response.json()["object_id"] == payload["@id"]
    assert response.json()["document"]["@type"] == payload["@type"]


def test_get_missing_logistics_object_returns_404(client):
    response = client.get(
        "/logistics-objects/"
        "https://example.org/logistics-objects/not-found"
    )

    assert response.status_code == 404

def test_retrieval_uses_jsonld_type_for_legacy_records(client):
    import json
    from api import main as api

    shipment = sample_shipment("legacy-type-test")
    assert client.post("/logistics-objects", json=shipment).status_code == 201

    # Simulate a record written by the older, buggy implementation.
    with api.connect_db() as conn:
        conn.execute(
            "UPDATE logistics_objects SET object_type = ? WHERE object_id = ?",
            (json.dumps(shipment), shipment["@id"]),
        )

    item_url = "/logistics-objects/" + shipment["@id"]
    single = client.get(item_url)
    listing = client.get("/logistics-objects")

    assert single.status_code == 200
    assert single.json()["object_type"] == shipment["@type"]

    listed_item = next(
        item for item in listing.json()["items"]
        if item["object_id"] == shipment["@id"]
    )
    assert listed_item["object_type"] == shipment["@type"]
