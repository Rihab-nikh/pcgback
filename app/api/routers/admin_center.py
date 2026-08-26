"""Automation summary, integrations (API keys, webhooks), firm settings,
billing & subscription.

Honesty notes:
- API keys are real credentials (PBKDF2-hashed, shown once).
- Webhooks are stored configuration; delivery is not implemented yet.
- Plan changes update the plan field — there is no payment processor.
"""
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import firm_admin_only, firm_member, user_admin
from app.core.db import execute, new_id, now, query, query_one
from app.core.security import hash_password
from app.repositories import users as users_repo
from app.repositories.system import audit

router = APIRouter(tags=["admin-center"])


# ── AI Automation Center ───────────────────────────────────────────────────
@router.get("/automation/summary")
def automation_summary(user: dict = Depends(firm_member)):
    """One number per automation, computed from the firm's real data."""
    firm_id = user["firm_id"]

    def one(sql: str, params: tuple = ()) -> int:
        return query_one(sql, (firm_id, *params))["n"] or 0

    auto_published = one("SELECT COUNT(*) AS n FROM audit_logs WHERE firm_id = ? "
                         "AND action = 'invoice.auto_publish'")
    monthly = query("""SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count
                       FROM audit_logs WHERE firm_id = ? AND action = 'invoice.auto_publish'
                       GROUP BY month ORDER BY month DESC LIMIT 12""", (firm_id,))
    return {
        "auto_publish": {
            "rules": one("SELECT COUNT(*) AS n FROM supplier_priors WHERE firm_id = ? AND auto_publish = 1"),
            "invoices_auto_published": auto_published,
        },
        "auto_categorization": {
            "suppliers_with_category": one("SELECT COUNT(*) AS n FROM supplier_priors "
                                           "WHERE firm_id = ? AND invoice_category IS NOT NULL"),
            "established": one("SELECT COUNT(*) AS n FROM supplier_priors "
                               "WHERE firm_id = ? AND confirmations >= 2"),
        },
        "supplier_rules": {
            "manual_rules": one("SELECT COUNT(*) AS n FROM supplier_priors "
                                "WHERE firm_id = ? AND rule_source = 'manual_override'"),
            "ai_learned": one("SELECT COUNT(*) AS n FROM supplier_priors "
                              "WHERE firm_id = ? AND rule_source = 'ai_learned'"),
        },
        "vat_rules": {
            "suppliers_with_vat": one("SELECT COUNT(*) AS n FROM supplier_priors "
                                      "WHERE firm_id = ? AND tva_pct IS NOT NULL"),
            "deviations_caught": one("SELECT COUNT(*) AS n FROM insights "
                                     "WHERE firm_id = ? AND kind = 'vat_deviation'"),
        },
        "duplicate_detection": {
            "duplicates_blocked": one("SELECT COUNT(*) AS n FROM invoices "
                                      "WHERE firm_id = ? AND is_duplicate_of IS NOT NULL"),
        },
        "bank_matching": {
            "auto_suggested": one("SELECT COUNT(*) AS n FROM bank_transactions "
                                  "WHERE firm_id = ? AND status = 'suggested'"),
            "matched": one("SELECT COUNT(*) AS n FROM bank_transactions "
                           "WHERE firm_id = ? AND status = 'matched'"),
        },
        "document_routing": {
            "auto_classified": one("SELECT COUNT(*) AS n FROM documents "
                                   "WHERE firm_id = ? AND ai_classification IS NOT NULL"),
        },
        "auto_published_by_month": list(reversed(monthly)),
    }


# ── Integrations ───────────────────────────────────────────────────────────
class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)


class WebhookCreateRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=500, pattern=r"^https?://")
    events: list[str] = Field(default_factory=lambda: ["invoice.approved"])


@router.get("/integrations/status")
def integrations_status(user: dict = Depends(firm_member)):
    firm = users_repo.get_firm(user["firm_id"])
    keys = query("""SELECT id, name, prefix, created_at, revoked_at,
                    (SELECT full_name FROM users WHERE id = created_by) AS created_by_name
                    FROM api_keys WHERE firm_id = ? ORDER BY created_at DESC""", (user["firm_id"],))
    hooks = query("SELECT * FROM webhooks WHERE firm_id = ? ORDER BY created_at DESC",
                  (user["firm_id"],))
    connectors = query_one("SELECT COUNT(*) AS n FROM supplier_connections WHERE firm_id = ?",
                           (user["firm_id"],))["n"] or 0
    bank_accounts = query_one("SELECT COUNT(*) AS n FROM bank_accounts WHERE firm_id = ? AND is_archived = 0",
                              (user["firm_id"],))["n"] or 0
    return {
        "accounting": {"software": firm.get("accounting_software"),
                       "status": "export_ready",   # FEC/CSV/Odoo exports work today
                       "formats": ["FEC (Sage/Cegid/Quadratus)", "CSV", "Odoo CSV"]},
        "banks": {"status": "import_ready", "accounts": bank_accounts,
                  "formats": ["CSV", "CAMT.053", "MT940"]},
        "supplier_connectors": {"status": "connected" if connectors else "available",
                                "count": connectors},
        "email": {"status": "reserved"},           # address reserved, inbound not live
        "cloud_storage": {"status": "coming_soon"},
        "slack": {"status": "coming_soon"},
        "teams": {"status": "coming_soon"},
        "zapier": {"status": "coming_soon"},
        "api_keys": [k | {"active": k["revoked_at"] is None} for k in keys],
        "webhooks": [h | {"events": json.loads(h["events"] or "[]")} for h in hooks],
    }


@router.post("/integrations/api-keys", status_code=201)
def create_api_key(body: ApiKeyCreateRequest, admin: dict = Depends(user_admin)):
    """Returns the raw key ONCE; only the hash is stored."""
    raw = "pcg_" + secrets.token_urlsafe(32)
    key_id = new_id()
    execute("""INSERT INTO api_keys (id, firm_id, name, prefix, key_hash, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (key_id, admin["firm_id"], body.name, raw[:12], hash_password(raw),
             admin["id"], now()))
    audit("api_key.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="api_key", entity_id=key_id, detail=body.name)
    return {"id": key_id, "name": body.name, "key": raw,
            "warning": "Copiez cette clé maintenant — elle ne sera plus jamais affichée."}


@router.delete("/integrations/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, admin: dict = Depends(user_admin)):
    if not query_one("SELECT 1 FROM api_keys WHERE id = ? AND firm_id = ?",
                     (key_id, admin["firm_id"])):
        raise HTTPException(status_code=404, detail="API key not found")
    execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND firm_id = ?",
            (now(), key_id, admin["firm_id"]))
    audit("api_key.revoke", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="api_key", entity_id=key_id)


@router.post("/integrations/webhooks", status_code=201)
def create_webhook(body: WebhookCreateRequest, admin: dict = Depends(user_admin)):
    hook_id = new_id()
    execute("INSERT INTO webhooks (id, firm_id, url, events, is_active, created_at) VALUES (?,?,?,?,1,?)",
            (hook_id, admin["firm_id"], body.url, json.dumps(body.events), now()))
    audit("webhook.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="webhook", entity_id=hook_id, detail=body.url)
    return query_one("SELECT * FROM webhooks WHERE id = ?", (hook_id,))


@router.delete("/integrations/webhooks/{hook_id}", status_code=204)
def delete_webhook(hook_id: str, admin: dict = Depends(user_admin)):
    execute("DELETE FROM webhooks WHERE id = ? AND firm_id = ?", (hook_id, admin["firm_id"]))
    audit("webhook.delete", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="webhook", entity_id=hook_id)


# ── Business settings ──────────────────────────────────────────────────────
class FirmUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=120)
    accounting_software: str | None = Field(None, max_length=60)
    country: str | None = Field(None, min_length=2, max_length=2)
    currency: str | None = Field(None, min_length=3, max_length=3)
    logo: str | None = Field(None, max_length=500_000)
    settings: dict | None = None    # accounting defaults, tax rates, numbering…


@router.get("/firm")
def get_firm(user: dict = Depends(firm_member)):
    firm = users_repo.get_firm(user["firm_id"])
    firm["settings"] = json.loads(firm.get("settings") or "{}")
    return firm


@router.patch("/firm")
def update_firm(body: FirmUpdateRequest, admin: dict = Depends(user_admin)):
    sets, params = [], []
    for field in ("name", "accounting_software", "country", "currency", "logo"):
        value = getattr(body, field)
        if value is not None:
            sets.append(f"{field} = ?")
            params.append(value.upper() if field in ("country", "currency") else value)
    if body.settings is not None:
        current = json.loads(users_repo.get_firm(admin["firm_id"]).get("settings") or "{}")
        sets.append("settings = ?")
        params.append(json.dumps(current | body.settings))
    if sets:
        execute(f"UPDATE firms SET {', '.join(sets)} WHERE id = ?",
                tuple(params + [admin["firm_id"]]))
        audit("firm.update", user_id=admin["id"], firm_id=admin["firm_id"],
              entity_type="firm", entity_id=admin["firm_id"])
    return get_firm(admin)


@router.get("/firm/audit")
def firm_audit_center(limit: int = 100, admin: dict = Depends(user_admin)):
    from app.repositories.system import list_audit
    return list_audit(admin["firm_id"], limit=min(limit, 500))


# ── Billing & subscription ─────────────────────────────────────────────────
PLANS = {
    "trial":      {"label": "Essai",       "seats": 3,   "invoices_per_month": 50,   "price_mad": 0},
    "pro":        {"label": "Pro",         "seats": 15,  "invoices_per_month": 1000, "price_mad": 990},
    "enterprise": {"label": "Entreprise",  "seats": 999, "invoices_per_month": 999_999, "price_mad": 4900},
}


@router.get("/billing/summary")
def billing_summary(user: dict = Depends(firm_member)):
    firm_id = user["firm_id"]
    firm = users_repo.get_firm(firm_id)
    month = now()[:7]
    seats_used = query_one("SELECT COUNT(*) AS n FROM users WHERE firm_id = ? AND is_active = 1",
                           (firm_id,))["n"] or 0
    invoices_month = query_one("SELECT COUNT(*) AS n FROM invoices WHERE firm_id = ? "
                               "AND substr(created_at, 1, 7) = ?", (firm_id, month))["n"] or 0
    invoices_total = query_one("SELECT COUNT(*) AS n FROM invoices WHERE firm_id = ?",
                               (firm_id,))["n"] or 0
    plan = PLANS.get(firm["plan"], PLANS["trial"])
    return {
        "plan": firm["plan"], "plan_details": plan, "plans": PLANS,
        "member_since": firm["created_at"],
        "usage": {"seats_used": seats_used, "seats_limit": plan["seats"],
                  "invoices_this_month": invoices_month,
                  "invoices_limit": plan["invoices_per_month"],
                  "invoices_total": invoices_total},
        # No payment processor is connected: no invoices, no card on file.
        "payment_method": None,
        "billing_history": [],
    }


@router.post("/billing/plan")
def change_plan(plan: str, admin: dict = Depends(firm_admin_only)):
    """Changes the plan field. No payment is processed — billing integration
    (CMI/Stripe) is a deliberate later step."""
    if plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {plan}")
    execute("UPDATE firms SET plan = ? WHERE id = ?", (plan, admin["firm_id"]))
    audit("billing.plan_change", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="firm", entity_id=admin["firm_id"], detail=plan)
    return {"plan": plan, "note": "Aucun paiement traité — l'intégration de paiement arrive plus tard."}
