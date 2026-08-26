"""Lettrage: suggestions équilibrées, pose de code, contrôle d'équilibre,
double-lettrage refusé, délettrage."""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/let.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

_COUNTER = {"n": 0}


@pytest.fixture(scope="module", autouse=True)
def fake_extractor():
    async def _fake(image, perspective=None, exercise_context=None):
        _COUNTER["n"] += 1
        return ExtractedInvoiceData(
            invoice_type="DOIT", invoice_category="facture_achat",
            date="2026-06-18", invoice_number=f"LET-{_COUNTER['n']:04d}",
            supplier_name="LET SUPPLIES", montant_brut=1_000, tva_pct=20,
            payment_mode="banque")   # mode de paiement informatif; règlement créé séparément
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
        "firm_name": "Cabinet Lettrage", "full_name": "Let Admin",
        "email": "let@let.ma", "password": "password123"}).json()
    h = {"Authorization": f"Bearer {reg['access_token']}"}
    cid = client.post("/clients", headers=h, json={"name": "Client Let"}).json()["id"]
    for i in range(2):
        r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=h,
                        files=[("files", (f"l{i}.jpg", io.BytesIO(f"let-{i}".encode()), "image/jpeg"))])
        inv_id = r.json()["items"][0]["invoice_id"]
        review = client.post(f"/invoices/{inv_id}/review", headers=h, json={"action": "approve", "post_now": True})
        assert review.status_code == 200, review.text
        od = client.post("/od", headers=h, json={
            "journal": "BQ", "date": "2026-06-19", "piece": f"LET-PAY-{i+1}",
            "libelle": f"Règlement LET-{i+1:04d}",
            "lines": [
                {"account_number": "4411", "account_label": "Fournisseurs", "side": "DEBIT", "amount": 1200.0},
                {"account_number": "5141", "account_label": "Banques", "side": "CREDIT", "amount": 1200.0},
            ],
        })
        assert od.status_code == 201, od.text
    return h


def test_tiers_accounts_listed(client, firm):
    accounts = client.get("/lettrage/accounts", headers=firm).json()
    numbers = {a["account_number"] for a in accounts}
    assert "4411" in numbers                       # fournisseurs
    assert all(a["account_number"][0] in "34" for a in accounts)
    f4411 = next(a for a in accounts if a["account_number"] == "4411")
    assert f4411["lines"] >= 4 and f4411["lettered"] == 0


def test_suggestions_are_balanced_same_piece(client, firm):
    det = client.get("/lettrage/4411", headers=firm).json()
    assert det["unlettered"] >= 4
    assert len(det["suggestions"]) >= 2
    s = det["suggestions"][0]
    assert s["reason"] == "montants égaux" and s["total"] > 0
    # la suggestion référence des lignes des deux sens
    sides = set()
    lines_by_ref = {(l["invoice_id"], l["entry_idx"], l["line_idx"]): l for l in det["lines"]}
    for ref in s["line_refs"]:
        sides.add(lines_by_ref[(ref["invoice_id"], ref["entry_idx"], ref["line_idx"])]["side"])
    assert sides == {"DEBIT", "CREDIT"}


def test_letter_suggestion_then_lines_marked(client, firm):
    det = client.get("/lettrage/4411", headers=firm).json()
    s = det["suggestions"][0]
    r = client.post("/lettrage/4411", headers=firm, json={"line_refs": s["line_refs"]})
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "A" and r.json()["total"] == s["total"]
    det2 = client.get("/lettrage/4411", headers=firm).json()
    lettered = [l for l in det2["lines"] if l["lettre"] == "A"]
    assert len(lettered) == len(s["line_refs"])
    assert det2["unlettered"] == det["unlettered"] - len(s["line_refs"])
    assert det2["groups"][0]["code"] == "A"


def test_unbalanced_and_double_lettering_rejected(client, firm):
    det = client.get("/lettrage/4411", headers=firm).json()
    free = [l for l in det["lines"] if not l["lettre"]]
    debit = next(l for l in free if l["side"] == "DEBIT")
    other_debit_or_same = [l for l in free if l["side"] == "DEBIT"]
    # déséquilibré: deux débits sans crédit
    if len(other_debit_or_same) >= 2:
        refs = [{"invoice_id": l["invoice_id"], "entry_idx": l["entry_idx"], "line_idx": l["line_idx"]}
                for l in other_debit_or_same[:2]]
    else:
        refs = [{"invoice_id": debit["invoice_id"], "entry_idx": debit["entry_idx"], "line_idx": debit["line_idx"]}] * 2
    assert client.post("/lettrage/4411", headers=firm, json={"line_refs": refs}).status_code == 422
    # double lettrage: relettrer des lignes du code A
    lettered = [l for l in det["lines"] if l["lettre"] == "A"]
    refs_a = [{"invoice_id": l["invoice_id"], "entry_idx": l["entry_idx"], "line_idx": l["line_idx"]}
              for l in lettered]
    assert client.post("/lettrage/4411", headers=firm, json={"line_refs": refs_a}).status_code == 409


def test_unletter_frees_lines(client, firm):
    assert client.delete("/lettrage/4411/A", headers=firm).status_code == 204
    det = client.get("/lettrage/4411", headers=firm).json()
    assert all(l["lettre"] != "A" for l in det["lines"])
    assert det["groups"] == []
    assert client.delete("/lettrage/4411/A", headers=firm).status_code == 404
