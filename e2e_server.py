"""Backend runner for Playwright E2E tests.

Same app as production, but:
- fresh temp database + storage per run
- the OpenAI vision extractor is replaced with a deterministic fake
  (unique invoice numbers so uploads never collide as duplicates)
- ACCOUNTS_OFFLINE=1 -> PCG fallbacks, no account-search service needed

Run: python e2e_server.py   (listens on 127.0.0.1:8600)
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="pcg-e2e-")
os.environ["DATABASE_PATH"] = f"{_tmp}/e2e.db"
os.environ["STORAGE_BACKEND"] = "memory"
os.environ.setdefault("OPENAI_API_KEY", "e2e-fake-key")
os.environ["ACCOUNTS_OFFLINE"] = "1"

from app import main_pipeline  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

_counter = {"n": 0}


async def _fake_extractor(image, perspective=None, exercise_context=None):
    _counter["n"] += 1
    return ExtractedInvoiceData(
        invoice_type="DOIT", invoice_category="facture_achat",
        date="2026-06-18", invoice_number=f"E2E-{_counter['n']:04d}",
        supplier_name="TECHNO BUREAU", montant_brut=10_000,
        remise_pct=10, escompte_pct=2, tva_pct=20,
        payment_mode="banque",
    )


main_pipeline.extract_invoice_data = _fake_extractor

if __name__ == "__main__":
    import uvicorn

    from app.main import app
    print(f"E2E backend on http://127.0.0.1:8600  (db: {_tmp})")
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")
