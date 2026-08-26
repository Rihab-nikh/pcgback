"""Versioned Moroccan tax-rule registry helpers.

Tax rates and withholding treatments are data, not LLM prompt assumptions.
The platform seeds broad historical rate sets; transaction-specific exemptions,
withholding rules and firm policies must be configured with evidence/legal basis.
"""
from __future__ import annotations

from datetime import date

from app.core.db import query, query_one
from app.services.dates import parse_iso

LEGACY_TVA_RATES = {0.0, 7.0, 10.0, 14.0, 20.0}
TVA_RATES_2026 = {0.0, 10.0, 20.0}


def _registry_rates(invoice_date: str | None) -> set[float]:
    d = parse_iso(invoice_date)
    if not d:
        return set()
    ds = d.isoformat()
    try:
        rows = query(
            """SELECT rate FROM tax_rules
               WHERE tax_type='VAT_RATE' AND is_active=1
                 AND effective_from<=?
                 AND (effective_to IS NULL OR effective_to>=?)""",
            (ds, ds),
        )
        return {float(r["rate"]) for r in rows if r.get("rate") not in (None, "")}
    except Exception:
        return set()


def valid_tva_rates(invoice_date: str | None) -> set[float]:
    registry = _registry_rates(invoice_date)
    if registry:
        return registry
    d = parse_iso(invoice_date)
    if d and d >= date(2026, 1, 1):
        return set(TVA_RATES_2026)
    return set(LEGACY_TVA_RATES)


def rate_is_valid(rate: float, invoice_date: str | None) -> bool:
    return float(rate) in valid_tva_rates(invoice_date)


def mixed_rates_are_valid(rates: list[float], invoice_date: str | None) -> bool:
    allowed = valid_tva_rates(invoice_date)
    return all(float(r) in allowed for r in rates)


def find_tax_rule(*, firm_id: str | None, tax_type: str, invoice_date: str | None,
                  transaction_nature: str | None = None, party_type: str | None = None,
                  rate: float | None = None) -> dict | None:
    """Find the most specific active firm rule, then platform rule.

    A caller may use this for exact transaction treatments; no matching rule means
    "review required", never permission to invent a tax treatment.
    """
    d = parse_iso(invoice_date)
    if not d:
        return None
    ds = d.isoformat()
    sql = """SELECT * FROM tax_rules WHERE is_active=1 AND tax_type=?
             AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
             AND (firm_id=? OR firm_id IS NULL)"""
    params: list = [tax_type, ds, ds, firm_id]
    if transaction_nature:
        sql += " AND (transaction_nature=? OR transaction_nature IS NULL)"
        params.append(transaction_nature)
    if party_type:
        sql += " AND (party_type=? OR party_type IS NULL)"
        params.append(party_type)
    if rate is not None:
        sql += " AND rate=?"
        params.append(str(rate).rstrip("0").rstrip(".") if "." in str(rate) else str(rate))
    sql += " ORDER BY CASE WHEN firm_id IS NOT NULL THEN 0 ELSE 1 END, effective_from DESC LIMIT 1"
    try:
        return query_one(sql, tuple(params))
    except Exception:
        return None


def withholding_metadata_complete(data) -> bool:
    """Reject generic/bare RAS assumptions.

    We require the tax type, legal basis and explicit base. VAT-withholding also
    requires an approved liability account and the party/certificate facts that
    determine the applicable regime. This prevents the old "all services = 10%"
    behavior and leaves uncertain cases for accountant review.
    """
    pct = float(getattr(data, "retenue_a_la_source_pct", 0) or 0)
    if pct <= 0:
        return True
    wtype = getattr(data, "withholding_type", None)
    legal = (getattr(data, "withholding_legal_basis", None) or "").strip()
    base = getattr(data, "withholding_base", None)
    if not wtype or not legal or not base:
        return False
    if wtype == "cit_iit":
        return base in {"net_financier_ht", "ttc"}
    if wtype == "vat_withholding":
        if base != "tva_amount" or not getattr(data, "withholding_account_number", None):
            return False
        return (
            getattr(data, "payer_entity_type", "unknown") != "unknown"
            and getattr(data, "supplier_entity_type", "unknown") != "unknown"
            and getattr(data, "supplier_residency", "unknown") != "unknown"
            and getattr(data, "tax_compliance_certificate", "unknown") != "unknown"
        )
    return bool(getattr(data, "withholding_account_number", None))


def tax_risk_level(data) -> str:
    """Deterministic triage, separate from extraction confidence."""
    if float(getattr(data, "retenue_a_la_source_pct", 0) or 0) > 0 and not withholding_metadata_complete(data):
        return "critical"
    rates = [float(x.tva_rate) for x in getattr(data, "line_items", [])] or [float(getattr(data, "tva_pct", 0) or 0)]
    if not mixed_rates_are_valid(rates, getattr(data, "date", None)):
        return "high"
    if any(abs(r) < 0.001 for r in rates) and not (
        getattr(data, "tva_legal_basis", None) or getattr(data, "vat_exemption_code", None)
    ):
        return "high"
    if getattr(data, "invoice_type", "DOIT") == "AVOIR" and not getattr(data, "credit_note_reason", None):
        return "high"
    return "low"
