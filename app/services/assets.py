"""Fixed-asset register and controlled straight-line depreciation.

The register is populated from authoritative class-2 posting lines. Depreciation
can be previewed without assumptions; posting requires firm-configured
accumulated-depreciation accounts so the engine never invents statutory codes.
"""
from __future__ import annotations

import calendar
import json
from datetime import date

from app.core.db import connect, lock_posting_sequence, new_id, now, query, query_one
from app.services.dates import normalize_date
from app.services.posting import from_cents


def list_assets(firm_id: str, client_id: str | None = None, status: str = "active") -> list[dict]:
    where = "WHERE a.firm_id=?"
    params: list = [firm_id]
    if client_id:
        where += " AND a.client_id=?"; params.append(client_id)
    if status:
        where += " AND a.status=?"; params.append(status)
    rows = query(f"""SELECT a.*,i.invoice_number FROM fixed_assets a
                     LEFT JOIN invoices i ON i.id=a.invoice_id
                     {where} ORDER BY acquisition_date,id""", tuple(params))
    for r in rows:
        r["acquisition_cost"] = from_cents(r["acquisition_cost_cents"])
        r["residual_value"] = from_cents(r["residual_value_cents"])
        r["accumulated_depreciation"] = from_cents(r["accumulated_depreciation_cents"])
        r["net_book_value"] = from_cents(max(0, r["acquisition_cost_cents"] - r["accumulated_depreciation_cents"]))
    return rows


def _period_end(period: str) -> date:
    year, month = map(int, period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def _firm_asset_settings(firm_id: str) -> dict:
    row = query_one("SELECT settings FROM firms WHERE id=?", (firm_id,))
    try:
        return json.loads((row or {}).get("settings") or "{}")
    except Exception:
        return {}


def depreciation_preview(firm_id: str, period: str, client_id: str | None = None) -> dict:
    end = _period_end(period)
    settings = _firm_asset_settings(firm_id)
    accum_map = settings.get("depreciation_accounts", {})
    expense_account = settings.get("depreciation_expense_account", "6193")
    items = []
    total = 0
    for a in list_assets(firm_id, client_id):
        in_service = normalize_date(a.get("in_service_date") or a.get("acquisition_date"))
        if not in_service or date.fromisoformat(in_service) > end:
            continue
        depreciable = max(0, a["acquisition_cost_cents"] - a["residual_value_cents"])
        remaining = max(0, depreciable - a["accumulated_depreciation_cents"])
        monthly = int(round(depreciable / max(1, int(a["useful_life_months"]))))
        amount = min(remaining, monthly)
        if amount <= 0:
            continue
        accumulated_account = accum_map.get(a["account_number"])
        item = {
            "asset_id": a["id"], "description": a["description"], "asset_account": a["account_number"],
            "expense_account": expense_account, "accumulated_account": accumulated_account,
            "amount_cents": amount, "amount": from_cents(amount),
            "ready_to_post": bool(accumulated_account),
        }
        items.append(item); total += amount
    return {
        "period": period, "items": items, "total_cents": total, "total": from_cents(total),
        "ready_to_post": bool(items) and all(x["ready_to_post"] for x in items),
        "configuration_required": sorted({x["asset_account"] for x in items if not x["accumulated_account"]}),
        "note": "Configure firms.settings.depreciation_accounts per asset account before posting; no statutory accumulated-depreciation account is guessed.",
    }


def post_depreciation(firm_id: str, period: str, user_id: str, client_id: str | None = None) -> dict:
    preview = depreciation_preview(firm_id, period, client_id)
    if not preview["items"]:
        raise ValueError("No depreciation to post for this period")
    if not preview["ready_to_post"]:
        raise ValueError("Depreciation account configuration required for: " + ", ".join(preview["configuration_required"]))
    existing = query_one("SELECT * FROM depreciation_runs WHERE firm_id=? AND client_id IS ? AND period=?", (firm_id, client_id, period))
    if existing:
        return existing
    pdate = _period_end(period).isoformat()
    from app.services.posting import _period_is_open
    year = int(period[:4])
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lock_posting_sequence(conn, firm_id, year)
        if not _period_is_open(conn, firm_id, pdate):
            raise ValueError(f"Accounting period containing {pdate} is closed")
        run_id = new_id()
        conn.execute("INSERT INTO depreciation_runs(id,firm_id,client_id,period,status,created_by,created_at) VALUES(?,?,?,?, 'posted',?,?)",
                     (run_id, firm_id, client_id, period, user_id, now()))
        entry_number = int(conn.execute("SELECT COALESCE(MAX(entry_number),0)+1 AS n FROM posting_batches WHERE firm_id=? AND fiscal_year=?", (firm_id, year)).fetchone()["n"])
        batch_id = new_id()
        conn.execute("""INSERT INTO posting_batches
            (id,firm_id,client_id,posting_date,document_date,journal_code,fiscal_year,entry_number,status,posted_by,posted_at)
            VALUES(?,?,?,?,?,'OD',?,?,'posted',?,?)""",
            (batch_id,firm_id,client_id,pdate,pdate,year,entry_number,user_id,now()))
        idx = 0
        # aggregate accounts while retaining per-asset depreciation details
        totals: dict[tuple[str,str], int] = {}
        for item in preview["items"]:
            conn.execute("INSERT INTO depreciation_lines(run_id,asset_id,amount_cents,expense_account,accumulated_account) VALUES(?,?,?,?,?)",
                         (run_id,item["asset_id"],item["amount_cents"],item["expense_account"],item["accumulated_account"]))
            totals[(item["expense_account"], item["accumulated_account"])] = totals.get((item["expense_account"], item["accumulated_account"]),0)+item["amount_cents"]
            conn.execute("UPDATE fixed_assets SET accumulated_depreciation_cents=accumulated_depreciation_cents+? WHERE id=?",
                         (item["amount_cents"],item["asset_id"]))
        for (expense,accum),amount in totals.items():
            for acc,label,side in ((expense,"Dotations aux amortissements","DEBIT"),(accum,"Amortissements cumulés","CREDIT")):
                conn.execute("""INSERT INTO posting_lines(batch_id,line_idx,account_number,account_label,side,amount_cents,entry_label,source_perspective)
                                VALUES(?,?,?,?,?,?,?,?)""",
                             (batch_id,idx,acc,label,side,amount,f"Dotation amortissements {period}","OD")); idx += 1
        conn.commit()
    return query_one("SELECT * FROM depreciation_runs WHERE id=?", (run_id,)) or {"id":run_id}
