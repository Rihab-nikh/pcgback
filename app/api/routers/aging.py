"""Balance âgée — outil de pilotage, pas un simple tableau 30/60/90.

Définition comptable stricte : l'encours d'un tiers = ses lignes de journal
NON LETTRÉES sur le compte de tiers (4411 fournisseurs, 3421 clients).
Chaque solde est décomposé facture par facture (jours de retard inclus) et le
niveau de risque est accompagné de raisons déterministes — jamais un score
opaque. « Chaque chiffre est explicable. »
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_permission
from app.api.routers.lettrage import _lettered_refs
from app.core.db import query
from app.repositories import invoices as inv_repo

router = APIRouter(prefix="/aging", tags=["aging"])
journal_view = require_permission("journal.view")

KINDS = {"fournisseurs": "4411", "clients": "3421"}
BUCKETS = ((0, 30, "b0_30"), (31, 60, "b31_60"), (61, 90, "b61_90"), (91, 10**6, "b90"))


def _days_overdue(ref_date: str | None, today: date) -> int:
    if not ref_date:
        return 0
    try:
        d = datetime.fromisoformat(ref_date[:10]).date()
    except ValueError:
        return 0
    return max(0, (today - d).days)


def _bucket(days: int) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= days <= hi:
            return name
    return "b90"


@router.get("")
def aged_balance(kind: str = "fournisseurs", user: dict = Depends(journal_view)):
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(KINDS)}")
    account = KINDS[kind]
    firm_id = user["firm_id"]
    today = date.today()
    lettered = _lettered_refs(firm_id)

    # due_date per invoice (extraction) — fallback to invoice date happens per line
    due_dates = {r["id"]: r["due"] for r in query(
        """SELECT id, json_extract(response_json, '$.step1_identification.due_date') AS due
           FROM invoices WHERE firm_id = ? AND response_json IS NOT NULL""", (firm_id,))}
    client_names = {r["id"]: r["name"] for r in query(
        "SELECT id, name FROM clients WHERE firm_id = ?", (firm_id,))}
    inv_meta = {r["id"]: r for r in query(
        "SELECT id, supplier_name, client_id FROM invoices WHERE firm_id = ?", (firm_id,))}

    # outstanding per invoice on this account = signed sum of UNLETTERED lines
    per_invoice: dict[str, dict] = {}
    for r in inv_repo.journal_rows(firm_id):
        if r["account_number"] != account:
            continue
        if (r["invoice_id"], r["entry_idx"], r["line_idx"]) in lettered:
            continue
        inv = per_invoice.setdefault(r["invoice_id"], {
            "invoice_id": r["invoice_id"], "invoice_number": r["invoice_number"],
            "date": r["date"], "signed": 0.0, "lines": 0})
        inv["signed"] += r["amount"] if r["side"] == "DEBIT" else -r["amount"]
        inv["lines"] += 1

    # group by counterparty — DIRECTIONNEL : pour les fournisseurs (compte
    # créditeur) l'encours d'une pièce = crédits − débits ; pour les clients,
    # l'inverse. Un règlement isolé (débit 4411 non lettré) réduit l'encours
    # au lieu d'en créer un.
    tiers: dict[str, dict] = {}
    for inv_id, inv in per_invoice.items():
        outstanding = round(-inv["signed"] if kind == "fournisseurs" else inv["signed"], 2)
        if outstanding < 0.01:
            continue  # réglée/à cheval : rien à relancer sur cette pièce
        meta = inv_meta.get(inv_id, {})
        name = (meta.get("supplier_name") if kind == "fournisseurs"
                else client_names.get(meta.get("client_id"))) or "—"
        ref_date = due_dates.get(inv_id) or inv["date"]
        days = _days_overdue(ref_date, today)
        t = tiers.setdefault(name, {"name": name, "total": 0.0,
            "b0_30": 0.0, "b31_60": 0.0, "b61_90": 0.0, "b90": 0.0, "invoices": []})
        t["total"] = round(t["total"] + outstanding, 2)
        t[_bucket(days)] = round(t[_bucket(days)] + outstanding, 2)
        t["invoices"].append({
            "invoice_id": inv_id, "invoice_number": inv["invoice_number"],
            "amount": outstanding, "ref_date": ref_date, "days_overdue": days,
            "due_known": bool(due_dates.get(inv_id))})

    rows = []
    for t in tiers.values():
        t["invoices"].sort(key=lambda i: -i["days_overdue"])
        oldest = t["invoices"][0]["days_overdue"] if t["invoices"] else 0
        reasons = []
        if t["b90"] > 0:
            reasons.append(f"encours de plus de 90 jours : {t['b90']:.2f} MAD")
        if oldest > 0:
            reasons.append(f"pièce la plus ancienne : {oldest} jours")
        reasons.append(f"{len(t['invoices'])} pièce(s) non lettrée(s), aucun règlement associé")
        if t["total"] > 0 and len(t["invoices"]) >= 3:
            reasons.append("plusieurs pièces accumulées sans lettrage")
        risk = ("eleve" if t["b90"] > 0 or oldest > 90
                else "moyen" if t["b61_90"] > 0 or oldest > 60
                else "faible")
        rows.append(t | {"risk": risk, "reasons": reasons, "oldest_days": oldest})

    rows.sort(key=lambda r: -r["total"])
    totals = {k: round(sum(r[k] for r in rows), 2)
              for k in ("total", "b0_30", "b31_60", "b61_90", "b90")}
    return {"kind": kind, "account": account, "rows": rows, "totals": totals,
            "as_of": today.isoformat()}
