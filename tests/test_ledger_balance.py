"""Grand livre + balance générale: pure aggregations of the generated journal —
they must always agree with it (same totals, balanced)."""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/gl.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

_COUNTER = {"n": 0}


# Module-scoped patch: the seeding fixture below is module-scoped too, so a
# function-scoped autouse monkeypatch would run AFTER it (higher scopes first).
@pytest.fixture(scope="module", autouse=True)
def fake_extractor():
    async def _fake(image, perspective=None, exercise_context=None):
        _COUNTER["n"] += 1
        return ExtractedInvoiceData(
            invoice_type="DOIT", invoice_category="facture_achat",
            date="2026-06-18", invoice_number=f"GL-{_COUNTER['n']:04d}",
            supplier_name="GL SUPPLIES", montant_brut=1_000, tva_pct=20,
            payment_mode="banque")
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
        "firm_name": "Cabinet GL", "full_name": "GL Admin",
        "email": "gl@gl.ma", "password": "password123"}).json()
    h = {"Authorization": f"Bearer {reg['access_token']}"}
    cid = client.post("/clients", headers=h, json={"name": "Client GL"}).json()["id"]
    for i in range(3):
        r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=h,
                        files=[("files", (f"g{i}.jpg", io.BytesIO(f"ledger-{i}".encode()), "image/jpeg"))])
        inv_id = r.json()["items"][0]["invoice_id"]
        review = client.post(f"/invoices/{inv_id}/review", headers=h, json={"action": "approve", "post_now": True})
        assert review.status_code == 200, review.text
        od = client.post("/od", headers=h, json={
            "journal": "BQ", "date": "2026-06-19", "piece": f"GL-PAY-{i+1}",
            "libelle": f"Règlement GL-{i+1:04d}",
            "lines": [
                {"account_number": "4411", "account_label": "Fournisseurs", "side": "DEBIT", "amount": 1200.0},
                {"account_number": "5141", "account_label": "Banques", "side": "CREDIT", "amount": 1200.0},
            ],
        })
        assert od.status_code == 201, od.text
    return h


def test_general_ledger_aggregates_and_orders(client, firm):
    gl = client.get("/ledger", headers=firm).json()
    assert gl["count"] > 0
    numbers = [a["account_number"] for a in gl["accounts"]]
    assert numbers == sorted(numbers)
    banque = next(a for a in gl["accounts"] if a["account_number"] == "5141")
    assert banque["classe"] == "5" and banque["entries"] >= 3
    # every account: solde = debit - credit, sens coherent
    for a in gl["accounts"]:
        assert a["solde"] == pytest.approx(round(a["total_debit"] - a["total_credit"], 2))
        assert a["sens"] == ("D" if a["solde"] > 0 else "C" if a["solde"] < 0 else "=")


def test_account_ledger_running_balance(client, firm):
    detail = client.get("/ledger/5141", headers=firm).json()
    assert detail["account_label"]
    assert len(detail["lines"]) >= 3
    # running balance recomputes line by line
    run = 0.0
    for line in detail["lines"]:
        run += line["amount"] if line["side"] == "DEBIT" else -line["amount"]
        assert line["solde"] == pytest.approx(round(run, 2))
    assert detail["solde"] == pytest.approx(round(run, 2))


def test_trial_balance_is_balanced_and_matches_journal(client, firm):
    bal = client.get("/balance", headers=firm).json()
    assert bal["balanced"] is True
    journal = client.get("/journal", headers=firm).json()
    assert bal["total_debit"] == pytest.approx(journal["total_debit"])
    assert bal["total_credit"] == pytest.approx(journal["total_credit"])
    # classes sum to the same totals
    assert sum(c["total_debit"] for c in bal["classes"]) == pytest.approx(bal["total_debit"])
    assert {c["classe"] for c in bal["classes"]} >= {"3", "4", "5", "6"}


def test_ledger_respects_journal_view_permission(client, firm):
    """The GL/balance sit behind the same journal.view permission."""
    # create an employee: no journal.view by default
    client.post("/team/accountants", headers=firm, json={
        "full_name": "Emp GL", "email": "empgl@gl.ma", "password": "password123",
        "role": "employee"})
    tok = client.post("/auth/login", json={"email": "empgl@gl.ma",
                                           "password": "password123"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.get("/ledger", headers=h).status_code == 403
    assert client.get("/balance", headers=h).status_code == 403


def test_financial_statements_are_a_balance_projection(client, firm):
    """P6 : bilan/CPC = projection de la balance. Actif = Passif (résultat
    inclus), résultat = produits - charges, et chaque rubrique se déroule
    en comptes réels."""
    fs = client.get("/financial-statements", headers=firm).json()
    b, cpc = fs["bilan"], fs["cpc"]
    assert b["equilibre"] is True
    assert b["total_actif"] == pytest.approx(b["total_passif"])
    assert cpc["resultat"] == pytest.approx(cpc["produits"] - cpc["charges"])
    # mapping PCG : nos comptes connus sont au bon endroit
    def find(state, number):
        for r in state:
            for a in r["accounts"]:
                if a["account_number"] == number:
                    return r["rubrique"], a["amount"]
        return None
    # (dans ce jeu, banque et fournisseurs sont soldés à 0 par la double
    #  perspective — on vérifie le mapping sur des comptes au solde non nul)
    rub, amt = find(b["actif"], "34552")           # TVA récupérable/charges -> créances
    assert rub == "Créances de l'actif circulant" and amt > 0
    assert find(b["passif"], "4455") is None       # no seller-side output VAT in buyer books
    rub, amt = find(b["passif"], "5141")           # bank overdraft from explicit settlements
    assert rub == "Trésorerie — Passif" and amt > 0
    rub, amt = find(cpc["rubriques"], "6111")      # achats -> charges
    assert rub == "Charges d'exploitation" and amt > 0
    assert find(b["actif"], "5141") or find(b["passif"], "5141")  # mappée quelque part
    # le résultat boucle le passif
    assert any("Résultat net" in r["rubrique"] for r in b["passif"])
    # la balance reste la source unique : total actif == somme des soldes débiteurs
    bal = client.get("/balance", headers=firm).json()
    assert fs["source"].startswith("projection")
    assert b["total_actif"] <= bal["total_debit"] + 0.01
