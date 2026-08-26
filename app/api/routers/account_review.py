"""Fiche compte — le centre de révision.

Un seul appel agrège tout ce que le moteur calcule déjà pour un compte :
soldes, lettrage, TVA, doublons, séquences fournisseurs, pièces, évolution,
contreparties — et en déduit un ÉTAT DE RÉVISION justifié par des contrôles
factuels (jamais un « score IA » opaque) :
  revise > a_verifier > anomalies > bloque
La revue peut être marquée (POST) : « revu par X le … », persistée et auditée.
"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_permission
from app.api.routers.lettrage import _lettered_refs
from app.core.db import execute, new_id, now, query, query_one
from app.core.storage import StorageError, get_storage, object_key
from app.repositories import invoices as inv_repo
from app.repositories.system import audit
from app.services.close import sequence_gaps

router = APIRouter(prefix="/accounts", tags=["account-review"])
journal_view = require_permission("journal.view")
review_perm = require_permission("invoices.review")

TIERS_CLASSES = ("3", "4")


def _invoice_has_original(firm_id: str, inv: dict) -> bool:
    key = inv.get("file_path") or object_key("invoices", firm_id, inv["id"], inv.get("filename"))
    try:
        return get_storage().exists(key)
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")


class MarkReviewRequest(BaseModel):
    note: str | None = Field(None, max_length=500)


def _account_data(firm_id: str, account: str):
    rows = [r for r in inv_repo.journal_rows(firm_id) if r["account_number"] == account]
    if not rows:
        raise HTTPException(status_code=404, detail="Compte sans écriture")
    return rows


@router.get("/review-overview")
def review_overview(user: dict = Depends(journal_view)):
    """Vue chef de mission — tous les comptes avec leur état de révision,
    la dernière revue, et la progression par collaborateur. Un seul passage
    sur le journal : O(lignes), pas O(comptes × lignes)."""
    firm_id = user["firm_id"]
    lettered = _lettered_refs(firm_id)
    rows = inv_repo.journal_rows(firm_id)
    if not rows:
        return {"accounts": [], "summary": {"revise": 0, "a_verifier": 0,
                "anomalies": 0, "bloque": 0}, "reviewers": [], "total_auto_pct": None}

    invoices = {r["id"]: r for r in query(
        """SELECT id, verdict, is_duplicate_of, filename, file_path FROM invoices
           WHERE firm_id = ? AND status = 'approved'""", (firm_id,))}
    vat_invoice_ids = {r["invoice_id"] for r in query(
        "SELECT invoice_id FROM insights WHERE firm_id = ? AND dismissed = 0 "
        "AND kind = 'vat_deviation'", (firm_id,))}

    acc: dict[str, dict] = {}
    acc_invoices: dict[str, set] = {}
    for r in rows:
        a = acc.setdefault(r["account_number"], {
            "account_number": r["account_number"], "account_label": r["account_label"],
            "classe": r["account_number"][0], "entries": 0, "unlettered": 0,
            "last_move": None})
        a["entries"] += 1
        if (r["invoice_id"], r["entry_idx"], r["line_idx"]) not in lettered:
            a["unlettered"] += 1
        if r["date"] and (a["last_move"] is None or r["date"] > a["last_move"]):
            a["last_move"] = r["date"]
        acc_invoices.setdefault(r["account_number"], set()).add(r["invoice_id"])

    out = []
    for num, a in acc.items():
        ids = {i for i in acc_invoices[num] if not i.startswith("od-")}  # pièces IA
        invs = [invoices[i] for i in ids if i in invoices]
        invalid = any(i["verdict"] == "INVALID" for i in invs)
        dups = any(i["is_duplicate_of"] for i in invs)
        vat = any(i in vat_invoice_ids for i in ids)
        is_tiers = num[0] in TIERS_CLASSES
        missing = any(not _invoice_has_original(firm_id, i) for i in invs)
        etat = ("bloque" if invalid else "anomalies" if dups or vat
                else "a_verifier" if (is_tiers and a["unlettered"]) or missing
                else "revise")
        auto = sum(1 for i in invs if i["verdict"] == "VALID" and not i["is_duplicate_of"])
        out.append(a | {"etat": etat, "pieces": len(ids),
                        "auto_pct": round(auto / len(ids) * 100, 1) if ids else None,
                        "unlettered": a["unlettered"] if is_tiers else None})

    # dernière revue par compte
    for r in query("""SELECT account_number, created_at AS at, etat, by_name
                      FROM (
                        SELECT r.account_number, r.created_at, r.etat, u.full_name AS by_name,
                               ROW_NUMBER() OVER (PARTITION BY r.account_number ORDER BY r.created_at DESC) AS rn
                        FROM account_reviews r JOIN users u ON u.id = r.reviewed_by
                        WHERE r.firm_id = ?
                      ) latest WHERE rn = 1""", (firm_id,)):
        for a in out:
            if a["account_number"] == r["account_number"]:
                a["last_review"] = {"at": r["at"], "by_name": r["by_name"], "etat": r["etat"]}
    for a in out:
        a.setdefault("last_review", None)

    out.sort(key=lambda a: ({"bloque": 0, "anomalies": 1, "a_verifier": 2, "revise": 3}[a["etat"]],
                            a["account_number"]))
    summary = {k: sum(1 for a in out if a["etat"] == k)
               for k in ("revise", "a_verifier", "anomalies", "bloque")}
    reviewers = query("""SELECT u.full_name AS name, COUNT(*) AS reviews,
                         MAX(r.created_at) AS last_at
                         FROM account_reviews r JOIN users u ON u.id = r.reviewed_by
                         WHERE r.firm_id = ? GROUP BY r.reviewed_by, u.full_name
                         ORDER BY reviews DESC""", (firm_id,))
    all_pieces = {i for ids in acc_invoices.values() for i in ids if i in invoices}
    auto_all = sum(1 for i in all_pieces
                   if invoices[i]["verdict"] == "VALID" and not invoices[i]["is_duplicate_of"])
    return {"accounts": out, "summary": summary, "reviewers": reviewers,
            "total_auto_pct": round(auto_all / len(all_pieces) * 100, 1) if all_pieces else None}


@router.get("/{account_number}/review")
def account_review(account_number: str, user: dict = Depends(journal_view)):
    firm_id = user["firm_id"]
    rows = _account_data(firm_id, account_number)
    lettered = _lettered_refs(firm_id)
    all_ids = sorted({r["invoice_id"] for r in rows})
    od_count = sum(1 for i in all_ids if i.startswith("od-"))
    invoice_ids = [i for i in all_ids if not i.startswith("od-")]  # pièces IA réelles
    placeholders = ",".join("?" * len(invoice_ids)) or "''"
    invoices = {r["id"]: r for r in query(
        f"""SELECT id, invoice_number, supplier_name, invoice_date, ttc, filename, file_path,
            is_duplicate_of, verdict, confidence, created_at
            FROM invoices WHERE firm_id = ? AND id IN ({placeholders})""",
        (firm_id, *invoice_ids))}

    # ── Stats de tête ──
    total_debit = round(sum(r["amount"] for r in rows if r["side"] == "DEBIT"), 2)
    total_credit = round(sum(r["amount"] for r in rows if r["side"] == "CREDIT"), 2)
    solde = round(total_debit - total_credit, 2)
    unlettered = [r for r in rows
                  if (r["invoice_id"], r["entry_idx"], r["line_idx"]) not in lettered]
    is_tiers = account_number[0] in TIERS_CLASSES
    last_move = max((r["date"] or "" for r in rows), default=None) or None

    # ── Contrôles factuels (chaque ⚠ porte ses faits, jamais un verdict nu) ──
    checks: list[dict] = []

    invalid = [i for i in invoices.values() if i["verdict"] == "INVALID"]
    checks.append({"id": "equilibre", "label": "Équilibre des écritures",
                   "ok": not invalid,
                   "facts": [f"pièce {i['invoice_number']} : verdict INVALID" for i in invalid]
                   or [f"{len(invoice_ids)} pièces, toutes équilibrées et VALID"]})

    vat_ins = query(f"""SELECT i.message, v.invoice_number FROM insights i
                        JOIN invoices v ON v.id = i.invoice_id
                        WHERE i.firm_id = ? AND i.dismissed = 0 AND i.kind = 'vat_deviation'
                        AND i.invoice_id IN ({placeholders})""", (firm_id, *invoice_ids))
    checks.append({"id": "tva", "label": "Cohérence TVA",
                   "ok": not vat_ins,
                   "facts": [f"{x['invoice_number']} : {x['message']}" for x in vat_ins]
                   or ["aucune déviation par rapport à l'historique fournisseur"]})

    dups = []
    for i in invoices.values():
        if i["is_duplicate_of"] and i["is_duplicate_of"] in invoices:
            o = invoices[i["is_duplicate_of"]]
            dups.append({"invoice_number": i["invoice_number"], "invoice_id": i["id"],
                         "facts": ["même fournisseur : " + str(i["supplier_name"]),
                                   "même numéro : " + str(i["invoice_number"]),
                                   f"même montant TTC : {i['ttc']:.2f} MAD",
                                   f"pièce originale : {o['invoice_number']}"]})
        elif i["is_duplicate_of"]:
            dups.append({"invoice_number": i["invoice_number"], "invoice_id": i["id"],
                         "facts": ["doublon d'une pièce hors de ce compte"]})
    checks.append({"id": "doublons", "label": "Doublons",
                   "ok": not dups,
                   "facts": [f"{d['invoice_number']} — " + " · ".join(d["facts"]) for d in dups]
                   or ["aucun doublon détecté"]})

    if is_tiers:
        pct_lettered = round((1 - len(unlettered) / len(rows)) * 100, 1) if rows else 100.0
        checks.append({"id": "lettrage", "label": "Lettrage",
                       "ok": len(unlettered) == 0,
                       "facts": [f"{len(unlettered)} ligne(s) non lettrée(s) sur {len(rows)}"]
                       if unlettered else [f"{len(rows)} lignes, 100 % lettrées"]})
    else:
        pct_lettered = None

    missing_files = [i for i in invoices.values() if not _invoice_has_original(firm_id, i)]
    checks.append({"id": "pieces", "label": "Pièces justificatives",
                   "ok": not missing_files,
                   "facts": [f"original absent : {i['invoice_number']}" for i in missing_files]
                   or [f"{len(invoice_ids)} pièces, originaux tous archivés"]})

    if account_number == "4411":
        gaps = sequence_gaps(firm_id)
        checks.append({"id": "sequences", "label": "Séquences fournisseurs",
                       "ok": not gaps,
                       "facts": [f"{g['supplier']} : trou entre {g['after']} et {g['before']}"
                                 for g in gaps[:5]]
                       or ["aucune rupture de numérotation détectée"]})

    # ── État de révision (dérivé des contrôles, pas d'un score magique) ──
    if invalid:
        etat = "bloque"
    elif dups or vat_ins:
        etat = "anomalies"
    elif (is_tiers and unlettered) or missing_files:
        etat = "a_verifier"
    else:
        etat = "revise"

    # ── Métrique chef de mission : temps de révision économisé ──
    auto_ok = [i for i in invoices.values()
               if i["verdict"] == "VALID" and not i["is_duplicate_of"]]
    needs_touch = len(invoice_ids) - len(auto_ok)
    review_metrics = {
        "entries_analyzed": len(rows),
        "pieces_analyzed": len(invoice_ids),
        "auto_validated": len(auto_ok),
        "needs_intervention": needs_touch,
        "auto_pct": round(len(auto_ok) / len(invoice_ids) * 100, 1) if invoice_ids else 0.0,
    }

    # ── Santé (barres factuelles) — sur les pièces IA ; un compte 100 % OD
    #    n'a pas de pièce à noter ──
    n_pieces = len(invoice_ids) or 1
    sante = {
        "qualite": round(sum(1 for i in invoices.values() if i["verdict"] == "VALID")
                         / n_pieces * 100, 1) if invoice_ids else 100.0,
        "lettrage": pct_lettered,
        "tva": 100.0 if not vat_ins else round((1 - len(vat_ins) / n_pieces) * 100, 1),
        "pieces": round((1 - len(missing_files) / n_pieces) * 100, 1) if invoice_ids else 100.0,
    }

    # ── Évolution mensuelle + top contreparties ──
    monthly: dict[str, dict] = {}
    for r in rows:
        m = (r["date"] or "")[:7] or "?"
        mm = monthly.setdefault(m, {"month": m, "debit": 0.0, "credit": 0.0})
        mm["debit" if r["side"] == "DEBIT" else "credit"] += r["amount"]
    contreparties: dict[str, dict] = {}
    for r in rows:
        sup = invoices.get(r["invoice_id"], {}).get("supplier_name") or "—"
        c = contreparties.setdefault(sup, {"name": sup, "total": 0.0, "entries": 0})
        c["total"] = round(c["total"] + r["amount"], 2)
        c["entries"] += 1

    last_review = query_one(
        """SELECT r.created_at, r.etat, u.full_name AS reviewed_by_name
           FROM account_reviews r JOIN users u ON u.id = r.reviewed_by
           WHERE r.firm_id = ? AND r.account_number = ?
           ORDER BY r.created_at DESC LIMIT 1""", (firm_id, account_number))

    return {
        "account_number": account_number,
        "account_label": rows[0]["account_label"],
        "stats": {"solde": solde, "sens": "D" if solde >= 0 else "C",
                  "total_debit": total_debit, "total_credit": total_credit,
                  "entries": len(rows), "pieces": len(invoice_ids), "od_count": od_count,
                  "unlettered": len(unlettered) if is_tiers else None,
                  "last_move": last_move,
                  "avg_amount": round((total_debit + total_credit) / len(rows), 2)},
        "etat": etat, "checks": checks, "sante": sante,
        "review_metrics": review_metrics,
        "monthly": sorted(monthly.values(), key=lambda m: m["month"])[-12:],
        "contreparties": sorted(contreparties.values(), key=lambda c: -c["total"])[:8],
        "unlettered_lines": [
            {"invoice_id": r["invoice_id"], "invoice_number": r["invoice_number"],
             "date": r["date"], "side": r["side"], "amount": r["amount"]}
            for r in unlettered] if is_tiers else [],
        "last_review": last_review,
        "as_of": date.today().isoformat(),
    }


@router.post("/{account_number}/review", status_code=201)
def mark_reviewed(account_number: str, body: MarkReviewRequest,
                  user: dict = Depends(review_perm)):
    """« Revu par X le … » — persiste l'état constaté au moment de la revue."""
    current = account_review(account_number, user)
    rid = new_id()
    execute("""INSERT INTO account_reviews (id, firm_id, account_number, reviewed_by,
               entries_count, etat, note, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (rid, user["firm_id"], account_number, user["id"],
             current["stats"]["entries"], current["etat"], body.note, now()))
    audit("account.review", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="account", entity_id=account_number,
          detail=f"état {current['etat']} · {current['stats']['entries']} écritures")
    return {"id": rid, "etat": current["etat"],
            "entries": current["stats"]["entries"]}
