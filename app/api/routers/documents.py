"""GED: multi-category document management.

Upload (single or bulk) -> store file -> best-effort AI classification + OCR
-> searchable, versioned, retention-tracked documents. Invoices keep their
dedicated pipeline in /invoices; a 'facture' uploaded here is stored as a
plain document (route it through /invoices/upload for accounting)."""
import base64
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.api.deps import accountant_scope, firm_admin_only, firm_member
from app.api.routers.clients import _get_visible_client
from app.api.schemas import DocumentUpdateRequest
from app.core.logging import logger
from app.core.storage import (
    StorageError, StorageNotFound, content_type_for, download_headers, get_storage, object_key, safe_filename,
)
from app.repositories import documents as docs_repo
from app.repositories.system import audit, notify
from app.services.document_ai import CATEGORIES, analyze_document

router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_FILE_BYTES = 15 * 1024 * 1024   # 15 MB per file
RETENTION_YEARS = 10                 # Code de commerce: 10-year retention for accounting records


def _retention_until() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=365 * RETENTION_YEARS)).isoformat()


def _document_key(firm_id: str, doc_id: str, filename: str) -> str:
    return object_key("documents", firm_id, doc_id, filename)


def _get_visible_document(doc_id: str, user: dict) -> dict:
    doc = docs_repo.get_document(doc_id, user["firm_id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.get("client_id"):
        _get_visible_client(doc["client_id"], user)  # enforces accountant assignment
    return doc


async def _analyze_best_effort(doc_id: str, contents: bytes, content_type: str | None,
                               explicit_category: str | None, firm_id: str) -> None:
    """AI classification + OCR. Failure marks ocr_status='failed', never raises."""
    data_uri = f"data:{content_type or 'image/jpeg'};base64,{base64.b64encode(contents).decode()}"
    try:
        analysis = await analyze_document(data_uri)
        docs_repo.set_ocr(doc_id, "done", analysis.text, analysis.category)
        if not explicit_category:  # user's explicit choice wins over the AI suggestion
            docs_repo.update_document(doc_id, firm_id, category=analysis.category)
    except Exception as e:
        logger.error("Document analysis failed", doc_id=doc_id, error=str(e))
        docs_repo.set_ocr(doc_id, "failed", None, None)


@router.post("/upload", status_code=200)
async def upload_documents(client_id: str | None = None, category: str | None = None,
                           files: list[UploadFile] = File(...),
                           user: dict = Depends(firm_member)):
    """Bulk upload (a single file is a batch of one). Per-file isolation:
    one failure never aborts the rest; every file gets a result entry."""
    if client_id:
        _get_visible_client(client_id, user)
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Unknown category: {category}")

    results: list[dict] = []
    for file in files:
        item: dict = {"id": None, "filename": file.filename or "document",
                      "status": "failed", "error": None}
        try:
            contents = await file.read()
            if len(contents) > _MAX_FILE_BYTES:
                item["error"] = f"Fichier trop volumineux ({len(contents) // 1024} KB > 15 MB)"
                results.append(item)
                continue
            filename = safe_filename(file.filename, "document")
            doc = docs_repo.create_document(
                user["firm_id"], client_id=client_id, category=category or "divers",
                filename=filename, mime_type=file.content_type,
                size_bytes=len(contents), uploaded_by=user["id"],
                retention_until=_retention_until())
            try:
                get_storage().put_bytes(
                    _document_key(user["firm_id"], doc["id"], doc["filename"]),
                    contents, content_type=content_type_for(doc["filename"], file.content_type or "application/octet-stream"),
                )
            except StorageError:
                docs_repo.delete_document_record(doc["id"], user["firm_id"])
                raise
            item["id"] = doc["id"]
            await _analyze_best_effort(doc["id"], contents, file.content_type,
                                       category, user["firm_id"])
            item["status"] = "stored"
        except Exception as e:
            logger.error("Document upload error", filename=file.filename, error=str(e))
            item["error"] = str(e)[:200]
        results.append(item)

    stored = sum(1 for r in results if r["status"] == "stored")
    audit("document.upload", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="document", entity_id=results[0]["id"] if results else None,
          detail=f"{stored}/{len(results)} fichier(s)")
    if stored:
        notify(user["firm_id"], "document_uploaded",
               f"{stored} document(s) ajouté(s) à la GED.", user_id=user["id"])
    return {"total": len(results), "stored": stored,
            "failed": len(results) - stored, "items": results}


@router.get("")
def list_documents(client_id: str | None = None, category: str | None = None,
                   q: str | None = None, include_archived: bool = False,
                   limit: int = 50, offset: int = 0,
                   user: dict = Depends(firm_member)):
    return docs_repo.list_documents(
        user["firm_id"], client_id=client_id, category=category, q=q,
        include_archived=include_archived, accountant_id=accountant_scope(user),
        limit=min(limit, 200), offset=max(offset, 0))


@router.get("/categories")
def categories(user: dict = Depends(firm_member)):
    """Folder view: document count per category (active, latest versions)."""
    counts = {c["category"]: c["count"]
              for c in docs_repo.category_counts(user["firm_id"], accountant_scope(user))}
    return [{"category": c, "count": counts.get(c, 0)} for c in CATEGORIES]


@router.get("/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(firm_member)):
    doc = _get_visible_document(doc_id, user)
    doc["versions"] = docs_repo.version_history(doc_id, user["firm_id"])
    return doc


@router.get("/{doc_id}/file")
def download_document(doc_id: str, user: dict = Depends(firm_member)):
    doc = _get_visible_document(doc_id, user)
    key = _document_key(user["firm_id"], doc_id, doc["filename"])
    try:
        contents = get_storage().get_bytes(key)
    except StorageNotFound:
        raise HTTPException(status_code=404, detail="File not stored")
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    return Response(content=contents, media_type=doc.get("mime_type") or content_type_for(doc["filename"]),
                    headers=download_headers(doc["filename"]))


@router.patch("/{doc_id}")
def update_document(doc_id: str, body: DocumentUpdateRequest,
                    user: dict = Depends(firm_member)):
    _get_visible_document(doc_id, user)
    if body.category is not None and body.category not in CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Unknown category: {body.category}")
    if body.client_id:  # reattaching to a client requires visibility of that client
        _get_visible_client(body.client_id, user)
    docs_repo.update_document(doc_id, user["firm_id"], category=body.category,
                              tags=body.tags, client_id=body.client_id)
    audit("document.update", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="document", entity_id=doc_id)
    return docs_repo.get_document(doc_id, user["firm_id"])


@router.post("/{doc_id}/archive")
def archive_document(doc_id: str, archived: bool = True,
                     admin: dict = Depends(firm_admin_only)):
    _get_visible_document(doc_id, admin)
    docs_repo.update_document(doc_id, admin["firm_id"], is_archived=archived)
    audit("document.archive" if archived else "document.unarchive",
          user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="document", entity_id=doc_id)
    return docs_repo.get_document(doc_id, admin["firm_id"])


@router.post("/{doc_id}/version", status_code=201)
async def upload_version(doc_id: str, file: UploadFile = File(...),
                         user: dict = Depends(firm_member)):
    """Replace a document: stores a new version linked to the previous one."""
    previous = _get_visible_document(doc_id, user)
    contents = await file.read()
    if len(contents) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=422, detail="File exceeds 15 MB")
    doc = docs_repo.create_document(
        user["firm_id"], client_id=previous["client_id"], category=previous["category"],
        filename=safe_filename(file.filename, previous["filename"]), mime_type=file.content_type,
        size_bytes=len(contents), uploaded_by=user["id"],
        retention_until=_retention_until(),
        version=previous["version"] + 1, parent_id=previous["id"])
    docs_repo.update_document(doc["id"], user["firm_id"], tags=previous["tags"])
    try:
        get_storage().put_bytes(
            _document_key(user["firm_id"], doc["id"], doc["filename"]), contents,
            content_type=content_type_for(doc["filename"], file.content_type or "application/octet-stream"),
        )
    except StorageError:
        docs_repo.delete_document_record(doc["id"], user["firm_id"])
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    await _analyze_best_effort(doc["id"], contents, file.content_type,
                               previous["category"], user["firm_id"])
    audit("document.version", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="document", entity_id=doc["id"],
          detail=f"v{doc['version']} de {previous['id']}")
    return docs_repo.get_document(doc["id"], user["firm_id"])
