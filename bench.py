"""Performance baseline for PCG Maroc AI.

Phase 1 (in-process, instrumented): sequential bulk uploads at 1/10/100/500/1000
invoices through the real FastAPI app + accounting engine + SQLite, with the
OpenAI extractor replaced by a deterministic fake (so numbers measure OUR code,
not OpenAI latency). Per-invoice stage timing:
  - pipeline  = fake OCR + calculation cascade + journal + 14 validations
  - db_write  = every INSERT/UPDATE (wrapped app.core.db.execute)
  - overhead  = request handling, persistence to disk, duplicate check, insights

Phase 2 (real server): concurrency 5/10/20 simultaneous single uploads against
uvicorn on :8600 — this is the SQLite write-lock stress test.

Run: venv/Scripts/python.exe bench.py
"""
import io
import json
import os
import statistics
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp(prefix="pcg-bench-")
os.environ["DATABASE_PATH"] = f"{_tmp}/bench.db"
os.environ["STORAGE_BACKEND"] = "memory"
os.environ.setdefault("OPENAI_API_KEY", "bench-key")
os.environ["ACCOUNTS_OFFLINE"] = "1"

import psutil  # noqa: E402

from app.core import db as core_db  # noqa: E402
from app import main_pipeline  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

# ── Instrumentation ─────────────────────────────────────────────────────────
_counter = {"n": 0}
STATS = {"sql_time": 0.0, "sql_count": 0, "pipeline_times": []}


async def _fake_extractor(image, perspective=None, exercise_context=None):
    _counter["n"] += 1
    return ExtractedInvoiceData(
        invoice_type="DOIT", invoice_category="facture_achat",
        date="2026-06-18", invoice_number=f"BEN-{_counter['n']:06d}",
        supplier_name=f"SUPPLIER {_counter['n'] % 25}",   # 25 distinct suppliers
        montant_brut=1_000 + (_counter['n'] % 100), remise_pct=10,
        escompte_pct=2, tva_pct=20, payment_mode="banque")


main_pipeline.extract_invoice_data = _fake_extractor

_orig_execute = core_db.execute
def _timed_execute(sql, params=()):
    t0 = time.perf_counter()
    _orig_execute(sql, params)
    STATS["sql_time"] += time.perf_counter() - t0
    STATS["sql_count"] += 1
core_db.execute = _timed_execute

_orig_pipeline = main_pipeline.process_invoice_pipeline
async def _timed_pipeline(*a, **kw):
    t0 = time.perf_counter()
    out = await _orig_pipeline(*a, **kw)
    STATS["pipeline_times"].append(time.perf_counter() - t0)
    return out
main_pipeline.process_invoice_pipeline = _timed_pipeline

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
# The router binds process_invoice_pipeline at import time — rebind the timed one
from app.api.routers import invoices as invoices_router  # noqa: E402
invoices_router.process_invoice_pipeline = _timed_pipeline

PROC = psutil.Process()


def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


def fmt_ms(s):
    return f"{s * 1000:8.1f} ms"


def run_sequential(client, hdr, client_id, results):
    print(f"\n{'Scenario':>10} | {'total':>9} | {'avg/inv':>9} | {'p95/inv':>9} | "
          f"{'engine avg':>10} | {'db avg':>8} | {'sql/inv':>7} | {'RAM peak':>8} | {'db growth':>9}")
    print("-" * 110)
    for n in (1, 10, 100, 500, 1000):
        STATS["sql_time"] = 0.0; STATS["sql_count"] = 0; STATS["pipeline_times"] = []
        db_size0 = os.path.getsize(os.environ["DATABASE_PATH"])
        ram0 = PROC.memory_info().rss
        per_invoice = []
        PROC.cpu_percent()  # reset window
        t0 = time.perf_counter()
        # batches of 100 files per request (mirrors the UI's bulk upload)
        done = 0
        while done < n:
            batch = min(100, n - done)
            files = [("files", (f"b{done+i}.jpg", io.BytesIO(b"img"), "image/jpeg"))
                     for i in range(batch)]
            bt0 = time.perf_counter()
            r = client.post(f"/invoices/bulk-upload?client_id={client_id}",
                            headers=hdr, files=files)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["failed"] == 0, body
            per_invoice.extend([(time.perf_counter() - bt0) / batch] * batch)
            done += batch
        total = time.perf_counter() - t0
        cpu = PROC.cpu_percent()
        ram_peak = max(ram0, PROC.memory_info().rss)
        db_growth = os.path.getsize(os.environ["DATABASE_PATH"]) - db_size0
        engine = STATS["pipeline_times"]
        row = {
            "n": n, "total_s": round(total, 2),
            "avg_ms": round(statistics.mean(per_invoice) * 1000, 1),
            "p95_ms": round(pctl(per_invoice, 95) * 1000, 1),
            "engine_avg_ms": round(statistics.mean(engine) * 1000, 2),
            "engine_p95_ms": round(pctl(engine, 95) * 1000, 2),
            "db_avg_ms": round(STATS["sql_time"] / n * 1000, 2),
            "sql_per_invoice": round(STATS["sql_count"] / n, 1),
            "throughput_per_s": round(n / total, 1),
            "ram_peak_mb": round(ram_peak / 1e6, 1),
            "db_growth_kb": round(db_growth / 1024, 1),
            "cpu_pct": cpu,
        }
        results["sequential"].append(row)
        print(f"{n:>10} | {total:>8.2f}s | {fmt_ms(statistics.mean(per_invoice))} | "
              f"{fmt_ms(pctl(per_invoice, 95))} | {fmt_ms(statistics.mean(engine)):>10} | "
              f"{fmt_ms(STATS['sql_time']/n):>8} | {STATS['sql_count']/n:>7.1f} | "
              f"{ram_peak/1e6:>6.1f}MB | {db_growth/1024:>7.1f}KB")


def run_concurrency(base_url, token, client_id, results):
    import httpx
    print(f"\n{'Parallel':>10} | {'total':>9} | {'avg lat':>9} | {'p95 lat':>9} | "
          f"{'max lat':>9} | {'ok':>4} | {'fail':>4}")
    print("-" * 75)
    for workers in (5, 10, 20):
        latencies, failures = [], []
        def one(i):
            t0 = time.perf_counter()
            try:
                r = httpx.post(
                    f"{base_url}/invoices/bulk-upload?client_id={client_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    files=[("files", (f"c{workers}_{i}.jpg", b"img", "image/jpeg"))],
                    timeout=120)
                if r.status_code != 200 or r.json()["failed"]:
                    failures.append(r.text[:120])
            except Exception as e:
                failures.append(str(e)[:120])
            latencies.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        threads = [threading.Thread(target=one, args=(i,)) for i in range(workers)]
        for t in threads: t.start()
        for t in threads: t.join()
        total = time.perf_counter() - t0
        row = {"workers": workers, "total_s": round(total, 2),
               "avg_ms": round(statistics.mean(latencies) * 1000, 1),
               "p95_ms": round(pctl(latencies, 95) * 1000, 1),
               "max_ms": round(max(latencies) * 1000, 1),
               "ok": workers - len(failures), "failures": failures}
        results["concurrency"].append(row)
        print(f"{workers:>10} | {total:>8.2f}s | {fmt_ms(statistics.mean(latencies))} | "
              f"{fmt_ms(pctl(latencies, 95))} | {fmt_ms(max(latencies))} | "
              f"{workers - len(failures):>4} | {len(failures):>4}")
        for f in failures[:3]:
            print(f"           failure: {f}")


def main():
    results = {"sequential": [], "concurrency": [], "meta": {
        "cpu": psutil.cpu_count(), "machine_ram_gb": round(psutil.virtual_memory().total / 1e9, 1)}}

    with TestClient(app) as client:
        reg = client.post("/auth/register-firm", json={
            "firm_name": "Bench Firm", "full_name": "Bench Admin",
            "email": "bench@bench.ma", "password": "password123"}).json()
        hdr = {"Authorization": f"Bearer {reg['access_token']}"}
        client_id = client.post("/clients", headers=hdr, json={"name": "Bench Client"}).json()["id"]

        print("=" * 110)
        print("PHASE 1 — Sequential bulk uploads (in-process, instrumented)")
        run_sequential(client, hdr, client_id, results)

    # Phase 2: real server for genuine socket + SQLite lock behaviour
    print("\n" + "=" * 110)
    print("PHASE 2 — Concurrent uploads against live uvicorn (SQLite write-lock stress)")
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "e2e_server.py"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import httpx
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:8600/health", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(1)
        reg = httpx.post("http://127.0.0.1:8600/auth/register-firm", json={
            "firm_name": "Bench Live", "full_name": "Bench Live",
            "email": "live@bench.ma", "password": "password123"}, timeout=30).json()
        token = reg["access_token"]
        cid = httpx.post("http://127.0.0.1:8600/clients",
                         headers={"Authorization": f"Bearer {token}"},
                         json={"name": "Live Client"}, timeout=30).json()["id"]
        run_concurrency("http://127.0.0.1:8600", token, cid, results)
    finally:
        proc.kill()

    with open(f"{_tmp}/results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nJSON results: {_tmp}/results.json")
    print(f"Machine: {results['meta']['cpu']} CPUs, {results['meta']['machine_ram_gb']} GB RAM")


if __name__ == "__main__":
    main()
