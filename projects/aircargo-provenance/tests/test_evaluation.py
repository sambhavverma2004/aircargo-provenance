"""The simulator produces consistent labels and the evaluation pipeline runs."""

from evaluation import evaluate, simulator


def test_simulator_is_deterministic_and_labels_are_consistent():
    a = simulator.generate(20, seed=3)
    b = simulator.generate(20, seed=3)
    assert [r["body"] for r in a["records"]] == [r["body"] for r in b["records"]]

    ids = {r["record_id"] for r in a["records"]}
    for injection in a["injections"]:
        assert set(injection["corrupted_records"]) <= ids
        assert injection["type"] in injection["acceptable_types"]
    first_seen = {}
    for record in a["records"]:
        first_seen.setdefault(record["shipment_id"], record["kind"])
    assert set(first_seen.values()) == {"shipment"}  # shipment always arrives first


def test_clean_dataset_has_no_injections():
    data = simulator.generate(10, seed=1, conflict_rate=0.0)
    assert data["injections"] == []
    assert all(v["clean"] for v in data["shipments"].values())


def test_evaluation_runs_in_process():
    result = evaluate.evaluate_once(25, 5, lambda: evaluate.InProcessTarget("off"))
    scores = result["scores"]
    assert scores["filter"]["valid_wrongly_rejected"] == 0
    assert scores["filter"]["rejection_rate"] in (None, 1.0)
    assert scores["classification_accuracy"] >= 0.9
    assert result["unexpected_status"] == []
    assert result["performance"]["throughput_records_per_second"] > 0
    assert "WEIGHT_MISMATCH" in scores["per_type"]
    assert "Per conflict type" in evaluate.markdown({**result, "generated_at": "now"})
