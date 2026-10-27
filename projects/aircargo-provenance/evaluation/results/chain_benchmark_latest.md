# Algorand anchoring benchmark

Generated 2026-10-27T14:39:14.052129+00:00 - mode **algorand**, network `dockernet-v1` (localnet (default)).

## Sequential (submit -> confirm -> retrieve)

| Method | Tx | Fail | Submit p50 ms | Confirm p50 ms | Confirm p95 ms | Retrieve p50 ms | Fee µAlgo (spent) | Box bytes | Box deposit µAlgo (locked) | Fee + deposit ALGO/tx |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| record_event | 30 | 0 | 13.858 | 44.703 | 56.029 | 15.13 | 1000.0 | 108.0 | 59700.0 | 0.0607 |
| report_conflict | 30 | 0 | 16.647 | 45.263 | 58.345 | 15.52 | 1000.0 | 152.0 | 95400.0 | 0.0964 |
| register_evidence | 30 | 0 | 15.111 | 44.74 | 57.894 | 15.056 | 1000.0 | 82.0 | 49300.0 | 0.0503 |

## Pipelined throughput

| Method | Submitted | Confirmed | Total s | Throughput tx/s |
|---|---:|---:|---:|---:|
| record_event | 50 | 50 | 1.889 | 26.47 |
| report_conflict | 50 | 50 | 1.626 | 30.74 |

## Payload size (report_conflict)

| Reason bytes | Box bytes | Confirm p50 ms | Box MBR µAlgo |
|---:|---:|---:|---:|
| 16 | 153.0 | 28.307 | 95800.0 |
| 64 | 201.0 | 37.334 | 115000.0 |
| 256 | 393.0 | 26.058 | 191800.0 |
| 1024 | 1161.0 | 39.033 | 499000.0 |
