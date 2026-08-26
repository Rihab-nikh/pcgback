"""Use-case tests for v7–v10 features: organization signup, user management,
permissions, expense claims, connectors, supplier rules (auto-publish),
auto-split upload, archive, timeline, approvals, bank receipt requests,
sales category filter, accountant dashboard.

Fully offline: vision extractor monkeypatched, ACCOUNTS_OFFLINE=1.
Run: pytest tests/test_v7_use_cases.py
"""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/v7.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

FAKE = ExtractedInvoiceData(
    invoice_type="DOIT", invoice_category="facture_achat",
    date="2026-06-18", invoice_number="FAC-0001", supplier_name="MAROC SUPPLIES",
    montant_brut=1_000, tva_pct=20, payment_mode="banque",
)
_COUNTER = {"n": 0}


@pytest.fixture(autouse=True)
def fake_extractor(monkeypatch):
    async def _fake(image, perspective=None, exercise_context=None):
        _COUNTER["n"] += 1
        data = FAKE.model_copy(deep=True)
        data.invoice_number = f"FAC-{_COUNTER['n']:04d}"  # unique => no duplicates
        return data
    monkeypatch.setattr("app.main_pipeline.extract_invoice_data", _fake)
    monkeypatch.setattr("app.api.routers.invoices.ENABLE_AUTO_PUBLISH", True)
    monkeypatch.setattr("app.api.routers.invoices.ACCOUNTING_ENGINE_CERTIFIED", True)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def firm(client):
    """Organization signup with the full 'Create Organization' fields."""
    r = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet Gamma", "full_name": "Rihab Admin",
        "email": "admin@gamma.ma", "password": "password123",
        "accounting_software": "Sage", "country": "ma", "currency": "mad",
        "logo": "data:image/png;base64,iVBORw0KGgo="})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module")
def pcg_client_id(client, firm):
    r = client.post("/clients", headers=hdr(firm), json={"name": "Client Omega", "ice": "001"})
    assert r.status_code == 201
    return r.json()["id"]


def hdr(tok):
    return {"Authorization": f"Bearer {tok['access_token']}"}


def login(client, email, password="password123"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, tok, cid, name="f.jpg"):
    r = client.post(f"/invoices/bulk-upload?client_id={cid}", headers=hdr(tok),
                    files=[("files", (name, io.BytesIO(f"img-{name}".encode()), "image/jpeg"))])
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


# ── Organization signup ─────────────────────────────────────────────────────
def test_org_fields_persisted(client, firm):
    me = client.get("/auth/me", headers=hdr(firm)).json()
    f = me["firm"]
    assert f["accounting_software"] == "Sage"
    assert f["country"] == "MA" and f["currency"] == "MAD"   # normalized upper
    assert f["logo"].startswith("data:image/png")


# ── User management: invite, bulk invite, phone, deactivate ────────────────
def test_invite_user_all_fields(client, firm):
    r = client.post("/team/accountants", headers=hdr(firm), json={
        "full_name": "Sara Reviewer", "email": "sara@gamma.ma", "password": "password123",
        "role": "reviewer", "department": "Audit", "phone": "+212600000001"})
    assert r.status_code == 201, r.text
    users = client.get("/team/accountants", headers=hdr(firm)).json()
    sara = next(u for u in users if u["email"] == "sara@gamma.ma")
    assert sara["role"] == "reviewer" and sara["department"] == "Audit"
    assert sara["phone"] == "+212600000001"
    assert "expense_claims" in sara and "last_login_at" in sara


def test_bulk_invite_partial_success(client, firm):
    r = client.post("/team/invite-bulk", headers=hdr(firm), json={"users": [
        {"full_name": "Emp One", "email": "emp1@gamma.ma", "password": "password123",
         "role": "employee", "department": "Ventes"},
        {"full_name": "Dup", "email": "sara@gamma.ma", "password": "password123"},  # already exists
    ]})
    assert r.status_code == 207
    body = r.json()
    assert body["invited"] == 1
    results = {x["email"]: x for x in body["results"]}
    assert results["emp1@gamma.ma"]["created"] is True
    assert results["sara@gamma.ma"]["created"] is False


def test_deactivate_and_self_deactivation_blocked(client, firm):
    users = client.get("/team/accountants", headers=hdr(firm)).json()
    emp = next(u for u in users if u["email"] == "emp1@gamma.ma")
    me = next(u for u in users if u["email"] == "admin@gamma.ma")
    assert client.patch(f"/team/accountants/{me['id']}", headers=hdr(firm),
                        json={"is_active": False}).status_code == 400
    r = client.patch(f"/team/accountants/{emp['id']}", headers=hdr(firm), json={"is_active": False})
    assert r.status_code == 200
    # deactivated user cannot log in
    bad = client.post("/auth/login", json={"email": "emp1@gamma.ma", "password": "password123"})
    assert bad.status_code == 403
    client.patch(f"/team/accountants/{emp['id']}", headers=hdr(firm), json={"is_active": True})


# ── Permission matrix ───────────────────────────────────────────────────────
def test_permission_matrix_defaults_and_toggle(client, firm):
    m = client.get("/permissions", headers=hdr(firm)).json()
    assert set(m["roles"]) == {"business_admin", "firm_admin", "accountant", "reviewer", "employee"}
    assert m["matrix"]["firm_admin"]["users.manage"] is True
    assert m["matrix"]["employee"]["invoices.review"] is False
    # firm_admin cannot be toggled
    assert client.put("/permissions", headers=hdr(firm), json={
        "role": "firm_admin", "permission": "users.manage", "allowed": False}).status_code == 400
    # toggle employee expenses.approve on, then verify persisted
    r = client.put("/permissions", headers=hdr(firm), json={
        "role": "employee", "permission": "expenses.approve", "allowed": True})
    assert r.status_code == 200
    m2 = client.get("/permissions", headers=hdr(firm)).json()
    assert m2["matrix"]["employee"]["expenses.approve"] is True
    client.put("/permissions", headers=hdr(firm), json={
        "role": "employee", "permission": "expenses.approve", "allowed": False})


def test_role_guards(client, firm):
    """Employees can submit expenses but cannot manage users."""
    emp = login(client, "emp1@gamma.ma")
    assert client.get("/team/accountants", headers=hdr(emp)).status_code == 403
    assert client.get("/expenses/summary", headers=hdr(emp)).status_code == 200


# ── Expense claims lifecycle ────────────────────────────────────────────────
def test_expense_claim_full_lifecycle(client, firm):
    emp = login(client, "emp1@gamma.ma")
    # create draft
    c = client.post("/expenses", headers=hdr(emp), json={
        "title": "Taxi aéroport", "category": "transport", "amount": 250.5})
    assert c.status_code == 201 and c.json()["status"] == "draft"
    claim_id = c.json()["id"]
    # edit while draft
    assert client.patch(f"/expenses/{claim_id}", headers=hdr(emp),
                        json={"amount": 260.0}).json()["amount"] == 260.0
    # attach a receipt
    a = client.post(f"/expenses/{claim_id}/attachments", headers=hdr(emp),
                    files={"file": ("recu.jpg", io.BytesIO(b"receipt"), "image/jpeg")})
    assert a.status_code == 201
    att_id = a.json()["id"]
    # submit -> open; editing now rejected
    assert client.post(f"/expenses/{claim_id}/submit", headers=hdr(emp)).json()["status"] == "open"
    assert client.patch(f"/expenses/{claim_id}", headers=hdr(emp),
                        json={"amount": 1.0}).status_code == 400
    # owner cannot review their own claim; reviewer approves
    assert client.post(f"/expenses/{claim_id}/review", headers=hdr(emp),
                       json={"action": "approve"}).status_code in (400, 403)
    rev = login(client, "sara@gamma.ma")
    r = client.post(f"/expenses/{claim_id}/review", headers=hdr(rev),
                    json={"action": "approve", "note": "OK"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # attachment downloadable by reviewer
    dl = client.get(f"/expenses/attachments/{att_id}/file", headers=hdr(rev))
    assert dl.status_code == 200 and dl.content == b"receipt"
    # summary reflects the approval
    s = client.get("/expenses/summary", headers=hdr(emp)).json()
    assert s["approved"]["count"] == 1 and s["approved"]["total"] == 260.0


# ── Connectors: marketplace + email import ──────────────────────────────────
def test_connector_lifecycle(client, firm):
    cat = client.get("/connectors/catalog", headers=hdr(firm)).json()
    assert cat["connected_count"] == 0 and len(cat["suppliers"]) > 10
    assert client.post("/connectors/maroc_telecom/connect", headers=hdr(firm)).status_code == 201
    assert client.post("/connectors/maroc_telecom/connect", headers=hdr(firm)).status_code == 409
    assert client.post("/connectors/nope/connect", headers=hdr(firm)).status_code == 404
    sync = client.post("/connectors/maroc_telecom/sync", headers=hdr(firm)).json()
    assert sync["fetched"] == 0  # honest stub
    cat2 = client.get("/connectors/catalog", headers=hdr(firm)).json()
    mt = next(s for s in cat2["suppliers"] if s["key"] == "maroc_telecom")
    assert mt["connected"] and mt["last_sync_at"]
    assert client.delete("/connectors/maroc_telecom", headers=hdr(firm)).status_code == 204


def test_email_import_address(client, firm):
    r = client.get("/connectors/email-import", headers=hdr(firm)).json()
    assert r["address"].endswith("@inbox.pcgmaroc.ai")
    assert "+multi@" in r["multi_address"]
    assert r["active"] is False and r["history"] == []


# ── Supplier rules + auto-publish enforcement ───────────────────────────────
def test_supplier_rule_auto_publish(client, firm, pcg_client_id):
    # without a rule: needs_review
    item = upload(client, firm, pcg_client_id, "before-rule.jpg")
    assert item["status"] == "needs_review"
    # create an auto-publish rule for the faked supplier
    r = client.post("/rules", headers=hdr(firm), json={
        "supplier": "MAROC SUPPLIES", "payment_account": "5141",
        "rule_description": "Fournitures", "auto_publish": True})
    assert r.status_code == 201
    rules = client.get("/rules", headers=hdr(firm)).json()
    rule = next(x for x in rules if x["supplier"] == "maroc supplies")
    assert rule["auto_publish"] == 1 and rule["rule_source"] == "manual_override"
    # with the rule: VALID invoice is published automatically
    item2 = upload(client, firm, pcg_client_id, "after-rule.jpg")
    assert item2["status"] == "approved"
    # delete rule -> back to needs_review
    assert client.delete("/rules/maroc supplies", headers=hdr(firm)).status_code == 204
    item3 = upload(client, firm, pcg_client_id, "after-delete.jpg")
    assert item3["status"] == "needs_review"


# ── Auto-split upload ───────────────────────────────────────────────────────
def test_upload_auto_split_pdf(client, firm, pcg_client_id):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.add_blank_page(width=595, height=842)
    buf = io.BytesIO(); w.write(buf)
    r = client.post(f"/invoices/upload-split?client_id={pcg_client_id}", headers=hdr(firm),
                    files={"file": ("lot.pdf", io.BytesIO(buf.getvalue()), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"] == 2 and body["total"] == 2
    assert {i["filename"] for i in body["items"]} == {"lot_p1.pdf", "lot_p2.pdf"}
    # non-PDF rejected
    bad = client.post(f"/invoices/upload-split?client_id={pcg_client_id}", headers=hdr(firm),
                      files={"file": ("x.jpg", io.BytesIO(b"img"), "image/jpeg")})
    assert bad.status_code == 422


# ── Archive & timeline ──────────────────────────────────────────────────────
def test_archive_restore_and_list_exclusion(client, firm, pcg_client_id):
    item = upload(client, firm, pcg_client_id, "to-archive.jpg")
    inv_id = item["invoice_id"]
    assert client.post(f"/invoices/{inv_id}/archive", headers=hdr(firm)).json()["is_archived"]
    live_ids = {i["id"] for i in client.get("/invoices?limit=200", headers=hdr(firm)).json()["items"]}
    archived_ids = {i["id"] for i in
                    client.get("/invoices?archived=true&limit=200", headers=hdr(firm)).json()["items"]}
    assert inv_id not in live_ids and inv_id in archived_ids
    assert client.post(f"/invoices/{inv_id}/archive?archived=false",
                       headers=hdr(firm)).json()["is_archived"] is False


def test_timeline_records_lifecycle(client, firm):
    events = client.get("/timeline", headers=hdr(firm)).json()
    actions = {e["action"] for e in events}
    assert "invoice.auto_publish" in actions      # from the rules test
    assert "invoice.archive" in actions and "invoice.restore" in actions


# ── Approvals: queue + workflow routing ─────────────────────────────────────
def test_approval_workflow_routing_and_bulk(client, firm):
    users = client.get("/team/accountants", headers=hdr(firm)).json()
    sara = next(u for u in users if u["email"] == "sara@gamma.ma")
    wf = client.post("/approvals/workflows", headers=hdr(firm), json={
        "name": "Gros achats", "supplier": "MAROC SUPPLIES", "min_amount": 100,
        "approvers": [sara["id"]], "priority": 5})
    assert wf.status_code == 201
    q = client.get("/approvals/queue", headers=hdr(firm)).json()
    assert q["workflow_count"] >= 1
    routed = [i for i in q["invoices"] if i["workflow"]]
    assert routed and routed[0]["workflow"]["name"] == "Gros achats"
    assert routed[0]["workflow"]["approver_names"] == ["Sara Reviewer"]
    # unknown approver rejected
    assert client.post("/approvals/workflows", headers=hdr(firm), json={
        "name": "Bad", "approvers": ["ghost"]}).status_code == 404
    wf_id = client.get("/approvals/workflows", headers=hdr(firm)).json()[0]["id"]
    assert client.delete(f"/approvals/workflows/{wf_id}", headers=hdr(firm)).status_code == 204


# ── Bank: receipt request ───────────────────────────────────────────────────
def test_bank_request_receipt(client, firm):
    acc = client.post("/treasury/accounts", headers=hdr(firm), json={
        "name": "Compte Gamma", "bank_name": "CIH"})
    assert acc.status_code == 201
    csv_body = "date;libelle;montant;reference\n01/07/2026;FRAIS DIVERS;-42,00;X1\n"
    imp = client.post(f"/treasury/accounts/{acc.json()['id']}/import", headers=hdr(firm),
                      files={"file": ("r.csv", io.BytesIO(csv_body.encode()), "text/csv")},
                      params={"opening_balance": "0", "closing_balance": "-42.00"})
    assert imp.status_code == 201
    tx = client.get("/treasury/transactions", headers=hdr(firm)).json()["items"][0]
    r = client.post(f"/treasury/transactions/{tx['id']}/request-receipt", headers=hdr(firm))
    assert r.status_code == 200 and r.json()["requested"]
    notifs = client.get("/notifications", headers=hdr(firm)).json()
    assert any(n["kind"] == "receipt_requested" for n in notifs)


# ── Sales inbox: category filter + tva column ───────────────────────────────
def test_category_filter_and_tva_column(client, firm):
    achats = client.get("/invoices?category=facture_achat&limit=200", headers=hdr(firm)).json()
    assert achats["total"] > 0
    assert all(i["category"] == "facture_achat" for i in achats["items"])
    assert achats["items"][0]["tva"] == 200.0     # 1000 HT @ 20%
    ventes = client.get("/invoices?category=facture_vente&limit=200", headers=hdr(firm)).json()
    assert ventes["total"] == 0


# ── Accountant dashboard ────────────────────────────────────────────────────
def test_accountant_dashboard_cards(client, firm):
    d = client.get("/dashboard/accountant", headers=hdr(firm)).json()
    cards = d["cards"]
    for key in ("documents_waiting", "published_today", "needs_review", "auto_published",
                "ocr_accuracy", "time_saved_hours", "connected_suppliers", "bank_matches"):
        assert key in cards
    assert cards["auto_published"] >= 1           # the rules test auto-published one
    assert d["monthly_documents"] and d["spend_categories"]
    assert d["supplier_distribution"][0]["supplier"] == "MAROC SUPPLIES"


# ── Cross-tenant isolation for the new resources ────────────────────────────
def test_new_resources_tenant_isolation(client, firm):
    beta = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet Delta", "full_name": "Delta Admin",
        "email": "d@delta.ma", "password": "password123"}).json()
    assert client.get("/rules", headers=hdr(beta)).json() == []
    assert client.get("/expenses", headers=hdr(beta)).json() == []
    assert client.get("/approvals/queue", headers=hdr(beta)).json()["invoices"] == []
    assert client.get("/connectors/catalog", headers=hdr(beta)).json()["connected_count"] == 0
    assert client.get("/timeline", headers=hdr(beta)).json() == []
