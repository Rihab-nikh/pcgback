"""Lettrage des comptes de tiers — rapprocher débits et crédits d'un même
compte (3421 clients, 4411 fournisseurs…) sous un code commun (A, B, … AA).

Règle d'or : un lettrage n'est accepté que si la somme des débits égale la
somme des crédits (tolérance 0,01). Les suggestions sont déterministes :
mêmes pièces d'abord, puis montants strictement égaux.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_permission
from app.core.db import execute, new_id, now, query, query_one
from app.repositories import invoices as inv_repo
from app.repositories.system import audit

router = APIRouter(prefix="/lettrage", tags=["lettrage"])
journal_view = require_permission("journal.view")

TIERS_CLASSES = ("3", "4")   # actif/passif circulant — comptes lettrables


class LineRef(BaseModel):
    invoice_id: str
    entry_idx: int = Field(..., ge=0)
    line_idx: int = Field(..., ge=0)


class LetterRequest(BaseModel):
    line_refs: list[LineRef] = Field(..., min_length=2)


def _code_for(n: int) -> str:
    """0->A … 25->Z, 26->AA …"""
    out = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _lettered_refs(firm_id: str) -> dict[tuple, str]:
    rows = query("""SELECT ll.invoice_id, ll.entry_idx, ll.line_idx, l.code
                    FROM lettrage_lines ll JOIN lettrages l ON l.id = ll.lettrage_id
                    WHERE ll.firm_id = ?""", (firm_id,))
    return {(r["invoice_id"], r["entry_idx"], r["line_idx"]): r["code"] for r in rows}


def _account_lines(firm_id: str, account: str) -> list[dict]:
    lettered = _lettered_refs(firm_id)
    out = []
    for r in inv_repo.journal_rows(firm_id):
        if r["account_number"] != account:
            continue
        key = (r["invoice_id"], r["entry_idx"], r["line_idx"])
        out.append(r | {"lettre": lettered.get(key)})
    out.sort(key=lambda r: (r["date"] or "", r["invoice_number"] or ""))
    return out


def _suggestions(lines: list[dict]) -> list[dict]:
    """Groupes équilibrés proposés au lettrage automatique."""
    free = [l for l in lines if not l["lettre"]]
    used: set[tuple] = set()
    sugg = []
    ref = lambda l: {"invoice_id": l["invoice_id"], "entry_idx": l["entry_idx"],  # noqa: E731
                     "line_idx": l["line_idx"]}
    key = lambda l: (l["invoice_id"], l["entry_idx"], l["line_idx"])  # noqa: E731
    # 1) même pièce: facture + règlement générés ensemble
    by_invoice: dict[str, list[dict]] = {}
    for l in free:
        by_invoice.setdefault(l["invoice_id"], []).append(l)
    for inv_id, group in by_invoice.items():
        d = round(sum(l["amount"] for l in group if l["side"] == "DEBIT"), 2)
        c = round(sum(l["amount"] for l in group if l["side"] == "CREDIT"), 2)
        if d == c and d > 0 and len(group) >= 2:
            sugg.append({"reason": "même pièce", "total": d,
                         "invoice_number": group[0]["invoice_number"],
                         "line_refs": [ref(l) for l in group]})
            used.update(key(l) for l in group)
    # 2) montants strictement égaux entre deux pièces
    debits = [l for l in free if l["side"] == "DEBIT" and key(l) not in used]
    credits = [l for l in free if l["side"] == "CREDIT" and key(l) not in used]
    for d in debits:
        m = next((c for c in credits if c["amount"] == d["amount"] and key(c) not in used), None)
        if m:
            used.update({key(d), key(m)})
            sugg.append({"reason": "montants égaux", "total": d["amount"],
                         "invoice_number": f'{d["invoice_number"]} ↔ {m["invoice_number"]}',
                         "line_refs": [ref(d), ref(m)]})
    return sugg


@router.get("/accounts")
def lettrable_accounts(user: dict = Depends(journal_view)):
    """Comptes de tiers (classes 3 & 4) avec leur avancement de lettrage."""
    lettered = _lettered_refs(user["firm_id"])
    acc: dict[str, dict] = {}
    for r in inv_repo.journal_rows(user["firm_id"]):
        if r["account_number"][0] not in TIERS_CLASSES:
            continue
        a = acc.setdefault(r["account_number"], {
            "account_number": r["account_number"], "account_label": r["account_label"],
            "lines": 0, "lettered": 0})
        a["lines"] += 1
        if (r["invoice_id"], r["entry_idx"], r["line_idx"]) in lettered:
            a["lettered"] += 1
    return sorted(acc.values(), key=lambda a: a["account_number"])


@router.get("/{account_number}")
def account_lettrage(account_number: str, user: dict = Depends(journal_view)):
    lines = _account_lines(user["firm_id"], account_number)
    groups = query("""SELECT code, total, created_at,
                      (SELECT full_name FROM users WHERE id = created_by) AS created_by_name
                      FROM lettrages WHERE firm_id = ? AND account_number = ?
                      ORDER BY created_at""", (user["firm_id"], account_number))
    return {"account_number": account_number,
            "lines": lines,
            "suggestions": _suggestions(lines),
            "groups": groups,
            "unlettered": sum(1 for l in lines if not l["lettre"])}


@router.post("/{account_number}", status_code=201)
def letter(account_number: str, body: LetterRequest, user: dict = Depends(journal_view)):
    firm_id = user["firm_id"]
    # Résoudre chaque ref sur les lignes réelles du compte (jamais de montants client)
    lines = {(l["invoice_id"], l["entry_idx"], l["line_idx"]): l
             for l in _account_lines(firm_id, account_number)}
    picked = []
    for ref in body.line_refs:
        line = lines.get((ref.invoice_id, ref.entry_idx, ref.line_idx))
        if not line:
            raise HTTPException(status_code=404, detail="Ligne introuvable sur ce compte")
        if line["lettre"]:
            raise HTTPException(status_code=409, detail=f"Ligne déjà lettrée ({line['lettre']})")
        picked.append(line)
    d = round(sum(l["amount"] for l in picked if l["side"] == "DEBIT"), 2)
    c = round(sum(l["amount"] for l in picked if l["side"] == "CREDIT"), 2)
    if abs(d - c) > 0.01 or d == 0:
        raise HTTPException(status_code=422,
                            detail=f"Lettrage déséquilibré : débits {d:.2f} ≠ crédits {c:.2f}")
    n = query_one("SELECT COUNT(*) AS n FROM lettrages WHERE firm_id = ? AND account_number = ?",
                  (firm_id, account_number))["n"]
    code = _code_for(n)
    lid = new_id()
    execute("""INSERT INTO lettrages (id, firm_id, account_number, code, total, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (lid, firm_id, account_number, code, d, user["id"], now()))
    for l in picked:
        execute("""INSERT INTO lettrage_lines (lettrage_id, firm_id, invoice_id, entry_idx, line_idx, side, amount)
                   VALUES (?,?,?,?,?,?,?)""",
                (lid, firm_id, l["invoice_id"], l["entry_idx"], l["line_idx"], l["side"], l["amount"]))
    audit("lettrage.create", user_id=user["id"], firm_id=firm_id,
          entity_type="lettrage", entity_id=lid,
          detail=f"{account_number} {code} = {d:.2f} ({len(picked)} lignes)")
    return {"code": code, "total": d, "lines": len(picked)}


@router.delete("/{account_number}/{code}", status_code=204)
def unletter(account_number: str, code: str, user: dict = Depends(journal_view)):
    row = query_one("SELECT id FROM lettrages WHERE firm_id = ? AND account_number = ? AND code = ?",
                    (user["firm_id"], account_number, code))
    if not row:
        raise HTTPException(status_code=404, detail="Lettrage introuvable")
    execute("DELETE FROM lettrage_lines WHERE lettrage_id = ?", (row["id"],))
    execute("DELETE FROM lettrages WHERE id = ?", (row["id"],))
    audit("lettrage.delete", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="lettrage", entity_id=row["id"], detail=f"{account_number} {code}")
