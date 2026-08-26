"""Month-end close assistant + interoperability exports (land-and-expand).

Close readiness answers "am I ready to close this month?" with a concrete
blocker list — computed, not guessed:
- invoices still awaiting review / failed / invalid
- unresolved duplicate flags
- open anomaly insights
- gaps in supplier invoice-number sequences (the honest, deterministic proxy
  for "missing supplier invoices": if you booked TECHNO n°140 and n°143,
  n°141-142 are probably sitting in someone's inbox)

Exports: FEC (the French/Moroccan-audit standard interchange format, importable
by Cegid, Sage, Quadratus...) so firms can run PCG Maroc AI as the AI layer in
front of their existing accounting system.
"""
import json
from collections import defaultdict

from app.core.db import execute, new_id, now, query, query_one
from app.repositories.invoices import _norm_number, _norm_supplier


def sequence_gaps(firm_id: str, client_id: str | None = None) -> list[dict]:
    """Deprecated control. Supplier invoice numbers are not expected to be
    consecutive within one customer's books, so gaps create false positives.
    Kept as an empty compatibility hook for older API consumers.
    """
    return []


CLOSE_MANUAL_CHECKS = {
    "accruals": "Charges à payer / produits à recevoir",
    "prepaids": "Charges constatées d'avance / produits constatés d'avance",
    "inventory": "Inventaire et variations de stocks",
    "payroll": "Paie et charges sociales",
    "fx": "Soldes en devises et écarts de conversion",
    "prior_period": "Ajustements de périodes antérieures",
}


def ensure_close_checks(firm_id: str, client_id: str | None, month: str) -> list[dict]:
    for ctype in CLOSE_MANUAL_CHECKS:
        if not query_one("SELECT id FROM close_adjustment_checks WHERE firm_id=? AND client_id IS ? AND month=? AND check_type=?",
                         (firm_id,client_id,month,ctype)):
            execute("INSERT INTO close_adjustment_checks(id,firm_id,client_id,month,check_type,status,updated_at) VALUES(?,?,?,?,?,'pending',?)",
                    (new_id(),firm_id,client_id,month,ctype,now()))
    return query("SELECT * FROM close_adjustment_checks WHERE firm_id=? AND client_id IS ? AND month=? ORDER BY check_type",
                 (firm_id,client_id,month))


def update_close_check(firm_id: str, client_id: str | None, month: str, check_type: str, status: str, note: str | None, user_id: str) -> dict:
    if check_type not in CLOSE_MANUAL_CHECKS or status not in {"pending","done","not_applicable"}:
        raise ValueError("Invalid close check type/status")
    ensure_close_checks(firm_id,client_id,month)
    execute("UPDATE close_adjustment_checks SET status=?,note=?,updated_by=?,updated_at=? WHERE firm_id=? AND client_id IS ? AND month=? AND check_type=?",
            (status,note,user_id,now(),firm_id,client_id,month,check_type))
    return query_one("SELECT * FROM close_adjustment_checks WHERE firm_id=? AND client_id IS ? AND month=? AND check_type=?",
                     (firm_id,client_id,month,check_type)) or {}


def _subledger_control(firm_id: str, client_id: str | None, month: str | None) -> dict:
    from app.repositories import invoices as inv_repo
    date_to = f"{month}-31" if month else None
    rows = inv_repo.journal_rows(firm_id,client_id,None,date_to)
    ap_gl = round(sum(r["amount"] for r in rows if r["account_number"]=="4411" and r["side"]=="CREDIT") -
                  sum(r["amount"] for r in rows if r["account_number"]=="4411" and r["side"]=="DEBIT"),2)
    ar_gl = round(sum(r["amount"] for r in rows if r["account_number"]=="3421" and r["side"]=="DEBIT") -
                  sum(r["amount"] for r in rows if r["account_number"]=="3421" and r["side"]=="CREDIT"),2)
    where="WHERE i.firm_id=? AND i.posting_status='posted'"; params=[firm_id]
    if client_id: where += " AND i.client_id=?"; params.append(client_id)
    if date_to: where += " AND i.posting_date<=?"; params.append(date_to)
    docs=query(f"""SELECT i.id,i.document_direction,COALESCE(i.net_a_payer_cents,CAST(ROUND(i.net_a_payer*100) AS INTEGER)) due_cents,
        COALESCE((SELECT SUM(p.amount_cents) FROM payment_allocations p WHERE p.invoice_id=i.id),0) paid_cents FROM invoices i {where}""",tuple(params))
    ap=ar=0
    for d in docs:
        direction=d.get("document_direction") or "purchase"; due=int(d["due_cents"] or 0); paid=int(d["paid_cents"] or 0)
        outstanding=max(0,due-paid)
        if direction=="purchase": ap+=outstanding
        elif direction=="sale": ar+=outstanding
        elif direction=="purchase_credit_note": ap-=due
        elif direction=="sale_credit_note": ar-=due
    ap_sub=round(ap/100,2); ar_sub=round(ar/100,2)
    return {"ap_difference":round(ap_gl-ap_sub,2),"ar_difference":round(ar_gl-ar_sub,2),"ap_gl":ap_gl,"ap_subledger":ap_sub,"ar_gl":ar_gl,"ar_subledger":ar_sub}


def close_readiness(firm_id: str, client_id: str | None = None, month: str | None = None) -> dict:
    """Month-end controls scoped to the requested accounting month."""
    where = "WHERE firm_id = ?"
    params: list = [firm_id]
    if client_id:
        where += " AND client_id = ?"; params.append(client_id)
    if month:
        where += " AND substr(invoice_date,1,7) = ?"; params.append(month)

    counts = query(f"""SELECT
        SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) AS pending_review,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN verdict='INVALID' AND status != 'rejected' THEN 1 ELSE 0 END) AS invalid,
        SUM(CASE WHEN is_duplicate_of IS NOT NULL AND status='needs_review' THEN 1 ELSE 0 END) AS open_duplicates,
        SUM(CASE WHEN status='approved' AND posting_status!='posted' THEN 1 ELSE 0 END) AS approved_unposted,
        SUM(CASE WHEN posting_status='posted' THEN 1 ELSE 0 END) AS posted
        FROM invoices {where}""", tuple(params))[0]
    counts = {k: v or 0 for k, v in counts.items()}

    ins_where = "WHERE i.firm_id=? AND i.dismissed=0 AND i.severity='warning' AND v.status='needs_review'"
    ins_params: list = [firm_id]
    if client_id:
        ins_where += " AND v.client_id=?"; ins_params.append(client_id)
    if month:
        ins_where += " AND substr(v.invoice_date,1,7)=?"; ins_params.append(month)
    open_ins = query(f"SELECT COUNT(*) AS n FROM insights i JOIN invoices v ON v.id=i.invoice_id {ins_where}", tuple(ins_params))[0]["n"] or 0

    bank_where = "WHERE t.firm_id=? AND t.status IN ('unmatched','suggested') AND t.is_duplicate_of IS NULL"
    bank_params: list = [firm_id]
    if month:
        bank_where += " AND substr(t.date,1,7)=?"; bank_params.append(month)
    unreconciled_bank = query(f"SELECT COUNT(*) AS n FROM bank_transactions t {bank_where}", tuple(bank_params))[0]["n"] or 0

    stmt_where = "WHERE firm_id=? AND control_difference_cents IS NOT NULL AND ABS(control_difference_cents)>1"
    stmt_params: list = [firm_id]
    if month:
        stmt_where += " AND (substr(period_start,1,7)=? OR substr(period_end,1,7)=?)"; stmt_params += [month, month]
    bad_statements = query(f"SELECT COUNT(*) AS n FROM bank_statements {stmt_where}", tuple(stmt_params))[0]["n"] or 0

    # Suspense 3497 must be zero/cleared before close. Use authoritative ledger.
    from app.repositories import invoices as inv_repo
    rows = inv_repo.journal_rows(firm_id, client_id, f"{month}-01" if month else None, f"{month}-31" if month else None)
    suspense_deb = sum(r["amount"] for r in rows if r["account_number"].startswith("3497") and r["side"] == "DEBIT")
    suspense_cred = sum(r["amount"] for r in rows if r["account_number"].startswith("3497") and r["side"] == "CREDIT")
    suspense_balance = round(suspense_deb - suspense_cred, 2)

    # AP/AR subsidiary-ledger tie-out and unresolved accounting-rule failures.
    subledger = _subledger_control(firm_id, client_id, month)
    rule_where = "WHERE firm_id=? AND severity IN ('critical','high')"; rule_params=[firm_id]
    if month: rule_where += " AND substr(created_at,1,7)<=?"; rule_params.append(month)
    rule_failures = query(f"SELECT COUNT(*) AS n FROM accounting_rule_events {rule_where}",tuple(rule_params))[0]["n"] or 0

    manual_checks = ensure_close_checks(firm_id, client_id, month) if month else []
    pending_manual = [c for c in manual_checks if c["status"] == "pending"]

    # Fixed assets posted in or before the period should be included in the register;
    # if active assets exist, a depreciation run/check must exist for the month.
    assets_count = 0; depreciation_missing = False
    if month:
        assets_count = query("SELECT COUNT(*) AS n FROM fixed_assets WHERE firm_id=? AND client_id IS ? AND acquisition_date<=? AND status='active'",
                             (firm_id,client_id,f"{month}-31"))[0]["n"] or 0
        depreciation_missing = bool(assets_count and not query_one("SELECT id FROM depreciation_runs WHERE firm_id=? AND client_id IS ? AND period=? AND status='posted'",(firm_id,client_id,month)))

    blockers: list[str] = []
    if counts["pending_review"]: blockers.append(f"{counts['pending_review']} facture(s) en attente de validation")
    if counts["invalid"]: blockers.append(f"{counts['invalid']} facture(s) avec contrôles en échec")
    if counts["open_duplicates"]: blockers.append(f"{counts['open_duplicates']} doublon(s) non résolu(s)")
    if counts["failed"]: blockers.append(f"{counts['failed']} facture(s) en échec de traitement")
    if counts["approved_unposted"]: blockers.append(f"{counts['approved_unposted']} facture(s) approuvée(s) mais non comptabilisée(s)")
    if open_ins: blockers.append(f"{open_ins} anomalie(s) signalée(s) non traitée(s)")
    if unreconciled_bank: blockers.append(f"{unreconciled_bank} transaction(s) bancaire(s) non rapprochée(s)")
    if bad_statements: blockers.append(f"{bad_statements} relevé(s) bancaire(s) ne bouclent pas sur solde initial + mouvements = solde final")
    if abs(suspense_balance) > 0.01: blockers.append(f"Compte d'attente 3497 non soldé : {suspense_balance:.2f} MAD")
    if abs(subledger["ap_difference"]) > 0.02: blockers.append(f"Fournisseurs 4411 non rapprochés du sous-grand-livre : écart {subledger['ap_difference']:.2f} MAD")
    if abs(subledger["ar_difference"]) > 0.02: blockers.append(f"Clients 3421 non rapprochés du sous-grand-livre : écart {subledger['ar_difference']:.2f} MAD")
    if rule_failures: blockers.append(f"{rule_failures} anomalie(s) de règle comptable/fiscale High/Critical non résolue(s)")
    if pending_manual: blockers.append(f"{len(pending_manual)} contrôle(s) de clôture manuels restent à documenter")
    if depreciation_missing: blockers.append("Immobilisations actives : dotation/amortissement du mois non comptabilisé ou non marqué non applicable")

    return {"ready": not blockers, "blockers": blockers, "counts": counts,
            "sequence_gaps": [], "open_insights": open_ins, "unreconciled_bank": unreconciled_bank,
            "bank_statement_control_failures": bad_statements, "suspense_balance": suspense_balance,
            "subledger_reconciliation": subledger, "accounting_rule_failures": rule_failures,
            "manual_checks": [{**c,"label":CLOSE_MANUAL_CHECKS.get(c["check_type"],c["check_type"])} for c in manual_checks],
            "fixed_assets": assets_count, "depreciation_missing": depreciation_missing,
            "summary": "Prêt à clôturer." if not blockers else f"Clôture bloquée par {len(blockers)} point(s) — voir la liste."}


# ── FEC export (Cegid/Sage/Quadratus-importable) ──
FEC_HEADER = ("JournalCode|JournalLib|EcritureNum|EcritureDate|CompteNum|CompteLib|"
              "CompAuxNum|CompAuxLib|PieceRef|PieceDate|EcritureLib|Debit|Credit|"
              "EcritureLet|DateLet|ValidDate|Montantdevise|Idevise")


def _fec_date(d: str | None) -> str:
    from app.services.dates import normalize_date
    n = normalize_date(d)
    return n.replace("-", "") if n else ""


def _fec_amount(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def fec_export(firm_id: str, client_id: str | None = None,
               date_from: str | None = None, date_to: str | None = None) -> str:
    """FEC-like export from the authoritative posted ledger plus balanced ODs.

    It never reads AI response JSON. Journal codes and posting dates come from
    the accounting ledger.
    """
    from app.repositories import invoices as inv_repo
    rows = inv_repo.journal_rows(firm_id, client_id, date_from, date_to)
    lines = [FEC_HEADER]
    # Group lines into accounting entries while preserving stable posted numbers.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("source"), r.get("journal_code") or r.get("perspective") or "OD",
               r.get("entry_number") or r.get("invoice_id"), r.get("date"), r.get("invoice_id"))
        groups[key].append(r)
    for key, entry_rows in sorted(groups.items(), key=lambda kv: (kv[0][3] or "", int(kv[0][2] or 0))):
        source, journal, entry_num, posting_date, invoice_id = key
        ecriture_num = str(entry_num)
        piece_ref = entry_rows[0].get("invoice_number") or str(invoice_id)[:8]
        piece_date = entry_rows[0].get("document_date") or posting_date
        lib = entry_rows[0].get("libelle") or "Écriture comptable"
        journal_lib = {"AC":"Achats", "VE":"Ventes", "BQ":"Banque", "OD":"Opérations diverses"}.get(journal, journal)
        for r in entry_rows:
            debit = _fec_amount(r["amount"]) if r["side"] == "DEBIT" else "0,00"
            credit = _fec_amount(r["amount"]) if r["side"] == "CREDIT" else "0,00"
            lines.append("|".join([
                journal, journal_lib, ecriture_num, _fec_date(posting_date),
                r["account_number"], (r["account_label"] or "").replace("|", "/"),
                str(r.get("aux_account") or ""), str(r.get("partner_name") or "").replace("|", "/"),
                str(piece_ref), _fec_date(piece_date), str(lib).replace("|", "/"),
                debit, credit, "", "", _fec_date(posting_date), "", "",
            ]))
    return "\r\n".join(lines) + "\r\n"


# ── Account-choice explanations (explainability) ──
def account_explanations(extraction: dict, entries: list[dict]) -> list[str]:
    """Plain-language, deterministic explanations of WHY each notable account
    was chosen — the teaching layer for junior accountants."""
    out: list[str] = []
    accounts = {l["account_number"] for e in entries for l in e["lines"]}
    if extraction.get("is_immobilisation"):
        cls2 = sorted(a for a in accounts if a.startswith("23"))
        if cls2:
            t = extraction.get("immobilisation_type") or "équipement"
            out.append(f"Compte {cls2[0]} (classe 2) : la facture porte sur un bien durable "
                       f"({t}) — sous le PCG marocain, il est immobilisé à l'actif, "
                       f"pas passé en charge (classe 6).")
        if "34551" in accounts:
            out.append("Compte 34551 : la TVA sur immobilisations se récupère via 34551, "
                       "distinct du 34552 réservé aux charges — ce qui isole la TVA "
                       "des investissements dans les déclarations.")
    elif any(a.startswith("61") for a in accounts):
        acct = sorted(a for a in accounts if a.startswith("61"))[0]
        nature = "prestation de services" if extraction.get("invoice_category") == "facture_service" else "achat de marchandises"
        out.append(f"Compte {acct} (classe 6) : dépense courante ({nature}), "
                   f"consommée dans l'exercice — donc en charge, pas à l'actif.")
    if "4452" in accounts:
        basis = extraction.get("withholding_legal_basis") or "base légale à vérifier"
        out.append(f"Compte 4452 : retenue à la source de {extraction.get('retenue_a_la_source_pct')}% — "
                   f"traitement documenté par « {basis} ». Aucune référence légale n'est inventée par l'explication.")
    if "7386" in accounts or "6386" in accounts:
        out.append(f"Comptes 6386/7386 : l'escompte de {extraction.get('escompte_pct')}% est une "
                   "réduction FINANCIÈRE — contrairement aux rabais/remises/ristournes, "
                   "il se comptabilise séparément.")
    if "6165" in accounts:
        out.append("Compte 6165 : les droits de timbre sont une charge fiscale de l'acheteur, "
                   "hors base TVA.")
    return out
