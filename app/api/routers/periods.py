"""Accounting periods and close/reopen controls.

Posting is permitted only in OPEN configured periods. If a firm has not yet
configured periods, posting stays backward-compatible/open; once periods are
created, the dates they cover are governed here.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import firm_admin_only, firm_member
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.system import audit
from app.services.dates import normalize_date

router = APIRouter(prefix="/accounting-periods", tags=["accounting-periods"])


class PeriodCreate(BaseModel):
    period_start: str
    period_end: str


class PeriodStatusChange(BaseModel):
    status: str = Field(..., pattern="^(OPEN|SOFT_CLOSED|CLOSED)$")
    reason: str = Field(..., min_length=3, max_length=500)


@router.get("")
def list_periods(user: dict = Depends(firm_member)):
    return query("SELECT * FROM accounting_periods WHERE firm_id=? ORDER BY period_start DESC",
                 (user["firm_id"],))


@router.post("", status_code=201)
def create_period(body: PeriodCreate, admin: dict = Depends(firm_admin_only)):
    start, end = normalize_date(body.period_start), normalize_date(body.period_end)
    if not start or not end or end < start:
        raise HTTPException(status_code=422, detail="Invalid accounting period dates")
    overlap = query_one("""SELECT id FROM accounting_periods
                           WHERE firm_id=? AND NOT(period_end < ? OR period_start > ?)""",
                        (admin["firm_id"], start, end))
    if overlap:
        raise HTTPException(status_code=409, detail="Accounting periods may not overlap")
    pid = new_id()
    execute("""INSERT INTO accounting_periods
               (id,firm_id,period_start,period_end,status,created_at)
               VALUES (?,?,?,?,'OPEN',?)""",
            (pid, admin["firm_id"], start, end, now()))
    audit("period.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="accounting_period", entity_id=pid, detail=f"{start}..{end}")
    return query_one("SELECT * FROM accounting_periods WHERE id=?", (pid,))


@router.post("/{period_id}/status")
def change_period_status(period_id: str, body: PeriodStatusChange,
                         admin: dict = Depends(firm_admin_only)):
    period = query_one("SELECT * FROM accounting_periods WHERE id=? AND firm_id=?",
                       (period_id, admin["firm_id"]))
    if not period:
        raise HTTPException(status_code=404, detail="Accounting period not found")
    old = period["status"]
    if old == body.status:
        return period
    # Reopening is exceptional and remains fully auditable.
    execute("""UPDATE accounting_periods SET status=?, closed_by=?, closed_at=? WHERE id=?""",
            (body.status,
             admin["id"] if body.status != "OPEN" else None,
             now() if body.status != "OPEN" else None,
             period_id))
    audit("period.status", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="accounting_period", entity_id=period_id,
          detail=f"{old}->{body.status}: {body.reason}")
    return query_one("SELECT * FROM accounting_periods WHERE id=?", (period_id,))
