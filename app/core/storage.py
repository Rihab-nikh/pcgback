"""Private binary object storage.

Production uses an S3-compatible bucket (Cloudflare R2 supported).  The API
keeps authorization in FastAPI and proxies bytes from the private bucket, so
objects never need to be public.  No application upload is written to the
container filesystem.
"""
from __future__ import annotations

import mimetypes
import threading
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import quote

from app.core.config import (
    OBJECT_STORAGE_ACCESS_KEY_ID,
    OBJECT_STORAGE_BUCKET,
    OBJECT_STORAGE_ENDPOINT_URL,
    OBJECT_STORAGE_PREFIX,
    OBJECT_STORAGE_REGION,
    OBJECT_STORAGE_SECRET_ACCESS_KEY,
    STORAGE_BACKEND,
)


class StorageError(RuntimeError):
    pass


class StorageNotFound(StorageError):
    pass


def safe_filename(filename: str | None, fallback: str = "file") -> str:
    """Return only a safe basename for an object key / download filename."""
    raw = (filename or fallback).replace("\\", "/")
    name = PurePosixPath(raw).name.strip().replace("\x00", "")
    if name in {"", ".", ".."}:
        return fallback
    return name[:240]


def object_key(kind: str, firm_id: str, entity_id: str, filename: str | None) -> str:
    parts = [p for p in (OBJECT_STORAGE_PREFIX, firm_id, kind, entity_id, safe_filename(filename)) if p]
    return "/".join(str(p).strip("/") for p in parts)


def content_type_for(filename: str | None, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(filename or "")[0] or fallback


def download_headers(filename: str | None) -> dict[str, str]:
    name = safe_filename(filename, "download")
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


class MemoryStorage:
    """Process-local storage used only for deterministic tests/dev."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.RLock()

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        with self._lock:
            self._objects[key] = (bytes(data), content_type or "application/octet-stream")

    def get_bytes(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._objects[key][0]
            except KeyError as e:
                raise StorageNotFound(key) from e

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)


class S3Storage:
    """S3-compatible private storage (including Cloudflare R2)."""

    def __init__(self) -> None:
        if not all((OBJECT_STORAGE_ENDPOINT_URL, OBJECT_STORAGE_BUCKET,
                    OBJECT_STORAGE_ACCESS_KEY_ID, OBJECT_STORAGE_SECRET_ACCESS_KEY)):
            raise StorageError(
                "S3 storage is not configured. Set endpoint/account, bucket, access key and secret key."
            )
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:  # pragma: no cover - dependency/configuration failure
            raise StorageError("boto3 is required for STORAGE_BACKEND=s3") from e

        self.bucket = OBJECT_STORAGE_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=OBJECT_STORAGE_ENDPOINT_URL,
            aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY_ID,
            aws_secret_access_key=OBJECT_STORAGE_SECRET_ACCESS_KEY,
            region_name=OBJECT_STORAGE_REGION,
            config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
        )

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )
        except Exception as e:
            raise StorageError(f"Object upload failed for {key}: {e}") from e

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise StorageNotFound(key) from e
            raise StorageError(f"Object download failed for {key}: {e}") from e

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise StorageError(f"Object existence check failed for {key}: {e}") from e

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            raise StorageError(f"Object delete failed for {key}: {e}") from e


@lru_cache(maxsize=1)
def get_storage():
    if STORAGE_BACKEND == "memory":
        return MemoryStorage()
    if STORAGE_BACKEND == "s3":
        return S3Storage()
    raise StorageError(f"Unsupported storage backend: {STORAGE_BACKEND}")
