"""Balance âgée : buckets par ancienneté, exclusion des lignes lettrées,
risque justifié par des raisons, décomposition par facture."""
import io
import os
import tempfile
from datetime import date, timedelta

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/aging.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

TODAY = date.today()
# Factures 0-2 : impayées (payment none) à 10/45/100 jours -> une par tranche.
# Facture 3 : PAYÉE (banque) -> ses lignes 4411 s'équilibrent : hors balance âgée.
AGES = [10, 45, 100, 5]
MODES = ["none", "none", "none", "banque"]
_COUNTER = {"n": 0}


@pytest.fixture(scope="module", autouse=True)
def fake_extractor():
    async def _fake(image, perspective=None, exercise_context=None):
        i = _COUNTER["n"]; _COUNTER["n"] += 1
        d = (TODAY - timedelta(days=AGES[i % len(AGES)])).isoformat()
        return ExtractedInvoiceData(
            invoice_type="DOIT", invoice_category="facture_achat",
            date=d, due_date=d, invoice_number=f"AGE-{i:04d}",
            supplier_name=("FOURNISSEUR PAYE" if MODES[i % len(MODES)] == "banque"
                           else f"FOURNISSEUR {AGES[i % len(AGES)]}J"),
            montant_brut=(1_250 if MODES[i % len(MODES)] == "banque" else 1_000),
            tva_pct=20, payment_mode=MODES[i % len(MODES)])
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
        "firm_name": "Cabinet Aging", "full_name": "Age Admin",
        "email": "age@age.ma", "password": "password123"}).json()
    h = {"Authorization": f"Bearer {reg['access_token']}"}
    cid = client.post("/clients", headers=h, json={"name": "Client Age"}).json()["id"]
    for i in range(4):
        r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=h,
                        files=[("files", (f"a{i}.jpg", io.BytesIO(f"aging-{i}".encode()), "image/jpeg"))])
        inv_id = r.json()["items"][0]["invoice_id"]
        review = client.post(f"/invoices/{inv_id}/review", headers=h, json={"action": "approve", "post_now": True})
        assert review.status_code == 200, review.text
    # Payment mode is not evidence. Create an explicit bank-style manual settlement
    # for the fourth invoice, then letter the unique 1,500 MAD pair.
    od = client.post("/od", headers=h, json={
        "journal": "BQ", "date": TODAY.isoformat(), "piece": "PAY-AGE-0003",
        "libelle": "Règlement fournisseur payé",
        "lines": [
            {"account_number": "4411", "account_label": "Fournisseurs", "side": "DEBIT", "amount": 1500.0},
            {"account_number": "5141", "account_label": "Banques", "side": "CREDIT", "amount": 1500.0},
        ],
    })
    assert od.status_code == 201, od.text
    det = client.get("/lettrage/4411", headers=h).json()
    paid = next(x for x in det["suggestions"] if x["reason"] == "montants égaux" and x["total"] == 1500.0)
    assert client.post("/lettrage/4411", headers=h, json={"line_refs": paid["line_refs"]}).status_code == 201
    return h


def test_buckets_by_age_and_risk_reasons(client, firm):
    aging = client.get("/aging?kind=fournisseurs", headers=firm).json()
    rows = {r["name"]: r for r in aging["rows"]}
    assert set(rows) == {"FOURNISSEUR 10J", "FOURNISSEUR 45J", "FOURNISSEUR 100J"}

    r10, r45, r100 = rows["FOURNISSEUR 10J"], rows["FOURNISSEUR 45J"], rows["FOURNISSEUR 100J"]
    assert r10["b0_30"] > 0 and r10["b31_60"] == 0 and r10["risk"] == "faible"
    assert r45["b31_60"] > 0 and r45["b0_30"] == 0 and r45["risk"] == "faible"
    assert r100["b90"] > 0 and r100["risk"] == "eleve"

    # risque justifié : des raisons lisibles, jamais un score opaque
    assert any("90 jours" in x for x in r100["reasons"])
    assert any("pièce la plus ancienne : 100 jours" in x for x in r100["reasons"])

    # décomposition par facture : le "pourquoi ce solde" est fourni
    inv = r100["invoices"][0]
    assert inv["days_overdue"] == 100 and inv["amount"] > 0 and inv["invoice_id"]

    # totaux cohérents
    t = aging["totals"]
    assert t["total"] == pytest.approx(t["b0_30"] + t["b31_60"] + t["b61_90"] + t["b90"])


def test_settled_invoices_are_out_of_the_aging(client, firm):
    """Une pièce réglée (débit = crédit sur 4411) est HORS balance âgée,
    lettrée ou non ; et son lettrage n'y change rien — définition comptable."""
    aging = client.get("/aging?kind=fournisseurs", headers=firm).json()
    names = {r["name"] for r in aging["rows"]}
    assert "FOURNISSEUR PAYE" not in names           # réglée => encours nul
    # les 3 impayées valent chacune TTC 1200 (1000 + 20% TVA)
    assert aging["totals"]["total"] == pytest.approx(3 * 1200.0)
    # Le règlement est une écriture BQ explicite, puis lettrée contre la facture.
    det = client.get("/lettrage/4411", headers=firm).json()
    assert any(g["total"] == 1500.0 for g in det["groups"])
    after = client.get("/aging?kind=fournisseurs", headers=firm).json()
    assert after["totals"]["total"] == pytest.approx(3 * 1200.0)


def test_kind_validation_and_permission(client, firm):
    assert client.get("/aging?kind=nimporte", headers=firm).status_code == 422
    client.post("/team/accountants", headers=firm, json={
        "full_name": "Emp Age", "email": "empage@age.ma", "password": "password123",
        "role": "employee"})
    tok = client.post("/auth/login", json={"email": "empage@age.ma",
                                           "password": "password123"}).json()
    assert client.get("/aging", headers={
        "Authorization": f"Bearer {tok['access_token']}"}).status_code == 403
