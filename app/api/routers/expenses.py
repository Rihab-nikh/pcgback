"""Expense claims: employee submits (draft -> open), reviewer approves/rejects.

Visibility: everyone sees their own claims; users with the expenses.approve
permission also see the whole firm's claims.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.api.deps import firm_any, require_permission

submit_perm = require_permission("expenses.submit")
from app.api.routers.permissions import has_permission
from app.api.schemas import ExpenseCreateRequest, ExpenseUpdateRequest, ReviewRequest
from app.core.db import execute, new_id, now, query, query_one
from app.core.storage import (
    StorageError, StorageNotFound, content_type_for, download_headers, get_storage, object_key, safe_filename,
)
from app.repositories.system import audit, notify

router = APIRouter(prefix="/expenses", tags=["expenses"])

_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
STATUSES = ("draft", "open", "approved", "rejected")


def _is_reviewer(user: dict) -> bool:
    return has_permission(user["firm_id"], user["role"], "expenses.approve")


def _get_claim(claim_id: str, user: dict) -> dict:
    claim = query_one("SELECT * FROM expense_claims WHERE id = ? AND firm_id = ?",
                      (claim_id, user["firm_id"]))
    if not claim or (claim["user_id"] != user["id"] and not _is_reviewer(user)):
        raise HTTPException(status_code=404, detail="Claim not found")
    return claim


def _attachments(claim_id: str) -> list[dict]:
    return query("""SELECT a.id, a.filename, a.created_at,
                    (SELECT full_name FROM users WHERE id = a.uploaded_by) AS uploaded_by_name
                    FROM expense_attachments a WHERE a.claim_id = ? ORDER BY a.created_at""",
                 (claim_id,))


@router.get("/summary")
def summary(user: dict = Depends(firm_any)):
    """Status tiles for the dashboard: reviewers see the firm, others themselves."""
    where, params = "firm_id = ?", [user["firm_id"]]
    if not _is_reviewer(user):
        where += " AND user_id = ?"; params.append(user["id"])
    rows = query(f"""SELECT status, COUNT(*) AS n, SUM(amount) AS total
                     FROM expense_claims WHERE {where} GROUP BY status""", tuple(params))
    by = {r["status"]: r for r in rows}
    return {s: {"count": by.get(s, {}).get("n", 0), "total": round(by.get(s, {}).get("total") or 0, 2)}
            for s in STATUSES}


@router.get("")
def list_claims(status: str | None = None, mine: bool = False,
                user: dict = Depends(firm_any)):
    where, params = "c.firm_id = ?", [user["firm_id"]]
    if mine or not _is_reviewer(user):
        where += " AND c.user_id = ?"; params.append(user["id"])
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")
        where += " AND c.status = ?"; params.append(status)
    return query(f"""SELECT c.*,
                     (SELECT full_name FROM users WHERE id = c.user_id) AS user_name,
                     (SELECT full_name FROM users WHERE id = c.reviewed_by) AS reviewer_name,
                     (SELECT COUNT(*) FROM expense_attachments a WHERE a.claim_id = c.id) AS attachment_count
                     FROM expense_claims c WHERE {where} ORDER BY c.created_at DESC""", tuple(params))


@router.post("", status_code=201)
def create_claim(body: ExpenseCreateRequest, user: dict = Depends(submit_perm)):
    claim_id = new_id()
    status = "open" if body.submit else "draft"
    execute("""INSERT INTO expense_claims (id, firm_id, user_id, title, description, category,
               amount, currency, expense_date, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (claim_id, user["firm_id"], user["id"], body.title, body.description, body.category,
             body.amount, body.currency.upper(), body.expense_date, status, now()))
    audit("expense.create", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="expense_claim", entity_id=claim_id, detail=status)
    return _get_claim(claim_id, user)


@router.get("/{claim_id}")
def claim_details(claim_id: str, user: dict = Depends(firm_any)):
    claim = _get_claim(claim_id, user)
    names = query_one("""SELECT (SELECT full_name FROM users WHERE id = ?) AS user_name,
                         (SELECT full_name FROM users WHERE id = ?) AS reviewer_name""",
                      (claim["user_id"], claim["reviewed_by"]))
    return claim | (names or {}) | {"attachments": _attachments(claim_id),
                                    "can_review": _is_reviewer(user) and claim["user_id"] != user["id"]}


@router.patch("/{claim_id}")
def update_claim(claim_id: str, body: ExpenseUpdateRequest, user: dict = Depends(firm_any)):
    claim = _get_claim(claim_id, user)
    if claim["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can edit a claim")
    if claim["status"] != "draft":
        raise HTTPException(status_code=400, detail="Only draft claims can be edited")
    for field in ("title", "description", "category", "amount", "expense_date"):
        value = getattr(body, field)
        if value is not None:
            execute(f"UPDATE expense_claims SET {field} = ? WHERE id = ?", (value, claim_id))
    return _get_claim(claim_id, user)


@router.post("/{claim_id}/submit")
def submit_claim(claim_id: str, user: dict = Depends(submit_perm)):
    claim = _get_claim(claim_id, user)
    if claim["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can submit a claim")
    if claim["status"] != "draft":
        raise HTTPException(status_code=400, detail="Only draft claims can be submitted")
    execute("UPDATE expense_claims SET status = 'open' WHERE id = ?", (claim_id,))
    audit("expense.submit", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="expense_claim", entity_id=claim_id)
    notify(user["firm_id"], "expense_submitted",
           f"Note de frais « {claim['title']} » soumise par {user['full_name']} "
           f"({claim['amount']:.2f} {claim['currency']}).")
    return _get_claim(claim_id, user)


@router.post("/{claim_id}/review")
def review_claim(claim_id: str, body: ReviewRequest, user: dict = Depends(firm_any)):
    claim = _get_claim(claim_id, user)
    if not _is_reviewer(user):
        raise HTTPException(status_code=403, detail="Requires the expenses.approve permission")
    if claim["user_id"] == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot review your own claim")
    if claim["status"] != "open":
        raise HTTPException(status_code=400, detail="Only open claims can be reviewed")
    new_status = "approved" if body.action == "approve" else "rejected"
    execute("""UPDATE expense_claims SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
               WHERE id = ?""", (new_status, user["id"], now(), body.note, claim_id))
    audit(f"expense.{body.action}", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="expense_claim", entity_id=claim_id, detail=body.note or "")
    notify(user["firm_id"], "expense_reviewed",
           f"Note de frais « {claim['title']} » {'approuvée' if new_status == 'approved' else 'rejetée'} "
           f"par {user['full_name']}.", user_id=claim["user_id"])
    return _get_claim(claim_id, user)


# ── Attachments ──
@router.post("/{claim_id}/attachments", status_code=201)
async def upload_attachment(claim_id: str, file: UploadFile = File(...),
                            user: dict = Depends(firm_any)):
    claim = _get_claim(claim_id, user)
    if claim["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can attach files")
    if claim["status"] in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Reviewed claims cannot be modified")
    contents = await file.read()
    if len(contents) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=422, detail="File exceeds 10 MB")
    att_id = new_id()
    safe_name = safe_filename(file.filename, "attachment")
    key = object_key("expenses", user["firm_id"], att_id, safe_name)
    try:
        get_storage().put_bytes(key, contents, content_type=content_type_for(safe_name, file.content_type or "application/octet-stream"))
        execute("""INSERT INTO expense_attachments (id, firm_id, claim_id, filename, uploaded_by, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (att_id, user["firm_id"], claim_id, safe_name, user["id"], now()))
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    except Exception:
        # Avoid orphaning an object if metadata persistence fails.
        try:
            get_storage().delete(key)
        except StorageError:
            pass
        raise
    return {"id": att_id, "filename": safe_name}


@router.get("/attachments/{att_id}/file")
def download_attachment(att_id: str, user: dict = Depends(firm_any)):
    att = query_one("SELECT * FROM expense_attachments WHERE id = ? AND firm_id = ?",
                    (att_id, user["firm_id"]))
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    _get_claim(att["claim_id"], user)  # visibility check
    key = object_key("expenses", user["firm_id"], att_id, att["filename"])
    try:
        contents = get_storage().get_bytes(key)
    except StorageNotFound:
        raise HTTPException(status_code=404, detail="File missing from storage")
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    return Response(content=contents, media_type=content_type_for(att["filename"]),
                    headers=download_headers(att["filename"]))
