"""Document acquisition channels: supplier marketplace + email import.

Connections are real, persisted records. The sync itself is a STUB — no
supplier integration exists yet, and sync honestly reports 0 documents
fetched rather than inventing data.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import firm_member
from app.core.db import execute, new_id, now, query, query_one
from app.repositories.system import audit

router = APIRouter(prefix="/connectors", tags=["connectors"])

# Static catalog of suppliers accountants ask for in Morocco.
# key -> (name, category)
CATALOG: list[dict] = [
    {"key": "maroc_telecom", "name": "Maroc Telecom", "category": "telecom", "popular": True},
    {"key": "orange_ma",     "name": "Orange Maroc",  "category": "telecom", "popular": True},
    {"key": "inwi",          "name": "inwi",          "category": "telecom", "popular": True},
    {"key": "onee",          "name": "ONEE (eau & électricité)", "category": "utilities", "popular": True},
    {"key": "lydec",         "name": "Lydec",         "category": "utilities", "popular": False},
    {"key": "redal",         "name": "Redal",         "category": "utilities", "popular": False},
    {"key": "amendis",       "name": "Amendis",       "category": "utilities", "popular": False},
    {"key": "totalenergies", "name": "TotalEnergies Maroc", "category": "carburant", "popular": True},
    {"key": "shell_vivo",    "name": "Vivo Energy (Shell)", "category": "carburant", "popular": False},
    {"key": "afriquia",      "name": "Afriquia",      "category": "carburant", "popular": False},
    {"key": "ram",           "name": "Royal Air Maroc", "category": "voyage", "popular": False},
    {"key": "oncf",          "name": "ONCF",          "category": "voyage", "popular": False},
    {"key": "ctm",           "name": "CTM",           "category": "voyage", "popular": False},
    {"key": "amazon",        "name": "Amazon",        "category": "ecommerce", "popular": True},
    {"key": "jumia",         "name": "Jumia",         "category": "ecommerce", "popular": False},
    {"key": "microsoft",     "name": "Microsoft 365", "category": "software", "popular": True},
    {"key": "google",        "name": "Google Workspace", "category": "software", "popular": False},
    {"key": "ovh",           "name": "OVHcloud",      "category": "software", "popular": False},
]
_BY_KEY = {c["key"]: c for c in CATALOG}
CATEGORIES = sorted({c["category"] for c in CATALOG})


def _connections(firm_id: str) -> dict[str, dict]:
    return {r["supplier_key"]: r for r in
            query("SELECT * FROM supplier_connections WHERE firm_id = ?", (firm_id,))}


@router.get("/catalog")
def catalog(q: str | None = None, category: str | None = None,
            user: dict = Depends(firm_member)):
    conns = _connections(user["firm_id"])
    items = []
    for c in CATALOG:
        if q and q.lower() not in c["name"].lower():
            continue
        if category and c["category"] != category:
            continue
        conn = conns.get(c["key"])
        items.append(c | {"connected": conn is not None,
                          "last_sync_at": conn["last_sync_at"] if conn else None})
    return {"categories": CATEGORIES, "suppliers": items,
            "connected_count": len(conns)}


@router.post("/{supplier_key}/connect", status_code=201)
def connect(supplier_key: str, user: dict = Depends(firm_member)):
    if supplier_key not in _BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown supplier")
    if query_one("SELECT 1 FROM supplier_connections WHERE firm_id = ? AND supplier_key = ?",
                 (user["firm_id"], supplier_key)):
        raise HTTPException(status_code=409, detail="Already connected")
    execute("""INSERT INTO supplier_connections (id, firm_id, supplier_key, connected_by, created_at)
               VALUES (?,?,?,?,?)""",
            (new_id(), user["firm_id"], supplier_key, user["id"], now()))
    audit("connector.connect", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="connector", entity_id=supplier_key)
    return {"supplier_key": supplier_key, "connected": True}


@router.delete("/{supplier_key}", status_code=204)
def disconnect(supplier_key: str, user: dict = Depends(firm_member)):
    execute("DELETE FROM supplier_connections WHERE firm_id = ? AND supplier_key = ?",
            (user["firm_id"], supplier_key))
    audit("connector.disconnect", user_id=user["id"], firm_id=user["firm_id"],
          entity_type="connector", entity_id=supplier_key)


@router.post("/{supplier_key}/sync")
def sync(supplier_key: str, user: dict = Depends(firm_member)):
    """STUB: records the sync attempt; no supplier integration exists yet,
    so it truthfully reports zero fetched documents."""
    if not query_one("SELECT 1 FROM supplier_connections WHERE firm_id = ? AND supplier_key = ?",
                     (user["firm_id"], supplier_key)):
        raise HTTPException(status_code=404, detail="Supplier not connected")
    execute("UPDATE supplier_connections SET last_sync_at = ? WHERE firm_id = ? AND supplier_key = ?",
            (now(), user["firm_id"], supplier_key))
    return {"supplier_key": supplier_key, "fetched": 0,
            "message": "Synchronisation enregistrée — l'intégration fournisseur "
                       "n'est pas encore active, aucun document récupéré."}


# ── Email import ──
@router.get("/email-import")
def email_import(user: dict = Depends(firm_member)):
    """Dedicated forwarding address per firm. STUB: the inbound mail service
    is not connected yet; the address is stable and reserved for this firm."""
    firm_id = user["firm_id"]
    address = f"docs-{firm_id[:10]}@inbox.pcgmaroc.ai"
    multi_address = f"docs-{firm_id[:10]}+multi@inbox.pcgmaroc.ai"
    # History: invoices whose filename marks them as email-imported (none until live).
    history = query("""SELECT id, filename, supplier_name, status, created_at FROM invoices
                       WHERE firm_id = ? AND filename LIKE 'email_%'
                       ORDER BY created_at DESC LIMIT 20""", (firm_id,))
    return {"address": address, "multi_address": multi_address,
            "active": False, "history": history}
