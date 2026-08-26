"""Health scores, firm knowledge base, confidence breakdown, outcome metrics.

Same doctrine as the insight engine: everything here is computed from the
firm's own data. The health score is a weighted summary of real blockers;
the knowledge base is the supplier_priors table made visible ("confirmed
17 times by your firm"); the confidence breakdown decomposes trust into
named factors instead of one opaque percentage; the outcomes endpoint
reports MEASURED numbers only — never invented marketing claims.
"""
import json

from app.core.db import query, query_one
from app.repositories.invoices import _norm_supplier
from app.services.close import sequence_gaps
from app.services.insights import MIN_CONFIRMATIONS, get_prior

# Health score penalties (per occurrence, capped so one bad week ≠ score 0)
_PENALTIES = {"pending_review": 3, "invalid": 8, "open_duplicates": 10,
              "failed": 6, "warnings": 5, "gaps": 7}
_CAPS = {"pending_review": 15, "invalid": 24, "open_duplicates": 20,
         "failed": 12, "warnings": 20, "gaps": 21}


# ── Accounting Health Score ──
def client_health(firm_id: str, client_id: str) -> dict:
    c = query_one("""SELECT
        SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) AS pending_review,
        SUM(CASE WHEN verdict='INVALID' AND status NOT IN ('rejected','approved') THEN 1 ELSE 0 END) AS invalid,
        SUM(CASE WHEN is_duplicate_of IS NOT NULL AND status='needs_review' THEN 1 ELSE 0 END) AS open_duplicates,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM invoices WHERE firm_id = ? AND client_id = ?""", (firm_id, client_id))
    c = {k: v or 0 for k, v in c.items()}
    c["warnings"] = query_one("""SELECT COUNT(*) AS n FROM insights i
        JOIN invoices v ON v.id = i.invoice_id
        WHERE i.firm_id = ? AND v.client_id = ? AND i.dismissed = 0
          AND i.severity = 'warning' AND v.status = 'needs_review'""",
        (firm_id, client_id))["n"] or 0
    gaps = sequence_gaps(firm_id, client_id)
    c["gaps"] = len(gaps)

    score = 100
    issues: list[str] = []
    for key, count in c.items():
        if count:
            score -= min(count * _PENALTIES[key], _CAPS[key])
    if c["gaps"]:
        missing = sum(g["missing_count"] for g in gaps)
        issues.append(f"{missing} facture(s) probablement manquante(s) (trous de numérotation)")
    if c["open_duplicates"]:
        issues.append(f"{c['open_duplicates']} doublon(s) non résolu(s)")
    if c["invalid"]:
        issues.append(f"{c['invalid']} facture(s) avec contrôles en échec")
    if c["warnings"]:
        issues.append(f"{c['warnings']} anomalie(s) signalée(s) (TVA, ICE, classification…)")
    if c["pending_review"]:
        issues.append(f"{c['pending_review']} facture(s) en attente de validation")
    if c["failed"]:
        issues.append(f"{c['failed']} facture(s) en échec de traitement")

    score = max(0, score)
    return {"client_id": client_id, "score": score, "issues": issues,
            "recommendation": "Rien à signaler — prêt pour la clôture." if score == 100
                else "Résolvez ces points avant la clôture mensuelle." if score >= 60
                else "Situation dégradée — traitez ce dossier en priorité."}


def firm_health(firm_id: str, assigned_to: str | None = None) -> list[dict]:
    """Manager scan view: every client's health at a glance, worst first."""
    where = "WHERE firm_id = ? AND is_archived = 0"
    params: list = [firm_id]
    if assigned_to:
        where += " AND assigned_to = ?"
        params.append(assigned_to)
    clients = query(f"SELECT id, name FROM clients {where}", tuple(params))
    out = []
    for c in clients:
        h = client_health(firm_id, c["id"])
        h["client_name"] = c["name"]
        out.append(h)
    return sorted(out, key=lambda h: h["score"])


# ── Firm Knowledge Base ──
def knowledge_base(firm_id: str) -> list[dict]:
    """What the firm has taught the system, made visible."""
    rows = query("""SELECT * FROM supplier_priors WHERE firm_id = ?
                    ORDER BY confirmations DESC""", (firm_id,))
    out = []
    for r in rows:
        learned = []
        if r["tva_pct"] is not None:
            learned.append(f"TVA {r['tva_pct']:g} %")
        if r["is_immobilisation"] is not None:
            learned.append("immobilisation (classe 2)" if r["is_immobilisation"] else "charge (classe 6)")
        if r["immobilisation_type"]:
            learned.append(f"type: {r['immobilisation_type']}")
        out.append({"supplier": r["supplier_norm"], "confirmations": r["confirmations"],
                    "established": r["confirmations"] >= MIN_CONFIRMATIONS,
                    "learned": learned, "updated_at": r["updated_at"],
                    "summary": f"{', '.join(learned) or '—'} — confirmé {r['confirmations']} fois par votre cabinet."})
    return out


def prior_agreement_note(firm_id: str, extraction: dict) -> str | None:
    """The trust-building line: 'suggested because your firm approved this N times'."""
    sup = _norm_supplier(extraction.get("supplier_name"))
    if not sup:
        return None
    p = get_prior(firm_id, sup)
    if not p or p["confirmations"] < MIN_CONFIRMATIONS:
        return None
    matches = (p["tva_pct"] is None or extraction.get("tva_pct") == p["tva_pct"]) and \
              (p["is_immobilisation"] is None or
               bool(extraction.get("is_immobilisation")) == bool(p["is_immobilisation"]))
    if matches:
        return (f"Classification conforme aux habitudes de votre cabinet pour ce fournisseur "
                f"— confirmée {p['confirmations']} fois lors d'approbations et corrections précédentes.")
    return None


# ── Confidence Breakdown ──
def confidence_breakdown(firm_id: str, invoice: dict) -> dict:
    """Decompose 'why 92%' into named, checkable factors."""
    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    checks = resp.get("validation_checks", [])
    factors: list[dict] = []

    def factor(name: str, ok: bool, detail: str):
        factors.append({"factor": name, "ok": ok, "detail": detail})

    if checks:
        passed = sum(1 for c in checks if c["passed"])
        factor("Contrôles comptables", passed == len(checks),
               f"{passed}/{len(checks)} règles PCG validées")
    fcs = ext.get("field_confidences", [])
    if fcs:
        low = [f for f in fcs if f["confidence"] < 0.75]
        avg = sum(f["confidence"] for f in fcs) / len(fcs)
        factor("Qualité de lecture (extraction)", not low,
               f"confiance moyenne {avg:.0%}" + (f", {len(low)} champ(s) incertain(s): "
               + ", ".join(f["field"] for f in low[:3]) if low else ""))
    sup = _norm_supplier(ext.get("supplier_name"))
    if sup:
        n = query_one("""SELECT COUNT(*) AS n FROM invoices
                         WHERE firm_id = ? AND id != ? AND supplier_name IS NOT NULL
                           AND status = 'approved'""", (firm_id, invoice["id"]))["n"] or 0
        hist = query_one("""SELECT COUNT(*) AS n FROM invoices
                            WHERE firm_id = ? AND id != ? AND status='approved'
                              AND LOWER(supplier_name) LIKE ?""",
                         (firm_id, invoice["id"], f"%{sup.split(' ')[0]}%"))["n"] or 0 if n else 0
        factor("Fournisseur connu", hist > 0,
               f"{hist} facture(s) approuvée(s) de ce fournisseur" if hist else "premier passage de ce fournisseur")
        note = prior_agreement_note(firm_id, ext)
        p = get_prior(firm_id, sup)
        if p and p["confirmations"] >= MIN_CONFIRMATIONS:
            factor("Habitudes du cabinet", note is not None,
                   note or "la classification diffère des habitudes apprises du cabinet")
    warnings = query_one("""SELECT COUNT(*) AS n FROM insights
                            WHERE invoice_id = ? AND severity='warning' AND dismissed=0""",
                         (invoice["id"],))["n"] or 0
    factor("Anomalies", warnings == 0,
           "aucune anomalie détectée" if warnings == 0 else f"{warnings} anomalie(s) ouverte(s)")

    ok = sum(1 for f in factors if f["ok"])
    return {"score": round(ok / len(factors), 2) if factors else None,
            "factors": factors,
            "verdict_hint": "Approuvable en confiance." if ok == len(factors)
                else "Vérifiez les facteurs signalés avant d'approuver."}


# ── Measured outcome metrics (never invented) ──
def outcome_metrics(firm_id: str) -> dict:
    inv = query_one("""SELECT COUNT(*) AS processed,
        ROUND(AVG(duration_ms),0) AS avg_ai_ms,
        SUM(CASE WHEN is_duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS duplicates_flagged,
        SUM(CASE WHEN verdict='INVALID' THEN 1 ELSE 0 END) AS invalid_caught
        FROM invoices WHERE firm_id = ? AND status != 'failed'""", (firm_id,))
    dup_posted = query_one("""SELECT COUNT(*) AS n FROM invoices
        WHERE firm_id = ? AND is_duplicate_of IS NOT NULL AND status='approved'""", (firm_id,))["n"] or 0
    edits = query_one("""SELECT COUNT(*) AS n FROM audit_logs
        WHERE firm_id = ? AND action='invoice.edit'""", (firm_id,))["n"] or 0
    insights_raised = query_one("SELECT COUNT(*) AS n FROM insights WHERE firm_id = ?", (firm_id,))["n"] or 0
    priors = query_one("""SELECT COUNT(*) AS n FROM supplier_priors
        WHERE firm_id = ? AND confirmations >= ?""", (firm_id, MIN_CONFIRMATIONS))["n"] or 0
    processed = inv["processed"] or 0
    return {
        "invoices_processed": processed,
        "avg_ai_processing_ms": inv["avg_ai_ms"] or 0,
        "duplicates_flagged_before_posting": (inv["duplicates_flagged"] or 0) - dup_posted,
        "invalid_entries_caught_before_posting": inv["invalid_caught"] or 0,
        "anomalies_surfaced": insights_raised,
        "human_corrections_total": edits,
        "corrections_per_invoice": round(edits / processed, 3) if processed else 0.0,
        "suppliers_learned": priors,
        "note": ("Chiffres mesurés sur vos données. corrections_per_invoice en baisse = "
                 "le système apprend vos habitudes; comparez-le mois par mois."),
    }
