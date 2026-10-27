"""AlgorandBackend against a fake algod: real algokit-utils / algosdk code
builds, signs, submits and confirms the transactions; the fake node decodes
the ABI calls and stores boxes like the contracts do."""

import json
from pathlib import Path

import pytest

pytest.importorskip("algokit_utils")

from algosdk import account, mnemonic  # noqa: E402

from api import chain  # noqa: E402
from tests.fake_algod import FakeAlgod  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parent.parent / "smart_contracts" / "artifacts"
CLIENTS = [
    ARTIFACTS / "conflict_registry" / "conflict_registry_client.py",
    ARTIFACTS / "evidence_registry" / "evidence_registry_client.py",
    ARTIFACTS / "hello_world" / "air_cargo_provenance_client.py",
]
pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in CLIENTS),
    reason="typed clients not generated yet: run `algokit project run build`",
)

APP_IDS = {"conflict_registry": 1001, "evidence_registry": 1002, "hello_world": 1003}


@pytest.fixture
def backend(monkeypatch):
    fake = FakeAlgod().start()
    private_key, _ = account.generate_account()
    monkeypatch.setenv("ALGOD_SERVER", fake.url.rsplit(":", 1)[0])
    monkeypatch.setenv("ALGOD_PORT", fake.url.rsplit(":", 1)[1])
    monkeypatch.setenv("ALGOD_TOKEN", "a" * 64)
    monkeypatch.setenv("DEPLOYER_MNEMONIC", mnemonic.from_private_key(private_key))
    monkeypatch.delenv("INDEXER_SERVER", raising=False)
    backend = chain.AlgorandBackend(app_ids=APP_IDS)
    backend.fake = fake
    yield backend
    fake.stop()


def test_report_conflict_goes_through_algokit_and_is_read_back(backend):
    digest = chain.sha256_bytes(b"conflict")
    call = chain.conflict_call("conf-1", "ship-1", "rec-a", "rec-b", "WEIGHT_MISMATCH", digest)

    result = backend.call(call)

    sent = backend.fake.calls[-1]
    assert sent["method"] == "report_conflict"
    assert sent["app_id"] == APP_IDS["conflict_registry"]
    assert sent["args"][4] == "WEIGHT_MISMATCH" and sent["args"][5] == digest
    assert sent["boxes"] == set(call.boxes)
    assert sent["sender"] == backend.sender.address
    assert result.confirmed_round > 0 and result.fee_microalgos >= 1000
    assert result.confirm_ms >= result.submit_ms >= 0
    assert backend.read_box("conflict", call.main_box) == call.expected


def test_resolve_and_double_resolve(backend):
    digest = chain.sha256_bytes(b"c2")
    backend.call(chain.conflict_call("conf-2", "s", "a", "b", "UNIT_MISMATCH", digest))
    resolve = chain.resolve_call("conf-2", "s", "a", "b", "UNIT_MISMATCH", digest, "accepted:a", "ops")
    backend.call(resolve)
    assert backend.read_box("conflict", resolve.main_box) == resolve.expected
    with pytest.raises(Exception, match="not open"):
        backend.call(resolve)


def test_event_and_evidence_calls(backend):
    event = chain.event_call("evt-1", "ship-1", "DEP", 1761559200, "https://airline.example",
                             "BOM", chain.sha256_bytes(b"payload"))
    evidence = chain.evidence_call("ev-1", "ship-1", chain.sha256_bytes(b"file"), "https://ffw.example")
    backend.call(event)
    backend.call(evidence)
    assert backend.fake.calls[-2]["args"][3] == 1761559200  # uint64 encoded correctly
    assert backend.read_box("event", event.main_box) == event.expected
    assert backend.read_box("evidence", evidence.main_box) == evidence.expected
    assert backend.fake.calls[-1]["app_id"] == APP_IDS["evidence_registry"]


def test_missing_box_returns_none(backend):
    assert backend.read_box("conflict", b"cfl" + b"0" * 32) is None


def test_app_ids_come_from_deploy_file_and_env(tmp_path, monkeypatch):
    path = tmp_path / "deployed_apps.json"
    path.write_text(json.dumps({"localnet": {"conflict_registry": 5, "evidence_registry": 6}}))
    monkeypatch.setattr(chain, "DEPLOYED_APPS_FILE", path)
    monkeypatch.setenv("HELLO_WORLD_APP_ID", "7")
    monkeypatch.delenv("CHAIN_NETWORK", raising=False)
    assert chain.load_app_ids() == {"conflict_registry": 5, "evidence_registry": 6, "hello_world": 7}


def test_missing_app_id_gives_clear_error(backend, monkeypatch):
    with pytest.raises(RuntimeError, match="algokit project deploy"):
        chain.AlgorandBackend(app_ids={"conflict_registry": 1})


def test_api_in_algorand_mode_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import main as api

    fake = FakeAlgod().start()
    private_key, _ = account.generate_account()
    monkeypatch.setenv("CHAIN_MODE", "algorand")
    monkeypatch.setenv("ALGOD_SERVER", fake.url.rsplit(":", 1)[0])
    monkeypatch.setenv("ALGOD_PORT", fake.url.rsplit(":", 1)[1])
    monkeypatch.setenv("ALGOD_TOKEN", "a" * 64)
    monkeypatch.setenv("DEPLOYER_MNEMONIC", mnemonic.from_private_key(private_key))
    for folder, app_id in APP_IDS.items():
        monkeypatch.setenv(f"{folder.upper()}_APP_ID", str(app_id))
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "algo.db")
    shipment = "https://example.org/logistics-objects/algo-1"
    try:
        with TestClient(api.app) as client:
            client.post("/logistics-objects", json={
                "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
                "@id": shipment, "@type": "cargo:Shipment"})
            for n, (org, kg) in enumerate((("https://f.example/o", 100), ("https://a.example/o", 150))):
                last = client.post(f"/logistics-objects/{shipment}/party-reports", json={
                    "@context": {"cargo": "https://onerecord.iata.org/ns/cargo#"},
                    "@id": f"https://r.example/{n}", "@type": "cargo:Shipment",
                    "acp:reportFor": shipment, "acp:recordingOrganization": org,
                    "cargo:totalGrossWeight": {"cargo:numericalValue": kg, "cargo:unit": "KGM"},
                })
            conflict_id = last.json()["conflicts"][0]["conflictId"]
            assert api.CHAIN.drain(20)
            detail = client.get(f"/conflicts/{conflict_id}").json()
            assert detail["chain_status"] == "CONFIRMED", detail["chain_transactions"]
            assert client.get(f"/conflicts/{conflict_id}/verify").json()["onChainMatch"] is True
            status = client.get("/chain/status").json()
            assert status["mode"] == "algorand"
            assert status["app_ids"]["conflict_registry"] == APP_IDS["conflict_registry"]
            live = client.get("/metrics/live").json()["chain"]["algorand"]
            assert live["confirmed"] == 1 and live["confirmMs"]["count"] == 1
    finally:
        fake.stop()


def test_benchmark_runs_against_algorand_backend(backend, monkeypatch, tmp_path):
    from evaluation import benchmark_chain as bench

    monkeypatch.setattr(bench.chain, "AlgorandBackend", lambda: backend)
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path)
    monkeypatch.setenv("CHAIN_MODE", "algorand")
    assert bench.main(["--n", "3", "--pipelined", "4", "--payload-sizes", "16,128"]) == 0
    result = json.loads((tmp_path / "chain_benchmark_latest.json").read_text())
    assert result["mode"] == "algorand"
    assert all(row["failures"] == 0 for row in result["sequential"])
    assert result["pipelined"][0]["confirmed"] == 4
    sizes = [row["box_value_bytes"] for row in result["payload"]]
    assert sizes[1] > sizes[0]  # larger reason -> larger box -> higher MBR
    assert result["network"]["genesis_id"] == "fakenet-v1"
