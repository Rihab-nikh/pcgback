"""OD manuelles — écritures de régularisation, volontairement simples :
journal, date, libellé, lignes débit/crédit, pièce jointe optionnelle.
Règle bloquante : Σ débits = Σ crédits (0,01). Tout est audité.

Elles alimentent journal_rows : Grand Livre, Balance, Lettrage, Balance âgée
et Fiche compte les intègrent automatiquement.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import require_permission
from app.core.db import execute, new_id, now, query, query_one
from app.core.storage import (
    StorageError, StorageNotFound, content_type_for, download_headers, get_storage, object_key, safe_filename,
)
from app.repositories.system import audit
from app.services.dates import normalize_date
from app.services.posting import post_manual_entry, reverse_manual_entry, to_cents

router = APIRouter(prefix="/od", tags=["od"])
od_perm = require_permission("invoices.review")   # saisir une OD = acte de révision
journal_view = require_permission("journal.view")

JOURNALS = ("OD", "AN", "BQ", "CAI")
_MAX_PIECE_BYTES = 10 * 1024 * 1024


class OdLine(BaseModel):
    account_number: str = Field(..., min_length=2, max_length=10)
    account_label: str = Field(..., min_length=1, max_length=120)
    side: str = Field(..., pattern="^(DEBIT|CREDIT)$")
    amount: float = Field(..., gt=0)


class OdCreateRequest(BaseModel):
    journal: str = Field("OD", pattern="^(OD|AN|BQ|CAI)$")
    date: str = Field(..., min_length=10, max_length=10)   # YYYY-MM-DD
    libelle: str = Field(..., min_length=3, max_length=200)
    lines: list[OdLine] = Field(..., min_length=2)


@router.get("")
def list_entries(user: dict = Depends(journal_view)):
    entries = query("""SELECT e.*, u.full_name AS created_by_name
                       FROM manual_entries e JOIN users u ON u.id = e.created_by
                       WHERE e.firm_id = ? ORDER BY e.date DESC, e.created_at DESC
                       LIMIT 200""", (user["firm_id"],))
    for e in entries:
        e["lines"] = query("""SELECT line_idx, account_number, account_label, side, amount
                              FROM manual_entry_lines WHERE entry_id = ?
                              ORDER BY line_idx""", (e["id"],))
        e["total"] = round(sum(l["amount"] for l in e["lines"] if l["side"] == "DEBIT"), 2)
    return entries


@router.post("", status_code=201)
def create_entry(body: OdCreateRequest, user: dict = Depends(od_perm)):
    posting_date = normalize_date(body.date)
    if not posting_date or posting_date != body.date:
        raise HTTPException(status_code=422, detail="Date OD invalide; format requis YYYY-MM-DD")
    d_cents = sum(to_cents(l.amount) for l in body.lines if l.side == "DEBIT")
    c_cents = sum(to_cents(l.amount) for l in body.lines if l.side == "CREDIT")
    if d_cents != c_cents:
        raise HTTPException(status_code=422, detail=f"Écriture déséquilibrée au centime : {d_cents/100:.2f} ≠ {c_cents/100:.2f}")
    if d_cents <= 0:
        raise HTTPException(status_code=422, detail="Écriture vide")
    d = d_cents / 100
    eid = new_id()
    execute("""INSERT INTO manual_entries (id, firm_id, journal, date, libelle, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (eid, user["firm_id"], body.journal, body.date, body.libelle, user["id"], now()))
    for i, l in enumerate(body.lines):
        execute("""INSERT INTO manual_entry_lines (entry_id, line_idx, account_number,
                   account_label, side, amount) VALUES (?,?,?,?,?,?)""",
                (eid, i, l.account_number, l.account_label, l.side, l.amount))
    try:
        posting = post_manual_entry(user["firm_id"], eid, user["id"])
    except ValueError as e:
        execute("DELETE FROM manual_entry_lines WHERE entry_id=?", (eid,))
        execute("DELETE FROM manual_entries WHERE id=?", (eid,))
        raise HTTPException(status_code=409, detail=str(e))
    audit("od.create", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="manual_entry", entity_id=eid,
          detail=f"{body.journal} {body.date} · {d:.2f} MAD · {len(body.lines)} lignes")
    return {"id": eid, "total": d, "lines": len(body.lines), "posting": posting}


@router.delete("/{entry_id}")
def delete_entry(entry_id: str, user: dict = Depends(od_perm)):
    """Posted ODs are immutable. Use a dated reversal instead of deletion."""
    e = query_one("SELECT id FROM manual_entries WHERE id = ? AND firm_id = ?",
                  (entry_id, user["firm_id"]))
    if not e:
        raise HTTPException(status_code=404, detail="OD introuvable")
    raise HTTPException(status_code=409, detail="OD comptabilisée et immuable — utilisez /reverse pour une contre-passation")


@router.post("/{entry_id}/reverse")
def reverse_entry(entry_id: str, reversal_date: str, reason: str, user: dict = Depends(od_perm)):
    if not reason.strip():
        raise HTTPException(status_code=422, detail="Motif de contre-passation obligatoire")
    try:
        posting = reverse_manual_entry(user["firm_id"], entry_id, user["id"], reversal_date, reason.strip())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit("od.reverse", user_id=user["id"], firm_id=user["firm_id"], entity_type="manual_entry", entity_id=entry_id, detail=reason.strip())
    return {"id": entry_id, "reversal": posting}


@router.post("/{entry_id}/piece", status_code=201)
async def attach_piece(entry_id: str, file: UploadFile = File(...),
                       user: dict = Depends(od_perm)):
    e = query_one("SELECT id, piece FROM manual_entries WHERE id = ? AND firm_id = ?",
                  (entry_id, user["firm_id"]))
    if not e:
        raise HTTPException(status_code=404, detail="OD introuvable")
    contents = await file.read()
    if len(contents) > _MAX_PIECE_BYTES:
        raise HTTPException(status_code=422, detail="Fichier > 10 MB")
    safe = safe_filename(file.filename, "piece")
    key = object_key("od", user["firm_id"], entry_id, safe)
    try:
        get_storage().put_bytes(key, contents, content_type=content_type_for(safe, file.content_type or "application/octet-stream"))
        execute("UPDATE manual_entries SET piece = ? WHERE id = ?", (safe, entry_id))
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    except Exception:
        try:
            get_storage().delete(key)
        except StorageError:
            pass
        raise
    # If a previous piece had another name, remove its now-unreferenced object.
    if e.get("piece") and e["piece"] != safe:
        try:
            get_storage().delete(object_key("od", user["firm_id"], entry_id, e["piece"]))
        except StorageError:
            pass
    return {"id": entry_id, "piece": safe}


@router.get("/{entry_id}/piece")
def download_piece(entry_id: str, user: dict = Depends(journal_view)):
    e = query_one("SELECT piece FROM manual_entries WHERE id = ? AND firm_id = ?",
                  (entry_id, user["firm_id"]))
    if not e or not e["piece"]:
        raise HTTPException(status_code=404, detail="Pièce introuvable")
    key = object_key("od", user["firm_id"], entry_id, e["piece"])
    try:
        contents = get_storage().get_bytes(key)
    except StorageNotFound:
        raise HTTPException(status_code=404, detail="Fichier absent du stockage")
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    return Response(content=contents, media_type=content_type_for(e["piece"]),
                    headers=download_headers(e["piece"]))
