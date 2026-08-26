"""Invoices: lifecycle, list filters/search, duplicate detection, stats, exports."""
import json

from app.core.db import execute, new_id, now, query, query_one

LIST_COLUMNS = """i.id, i.client_id, i.uploaded_by, i.filename, i.status, i.verdict,
    i.confidence, i.extraction_confidence, i.validation_pass_rate, i.accounting_rule_confidence, i.tax_risk_level, i.reviewer_confidence,
    i.posting_status, i.posting_date, i.invoice_number, i.supplier_name, i.supplier_ice, i.invoice_date, i.currency, i.document_direction, i.ttc,
    i.net_a_payer, i.is_duplicate_of, i.duration_ms, i.error, i.is_archived, i.created_at,
    c.name AS client_name,
    json_extract(i.response_json, '$.step1_identification.invoice_category') AS category,
    json_extract(i.response_json, '$.step2_calculations.tva_amount') AS tva,
    (SELECT full_name FROM users WHERE id = i.uploaded_by) AS uploaded_by_name,
    (SELECT full_name FROM users WHERE id = i.reviewed_by) AS reviewed_by_name"""


def create_invoice(firm_id: str, client_id: str, uploaded_by: str,
                   filename: str, file_path: str | None, source_hash: str | None = None) -> str:
    iid = new_id()
    execute("""INSERT INTO invoices (id, firm_id, client_id, uploaded_by, filename, file_path, source_hash, status, created_at)
               VALUES (?,?,?,?,?,?,?, 'processing', ?)""",
            (iid, firm_id, client_id, uploaded_by, filename, file_path, source_hash, now()))
    return iid


def finish_processing(invoice_id: str, *, response_json: dict, verdict: str, confidence: float,
                      invoice_number: str | None, supplier_name: str | None,
                      extraction_confidence: float | None = None, validation_pass_rate: float | None = None,
                      invoice_date: str | None, ttc: float, net_a_payer: float,
                      model: str, duration_ms: int, duplicate_of: str | None) -> None:
    from app.services.posting import to_cents
    from app.services.tax_rules import tax_risk_level
    status = "needs_review"
    extraction = response_json.get("step1_identification") or {}
    # accounting confidence is deliberately distinct from OCR/extraction confidence:
    # full score requires no suspense account and a valid deterministic verdict.
    all_accounts = [str(l.get("account_number", "")) for e in response_json.get("step4_journal_entries", []) for l in e.get("lines", [])]
    accounting_conf = float(validation_pass_rate or 0)
    if "3497" in all_accounts or verdict != "VALID":
        accounting_conf = min(accounting_conf, 0.49)
    try:
        from app.models.invoice import ExtractedInvoiceData
        risk = tax_risk_level(ExtractedInvoiceData(**extraction))
    except Exception:
        risk = "high"
    direction = extraction.get("document_direction")
    if not direction:
        owner = extraction.get("requested_perspective")
        is_credit = extraction.get("invoice_type") == "AVOIR"
        if owner == "seller" or extraction.get("invoice_category") == "facture_vente":
            direction = "sale_credit_note" if is_credit else "sale"
        else:
            direction = "purchase_credit_note" if is_credit else "purchase"
    execute("""UPDATE invoices SET status=?, verdict=?, confidence=?, extraction_confidence=?,
               validation_pass_rate=?, accounting_rule_confidence=?, tax_risk_level=?, invoice_number=?, supplier_name=?, supplier_ice=?,
               customer_name=?, customer_ice=?, invoice_date=?, currency=?, exchange_rate=?, document_direction=?, ttc=?, ttc_cents=?,
               net_a_payer=?, net_a_payer_cents=?, model=?, duration_ms=?, response_json=?, is_duplicate_of=?,
               posting_status=CASE WHEN posting_status='posted' THEN posting_status ELSE 'unposted' END
               WHERE id=?""",
            (status, verdict, confidence, extraction_confidence, validation_pass_rate, accounting_conf, risk, invoice_number, supplier_name,
             extraction.get("supplier_ice"), extraction.get("customer_name"), extraction.get("customer_ice"), invoice_date,
             (extraction.get("currency") or "MAD").upper(), str(extraction.get("exchange_rate") or 1), direction, ttc, to_cents(ttc),
             net_a_payer, to_cents(net_a_payer), model, duration_ms, json.dumps(response_json), duplicate_of, invoice_id))


def fail_processing(invoice_id: str, error: str) -> None:
    execute("UPDATE invoices SET status='failed', error=? WHERE id=?", (error[:2000], invoice_id))


def get_invoice(invoice_id: str, firm_id: str) -> dict | None:
    row = query_one("""SELECT i.*,
               (SELECT full_name FROM users WHERE id = i.uploaded_by) AS uploaded_by_name,
               (SELECT full_name FROM users WHERE id = i.reviewed_by) AS reviewed_by_name
               FROM invoices i WHERE i.id = ? AND i.firm_id = ?""", (invoice_id, firm_id))
    if row and row.get("response_json"):
        row["response"] = json.loads(row.pop("response_json"))
    if row:
        from app.services.posting import from_cents, to_cents
        allocated = query_one("SELECT COALESCE(SUM(amount_cents),0) AS n FROM payment_allocations WHERE invoice_id=? AND firm_id=?",
                              (invoice_id, firm_id))["n"] or 0
        due_cents = int(row.get("net_a_payer_cents") or to_cents(row.get("net_a_payer") or 0))
        outstanding = max(0, due_cents - int(allocated))
        row["paid_amount"] = from_cents(int(allocated))
        row["outstanding_amount"] = from_cents(outstanding)
        if due_cents > 0 and outstanding == 0:
            row["payment_status"] = "PAID"
        elif int(allocated) > 0:
            row["payment_status"] = "PARTIALLY_PAID"
        else:
            from app.services.dates import normalize_date
            from datetime import date
            due = normalize_date((row.get("response") or {}).get("step1_identification", {}).get("due_date"))
            row["payment_status"] = "OVERDUE" if due and due < date.today().isoformat() else "UNPAID"
    return row


def _norm_supplier(name: str | None) -> str:
    """Normalize supplier for fuzzy comparison: casefold, strip punctuation/legal suffixes."""
    if not name:
        return ""
    import re
    n = re.sub(r"[^a-z0-9 ]", " ", name.casefold())
    n = re.sub(r"\b(sarl|sa|sas|snc|au|s a r l)\b", " ", n)
    return " ".join(n.split())


def _norm_number(num: str | None) -> str:
    """Invoice-number core: digits only (FAC-2026/0142 == FAC 20260142)."""
    return "".join(ch for ch in (num or "") if ch.isdigit())


def find_duplicate(firm_id: str, supplier_name: str | None, invoice_number: str | None,
                   ttc: float, exclude_id: str, *, supplier_ice: str | None = None,
                   invoice_date: str | None = None, currency: str = "MAD", source_hash: str | None = None) -> str | None:
    """Layered duplicate detection using source hash + accounting identity.

    Strong match: identical source hash OR supplier ICE/normalized supplier + invoice
    number + date + amount/currency. Fuzzy supplier fallback remains a review hint.
    """
    if not source_hash:
        cur = query_one("SELECT source_hash FROM invoices WHERE id=? AND firm_id=?", (exclude_id, firm_id))
        source_hash = cur.get("source_hash") if cur else None
    num = _norm_number(invoice_number)
    sup = _norm_supplier(supplier_name)
    ice = "".join(ch for ch in (supplier_ice or "") if ch.isdigit())
    if source_hash:
        row = query_one("SELECT id FROM invoices WHERE firm_id=? AND id!=? AND source_hash=? AND status!='failed' LIMIT 1",
                        (firm_id, exclude_id, source_hash))
        if row:
            return row["id"]
    if not num and not sup and not ice:
        return None
    from app.services.posting import to_cents
    cents = to_cents(ttc)
    tol_cents = max(abs(cents) // 100, 100)  # 1% or 1 MAD
    candidates = query("""SELECT id,supplier_name,supplier_ice,invoice_number,invoice_date,currency,ttc,ttc_cents,source_hash
                          FROM invoices WHERE firm_id=? AND id!=? AND status!='failed'
                          ORDER BY created_at DESC LIMIT 100""", (firm_id, exclude_id))
    for c in candidates:
        ccents = int(c.get("ttc_cents") or to_cents(c.get("ttc") or 0))
        if abs(ccents - cents) > tol_cents or (c.get("currency") or "MAD").upper() != (currency or "MAD").upper():
            continue
        c_num, c_sup = _norm_number(c.get("invoice_number")), _norm_supplier(c.get("supplier_name"))
        c_ice = "".join(ch for ch in (c.get("supplier_ice") or "") if ch.isdigit())
        identity_match = bool(ice and c_ice and ice == c_ice) or bool(sup and c_sup and (sup == c_sup or sup in c_sup or c_sup in sup))
        number_match = bool(num and c_num and num == c_num)
        date_match = not invoice_date or not c.get("invoice_date") or invoice_date == c.get("invoice_date")
        if number_match and identity_match and date_match:
            return c["id"]
        if number_match and not (ice or c_ice) and identity_match:
            return c["id"]
    return None


def list_invoices(firm_id: str, *, client_id: str | None = None, status: str | None = None,
                  q: str | None = None, date_from: str | None = None, date_to: str | None = None,
                  accountant_id: str | None = None, limit: int = 50, offset: int = 0,
                  archived: bool = False, category: str | None = None) -> dict:
    where = "WHERE i.firm_id = ? AND i.is_archived = ?"
    params: list = [firm_id, 1 if archived else 0]
    if category:
        where += (" AND json_extract(i.response_json,"
                  " '$.step1_identification.invoice_category') = ?")
        params.append(category)
    if client_id:
        where += " AND i.client_id = ?"; params.append(client_id)
    if status:
        where += " AND i.status = ?"; params.append(status)
    if q:
        where += " AND (i.supplier_name LIKE ? OR i.invoice_number LIKE ? OR i.filename LIKE ?)"
        params += [f"%{q}%"] * 3
    if date_from:
        where += " AND i.invoice_date >= ?"; params.append(date_from)
    if date_to:
        where += " AND i.invoice_date <= ?"; params.append(date_to)
    if accountant_id:  # accountants: restrict to their assigned clients
        where += " AND i.client_id IN (SELECT id FROM clients WHERE firm_id = ? AND assigned_to = ?)"
        params += [firm_id, accountant_id]

    total = query_one(f"SELECT COUNT(*) AS n FROM invoices i {where}", tuple(params))["n"]
    rows = query(f"""SELECT {LIST_COLUMNS} FROM invoices i JOIN clients c ON c.id = i.client_id
                     {where} ORDER BY i.created_at DESC LIMIT ? OFFSET ?""",
                 tuple(params + [limit, offset]))
    return {"total": total, "items": rows, "limit": limit, "offset": offset}


def review_invoice(invoice_id: str, firm_id: str, reviewer_id: str, approve: bool) -> None:
    execute("""UPDATE invoices SET status = ?, reviewed_by = ?, reviewed_at = ?
               WHERE id = ? AND firm_id = ?""",
            ("approved" if approve else "rejected", reviewer_id, now(), invoice_id, firm_id))


# ── Stats & reporting ──
def firm_stats(firm_id: str, accountant_id: str | None = None) -> dict:
    scope = "firm_id = ?"
    params: list = [firm_id]
    if accountant_id:
        scope += " AND client_id IN (SELECT id FROM clients WHERE assigned_to = ?)"
        params.append(accountant_id)
    row = query_one(f"""SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) AS needs_review,
        SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN verdict='INVALID' THEN 1 ELSE 0 END) AS invalid,
        SUM(CASE WHEN is_duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
        SUM(CASE WHEN DATE(created_at)=CURRENT_DATE THEN 1 ELSE 0 END) AS today,
        ROUND(AVG(confidence), 3) AS avg_confidence,
        ROUND(AVG(duration_ms), 0) AS avg_duration_ms
        FROM invoices WHERE {scope}""", tuple(params))
    return {k: (v or 0) for k, v in row.items()}


def monthly_report(firm_id: str, client_id: str | None = None) -> list[dict]:
    where = "WHERE firm_id = ? AND posting_status = 'posted'"
    params: list = [firm_id]
    if client_id:
        where += " AND client_id = ?"; params.append(client_id)
    return query(f"""SELECT substr(COALESCE(invoice_date, created_at), 1, 7) AS month,
                            COUNT(*) AS invoices, ROUND(SUM(COALESCE(ttc_cents,CAST(ROUND(ttc*100) AS INTEGER)))/100.0, 2) AS total_ttc,
                            ROUND(SUM(COALESCE(net_a_payer_cents,CAST(ROUND(net_a_payer*100) AS INTEGER)))/100.0, 2) AS total_net
                     FROM invoices {where} GROUP BY month ORDER BY month DESC LIMIT 24""",
                 tuple(params))


def journal_rows(firm_id: str, client_id: str | None = None,
                 date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Authoritative ledger rows from normalized immutable posting batches only."""
    from app.services.posting import posted_journal_rows
    return posted_journal_rows(firm_id, client_id, date_from, date_to)


def platform_stats() -> dict:
    """Super-admin view: cross-tenant aggregates only, no invoice contents."""
    firms = query_one("SELECT COUNT(*) AS n, SUM(is_active) AS active FROM firms")
    users = query_one("SELECT COUNT(*) AS n FROM users WHERE role != 'super_admin'")
    inv = query_one("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN DATE(created_at)=CURRENT_DATE THEN 1 ELSE 0 END) AS today,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
        ROUND(AVG(duration_ms),0) AS avg_duration_ms FROM invoices""")
    return {"firms": firms["n"] or 0, "active_firms": firms["active"] or 0,
            "users": users["n"] or 0,
            "invoices_total": inv["total"] or 0, "invoices_today": inv["today"] or 0,
            "invoices_failed": inv["failed"] or 0, "avg_duration_ms": inv["avg_duration_ms"] or 0}
