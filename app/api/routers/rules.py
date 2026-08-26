"""Supplier rules — the firm's explicit automation policy per supplier.

Rules live in supplier_priors (same table the AI learns into): a manual rule
sets rule_source='manual_override'. auto_publish is enforced in the upload
pipeline: a VALID, non-duplicate invoice from that supplier is approved
without human review.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import firm_member
from app.core.db import execute, now, query, query_one
from app.repositories.invoices import _norm_supplier
from app.repositories.system import audit

router = APIRouter(prefix="/rules", tags=["rules"])


class RuleUpsertRequest(BaseModel):
    supplier: str = Field(..., min_length=2, max_length=200)
    invoice_category: str | None = None          # facture_achat | facture_service | ...
    tva_pct: float | None = Field(None, ge=0, le=20)
    payment_account: str | None = Field(None, max_length=10)   # 5141 | 5161 | ...
    rule_description: str | None = Field(None, max_length=200)
    auto_publish: bool = False
    extract_line_items: bool = False


@router.get("")
def list_rules(user: dict = Depends(firm_member)):
    rows = query("""SELECT supplier_norm AS supplier, invoice_category, tva_pct,
                    payment_account, rule_description, auto_publish, extract_line_items,
                    confirmations, locked, rule_source, updated_at
                    FROM supplier_priors WHERE firm_id = ?
                    ORDER BY rule_source = 'manual_override' DESC, confirmations DESC""",
                 (user["firm_id"],))
    return rows


@router.post("", status_code=201)
def upsert_rule(body: RuleUpsertRequest, user: dict = Depends(firm_member)):
    norm = _norm_supplier(body.supplier)
    if not norm:
        raise HTTPException(status_code=422, detail="Invalid supplier name")
    existing = query_one("SELECT 1 FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
                         (user["firm_id"], norm))
    if existing:
        execute("""UPDATE supplier_priors SET invoice_category = COALESCE(?, invoice_category),
                   tva_pct = COALESCE(?, tva_pct), payment_account = ?, rule_description = ?,
                   auto_publish = ?, extract_line_items = ?, rule_source = 'manual_override',
                   locked = 1, updated_at = ?
                   WHERE firm_id = ? AND supplier_norm = ?""",
                (body.invoice_category, body.tva_pct, body.payment_account,
                 body.rule_description, 1 if body.auto_publish else 0,
                 1 if body.extract_line_items else 0, now(), user["firm_id"], norm))
    else:
        execute("""INSERT INTO supplier_priors (firm_id, supplier_norm, invoice_category,
                   tva_pct, payment_account, rule_description, auto_publish, extract_line_items,
                   confirmations, locked, rule_source, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,0,1,'manual_override',?)""",
                (user["firm_id"], norm, body.invoice_category, body.tva_pct,
                 body.payment_account, body.rule_description,
                 1 if body.auto_publish else 0, 1 if body.extract_line_items else 0, now()))
    audit("rule.upsert", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="supplier_rule", entity_id=norm,
          detail=f"auto_publish={body.auto_publish}")
    return query_one("SELECT * FROM supplier_priors WHERE firm_id = ? AND supplier_norm = ?",
                     (user["firm_id"], norm))


@router.delete("/{supplier}", status_code=204)
def delete_rule(supplier: str, user: dict = Depends(firm_member)):
    """Removes the manual rule flags; AI-learned facts (category, TVA) stay."""
    execute("""UPDATE supplier_priors SET payment_account = NULL, rule_description = NULL,
               auto_publish = 0, extract_line_items = 0,
               rule_source = 'ai_learned', locked = 0, updated_at = ?
               WHERE firm_id = ? AND supplier_norm = ?""",
            (now(), user["firm_id"], supplier))
    audit("rule.delete", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="supplier_rule", entity_id=supplier)


def auto_publish_rule(firm_id: str, supplier_name: str | None) -> dict | None:
    """Enforcement lookup used by the upload pipeline."""
    if not supplier_name:
        return None
    norm = _norm_supplier(supplier_name)
    return query_one("""SELECT * FROM supplier_priors
                        WHERE firm_id = ? AND supplier_norm = ? AND auto_publish = 1""",
                     (firm_id, norm))
