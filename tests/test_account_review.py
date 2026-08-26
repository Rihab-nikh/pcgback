"""Fiche compte : état de révision justifié par des contrôles factuels,
métrique d'automatisation, marquage « revu par X », détection de doublon
avec faits nommés."""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/rev.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

_COUNTER = {"n": 0}
# pièces 0-2 : uniques, payées (lettrables) ; pièce 3 : DOUBLON de la pièce 2
NUMBERS = ["REV-0001", "REV-0002", "REV-0003", "REV-0003"]


@pytest.fixture(scope="module", autouse=True)
def fake_extractor():
    async def _fake(image, perspective=None, exercise_context=None):
        i = _COUNTER["n"]; _COUNTER["n"] += 1
        return ExtractedInvoiceData(
            invoice_type="DOIT", invoice_category="facture_achat",
            date="2026-06-18", invoice_number=NUMBERS[i % len(NUMBERS)],
            supplier_name="REV SUPPLIES", montant_brut=1_000, tva_pct=20,
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
        "firm_name": "Cabinet Revue", "full_name": "Rev Admin",
        "email": "rev@rev.ma", "password": "password123"}).json()
    h = {"Authorization": f"Bearer {reg['access_token']}"}
    cid = client.post("/clients", headers=h, json={"name": "Client Rev"}).json()["id"]
    for i in range(4):
        r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=h,
                        files=[("files", (f"r{i}.jpg", io.BytesIO(f"img-{min(i, 2)}".encode()), "image/jpeg"))])
        item = r.json()["items"][0]
        if item["status"] != "duplicate":     # le doublon reste en l'état
            review = client.post(f"/invoices/{item['invoice_id']}/review", headers=h,
                                 json={"action": "approve", "post_now": True})
            assert review.status_code == 200, review.text
            od = client.post("/od", headers=h, json={
                "journal": "BQ", "date": "2026-06-19",
                "piece": f"PAY-{i+1:04d}",
                "libelle": f"Règlement REV-{i+1:04d}",
                "lines": [
                    {"account_number": "4411", "account_label": "Fournisseurs", "side": "DEBIT", "amount": 1200.0},
                    {"account_number": "5141", "account_label": "Banques", "side": "CREDIT", "amount": 1200.0},
                ],
            })
            assert od.status_code == 201, od.text
    return h


def test_review_reports_facts_not_verdicts(client, firm):
    rev = client.get("/accounts/4411/review", headers=firm).json()
    assert rev["account_label"]
    assert rev["stats"]["entries"] >= 6 and rev["stats"]["pieces"] == 3  # doublon non approuvé
    # chaque contrôle porte des faits lisibles
    by_id = {c["id"]: c for c in rev["checks"]}
    assert by_id["equilibre"]["ok"] is True
    assert "toutes équilibrées" in by_id["equilibre"]["facts"][0]
    assert by_id["tva"]["ok"] is True
    assert "sequences" in by_id            # spécifique 4411
    # doublon : la pièce 4 n'est pas approuvée, donc hors journal -> pas d'anomalie ici
    assert by_id["doublons"]["ok"] is True
    # tiers : contrôle lettrage présent avec compte précis
    assert "non lettrée" in by_id["lettrage"]["facts"][0] or "100 %" in by_id["lettrage"]["facts"][0]


def test_etat_derives_from_checks_and_sante_bars(client, firm):
    rev = client.get("/accounts/4411/review", headers=firm).json()
    # pièces réglées non lettrées -> à vérifier (pas d'anomalie, pas de blocage)
    assert rev["etat"] == "a_verifier"
    s = rev["sante"]
    assert s["qualite"] == 100.0 and s["tva"] == 100.0 and s["pieces"] == 100.0
    assert 0 <= s["lettrage"] < 100
    m = rev["review_metrics"]
    assert m["auto_validated"] == 3 and m["needs_intervention"] == 0
    assert m["auto_pct"] == 100.0


def test_lettering_moves_etat_to_revise(client, firm):
    det = client.get("/lettrage/4411", headers=firm).json()
    for s in [x for x in det["suggestions"] if x["reason"] == "montants égaux"]:
        assert client.post("/lettrage/4411", headers=firm,
                           json={"line_refs": s["line_refs"]}).status_code == 201
    rev = client.get("/accounts/4411/review", headers=firm).json()
    assert rev["stats"]["unlettered"] == 0
    assert rev["etat"] == "revise"
    by_id = {c["id"]: c for c in rev["checks"]}
    assert by_id["lettrage"]["ok"] is True and "100 %" in by_id["lettrage"]["facts"][0]


def test_mark_reviewed_persists_and_shows(client, firm):
    r = client.post("/accounts/4411/review", headers=firm, json={"note": "RAS"})
    assert r.status_code == 201 and r.json()["etat"] == "revise"
    rev = client.get("/accounts/4411/review", headers=firm).json()
    assert rev["last_review"]["etat"] == "revise"
    assert rev["last_review"]["reviewed_by_name"] == "Rev Admin"


def test_unknown_account_404(client, firm):
    assert client.get("/accounts/9999/review", headers=firm).status_code == 404


def test_review_overview_for_mission_lead(client, firm):
    """Vue chef de mission : états par compte, tri par urgence, progression."""
    ov = client.get("/accounts/review-overview", headers=firm).json()
    assert ov["summary"]["bloque"] == 0
    accounts = {a["account_number"]: a for a in ov["accounts"]}
    assert "4411" in accounts and "5141" in accounts
    # 4411 entièrement lettré par le test précédent -> révisé
    assert accounts["4411"]["etat"] == "revise"
    assert accounts["4411"]["last_review"]["by_name"] == "Rev Admin"
    # tri : les états les plus urgents d'abord
    order = [a["etat"] for a in ov["accounts"]]
    rank = {"bloque": 0, "anomalies": 1, "a_verifier": 2, "revise": 3}
    assert order == sorted(order, key=lambda e: rank[e])
    # progression collaborateurs + métrique dirigeant
    assert ov["reviewers"][0]["name"] == "Rev Admin" and ov["reviewers"][0]["reviews"] >= 1
    assert ov["total_auto_pct"] == 100.0
