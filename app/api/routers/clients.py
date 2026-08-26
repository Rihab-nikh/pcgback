"""Clients: CRUD, assignment, per-client summary. Accountants see only assigned clients."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import accountant_scope, firm_member, require_permission

manage_clients = require_permission("clients.manage")
from app.api.schemas import ClientCreateRequest, ClientUpdateRequest
from app.repositories import clients as clients_repo
from app.repositories.invoices import firm_stats, list_invoices
from app.repositories.system import audit
from app.services.health import client_health

router = APIRouter(prefix="/clients", tags=["clients"])


def _get_visible_client(client_id: str, user: dict) -> dict:
    client = clients_repo.get_client(client_id, user["firm_id"])
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if user["role"] == "accountant" and client["assigned_to"] != user["id"]:
        raise HTTPException(status_code=404, detail="Client not found")  # don't reveal existence
    return client


@router.get("")
def list_clients(q: str | None = None, include_archived: bool = False,
                 user: dict = Depends(firm_member)):
    return clients_repo.list_clients(user["firm_id"], assigned_to=accountant_scope(user),
                                     include_archived=include_archived, q=q)


@router.post("", status_code=201)
def create_client(body: ClientCreateRequest, admin: dict = Depends(manage_clients)):
    client = clients_repo.create_client(admin["firm_id"], body.name, body.ice,
                                        body.if_number, body.address, body.assigned_to)
    audit("client.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="client", entity_id=client["id"], detail=body.name)
    return client


@router.get("/{client_id}")
def get_client(client_id: str, user: dict = Depends(firm_member)):
    return _get_visible_client(client_id, user)


@router.patch("/{client_id}")
def update_client(client_id: str, body: ClientUpdateRequest, admin: dict = Depends(manage_clients)):
    _get_visible_client(client_id, admin)
    clients_repo.update_client(client_id, admin["firm_id"], **body.model_dump(exclude_none=True))
    audit("client.update", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="client", entity_id=client_id)
    return clients_repo.get_client(client_id, admin["firm_id"])


@router.get("/{client_id}/summary")
def client_summary(client_id: str, user: dict = Depends(firm_member)):
    """Client workspace header: identity + stats + recent invoices."""
    client = _get_visible_client(client_id, user)
    recent = list_invoices(user["firm_id"], client_id=client_id, limit=10)
    stats = firm_stats(user["firm_id"])
    return {"client": client, "recent_invoices": recent["items"],
            "invoice_total": recent["total"], "firm_context": stats,
            "health": client_health(user["firm_id"], client_id)}
