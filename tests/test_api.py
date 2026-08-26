"""End-to-end API tests: auth -> team -> clients -> upload -> review -> reports.

Runs fully offline: the vision extractor is monkeypatched with a deterministic
fake, and ACCOUNTS_OFFLINE=1 makes account resolution use PCG fallbacks.
Run: pytest tests/test_api.py
"""
import io
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = f"{_tmp}/api.db"
os.environ["STORAGE_BACKEND"] = "memory"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.invoice import ExtractedInvoiceData  # noqa: E402

FAKE = ExtractedInvoiceData(
    invoice_type="DOIT", invoice_category="facture_achat",
    date="2026-06-18", invoice_number="FAC-0142", supplier_name="TECHNO BUREAU",
    montant_brut=10_000, remise_pct=10, escompte_pct=2, tva_pct=20,
    is_immobilisation=True, immobilisation_type="it", payment_mode="banque",
)


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
def firm(client):
    r = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet Alpha", "full_name": "Admin",
        "email": "admin@alpha.ma", "password": "password123"})
    assert r.status_code == 201, r.text
    return r.json()


def hdr(tok):
    return {"Authorization": f"Bearer {tok['access_token']}"}


def test_login_and_me(client, firm):
    r = client.post("/auth/login", json={"email": "admin@alpha.ma", "password": "password123"})
    assert r.status_code == 200
    me = client.get("/auth/me", headers=hdr(r.json()))
    assert me.json()["role"] == "firm_admin"
    assert client.post("/auth/login", json={"email": "admin@alpha.ma", "password": "wrong"}).status_code == 401


def test_full_invoice_workflow(client, firm):
    h = hdr(firm)
    # Firm admin creates an accountant and a client assigned to them
    acc = client.post("/team/accountants", headers=h, json={
        "full_name": "Ahmed", "email": "ahmed@alpha.ma", "password": "password123"})
    assert acc.status_code == 201
    cl = client.post("/clients", headers=h, json={
        "name": "Atlas Distribution", "ice": "0015234", "assigned_to": acc.json()["id"]})
    assert cl.status_code == 201
    client_id = cl.json()["id"]

    # Accountant logs in and uploads an invoice
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    up = client.post(f"/invoices/upload?client_id={client_id}", headers=hdr(at),
                     files={"file": ("fac.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")})
    assert up.status_code == 201, up.text
    inv = up.json()
    assert inv["status"] == "needs_review"
    assert inv["verdict"] == "VALID"
    assert inv["supplier_name"] == "TECHNO BUREAU"
    assert inv["ttc"] == 10_584.0          # 10 000 → remise 10% → escompte 2% → TVA 20%
    assert inv["confidence"] == 1.0

    # Full response persisted (journal, checks, report)
    detail = client.get(f"/invoices/{inv['id']}", headers=hdr(at)).json()
    assert detail["response"]["step4_journal_entries"][0]["lines"]
    assert any(l["account_number"] == "2355"
               for e in detail["response"]["step4_journal_entries"] for l in e["lines"])

    # Duplicate detection on second upload of the same invoice
    dup = client.post(f"/invoices/upload?client_id={client_id}", headers=hdr(at),
                      files={"file": ("fac2.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")})
    assert dup.json()["is_duplicate_of"] == inv["id"]

    # Approve → journal ledger + CSV export + dashboard
    r = client.post(f"/invoices/{inv['id']}/review", headers=hdr(at), json={"action": "approve", "post_now": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["posting_status"] == "posted"
    ledger = client.get("/journal", headers=hdr(at)).json()
    assert ledger["balanced"] and len(ledger["lines"]) > 0
    csv_r = client.get("/exports/journal.csv", headers=hdr(at))
    assert csv_r.status_code == 200 and "Compte" in csv_r.text
    dash = client.get("/dashboard", headers=hdr(at)).json()
    assert dash["stats"]["approved"] == 1
    notifs = client.get("/notifications", headers=hdr(at)).json()
    assert any(n["kind"] == "duplicate_detected" for n in notifs)


def test_tenant_isolation(client, firm):
    """Firm Beta can never see Firm Alpha's data — 404, not 403."""
    beta = client.post("/auth/register-firm", json={
        "firm_name": "Cabinet Beta", "full_name": "Beta Admin", "email": "b@beta.ma",
        "password": "password123"}).json()
    alpha_clients = client.get("/clients", headers=hdr(firm)).json()
    alpha_client_id = alpha_clients[0]["id"]
    assert client.get(f"/clients/{alpha_client_id}", headers=hdr(beta)).status_code == 404
    assert client.get("/invoices", headers=hdr(beta)).json()["total"] == 0
    alpha_inv = client.get("/invoices", headers=hdr(firm)).json()["items"][0]["id"]
    assert client.get(f"/invoices/{alpha_inv}", headers=hdr(beta)).status_code == 404


def test_role_boundaries(client, firm):
    # Accountant cannot create clients or accountants
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    assert client.post("/clients", headers=hdr(at), json={"name": "X"}).status_code == 403
    assert client.get("/team/accountants", headers=hdr(at)).status_code == 403
    # Firm admin cannot access platform endpoints
    assert client.get("/platform/stats", headers=hdr(firm)).status_code == 403
    # Super admin can access platform, not firm data
    sa = client.post("/auth/login", json={
        "email": os.environ.get("SUPER_ADMIN_EMAIL", "admin@pcg-maroc.ai"),
        "password": os.environ.get("SUPER_ADMIN_PASSWORD", "change-me-now")}).json()
    assert client.get("/platform/stats", headers=hdr(sa)).status_code == 200
    assert client.get("/platform/firms", headers=hdr(sa)).status_code == 200
    assert client.get("/invoices", headers=hdr(sa)).status_code == 403
    # A fresh client has neither Authorization header nor the login cookie
    # accumulated by this module-scoped client.
    with TestClient(app) as anon:
        assert anon.get("/dashboard").status_code == 401


def test_inline_edit_recomputes_pipeline(client, firm):
    """PATCH /invoices/{id}/extraction re-runs compute/journal/validation."""
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    inv_id = client.get("/invoices?status=needs_review", headers=hdr(at)).json()["items"][0]["id"]
    r = client.patch(f"/invoices/{inv_id}/extraction", headers=hdr(at),
                     json={"montant_brut": 20_000, "remise_pct": 0, "escompte_pct": 0})
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["ttc"] == 24_000.0                      # 20 000 × 1.20 — fully recomputed
    checks = inv["response"]["validation_checks"]
    assert all("explanation" in c for c in checks)     # smarter explanations present
    fc = {f["field"]: f["confidence"] for f in inv["response"]["step1_identification"]["field_confidences"]}
    assert fc["montant_brut"] == 1.0                   # human correction = confidence 1.0
    assert client.patch(f"/invoices/{inv_id}/extraction", headers=hdr(at),
                        json={"not_a_field": 1}).status_code == 422


def test_intelligence_endpoints(client, firm):
    """Insights on detail, firm insight feed, close readiness, FEC export, NL query."""
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    inv_id = client.get("/invoices", headers=hdr(at)).json()["items"][0]["id"]

    detail = client.get(f"/invoices/{inv_id}", headers=hdr(at)).json()
    assert "insights" in detail                       # duplicate insight persisted at upload
    if detail["status"] == "approved":
        assert "account_explanations" in detail
        assert any("2355" in e or "classe 2" in e for e in detail["account_explanations"])

    assert client.get("/insights", headers=hdr(at)).status_code == 200

    cr = client.get("/close/readiness", headers=hdr(at)).json()
    assert "ready" in cr and "blockers" in cr and "sequence_gaps" in cr

    fec = client.get("/exports/fec", headers=hdr(at))
    assert fec.status_code == 200 and fec.text.startswith("JournalCode|")


def test_nl_assistant(client, firm, monkeypatch):
    """LLM only translates; results come from the real repository."""
    from app.api.routers import assistant as mod
    from app.api.routers.assistant import FilterSpec

    async def fake_translate(q):
        return FilterSpec(supplier="TECHNO", min_ttc=1000, answerable=True, reason_if_not=None)
    monkeypatch.setattr(mod, "_translate", fake_translate)

    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    r = client.post("/assistant/query", headers=hdr(at),
                    json={"question": "factures TECHNO au-dessus de 1000 MAD"})
    assert r.status_code == 200
    body = r.json()
    assert body["answerable"] and len(body["items"]) >= 1
    assert all((i["ttc"] or 0) >= 1000 for i in body["items"])

    async def fake_no(q):
        return FilterSpec(answerable=False, reason_if_not="Question hors périmètre factures.")
    monkeypatch.setattr(mod, "_translate", fake_no)
    r2 = client.post("/assistant/query", headers=hdr(at), json={"question": "quel temps fait-il"})
    assert r2.json()["answerable"] is False and r2.json()["items"] == []


def test_health_knowledge_outcomes(client, firm):
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()

    scan = client.get("/health/clients", headers=hdr(at)).json()
    assert isinstance(scan, list) and all("score" in h and "issues" in h for h in scan)
    assert scan == sorted(scan, key=lambda h: h["score"])       # worst first

    cid = client.get("/clients", headers=hdr(at)).json()[0]["id"]
    summary = client.get(f"/clients/{cid}/summary", headers=hdr(at)).json()
    assert "health" in summary and 0 <= summary["health"]["score"] <= 100

    kb = client.get("/knowledge", headers=hdr(at)).json()
    assert isinstance(kb, list)                                  # priors visible once learned
    if kb:
        assert "confirmations" in kb[0] and "summary" in kb[0]

    inv_id = client.get("/invoices", headers=hdr(at)).json()["items"][0]["id"]
    detail = client.get(f"/invoices/{inv_id}", headers=hdr(at)).json()
    if detail.get("response"):
        bd = detail["confidence_breakdown"]
        assert "factors" in bd and all({"factor", "ok", "detail"} <= set(f) for f in bd["factors"])

    m = client.get("/metrics/outcomes", headers=hdr(at)).json()
    assert m["invoices_processed"] >= 1
    assert "duplicates_flagged_before_posting" in m and "corrections_per_invoice" in m


def test_feedback_flow(client, firm):
    at = client.post("/auth/login", json={"email": "ahmed@alpha.ma", "password": "password123"}).json()
    inv_id = client.get("/invoices", headers=hdr(at)).json()["items"][0]["id"]
    r = client.post(f"/invoices/{inv_id}/feedback", headers=hdr(at),
                    json={"rating": "down", "reason": "wrong_vat", "comment": "taux erroné"})
    assert r.status_code == 200
    # Re-submit updates rather than duplicates
    client.post(f"/invoices/{inv_id}/feedback", headers=hdr(at), json={"rating": "up"})
    mine = client.get(f"/invoices/{inv_id}/feedback", headers=hdr(at)).json()
    assert mine["rating"] == "up" and mine["reason"] is None
    summary = client.get("/metrics/feedback", headers=hdr(at)).json()
    assert summary["total"] == 1 and summary["satisfaction_rate"] == 1.0


def test_documents_ged(client, firm, monkeypatch):
    """GED: bulk upload -> OCR search -> folders -> tags -> versions -> archive -> isolation."""
    from app.services.document_ai import DocumentAnalysis

    async def fake_analyze(data_uri):
        return DocumentAnalysis(category="contrat", text="Contrat de bail — ICE 001234567")
    monkeypatch.setattr("app.api.routers.documents.analyze_document", fake_analyze)

    h = hdr(firm)
    cl = client.post("/clients", headers=h, json={"name": "GED Client"}).json()

    # Bulk upload: 2 files, no explicit category -> AI classification applies
    r = client.post(f"/documents/upload?client_id={cl['id']}", headers=h, files=[
        ("files", ("bail.jpg", io.BytesIO(b"img-1"), "image/jpeg")),
        ("files", ("annexe.pdf", io.BytesIO(b"pdf-2"), "application/pdf")),
    ])
    assert r.status_code == 200 and r.json()["stored"] == 2
    doc_id = r.json()["items"][0]["id"]

    # OCR text is searchable; AI category was applied
    found = client.get("/documents?q=bail", headers=h).json()
    assert found["total"] >= 1
    doc = client.get(f"/documents/{doc_id}", headers=h).json()
    assert doc["category"] == "contrat" and doc["ocr_status"] == "done"

    # Explicit category wins over AI suggestion
    r2 = client.post("/documents/upload?category=paie", headers=h, files=[
        ("files", ("bulletin.jpg", io.BytesIO(b"img-3"), "image/jpeg"))])
    paie_doc = client.get(f"/documents/{r2.json()['items'][0]['id']}", headers=h).json()
    assert paie_doc["category"] == "paie" and paie_doc["client_id"] is None

    # Folder counts
    cats = {c["category"]: c["count"] for c in client.get("/documents/categories", headers=h).json()}
    assert cats["contrat"] == 2 and cats["paie"] == 1

    # Tags + original file download
    client.patch(f"/documents/{doc_id}", headers=h, json={"tags": ["bail", "2026"]})
    assert client.get(f"/documents/{doc_id}", headers=h).json()["tags"] == ["bail", "2026"]
    assert client.get(f"/documents/{doc_id}/file", headers=h).status_code == 200

    # New version replaces the old one in listings; history keeps both
    v2 = client.post(f"/documents/{doc_id}/version", headers=h, files={
        "file": ("bail-signe.jpg", io.BytesIO(b"img-4"), "image/jpeg")})
    assert v2.status_code == 201 and v2.json()["version"] == 2
    listing = client.get("/documents", headers=h).json()
    ids = [d["id"] for d in listing["items"]]
    assert v2.json()["id"] in ids and doc_id not in ids
    history = client.get(f"/documents/{v2.json()['id']}", headers=h).json()["versions"]
    assert [d["version"] for d in history] == [2, 1]

    # Archive (admin only) removes from default listing
    client.post(f"/documents/{v2.json()['id']}/archive", headers=h)
    ids_after = [d["id"] for d in client.get("/documents", headers=h).json()["items"]]
    assert v2.json()["id"] not in ids_after
    archived_ids = [d["id"] for d in
                    client.get("/documents?include_archived=true", headers=h).json()["items"]]
    assert v2.json()["id"] in archived_ids

    # Tenant isolation: firm Beta sees nothing
    beta = client.post("/auth/login", json={"email": "b@beta.ma", "password": "password123"}).json()
    assert client.get("/documents", headers=hdr(beta)).json()["total"] == 0
    assert client.get(f"/documents/{doc_id}", headers=hdr(beta)).status_code == 404


def test_treasury_reconciliation(client, firm):
    """Treasury: account -> CSV import -> auto-suggestion -> confirm -> summary -> isolation."""
    h = hdr(firm)
    acc = client.post("/treasury/accounts", headers=h, json={
        "name": "BMCE Compte Principal", "bank_name": "BMCE", "rib": "011780000019210001234567"})
    assert acc.status_code == 201
    acc_id = acc.json()["id"]

    # The invoice workflow test created FAC-0142 / TECHNO BUREAU, TTC 10584.0
    # (10000 brut, -10% remise, -2% escompte, +20% TVA). A statement line paying
    # it should be auto-suggested with an explanation.
    csv_body = (
        "date;libelle;montant;reference\n"
        "18/06/2026;VIREMENT TECHNO BUREAU FAC 0142;-10584,00;VIR-778\n"
        "19/06/2026;FRAIS TENUE DE COMPTE;-25,50;\n"
        "19/06/2026;FRAIS TENUE DE COMPTE;-25,50;\n"          # exact duplicate line
    )
    imp = client.post(f"/treasury/accounts/{acc_id}/import", headers=h,
                      files={"file": ("releve.csv", io.BytesIO(csv_body.encode()), "text/csv")},
                      params={"opening_balance": "0", "closing_balance": "-10635.00"})
    assert imp.status_code == 201, imp.text
    body = imp.json()
    assert body["format"] == "csv" and body["transactions"] == 3
    assert body["suggested"] >= 1 and body["duplicates"] == 1

    txs = client.get(f"/treasury/transactions?account_id={acc_id}", headers=h).json()
    assert txs["total"] == 3
    suggested = [t for t in txs["items"] if t["status"] == "suggested"]
    assert suggested and suggested[0]["match_confidence"] >= 0.6
    assert "montant" in suggested[0]["match_explanation"]
    assert suggested[0]["matched_invoice_number"] == "FAC-0142"
    dupes = [t for t in txs["items"] if t["is_duplicate_of"]]
    assert len(dupes) == 1

    # Candidates endpoint explains the ranking
    cands = client.get(f"/treasury/transactions/{suggested[0]['id']}/candidates", headers=h).json()
    assert cands and cands[0]["score"] >= 0.6 and cands[0]["reasons"]

    # Human confirms the suggestion
    ok = client.post(f"/treasury/transactions/{suggested[0]['id']}/match", headers=h,
                     json={"invoice_id": suggested[0]["matched_invoice_id"]})
    assert ok.status_code == 200 and ok.json()["status"] == "matched"

    # Exclude a fee line, then check the dashboard summary
    fee = [t for t in txs["items"] if t["status"] == "unmatched" and not t["is_duplicate_of"]][0]
    client.post(f"/treasury/transactions/{fee['id']}/exclude", headers=h)
    summary = client.get("/treasury/accounts", headers=h).json()
    mine = [a for a in summary if a["id"] == acc_id][0]
    assert mine["matched"] == 1 and mine["total_tx"] == 2  # excluded line out
    assert round(mine["balance"], 2) == -10635.00  # authoritative closing balance from the imported statement

    # Unmatch works
    client.post(f"/treasury/transactions/{suggested[0]['id']}/unmatch", headers=h)
    txs2 = client.get(f"/treasury/transactions?account_id={acc_id}&status=unmatched", headers=h).json()
    assert any(t["id"] == suggested[0]["id"] for t in txs2["items"])

    # Tenant isolation
    beta = client.post("/auth/login", json={"email": "b@beta.ma", "password": "password123"}).json()
    assert client.get("/treasury/accounts", headers=hdr(beta)).json() == []
    assert client.get(f"/treasury/transactions?account_id={acc_id}",
                      headers=hdr(beta)).status_code == 404


def test_statement_parsers():
    """MT940 and CAMT.053 parse to the same normalized shape as CSV."""
    from app.services.bank_import import parse_camt053, parse_mt940

    mt940 = (
        ":20:REF001\n:25:011780/0001921000\n:28C:00001\n"
        ":60F:C260601MAD125000,00\n"
        ":61:260618D10584,00NTRFVIR-778//BANKREF1\n"
        ":86:VIREMENT TECHNO BUREAU FACTURE 0142\n"
        ":61:260619C5000,00NTRFREM-12\n"
        ":86:REMISE CHEQUE 12\n"
        ":62F:C260630MAD119416,00\n")
    lines = parse_mt940(mt940)
    assert len(lines) == 2
    assert lines[0]["date"] == "2026-06-18" and lines[0]["amount"] == -10584.0
    assert "TECHNO BUREAU" in lines[0]["label"]
    assert lines[1]["amount"] == 5000.0

    camt = """<?xml version="1.0" encoding="UTF-8"?>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
      <BkToCstmrStmt><Stmt>
        <Ntry><Amt Ccy="MAD">10584.00</Amt><CdtDbtInd>DBIT</CdtDbtInd>
          <BookgDt><Dt>2026-06-18</Dt></BookgDt>
          <NtryDtls><TxDtls><RmtInf><Ustrd>VIREMENT TECHNO BUREAU FAC 0142</Ustrd></RmtInf>
          <Refs><EndToEndId>VIR-778</EndToEndId></Refs></TxDtls></NtryDtls></Ntry>
        <Ntry><Amt Ccy="MAD">2500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
          <BookgDt><Dt>2026-06-20</Dt></BookgDt>
          <NtryDtls><TxDtls><RmtInf><Ustrd>ENCAISSEMENT CLIENT X</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
      </Stmt></BkToCstmrStmt>
    </Document>"""
    entries = parse_camt053(camt)
    assert len(entries) == 2
    assert entries[0]["amount"] == -10584.0 and entries[0]["date"] == "2026-06-18"
    assert entries[1]["amount"] == 2500.0
