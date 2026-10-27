"""Evaluate conflict detection and classification on labelled synthetic data.

Feeds a simulator dataset through the API (in-process by default, or a live
server with --url), then compares the conflicts the system raised with the
injected ground truth.

Metrics
  Detection / classification (per conflict type):
    precision  conflicts of type T explained by an injected fault for which T
               is an acceptable consequence  /  all conflicts of type T
    recall     injected faults of type T for which a conflict of type T
               involves the corrupted record  /  injected faults of type T
    F1, macro and micro averages, confusion matrix (injected vs predicted)
  Shipment level: false-alarm rate on clean shipments, detection rate on
    shipments with faults.
  Input filter: invalid records rejected (HTTP 422) and valid records
    accepted.
  Performance: per-record API latency (client side and server processing
    time), throughput, and how they change with workload size (--scaling),
    following the workload-scaling evaluation style of Sund et al. (2020).

Usage
  python -m evaluation.evaluate                      # 200 shipments, in-process
  python -m evaluation.evaluate --shipments 500 --seed 11
  python -m evaluation.evaluate --scaling 50,200,500,1000
  python -m evaluation.evaluate --url http://127.0.0.1:8000   # live server
"""

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"
sys.path.insert(0, str(PROJECT_ROOT))

from evaluation import simulator  # noqa: E402


# --------------------------------------------------------------------------- #
# Running records through the API
# --------------------------------------------------------------------------- #

class InProcessTarget:
    """Runs the FastAPI app in this process against a temporary database."""

    def __init__(self, chain_mode="off"):
        os.environ["CHAIN_MODE"] = chain_mode
        from fastapi.testclient import TestClient
        from api import main as api

        self.api = api
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        api.DB_PATH = Path(self.tmp.name) / "evaluation.db"
        api.EVIDENCE_DIR = Path(self.tmp.name) / "evidence"
        self.client = TestClient(api.app)
        self.client.__enter__()
        self.name = "in-process"

    def post(self, path, body):
        response = self.client.post(path, json=body)
        return response.status_code, response.json()

    def get(self, path, params=None):
        return self.client.get(path, params=params).json()

    def drain(self):
        self.api.CHAIN.drain(600)

    def close(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()


class HttpTarget:
    def __init__(self, url):
        import httpx
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60)
        self.name = url

    def post(self, path, body):
        response = self.client.post(path, json=body)
        return response.status_code, response.json()

    def get(self, path, params=None):
        return self.client.get(path, params=params).json()

    def drain(self):
        pass

    def close(self):
        self.client.close()


def api_path(record):
    if record["path"] == "/logistics-objects":
        return record["path"]
    suffix = record["path"].rsplit("/", 1)[1]
    return f"/logistics-objects/{quote(record['shipment_id'], safe='')}/{suffix}"


def run_records(target, dataset):
    timings = []
    responses = []
    started = time.perf_counter()
    for record in dataset["records"]:
        t0 = time.perf_counter()
        status, body = target.post(api_path(record), record["body"])
        elapsed = (time.perf_counter() - t0) * 1000
        timings.append((record["kind"], elapsed))
        responses.append((record, status, body))
    wall = time.perf_counter() - started
    target.drain()
    return responses, timings, wall


def fetch_conflicts(target):
    items, offset = [], 0
    while True:
        page = target.get("/conflicts", {"limit": 5000, "offset": offset})
        items.extend(page["items"])
        offset += page["count"]
        if page["count"] == 0 or offset >= page.get("total", offset):
            return items


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 3)


def stats(values):
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 3) if values else None,
        "p50": percentile(values, 50), "p95": percentile(values, 95),
        "p99": percentile(values, 99), "max": round(max(values), 3) if values else None,
    }


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def f1(precision, recall):
    if not precision or not recall:
        return 0.0 if precision is not None and recall is not None else None
    return round(2 * precision * recall / (precision + recall), 4)


def score(dataset, conflicts, responses):
    injections = {i["injection_id"]: i for i in dataset["injections"]}
    by_record = defaultdict(list)
    for injection in injections.values():
        for record_id in injection["corrupted_records"]:
            by_record[record_id].append(injection)

    types = list(simulator.INJECTION_TYPES)
    predicted = Counter()
    true_positive = Counter()
    detected_injections = set()
    confusion = {t: Counter() for t in types}
    false_positives = []

    for conflict in conflicts:
        ctype = conflict["conflict_type"]
        predicted[ctype] += 1
        involved = {conflict["object_id"], conflict.get("related_object_id")} - {None}
        touched = [inj for rid in involved for inj in by_record.get(rid, [])]
        explained = False
        for injection in touched:
            confusion[injection["type"]][ctype] += 1
            if ctype in injection["acceptable_types"]:
                explained = True
            if ctype == injection["type"]:
                detected_injections.add(injection["injection_id"])
        if explained:
            true_positive[ctype] += 1
        else:
            false_positives.append({
                "conflict_type": ctype, "severity": conflict["severity"],
                "shipment_id": conflict.get("shipment_id"),
                "description": conflict["description"],
                "touches_injection": [i["type"] for i in touched],
            })

    injected = Counter(i["type"] for i in injections.values())
    per_type = {}
    for t in types:
        p = ratio(true_positive[t], predicted[t])
        detected = sum(1 for i in injections.values()
                       if i["type"] == t and i["injection_id"] in detected_injections)
        r = ratio(detected, injected[t])
        per_type[t] = {
            "injected": injected[t], "detected": detected, "predicted": predicted[t],
            "true_positive": true_positive[t], "precision": p, "recall": r, "f1": f1(p, r),
        }
    for injection in injections.values():
        if injection["injection_id"] not in detected_injections:
            confusion[injection["type"]]["(missed)"] += 1

    valid = [t for t in types if per_type[t]["injected"] or per_type[t]["predicted"]]

    def macro(key):
        values = [per_type[t][key] for t in valid if per_type[t][key] is not None]
        return round(sum(values) / len(values), 4) if values else None

    micro_p = ratio(sum(true_positive.values()), sum(predicted.values()))
    micro_r = ratio(len(detected_injections), len(injections))

    # Shipment level
    labels = dataset["shipments"]
    flagged = {c.get("shipment_id") for c in conflicts}
    clean = [s for s, v in labels.items() if v["clean"]]
    faulty = [s for s, v in labels.items() if not v["clean"]]
    hard_names = [s for s, v in labels.items() if v.get("hard_name_variant")]

    # Input filter
    invalid = [(r, s) for r, s, _ in responses if r["expected_status"] == 422]
    valid_records = [(r, s) for r, s, _ in responses if r["expected_status"] != 422]
    rejected_by_reason = Counter(r["invalid_kind"] for r, s in invalid if s in (400, 422))

    return {
        "per_type": per_type,
        "macro": {"precision": macro("precision"), "recall": macro("recall"), "f1": macro("f1")},
        "micro": {"precision": micro_p, "recall": micro_r, "f1": f1(micro_p, micro_r)},
        "classification_accuracy": ratio(len(detected_injections), len(injections)),
        "confusion": {t: dict(confusion[t]) for t in types if confusion[t]},
        "shipments": {
            "total": len(labels), "clean": len(clean), "with_faults": len(faulty),
            "clean_with_false_alarm": sum(1 for s in clean if s in flagged),
            "false_alarm_rate": ratio(sum(1 for s in clean if s in flagged), len(clean)),
            "faulty_detected": sum(1 for s in faulty if s in flagged),
            "detection_rate": ratio(sum(1 for s in faulty if s in flagged), len(faulty)),
            "hard_name_variant_shipments": len(hard_names),
        },
        "filter": {
            "invalid_sent": len(invalid),
            "invalid_rejected": sum(1 for _, s in invalid if s in (400, 422)),
            "rejection_rate": ratio(sum(1 for _, s in invalid if s in (400, 422)), len(invalid)),
            "valid_sent": len(valid_records),
            "valid_wrongly_rejected": sum(1 for _, s in valid_records if s in (400, 422)),
            "rejected_by_reason": dict(rejected_by_reason),
        },
        "false_positive_examples": false_positives[:25],
        "false_positive_count": len(false_positives),
        "totals": {"conflicts": len(conflicts), "injections": len(injections),
                   "detected_injections": len(detected_injections)},
    }


def performance(timings, wall, live):
    by_kind = defaultdict(list)
    for kind, ms in timings:
        by_kind[kind].append(ms)
    return {
        "records": len(timings),
        "wall_seconds": round(wall, 3),
        "throughput_records_per_second": round(len(timings) / wall, 2) if wall else None,
        "client_latency_ms": stats([ms for _, ms in timings]),
        "client_latency_ms_by_kind": {k: stats(v) for k, v in by_kind.items()},
        "server_processing_ms": live["ingestion"]["processingMs"] if live else None,
        "server_processing_ms_by_kind": live["ingestion"]["byKind"] if live else None,
        "chain": live.get("chain") if live else None,
    }


def evaluate_once(shipments, seed, target_factory, conflict_rate=0.6):
    dataset = simulator.generate(shipments, seed, conflict_rate)
    target = target_factory()
    try:
        responses, timings, wall = run_records(target, dataset)
        conflicts = fetch_conflicts(target)
        live = target.get("/metrics/live")
        mismatched_status = [
            {"seq": r["seq"], "kind": r["kind"], "expected": r["expected_status"],
             "got": s, "detail": b.get("detail") if isinstance(b, dict) else None}
            for r, s, b in responses
            if (r["expected_status"] == 422) != (s in (400, 422))
        ]
        return {
            "dataset": dataset["meta"],
            "target": target.name,
            "scores": score(dataset, conflicts, responses),
            "performance": performance(timings, wall, live),
            "unexpected_status": mismatched_status[:20],
        }
    finally:
        target.close()


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def fmt(value, pct=True):
    if value is None:
        return "-"
    return f"{value * 100:.1f}%" if pct else f"{value}"


def markdown(result):
    s = result["scores"]
    p = result["performance"]
    lines = [
        "# Conflict detection evaluation",
        "",
        f"Generated {result['generated_at']} - dataset seed {result['dataset']['seed']}, "
        f"{result['dataset']['shipments']} shipments, {result['dataset']['records']} records, "
        f"{s['totals']['injections']} injected faults. Target: {result['target']}.",
        "",
        "Synthetic data from evaluation/simulator.py; results describe the prototype on "
        "simulated data, not industry-wide validation.",
        "",
        "## Per conflict type",
        "",
        "| Type | Injected | Detected | Predicted | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for t, v in s["per_type"].items():
        lines.append(f"| {t} | {v['injected']} | {v['detected']} | {v['predicted']} | "
                     f"{fmt(v['precision'])} | {fmt(v['recall'])} | {fmt(v['f1'])} |")
    lines += [
        f"| **Macro average** | | | | {fmt(s['macro']['precision'])} | "
        f"{fmt(s['macro']['recall'])} | {fmt(s['macro']['f1'])} |",
        f"| **Micro average** | | | | {fmt(s['micro']['precision'])} | "
        f"{fmt(s['micro']['recall'])} | {fmt(s['micro']['f1'])} |",
        "",
        f"Classification accuracy (faults detected with the correct type): "
        f"{fmt(s['classification_accuracy'])}",
        "",
        "## Shipment level",
        "",
        f"- False-alarm rate on clean shipments: {fmt(s['shipments']['false_alarm_rate'])} "
        f"({s['shipments']['clean_with_false_alarm']} of {s['shipments']['clean']})",
        f"- Shipments with faults detected: {fmt(s['shipments']['detection_rate'])} "
        f"({s['shipments']['faulty_detected']} of {s['shipments']['with_faults']})",
        "",
        "## Input filter",
        "",
        f"- Invalid records rejected: {s['filter']['invalid_rejected']} of "
        f"{s['filter']['invalid_sent']} ({fmt(s['filter']['rejection_rate'])})",
        f"- Valid records wrongly rejected: {s['filter']['valid_wrongly_rejected']} of "
        f"{s['filter']['valid_sent']}",
        "",
        "## Performance",
        "",
        f"- Throughput: {p['throughput_records_per_second']} records/s "
        f"({p['records']} records in {p['wall_seconds']} s)",
        f"- Client latency ms: p50 {p['client_latency_ms']['p50']}, "
        f"p95 {p['client_latency_ms']['p95']}, p99 {p['client_latency_ms']['p99']}",
    ]
    if p.get("server_processing_ms"):
        sp = p["server_processing_ms"]
        lines.append(f"- Server processing ms: p50 {sp['p50']}, p95 {sp['p95']}, p99 {sp['p99']}")
    if result.get("scaling"):
        lines += ["", "## Workload scaling", "",
                  "| Shipments | Records | Throughput rec/s | p50 ms | p95 ms | Macro F1 |",
                  "|---:|---:|---:|---:|---:|---:|"]
        for row in result["scaling"]:
            lines.append(f"| {row['shipments']} | {row['records']} | {row['throughput']} | "
                         f"{row['p50_ms']} | {row['p95_ms']} | {fmt(row['macro_f1'])} |")
    if s["false_positive_examples"]:
        lines += ["", "## False positives (first examples)", ""]
        for fp in s["false_positive_examples"][:10]:
            lines.append(f"- {fp['conflict_type']} ({fp['severity']}): {fp['description']}")
    return "\n".join(lines) + "\n"


def save(result, name="evaluation"):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    text = json.dumps(result, indent=2)
    (RESULTS_DIR / f"{name}_{stamp}.json").write_text(text)
    (RESULTS_DIR / f"{name}_latest.json").write_text(text)
    (RESULTS_DIR / f"{name}_latest.md").write_text(markdown(result))
    return RESULTS_DIR / f"{name}_latest.md"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shipments", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--conflict-rate", type=float, default=0.6)
    parser.add_argument("--url", help="evaluate a running server instead of in-process")
    parser.add_argument("--chain-mode", default="off", choices=["off", "simulated", "algorand"],
                        help="anchoring mode for the in-process run")
    parser.add_argument("--scaling", help="comma-separated shipment counts, e.g. 50,200,500")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)

    if args.url:
        factory = lambda: HttpTarget(args.url)  # noqa: E731
    else:
        factory = lambda: InProcessTarget(args.chain_mode)  # noqa: E731

    result = evaluate_once(args.shipments, args.seed, factory, args.conflict_rate)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    if args.scaling:
        rows = []
        for n in [int(x) for x in args.scaling.split(",") if x.strip()]:
            run = evaluate_once(n, args.seed, factory, args.conflict_rate)
            perf = run["performance"]
            rows.append({
                "shipments": n, "records": perf["records"],
                "throughput": perf["throughput_records_per_second"],
                "p50_ms": perf["client_latency_ms"]["p50"],
                "p95_ms": perf["client_latency_ms"]["p95"],
                "server_p50_ms": (perf["server_processing_ms"] or {}).get("p50"),
                "macro_f1": run["scores"]["macro"]["f1"],
            })
            print(f"  scaling {n:>5} shipments: {perf['throughput_records_per_second']} rec/s, "
                  f"p95 {perf['client_latency_ms']['p95']} ms")
        result["scaling"] = rows

    print(markdown(result))
    if not args.no_save:
        path = save(result)
        print(f"Saved {path} (and JSON next to it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
