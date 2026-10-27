# Conflict detection evaluation

Generated 2026-10-27T13:53:17.100445+00:00 - dataset seed 11, 300 shipments, 3382 records, 196 injected faults. Target: in-process.

Synthetic data from evaluation/simulator.py; results describe the prototype on simulated data, not industry-wide validation.

## Per conflict type

| Type | Injected | Detected | Predicted | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| STATUS_MISMATCH | 18 | 18 | 18 | 100.0% | 100.0% | 100.0% |
| INVALID_TRANSITION | 19 | 19 | 39 | 100.0% | 100.0% | 100.0% |
| LOCATION_MISMATCH | 20 | 20 | 90 | 100.0% | 100.0% | 100.0% |
| WEIGHT_MISMATCH | 13 | 13 | 26 | 100.0% | 100.0% | 100.0% |
| UNIT_MISMATCH | 17 | 17 | 34 | 100.0% | 100.0% | 100.0% |
| VOLUME_MISMATCH | 18 | 18 | 36 | 100.0% | 100.0% | 100.0% |
| PIECE_COUNT_MISMATCH | 15 | 15 | 30 | 100.0% | 100.0% | 100.0% |
| PARTY_MISMATCH | 18 | 18 | 52 | 69.2% | 100.0% | 81.8% |
| ROUTE_MISMATCH | 15 | 15 | 30 | 100.0% | 100.0% | 100.0% |
| SPECIAL_HANDLING_MISMATCH | 11 | 11 | 22 | 100.0% | 100.0% | 100.0% |
| REFERENCE_MISMATCH | 18 | 18 | 36 | 100.0% | 100.0% | 100.0% |
| DUPLICATE_IDENTIFIER | 14 | 14 | 14 | 100.0% | 100.0% | 100.0% |
| **Macro average** | | | | 97.4% | 100.0% | 98.5% |
| **Micro average** | | | | 96.2% | 100.0% | 98.1% |

Classification accuracy (faults detected with the correct type): 100.0%

## Shipment level

- False-alarm rate on clean shipments: 1.7% (2 of 121)
- Shipments with faults detected: 100.0% (179 of 179)

## Input filter

- Invalid records rejected: 68 of 68 (100.0%)
- Valid records wrongly rejected: 0 of 3314

## Performance

- Throughput: 194.6 records/s (3382 records in 17.38 s)
- Client latency ms: p50 4.897, p95 7.327, p99 9.066
- Server processing ms: p50 2.206, p95 3.948, p99 5.279

## Workload scaling

| Shipments | Records | Throughput rec/s | p50 ms | p95 ms | Macro F1 |
|---:|---:|---:|---:|---:|---:|
| 50 | 567 | 194.06 | 4.886 | 7.309 | 100.0% |
| 200 | 2252 | 179.84 | 5.324 | 7.829 | 98.5% |
| 500 | 5634 | 190.1 | 5.058 | 7.363 | 98.6% |

## False positives (first examples)

- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.85 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.85 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.85 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.85 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
- PARTY_MISMATCH (MEDIUM): Different consignee names (similarity 0.87 < 0.88).
