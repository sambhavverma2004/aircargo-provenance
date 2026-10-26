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
