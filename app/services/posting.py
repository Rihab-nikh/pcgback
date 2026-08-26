"""Authoritative accounting posting ledger.

AI output is a proposal.  Official reports are built from normalized posted
journal batches whose monetary values are stored as integer centimes.
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

from app.core.db import connect, lock_posting_sequence, new_id, now, query, query_one
from app.services.dates import fiscal_year, normalize_date

CENT = Decimal("0.01")


def to_cents(value: float | int | str | Decimal) -> int:
    d = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(value: int) -> float:
    return float((Decimal(int(value)) / 100).quantize(CENT))


def book_owner_perspective(extraction: dict) -> str:
    requested = extraction.get("requested_perspective")
    if requested in ("buyer", "seller"):
        return requested.title()
    # Safe legacy migration: infer one owner; never export both legal entities.
    category = extraction.get("invoice_category")
    return "Seller" if category == "facture_vente" else "Buyer"


def selected_book_entries(response: dict) -> list[dict]:
    extraction = response.get("step1_identification") or {}
    owner = book_owner_perspective(extraction)
    return [
        e for e in (response.get("step4_journal_entries") or [])
        if e.get("perspective") == owner and "Settlement" not in e.get("perspective", "")
    ]


def record_rule_event(firm_id: str | None, invoice_id: str | None, severity: str, rule_code: str, message: str, context: dict | None = None) -> None:
    """Durable observability for accounting-rule failures/warnings."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO accounting_rule_events(id,firm_id,invoice_id,severity,rule_code,message,context_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id(), firm_id, invoice_id, severity, rule_code, message, json.dumps(context or {}, ensure_ascii=False), now()),
            )
            conn.commit()
    except Exception:
        pass


def _line_metadata(extraction: dict, line: dict, owner: str) -> tuple[str | None, str | None, str | None]:
    aux = extraction.get("auxiliary_account")
    partner = extraction.get("customer_name") if owner == "Seller" else extraction.get("supplier_name")
    account = str(line.get("account_number") or "")
    tax_code = None
    if account.startswith("3455"):
        tax_code = extraction.get("tax_treatment_code") or "VAT_INPUT"
    elif account == "4455":
        tax_code = extraction.get("tax_treatment_code") or "VAT_OUTPUT"
    elif account.startswith("4452"):
        tax_code = extraction.get("withholding_type") or "WITHHOLDING"
    return aux, partner, tax_code


def _register_fixed_assets(conn, *, firm_id: str, client_id: str | None, invoice_id: str, batch_id: str, response: dict, pdate: str) -> None:
    extraction = response.get("step1_identification") or {}
    # The posted class-2 line is authoritative for acquisition cost. This avoids
    # reconstructing asset cost from AI line arithmetic after posting.
    rows = conn.execute(
        "SELECT line_idx,account_number,account_label,amount_cents,entry_label FROM posting_lines WHERE batch_id=? AND side='DEBIT' AND account_number LIKE '2%'",
        (batch_id,),
    ).fetchall()
    type_by_account = {
        "2321": "building", "2331": "installation", "2332": "equipment",
        "2340": "vehicle", "2351": "furniture", "2355": "it", "2380": "other",
    }
    for r in rows:
        if r["account_number"].startswith(("23", "22")):
            life = {"building": 240, "installation": 120, "equipment": 60, "vehicle": 60, "furniture": 120, "it": 36, "other": 60}.get(type_by_account.get(r["account_number"], "other"), 60)
            conn.execute(
                """INSERT OR IGNORE INTO fixed_assets
                   (id,firm_id,client_id,invoice_id,posting_batch_id,source_line_index,asset_type,description,account_number,
                    acquisition_date,in_service_date,acquisition_cost_cents,residual_value_cents,useful_life_months,depreciation_method,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,'straight_line',?)""",
                (new_id(), firm_id, client_id, invoice_id, batch_id, r["line_idx"], type_by_account.get(r["account_number"], "other"),
                 r["entry_label"] or r["account_label"], r["account_number"], pdate, pdate, r["amount_cents"], life, now()),
            )


def _period_is_open(conn, firm_id: str, posting_date: str) -> bool:
    row = conn.execute(
        """SELECT status FROM accounting_periods
           WHERE firm_id = ? AND period_start <= ? AND period_end >= ?
           ORDER BY period_start DESC LIMIT 1""",
        (firm_id, posting_date, posting_date),
    ).fetchone()
    # No configured period means open (backward-compatible pilot mode).
    return row is None or row["status"] == "OPEN"


def post_invoice(firm_id: str, invoice_id: str, user_id: str,
                 posting_date: str | None = None) -> dict:
    inv = query_one("SELECT * FROM invoices WHERE id = ? AND firm_id = ?", (invoice_id, firm_id))
    if not inv:
        raise ValueError("Invoice not found")
    if inv["status"] != "approved":
        raise ValueError("Only approved invoices can be posted")
    if inv.get("verdict") != "VALID" and not inv.get("validation_override_note"):
        raise ValueError("INVALID invoice requires a documented override before posting")
    if inv.get("posting_status") == "posted":
        existing = query_one("SELECT * FROM posting_batches WHERE invoice_id = ? AND status = 'posted'",
                             (invoice_id,))
        return existing or {"invoice_id": invoice_id, "status": "posted"}
    response = json.loads(inv["response_json"] or "{}")
    entries = selected_book_entries(response)
    if not entries:
        raise ValueError("No book-owner journal entry available for posting")

    document_date = normalize_date(inv.get("invoice_date"))
    pdate = normalize_date(posting_date) or document_date
    if not pdate:
        raise ValueError("A valid posting/accounting date is required")
    year = fiscal_year(pdate)
    extraction = response.get("step1_identification") or {}
    owner = book_owner_perspective(extraction)
    journal_code = extraction.get("journal_code") or ("VE" if owner == "Seller" else "AC")

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, pdate):
            raise ValueError(f"Accounting period containing {pdate} is closed")
        row = conn.execute(
            "SELECT COALESCE(MAX(entry_number), 0) + 1 AS n FROM posting_batches WHERE firm_id = ? AND fiscal_year = ?",
            (firm_id, year),
        ).fetchone()
        entry_number = int(row["n"])
        batch_id = new_id()
        conn.execute(
            """INSERT INTO posting_batches
               (id, firm_id, client_id, invoice_id, posting_date, document_date,
                journal_code, fiscal_year, entry_number, status, posted_by, posted_at)
               VALUES (?,?,?,?,?,?,?,?,?,'posted',?,?)""",
            (batch_id, firm_id, inv["client_id"], invoice_id, pdate, document_date,
             journal_code, year, entry_number, user_id, now()),
        )
        idx = 0
        for entry in entries:
            for line in entry.get("lines", []):
                amount_cents = to_cents(line["amount"])
                if amount_cents <= 0:
                    raise ValueError("Posting lines must have positive amounts")
                aux, partner, tax_code = _line_metadata(extraction, line, owner)
                conn.execute(
                    """INSERT INTO posting_lines
                       (batch_id, line_idx, account_number, account_label, side,
                        amount_cents, entry_label, source_perspective, aux_account, partner_name, tax_code)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, idx, line["account_number"], line["account_label"], line["side"],
                     amount_cents, entry.get("libelle") or "", entry.get("perspective") or "", aux, partner, tax_code),
                )
                idx += 1
        deb = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS n FROM posting_lines WHERE batch_id=? AND side='DEBIT'",
                           (batch_id,)).fetchone()["n"]
        cred = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS n FROM posting_lines WHERE batch_id=? AND side='CREDIT'",
                            (batch_id,)).fetchone()["n"]
        if deb != cred:
            record_rule_event(firm_id, invoice_id, "critical", "POSTING_UNBALANCED",
                              f"Posting batch is unbalanced by {from_cents(abs(deb-cred)):.2f} MAD", {"batch_id": batch_id})
            raise ValueError(f"Posting batch is unbalanced by {from_cents(abs(deb-cred)):.2f} MAD")
        _register_fixed_assets(conn, firm_id=firm_id, client_id=inv["client_id"], invoice_id=invoice_id, batch_id=batch_id, response=response, pdate=pdate)
        conn.execute("UPDATE invoices SET posting_status='posted', posting_date=?, posted_by=?, posted_at=? WHERE id=?",
                     (pdate, user_id, now(), invoice_id))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id = ?", (batch_id,)) or {"id": batch_id}


def reverse_invoice(firm_id: str, invoice_id: str, user_id: str,
                    reversal_date: str | None = None, reason: str | None = None) -> dict:
    original = query_one(
        "SELECT * FROM posting_batches WHERE firm_id=? AND invoice_id=? AND status='posted' ORDER BY posted_at DESC LIMIT 1",
        (firm_id, invoice_id),
    )
    if not original:
        raise ValueError("No posted batch to reverse")
    if original.get("reversed_by"):
        raise ValueError("Posting batch has already been reversed")
    rdate = normalize_date(reversal_date) or original["posting_date"]
    year = fiscal_year(rdate)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, rdate):
            raise ValueError(f"Accounting period containing {rdate} is closed")
        entry_number = conn.execute(
            "SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?",
            (firm_id, year),
        ).fetchone()["n"]
        reversal_id = new_id()
        conn.execute(
            """INSERT INTO posting_batches
               (id, firm_id, client_id, invoice_id, posting_date, document_date, journal_code,
                fiscal_year, entry_number, status, posted_by, posted_at, reversal_of, reversal_reason)
               VALUES (?,?,?,?,?,?,?,?,?,'posted',?,?,?,?)""",
            (reversal_id, firm_id, original["client_id"], invoice_id, rdate, original["document_date"],
             original["journal_code"], year, entry_number, user_id, now(), original["id"], reason),
        )
        lines = conn.execute("SELECT * FROM posting_lines WHERE batch_id=? ORDER BY line_idx", (original["id"],)).fetchall()
        for line in lines:
            side = "CREDIT" if line["side"] == "DEBIT" else "DEBIT"
            conn.execute(
                """INSERT INTO posting_lines
                   (batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective,aux_account,partner_name,tax_code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (reversal_id, line["line_idx"], line["account_number"], line["account_label"], side,
                 line["amount_cents"], f"Contre-passation — {line['entry_label']}", line["source_perspective"], line["aux_account"], line["partner_name"], line["tax_code"]),
            )
        conn.execute("UPDATE posting_batches SET reversed_by=? WHERE id=?", (reversal_id, original["id"]))
        conn.execute("UPDATE invoices SET posting_status='reversed' WHERE id=?", (invoice_id,))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id=?", (reversal_id,)) or {"id": reversal_id}


def period_is_open(firm_id: str, posting_date: str) -> bool:
    pdate = normalize_date(posting_date)
    if not pdate:
        return False
    with connect() as conn:
        return _period_is_open(conn, firm_id, pdate)


def post_manual_entry(firm_id: str, entry_id: str, user_id: str) -> dict:
    e = query_one("SELECT * FROM manual_entries WHERE id=? AND firm_id=?", (entry_id, firm_id))
    if not e:
        raise ValueError("Manual entry not found")
    pdate = normalize_date(e["date"])
    if not pdate:
        raise ValueError("Manual entry requires a valid YYYY-MM-DD date")
    year = fiscal_year(pdate)
    lines = query("SELECT * FROM manual_entry_lines WHERE entry_id=? ORDER BY line_idx", (entry_id,))
    if not lines:
        raise ValueError("Manual entry has no lines")
    deb = sum(int(l.get("amount_cents") or to_cents(l["amount"])) for l in lines if l["side"] == "DEBIT")
    cred = sum(int(l.get("amount_cents") or to_cents(l["amount"])) for l in lines if l["side"] == "CREDIT")
    if deb != cred or deb <= 0:
        raise ValueError("Manual entry must be non-empty and exactly balanced to centimes")
    existing = query_one("SELECT * FROM posting_batches WHERE firm_id=? AND manual_entry_id=? AND reversal_of IS NULL", (firm_id, entry_id))
    if existing:
        return existing
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, pdate):
            raise ValueError(f"Accounting period containing {pdate} is closed")
        entry_number = int(conn.execute("SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?", (firm_id, year)).fetchone()["n"])
        batch_id = new_id()
        conn.execute("""INSERT INTO posting_batches
            (id,firm_id,client_id,invoice_id,manual_entry_id,posting_date,document_date,journal_code,fiscal_year,entry_number,status,posted_by,posted_at)
            VALUES (?,?,NULL,NULL,?,?,?,?,?,?,'posted',?,?)""",
            (batch_id, firm_id, entry_id, pdate, pdate, e["journal"], year, entry_number, user_id, now()))
        for idx,l in enumerate(lines):
            conn.execute("""INSERT INTO posting_lines
                (batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective)
                VALUES (?,?,?,?,?,?,?,?)""",
                (batch_id, idx, l["account_number"], l["account_label"], l["side"], int(l.get("amount_cents") or to_cents(l["amount"])), e["libelle"], e["journal"]))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id=?", (batch_id,)) or {"id": batch_id}


def reverse_manual_entry(firm_id: str, entry_id: str, user_id: str, reversal_date: str, reason: str) -> dict:
    original = query_one("SELECT * FROM posting_batches WHERE firm_id=? AND manual_entry_id=? AND reversal_of IS NULL ORDER BY posted_at DESC LIMIT 1", (firm_id, entry_id))
    if not original:
        raise ValueError("Manual entry has not been posted")
    if original.get("reversed_by"):
        raise ValueError("Manual entry has already been reversed")
    rdate = normalize_date(reversal_date)
    if not rdate:
        raise ValueError("A valid reversal date is required")
    year = fiscal_year(rdate)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, rdate):
            raise ValueError(f"Accounting period containing {rdate} is closed")
        entry_number = int(conn.execute("SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?", (firm_id, year)).fetchone()["n"])
        rid = new_id()
        conn.execute("""INSERT INTO posting_batches
            (id,firm_id,client_id,invoice_id,manual_entry_id,posting_date,document_date,journal_code,fiscal_year,entry_number,status,posted_by,posted_at,reversal_of,reversal_reason)
            VALUES (?,?,NULL,NULL,?,?,?,?,?,?,'posted',?,?,?,?)""",
            (rid, firm_id, entry_id, rdate, original["document_date"], original["journal_code"], year, entry_number, user_id, now(), original["id"], reason))
        lines = conn.execute("SELECT * FROM posting_lines WHERE batch_id=? ORDER BY line_idx", (original["id"],)).fetchall()
        for l in lines:
            side = "CREDIT" if l["side"] == "DEBIT" else "DEBIT"
            conn.execute("""INSERT INTO posting_lines
                (batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective)
                VALUES (?,?,?,?,?,?,?,?)""",
                (rid, l["line_idx"], l["account_number"], l["account_label"], side, l["amount_cents"], f"Contre-passation — {l['entry_label']}", l["source_perspective"]))
        conn.execute("UPDATE posting_batches SET reversed_by=? WHERE id=?", (rid, original["id"]))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id=?", (rid,)) or {"id": rid}


def post_bank_match(firm_id: str, tx_id: str, invoice_id: str, user_id: str | None) -> dict:
    """Post a confirmed bank/invoice match as a BQ journal entry.

    The invoice establishes whether the book owner is buyer or seller; therefore
    the counterpart is not guessed from cash-flow direction.
    """
    tx = query_one("""SELECT t.*,a.pcg_account,a.client_id AS bank_client_id FROM bank_transactions t
                      JOIN bank_accounts a ON a.id=t.bank_account_id
                      WHERE t.id=? AND t.firm_id=?""", (tx_id, firm_id))
    inv = query_one("SELECT * FROM invoices WHERE id=? AND firm_id=?", (invoice_id, firm_id))
    if not tx or not inv:
        raise ValueError("Transaction or invoice not found")
    if inv.get("posting_status") != "posted":
        raise ValueError("Invoice must be posted before its payment can be posted")
    existing = query_one("SELECT * FROM posting_batches WHERE firm_id=? AND bank_transaction_id=? AND reversal_of IS NULL", (firm_id, tx_id))
    if existing:
        return existing
    response = json.loads(inv.get("response_json") or "{}")
    extraction = response.get("step1_identification") or {}
    owner = book_owner_perspective(extraction)
    pdate = normalize_date(tx.get("date"))
    if not pdate:
        raise ValueError("Bank transaction has invalid date")
    amount_cents = abs(int(tx.get("amount_cents") or to_cents(tx.get("amount") or 0)))
    if amount_cents <= 0:
        raise ValueError("Bank transaction amount must be non-zero")
    bank_account = tx.get("pcg_account") or "5141"
    if owner == "Buyer":
        # Paying a supplier: reduce 4411 against bank. Direction must be outflow.
        if float(tx.get("amount") or 0) >= 0:
            raise ValueError("A purchase invoice cannot be settled by an incoming bank transaction")
        lines = [("4411", "Fournisseurs", "DEBIT"), (bank_account, "Banque", "CREDIT")]
        partner = extraction.get("supplier_name")
    else:
        if float(tx.get("amount") or 0) <= 0:
            raise ValueError("A sales invoice cannot be settled by an outgoing bank transaction")
        lines = [(bank_account, "Banque", "DEBIT"), ("3421", "Clients", "CREDIT")]
        partner = extraction.get("customer_name")
    year = fiscal_year(pdate)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, pdate):
            raise ValueError(f"Accounting period containing {pdate} is closed")
        num = int(conn.execute("SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?", (firm_id,year)).fetchone()["n"])
        bid = new_id()
        conn.execute("""INSERT INTO posting_batches
            (id,firm_id,client_id,invoice_id,bank_transaction_id,posting_date,document_date,journal_code,fiscal_year,entry_number,status,posted_by,posted_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'posted',?,?)""",
            (bid,firm_id,inv.get("client_id"),invoice_id,tx_id,pdate,pdate,"BQ",year,num,user_id or inv.get("reviewed_by") or inv.get("uploaded_by"),now()))
        label = f"Règlement {inv.get('invoice_number') or invoice_id[:8]} — {tx.get('label') or ''}".strip()
        for idx,(acc,acc_label,side) in enumerate(lines):
            conn.execute("""INSERT INTO posting_lines
                (batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective,aux_account,partner_name,tax_code)
                VALUES(?,?,?,?,?,?,?,?,?,?,NULL)""",
                (bid,idx,acc,acc_label,side,amount_cents,label,owner,extraction.get("auxiliary_account"),partner))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id=?", (bid,)) or {"id": bid}


def reverse_bank_match(firm_id: str, tx_id: str, user_id: str | None, reason: str = "Rapprochement annulé") -> dict | None:
    original = query_one("SELECT * FROM posting_batches WHERE firm_id=? AND bank_transaction_id=? AND reversal_of IS NULL ORDER BY posted_at DESC LIMIT 1", (firm_id,tx_id))
    if not original or original.get("reversed_by"):
        return None
    rdate = normalize_date(query_one("SELECT date FROM bank_transactions WHERE id=? AND firm_id=?", (tx_id,firm_id)).get("date"))
    year=fiscal_year(rdate)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn,firm_id,rdate):
            raise ValueError(f"Accounting period containing {rdate} is closed; reverse in an open period explicitly")
        num=int(conn.execute("SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?",(firm_id,year)).fetchone()["n"])
        rid=new_id()
        conn.execute("""INSERT INTO posting_batches
            (id,firm_id,client_id,invoice_id,bank_transaction_id,posting_date,document_date,journal_code,fiscal_year,entry_number,status,posted_by,posted_at,reversal_of,reversal_reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,'posted',?,?,?,?)""",
            (rid,firm_id,original.get("client_id"),original.get("invoice_id"),tx_id,rdate,rdate,"BQ",year,num,user_id or original.get("posted_by"),now(),original["id"],reason))
        for line in conn.execute("SELECT * FROM posting_lines WHERE batch_id=? ORDER BY line_idx",(original["id"],)).fetchall():
            side="CREDIT" if line["side"]=="DEBIT" else "DEBIT"
            conn.execute("""INSERT INTO posting_lines
                (batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective,aux_account,partner_name,tax_code)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (rid,line["line_idx"],line["account_number"],line["account_label"],side,line["amount_cents"],f"Contre-passation — {line['entry_label']}",line["source_perspective"],line["aux_account"],line["partner_name"],line["tax_code"]))
        conn.execute("UPDATE posting_batches SET reversed_by=? WHERE id=?",(rid,original["id"]))
        conn.commit()
    return query_one("SELECT * FROM posting_batches WHERE id=?",(rid,))


def posted_journal_rows(firm_id: str, client_id: str | None = None,
                        date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    where = "WHERE b.firm_id=? AND b.status='posted'"
    params: list = [firm_id]
    if client_id:
        where += " AND b.client_id=?"; params.append(client_id)
    if date_from:
        where += " AND b.posting_date>=?"; params.append(normalize_date(date_from) or date_from)
    if date_to:
        where += " AND b.posting_date<=?"; params.append(normalize_date(date_to) or date_to)
    rows = query(
        f"""SELECT b.id AS batch_id,b.invoice_id,b.manual_entry_id,b.bank_transaction_id,b.client_id,b.posting_date,b.journal_code,
                   b.entry_number,b.document_date,i.invoice_number,m.piece AS manual_piece,l.line_idx,l.account_number,
                   l.account_label,l.side,l.amount_cents,l.entry_label,l.source_perspective,l.aux_account,l.partner_name,l.tax_code
            FROM posting_batches b
            JOIN posting_lines l ON l.batch_id=b.id
            LEFT JOIN invoices i ON i.id=b.invoice_id
            LEFT JOIN manual_entries m ON m.id=b.manual_entry_id
            LEFT JOIN bank_transactions bt ON bt.id=b.bank_transaction_id
            {where}
            ORDER BY b.posting_date,b.entry_number,l.line_idx""",
        tuple(params),
    )
    return [{
        "invoice_id": r["invoice_id"] or (f"od-{r['manual_entry_id']}" if r.get("manual_entry_id") else None), "client_id": r["client_id"],
        "invoice_number": r["invoice_number"] or (r.get("manual_piece") or (f"{r['journal_code']}-{r['entry_number']}" if r.get("manual_entry_id") else None)), "date": r["posting_date"],
        "document_date": r["document_date"], "perspective": r["source_perspective"],
        "journal_code": r["journal_code"], "entry_number": r["entry_number"],
        "libelle": r["entry_label"], "side": r["side"],
        "account_number": r["account_number"], "account_label": r["account_label"],
        "amount": from_cents(r["amount_cents"]), "amount_cents": r["amount_cents"],
        "aux_account": r.get("aux_account"), "partner_name": r.get("partner_name"), "tax_code": r.get("tax_code"),
        "entry_idx": r["entry_number"],
        "line_idx": r["line_idx"], "source": "od" if r.get("manual_entry_id") else ("bank" if r.get("bank_transaction_id") else "posting"),
    } for r in rows]
