"""OD manuelles : équilibre bloquant, intégration automatique dans le
Grand Livre / Balance / Lettrage / Balance âgée, lettrage OD ↔ facture,
suppression protégée, pièce jointe."""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/od.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def fake_extractor():
    async def _fake(image, perspective=None, exercise_context=None):
        return ExtractedInvoiceData(
            invoice_type="DOIT", invoice_category="facture_achat",
            date="2026-06-18", invoice_number="OD-FAC-1",
            supplier_name="OD SUPPLIES", montant_brut=1_000, tva_pct=20,
            payment_mode="none")   # impayée -> 4411 crédit 1200, non soldée
    mp = pytest.MonkeyPatch()
    mp.setattr("app.main_pipeline.extract_invoice_data", _fake)
    yield
    mp.undo()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def firm(client):
    reg = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet OD", "full_name": "OD Admin",
        "email": "od@od.ma", "password": "password123"}).json()
    h = {"Authorization": f"Bearer {reg['access_token']}"}
    cid = client.post("/clients", headers=h, json={"name": "Client OD"}).json()["id"]
    r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=h,
                    files=[("files", ("od.jpg", io.BytesIO(b"img"), "image/jpeg"))])
    client.post(f"/invoices/{r.json()['items'][0]['invoice_id']}/review",
                headers=h, json={"action": "approve", "post_now": True})
    return h


REGLEMENT = {
    "journal": "BQ", "date": "2026-07-01",
    "libelle": "Règlement fournisseur OD SUPPLIES par virement",
    "lines": [
        {"account_number": "4411", "account_label": "Fournisseurs", "side": "DEBIT", "amount": 1200.0},
        {"account_number": "5141", "account_label": "Banques", "side": "CREDIT", "amount": 1200.0},
    ]}


def test_unbalanced_od_rejected(client, firm):
    bad = dict(REGLEMENT, lines=[dict(REGLEMENT["lines"][0]),
                                 dict(REGLEMENT["lines"][1], amount=1100.0)])
    r = client.post("/od", headers=firm, json=bad)
    assert r.status_code == 422 and "déséquilibrée" in r.json()["detail"]


def test_od_flows_into_ledger_balance_and_journal(client, firm):
    bal_before = client.get("/balance", headers=firm).json()
    r = client.post("/od", headers=firm, json=REGLEMENT)
    assert r.status_code == 201
    # Grand livre : 4411 reçoit le débit de l'OD
    gl = client.get("/ledger/4411", headers=firm).json()
    assert any(l["perspective"] == "BQ" and l["side"] == "DEBIT" and l["amount"] == 1200.0
               for l in gl["lines"])
    # Balance : toujours équilibrée, totaux augmentés de 1200 de chaque côté
    bal = client.get("/balance", headers=firm).json()
    assert bal["balanced"] is True
    assert bal["total_debit"] == pytest.approx(bal_before["total_debit"] + 1200.0)
    assert bal["total_credit"] == pytest.approx(bal_before["total_credit"] + 1200.0)


def test_letter_od_against_invoice_clears_aging(client, firm):
    """LE test d'intégration : la facture impayée (crédit 4411) se lettre
    contre l'OD de règlement (débit 4411) -> l'encours fournisseur tombe à 0."""
    aging = client.get("/aging?kind=fournisseurs", headers=firm).json()
    assert aging["totals"]["total"] == pytest.approx(1200.0)
    det = client.get("/lettrage/4411", headers=firm).json()
    s = next(x for x in det["suggestions"] if x["reason"] == "montants égaux")
    assert client.post("/lettrage/4411", headers=firm,
                       json={"line_refs": s["line_refs"]}).status_code == 201
    after = client.get("/aging?kind=fournisseurs", headers=firm).json()
    assert after["totals"]["total"] == 0.0
    # et la fiche compte passe au vert
    rev = client.get("/accounts/4411/review", headers=firm).json()
    assert rev["etat"] == "revise" and rev["stats"]["od_count"] == 1


def test_lettered_od_cannot_be_deleted(client, firm):
    od_id = client.get("/od", headers=firm).json()[0]["id"]
    r = client.delete(f"/od/{od_id}", headers=firm)
    assert r.status_code == 409 and "immuable" in r.json()["detail"] and "/reverse" in r.json()["detail"]


def test_piece_attach_and_download(client, firm):
    od_id = client.get("/od", headers=firm).json()[0]["id"]
    up = client.post(f"/od/{od_id}/piece", headers=firm,
                     files={"file": ("virement.pdf", io.BytesIO(b"pdf-bytes"), "application/pdf")})
    assert up.status_code == 201
    dl = client.get(f"/od/{od_id}/piece", headers=firm)
    assert dl.status_code == 200 and dl.content == b"pdf-bytes"


def test_od_requires_review_permission(client, firm):
    client.post("/team/accountants", headers=firm, json={
        "full_name": "Emp OD", "email": "empod@od.ma", "password": "password123",
        "role": "employee"})
    tok = client.post("/auth/login", json={"email": "empod@od.ma",
                                           "password": "password123"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post("/od", headers=h, json=REGLEMENT).status_code == 403
