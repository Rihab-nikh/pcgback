"""Approval center: the pending queue (invoices + expense claims) and the
workflow builder (conditions -> approver chain).

Workflows are stored and used to ROUTE items in the queue (each pending
invoice is annotated with the matching workflow and its approvers). They are enforced by the invoice review endpoint. Ordered approvers are
materialized as approval steps and must approve sequentially.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import firm_member, user_admin
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.invoices import _norm_supplier
from app.repositories.system import audit

router = APIRouter(prefix="/approvals", tags=["approvals"])


class WorkflowRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    supplier: str | None = None            # condition: supplier name fragment
    category: str | None = None            # condition: invoice_category
    min_amount: float | None = Field(None, ge=0)
    max_amount: float | None = Field(None, ge=0)
    approvers: list[str] = Field(..., min_length=1)   # ordered user ids
    priority: int = 0
    is_active: bool = True


def _load_workflows(firm_id: str) -> list[dict]:
    rows = query("""SELECT * FROM approval_workflows WHERE firm_id = ?
                    ORDER BY priority DESC, created_at""", (firm_id,))
    out = []
    for r in rows:
        r["conditions"] = json.loads(r["conditions"] or "{}")
        r["approvers"] = json.loads(r["approvers"] or "[]")
        names = query(f"""SELECT id, full_name FROM users
                          WHERE id IN ({','.join('?' * len(r['approvers'])) or "''"})""",
                      tuple(r["approvers"]))
        by_id = {u["id"]: u["full_name"] for u in names}
        r["approver_names"] = [by_id.get(a, "?") for a in r["approvers"]]
        out.append(r)
    return out


def _matches(wf: dict, inv: dict) -> bool:
    c = wf["conditions"]
    if c.get("supplier") and _norm_supplier(c["supplier"]) not in _norm_supplier(inv.get("supplier_name")):
        return False
    if c.get("category") and inv.get("category") != c["category"]:
        return False
    amount = inv.get("ttc") or 0
    if c.get("min_amount") is not None and amount < c["min_amount"]:
        return False
    if c.get("max_amount") is not None and amount > c["max_amount"]:
        return False
    return True


@router.get("/queue")
def approval_queue(user: dict = Depends(firm_member)):
    """Pending invoices + pending expense claims, invoices annotated with the
    matching workflow (highest priority wins)."""
    firm_id = user["firm_id"]
    workflows = [w for w in _load_workflows(firm_id) if w["is_active"]]
    invoices = query("""SELECT i.id, i.supplier_name, i.invoice_number, i.invoice_date, i.ttc,
                        i.confidence, i.created_at, c.name AS client_name,
                        json_extract(i.response_json, '$.step1_identification.invoice_category') AS category
                        FROM invoices i JOIN clients c ON c.id = i.client_id
                        WHERE i.firm_id = ? AND i.status = 'needs_review' AND i.is_archived = 0
                        ORDER BY i.created_at""", (firm_id,))
    for inv in invoices:
        wf = next((w for w in workflows if _matches(w, inv)), None)
        inv["workflow"] = ({"id": wf["id"], "name": wf["name"],
                            "approver_names": wf["approver_names"]} if wf else None)
    claims = query("""SELECT c.id, c.title, c.amount, c.currency, c.category, c.created_at,
                      (SELECT full_name FROM users WHERE id = c.user_id) AS user_name
                      FROM expense_claims c WHERE c.firm_id = ? AND c.status = 'open'
                      ORDER BY c.created_at""", (firm_id,))
    return {"invoices": invoices, "expense_claims": claims,
            "workflow_count": len(workflows)}


@router.get("/workflows")
def list_workflows(user: dict = Depends(firm_member)):
    return _load_workflows(user["firm_id"])


@router.post("/workflows", status_code=201)
def create_workflow(body: WorkflowRequest, admin: dict = Depends(user_admin)):
    for uid in body.approvers:
        u = query_one("SELECT 1 FROM users WHERE id = ? AND firm_id = ?", (uid, admin["firm_id"]))
        if not u:
            raise HTTPException(status_code=404, detail=f"Approver not found: {uid}")
    wf_id = new_id()
    conditions = {k: v for k, v in (("supplier", body.supplier), ("category", body.category),
                                    ("min_amount", body.min_amount), ("max_amount", body.max_amount))
                  if v is not None and v != ""}
    execute("""INSERT INTO approval_workflows (id, firm_id, name, conditions, approvers,
               priority, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (wf_id, admin["firm_id"], body.name, json.dumps(conditions),
             json.dumps(body.approvers), body.priority, 1 if body.is_active else 0, now()))
    audit("workflow.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="approval_workflow", entity_id=wf_id, detail=body.name)
    return _load_workflows(admin["firm_id"])


@router.delete("/workflows/{wf_id}", status_code=204)
def delete_workflow(wf_id: str, admin: dict = Depends(user_admin)):
    execute("DELETE FROM approval_workflows WHERE id = ? AND firm_id = ?",
            (wf_id, admin["firm_id"]))
    audit("workflow.delete", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="approval_workflow", entity_id=wf_id)


def matching_workflow(firm_id: str, inv: dict) -> dict | None:
    """Return the highest-priority active workflow matching an invoice."""
    workflows = [w for w in _load_workflows(firm_id) if w["is_active"]]
    return next((w for w in workflows if _matches(w, inv)), None)


def ensure_invoice_steps(firm_id: str, invoice_id: str, inv: dict) -> list[dict]:
    """Materialize the matching workflow as immutable ordered approval steps."""
    existing = query("SELECT * FROM invoice_approval_steps WHERE firm_id=? AND invoice_id=? ORDER BY step_index",
                     (firm_id, invoice_id))
    if existing:
        return existing
    wf = matching_workflow(firm_id, inv)
    if not wf:
        return []
    for idx, uid in enumerate(wf["approvers"]):
        execute("""INSERT OR IGNORE INTO invoice_approval_steps
                   (id,invoice_id,firm_id,workflow_id,step_index,approver_id)
                   VALUES (?,?,?,?,?,?)""",
                (new_id(), invoice_id, firm_id, wf["id"], idx, uid))
    return query("SELECT * FROM invoice_approval_steps WHERE firm_id=? AND invoice_id=? ORDER BY step_index",
                 (firm_id, invoice_id))


def record_invoice_approval(firm_id: str, invoice_id: str, inv: dict,
                            approver_id: str, note: str | None) -> tuple[bool, str | None]:
    """Enforce ordered workflow. Returns (workflow_complete, workflow_name)."""
    steps = ensure_invoice_steps(firm_id, invoice_id, inv)
    if not steps:
        return True, None
    pending = next((s for s in steps if not s.get("approved_at")), None)
    if pending is None:
        return True, matching_workflow(firm_id, inv)["name"] if matching_workflow(firm_id, inv) else None
    if pending["approver_id"] != approver_id:
        raise PermissionError("This invoice is awaiting approval from another configured approver")
    execute("UPDATE invoice_approval_steps SET approved_at=?, note=? WHERE id=?",
            (now(), note, pending["id"]))
    remaining = query_one("SELECT COUNT(*) AS n FROM invoice_approval_steps WHERE invoice_id=? AND approved_at IS NULL",
                          (invoice_id,))["n"]
    wf = matching_workflow(firm_id, inv)
    return remaining == 0, wf["name"] if wf else None
