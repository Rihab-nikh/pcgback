from pathlib import Path

import pytest

from app.core.storage import (
    MemoryStorage,
    StorageNotFound,
    content_type_for,
    download_headers,
    object_key,
    safe_filename,
)


def test_safe_filename_blocks_path_traversal_and_windows_paths():
    assert safe_filename("../../secret.pdf") == "secret.pdf"
    assert safe_filename(r"C:\\fake\\invoice.jpg") == "invoice.jpg"
    assert safe_filename("", "invoice") == "invoice"


def test_object_key_is_tenant_and_entity_scoped():
    a = object_key("invoices", "firm-a", "inv-1", "same.pdf")
    b = object_key("invoices", "firm-b", "inv-1", "same.pdf")
    assert a != b
    assert "/firm-a/invoices/inv-1/same.pdf" in f"/{a}"


def test_memory_storage_roundtrip_delete_and_missing():
    storage = MemoryStorage()
    storage.put_bytes("a/b.pdf", b"invoice", content_type="application/pdf")
    assert storage.exists("a/b.pdf")
    assert storage.get_bytes("a/b.pdf") == b"invoice"
    storage.delete("a/b.pdf")
    assert not storage.exists("a/b.pdf")
    with pytest.raises(StorageNotFound):
        storage.get_bytes("a/b.pdf")


def test_download_metadata_helpers():
    assert content_type_for("x.pdf") == "application/pdf"
    assert "filename*=UTF-8''" in download_headers("facture été.pdf")["Content-Disposition"]


def test_upload_routers_do_not_persist_files_locally():
    root = Path(__file__).parents[1] / "app" / "api" / "routers"
    for name in ("invoices.py", "documents.py", "expenses.py", "od.py", "account_review.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "STORAGE_DIR" not in source
        assert ".write_bytes(" not in source
        assert "FileResponse" not in source
