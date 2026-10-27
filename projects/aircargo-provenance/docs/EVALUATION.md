# Evaluation: method and results

Two separate questions are evaluated:

1. **Does the system detect and correctly classify conflicts?** (`evaluation/evaluate.py`)
2. **What does anchoring on Algorand cost, and how fast is it?** (`evaluation/benchmark_chain.py`)

All detection numbers below are from **synthetic data**. They describe how the prototype behaves on the simulator's scenarios, not real-world accuracy.

## 1. Data: `evaluation/simulator.py`

Each simulated shipment is single-leg and reported by three parties, as in a typical air export:

| Party | Records |
|---|---|
| Freight forwarder (FFW) | booking event `BKD`, booking report |
| Airline (AIR) | acceptance report, `MAN`, `DEP`, `ARR` events |
| Ground handler (GHA) | warehouse report, `RCS`, `DEP`, `DLV` events |

That gives 11 records per shipment: 1 shipment, 7 events, 3 party reports. Routes, block times, cargo profiles (perishables, pharma, lithium batteries, flammables, live animals, heavy cargo), weights, volumes, piece counts and AWB numbers with a valid mod-7 check digit are randomised from a seed. All organisations are fictional.

**Clean shipments contain hard negatives**, realistic differences that must *not* be flagged:

- weight re-weighed within ±1.2 %; the ground handler reports in pounds (35 %) or cubic feet (30 %);
- company-name variants (`Tata Exports Private Limited` / `TATA EXPORTS PVT. LTD.`, `Müller Pharma GmbH` / `MÜLLER PHARMA GMBH`, `L.L.C.` / `LLC`...);
- waybill formatting (`176-12345675`, `17612345675`, `176 1234 5675`);
- special handling codes in a different order;
- the handler's departure time within ±20 minutes of the airline's;
- 5 % of shipments use **genuinely hard name variants** that a simple normaliser can miss (`Shree Ganesh Traders` / `Shree Ganesh Trading Co`).

**60 % of shipments get an injected fault** of one of the 12 types (10 % of those get a second attribute fault). Each fault has a magnitude that clearly breaks the rule. Examples: the airline's departure is shifted 45 min to 3 h; delivery is placed before arrival; a kilogram figure is entered as LBR; a waybill digit is changed; a dangerous-goods code is dropped; an event is replayed. Records arrive interleaved across shipments in random order, and about 2 % of records are deliberately invalid (no unit, `IN_TRANSIT` as a status code, no timezone, relative `@id`...).

**Labels** record which records each fault corrupted and which conflict types are a correct consequence. Usually that's only the fault's own type. The exception: a wrong route also makes the arrival and delivery locations contradict that report, so `LOCATION_MISMATCH` is accepted there too. The rules are *not* used to make the labels.

## 2. Metrics

| Metric | Definition |
|---|---|
| Precision (type T) | conflicts of type T that touch a corrupted record whose fault accepts T ÷ all conflicts of type T |
| Recall (type T) | faults of type T with at least one type-T conflict touching the corrupted record ÷ faults of type T |
| F1, macro / micro | standard; macro over the 12 types |
| Classification accuracy | faults detected with their own type ÷ all faults |
| Confusion matrix | injected fault type × conflict type raised on corrupted records |
| False-alarm rate | clean shipments with any conflict ÷ clean shipments |
| Filter | invalid records rejected with 400/422; valid records wrongly rejected |
| Latency | per record: client round trip and server processing (p50/p95/p99) |
| Throughput | records ÷ wall time; repeated for growing workloads (`--scaling`) |

The workload-scaling run follows the style of Sund et al. (2020), who evaluate event processing under different workloads. Throughput and latency are measured in-process (FastAPI TestClient, SQLite) on a development container. Absolute numbers depend on the machine; the trend matters.

## 3. Results (seed 11, 300 shipments, 3,382 records, 196 faults)

Rule thresholds were developed on seed 7. The results below use seed 11, which was not used during development. Reproduce with:

```
poetry run python -m evaluation.evaluate --shipments 300 --seed 11 --scaling 50,200,500
```

| Type | Injected | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Duplicate identifier | 14 | 100 % | 100 % | 100 % |
| Timestamp mismatch | 18 | 100 % | 100 % | 100 % |
| Impossible sequence | 19 | 100 % | 100 % | 100 % |
| Location mismatch | 20 | 100 % | 100 % | 100 % |
| Weight mismatch | 13 | 100 % | 100 % | 100 % |
| Unit mismatch | 17 | 100 % | 100 % | 100 % |
| Volume mismatch | 18 | 100 % | 100 % | 100 % |
| Piece count mismatch | 15 | 100 % | 100 % | 100 % |
| **Party info mismatch** | 18 | **69.2 %** | 100 % | **81.8 %** |
| Route mismatch | 15 | 100 % | 100 % | 100 % |
| Special handling mismatch | 11 | 100 % | 100 % | 100 % |
| Waybill mismatch | 18 | 100 % | 100 % | 100 % |
| **Macro average** | | **97.4 %** | **100 %** | **98.5 %** |

- Faults classified with the correct type: **100 %** (196 of 196)
- False alarms on clean shipments: **1.7 %** (2 of 121)
- Invalid records rejected by the filter: **100 %** (68 of 68); valid records wrongly rejected: **0** of 3,314
- Throughput: **~195 records/s**; client latency p50 4.9 ms, p95 7.3 ms; server processing p50 2.2 ms, p95 3.9 ms

| Workload (shipments) | Records | Throughput rec/s | p95 ms | Macro F1 |
|---:|---:|---:|---:|---:|
| 50 | 567 | 194 | 7.3 | 100 % |
| 200 | 2,252 | 180 | 7.8 | 98.5 % |
| 500 | 5,634 | 190 | 7.4 | 98.6 % |

### How to read these numbers

- **Recall is 100 % by construction.** Injected faults are clearly outside the tolerances, so a correct rule must catch them. Recall here checks that every rule is wired and classifies correctly; it doesn't measure difficulty.
- **Precision and the false-alarm rate are the informative results**, because they are measured against the hard negatives. Unit conversion, re-weighing noise, waybill formatting, code order and departure-time jitter produced **no** false alarms.
- **All 16 false positives are `PARTY_MISMATCH` on the hard name variants**, for example "Shree Ganesh Traders" vs "Shree Ganesh Trading Co" (similarity 0.85 < 0.88). This is the real limitation of name matching without organisation identifiers. When both reports carry party `@id`s, ids decide and names are not compared.
- **Throughput stays flat as the workload grows**, because each record is compared only with the records of its own shipment (indexed by shipment id). Cost per record depends on the parties per shipment, not on total volume.
- The **confusion matrix** (dashboard → Metrics) has off-diagonal counts only where expected: shipments with two faults in the same record, and wrong routes, which also contradict event locations.

### Threats to validity

- **Synthetic data:** the name variants, cargo profiles and noise levels are the author's assumptions, not measured from industry data.
- **Single-leg only:** multi-leg routings would make `DEP`/`ARR` legitimately repeat.
- **Tolerances are design choices:** 30 min for timestamps, 5 min clock skew, 2 % weight, 5 % volume and 0.88 name similarity were not calibrated against operational data. `evaluate.py` can be rerun after changing the constants in `api/rules.py` and `api/report_rules.py` to report sensitivity.
- **No real ONE Record servers:** records are sent directly to the aggregator API.

## 4. Algorand anchoring benchmark

Lau, Liu, Ma & Talluri (IJPR 2025) evaluate an Algorand-based air cargo messaging system on transaction **submission, confirmation and retrieval**. `benchmark_chain.py` measures the same three for this project's contracts, plus throughput and cost:

| Measure | How |
|---|---|
| Submission latency | signed transaction → accepted by algod |
| Confirmation latency | submission start → confirmed in a block (`wait_for_confirmation`) |
| Retrieval latency | read the stored box back from algod |
| Throughput | 50 transactions submitted back-to-back, then all awaited |
| Cost | fee per transaction (spent) and box minimum-balance deposit (locked, refundable when the box is deleted) |
| Payload size | `report_conflict` with growing record sizes |

**Run it on a real node.** On LocalNet (a single local node), timings show the software overhead but are not comparable with a public network. For numbers comparable with the paper, run on TestNet (see `docs/RUNNING.md`). Simulated runs are labelled and must not be reported.

**Cost follows from protocol rules and is exact.** Minimum fee is 1,000 µAlgo per transaction. A box costs 2,500 + 400 × (name bytes + value bytes) µAlgo:

| Anchored item | Box bytes (name + value) | Deposit µAlgo | Fee µAlgo |
|---|---:|---:|---:|
| Event (`record_event`) | 35 + 108 | 59,700 | 1,000 |
| Conflict (`report_conflict`, record box + status box) | 35 + 152, 35 + 4 | 95,400 | 1,000 |
| Evidence (`register_evidence`) | 35 + 82 | 49,300 | 1,000 |

Box sizes are for the default record contents; a resolution later appends to the conflict record (the contract deletes and rewrites the box, which is safe on the AVM) and the deposit grows accordingly.

**Design implication for the report:** storage, not fees, dominates. Anchoring every event (7 per shipment here) locks about 0.42 ALGO per shipment. Anchoring only conflicts locks about 0.095 ALGO per conflict. That's why on-chain values are short hashes and keys, never shipment data, and why `ANCHOR_EVENTS=0` exists. Batching many hashes into one Merkle root per block of records would reduce it further (future work).

## 5. Tests

`poetry run pytest -q` runs 182 tests:

- rule unit tests, including the boundaries (exactly Δ, exactly ε, exactly 2 %);
- API tests for all worked cases, including arrival-order independence;
- contract tests in the Algorand Python emulator (`algorand-python-testing`);
- the real `algokit-utils` transaction path against a fake algod that decodes the ABI calls and applies the contracts' box writes;
- the evaluation pipeline itself.
