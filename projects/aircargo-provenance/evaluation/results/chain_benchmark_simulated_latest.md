# Algorand anchoring benchmark

Generated 2026-10-27T13:59:19.454990+00:00 - mode **simulated**, network `simulated` (in-memory).

> **Simulated run - not an Algorand measurement. Do not report these numbers.**

## Sequential (submit -> confirm -> retrieve)

| Method | Tx | Fail | Submit p50 ms | Confirm p50 ms | Confirm p95 ms | Retrieve p50 ms | Fee µAlgo (spent) | Box bytes | Box deposit µAlgo (locked) | Fee + deposit ALGO/tx |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| record_event | 20 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 1000.0 | 108.0 | 59700.0 | 0.0607 |
| report_conflict | 20 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 1000.0 | 152.0 | 95400.0 | 0.0964 |
| register_evidence | 20 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 1000.0 | 82.0 | 49300.0 | 0.0503 |

## Pipelined throughput

| Method | Submitted | Confirmed | Total s | Throughput tx/s |
|---|---:|---:|---:|---:|
| record_event | 30 | 30 | 0.0 | 106183.04 |
| report_conflict | 30 | 30 | 0.0 | 85631.83 |

## Payload size (report_conflict)

| Reason bytes | Box bytes | Confirm p50 ms | Box MBR µAlgo |
|---:|---:|---:|---:|
| 16 | 153.0 | 0.0 | 95800.0 |
| 64 | 201.0 | 0.0 | 115000.0 |
| 256 | 393.0 | 0.0 | 191800.0 |
| 1024 | 1161.0 | 0.0 | 499000.0 |
