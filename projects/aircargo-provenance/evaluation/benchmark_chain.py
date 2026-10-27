"""Algorand performance benchmark for the anchoring layer.

Follows the evaluation dimensions of Lau, Liu, Ma & Talluri (IJPR, 2025),
who report transaction submission, confirmation and retrieval performance
for an Algorand-based air cargo messaging system, plus throughput and cost:

  submission latency   time for the node to accept the signed transaction
  confirmation latency submission start -> transaction confirmed in a block
  retrieval latency    time to read the stored box back from the node
  throughput           confirmed transactions per second with many in flight
  cost                 fee per transaction (spent) and the box minimum-balance
                       deposit (locked while the box exists, refundable when
                       the box is deleted)

Workloads
  sequential  N transactions per method, each awaited before the next
  pipelined   N transactions submitted back-to-back, then all awaited
  payload     report_conflict with reason fields of growing size

Run on LocalNet (fast, dev-mode blocks) for a functional check, and on
TestNet for latency that is comparable with the paper (about 3 s blocks):

  algokit localnet start
  algokit project deploy localnet
  CHAIN_MODE=algorand python -m evaluation.benchmark_chain --n 50

  # TestNet: set ALGOD_SERVER / ALGOD_TOKEN / DEPLOYER_MNEMONIC and
  # CHAIN_NETWORK=testnet, deploy, then run the same command.

  python -m evaluation.benchmark_chain --simulated   # dry run, no node
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import chain  # noqa: E402
from evaluation.evaluate import RESULTS_DIR, stats  # noqa: E402


def make_call(method, n, run_id, reason_size=None):
    key = f"bench-{run_id}-{method}-{n}"
    digest = chain.sha256_bytes(key.encode())
    if method == "record_event":
        return chain.event_call(key, f"ship-{run_id}-{n}", "DEP",
                                int(time.time()), "https://airline.example/org", "BOM", digest)
    if method == "register_evidence":
        return chain.evidence_call(key, f"ship-{run_id}-{n}", digest, "https://forwarder.example/org")
    reason = "WEIGHT_MISMATCH"
    if reason_size:
        reason = (reason + "|" + "x" * reason_size)[:reason_size]
    return chain.conflict_call(key, f"ship-{run_id}-{n}", f"rec-a-{n}", f"rec-b-{n}", reason, digest)


def measure_sequential(backend, method, n, run_id, reason_size=None):
    submit, confirm, retrieve, fees, mbr, rounds_waited, values = [], [], [], [], [], [], []
    failures = 0
    for i in range(n):
        call = make_call(method, i, run_id, reason_size)
        try:
            before = backend.network_info()["last_round"]
            result = backend.call(call)
        except Exception as error:
            failures += 1
            print(f"    {method} #{i} failed: {error}")
            continue
        t0 = time.perf_counter()
        value = backend.read_box(call.contract, call.main_box)
        retrieve.append((time.perf_counter() - t0) * 1000)
        submit.append(result.submit_ms)
        confirm.append(result.confirm_ms)
        fees.append(result.fee_microalgos)
        values.append(len(call.expected))
        mbr.append(sum(chain.box_mbr(len(b), len(call.expected) if b == call.main_box else len(b"OPEN"))
                       for b in call.boxes))
        if result.confirmed_round and before:
            rounds_waited.append(result.confirmed_round - before)
        if value is None or call.check_hash not in value:
            failures += 1
    return {
        "method": method, "transactions": n, "failures": failures,
        "submit_ms": stats(submit), "confirm_ms": stats(confirm), "retrieve_ms": stats(retrieve),
        "rounds_to_confirm": stats(rounds_waited),
        "fee_microalgos_per_tx": round(sum(fees) / len(fees), 1) if fees else None,
        "box_value_bytes": round(sum(values) / len(values), 1) if values else None,
        "box_mbr_microalgos_per_tx": round(sum(mbr) / len(mbr), 1) if mbr else None,
        "cost_algo_per_tx": round((sum(fees) + sum(mbr)) / len(fees) / 1e6, 6) if fees else None,
    }


def measure_pipelined(backend, method, n, run_id):
    calls = [make_call(method, 10_000 + i, run_id) for i in range(n)]
    sent = []
    started = time.perf_counter()
    for call in calls:
        try:
            tx_id, _, fee, _, _ = backend.send_only(call)
            sent.append(tx_id)
        except Exception as error:
            print(f"    pipelined submit failed: {error}")
    submitted = time.perf_counter()
    confirmed = 0
    for tx_id in sent:
        try:
            backend.wait(tx_id)
            confirmed += 1
        except Exception as error:
            print(f"    pipelined wait failed: {error}")
    finished = time.perf_counter()
    return {
        "method": method, "submitted": len(sent), "confirmed": confirmed,
        "submit_seconds": round(submitted - started, 3),
        "total_seconds": round(finished - started, 3),
        "throughput_tps": round(confirmed / (finished - started), 2) if finished > started else None,
    }


def markdown(result):
    lines = [
        "# Algorand anchoring benchmark",
        "",
        f"Generated {result['generated_at']} - mode **{result['mode']}**, network "
        f"`{result['network'].get('genesis_id')}` ({result['network'].get('server')}).",
        "",
    ]
    if result["mode"] != "algorand":
        lines += ["> **Simulated run - not an Algorand measurement. Do not report these numbers.**", ""]
    lines += [
        "## Sequential (submit -> confirm -> retrieve)", "",
        "| Method | Tx | Fail | Submit p50 ms | Confirm p50 ms | Confirm p95 ms | Retrieve p50 ms | Fee µAlgo (spent) | Box bytes | Box deposit µAlgo (locked) | Fee + deposit ALGO/tx |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["sequential"]:
        lines.append(
            f"| {row['method']} | {row['transactions']} | {row['failures']} | "
            f"{row['submit_ms']['p50']} | {row['confirm_ms']['p50']} | {row['confirm_ms']['p95']} | "
            f"{row['retrieve_ms']['p50']} | {row['fee_microalgos_per_tx']} | {row['box_value_bytes']} | "
            f"{row['box_mbr_microalgos_per_tx']} | {row['cost_algo_per_tx']} |"
        )
    lines += ["", "## Pipelined throughput", "",
              "| Method | Submitted | Confirmed | Total s | Throughput tx/s |",
              "|---|---:|---:|---:|---:|"]
    for row in result["pipelined"]:
        lines.append(f"| {row['method']} | {row['submitted']} | {row['confirmed']} | "
                     f"{row['total_seconds']} | {row['throughput_tps']} |")
    lines += ["", "## Payload size (report_conflict)", "",
              "| Reason bytes | Box bytes | Confirm p50 ms | Box MBR µAlgo |",
              "|---:|---:|---:|---:|"]
    for row in result["payload"]:
        lines.append(f"| {row['reason_bytes']} | {row['box_value_bytes']} | "
                     f"{row['confirm_ms']['p50']} | {row['box_mbr_microalgos_per_tx']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=30, help="transactions per sequential workload")
    parser.add_argument("--pipelined", type=int, default=50, help="transactions in the pipelined run")
    parser.add_argument("--payload-sizes", default="16,64,256,1024")
    parser.add_argument("--simulated", action="store_true", help="dry run without a node")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)

    if args.simulated:
        backend = chain.SimulatedBackend()
    else:
        if os.getenv("CHAIN_MODE", "algorand") != "algorand":
            print("Set CHAIN_MODE=algorand (or use --simulated).")
            return 2
        backend = chain.AlgorandBackend()
    run_id = uuid.uuid4().hex[:8]
    print(f"Benchmark {run_id} in {backend.mode} mode: {backend.network_info()}")

    sequential = []
    for method in ("record_event", "report_conflict", "register_evidence"):
        print(f"  sequential {method} x{args.n}")
        sequential.append(measure_sequential(backend, method, args.n, run_id))

    pipelined = []
    for method in ("record_event", "report_conflict"):
        print(f"  pipelined {method} x{args.pipelined}")
        pipelined.append(measure_pipelined(backend, method, args.pipelined, run_id))

    payload = []
    for size in [int(s) for s in args.payload_sizes.split(",") if s.strip()]:
        print(f"  payload reason={size} bytes")
        row = measure_sequential(backend, "report_conflict", max(5, args.n // 3),
                                 f"{run_id}p{size}", reason_size=size)
        row["reason_bytes"] = size
        payload.append(row)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": backend.mode,
        "network": backend.network_info(),
        "app_ids": getattr(backend, "app_ids", None),
        "parameters": vars(args),
        "sequential": sequential,
        "pipelined": pipelined,
        "payload": payload,
        "reference": "Lau, Liu, Ma & Talluri (2025), IJPR 63(14):5428-5451 - "
                     "submission, confirmation and retrieval on Algorand.",
    }
    report = markdown(result)
    print(report)
    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        name = "chain_benchmark" if backend.mode == "algorand" else "chain_benchmark_simulated"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (RESULTS_DIR / f"{name}_{stamp}.json").write_text(json.dumps(result, indent=2))
        (RESULTS_DIR / f"{name}_latest.json").write_text(json.dumps(result, indent=2))
        (RESULTS_DIR / f"{name}_latest.md").write_text(report)
        print(f"Saved {RESULTS_DIR / (name + '_latest.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
