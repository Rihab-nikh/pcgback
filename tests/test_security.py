"""Security tests: live permission-matrix enforcement + login rate limiting.

The point of these tests: toggling a permission in the matrix must actually
change authorization — the UI and the backend may never disagree.
"""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/sec.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

FAKE = ExtractedInvoiceData(
    invoice_type="DOIT", invoice_category="facture_achat",
    date="2026-06-18", invoice_number="SEC-0001", supplier_name="SEC SUPPLIES",
    montant_brut=1_000, tva_pct=20)


@pytest.fixture(autouse=True)
def fake_extractor(monkeypatch):
    async def _fake(image, perspective=None, exercise_context=None):
        return FAKE.model_copy(deep=True)
    monkeypatch.setattr("app.main_pipeline.extract_invoice_data", _fake)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet Secu", "full_name": "Sec Admin",
        "email": "admin@secu.ma", "password": "password123"})
    assert r.status_code == 201
    return r.json()


def hdr(tok):
    return {"Authorization": f"Bearer {tok['access_token']}"}


def toggle(client, admin, role, permission, allowed):
    r = client.put("/permissions", headers=hdr(admin),
                   json={"role": role, "permission": permission, "allowed": allowed})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module")
def accountant(client, admin):
    r = client.post("/team/accountants", headers=hdr(admin), json={
        "full_name": "Acc Sec", "email": "acc@secu.ma", "password": "password123",
        "role": "accountant"})
    assert r.status_code == 201
    login = client.post("/auth/login", json={"email": "acc@secu.ma", "password": "password123"})
    return login.json()


@pytest.fixture(scope="module")
def client_id(client, admin, accountant):
    # Assigned to the accountant — accountants only see their own clients.
    users = client.get("/team/accountants", headers=hdr(admin)).json()
    acc_id = next(u["id"] for u in users if u["email"] == "acc@secu.ma")
    return client.post("/clients", headers=hdr(admin),
                       json={"name": "Client Secu", "assigned_to": acc_id}).json()["id"]


# ── The matrix drives authorization, live ───────────────────────────────────
def test_toggle_upload_permission_changes_authorization(client, admin, accountant, client_id):
    def try_upload():
        return client.post(f"/invoices/bulk-upload?client_id={client_id}", headers=hdr(accountant),
                           files=[("files", ("f.jpg", io.BytesIO(b"img"), "image/jpeg"))])
    assert try_upload().status_code == 200          # default: accountant may upload
    toggle(client, admin, "accountant", "invoices.upload", False)
    r = try_upload()
    assert r.status_code == 403 and "invoices.upload" in r.json()["detail"]
    toggle(client, admin, "accountant", "invoices.upload", True)
    assert try_upload().status_code == 200          # restored


def test_toggle_review_permission(client, admin, accountant, client_id):
    inv = client.get("/invoices?status=needs_review", headers=hdr(admin)).json()["items"][0]
    toggle(client, admin, "accountant", "invoices.review", False)
    assert client.post(f"/invoices/{inv['id']}/review", headers=hdr(accountant),
                       json={"action": "approve"}).status_code == 403
    toggle(client, admin, "accountant", "invoices.review", True)
    assert client.post(f"/invoices/{inv['id']}/review", headers=hdr(accountant),
                       json={"action": "approve"}).status_code == 200


def test_toggle_journal_and_reports_view(client, admin, accountant):
    assert client.get("/journal", headers=hdr(accountant)).status_code == 200
    toggle(client, admin, "accountant", "journal.view", False)
    assert client.get("/journal", headers=hdr(accountant)).status_code == 403
    assert client.get("/exports/journal.csv", headers=hdr(accountant)).status_code == 403
    toggle(client, admin, "accountant", "journal.view", True)

    toggle(client, admin, "accountant", "reports.view", False)
    assert client.get("/reports/monthly", headers=hdr(accountant)).status_code == 403
    toggle(client, admin, "accountant", "reports.view", True)


def test_grant_users_manage_to_accountant(client, admin, accountant):
    """The matrix can also EXPAND access: grant an accountant user management."""
    assert client.get("/team/accountants", headers=hdr(accountant)).status_code == 403
    toggle(client, admin, "accountant", "users.manage", True)
    assert client.get("/team/accountants", headers=hdr(accountant)).status_code == 200
    toggle(client, admin, "accountant", "users.manage", False)
    assert client.get("/team/accountants", headers=hdr(accountant)).status_code == 403


def test_firm_admin_immune_to_matrix(client, admin):
    """firm_admin can never lock itself out."""
    assert client.put("/permissions", headers=hdr(admin), json={
        "role": "firm_admin", "permission": "users.manage", "allowed": False}).status_code == 400
    assert client.get("/team/accountants", headers=hdr(admin)).status_code == 200


def test_expense_submit_permission(client, admin, accountant):
    toggle(client, admin, "accountant", "expenses.submit", False)
    assert client.post("/expenses", headers=hdr(accountant),
                       json={"title": "Taxi", "amount": 10}).status_code == 403
    toggle(client, admin, "accountant", "expenses.submit", True)
    assert client.post("/expenses", headers=hdr(accountant),
                       json={"title": "Taxi", "amount": 10}).status_code == 201


def test_super_admin_never_passes_permission_guards(client):
    sa = client.post("/auth/login", json={
        "email": os.environ.get("SUPER_ADMIN_EMAIL", "admin@pcg-maroc.ai"),
        "password": os.environ.get("SUPER_ADMIN_PASSWORD", "change-me-now")}).json()
    assert client.get("/team/accountants", headers=hdr(sa)).status_code == 403
    assert client.get("/journal", headers=hdr(sa)).status_code == 403


# ── Login rate limiting ─────────────────────────────────────────────────────
def test_login_rate_limit_and_audit(client, admin):
    email = "ratelimit@secu.ma"
    client.post("/team/accountants", headers=hdr(admin), json={
        "full_name": "Rate Limited", "email": email, "password": "password123"})
    # 5 failures allowed...
    for _ in range(5):
        assert client.post("/auth/login",
                           json={"email": email, "password": "wrong"}).status_code == 401
    # ...6th attempt is throttled, even with the CORRECT password
    assert client.post("/auth/login",
                       json={"email": email, "password": "wrong"}).status_code == 429
    assert client.post("/auth/login",
                       json={"email": email, "password": "password123"}).status_code == 429
    # failures + throttling are audited
    events = client.get("/firm/audit?limit=100", headers=hdr(admin)).json()
    actions = [e["action"] for e in events]
    assert "auth.login_failed" in actions


def test_rate_limit_is_per_account(client, admin):
    """Hammering one account must not lock out another."""
    r = client.post("/auth/login", json={"email": "admin@secu.ma", "password": "password123"})
    assert r.status_code == 200
