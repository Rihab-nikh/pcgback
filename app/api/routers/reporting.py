"""Dashboards, journal ledger, exports, monthly reports, notifications, audit,
knowledge management, invoice explainability, and extended outcome metrics."""
import csv
import io
import json as _json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import accountant_scope, firm_admin_only, firm_member, require_permission

journal_view = require_permission("journal.view")
reports_view = require_permission("reports.view")
from app.core.db import execute, now, query, query_one
from app.repositories import clients as clients_repo
from app.repositories import invoices as inv_repo
from app.repositories import users as users_repo
from app.repositories.system import list_audit, list_notifications, mark_read
from app.services.close import close_readiness, fec_export
from app.services.explain import full_explain
from app.services.health import firm_health, outcome_metrics
from app.services.insights import firm_open_insights

router = APIRouter(tags=["reporting"])


class CloseCheckUpdate(BaseModel):
    status: str
    note: str | None = None


# ── Dashboard ──────────────────────────────────────────────────────────────
@router.get("/dashboard")
def dashboard(user: dict = Depends(firm_member)):
    """Role-aware dashboard: accountant sees their scope, firm admin sees the firm."""
    scope = accountant_scope(user)
    stats = inv_repo.firm_stats(user["firm_id"], accountant_id=scope)
    clients = clients_repo.list_clients(user["firm_id"], assigned_to=scope)
    recent = inv_repo.list_invoices(user["firm_id"], accountant_id=scope, limit=8)
    out = {
        "stats": stats,
        "client_count": len(clients),
        "clients_with_pending": [c for c in clients if c["pending_count"] > 0][:6],
        "recent_invoices": recent["items"],
        "notifications_unread": len(list_notifications(user["firm_id"], user["id"], unread_only=True)),
    }
    if user["role"] == "firm_admin":
        out["team"] = users_repo.list_firm_users(user["firm_id"])
    return out


# ── Accountant workspace dashboard (v8) ───────────────────────────────────
@router.get("/dashboard/accountant")
def accountant_dashboard(user: dict = Depends(firm_member)):
    """Cards + charts for the accountant home screen. All numbers computed
    from the firm's invoice history — no invented figures."""
    firm_id = user["firm_id"]
    today = now()[:10]

    def count(where: str, params: tuple = ()) -> int:
        return query_one(f"SELECT COUNT(*) AS n FROM invoices WHERE firm_id = ? AND {where}",
                         (firm_id, *params))["n"] or 0

    documents_waiting = count("status IN ('processing','needs_review')")
    published_today = count("status = 'approved' AND substr(reviewed_at,1,10) = ?", (today,))
    needs_review = count("status = 'needs_review'")

    auto_published = query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE firm_id=? AND action='invoice.auto_publish'",
        (firm_id,))["n"] or 0

    ocr = query_one("SELECT AVG(extraction_confidence) AS c FROM invoices WHERE firm_id = ? "
                    "AND extraction_confidence IS NOT NULL", (firm_id,))["c"]
    # Same estimate basis as /metrics/outcomes/extended: 3 min per auto-published invoice.
    time_saved_hours = round(auto_published * 3.0 / 60, 1)

    monthly_documents = query(
        """SELECT substr(COALESCE(invoice_date, created_at), 1, 7) AS month, COUNT(*) AS count
           FROM invoices WHERE firm_id = ? GROUP BY month ORDER BY month DESC LIMIT 12""",
        (firm_id,))

    spend_categories = query(
        """SELECT COALESCE(json_extract(response_json, '$.step1_identification.invoice_category'),
                           'autre') AS category,
                  COUNT(*) AS count, ROUND(SUM(COALESCE(ttc, 0)), 2) AS total
           FROM invoices WHERE firm_id = ? AND status IN ('approved','needs_review')
           GROUP BY category ORDER BY total DESC""", (firm_id,))

    supplier_distribution = query(
        """SELECT MIN(supplier_name) AS supplier, COUNT(*) AS count,
                  ROUND(SUM(COALESCE(ttc, 0)), 2) AS total
           FROM invoices WHERE firm_id = ? AND supplier_name IS NOT NULL
           GROUP BY LOWER(supplier_name) ORDER BY count DESC LIMIT 8""", (firm_id,))

    recent_activity = list_audit(firm_id, limit=12)
    connected_suppliers = query_one(
        "SELECT COUNT(*) AS n FROM supplier_connections WHERE firm_id = ?", (firm_id,))["n"] or 0
    bank_matches = query_one(
        "SELECT COUNT(*) AS n FROM bank_transactions WHERE firm_id = ? AND status = 'matched'",
        (firm_id,))["n"] or 0

    return {
        "cards": {
            "documents_waiting": documents_waiting,
            "published_today": published_today,
            "needs_review": needs_review,
            "auto_published": auto_published,
            "ocr_accuracy": round(ocr * 100, 1) if ocr is not None else None,
            "time_saved_hours": time_saved_hours,
            "connected_suppliers": connected_suppliers,
            "bank_matches": bank_matches,
        },
        "monthly_documents": list(reversed(monthly_documents)),
        "spend_categories": spend_categories,
        "supplier_distribution": supplier_distribution,
        "recent_activity": recent_activity,
        "notifications": list_notifications(firm_id, user["id"], unread_only=True)[:8],
    }


# ── Journal ledger ─────────────────────────────────────────────────────────
@router.get("/journal")
def journal_ledger(client_id: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, user: dict = Depends(journal_view)):
    rows = inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
    debit = round(sum(r["amount"] for r in rows if r["side"] == "DEBIT"), 2)
    credit = round(sum(r["amount"] for r in rows if r["side"] == "CREDIT"), 2)
    return {"lines": rows, "total_debit": debit, "total_credit": credit,
            "balanced": abs(debit - credit) < 0.01}


# ── Grand livre & balance générale (v11) ──────────────────────────────────
# Both are pure aggregations of the journal the engine already generated —
# no new bookkeeping, so they can never disagree with the journal.

def _classe(account: str) -> str:
    return account[0] if account else "?"


@router.get("/ledger")
def general_ledger(client_id: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, user: dict = Depends(journal_view)):
    """Grand livre — one row per account: movement totals and balance."""
    rows = inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
    accounts: dict[str, dict] = {}
    for r in rows:
        a = accounts.setdefault(r["account_number"], {
            "account_number": r["account_number"], "account_label": r["account_label"],
            "classe": _classe(r["account_number"]),
            "total_debit": 0.0, "total_credit": 0.0, "entries": 0})
        a["total_debit" if r["side"] == "DEBIT" else "total_credit"] += r["amount"]
        a["entries"] += 1
    out = []
    for a in sorted(accounts.values(), key=lambda x: x["account_number"]):
        solde = round(a["total_debit"] - a["total_credit"], 2)
        out.append(a | {
            "total_debit": round(a["total_debit"], 2),
            "total_credit": round(a["total_credit"], 2),
            "solde": solde,
            "sens": "D" if solde > 0 else "C" if solde < 0 else "=",
        })
    return {"accounts": out, "count": len(out)}


@router.get("/ledger/{account_number}")
def account_ledger(account_number: str, client_id: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   user: dict = Depends(journal_view)):
    """Mouvements d'un compte, ordre chronologique, avec solde progressif."""
    rows = [r for r in inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
            if r["account_number"] == account_number]
    rows.sort(key=lambda r: (r["date"] or "", r["invoice_number"] or ""))
    running = 0.0
    out = []
    for r in rows:
        running += r["amount"] if r["side"] == "DEBIT" else -r["amount"]
        out.append(r | {"solde": round(running, 2)})
    return {"account_number": account_number,
            "account_label": rows[0]["account_label"] if rows else None,
            "lines": out,
            "total_debit": round(sum(r["amount"] for r in rows if r["side"] == "DEBIT"), 2),
            "total_credit": round(sum(r["amount"] for r in rows if r["side"] == "CREDIT"), 2),
            "solde": round(running, 2)}


@router.get("/balance")
def trial_balance(client_id: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, user: dict = Depends(journal_view)):
    """Balance générale — par compte et par classe, avec totaux (équilibre)."""
    ledger = general_ledger(client_id, date_from, date_to, user)["accounts"]
    total_debit = round(sum(a["total_debit"] for a in ledger), 2)
    total_credit = round(sum(a["total_credit"] for a in ledger), 2)
    classes: dict[str, dict] = {}
    for a in ledger:
        c = classes.setdefault(a["classe"], {"classe": a["classe"],
            "total_debit": 0.0, "total_credit": 0.0, "accounts": 0})
        c["total_debit"] = round(c["total_debit"] + a["total_debit"], 2)
        c["total_credit"] = round(c["total_credit"] + a["total_credit"], 2)
        c["accounts"] += 1
    return {"accounts": ledger,
            "classes": sorted(classes.values(), key=lambda c: c["classe"]),
            "total_debit": total_debit, "total_credit": total_credit,
            "balanced": abs(total_debit - total_credit) < 0.01}


# ── États financiers justifiables (v14) ───────────────────────────────────
# PROJECTION de la balance — pas un moteur séparé. Chaque poste est un
# mapping de préfixes PCG ; chaque montant se déroule : poste → comptes →
# grand livre → pièce. Un compte au solde contraire apparaît en négatif dans
# sa rubrique naturelle (lisible par un CAC, jamais masqué).

# préfixe (le plus long gagne) -> (état, rubrique)
_STATEMENT_MAP = [
    # Bilan — actif immobilisé / corrections de valeur
    ("21", ("actif", "Immobilisations en non-valeurs")),
    ("22", ("actif", "Immobilisations incorporelles")),
    ("231", ("actif", "Terrains")),
    ("232", ("actif", "Constructions")),
    ("233", ("actif", "Installations techniques, matériel et outillage")),
    ("234", ("actif", "Matériel de transport")),
    ("235", ("actif", "Mobilier, matériel de bureau et aménagements")),
    ("238", ("actif", "Autres immobilisations corporelles")),
    ("24", ("actif", "Immobilisations financières")),
    ("25", ("actif", "Immobilisations financières")),
    ("28", ("actif", "Amortissements des immobilisations")),   # solde créditeur = diminution de l'actif
    ("29", ("actif", "Provisions pour dépréciation des immobilisations")),
    # Actif circulant
    ("31", ("actif", "Stocks")),
    ("32", ("actif", "Stocks")),
    ("33", ("actif", "Stocks")),
    ("34", ("actif", "Créances de l'actif circulant")),
    ("35", ("actif", "Titres et valeurs de placement")),
    ("37", ("actif", "Ecarts de conversion — Actif")),
    ("39", ("actif", "Provisions pour dépréciation de l'actif circulant")),
    ("514", ("actif", "Banques, trésorerie disponible")),
    ("516", ("actif", "Caisses")),
    ("51", ("actif", "Trésorerie — Actif")),
    # Financement / passif
    ("11", ("passif", "Capitaux propres")),
    ("13", ("passif", "Capitaux propres assimilés")),
    ("14", ("passif", "Dettes de financement")),
    ("15", ("passif", "Provisions durables pour risques et charges")),
    ("17", ("passif", "Ecarts de conversion — Passif")),
    ("44", ("passif", "Dettes du passif circulant")),
    ("45", ("passif", "Autres provisions pour risques et charges")),
    ("55", ("passif", "Trésorerie — Passif")),
    # CPC — rubriques par nature
    ("61", ("cpc", "Charges d'exploitation")),
    ("63", ("cpc", "Charges financières")),
    ("65", ("cpc", "Charges non courantes")),
    ("67", ("cpc", "Impôts sur les résultats")),
    ("71", ("cpc", "Produits d'exploitation")),
    ("73", ("cpc", "Produits financiers")),
    ("75", ("cpc", "Produits non courants")),
    # Conservative fallbacks
    ("1", ("passif", "Financement permanent — autres")),
    ("2", ("actif", "Actif immobilisé — autres")),
    ("3", ("actif", "Actif circulant — autres")),
    ("4", ("passif", "Passif circulant — autres")),
    ("5", ("actif", "Trésorerie — Actif")),
    ("6", ("cpc", "Charges — autres")),
    ("7", ("cpc", "Produits — autres")),
]


def _map_account(number: str, solde: float) -> tuple[str, str]:
    for prefix, dest in sorted(_STATEMENT_MAP, key=lambda p: -len(p[0])):
        if number.startswith(prefix):
            state, rubrique = dest
            # Trésorerie active (51/514/516) with a credit balance is an
            # overdraft/liability.  Because prefixes are matched longest-first,
            # checking only ``prefix == "5"`` misses 5141/5161 entirely.
            if state == "actif" and number.startswith("5") and solde < 0:
                return ("passif", "Trésorerie — Passif")
            return (state, rubrique)
    return ("actif", "Autres")


@router.get("/financial-statements")
def financial_statements(client_id: str | None = None, date_from: str | None = None,
                         date_to: str | None = None, user: dict = Depends(journal_view)):
    """Moroccan Bilan/CPC projection from the authoritative posted ledger.

    Balance-sheet rubriques are cumulative through ``date_to``; CPC rubriques
    are period movements. When ``date_from`` is supplied the response exposes
    opening balances, and when dates are valid ISO dates it also exposes the
    corresponding prior-year comparison. No statement amount is calculated
    from invoice JSON.
    """
    from datetime import date, timedelta
    from app.services.dates import normalize_date

    n_from, n_to = normalize_date(date_from), normalize_date(date_to)
    # A Bilan is a closing position, not merely the movement in the selected period.
    closing_bal = trial_balance(client_id, None, n_to, user)
    period_bal = trial_balance(client_id, n_from, n_to, user)

    opening_by_account: dict[str, float] = {}
    if n_from:
        opening_to = (date.fromisoformat(n_from) - timedelta(days=1)).isoformat()
        opening = trial_balance(client_id, None, opening_to, user)
        opening_by_account = {a["account_number"]: float(a["solde"]) for a in opening["accounts"]}

    def prior_year_iso(value: str | None) -> str | None:
        if not value:
            return None
        d = date.fromisoformat(value)
        try:
            return d.replace(year=d.year - 1).isoformat()
        except ValueError:  # 29 February -> 28 February
            return d.replace(year=d.year - 1, day=28).isoformat()

    py_from, py_to = prior_year_iso(n_from), prior_year_iso(n_to)
    py_closing = trial_balance(client_id, None, py_to, user) if py_to else None
    py_period = trial_balance(client_id, py_from, py_to, user) if py_to else None

    def build(balance: dict, include: str, opening_map: dict[str, float] | None = None) -> dict:
        rubriques: dict[tuple[str, str], dict] = {}
        for a in balance["accounts"]:
            state, rub = _map_account(a["account_number"], a["solde"])
            if include == "bilan" and state == "cpc":
                continue
            if include == "cpc" and state != "cpc":
                continue
            solde = float(a["solde"])
            if state == "actif":
                amount = solde
            elif state == "passif":
                amount = -solde
            else:
                amount = solde if rub.startswith(("Charges", "Impôts")) else -solde
            op = 0.0
            if opening_map is not None:
                op_solde = float(opening_map.get(a["account_number"], 0.0))
                op = op_solde if state == "actif" else -op_solde if state == "passif" else 0.0
            r = rubriques.setdefault((state, rub), {"rubrique": rub, "total": 0.0,
                                                    "opening_balance": 0.0, "accounts": []})
            r["total"] = round(r["total"] + amount, 2)
            r["opening_balance"] = round(r["opening_balance"] + op, 2)
            r["accounts"].append({"account_number": a["account_number"],
                                  "account_label": a["account_label"],
                                  "opening_balance": round(op, 2), "amount": round(amount, 2)})
        out: dict[str, list[dict]] = {}
        for (state, _), r in rubriques.items():
            r["accounts"].sort(key=lambda x: x["account_number"])
            out.setdefault(state, []).append(r)
        for state in out:
            out[state].sort(key=lambda r: r["rubrique"])
        return out

    bs = build(closing_bal, "bilan", opening_by_account if n_from else None)
    cp = build(period_bal, "cpc")
    actif, passif, cpc = bs.get("actif", []), bs.get("passif", []), cp.get("cpc", [])
    charges = round(sum(r["total"] for r in cpc if r["rubrique"].startswith(("Charges", "Impôts"))), 2)
    produits = round(sum(r["total"] for r in cpc if r["rubrique"].startswith("Produits")), 2)
    resultat = round(produits - charges, 2)
    passif.append({"rubrique": "Résultat net de l'exercice (Produits − Charges)",
                   "total": resultat, "opening_balance": 0.0, "accounts": []})
    total_actif = round(sum(r["total"] for r in actif), 2)
    total_passif = round(sum(r["total"] for r in passif), 2)

    comparative = None
    if py_to and py_closing and py_period:
        py_bs = build(py_closing, "bilan")
        py_cp = build(py_period, "cpc")
        py_cpc = py_cp.get("cpc", [])
        py_charges = round(sum(r["total"] for r in py_cpc if r["rubrique"].startswith(("Charges", "Impôts"))), 2)
        py_produits = round(sum(r["total"] for r in py_cpc if r["rubrique"].startswith("Produits")), 2)
        comparative = {
            "period": {"date_from": py_from, "date_to": py_to},
            "bilan": {"actif": py_bs.get("actif", []), "passif": py_bs.get("passif", []),
                      "total_actif": round(sum(r["total"] for r in py_bs.get("actif", [])), 2),
                      "total_passif_before_result": round(sum(r["total"] for r in py_bs.get("passif", [])), 2)},
            "cpc": {"rubriques": py_cpc, "charges": py_charges, "produits": py_produits,
                    "resultat": round(py_produits - py_charges, 2)},
        }

    return {
        "period": {"date_from": n_from, "date_to": n_to},
        "bilan": {"actif": actif, "passif": passif,
                  "total_actif": total_actif, "total_passif": total_passif,
                  "equilibre": abs(total_actif - total_passif) < 0.01},
        "cpc": {"rubriques": cpc, "charges": charges, "produits": produits, "resultat": resultat},
        "comparative_prior_year": comparative,
        "source": "projection du grand livre comptabilisé — Bilan cumulatif, CPC par période; aucun calcul parallèle",
    }


# ── Accounting reconciliations & fixed assets ─────────────────────────────
@router.get("/reconciliations/tax")
def tax_reconciliation(client_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
                       user: dict = Depends(reports_view)):
    rows = inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
    def net(account_prefix: str, natural: str) -> float:
        debit = sum(r["amount"] for r in rows if r["account_number"].startswith(account_prefix) and r["side"] == "DEBIT")
        credit = sum(r["amount"] for r in rows if r["account_number"].startswith(account_prefix) and r["side"] == "CREDIT")
        return round((debit-credit) if natural=="debit" else (credit-debit),2)
    input_vat = round(net("34551","debit") + net("34552","debit"),2)
    output_vat = net("4455","credit")
    withholding = net("4452","credit")
    # Document-level VAT expected only for posted invoice source batches, once per invoice.
    where="WHERE firm_id=? AND posting_status='posted' AND response_json IS NOT NULL"; params=[user["firm_id"]]
    if client_id: where += " AND client_id=?"; params.append(client_id)
    if date_from: where += " AND posting_date>=?"; params.append(date_from)
    if date_to: where += " AND posting_date<=?"; params.append(date_to)
    docs=query(f"SELECT response_json,document_direction FROM invoices {where}",tuple(params))
    expected_input=expected_output=0.0
    for d in docs:
        try: resp=_json.loads(d["response_json"]); vat=float(resp.get("step2_calculations",{}).get("tva_amount") or 0)
        except Exception: continue
        if str(d.get("document_direction") or "").startswith("purchase"): expected_input += vat if "credit_note" not in d["document_direction"] else -vat
        else: expected_output += vat if "credit_note" not in d["document_direction"] else -vat
    expected_input=round(expected_input,2); expected_output=round(expected_output,2)
    return {
        "input_vat": input_vat, "expected_input_vat_from_documents": expected_input,
        "input_difference": round(input_vat-expected_input,2),
        "output_vat": output_vat, "expected_output_vat_from_documents": expected_output,
        "output_difference": round(output_vat-expected_output,2),
        "withholding_liability": withholding,
        "balanced_to_documents": abs(input_vat-expected_input)<=0.02 and abs(output_vat-expected_output)<=0.02,
        "source": "posted ledger ↔ posted source documents",
    }


@router.get("/reconciliations/subledgers")
def subsidiary_reconciliation(client_id: str | None = None, date_to: str | None = None, user: dict = Depends(reports_view)):
    rows=inv_repo.journal_rows(user["firm_id"],client_id,None,date_to)
    def ledger_credit_balance(account: str) -> float:
        d=sum(r["amount"] for r in rows if r["account_number"]==account and r["side"]=="DEBIT")
        c=sum(r["amount"] for r in rows if r["account_number"]==account and r["side"]=="CREDIT")
        return round(c-d,2)
    ap_gl=ledger_credit_balance("4411")
    ar_gl=round(-ledger_credit_balance("3421"),2)
    where="WHERE i.firm_id=? AND i.posting_status='posted'"; params=[user["firm_id"]]
    if client_id: where += " AND i.client_id=?"; params.append(client_id)
    if date_to: where += " AND i.posting_date<=?"; params.append(date_to)
    docs=query(f"""SELECT i.id,i.document_direction,COALESCE(i.net_a_payer_cents,CAST(ROUND(i.net_a_payer*100) AS INTEGER)) due_cents,
             COALESCE((SELECT SUM(p.amount_cents) FROM payment_allocations p WHERE p.invoice_id=i.id),0) paid_cents
             FROM invoices i {where}""",tuple(params))
    ap=ar=0
    for d in docs:
        outstanding=max(0,int(d["due_cents"] or 0)-int(d["paid_cents"] or 0))
        direction=d.get("document_direction") or "purchase"
        if direction=="purchase": ap+=outstanding
        elif direction=="sale": ar+=outstanding
        elif direction=="purchase_credit_note": ap-=int(d["due_cents"] or 0)
        elif direction=="sale_credit_note": ar-=int(d["due_cents"] or 0)
    ap_sub=round(ap/100,2); ar_sub=round(ar/100,2)
    return {
        "accounts_payable": {"general_ledger":ap_gl,"subsidiary":ap_sub,"difference":round(ap_gl-ap_sub,2)},
        "accounts_receivable": {"general_ledger":ar_gl,"subsidiary":ar_sub,"difference":round(ar_gl-ar_sub,2)},
        "reconciled": abs(ap_gl-ap_sub)<=0.02 and abs(ar_gl-ar_sub)<=0.02,
    }


@router.get("/fixed-assets")
def fixed_assets(client_id: str | None = None, user: dict = Depends(reports_view)):
    from app.services.assets import list_assets
    return {"assets":list_assets(user["firm_id"],client_id)}


@router.get("/fixed-assets/depreciation-preview")
def fixed_assets_depreciation_preview(period: str, client_id: str | None = None, user: dict = Depends(reports_view)):
    from app.services.assets import depreciation_preview
    try: return depreciation_preview(user["firm_id"],period,client_id)
    except Exception as e: raise HTTPException(status_code=422,detail=str(e))


@router.post("/fixed-assets/depreciation")
def fixed_assets_depreciation_post(period: str, client_id: str | None = None, user: dict = Depends(firm_admin_only)):
    from app.services.assets import post_depreciation
    try: return post_depreciation(user["firm_id"],period,user["id"],client_id)
    except ValueError as e: raise HTTPException(status_code=409,detail=str(e))


# ── Exports ────────────────────────────────────────────────────────────────
@router.get("/exports/journal.csv")
def export_journal_csv(client_id: str | None = None, date_from: str | None = None,
                       date_to: str | None = None, user: dict = Depends(journal_view)):
    rows = inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Date", "N° Facture", "Perspective", "Libellé", "Sens",
                     "Compte", "Intitulé", "Montant (MAD)"])
    for r in rows:
        writer.writerow([r["date"], r["invoice_number"], r["perspective"], r["libelle"],
                         r["side"], r["account_number"], r["account_label"],
                         f"{r['amount']:.2f}"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=journal.csv"})


@router.get("/exports/fec")
def export_fec(client_id: str | None = None, date_from: str | None = None,
               date_to: str | None = None, user: dict = Depends(journal_view)):
    """FEC export — importable by Sage, Cegid, Quadratus (land-and-expand bridge)."""
    content = fec_export(user["firm_id"], client_id, date_from, date_to)
    return StreamingResponse(iter([content]), media_type="text/plain",
                             headers={"Content-Disposition": "attachment; filename=export_fec.txt"})


@router.get("/exports/journal.fec")
def export_journal_fec(client_id: str | None = None, date_from: str | None = None,
                       date_to: str | None = None, user: dict = Depends(journal_view)):
    """Alias for /exports/fec — served as journal_fec.txt for the UI export button."""
    content = fec_export(user["firm_id"], client_id, date_from, date_to)
    return StreamingResponse(iter([content]), media_type="text/plain",
                             headers={"Content-Disposition": "attachment; filename=journal_fec.txt"})


@router.get("/exports/journal.odoo")
def export_journal_odoo(client_id: str | None = None, date_from: str | None = None,
                        date_to: str | None = None, user: dict = Depends(journal_view)):
    """Odoo-compatible CSV: account.move.line import format."""
    rows = inv_repo.journal_rows(user["firm_id"], client_id, date_from, date_to)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Journal", "Date", "Référence", "Compte", "Libellé du compte",
                     "Débit", "Crédit", "Partenaire"])
    for r in rows:
        debit = f"{r['amount']:.2f}" if r["side"] == "DEBIT" else "0.00"
        credit = f"{r['amount']:.2f}" if r["side"] == "CREDIT" else "0.00"
        writer.writerow([r.get("journal_code") or "OD", r["date"] or "", r["invoice_number"] or "",
                         r["account_number"], r["account_label"], debit, credit, r.get("partner_name") or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=journal_odoo.csv"})


# ── Reports ────────────────────────────────────────────────────────────────
@router.get("/reports/monthly")
def monthly_report(client_id: str | None = None, user: dict = Depends(reports_view)):
    return {"months": inv_repo.monthly_report(user["firm_id"], client_id)}


@router.get("/validation/summary")
def validation_summary(user: dict = Depends(firm_member)):
    scope = accountant_scope(user)
    result = inv_repo.list_invoices(user["firm_id"], accountant_id=scope, limit=200)
    failing: dict[str, int] = {}
    invalid_ids: list[str] = []
    for item in result["items"]:
        if item["verdict"] == "INVALID":
            invalid_ids.append(item["id"])
            full = inv_repo.get_invoice(item["id"], user["firm_id"])
            for check in (full.get("response") or {}).get("validation_checks", []):
                if not check["passed"]:
                    failing[check["description"]] = failing.get(check["description"], 0) + 1
    return {"invalid_count": len(invalid_ids), "invalid_invoice_ids": invalid_ids,
            "failing_checks": [{"description": k, "count": v}
                               for k, v in sorted(failing.items(), key=lambda kv: -kv[1])]}


# ── Notifications ──────────────────────────────────────────────────────────
@router.get("/notifications")
def notifications(unread_only: bool = False, user: dict = Depends(firm_member)):
    return list_notifications(user["firm_id"], user["id"], unread_only=unread_only)


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, user: dict = Depends(firm_member)):
    mark_read(notification_id, user["firm_id"])
    return {"id": notification_id, "is_read": True}


# ── Audit / Insights / Health / Close ─────────────────────────────────────
@router.get("/audit")
def firm_audit(limit: int = 100, admin: dict = Depends(firm_admin_only)):
    return list_audit(admin["firm_id"], limit=min(limit, 500))


@router.get("/close/readiness")
def close_check(client_id: str | None = None, month: str | None = None,
                user: dict = Depends(firm_member)):
    return close_readiness(user["firm_id"], client_id, month)


@router.get("/insights")
def open_insights(user: dict = Depends(firm_member)):
    return firm_open_insights(user["firm_id"])


@router.post("/insights/{insight_id}/dismiss")
def dismiss_insight(insight_id: str, user: dict = Depends(firm_member)):
    if not query_one("SELECT 1 FROM insights WHERE id = ? AND firm_id = ?",
                     (insight_id, user["firm_id"])):
        raise HTTPException(status_code=404, detail="Insight not found")
    execute("UPDATE insights SET dismissed = 1 WHERE id = ? AND firm_id = ?",
            (insight_id, user["firm_id"]))
    return {"id": insight_id, "dismissed": True}


@router.get("/timeline")
def submission_timeline(limit: int = 100, user: dict = Depends(firm_member)):
    """Submission history: every invoice event (upload, processed, published,
    rejected, edited, archived) from the audit trail, newest first."""
    return query("""
        SELECT a.id, a.action, a.detail, a.created_at, a.entity_id AS invoice_id,
               u.full_name AS user_name,
               i.invoice_number, i.supplier_name, i.filename, i.status AS invoice_status
        FROM audit_logs a
        LEFT JOIN users u ON u.id = a.user_id
        LEFT JOIN invoices i ON i.id = a.entity_id
        WHERE a.firm_id = ? AND a.entity_type = 'invoice'
        ORDER BY a.created_at DESC LIMIT ?""", (user["firm_id"], min(limit, 500)))


@router.get("/health/clients")
def clients_health(user: dict = Depends(firm_member)):
    return firm_health(user["firm_id"], assigned_to=accountant_scope(user))


# ── Invoice explainability ─────────────────────────────────────────────────
@router.get("/invoices/{invoice_id}/explain")
def explain_invoice(invoice_id: str, user: dict = Depends(firm_member)):
    """Single call: split_confidence + risk + accounting_reasoning + ai_suggestions."""
    from app.api.routers.invoices import _get_visible_invoice
    inv = _get_visible_invoice(invoice_id, user)
    if not inv.get("response"):
        raise HTTPException(status_code=409, detail="Invoice not yet processed")
    return full_explain(user["firm_id"], inv)


# ── Invoice audit trail ────────────────────────────────────────────────────
@router.get("/invoices/{invoice_id}/audit")
def invoice_audit(invoice_id: str, user: dict = Depends(firm_member)):
    """Full audit trail: who created/reviewed/approved + field-level edit history."""
    from app.api.routers.invoices import _get_visible_invoice
    inv = _get_visible_invoice(invoice_id, user)
    edits = query(
        """SELECT e.edit_session_id, e.created_at, u.full_name AS user_name,
                  e.field, e.old_value, e.new_value, e.comment
           FROM invoice_edits e
           LEFT JOIN users u ON u.id = e.user_id
           WHERE e.invoice_id = ? ORDER BY e.created_at, e.edit_session_id""",
        (invoice_id,),
    )
    created_by = query_one("SELECT full_name FROM users WHERE id = ?", (inv["uploaded_by"],))
    approved_by = None
    if inv.get("status") == "approved" and inv.get("reviewed_by"):
        approved_by = query_one("SELECT full_name FROM users WHERE id = ?", (inv["reviewed_by"],))
    return {
        "invoice_id": invoice_id,
        "created_by_name": created_by["full_name"] if created_by else None,
        "reviewed_by_name": inv.get("reviewed_by_name"),
        "approved_by_name": approved_by["full_name"] if approved_by and inv.get("status") == "approved" else None,
        "created_at": inv["created_at"],
        "reviewed_at": inv.get("reviewed_at"),
        "edits": edits,
    }


# ── Knowledge management ───────────────────────────────────────────────────
@router.get("/knowledge")
def firm_knowledge(user: dict = Depends(firm_member)):
    """Firm knowledge base with extended fields (locked, rule_source, learned_from)."""
    rows = query("SELECT * FROM supplier_priors WHERE firm_id = ? ORDER BY confirmations DESC",
                 (user["firm_id"],))
    out = []
    for r in rows:
        learned = []
        if r["tva_pct"] is not None:
            learned.append(f"TVA {r['tva_pct']:g} %")
        if r["is_immobilisation"] is not None:
            learned.append("immobilisation (classe 2)" if r["is_immobilisation"] else "charge (classe 6)")
        if r["immobilisation_type"]:
            learned.append(f"type: {r['immobilisation_type']}")
        corrections_n = query_one(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE firm_id = ? AND action = 'invoice.edit'",
            (user["firm_id"],),
        )["n"] or 0
        approvals_est = max(0, (r["confirmations"] or 0) - corrections_n)
        out.append({
            "supplier": r["supplier_norm"],
            "confirmations": r["confirmations"],
            "established": r["confirmations"] >= 2,
            "learned": learned,
            "updated_at": r["updated_at"],
            "summary": f"{', '.join(learned) or '—'} — confirmé {r['confirmations']} fois.",
            "preferred_account": r.get("invoice_category"),
            "preferred_account_label": None,
            "tva_pct": r["tva_pct"],
            "locked": bool(r.get("locked", 0)),
            "rule_source": r.get("rule_source", "ai_learned"),
            "learned_from": {"corrections": corrections_n, "approvals": approvals_est},
        })
    return out


@router.post("/knowledge/{supplier}/lock")
def lock_knowledge(supplier: str, user: dict = Depends(firm_member)):
    if not query_one("SELECT 1 FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
                     (user["firm_id"], supplier)):
        raise HTTPException(status_code=404, detail="Supplier not found in knowledge base")
    execute("UPDATE supplier_priors SET locked = 1, rule_source = 'firm_policy', updated_at = ? "
            "WHERE firm_id = ? AND supplier_norm = ?", (now(), user["firm_id"], supplier))
    return {"supplier": supplier, "locked": True, "rule_source": "firm_policy"}


@router.post("/knowledge/{supplier}/unlock")
def unlock_knowledge(supplier: str, user: dict = Depends(firm_member)):
    if not query_one("SELECT 1 FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
                     (user["firm_id"], supplier)):
        raise HTTPException(status_code=404, detail="Supplier not found in knowledge base")
    execute("UPDATE supplier_priors SET locked = 0, rule_source = 'ai_learned', updated_at = ? "
            "WHERE firm_id = ? AND supplier_norm = ?", (now(), user["firm_id"], supplier))
    return {"supplier": supplier, "locked": False, "rule_source": "ai_learned"}


@router.delete("/knowledge/{supplier}", status_code=204)
def forget_knowledge(supplier: str, user: dict = Depends(firm_member)):
    execute("DELETE FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
            (user["firm_id"], supplier))


@router.get("/knowledge/{supplier}/history")
def knowledge_history(supplier: str, user: dict = Depends(firm_member)):
    return query(
        """SELECT e.*, u.full_name AS user_name FROM invoice_edits e
           LEFT JOIN users u ON u.id = e.user_id
           WHERE e.firm_id = ? AND e.field LIKE '%supplier%'
           ORDER BY e.created_at DESC LIMIT 50""",
        (user["firm_id"],),
    )


# ── Outcome metrics ────────────────────────────────────────────────────────
@router.get("/metrics/outcomes")
def outcomes(user: dict = Depends(reports_view)):
    return outcome_metrics(user["firm_id"])


@router.get("/metrics/outcomes/extended")
def extended_outcomes(user: dict = Depends(reports_view)):
    """Extended metrics: hours saved, auto-approval rate, accuracy, trends, league tables."""
    firm_id = user["firm_id"]
    base = outcome_metrics(firm_id)

    total_approved = query_one(
        "SELECT COUNT(*) AS n FROM invoices WHERE firm_id = ? AND status = 'approved'",
        (firm_id,))["n"] or 0
    edited_then_approved = query_one(
        """SELECT COUNT(DISTINCT e.invoice_id) AS n FROM invoice_edits e
           JOIN invoices i ON i.id = e.invoice_id
           WHERE e.firm_id = ? AND i.status = 'approved'""",
        (firm_id,))["n"] or 0
    auto_approved = max(0, total_approved - edited_then_approved)
    auto_rate = round(auto_approved / total_approved * 100, 1) if total_approved else 0.0

    review_times = query(
        """SELECT (julianday(reviewed_at) - julianday(created_at)) * 86400 AS secs
           FROM invoices WHERE firm_id = ? AND status IN ('approved','rejected')
             AND reviewed_at IS NOT NULL""", (firm_id,))
    valid_times = [r["secs"] for r in review_times if r["secs"] and r["secs"] > 0]
    avg_review_secs = int(sum(valid_times) / len(valid_times)) if valid_times else 0

    accuracy = round((1 - base["corrections_per_invoice"]) * 100, 1)
    accuracy = max(0.0, min(100.0, accuracy))

    # Estimated hours saved.
    # Assumption: 3 min saved per auto-approved invoice vs ~3.5 min manual average.
    # Labelled as an estimate in the response — not a measured figure.
    _MINS_SAVED_PER_AUTO = 3.0
    hours_saved = round(auto_approved * _MINS_SAVED_PER_AUTO / 60, 1)
    hours_saved_basis = (
        f"Estimation basée sur {_MINS_SAVED_PER_AUTO:.0f} min économisées "
        f"par facture auto-approuvée (vs 3,5 min de révision manuelle moyenne)"
    )

    conf_trend = query(
        """SELECT substr(COALESCE(invoice_date, created_at), 1, 7) AS month,
                  ROUND(AVG(confidence), 3) AS avg_confidence
           FROM invoices WHERE firm_id = ? AND confidence IS NOT NULL
           GROUP BY month ORDER BY month DESC LIMIT 12""", (firm_id,))

    top_suppliers = query(
        """SELECT MIN(supplier_name) AS supplier, COUNT(*) AS count FROM invoices
           WHERE firm_id = ? AND supplier_name IS NOT NULL
             AND status IN ('approved','needs_review')
           GROUP BY LOWER(supplier_name) ORDER BY count DESC LIMIT 10""", (firm_id,))

    most_corrected = query(
        """SELECT MIN(i.supplier_name) AS supplier, COUNT(*) AS corrections
           FROM invoice_edits e JOIN invoices i ON i.id = e.invoice_id
           WHERE e.firm_id = ? AND i.supplier_name IS NOT NULL
           GROUP BY LOWER(i.supplier_name) ORDER BY corrections DESC LIMIT 10""", (firm_id,))

    # Most used accounts — parsed from stored response_json
    account_counts: dict[str, dict] = {}
    for row in query("SELECT response_json FROM invoices WHERE firm_id = ? AND status = 'approved' "
                     "AND response_json IS NOT NULL", (firm_id,)):
        try:
            resp = _json.loads(row["response_json"])
        except Exception:
            continue
        for entry in resp.get("step4_journal_entries", []):
            for line in entry.get("lines", []):
                acc = line["account_number"]
                if acc not in account_counts:
                    account_counts[acc] = {"account": acc, "label": line["account_label"], "count": 0}
                account_counts[acc]["count"] += 1
    most_used_accounts = sorted(account_counts.values(), key=lambda x: -x["count"])[:10]

    vat_row = query_one(
        """SELECT SUM(CAST(json_extract(response_json, '$.step2_calculations.tva_amount') AS REAL)) AS total_vat
           FROM invoices WHERE firm_id = ? AND status = 'approved' AND response_json IS NOT NULL""",
        (firm_id,))
    vat_recovered = round((vat_row["total_vat"] or 0), 2)

    monthly_volume = query(
        """SELECT substr(COALESCE(invoice_date, created_at), 1, 7) AS month, COUNT(*) AS count
           FROM invoices WHERE firm_id = ? GROUP BY month ORDER BY month DESC LIMIT 12""",
        (firm_id,))

    return {
        **base,
        "hours_saved": hours_saved,
        "hours_saved_note": hours_saved_basis,
        "auto_approved": auto_approved,
        "auto_approval_rate": auto_rate,
        "avg_review_time_seconds": avg_review_secs,
        "ai_accuracy_rate": accuracy,
        "confidence_trend": list(reversed(conf_trend)),
        "top_suppliers": top_suppliers,
        "most_corrected_suppliers": most_corrected,
        "most_used_accounts": most_used_accounts,
        "vat_recovered_total": vat_recovered,
        "monthly_volume": list(reversed(monthly_volume)),
    }
