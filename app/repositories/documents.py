"""GED documents. Same tenant rules as invoices: firm_id on every query,
accountants additionally restricted to their assigned clients (firm-level
documents with client_id NULL stay visible to every firm member)."""
import json

from app.core.db import execute, new_id, now, query, query_one

_SELECT = """
    SELECT d.*, c.name AS client_name,
           (SELECT full_name FROM users WHERE id = d.uploaded_by) AS uploader_name
    FROM documents d LEFT JOIN clients c ON c.id = d.client_id"""


def _row(r: dict | None) -> dict | None:
    if r is None:
        return None
    try:
        r["tags"] = json.loads(r.get("tags") or "[]")
    except ValueError:
        r["tags"] = []
    r.pop("searchable_text", None)  # heavy OCR text never leaves list/get payloads
    return r


def create_document(firm_id: str, *, client_id: str | None, category: str,
                    filename: str, mime_type: str | None, size_bytes: int,
                    uploaded_by: str, retention_until: str | None,
                    version: int = 1, parent_id: str | None = None,
                    invoice_id: str | None = None) -> dict:
    did = new_id()
    execute("""INSERT INTO documents
               (id, firm_id, client_id, invoice_id, category, tags, filename, mime_type,
                size_bytes, uploaded_by, ocr_status, retention_until, is_archived,
                version, parent_id, created_at)
               VALUES (?,?,?,?,?,'[]',?,?,?,?,'pending',?,0,?,?,?)""",
            (did, firm_id, client_id, invoice_id, category, filename, mime_type,
             size_bytes, uploaded_by, retention_until, version, parent_id, now()))
    return get_document(did, firm_id)



def delete_document_record(doc_id: str, firm_id: str) -> None:
    """Remove metadata after an object-storage write failure."""
    execute("DELETE FROM documents WHERE id = ? AND firm_id = ?", (doc_id, firm_id))


def get_document(doc_id: str, firm_id: str) -> dict | None:
    return _row(query_one(_SELECT + " WHERE d.id = ? AND d.firm_id = ?", (doc_id, firm_id)))


def list_documents(firm_id: str, *, client_id: str | None = None, category: str | None = None,
                   q: str | None = None, include_archived: bool = False,
                   accountant_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    where = "WHERE d.firm_id = ?"
    params: list = [firm_id]
    # Latest versions only: a document that has been superseded is a parent of another row
    where += " AND d.id NOT IN (SELECT parent_id FROM documents WHERE parent_id IS NOT NULL)"
    if client_id:
        where += " AND d.client_id = ?"; params.append(client_id)
    if category:
        where += " AND d.category = ?"; params.append(category)
    if not include_archived:
        where += " AND d.is_archived = 0"
    if q:
        where += """ AND (d.filename LIKE ? OR d.searchable_text LIKE ?
                     OR d.tags LIKE ? OR d.ai_classification LIKE ?)"""
        params += [f"%{q}%"] * 4
    if accountant_id:  # accountants: their clients' documents + firm-level documents
        where += """ AND (d.client_id IS NULL OR d.client_id IN
                     (SELECT id FROM clients WHERE firm_id = ? AND assigned_to = ?))"""
        params += [firm_id, accountant_id]

    total = query_one(f"SELECT COUNT(*) AS n FROM documents d {where}", tuple(params))["n"]
    rows = query(f"{_SELECT} {where} ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
                 tuple(params + [limit, offset]))
    return {"total": total, "items": [_row(r) for r in rows], "limit": limit, "offset": offset}


def category_counts(firm_id: str, accountant_id: str | None = None) -> list[dict]:
    where = """WHERE firm_id = ? AND is_archived = 0
               AND id NOT IN (SELECT parent_id FROM documents WHERE parent_id IS NOT NULL)"""
    params: list = [firm_id]
    if accountant_id:
        where += """ AND (client_id IS NULL OR client_id IN
                     (SELECT id FROM clients WHERE firm_id = ? AND assigned_to = ?))"""
        params += [firm_id, accountant_id]
    return query(f"SELECT category, COUNT(*) AS count FROM documents {where} GROUP BY category",
                 tuple(params))


def update_document(doc_id: str, firm_id: str, *, category: str | None = None,
                    tags: list[str] | None = None, client_id: str | None = None,
                    is_archived: bool | None = None) -> None:
    sets, params = [], []
    if category is not None:
        sets.append("category = ?"); params.append(category)
    if tags is not None:
        sets.append("tags = ?"); params.append(json.dumps(tags))
    if client_id is not None:  # empty string = detach from client
        sets.append("client_id = ?"); params.append(client_id or None)
    if is_archived is not None:
        sets.append("is_archived = ?"); params.append(1 if is_archived else 0)
    if sets:
        params += [doc_id, firm_id]
        execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = ? AND firm_id = ?",
                tuple(params))


def set_ocr(doc_id: str, status: str, text: str | None, classification: str | None) -> None:
    execute("""UPDATE documents SET ocr_status = ?, searchable_text = ?, ai_classification = ?
               WHERE id = ?""", (status, text, classification, doc_id))


def version_history(doc_id: str, firm_id: str) -> list[dict]:
    """Walk the parent chain from this document back to version 1 (newest first)."""
    chain: list[dict] = []
    current = get_document(doc_id, firm_id)
    while current is not None and len(chain) < 50:  # bound against cycles
        chain.append(current)
        current = get_document(current["parent_id"], firm_id) if current.get("parent_id") else None
    return chain
