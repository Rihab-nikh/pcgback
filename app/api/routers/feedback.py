"""Per-invoice feedback — the pilot's measurement instrument.

👍/👎 on every processed invoice; 👎 asks why (wrong_account / wrong_vat /
wrong_supplier / wrong_client / ocr_issue / other). The aggregate tells you
exactly where the AI fails in the field — better than guessing what to build.
"""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import firm_member
from app.api.routers.invoices import _get_visible_invoice
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.system import audit

router = APIRouter(tags=["feedback"])

REASONS = ("wrong_account", "wrong_vat", "wrong_supplier", "wrong_client", "ocr_issue", "other")


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reason: Literal["wrong_account", "wrong_vat", "wrong_supplier",
                    "wrong_client", "ocr_issue", "other"] | None = None
    comment: str | None = Field(None, max_length=500)


@router.post("/invoices/{invoice_id}/feedback")
def submit_feedback(invoice_id: str, body: FeedbackRequest, user: dict = Depends(firm_member)):
    _get_visible_invoice(invoice_id, user)
    execute("""INSERT INTO feedback (id, firm_id, invoice_id, user_id, rating, reason, comment, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (invoice_id, user_id) DO UPDATE SET
                 rating=excluded.rating, reason=excluded.reason,
                 comment=excluded.comment, created_at=excluded.created_at""",
            (new_id(), user["firm_id"], invoice_id, user["id"],
             body.rating, body.reason if body.rating == "down" else None,
             body.comment, now()))
    audit("invoice.feedback", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id,
          detail=f"{body.rating}:{body.reason or ''}")
    return {"invoice_id": invoice_id, "rating": body.rating, "reason": body.reason}


@router.get("/invoices/{invoice_id}/feedback")
def my_feedback(invoice_id: str, user: dict = Depends(firm_member)):
    _get_visible_invoice(invoice_id, user)
    return query_one("""SELECT rating, reason, comment FROM feedback
                        WHERE invoice_id = ? AND user_id = ?""",
                     (invoice_id, user["id"])) or {"rating": None}


@router.get("/metrics/feedback")
def feedback_summary(user: dict = Depends(firm_member)):
    """The gold data: satisfaction rate + failure modes ranked."""
    totals = query_one("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN rating='up' THEN 1 ELSE 0 END) AS up FROM feedback
        WHERE firm_id = ?""", (user["firm_id"],))
    total, up = totals["total"] or 0, totals["up"] or 0
    reasons = query("""SELECT reason, COUNT(*) AS count FROM feedback
                       WHERE firm_id = ? AND rating='down' AND reason IS NOT NULL
                       GROUP BY reason ORDER BY count DESC""", (user["firm_id"],))
    comments = query("""SELECT f.comment, f.reason, v.invoice_number FROM feedback f
                        JOIN invoices v ON v.id = f.invoice_id
                        WHERE f.firm_id = ? AND f.comment IS NOT NULL
                        ORDER BY f.created_at DESC LIMIT 20""", (user["firm_id"],))
    return {"total": total, "helpful": up, "not_helpful": total - up,
            "satisfaction_rate": round(up / total, 3) if total else None,
            "failure_modes": reasons, "recent_comments": comments}
