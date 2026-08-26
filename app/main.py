"""FastAPI app: wires the pipeline extract -> compute -> journal -> validate -> report."""
import base64
import os
import sys
from contextlib import asynccontextmanager

import httpx

# Force asyncio event loop - patch uvicorn before importing codewords_client
os.environ["UVICORN_LOOP"] = "asyncio"
sys.modules["uvloop"] = None  # prevent uvloop from being imported

from fastapi import FastAPI, File, HTTPException, UploadFile

from app.core.config import ACCOUNT_SEARCH_BASE_URL, CORS_ORIGINS
from app.core.logging import logger
from app.main_pipeline import process_invoice_pipeline
from app.models.invoice import InvoiceRequest, InvoiceResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup: init DB, seed super admin, check account search service."""
    from app.core.config import SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD
    from app.core.db import init_db
    from app.core.security import hash_password
    from app.repositories import users as users_repo

    init_db()
    if SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD and not users_repo.get_user_by_email(SUPER_ADMIN_EMAIL):
        users_repo.create_user(SUPER_ADMIN_EMAIL, hash_password(SUPER_ADMIN_PASSWORD),
                               "Platform Admin", "super_admin", None)
        logger.info("Super admin seeded", email=SUPER_ADMIN_EMAIL)
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{ACCOUNT_SEARCH_BASE_URL}/docs")
            resp.raise_for_status()
        logger.info("Account search service reachable", url=ACCOUNT_SEARCH_BASE_URL)
    except Exception as e:
        logger.warning(
            "Account search service unreachable at startup",
            url=ACCOUNT_SEARCH_BASE_URL,
            error=str(e),
        )
    yield


app = FastAPI(
    title="PCG Marocain Accounting Engine",
    description="Extracts invoice data from images and produces exam-ready journal entries following Moroccan PCG rules.",
    version="2.0.0",
    lifespan=lifespan,
)

# ── v2 API ──
from app.api.routers.admin import platform as platform_router, team as team_router  # noqa: E402
from app.api.routers.auth import router as auth_router  # noqa: E402
from app.api.routers.clients import router as clients_router  # noqa: E402
from app.api.routers.invoices import router as invoices_router  # noqa: E402
from app.api.routers.assistant import router as assistant_router  # noqa: E402
from app.api.routers.documents import router as documents_router  # noqa: E402
from app.api.routers.feedback import router as feedback_router  # noqa: E402
from app.api.routers.reporting import router as reporting_router  # noqa: E402
from app.api.routers.treasury import router as treasury_router  # noqa: E402
from app.api.routers.permissions import router as permissions_router  # noqa: E402
from app.api.routers.expenses import router as expenses_router  # noqa: E402
from app.api.routers.connectors import router as connectors_router  # noqa: E402
from app.api.routers.rules import router as rules_router  # noqa: E402
from app.api.routers.admin_center import router as admin_center_router  # noqa: E402
from app.api.routers.approvals import router as approvals_router  # noqa: E402
from app.api.routers.lettrage import router as lettrage_router  # noqa: E402
from app.api.routers.aging import router as aging_router  # noqa: E402
from app.api.routers.account_review import router as account_review_router  # noqa: E402
from app.api.routers.od import router as od_router  # noqa: E402
from app.api.routers.periods import router as periods_router  # noqa: E402

for r in (auth_router, platform_router, team_router, clients_router, invoices_router,
          documents_router, treasury_router, reporting_router, assistant_router, feedback_router,
          permissions_router, expenses_router, connectors_router, rules_router, admin_center_router,
          approvals_router, lettrage_router, aging_router, account_review_router, od_router, periods_router):
    app.include_router(r)

# ── Frontend (single-file app served same-origin at /app) ──
from pathlib import Path as _Path  # noqa: E402

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])  # for separate frontend dev servers

_INDEX = _Path(__file__).parent / "static" / "index.html"


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def frontend():
    return _INDEX.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


@app.post("/", response_model=InvoiceResponse)
async def process_invoice_upload(file: UploadFile = File(...)):
    """Process an invoice image file (drag-and-drop or file picker in Swagger)."""
    try:
        logger.info("STEPLOG START file_upload", filename=file.filename)
        contents = await file.read()
        image_data_uri = f"data:{file.content_type or 'image/jpeg'};base64,{base64.b64encode(contents).decode('utf-8')}"
        logger.info("STEPLOG END file_upload", size=len(contents))
        return await process_invoice_pipeline(image_data_uri)
    except Exception as e:
        logger.error("Invoice processing error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Invoice processing failed: {str(e)}")


@app.post("/process-invoice-json", response_model=InvoiceResponse)
async def process_invoice(request: InvoiceRequest):
    """Process an invoice from JSON body (for API integrations)."""
    try:
        return await process_invoice_pipeline(request.invoice_image)
    except Exception as e:
        logger.error("Invoice processing error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Invoice processing failed: {str(e)}")
