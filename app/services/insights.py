"""Intelligence layer — the "AI that actually understands accounting".

Every insight here is DETERMINISTIC: computed from the firm's own invoice
history with plain SQL + arithmetic. No LLM call, no hallucination risk,
millisecond latency. The vision model extracts; this layer understands.

Insights produced after each processing run:
- duplicate               "These invoices look like duplicates" (fuzzy matcher)
- ice_change              "This supplier changed its ICE"
- vat_deviation           "The VAT rate doesn't match this supplier's history"
- classification_drift    "This expense was classified differently before"
- client_mismatch         "This invoice probably belongs to Client B, not A"
- preference_conflict     "Extraction disagrees with what your team corrected before"

Learning: every human correction (PATCH extraction) and approval updates
supplier_priors — firm-specific memory that future extractions are checked
against. The firm's expertise compounds.
"""
import json

from app.core.db import execute, new_id, now, query, query_one
from app.repositories.invoices import _norm_supplier

MIN_HISTORY = 2          # history needed before deviation insights fire
MIN_CONFIRMATIONS = 2    # human confirmations before a prior is trusted


# ── History helpers ──
def _supplier_history(firm_id: str, supplier_norm: str, exclude_id: str, limit: int = 20) -> list[dict]:
    """Recent processed invoices of the same (normalized) supplier, with key
    extraction facts parsed out of the stored responses."""
    rows = query("""SELECT id, client_id, supplier_name, response_json FROM invoices
                    WHERE firm_id = ? AND id != ? AND status IN ('approved','needs_review')
                      AND supplier_name IS NOT NULL AND response_json IS NOT NULL
                    ORDER BY created_at DESC LIMIT 200""", (firm_id, exclude_id))
    out = []
    for r in rows:
        if _norm_supplier(r["supplier_name"]) != supplier_norm:
            continue
        ext = json.loads(r["response_json"])["step1_identification"]
        out.append({"id": r["id"], "client_id": r["client_id"],
                    "tva_pct": ext.get("tva_pct"), "is_immobilisation": ext.get("is_immobilisation"),
                    "immobilisation_type": ext.get("immobilisation_type"),
                    "invoice_category": ext.get("invoice_category"),
                    "supplier_ice": ext.get("supplier_ice")})
        if len(out) >= limit:
            break
    return out


def _majority(values: list) -> tuple:
    """(most common non-null value, its share, sample size)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, 0.0, 0
    best = max(set(vals), key=vals.count)
    return best, vals.count(best) / len(vals), len(vals)


# ── Insight generation ──
def generate_insights(firm_id: str, invoice_id: str, extraction: dict,
                      client_id: str, duplicate_of: str | None) -> list[dict]:
    """Run after every processing/edit. Persists and returns insights."""
    insights: list[dict] = []

    def add(kind: str, severity: str, message: str):
        insights.append({"kind": kind, "severity": severity, "message": message})

    if duplicate_of:
        add("duplicate", "warning",
            f"Doublon probable : même fournisseur, même numéro et montant équivalent "
            f"qu'une facture déjà traitée (id {duplicate_of[:8]}…).")

    sup_norm = _norm_supplier(extraction.get("supplier_name"))
    if sup_norm:
        hist = _supplier_history(firm_id, sup_norm, invoice_id)

        if len(hist) >= MIN_HISTORY:
            # ICE change
            ice_now = extraction.get("supplier_ice")
            ice_hist, share, n = _majority([h["supplier_ice"] for h in hist])
            if ice_now and ice_hist and share >= 0.8 and ice_now != ice_hist:
                add("ice_change", "warning",
                    f"L'ICE de ce fournisseur a changé : {ice_now} sur cette facture, "
                    f"contre {ice_hist} sur {n} facture(s) précédente(s). Vérifiez l'identité du fournisseur.")

            # VAT deviation
            tva_now = extraction.get("tva_pct")
            tva_hist, share, n = _majority([h["tva_pct"] for h in hist])
            if tva_now is not None and tva_hist is not None and share >= 0.8 and tva_now != tva_hist:
                add("vat_deviation", "warning",
                    f"TVA {tva_now}% sur cette facture, alors que ce fournisseur facture "
                    f"habituellement à {tva_hist}% ({n} facture(s)). Erreur d'extraction ou changement de régime ?")

            # Classification drift (expense <-> fixed asset)
            immo_now = bool(extraction.get("is_immobilisation"))
            immo_hist, share, n = _majority([h["is_immobilisation"] for h in hist])
            if immo_hist is not None and share >= 0.8 and immo_now != bool(immo_hist):
                before = "immobilisée (classe 2)" if immo_hist else "passée en charge (classe 6)"
                nowtxt = "immobilisée" if immo_now else "passée en charge"
                add("classification_drift", "warning",
                    f"Dépense {nowtxt} sur cette facture, alors qu'elle était {before} "
                    f"pour ce fournisseur les mois précédents ({n} facture(s)). À harmoniser.")

            # Client mismatch: supplier historically belongs to another client
            client_hist, share, n = _majority([h["client_id"] for h in hist])
            if client_hist and share >= 0.8 and client_hist != client_id and n >= MIN_HISTORY:
                other = query_one("SELECT name FROM clients WHERE id = ? AND firm_id = ?", (client_hist, firm_id))
                if other:
                    add("client_mismatch", "warning",
                        f"Ce fournisseur apparaît habituellement chez « {other['name']} » "
                        f"({n} facture(s)). Cette facture est-elle rattachée au bon client ?")

        # Learned-preference conflict (firm memory from human corrections)
        prior = get_prior(firm_id, sup_norm)
        if prior and prior["confirmations"] >= MIN_CONFIRMATIONS:
            if prior["tva_pct"] is not None and extraction.get("tva_pct") != prior["tva_pct"]:
                add("preference_conflict", "warning",
                    f"Votre équipe a confirmé {prior['confirmations']} fois une TVA à {prior['tva_pct']}% "
                    f"pour ce fournisseur ; l'extraction indique {extraction.get('tva_pct')}%.")
            if prior["is_immobilisation"] is not None and \
               bool(extraction.get("is_immobilisation")) != bool(prior["is_immobilisation"]):
                pref = "immobilisation" if prior["is_immobilisation"] else "charge"
                add("preference_conflict", "info",
                    f"Votre équipe classe habituellement ce fournisseur en {pref} "
                    f"(confirmé {prior['confirmations']} fois) — l'extraction diffère.")

    # Persist (replace previous insights for this invoice — e.g. after an edit)
    execute("DELETE FROM insights WHERE invoice_id = ?", (invoice_id,))
    for ins in insights:
        execute("""INSERT INTO insights (id, firm_id, invoice_id, kind, severity, message, dismissed, created_at)
                   VALUES (?,?,?,?,?,?,0,?)""",
                (new_id(), firm_id, invoice_id, ins["kind"], ins["severity"], ins["message"], now()))
    return insights


def list_insights(invoice_id: str, firm_id: str) -> list[dict]:
    return query("SELECT * FROM insights WHERE invoice_id = ? AND firm_id = ? ORDER BY severity DESC",
                 (invoice_id, firm_id))


def firm_open_insights(firm_id: str, limit: int = 50) -> list[dict]:
    return query("""SELECT i.*, v.invoice_number, v.supplier_name FROM insights i
                    JOIN invoices v ON v.id = i.invoice_id
                    WHERE i.firm_id = ? AND i.dismissed = 0 AND v.status = 'needs_review'
                    ORDER BY i.created_at DESC LIMIT ?""", (firm_id, limit))


# ── Learning from corrections ──
def get_prior(firm_id: str, supplier_norm: str) -> dict | None:
    return query_one("SELECT * FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
                     (firm_id, supplier_norm))


def learn_from_confirmation(firm_id: str, extraction: dict, human_corrected: bool) -> None:
    """Update the firm's supplier memory. Called on approval (weight 1) and on
    human correction (the strongest signal). Latest confirmed values win;
    the confirmations counter measures how established the prior is."""
    sup_norm = _norm_supplier(extraction.get("supplier_name"))
    if not sup_norm:
        return
    prior = get_prior(firm_id, sup_norm)
    inc = 2 if human_corrected else 1
    if prior:
        execute("""UPDATE supplier_priors SET invoice_category=?, is_immobilisation=?,
                   immobilisation_type=?, tva_pct=?, supplier_ice=?,
                   confirmations = confirmations + ?, updated_at=?
                   WHERE firm_id=? AND supplier_norm=?""",
                (extraction.get("invoice_category"),
                 int(bool(extraction.get("is_immobilisation"))),
                 extraction.get("immobilisation_type"), extraction.get("tva_pct"),
                 extraction.get("supplier_ice") or prior["supplier_ice"],
                 inc, now(), firm_id, sup_norm))
    else:
        execute("""INSERT INTO supplier_priors (firm_id, supplier_norm, invoice_category,
                   is_immobilisation, immobilisation_type, tva_pct, supplier_ice, confirmations, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (firm_id, sup_norm, extraction.get("invoice_category"),
                 int(bool(extraction.get("is_immobilisation"))),
                 extraction.get("immobilisation_type"), extraction.get("tva_pct"),
                 extraction.get("supplier_ice"), inc, now()))
