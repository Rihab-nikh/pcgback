"""Bank reconciliation: deterministic matching of bank transactions to
invoices. Same doctrine as the insights service — plain SQL + arithmetic,
every score explainable, no LLM call.

Scoring (0..1):
    amount equal (±1 MAD or ±1%)            0.60
    supplier name token found in the label  0.25
    invoice number found in label/reference 0.25  (capped with supplier at 0.30)
    date within 90 days                     0.10
A suggestion is stored when score >= 0.60; it never auto-confirms —
a human validates (status 'suggested' -> 'matched').
"""
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.invoices import _norm_number, _norm_supplier

SUGGEST_THRESHOLD = 0.60


def _date_days_apart(d1: str | None, d2: str | None) -> int | None:
    from datetime import date
    try:
        a = date.fromisoformat((d1 or "")[:10])
        b = date.fromisoformat((d2 or "")[:10])
        return abs((a - b).days)
    except ValueError:
        return None


def score_candidate(tx: dict, inv: dict) -> tuple[float, list[str]]:
    """Score one transaction/invoice pair; returns (score, reasons)."""
    score, reasons = 0.0, []
    tx_amount = abs((tx.get("amount_cents") or 0) / 100) if tx.get("amount_cents") is not None else abs(tx["amount"])
    for field in ("net_a_payer", "ttc"):
        inv_amount = inv.get(field)
        if inv_amount and abs(tx_amount - inv_amount) <= max(1.0, inv_amount * 0.01):
            score += 0.60
            reasons.append(f"montant {tx_amount:.2f} ≈ {field.replace('_', ' ')} {inv_amount:.2f}")
            break

    label_norm = _norm_supplier(tx["label"])
    name_score = 0.0
    supplier_norm = _norm_supplier(inv.get("supplier_name"))
    if supplier_norm and supplier_norm in label_norm:
        name_score += 0.25
        reasons.append(f"fournisseur « {inv['supplier_name']} » présent dans le libellé")
    num = _norm_number(inv.get("invoice_number"))
    haystack = _norm_number(tx["label"]) + _norm_number(tx.get("reference"))
    if num and len(num) >= 3 and num in haystack:
        name_score += 0.25
        reasons.append(f"n° de facture {inv['invoice_number']} retrouvé")
    score += min(name_score, 0.30)

    days = _date_days_apart(tx["date"], inv.get("invoice_date"))
    if days is not None and days <= 90:
        score += 0.10
        reasons.append(f"dates à {days} jour(s) d'écart")
    return round(min(score, 1.0), 2), reasons


def find_candidates(firm_id: str, tx: dict, limit: int = 5) -> list[dict]:
    """Ranked invoice candidates for one transaction (unmatched invoices only)."""
    tx_amount = abs((tx.get("amount_cents") or 0) / 100) if tx.get("amount_cents") is not None else abs(tx["amount"])
    tol = max(tx_amount * 0.05, 50.0)  # generous pre-filter; scoring narrows it
    invoices = query(
        """SELECT id, invoice_number, supplier_name, invoice_date, ttc, net_a_payer
           FROM invoices
           WHERE firm_id = ? AND status='approved' AND posting_status='posted'
             AND (ttc BETWEEN ? AND ? OR net_a_payer BETWEEN ? AND ?)
           ORDER BY created_at DESC LIMIT 200""",
        (firm_id, tx_amount - tol, tx_amount + tol, tx_amount - tol, tx_amount + tol))
    scored = []
    for inv in invoices:
        s, reasons = score_candidate(tx, inv)
        if s > 0:
            scored.append({**inv, "score": s, "reasons": reasons})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]


def suggest_entry(tx: dict, pcg_account: str) -> tuple[str, str]:
    """Conservative counterpart suggestion. Unknown movements go to a review
    bucket rather than pretending every outflow is a supplier and every inflow a customer."""
    label = (tx.get("label") or "").casefold()
    if any(k in label for k in ("frais", "commission", "agios", "tenue de compte")):
        return "6147", f"Frais/services bancaires à confirmer — D 6147 / C {pcg_account}"
    if any(k in label for k in ("salaire", "paie", "payroll")):
        return "4432", f"Règlement personnel à confirmer — D 4432 / C {pcg_account}"
    if any(k in label for k in ("tva", "impot", "impôt", "taxe", "dgi")):
        return "4458", f"Décaissement fiscal à classifier — D 4458 / C {pcg_account}"
    if any(k in label for k in ("virement interne", "transfert interne")):
        return "3497", "Transfert interne à rapprocher avec le compte bancaire opposé — classification requise"
    return "3497", "Contrepartie bancaire à classifier — aucune imputation tiers automatique"


def auto_match(firm_id: str, bank_account_id: str, pcg_account: str,
               transaction_ids: list[str]) -> int:
    """Run matching on freshly imported transactions. Returns #suggestions."""
    suggested = 0
    for tx_id in transaction_ids:
        rows = query("SELECT * FROM bank_transactions WHERE id = ? AND firm_id = ?",
                     (tx_id, firm_id))
        if not rows or rows[0]["status"] != "unmatched":
            continue
        tx = rows[0]
        account, label = suggest_entry(tx, pcg_account)
        execute("UPDATE bank_transactions SET suggested_account = ?, suggested_label = ? WHERE id = ?",
                (account, label, tx_id))
        candidates = find_candidates(firm_id, tx, limit=1)
        if candidates and candidates[0]["score"] >= SUGGEST_THRESHOLD:
            best = candidates[0]
            execute("""UPDATE bank_transactions
                       SET status = 'suggested', matched_invoice_id = ?,
                           match_confidence = ?, match_explanation = ?
                       WHERE id = ? AND firm_id = ?""",
                    (best["id"], best["score"], " · ".join(best["reasons"]), tx_id, firm_id))
            suggested += 1
    return suggested


def detect_duplicates(firm_id: str, bank_account_id: str, transaction_ids: list[str]) -> int:
    """Same account + same date + same amount + same normalized label = duplicate."""
    flagged = 0
    for tx_id in transaction_ids:
        rows = query("SELECT * FROM bank_transactions WHERE id = ? AND firm_id = ?",
                     (tx_id, firm_id))
        if not rows:
            continue
        tx = rows[0]
        twin = query(
            """SELECT id, label FROM bank_transactions
               WHERE firm_id = ? AND bank_account_id = ? AND id != ?
                 AND date = ? AND COALESCE(amount_cents,CAST(ROUND(amount*100) AS INTEGER)) = ? AND is_duplicate_of IS NULL
               ORDER BY created_at LIMIT 5""",
            (firm_id, bank_account_id, tx_id, tx["date"], int(tx.get("amount_cents") or round(float(tx["amount"])*100))))
        for t in twin:
            if _norm_supplier(t["label"]) == _norm_supplier(tx["label"]):
                execute("UPDATE bank_transactions SET is_duplicate_of = ? WHERE id = ?",
                        (t["id"], tx_id))
                flagged += 1
                break
    return flagged


def confirm_match(firm_id: str, tx_id: str, invoice_id: str, user_id: str | None, amount: float | None = None) -> None:
    from app.services.posting import to_cents
    tx = query_one("SELECT * FROM bank_transactions WHERE id=? AND firm_id=?", (tx_id, firm_id))
    inv = query_one("SELECT net_a_payer,net_a_payer_cents,status,posting_status FROM invoices WHERE id=? AND firm_id=?", (invoice_id, firm_id))
    if not tx or not inv:
        raise ValueError("Transaction or invoice not found")
    if inv.get("status") != "approved" or inv.get("posting_status") != "posted":
        raise ValueError("Payments can only be allocated to approved, posted invoices")
    paid = query_one("SELECT COALESCE(SUM(amount_cents),0) AS n FROM payment_allocations WHERE invoice_id=?", (invoice_id,))["n"]
    outstanding_cents = max(0, int(inv.get("net_a_payer_cents") or to_cents(inv["net_a_payer"] or 0)) - int(paid or 0))
    tx_cents = abs(int(tx.get("amount_cents") or to_cents(tx["amount"])))
    alloc_cents = min(to_cents(amount) if amount is not None else tx_cents, outstanding_cents)
    if alloc_cents <= 0:
        raise ValueError("No outstanding amount to allocate")
    execute("""UPDATE bank_transactions
               SET status = 'matched', matched_invoice_id = ?, matched_by = ?, matched_at = ?
               WHERE id = ? AND firm_id = ?""",
            (invoice_id, user_id, now(), tx_id, firm_id))
    execute("""INSERT INTO payment_allocations
               (id,firm_id,invoice_id,bank_transaction_id,amount_cents,allocated_by,allocated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (invoice_id, bank_transaction_id) DO UPDATE SET
                   id=excluded.id, firm_id=excluded.firm_id, amount_cents=excluded.amount_cents,
                   allocated_by=excluded.allocated_by, allocated_at=excluded.allocated_at""",
            (new_id(), firm_id, invoice_id, tx_id, alloc_cents, user_id, now()))
    from app.services.posting import post_bank_match
    post_bank_match(firm_id, tx_id, invoice_id, user_id)


def clear_match(firm_id: str, tx_id: str, user_id: str | None = None) -> None:
    from app.services.posting import reverse_bank_match
    reverse_bank_match(firm_id, tx_id, user_id)
    execute("""UPDATE bank_transactions
               SET status = 'unmatched', matched_invoice_id = NULL, match_confidence = NULL,
                   match_explanation = NULL, matched_by = NULL, matched_at = NULL
               WHERE id = ? AND firm_id = ?""", (tx_id, firm_id))
    execute("DELETE FROM payment_allocations WHERE firm_id=? AND bank_transaction_id=?", (firm_id, tx_id))


def account_summary(firm_id: str, accountant_id: str | None = None) -> list[dict]:
    """Per-account treasury dashboard: balance, unreconciled count, matched %."""
    where = "WHERE a.firm_id = ? AND a.is_archived = 0"
    params: list = [firm_id]
    if accountant_id:
        where += """ AND (a.client_id IS NULL OR a.client_id IN
                     (SELECT id FROM clients WHERE firm_id = ? AND assigned_to = ?))"""
        params += [firm_id, accountant_id]
    return query(f"""
        SELECT a.*, c.name AS client_name,
               COALESCE((SELECT ROUND(SUM(COALESCE(t.amount_cents,CAST(ROUND(t.amount*100) AS INTEGER)))/100.0, 2) FROM bank_transactions t
                         WHERE t.bank_account_id = a.id AND t.status != 'excluded'), 0) AS movement_total,
               (SELECT closing_balance_cents / 100.0 FROM bank_statements s
                WHERE s.bank_account_id=a.id AND s.closing_balance_cents IS NOT NULL
                ORDER BY s.created_at DESC LIMIT 1) AS balance,
               (SELECT COUNT(*) FROM bank_transactions t
                WHERE t.bank_account_id = a.id AND t.status IN ('unmatched', 'suggested')) AS unreconciled,
               (SELECT COUNT(*) FROM bank_transactions t
                WHERE t.bank_account_id = a.id AND t.status = 'matched') AS matched,
               (SELECT COUNT(*) FROM bank_transactions t
                WHERE t.bank_account_id = a.id AND t.status != 'excluded') AS total_tx
        FROM bank_accounts a LEFT JOIN clients c ON c.id = a.client_id
        {where} ORDER BY a.created_at""", tuple(params))
