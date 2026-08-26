"""Test config: no network, no real API key needed."""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ["ACCOUNTS_OFFLINE"] = "1"
import tempfile
_t = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_PATH", f"{_t}/test.db")
os.environ.setdefault("STORAGE_BACKEND", "memory")

os.environ.setdefault("SUPER_ADMIN_EMAIL", "superadmin@example.com")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "test-super-admin-password-2026")
