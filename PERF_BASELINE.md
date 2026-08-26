# Performance Baseline — PCG Maroc AI

Date: 2026-07-11 · Machine: 4 CPUs, 17 GB RAM (Windows 11) · SQLite, synchronous
processing, single uvicorn worker.

**Method.** The OpenAI vision extractor is replaced by a deterministic fake, so
every number below measures **our code**: FastAPI request handling, the
accounting engine (calculation cascade + journal + 14 validations), duplicate
detection, insights, and SQLite persistence. Real production adds OpenAI
latency (~2–8 s per invoice, network-bound) on top — that cost is external and
is precisely what the future background queue will absorb.
Phase 1 runs in-process (TestClient) with instrumented stages; Phase 2 hits a
live uvicorn over HTTP. Reproduce with `venv/Scripts/python.exe bench.py`.

## Phase 1 — Sequential bulk uploads

| Invoices | Total | Avg/inv | P95/inv | Engine avg | DB writes avg | SQL/inv | RAM peak | DB growth | Throughput |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.26 s | 258.5 ms* | — | 3.9 ms | 37.8 ms | 3.0 | 79.5 MB | 8 KB | — |
| 10 | 0.70 s | 69.8 ms | 69.8 ms | 3.3 ms | 31.9 ms | 3.0 | 81.6 MB | 88 KB | 14.3/s |
| 100 | 7.01 s | 70.1 ms | 70.1 ms | 3.1 ms | 32.1 ms | 3.0 | 88.8 MB | 888 KB | 14.3/s |
| 500 | 37.4 s | 74.8 ms | 90.1 ms | 2.6 ms | 34.6 ms | 3.0 | 93.3 MB | 4.4 MB | 13.4/s |
| 1000 | 79.1 s | 79.1 ms | 96.6 ms | 2.8 ms | 35.2 ms | 3.0 | 93.3 MB | 8.8 MB | 12.6/s |

\* first-invoice cost includes one-time warmup (imports, first connection).

**Where the time goes (per invoice, steady state ≈ 75 ms):**

| Stage | ~Time | Note |
|---|---:|---|
| Accounting engine (OCR-fake → cascade → journal → validations) | ~3 ms | deterministic, CPU-only |
| DB writes (3 statements) | ~33 ms | ~11 ms per statement — each `execute()` opens a fresh connection and commits (fsync) |
| Everything else (request parsing, file persistence, duplicate query, insights queries, response) | ~39 ms | read queries are not in the DB-write figure |

## Phase 2 — Concurrent uploads (live server, SQLite write-lock stress)

| Parallel uploads | Total | Avg latency | P95 | Max | OK | Failed |
|---:|---:|---:|---:|---:|---:|---:|
| 5 | 2.8 s | 2.76 s | 2.83 s | 2.83 s | 5 | 0 |
| 10 | 5.9 s | 5.64 s | 5.92 s | 5.92 s | 10 | 0 |
| 20 | 11.7 s | 11.0 s | 11.5 s | 11.5 s | 20 | 0 |

## Findings

1. **The accounting engine is not the bottleneck — by two orders of
   magnitude.** ~3 ms per invoice, flat across 1 → 1000. In isolation the
   engine could sustain ~300 invoices/second; overall platform throughput is
   governed by storage writes and, in production, by AI extraction latency.
2. **Zero failures, zero lock errors at every scale tested.** 1000 sequential
   and 20 concurrent uploads all succeeded. SQLite's write lock never surfaced
   as an error because the synchronous server serializes work first.
3. **Concurrency degrades by serialization, not by failure.** Latency scales
   linearly with parallel clients (5 → 2.8 s, 20 → 11.5 s each): one worker
   processes one upload at a time. This is the concrete evidence that the
   **background queue is the right next investment** — and that PostgreSQL
   alone would not fix perceived slowness.
4. **The cheapest real optimization is the DB connection pattern**, not a
   database swap. ~44 % of per-invoice time is 3 writes at ~11 ms each,
   dominated by per-call connect + commit/fsync. SQLite WAL mode + a reused
   connection would likely cut per-invoice time roughly in half. Worth doing
   only if pilot volumes make it matter.
5. **Memory is a non-issue.** RSS plateaus at ~93 MB through 1000 invoices;
   no growth pattern that suggests a leak.
6. **Storage cost ≈ 8.8 KB/invoice** (full AI response JSON included).
   100 000 invoices ≈ 0.9 GB — SQLite-manageable for any pilot.

## Capacity statement (for "can this handle our firm?")

A firm processing **500 invoices/day** consumes ~37 s of engine time per day —
under 0.1 % utilization. The practical constraint in production is OpenAI
extraction latency on the synchronous path (a user watching an upload waits
for the AI). The queue (`processing` status already exists for it) converts
that wait into a background job and is the gate for multi-user bulk workloads;
after that, Postgres is an operational upgrade, not a performance rescue.

## Honest limits of this baseline

- Fake OCR: real extraction adds external OpenAI latency not measured here.
- Phase 1 is in-process (no HTTP socket); Phase 2 covers the network path.
- Single machine, Windows, dev-grade disk; pilot hardware will differ.
- `insights`/read queries are inside "everything else", not separately timed.
