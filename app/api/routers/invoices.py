"""Invoice lifecycle: upload -> AI pipeline -> persist -> review -> approve/reject.

Processing is synchronous for now (single worker); the status field
('processing' -> 'needs_review' -> 'approved'/'rejected', or 'failed') is
already shaped for a future queue without an API change.
"""
import base64
import hashlib
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.api.deps import accountant_scope, firm_member, require_permission

upload_perm = require_permission("invoices.upload")
review_perm = require_permission("invoices.review")
from app.api.routers.clients import _get_visible_client
from app.api.schemas import ReviewRequest
from app.core.config import ENABLE_AUTO_PUBLISH, ACCOUNTING_ENGINE_CERTIFIED, EXTRACTION_MODEL
from app.core.db import execute as _db_execute, new_id, now as _db_now
from app.core.logging import logger
from app.core.storage import (
    StorageError, StorageNotFound, content_type_for, download_headers, get_storage, object_key, safe_filename,
)
from app.main_pipeline import process_invoice_pipeline
from app.repositories import invoices as inv_repo
from app.repositories.system import audit, notify
from app.services import insights as intel
from app.services.close import account_explanations
from app.services.health import confidence_breakdown, prior_agreement_note

router = APIRouter(prefix="/invoices", tags=["invoices"])

# Max file size accepted in bulk upload (10 MB per file)
_MAX_BULK_FILE_BYTES = 10 * 1024 * 1024


def _confidence_metrics(response) -> tuple[float, float]:
    checks = response.validation_checks or []
    validation_pass_rate = round(sum(c.passed for c in checks) / len(checks), 3) if checks else 0.0
    fcs = getattr(response.step1_identification, "field_confidences", None) or []
    values = [float(fc.confidence if hasattr(fc, "confidence") else fc.get("confidence", 0)) for fc in fcs]
    extraction_confidence = round(sum(values) / len(values), 3) if values else 0.0
    return extraction_confidence, validation_pass_rate


async def _process_one_document(client_id: str, user: dict, filename: str,
                                contents: bytes, content_type: str | None) -> dict:
    """Run one document through persist -> AI pipeline -> insights.
    Never raises: every outcome is reported in the returned item."""
    invoice_id = new_id()  # allocated before anything so the item always has an id
    item: dict = {"id": invoice_id, "filename": filename, "status": "failed",
                  "invoice_id": None, "error": None}
    try:
        # Size guard — reject before hitting the AI pipeline
        if len(contents) > _MAX_BULK_FILE_BYTES:
            item["error"] = f"Fichier trop volumineux ({len(contents) // 1024} KB > 10 MB)"
            return item

        # Persist metadata in PostgreSQL and binary content in private object storage.
        filename = safe_filename(filename, "invoice")
        storage_key = object_key("invoices", user["firm_id"], invoice_id, filename)
        _db_execute(
            """INSERT INTO invoices
               (id, firm_id, client_id, uploaded_by, filename, file_path, source_hash, status, created_at)
               VALUES (?,?,?,?,?,?,?, 'processing', ?)""",
            (invoice_id, user["firm_id"], client_id, user["id"], filename, storage_key, hashlib.sha256(contents).hexdigest(), _db_now()),
        )
        item["invoice_id"] = invoice_id
        try:
            get_storage().put_bytes(storage_key, contents, content_type=content_type_for(filename, content_type or "image/jpeg"))
        except StorageError as storage_err:
            inv_repo.fail_processing(invoice_id, f"Object storage failure: {storage_err}")
            item["error"] = "Object storage unavailable"
            return item

        # AI pipeline — failure marks the invoice as failed, does not raise
        data_uri = (
            f"data:{content_type or 'image/jpeg'};"
            f"base64,{base64.b64encode(contents).decode()}"
        )
        started = time.monotonic()
        try:
            response = await process_invoice_pipeline(data_uri)
        except Exception as pipeline_err:
            inv_repo.fail_processing(invoice_id, str(pipeline_err))
            item["error"] = str(pipeline_err)[:200]
            return item

        duration_ms = int((time.monotonic() - started) * 1000)
        extraction_confidence, validation_pass_rate = _confidence_metrics(response)
        confidence = validation_pass_rate
        extraction = response.step1_identification
        supplier = getattr(extraction, "supplier_name", None)
        duplicate_of = inv_repo.find_duplicate(
            user["firm_id"], supplier, extraction.invoice_number,
            response.step2_calculations.ttc, invoice_id, supplier_ice=extraction.supplier_ice,
            invoice_date=extraction.date, currency=extraction.currency,
        )
        inv_repo.finish_processing(
            invoice_id,
            response_json=response.model_dump(),
            verdict=response.verdict,
            confidence=confidence,
            extraction_confidence=extraction_confidence, validation_pass_rate=validation_pass_rate,
            invoice_number=extraction.invoice_number,
            supplier_name=supplier,
            invoice_date=extraction.date,
            ttc=response.step2_calculations.ttc,
            net_a_payer=response.step2_calculations.net_a_payer,
            model=EXTRACTION_MODEL,
            duration_ms=duration_ms,
            duplicate_of=duplicate_of,
        )
        intel.generate_insights(
            user["firm_id"], invoice_id,
            extraction.model_dump(), client_id, duplicate_of,
        )
        item["status"] = "duplicate" if duplicate_of else "needs_review"

        # Supplier rule: auto-publish VALID, non-duplicate invoices
        if ENABLE_AUTO_PUBLISH and ACCOUNTING_ENGINE_CERTIFIED and not duplicate_of and response.verdict == "VALID":
            from app.api.routers.rules import auto_publish_rule
            if auto_publish_rule(user["firm_id"], supplier):
                inv_repo.review_invoice(invoice_id, user["firm_id"], user["id"], approve=True)
                from app.services.posting import post_invoice
                post_invoice(user["firm_id"], invoice_id, user["id"])
                audit("invoice.auto_publish", user_id=user["id"], firm_id=user["firm_id"],
                      entity_type="invoice", entity_id=invoice_id, detail=supplier or "")
                item["status"] = "approved"

    except Exception as outer_err:
        # Catch anything not handled above (DB failure, disk full, etc.)
        logger.error("Bulk invoice error", filename=filename, error=str(outer_err))
        item["error"] = str(outer_err)[:200]
        # item["invoice_id"] may already be set — keep it so the UI can link
    return item


def _bulk_summary(results: list[dict]) -> dict:
    return {
        "total": len(results),
        "approved": sum(1 for r in results if r["status"] == "approved"),
        "needs_review": sum(1 for r in results if r["status"] == "needs_review"),
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "items": results,
    }


@router.post("/bulk-upload", status_code=200)
async def bulk_upload_invoices(
    client_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(upload_perm),
):
    """Upload multiple invoices at once. Each file runs the full pipeline independently.

    Guarantees:
    - One file failing never aborts the rest (per-file try/except, no shared transaction).
    - Returns partial successes: every file gets a status entry regardless of outcome.
    - Duplicate filenames are disambiguated via the invoice_id prefix on disk.
    - Files over 10 MB are rejected with status='failed' and a clear error message.
    """
    _get_visible_client(client_id, user)  # authorisation check only
    results = []
    for file in files:
        contents = await file.read()
        results.append(await _process_one_document(
            client_id, user, file.filename or "invoice", contents, file.content_type))
    return _bulk_summary(results)


@router.post("/upload-split", status_code=200)
async def upload_auto_split(client_id: str, file: UploadFile = File(...),
                            user: dict = Depends(upload_perm)):
    """Auto Split: one PDF containing several invoices -> one invoice per page.
    Each page becomes its own document and runs the pipeline independently."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise HTTPException(status_code=501,
                            detail="Auto-split requires pypdf: pip install pypdf")
    _get_visible_client(client_id, user)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Auto-split only works with PDF files")
    contents = await file.read()
    if len(contents) > _MAX_BULK_FILE_BYTES:
        raise HTTPException(status_code=422, detail="File exceeds 10 MB")
    import io as _io
    try:
        reader = PdfReader(_io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Unreadable PDF: {e}")

    base = safe_filename(file.filename, "document.pdf").rsplit(".", 1)[0]
    results = []
    for page_no, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        buf = _io.BytesIO()
        writer.write(buf)
        results.append(await _process_one_document(
            client_id, user, f"{base}_p{page_no}.pdf", buf.getvalue(), "application/pdf"))
    return _bulk_summary(results) | {"pages": len(reader.pages)}


@router.post("/upload", status_code=201)
async def upload_invoice(client_id: str, file: UploadFile = File(...),
                         user: dict = Depends(upload_perm)):
    client = _get_visible_client(client_id, user)
    contents = await file.read()

    # Persist metadata in PostgreSQL; the original binary lives only in object storage.
    filename = safe_filename(file.filename, "invoice")
    invoice_id = inv_repo.create_invoice(user["firm_id"], client_id, user["id"],
                                         filename, None, hashlib.sha256(contents).hexdigest())
    storage_key = object_key("invoices", user["firm_id"], invoice_id, filename)
    try:
        get_storage().put_bytes(storage_key, contents, content_type=content_type_for(filename, file.content_type or "image/jpeg"))
        _db_execute("UPDATE invoices SET file_path=? WHERE id=? AND firm_id=?",
                    (storage_key, invoice_id, user["firm_id"]))
    except StorageError as e:
        inv_repo.fail_processing(invoice_id, f"Object storage failure: {e}")
        raise HTTPException(status_code=503, detail="Object storage unavailable")

    data_uri = f"data:{file.content_type or 'image/jpeg'};base64,{base64.b64encode(contents).decode()}"
    started = time.monotonic()
    try:
        response = await process_invoice_pipeline(data_uri)
    except Exception as e:
        logger.error("Invoice processing failed", invoice_id=invoice_id, error=str(e))
        inv_repo.fail_processing(invoice_id, str(e))
        notify(user["firm_id"], "validation_failed",
               f"Le traitement de « {file.filename} » a échoué pour {client['name']}.",
               invoice_id=invoice_id)
        raise HTTPException(status_code=422, detail=f"Processing failed: {e}")
    duration_ms = int((time.monotonic() - started) * 1000)

    extraction_confidence, validation_pass_rate = _confidence_metrics(response)
    confidence = validation_pass_rate
    extraction = response.step1_identification
    supplier = getattr(extraction, "supplier_name", None)

    duplicate_of = inv_repo.find_duplicate(user["firm_id"], supplier,
                                           extraction.invoice_number,
                                           response.step2_calculations.ttc, invoice_id,
                                           supplier_ice=extraction.supplier_ice, invoice_date=extraction.date,
                                           currency=extraction.currency)

    inv_repo.finish_processing(
        invoice_id,
        response_json=response.model_dump(),
        verdict=response.verdict, confidence=confidence, extraction_confidence=extraction_confidence,
        validation_pass_rate=validation_pass_rate, invoice_number=extraction.invoice_number, supplier_name=supplier,
        invoice_date=extraction.date,
        ttc=response.step2_calculations.ttc,
        net_a_payer=response.step2_calculations.net_a_payer,
        model=EXTRACTION_MODEL, duration_ms=duration_ms, duplicate_of=duplicate_of,
    )
    audit("invoice.process", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id, detail=response.verdict)

    generated = intel.generate_insights(user["firm_id"], invoice_id,
                                        extraction.model_dump(), client_id, duplicate_of)
    for ins in generated:
        if ins["severity"] == "warning" and ins["kind"] != "duplicate":
            notify(user["firm_id"], "insight", ins["message"], invoice_id=invoice_id)

    if duplicate_of:
        notify(user["firm_id"], "duplicate_detected",
               f"Doublon possible : facture {extraction.invoice_number or file.filename} déjà traitée.",
               invoice_id=invoice_id)
    elif response.verdict == "INVALID":
        notify(user["firm_id"], "validation_failed",
               f"Facture {extraction.invoice_number or file.filename} : contrôles non validés.",
               invoice_id=invoice_id)
    else:
        notify(user["firm_id"], "invoice_processed",
               f"Facture {extraction.invoice_number or file.filename} traitée pour {client['name']}.",
               user_id=user["id"], invoice_id=invoice_id)

    return inv_repo.get_invoice(invoice_id, user["firm_id"])


@router.get("")
def list_invoices(client_id: str | None = None, status: str | None = None,
                  q: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, limit: int = 50, offset: int = 0,
                  archived: bool = False, category: str | None = None,
                  user: dict = Depends(firm_member)):
    return inv_repo.list_invoices(user["firm_id"], client_id=client_id, status=status,
                                  q=q, date_from=date_from, date_to=date_to,
                                  accountant_id=accountant_scope(user),
                                  limit=min(limit, 200), offset=max(offset, 0),
                                  archived=archived, category=category)


@router.post("/{invoice_id}/archive")
def archive_invoice(invoice_id: str, archived: bool = True,
                    user: dict = Depends(firm_member)):
    _get_visible_invoice(invoice_id, user)
    _db_execute("UPDATE invoices SET is_archived = ? WHERE id = ? AND firm_id = ?",
                (1 if archived else 0, invoice_id, user["firm_id"]))
    audit("invoice.archive" if archived else "invoice.restore",
          user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id)
    return {"id": invoice_id, "is_archived": archived}


def _get_visible_invoice(invoice_id: str, user: dict) -> dict:
    inv = inv_repo.get_invoice(invoice_id, user["firm_id"])
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _get_visible_client(inv["client_id"], user)  # enforces accountant assignment
    return inv


@router.get("/{invoice_id}")
def get_invoice(invoice_id: str, user: dict = Depends(firm_member)):
    inv = _get_visible_invoice(invoice_id, user)
    inv["insights"] = intel.list_insights(invoice_id, user["firm_id"])
    if inv.get("response"):
        ext = inv["response"]["step1_identification"]
        inv["account_explanations"] = account_explanations(
            ext, inv["response"].get("step4_journal_entries", []))
        note = prior_agreement_note(user["firm_id"], ext)
        if note:
            inv["account_explanations"].append(note)
        inv["confidence_breakdown"] = confidence_breakdown(user["firm_id"], inv)
        # Inject explainability fields inline (single DB round-trip for the workspace)
        from app.services.explain import full_explain as _explain
        try:
            explain = _explain(user["firm_id"], inv)
            inv["split_confidence"] = explain["split_confidence"]
            inv["risk"] = explain["risk"]
            inv["accounting_reasoning"] = explain["accounting_reasoning"]
            inv["ai_suggestions"] = explain["ai_suggestions"]
        except Exception:
            pass  # explainability is non-critical; never break invoice display
    return inv


@router.get("/{invoice_id}/file")
def download_original(invoice_id: str, user: dict = Depends(firm_member)):
    inv = _get_visible_invoice(invoice_id, user)
    key = inv.get("file_path") or object_key("invoices", user["firm_id"], invoice_id, inv.get("filename"))
    try:
        contents = get_storage().get_bytes(key)
    except StorageNotFound:
        raise HTTPException(status_code=404, detail="Original file not stored")
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    return Response(content=contents, media_type=content_type_for(inv.get("filename")),
                    headers=download_headers(inv.get("filename")))


@router.patch("/{invoice_id}/extraction")
async def edit_extraction(invoice_id: str, overrides: dict, user: dict = Depends(review_perm)):
    """Inline editing: apply field corrections to the stored extraction, then
    re-run compute -> journal -> validation -> report (no re-extraction, no
    OpenAI call). The whole workspace updates from the returned invoice."""
    from app.main_pipeline import pipeline_from_data
    from app.models.invoice import ExtractedInvoiceData

    inv = _get_visible_invoice(invoice_id, user)
    if not inv.get("response"):
        raise HTTPException(status_code=409, detail="Invoice has no stored extraction to edit")
    if inv["status"] not in ("needs_review", "rejected"):
        raise HTTPException(status_code=409, detail=f"Cannot edit a '{inv['status']}' invoice")

    current = inv["response"]["step1_identification"]
    editable = set(ExtractedInvoiceData.model_fields) - {"field_confidences", "field_regions"}
    unknown = set(overrides) - editable
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fields: {sorted(unknown)}")
    merged = {**current, **overrides}
    # Human-corrected fields get confidence 1.0
    conf = {fc["field"]: fc["confidence"] for fc in current.get("field_confidences", [])}
    conf.update({f: 1.0 for f in overrides})
    merged["field_confidences"] = [{"field": k, "confidence": v} for k, v in conf.items()]
    try:
        data = ExtractedInvoiceData(**merged)
        response = await pipeline_from_data(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Correction rejected: {e}")

    extraction_confidence, validation_pass_rate = _confidence_metrics(response)
    confidence = validation_pass_rate
    duplicate_of = inv_repo.find_duplicate(user["firm_id"], data.supplier_name,
                                           data.invoice_number, response.step2_calculations.ttc,
                                           invoice_id, supplier_ice=data.supplier_ice, invoice_date=data.date, currency=data.currency)
    inv_repo.finish_processing(
        invoice_id, response_json=response.model_dump(), verdict=response.verdict,
        confidence=confidence, extraction_confidence=extraction_confidence,
        validation_pass_rate=validation_pass_rate, invoice_number=data.invoice_number,
        supplier_name=data.supplier_name, invoice_date=data.date,
        ttc=response.step2_calculations.ttc, net_a_payer=response.step2_calculations.net_a_payer,
        model=inv.get("model") or EXTRACTION_MODEL, duration_ms=inv.get("duration_ms") or 0,
        duplicate_of=duplicate_of,
    )
    intel.learn_from_confirmation(user["firm_id"], data.model_dump(), human_corrected=True)
    intel.generate_insights(user["firm_id"], invoice_id, data.model_dump(),
                            inv["client_id"], duplicate_of)
    audit("invoice.edit", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id, detail=",".join(sorted(overrides)))

    # Record fine-grained field edits for audit trail
    # All fields changed in one save share the same session id
    from app.core.db import execute as _execute, new_id as _new_id, now as _now
    session_id = _new_id()
    for field, new_val in overrides.items():
        old_val = current.get(field)
        if str(old_val) != str(new_val):
            _execute(
                """INSERT INTO invoice_edits
                   (id, invoice_id, firm_id, user_id, edit_session_id,
                    field, old_value, new_value, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (_new_id(), invoice_id, user["firm_id"], user["id"], session_id,
                 field,
                 str(old_val) if old_val is not None else None,
                 str(new_val) if new_val is not None else None,
                 _now()),
            )

    return inv_repo.get_invoice(invoice_id, user["firm_id"])


@router.post("/{invoice_id}/review")
def review(invoice_id: str, body: ReviewRequest, user: dict = Depends(review_perm)):
    inv = _get_visible_invoice(invoice_id, user)
    if inv["status"] not in ("needs_review",):
        raise HTTPException(status_code=409, detail=f"Invoice is '{inv['status']}', not reviewable")
    if body.action == "reject":
        inv_repo.review_invoice(invoice_id, user["firm_id"], user["id"], False)
        audit("invoice.reject", user_id=user["id"], firm_id=user["firm_id"],
              entity_type="invoice", entity_id=invoice_id, detail=body.note)
        return inv_repo.get_invoice(invoice_id, user["firm_id"])

    if inv.get("verdict") == "INVALID":
        if not body.override_invalid or not (body.note or "").strip():
            raise HTTPException(status_code=409, detail="INVALID invoice requires override_invalid=true and a documented reason")
        if user.get("role") not in ("firm_admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Only an administrator can override an INVALID invoice")
        _db_execute("UPDATE invoices SET validation_override_note=? WHERE id=? AND firm_id=?",
                    (body.note.strip(), invoice_id, user["firm_id"]))
        inv["validation_override_note"] = body.note.strip()

    # Enforce any configured ordered approval workflow.
    from app.api.routers.approvals import record_invoice_approval
    wf_inv = dict(inv)
    if inv.get("response"):
        wf_inv["category"] = inv["response"].get("step1_identification", {}).get("invoice_category")
    try:
        complete, workflow_name = record_invoice_approval(user["firm_id"], invoice_id, wf_inv, user["id"], body.note)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not complete:
        audit("invoice.approval_step", user_id=user["id"], firm_id=user["firm_id"],
              entity_type="invoice", entity_id=invoice_id, detail=workflow_name or body.note)
        return inv_repo.get_invoice(invoice_id, user["firm_id"])

    inv_repo.review_invoice(invoice_id, user["firm_id"], user["id"], True)
    if body.reviewer_confidence is not None:
        _db_execute("UPDATE invoices SET reviewer_confidence=? WHERE id=? AND firm_id=?",
                    (body.reviewer_confidence, invoice_id, user["firm_id"]))
    if inv.get("response"):
        intel.learn_from_confirmation(user["firm_id"],
                                      inv["response"]["step1_identification"], human_corrected=False)
    if body.post_now:
        from app.services.posting import post_invoice
        try:
            post_invoice(user["firm_id"], invoice_id, user["id"], body.posting_date)
        except ValueError as e:
            # Approval remains valid, but posting is a distinct accounting action.
            raise HTTPException(status_code=409, detail=f"Approved but not posted: {e}")
    audit("invoice.approve", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id, detail=body.note)
    return inv_repo.get_invoice(invoice_id, user["firm_id"])


@router.post("/{invoice_id}/post")
def post_approved_invoice(invoice_id: str, posting_date: str | None = None, user: dict = Depends(review_perm)):
    inv = _get_visible_invoice(invoice_id, user)
    from app.services.posting import post_invoice
    try:
        result = post_invoice(user["firm_id"], invoice_id, user["id"], posting_date)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit("invoice.post", user_id=user["id"], firm_id=user["firm_id"], entity_type="invoice", entity_id=invoice_id)
    return {"invoice": inv_repo.get_invoice(invoice_id, user["firm_id"]), "posting": result}


@router.post("/{invoice_id}/reverse")
def reverse_posted_invoice(invoice_id: str, reason: str, reversal_date: str | None = None, user: dict = Depends(review_perm)):
    _get_visible_invoice(invoice_id, user)
    if not reason.strip():
        raise HTTPException(status_code=422, detail="Reversal reason is required")
    from app.services.posting import reverse_invoice
    try:
        result = reverse_invoice(user["firm_id"], invoice_id, user["id"], reversal_date, reason.strip())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit("invoice.reverse", user_id=user["id"], firm_id=user["firm_id"], entity_type="invoice", entity_id=invoice_id, detail=reason)
    return {"invoice": inv_repo.get_invoice(invoice_id, user["firm_id"]), "posting": result}


@router.post("/{invoice_id}/reprocess")
async def reprocess(invoice_id: str, user: dict = Depends(review_perm)):
    """Re-run the AI pipeline on the stored original file."""
    inv = _get_visible_invoice(invoice_id, user)
    key = inv.get("file_path") or object_key("invoices", user["firm_id"], invoice_id, inv.get("filename"))
    try:
        contents = get_storage().get_bytes(key)
    except StorageNotFound:
        raise HTTPException(status_code=404, detail="Original file not stored; re-upload instead")
    except StorageError:
        raise HTTPException(status_code=503, detail="Object storage unavailable")
    data_uri = f"data:{content_type_for(inv.get('filename'), 'image/jpeg')};base64,{base64.b64encode(contents).decode()}"
    started = time.monotonic()
    try:
        response = await process_invoice_pipeline(data_uri)
    except Exception as e:
        inv_repo.fail_processing(invoice_id, str(e))
        raise HTTPException(status_code=422, detail=f"Reprocessing failed: {e}")
    extraction_confidence, validation_pass_rate = _confidence_metrics(response)
    confidence = validation_pass_rate
    extraction = response.step1_identification
    inv_repo.finish_processing(
        invoice_id, response_json=response.model_dump(), verdict=response.verdict,
        confidence=confidence, extraction_confidence=extraction_confidence,
        validation_pass_rate=validation_pass_rate, invoice_number=extraction.invoice_number,
        supplier_name=getattr(extraction, "supplier_name", None), invoice_date=extraction.date,
        ttc=response.step2_calculations.ttc, net_a_payer=response.step2_calculations.net_a_payer,
        model=EXTRACTION_MODEL, duration_ms=int((time.monotonic() - started) * 1000),
        duplicate_of=inv["is_duplicate_of"],
    )
    audit("invoice.reprocess", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="invoice", entity_id=invoice_id)
    return inv_repo.get_invoice(invoice_id, user["firm_id"])
