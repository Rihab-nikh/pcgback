"""Invoice explainability service.

Deterministic, auditable explanations for every AI decision.
No LLM calls — all derived from structured data already in the database.
"""
import json

from app.core.db import query, query_one
from app.repositories.invoices import _norm_supplier
from app.services.health import confidence_breakdown
from app.services.insights import MIN_CONFIRMATIONS, get_prior, list_insights


# ---------------------------------------------------------------------------
# 1. Split confidence
# ---------------------------------------------------------------------------

def split_confidence(firm_id: str, invoice: dict) -> dict:
    """Decompose the flat confidence into three semantic dimensions.

    OCR         ← 'Qualité de lecture (extraction)' factor
    Accounting  ← 'Contrôles comptables' factor
    Business    ← average of 'Fournisseur connu', 'Habitudes du cabinet', 'Anomalies'
    """
    bd = confidence_breakdown(firm_id, invoice)
    factors = {f["factor"]: f for f in bd["factors"]}

    def _score(factor_name: str, default: float = 0.75) -> float:
        f = factors.get(factor_name)
        return (1.0 if f["ok"] else 0.45) if f else default

    ocr = _score("Qualité de lecture (extraction)", default=0.80)
    accounting = _score("Contrôles comptables", default=0.75)

    biz_names = ["Fournisseur connu", "Habitudes du cabinet", "Anomalies"]
    biz_scores = [_score(n) for n in biz_names if n in factors]
    business = round(sum(biz_scores) / len(biz_scores), 2) if biz_scores else 0.70

    return {"ocr": ocr, "accounting": accounting, "business": business}


# ---------------------------------------------------------------------------
# 2. Risk engine — explicit, named, auditable point values
# ---------------------------------------------------------------------------

# Each rule: (key, reason_fr, points)
# The score and thresholds are documented here so they can be read and adjusted.
#   >= 50 → High risk
#   >= 20 → Medium risk (needs review)
#    < 20 → Low risk
_SCORE_HIGH = 50
_SCORE_MEDIUM = 20

_RULES = [
    # key                  label (shown to user)                                   pts
    ("duplicate",          "Doublon possible détecté",                              50),
    ("invalid_verdict",    "Facture invalide (contrôles PCG en échec)",             30),
    ("new_supplier",       "Nouveau fournisseur — aucun historique dans ce cabinet", 20),
    ("amount_2x",          None,   # message built dynamically                      20
                                                                                        ),
    ("vat_mismatch",       None,   # message built dynamically                      15),
                                                                                        ),
    ("missing_ice",        "ICE fournisseur absent",                                10),
    ("warning_insight",    None,   # one entry per insight, capped                  10),
                                                                                        ),
]
# Simpler to keep as a plain lookup than a list of 3-tuples with None labels
_RULE_POINTS = {
    "duplicate":        50,
    "invalid_verdict":  30,
    "new_supplier":     20,
    "amount_2x":        20,
    "vat_mismatch":     15,
    "missing_ice":      10,
    "warning_insight":  10,   # per insight, capped at 20 total
}


def risk_flag(firm_id: str, invoice: dict, insights: list[dict]) -> dict:
    """Rule-based risk score.

    Points per rule (transparent, adjustable):
      Duplicate detected          +50  → always High on its own
      INVALID verdict             +30
      New supplier (< 2 confirms) +20
      Amount > 2× avg             +20
      VAT differs from history    +15
      Missing ICE                 +10
      Each warning insight        +10  (capped at +20)

    Thresholds:
      score >= 50 → High
      score >= 20 → Medium (Needs review)
      score <  20 → Low
    """
    reasons: list[str] = []
    score = 0

    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    sup_norm = _norm_supplier(ext.get("supplier_name"))

    def add(rule_key: str, message: str) -> None:
        nonlocal score
        score += _RULE_POINTS[rule_key]
        reasons.append(message)

    # ── Duplicate ────────────────────────────────────────────────────────
    if invoice.get("is_duplicate_of"):
        add("duplicate", "Doublon possible détecté (+50 pts)")

    # ── INVALID verdict ──────────────────────────────────────────────────
    if invoice.get("verdict") == "INVALID":
        add("invalid_verdict", "Facture invalide — contrôles PCG en échec (+30 pts)")

    if sup_norm:
        prior = get_prior(firm_id, sup_norm)

        # ── New supplier ─────────────────────────────────────────────────
        if not prior or prior["confirmations"] < MIN_CONFIRMATIONS:
            add("new_supplier", "Nouveau fournisseur — aucun historique dans ce cabinet (+20 pts)")

        # ── Amount vs average ─────────────────────────────────────────────
        ttc = invoice.get("ttc") or 0
        avg_row = query_one(
            """SELECT AVG(ttc) AS avg_ttc FROM invoices
               WHERE firm_id = ? AND id != ? AND status = 'approved'
                 AND ttc > 0 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, invoice["id"], f"%{sup_norm.split()[0]}%"),
        )
        if avg_row and avg_row["avg_ttc"] and ttc > avg_row["avg_ttc"] * 2:
            pct = int(ttc / avg_row["avg_ttc"] * 100 - 100)
            add("amount_2x", f"Montant {pct}% supérieur à la moyenne fournisseur (+20 pts)")

        # ── VAT deviation from locked prior ──────────────────────────────
        if prior and prior["tva_pct"] is not None:
            tva_now = ext.get("tva_pct")
            if tva_now is not None and tva_now != prior["tva_pct"]:
                add(
                    "vat_mismatch",
                    f"TVA {tva_now}% ≠ historique {prior['tva_pct']}% (+15 pts)",
                )

    # ── Missing ICE ───────────────────────────────────────────────────────
    if not ext.get("supplier_ice") and sup_norm:
        add("missing_ice", "ICE fournisseur absent (+10 pts)")

    # ── Warning insights (cap total contribution at +20) ─────────────────
    warnings = [i for i in insights if i["severity"] == "warning"]
    insight_contrib = 0
    kind_labels = {
        "duplicate": "Doublon possible",
        "vat_deviation": "TVA inhabituelle",
        "classification_drift": "Classification changée",
        "client_mismatch": "Client probablement erroné",
        "ice_change": "ICE fournisseur modifié",
        "preference_conflict": "Diffère des habitudes du cabinet",
    }
    for w in warnings:
        if insight_contrib >= 20:
            break
        label = kind_labels.get(w["kind"], w["kind"])
        msg = f"{label} (+10 pts)"
        if msg not in reasons:
            score += _RULE_POINTS["warning_insight"]
            insight_contrib += _RULE_POINTS["warning_insight"]
            reasons.append(msg)

    level = "high" if score >= _SCORE_HIGH else "medium" if score >= _SCORE_MEDIUM else "low"
    return {"level": level, "score": score, "reasons": reasons}


# ---------------------------------------------------------------------------
# 3. Accounting reasoning (deterministic, from structured data)
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "telecom":        ["internet", "téléphone", "fibre", "abonnement", "ligne", "mobile", "4g", "adsl"],
    "energie":        ["électricité", "eau", "gaz", "énergie", "consommation"],
    "transport":      ["transport", "livraison", "fret", "carburant", "péage", "taxi"],
    "loyer":          ["loyer", "bail", "location", "hébergement", "bureaux"],
    "maintenance":    ["maintenance", "entretien", "réparation", "pièces"],
    "informatique":   ["logiciel", "licence", "cloud", "abonnement", "saas", "informatique"],
    "fournitures":    ["fournitures", "papier", "cartouche", "consommables"],
    "immobilisation": ["ordinateur", "serveur", "véhicule", "mobilier", "installation", "matériel"],
}

_ACCOUNT_ALTERNATIVES: dict[str, list[dict]] = {
    "6121": [
        {"account": "6131", "label": "Locations",             "reason_rejected": "pas une location de bien"},
        {"account": "6141", "label": "Entretien et réparations", "reason_rejected": "pas une maintenance"},
        {"account": "2355", "label": "Matériel informatique", "reason_rejected": "pas un actif durable (< seuil d'immobilisation)"},
    ],
    "6131": [
        {"account": "6121", "label": "Achats non stockés",    "reason_rejected": "service, pas un achat de matière"},
        {"account": "6141", "label": "Entretien et réparations", "reason_rejected": "pas une réparation"},
    ],
    "6141": [
        {"account": "6121", "label": "Achats non stockés",    "reason_rejected": "opération de maintenance, pas un achat courant"},
        {"account": "2355", "label": "Matériel informatique", "reason_rejected": "réparation ≠ acquisition d'actif"},
    ],
    "2355": [
        {"account": "6121", "label": "Achats non stockés",    "reason_rejected": "bien durable → actif, pas charge"},
        {"account": "6141", "label": "Entretien",              "reason_rejected": "acquisition nouvelle, pas une réparation"},
    ],
}


def _detect_keywords(text: str) -> list[str]:
    text_low = text.lower()
    found: list[str] = []
    for kws in _CATEGORY_KEYWORDS.values():
        for kw in kws:
            if kw in text_low and kw not in found:
                found.append(kw)
    return found[:8]


def accounting_reasoning(firm_id: str, invoice: dict) -> dict | None:
    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    entries = resp.get("step4_journal_entries", [])

    # Primary expense/asset account (first non-receivable/payable debit)
    primary_account = primary_label = None
    for entry in entries:
        for line in entry.get("lines", []):
            if line["side"] == "DEBIT" and not line["account_number"].startswith(("3", "4")):
                primary_account = line["account_number"]
                primary_label = line["account_label"]
                break
        if primary_account:
            break
    if not primary_account:
        return None

    text_pool = " ".join(filter(None, [
        ext.get("supplier_name", ""),
        ext.get("invoice_category", ""),
        " ".join(ext.get("assumptions", [])),
    ]))
    keywords = _detect_keywords(text_pool)

    sup_norm = _norm_supplier(ext.get("supplier_name"))
    prev_count = 0
    history_note = None
    if sup_norm:
        row = query_one(
            """SELECT COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND id != ? AND status = 'approved'
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, invoice["id"], f"%{sup_norm.split()[0]}%"),
        )
        prev_count = row["n"] if row else 0
        prior = get_prior(firm_id, sup_norm)
        if prior and prior["confirmations"] >= MIN_CONFIRMATIONS:
            history_note = (
                f"Règle du cabinet confirmée {prior['confirmations']} fois — "
                f"classification {'immobilisation' if prior.get('is_immobilisation') else 'charge'}"
            )

    return {
        "recommended_account": primary_account,
        "recommended_label": primary_label,
        "keywords_detected": keywords,
        "supplier_history_note": history_note,
        "previous_invoices_count": prev_count,
        "alternatives": _ACCOUNT_ALTERNATIVES.get(primary_account, []),
    }


# ---------------------------------------------------------------------------
# 4. AI suggestions
# ---------------------------------------------------------------------------

def ai_suggestions(firm_id: str, invoice: dict, insights: list[dict]) -> list[dict]:
    suggestions: list[dict] = []
    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    sup_norm = _norm_supplier(ext.get("supplier_name"))

    insight_to_kind = {
        "duplicate":           "possible_duplicate",
        "vat_deviation":       "new_vat_rate",
        "classification_drift":"possible_immobilisation",
        "client_mismatch":     "missing_info",
        "ice_change":          "ice_changed",
        "preference_conflict": "missing_info",
    }
    for ins in insights:
        kind = insight_to_kind.get(ins["kind"])
        if kind:
            suggestions.append({"kind": kind, "message": ins["message"], "severity": ins["severity"]})

    if sup_norm:
        row = query_one(
            """SELECT COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND status IN ('approved','needs_review')
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, f"%{sup_norm.split()[0]}%"),
        )
        count = row["n"] if row else 0
        if count > 1:
            suggestions.insert(0, {
                "kind": "supplier_usage",
                "message": f"Fournisseur utilisé {count} fois dans ce cabinet",
                "severity": "info",
            })

        ttc = invoice.get("ttc") or 0
        avg_row = query_one(
            """SELECT AVG(ttc) AS avg_ttc, COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND status = 'approved' AND ttc > 0
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, f"%{sup_norm.split()[0]}%"),
        )
        if avg_row and avg_row["avg_ttc"] and avg_row["n"] >= 2:
            avg = avg_row["avg_ttc"]
            if ttc > avg * 1.35:
                suggestions.append({
                    "kind": "amount_higher",
                    "message": f"Montant {int(ttc / avg * 100 - 100)}% supérieur à la moyenne ({avg_row['n']} factures)",
                    "severity": "warning",
                })
            elif ttc < avg * 0.65:
                suggestions.append({
                    "kind": "amount_lower",
                    "message": f"Montant {int(100 - ttc / avg * 100)}% inférieur à la moyenne ({avg_row['n']} factures)",
                    "severity": "info",
                })

        prior = get_prior(firm_id, sup_norm)
        if prior and prior["confirmations"] >= MIN_CONFIRMATIONS:
            suggestions.append({
                "kind": "same_treatment",
                "message": f"Même traitement que les {prior['confirmations']} factures précédentes de ce fournisseur",
                "severity": "info",
            })
        elif count == 0:
            suggestions.append({
                "kind": "new_supplier",
                "message": "Nouveau fournisseur — aucune facture précédente dans ce cabinet",
                "severity": "warning",
            })

    missing = [f for f, v in [
        ("N° facture", ext.get("invoice_number")),
        ("Date",       ext.get("date")),
        ("Fournisseur",ext.get("supplier_name")),
    ] if not v]
    if missing:
        suggestions.append({
            "kind": "missing_info",
            "message": f"Informations manquantes : {', '.join(missing)}",
            "severity": "warning",
        })

    return suggestions


# ---------------------------------------------------------------------------
# 5. Combined explain payload
# ---------------------------------------------------------------------------

def full_explain(firm_id: str, invoice: dict) -> dict:
    insights = list_insights(invoice["id"], firm_id)
    return {
        "split_confidence":    split_confidence(firm_id, invoice),
        "risk":                risk_flag(firm_id, invoice, insights),
        "accounting_reasoning":accounting_reasoning(firm_id, invoice),
        "ai_suggestions":      ai_suggestions(firm_id, invoice, insights),
    }

    """Decompose the flat confidence into OCR / Accounting / Business.

    Mapping from existing confidence_breakdown factors:
    - OCR         ← 'Qualité de lecture (extraction)' factor
    - Accounting  ← 'Contrôles comptables' factor
    - Business    ← 'Fournisseur connu' + 'Habitudes du cabinet' + 'Anomalies'
    """
    bd = confidence_breakdown(firm_id, invoice)
    factors = {f["factor"]: f for f in bd["factors"]}

    def score(factor_name: str, default: float = 0.75) -> float:
        f = factors.get(factor_name)
        if f is None:
            return default
        return 1.0 if f["ok"] else 0.45

    ocr = score("Qualité de lecture (extraction)", default=0.80)
    accounting = score("Contrôles comptables", default=0.75)

    # Business score = average of supplier knowledge + firm habits + anomaly check
    biz_factors = ["Fournisseur connu", "Habitudes du cabinet", "Anomalies"]
    biz_scores = [score(f) for f in biz_factors if f in factors]
    business = round(sum(biz_scores) / len(biz_scores), 2) if biz_scores else 0.70

    return {"ocr": ocr, "accounting": accounting, "business": business}


# ---------------------------------------------------------------------------
# 2. Risk engine (deterministic rules, no LLM)
# ---------------------------------------------------------------------------

_RISK_RULES = [
    # (condition_fn, reason_fr, weight)
    # weight: high=3, medium=2, low=1  — total >= 4 → High, 2-3 → Medium, else Low
]


def risk_flag(firm_id: str, invoice: dict, insights: list[dict]) -> dict:
    """Rule-based risk scoring derived entirely from existing data.

    Rules:
    - Duplicate flagged                      → High (+3)
    - New supplier (no prior history)        → Medium (+2)
    - Amount unusually high (> 2× average)   → Medium (+2)
    - INVALID verdict                        → Medium (+2)
    - Any warning insight                    → Medium (+2 each, cap 4)
    - Missing ICE on a supplier invoice      → Low (+1)
    - Different VAT from supplier history    → Low (+1)
    - Different account from supplier hist.  → Low (+1)
    """
    reasons: list[str] = []
    score = 0

    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    sup_norm = _norm_supplier(ext.get("supplier_name"))

    # Duplicate
    if invoice.get("is_duplicate_of"):
        reasons.append("Doublon possible détecté")
        score += 3

    # Verdict INVALID
    if invoice.get("verdict") == "INVALID":
        reasons.append("Facture invalide (contrôles PCG en échec)")
        score += 2

    # New supplier
    if sup_norm:
        prior = get_prior(firm_id, sup_norm)
        hist_count = query_one(
            """SELECT COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND id != ? AND status = 'approved'
                 AND supplier_name IS NOT NULL""",
            (firm_id, invoice["id"]),
        )["n"] or 0
        if not prior or prior["confirmations"] < MIN_CONFIRMATIONS:
            reasons.append("Nouveau fournisseur — aucun historique dans ce cabinet")
            score += 2

        # Amount vs average
        ttc = invoice.get("ttc") or 0
        avg = query_one(
            """SELECT AVG(ttc) AS avg_ttc FROM invoices
               WHERE firm_id = ? AND status = 'approved' AND ttc > 0
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, f"%{sup_norm.split()[0]}%"),
        )
        if avg and avg["avg_ttc"] and ttc > avg["avg_ttc"] * 2:
            pct = int(ttc / avg["avg_ttc"] * 100 - 100)
            reasons.append(f"Montant {pct}% supérieur à la moyenne fournisseur")
            score += 2

        # VAT deviation from prior
        if prior and prior["tva_pct"] is not None:
            tva_now = ext.get("tva_pct")
            if tva_now is not None and tva_now != prior["tva_pct"]:
                reasons.append(
                    f"TVA {tva_now}% différente de l'historique ({prior['tva_pct']}%)"
                )
                score += 1

    # Missing ICE
    if not ext.get("supplier_ice") and sup_norm:
        reasons.append("ICE fournisseur absent")
        score += 1

    # Warning insights (cap contribution)
    warnings = [i for i in insights if i["severity"] == "warning"]
    contrib = min(len(warnings) * 2, 4)
    score += contrib
    for w in warnings[:3]:  # show at most 3 insight reasons
        kind_labels = {
            "duplicate": "Doublon possible",
            "vat_deviation": "TVA inhabituelle",
            "classification_drift": "Classification changée",
            "client_mismatch": "Client probablement erroné",
            "ice_change": "ICE fournisseur modifié",
            "preference_conflict": "Diffère des habitudes du cabinet",
        }
        label = kind_labels.get(w["kind"], w["kind"])
        if label not in reasons:
            reasons.append(label)

    if score >= 4:
        level = "high"
    elif score >= 2:
        level = "medium"
    else:
        level = "low"

    return {"level": level, "score": score, "reasons": reasons}


# ---------------------------------------------------------------------------
# 3. Accounting reasoning (deterministic, from structured data)
# ---------------------------------------------------------------------------

# Keywords per accounting category that the AI would detect
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "telecom": ["internet", "téléphone", "fibre", "abonnement", "ligne", "mobile", "4g", "adsl"],
    "energie": ["électricité", "eau", "gaz", "énergie", "consommation"],
    "transport": ["transport", "livraison", "fret", "carburant", "péage", "taxi"],
    "loyer": ["loyer", "bail", "location", "hébergement", "bureaux"],
    "maintenance": ["maintenance", "entretien", "réparation", "pièces"],
    "informatique": ["logiciel", "licence", "cloud", "abonnement", "saas", "informatique"],
    "fournitures": ["fournitures", "papier", "cartouche", "consommables"],
    "immobilisation": ["ordinateur", "serveur", "véhicule", "mobilier", "installation", "matériel"],
}

# Account alternatives to consider and why they are rejected
_ACCOUNT_ALTERNATIVES: dict[str, list[dict]] = {
    "6121": [
        {"account": "6131", "label": "Locations", "reason_rejected": "pas une location de bien"},
        {"account": "6141", "label": "Entretien et réparations", "reason_rejected": "pas une maintenance"},
        {"account": "2355", "label": "Matériel informatique", "reason_rejected": "pas un actif durable (< seuil d'immobilisation)"},
    ],
    "6131": [
        {"account": "6121", "label": "Achats de matières premières", "reason_rejected": "service, pas un achat de matière"},
        {"account": "6141", "label": "Entretien et réparations", "reason_rejected": "pas une réparation"},
    ],
    "6141": [
        {"account": "6121", "label": "Achats non stockés", "reason_rejected": "opération de maintenance, pas un achat courant"},
        {"account": "2355", "label": "Matériel informatique", "reason_rejected": "réparation ≠ acquisition d'actif"},
    ],
    "2355": [
        {"account": "6121", "label": "Achats non stockés", "reason_rejected": "bien durable → actif, pas charge"},
        {"account": "6141", "label": "Entretien", "reason_rejected": "acquisition nouvelle, pas une réparation"},
    ],
}


def _detect_keywords(text: str) -> list[str]:
    """Find accounting-relevant keywords in any text field."""
    text_low = text.lower()
    found: list[str] = []
    for kws in _CATEGORY_KEYWORDS.values():
        for kw in kws:
            if kw in text_low and kw not in found:
                found.append(kw)
    return found[:8]  # cap at 8 for display


def accounting_reasoning(firm_id: str, invoice: dict) -> dict | None:
    """Build a deterministic accounting reasoning object from structured data."""
    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    entries = resp.get("step4_journal_entries", [])

    # Find the primary debit account (first debit line of first entry)
    primary_account = None
    primary_label = None
    for entry in entries:
        for line in entry.get("lines", []):
            if line["side"] == "DEBIT" and not line["account_number"].startswith(("3", "4")):
                primary_account = line["account_number"]
                primary_label = line["account_label"]
                break
        if primary_account:
            break

    if not primary_account:
        return None

    # Keywords from supplier name + invoice category + assumptions
    text_pool = " ".join(filter(None, [
        ext.get("supplier_name", ""),
        ext.get("invoice_category", ""),
        " ".join(ext.get("assumptions", [])),
    ]))
    keywords = _detect_keywords(text_pool)

    # Supplier history
    sup_norm = _norm_supplier(ext.get("supplier_name"))
    prev_count = 0
    history_note = None
    if sup_norm:
        row = query_one(
            """SELECT COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND id != ? AND status = 'approved'
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, invoice["id"], f"%{sup_norm.split()[0]}%"),
        )
        prev_count = row["n"] if row else 0
        prior = get_prior(firm_id, sup_norm)
        if prior and prior["confirmations"] >= MIN_CONFIRMATIONS:
            history_note = (
                f"Règle du cabinet confirmée {prior['confirmations']} fois — "
                f"classification {'immobilisation' if prior.get('is_immobilisation') else 'charge'}"
            )

    # Alternatives
    alternatives = _ACCOUNT_ALTERNATIVES.get(primary_account, [])

    return {
        "recommended_account": primary_account,
        "recommended_label": primary_label,
        "keywords_detected": keywords,
        "supplier_history_note": history_note,
        "previous_invoices_count": prev_count,
        "alternatives": alternatives,
    }


# ---------------------------------------------------------------------------
# 4. AI suggestions (maps insights + stats to UI suggestion cards)
# ---------------------------------------------------------------------------

def ai_suggestions(firm_id: str, invoice: dict, insights: list[dict]) -> list[dict]:
    """Map existing data into user-facing suggestion cards.
    These are observations, not commands. Severity follows the source insight."""
    suggestions: list[dict] = []
    resp = invoice.get("response") or {}
    ext = resp.get("step1_identification", {})
    sup_norm = _norm_supplier(ext.get("supplier_name"))

    # Map insights to suggestions
    insight_to_kind = {
        "duplicate": "possible_duplicate",
        "vat_deviation": "new_vat_rate",
        "classification_drift": "possible_immobilisation",
        "client_mismatch": "missing_info",
        "ice_change": "ice_changed",
        "preference_conflict": "missing_info",
    }
    for ins in insights:
        kind = insight_to_kind.get(ins["kind"])
        if kind:
            suggestions.append({
                "kind": kind,
                "message": ins["message"],
                "severity": ins["severity"],
            })

    if sup_norm:
        # Supplier usage count
        row = query_one(
            """SELECT COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND status IN ('approved','needs_review')
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, f"%{sup_norm.split()[0]}%"),
        )
        count = row["n"] if row else 0
        if count > 1:
            suggestions.insert(0, {
                "kind": "supplier_usage",
                "message": f"Fournisseur utilisé {count} fois dans ce cabinet",
                "severity": "info",
            })

        # Amount vs average
        ttc = invoice.get("ttc") or 0
        avg_row = query_one(
            """SELECT AVG(ttc) AS avg_ttc, COUNT(*) AS n FROM invoices
               WHERE firm_id = ? AND status = 'approved' AND ttc > 0
                 AND LOWER(supplier_name) LIKE ?""",
            (firm_id, f"%{sup_norm.split()[0]}%"),
        )
        if avg_row and avg_row["avg_ttc"] and avg_row["n"] >= 2:
            avg = avg_row["avg_ttc"]
            if ttc > avg * 1.35:
                pct = int(ttc / avg * 100 - 100)
                suggestions.append({
                    "kind": "amount_higher",
                    "message": f"Montant {pct}% supérieur à la moyenne ({avg_row['n']} factures comparées)",
                    "severity": "warning",
                })
            elif ttc < avg * 0.65:
                pct = int(100 - ttc / avg * 100)
                suggestions.append({
                    "kind": "amount_lower",
                    "message": f"Montant {pct}% inférieur à la moyenne ({avg_row['n']} factures comparées)",
                    "severity": "info",
                })

        # Same treatment as previous
        prior = get_prior(firm_id, sup_norm)
        if prior and prior["confirmations"] >= MIN_CONFIRMATIONS:
            suggestions.append({
                "kind": "same_treatment",
                "message": (
                    f"Même traitement que les {prior['confirmations']} factures précédentes "
                    f"de ce fournisseur"
                ),
                "severity": "info",
            })
        elif count == 0:
            suggestions.append({
                "kind": "new_supplier",
                "message": "Nouveau fournisseur — aucune facture précédente dans ce cabinet",
                "severity": "warning",
            })

    # Missing mandatory info
    missing: list[str] = []
    if not ext.get("invoice_number"):
        missing.append("N° facture")
    if not ext.get("date"):
        missing.append("Date")
    if not ext.get("supplier_name"):
        missing.append("Fournisseur")
    if missing:
        suggestions.append({
            "kind": "missing_info",
            "message": f"Informations manquantes : {', '.join(missing)}",
            "severity": "warning",
        })

    return suggestions


# ---------------------------------------------------------------------------
# 5. Combined explain endpoint payload
# ---------------------------------------------------------------------------

def full_explain(firm_id: str, invoice: dict) -> dict:
    """Single call that produces everything the invoice workspace needs."""
    insights = list_insights(invoice["id"], firm_id)
    return {
        "split_confidence": split_confidence(firm_id, invoice),
        "risk": risk_flag(firm_id, invoice, insights),
        "accounting_reasoning": accounting_reasoning(firm_id, invoice),
        "ai_suggestions": ai_suggestions(firm_id, invoice, insights),
    }
