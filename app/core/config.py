"""Central configuration. All environment access lives here.

PostgreSQL is the production database. SQLite is accepted only when explicitly
selected for deterministic tests or one-shot legacy migration tooling.
"""
import os
import secrets
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

APP_ENV: str = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION: bool = APP_ENV in {"production", "prod"}

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
EXTRACTION_MODEL: str = os.environ.get("EXTRACTION_MODEL", "gpt-4o")

ACCOUNT_SEARCH_BASE_URL: str = os.environ.get("ACCOUNT_SEARCH_URL", "http://localhost:8001")
ACCOUNTS_OFFLINE: bool = os.environ.get("ACCOUNTS_OFFLINE", "0") == "1"

# PostgreSQL is the default runtime. DATABASE_PATH remains a compatibility hook
# for the test suite and the sqlite->postgres migration command only.
_raw_database_url = os.environ.get("DATABASE_URL", "").strip()
_raw_database_path = os.environ.get("DATABASE_PATH", "").strip()
if _raw_database_url.startswith("postgres://"):
    _raw_database_url = "postgresql://" + _raw_database_url[len("postgres://"):]
if not _raw_database_url:
    if _raw_database_path:
        DATABASE_URL = f"sqlite:///{os.path.abspath(_raw_database_path)}"
    else:
        DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/pcg_maroc_ai"
else:
    DATABASE_URL = _raw_database_url

DATABASE_BACKEND = "sqlite" if DATABASE_URL.startswith("sqlite:///") else "postgresql"
DATABASE_PATH: str | None = (
    DATABASE_URL[len("sqlite:///"):] if DATABASE_BACKEND == "sqlite" else None
)
if IS_PRODUCTION and DATABASE_BACKEND != "postgresql":
    raise RuntimeError("Production requires PostgreSQL; SQLite is not supported in production")

# Binary uploads are never persisted on the application filesystem.
# Production uses an S3-compatible object store (Cloudflare R2 supported).
# Tests/explicit local development may use an in-memory backend.
_default_storage_backend = "memory" if DATABASE_BACKEND == "sqlite" else "s3"
STORAGE_BACKEND: str = os.environ.get("STORAGE_BACKEND", _default_storage_backend).strip().lower()
if STORAGE_BACKEND not in {"memory", "s3"}:
    raise RuntimeError("STORAGE_BACKEND must be 'memory' or 's3'")

R2_ACCOUNT_ID: str = os.environ.get("R2_ACCOUNT_ID", "").strip()
OBJECT_STORAGE_ENDPOINT_URL: str = os.environ.get("OBJECT_STORAGE_ENDPOINT_URL", "").strip()
if not OBJECT_STORAGE_ENDPOINT_URL and R2_ACCOUNT_ID:
    OBJECT_STORAGE_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
OBJECT_STORAGE_BUCKET: str = (
    os.environ.get("OBJECT_STORAGE_BUCKET", "").strip()
    or os.environ.get("R2_BUCKET", "").strip()
)
OBJECT_STORAGE_ACCESS_KEY_ID: str = (
    os.environ.get("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    or os.environ.get("R2_ACCESS_KEY_ID", "").strip()
)
OBJECT_STORAGE_SECRET_ACCESS_KEY: str = (
    os.environ.get("OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    or os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
)
OBJECT_STORAGE_REGION: str = os.environ.get("OBJECT_STORAGE_REGION", "auto").strip() or "auto"
OBJECT_STORAGE_PREFIX: str = os.environ.get("OBJECT_STORAGE_PREFIX", "pcg").strip().strip("/")

if IS_PRODUCTION:
    if STORAGE_BACKEND != "s3":
        raise RuntimeError("Production requires STORAGE_BACKEND=s3; local filesystem storage is not supported")
    missing_storage = [name for name, value in {
        "OBJECT_STORAGE_ENDPOINT_URL/R2_ACCOUNT_ID": OBJECT_STORAGE_ENDPOINT_URL,
        "OBJECT_STORAGE_BUCKET/R2_BUCKET": OBJECT_STORAGE_BUCKET,
        "OBJECT_STORAGE_ACCESS_KEY_ID/R2_ACCESS_KEY_ID": OBJECT_STORAGE_ACCESS_KEY_ID,
        "OBJECT_STORAGE_SECRET_ACCESS_KEY/R2_SECRET_ACCESS_KEY": OBJECT_STORAGE_SECRET_ACCESS_KEY,
    }.items() if not value]
    if missing_storage:
        raise RuntimeError("Production object storage is incomplete: " + ", ".join(missing_storage))

_raw_jwt = os.environ.get("JWT_SECRET", "")
if IS_PRODUCTION:
    if len(_raw_jwt) < 32 or _raw_jwt in {"dev-secret-change-me", "change-me"}:
        raise RuntimeError("Production JWT_SECRET must be a non-default secret of at least 32 characters")
    JWT_SECRET = _raw_jwt
else:
    JWT_SECRET = _raw_jwt or secrets.token_urlsafe(48)

SUPER_ADMIN_EMAIL: str = os.environ.get("SUPER_ADMIN_EMAIL", "")
SUPER_ADMIN_PASSWORD: str = os.environ.get("SUPER_ADMIN_PASSWORD", "")
if IS_PRODUCTION and (not SUPER_ADMIN_EMAIL or len(SUPER_ADMIN_PASSWORD) < 14 or SUPER_ADMIN_PASSWORD == "change-me-now"):
    raise RuntimeError("Production bootstrap admin credentials must be explicitly configured with a strong password")

ENABLE_AUTO_PUBLISH: bool = os.environ.get("ENABLE_AUTO_PUBLISH", "0") == "1"
ACCOUNTING_ENGINE_CERTIFIED: bool = os.environ.get("ACCOUNTING_ENGINE_CERTIFIED", "0") == "1"

COOKIE_SECURE: bool = IS_PRODUCTION or os.environ.get("COOKIE_SECURE", "0") == "1"
COOKIE_SAMESITE: str = os.environ.get("COOKIE_SAMESITE", "lax")
ACCESS_COOKIE_NAME: str = os.environ.get("ACCESS_COOKIE_NAME", "pcg_access")
REFRESH_COOKIE_NAME: str = os.environ.get("REFRESH_COOKIE_NAME", "pcg_refresh")

CORS_ORIGINS: list[str] = [x.strip() for x in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if x.strip()]
