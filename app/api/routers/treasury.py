"""Treasury: bank accounts, statement import (CSV/CAMT.053/MT940),
reconciliation. Import -> parse -> duplicate detection -> auto-match
(suggestions only; a human confirms every match)."""
import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import accountant_scope, firm_admin_only, firm_member, require_permission

treasury_manage = require_permission("treasury.manage")
from app.api.routers.clients import _get_visible_client
from app.api.schemas import BankAccountCreateRequest, BankAccountUpdateRequest, MatchRequest
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.system import audit, notify
from app.services import reconciliation as recon
from app.services.bank_import import StatementParseError, _norm_amount, parse_statement
from app.services.posting import from_cents, to_cents

router = APIRouter(prefix="/treasury", tags=["treasury"])

_MAX_STATEMENT_BYTES = 10 * 1024 * 1024


def _get_visible_account(account_id: str, user: dict) -> dict:
    acc = query_one("SELECT * FROM bank_accounts WHERE id = ? AND firm_id = ?",
                    (account_id, user["firm_id"]))
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    if acc.get("client_id"):
        _get_visible_client(acc["client_id"], user)  # accountant assignment check
    return acc


def _get_visible_tx(tx_id: str, user: dict) -> dict:
    tx = query_one("SELECT * FROM bank_transactions WHERE id = ? AND firm_id = ?",
                   (tx_id, user["firm_id"]))
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    _get_visible_account(tx["bank_account_id"], user)
    return tx


# ── Accounts ──
@router.get("/accounts")
def list_accounts(user: dict = Depends(firm_member)):
    return recon.account_summary(user["firm_id"], accountant_scope(user))


@router.post("/accounts", status_code=201)
def create_account(body: BankAccountCreateRequest, admin: dict = Depends(firm_admin_only)):
    if body.client_id:
        _get_visible_client(body.client_id, admin)
    aid = new_id()
    execute("""INSERT INTO bank_accounts (id, firm_id, client_id, name, bank_name, rib,
               currency, pcg_account, is_archived, created_at) VALUES (?,?,?,?,?,?,?,?,0,?)""",
            (aid, admin["firm_id"], body.client_id or None, body.name, body.bank_name,
             body.rib, body.currency, body.pcg_account, now()))
    audit("bank_account.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="bank_account", entity_id=aid, detail=body.name)
    return query_one("SELECT * FROM bank_accounts WHERE id = ?", (aid,))


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, body: BankAccountUpdateRequest,
                   admin: dict = Depends(firm_admin_only)):
    _get_visible_account(account_id, admin)
    sets, params = [], []
    for field in ("name", "bank_name", "rib", "pcg_account"):
        value = getattr(body, field)
        if value is not None:
            sets.append(f"{field} = ?"); params.append(value)
    if body.is_archived is not None:
        sets.append("is_archived = ?"); params.append(1 if body.is_archived else 0)
    if sets:
        execute(f"UPDATE bank_accounts SET {', '.join(sets)} WHERE id = ? AND firm_id = ?",
                tuple(params + [account_id, admin["firm_id"]]))
    audit("bank_account.update", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="bank_account", entity_id=account_id)
    return query_one("SELECT * FROM bank_accounts WHERE id = ?", (account_id,))


# ── Statement import ──
@router.post("/accounts/{account_id}/import", status_code=201)
async def import_statement(account_id: str, file: UploadFile = File(...),
                           opening_balance: str | None = None, closing_balance: str | None = None,
                           user: dict = Depends(treasury_manage)):
    acc = _get_visible_account(account_id, user)
    contents = await file.read()
    if len(contents) > _MAX_STATEMENT_BYTES:
        raise HTTPException(status_code=422, detail="File exceeds 10 MB")
    try:
        fmt, lines = parse_statement(file.filename or "statement.csv", contents)
    except StatementParseError as e:
        raise HTTPException(status_code=422, detail=str(e))

    statement_hash = hashlib.sha256(contents).hexdigest()
    if query_one("SELECT id FROM bank_statements WHERE firm_id=? AND bank_account_id=? AND statement_hash=?",
                 (user["firm_id"], account_id, statement_hash)):
        raise HTTPException(status_code=409, detail="This exact bank statement was already imported")
    if opening_balance is None or closing_balance is None:
        raise HTTPException(status_code=422, detail="opening_balance and closing_balance are required for bank-statement control")
    ob = _norm_amount(opening_balance)
    cb = _norm_amount(closing_balance)
    if opening_balance is not None and ob is None:
        raise HTTPException(status_code=422, detail="Invalid opening_balance")
    if closing_balance is not None and cb is None:
        raise HTTPException(status_code=422, detail="Invalid closing_balance")
    debit_total = round(sum(abs(float(x["amount"])) for x in lines if float(x["amount"]) < 0), 2)
    credit_total = round(sum(float(x["amount"]) for x in lines if float(x["amount"]) > 0), 2)
    control_diff = round((ob + credit_total - debit_total) - cb, 2)
    if abs(control_diff) > 0.01:
        raise HTTPException(status_code=422, detail=f"Bank statement does not reconcile: opening + credits - debits - closing = {control_diff:.2f} MAD")
    period_start = min(x["date"] for x in lines) if lines else None
    period_end = max(x["date"] for x in lines) if lines else None

    # Insert the statement first — bank_transactions.statement_id has a FK to it.
    statement_id = new_id()
    execute("""INSERT INTO bank_statements (id, firm_id, bank_account_id, filename, format,
               imported_by, transaction_count, duplicate_count, created_at, statement_hash,
               opening_balance_cents, closing_balance_cents, debit_total_cents, credit_total_cents,
               control_difference_cents, period_start, period_end)
               VALUES (?,?,?,?,?,?,0,0,?,?,?,?,?,?,?,?,?)""",
            (statement_id, user["firm_id"], account_id, file.filename or "statement",
             fmt, user["id"], now(), statement_hash, to_cents(ob) if ob is not None else None,
             to_cents(cb) if cb is not None else None, to_cents(debit_total), to_cents(credit_total),
             to_cents(control_diff) if control_diff is not None else None, period_start, period_end))
    tx_ids: list[str] = []
    for line in lines:
        tx_id = new_id()
        execute("""INSERT INTO bank_transactions
                   (id, firm_id, bank_account_id, statement_id, date, value_date, label,
                    reference, amount, amount_cents, currency, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'unmatched',?)""",
                (tx_id, user["firm_id"], account_id, statement_id, line["date"],
                 line["value_date"], line["label"], line["reference"], line["amount"], to_cents(line["amount"]),
                 acc["currency"], now()))
        tx_ids.append(tx_id)

    duplicates = recon.detect_duplicates(user["firm_id"], account_id, tx_ids)
    suggested = recon.auto_match(user["firm_id"], account_id, acc["pcg_account"], tx_ids)

    execute("UPDATE bank_statements SET transaction_count = ?, duplicate_count = ? WHERE id = ?",
            (len(tx_ids), duplicates, statement_id))
    audit("bank_statement.import", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="bank_statement", entity_id=statement_id,
          detail=f"{fmt}: {len(tx_ids)} tx, {suggested} suggérées, {duplicates} doublons")
    notify(user["firm_id"], "bank_import",
           f"Relevé « {file.filename} » importé sur {acc['name']} : {len(tx_ids)} transactions, "
           f"{suggested} rapprochement(s) suggéré(s), {duplicates} doublon(s) détecté(s).")
    return {"statement_id": statement_id, "format": fmt, "transactions": len(tx_ids),
            "suggested": suggested, "duplicates": duplicates,
            "opening_balance": ob, "closing_balance": cb,
            "debit_total": debit_total, "credit_total": credit_total,
            "control_difference": control_diff,
            "control_ok": abs(control_diff) <= 0.01}


@router.get("/statements")
def list_statements(account_id: str | None = None, user: dict = Depends(firm_member)):
    where, params = "WHERE s.firm_id = ?", [user["firm_id"]]
    if account_id:
        _get_visible_account(account_id, user)
        where += " AND s.bank_account_id = ?"; params.append(account_id)
    return query(f"""SELECT s.*, a.name AS account_name,
                     (SELECT full_name FROM users WHERE id = s.imported_by) AS imported_by_name
                     FROM bank_statements s JOIN bank_accounts a ON a.id = s.bank_account_id
                     {where} ORDER BY s.created_at DESC LIMIT 100""", tuple(params))


# ── Transactions & reconciliation ──
@router.get("/transactions")
def list_transactions(account_id: str | None = None, status: str | None = None,
                      q: str | None = None, limit: int = 50, offset: int = 0,
                      user: dict = Depends(firm_member)):
    where, params = "WHERE t.firm_id = ?", [user["firm_id"]]
    if account_id:
        _get_visible_account(account_id, user)
        where += " AND t.bank_account_id = ?"; params.append(account_id)
    elif accountant_scope(user):
        where += """ AND t.bank_account_id IN
                     (SELECT id FROM bank_accounts WHERE firm_id = ? AND (client_id IS NULL
                      OR client_id IN (SELECT id FROM clients WHERE firm_id = ? AND assigned_to = ?)))"""
        params += [user["firm_id"], user["firm_id"], user["id"]]
    if status:
        where += " AND t.status = ?"; params.append(status)
    if q:
        where += " AND (t.label LIKE ? OR t.reference LIKE ?)"; params += [f"%{q}%"] * 2
    total = query_one(f"SELECT COUNT(*) AS n FROM bank_transactions t {where}", tuple(params))["n"]
    rows = query(f"""SELECT t.*, a.name AS account_name, i.invoice_number AS matched_invoice_number,
                     i.supplier_name AS matched_supplier
                     FROM bank_transactions t
                     JOIN bank_accounts a ON a.id = t.bank_account_id
                     LEFT JOIN invoices i ON i.id = t.matched_invoice_id
                     {where} ORDER BY t.date DESC, t.created_at DESC LIMIT ? OFFSET ?""",
                 tuple(params + [min(limit, 200), max(offset, 0)]))
    return {"total": total, "items": rows, "limit": limit, "offset": offset}


@router.get("/transactions/{tx_id}/candidates")
def match_candidates(tx_id: str, user: dict = Depends(firm_member)):
    """Ranked invoice candidates with score + reasons, for the manual match dialog."""
    tx = _get_visible_tx(tx_id, user)
    return recon.find_candidates(user["firm_id"], tx)


@router.post("/transactions/{tx_id}/match")
def match_transaction(tx_id: str, body: MatchRequest, user: dict = Depends(treasury_manage)):
    """Confirm a match (from a suggestion or manually chosen invoice)."""
    tx = _get_visible_tx(tx_id, user)
    inv = query_one("SELECT id FROM invoices WHERE id = ? AND firm_id = ?",
                    (body.invoice_id, user["firm_id"]))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    try:
        recon.confirm_match(user["firm_id"], tx_id, body.invoice_id, user["id"], body.amount)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit("bank_tx.match", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="bank_transaction", entity_id=tx_id, detail=body.invoice_id)
    notify(user["firm_id"], "reconciliation",
           f"Transaction « {tx['label'][:60]} » rapprochée d'une facture.", user_id=user["id"])
    return query_one("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,))


@router.post("/transactions/{tx_id}/unmatch")
def unmatch_transaction(tx_id: str, user: dict = Depends(treasury_manage)):
    _get_visible_tx(tx_id, user)
    recon.clear_match(user["firm_id"], tx_id, user["id"])
    audit("bank_tx.unmatch", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="bank_transaction", entity_id=tx_id)
    return query_one("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,))


@router.post("/transactions/{tx_id}/request-receipt")
def request_receipt(tx_id: str, user: dict = Depends(treasury_manage)):
    """Ask the firm for the missing receipt behind a bank line — creates a
    firm-wide notification and an audit entry."""
    tx = _get_visible_tx(tx_id, user)
    notify(user["firm_id"], "receipt_requested",
           f"Justificatif demandé pour la transaction « {tx['label'][:60]} » "
           f"du {tx['date']} ({tx['amount']:.2f} {tx['currency']}) — demandé par {user['full_name']}.")
    audit("bank_tx.request_receipt", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="bank_transaction", entity_id=tx_id)
    return {"id": tx_id, "requested": True}


@router.post("/transactions/{tx_id}/exclude")
def exclude_transaction(tx_id: str, user: dict = Depends(treasury_manage)):
    """Exclude a line from reconciliation (bank fees, internal transfers…)."""
    _get_visible_tx(tx_id, user)
    execute("UPDATE bank_transactions SET status = 'excluded' WHERE id = ? AND firm_id = ?",
            (tx_id, user["firm_id"]))
    audit("bank_tx.exclude", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="bank_transaction", entity_id=tx_id)
    return query_one("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,))
