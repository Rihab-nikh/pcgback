"""Platform administration (super admin) and firm team management (firm admin)."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import firm_admin_only, require_permission, super_admin_only
from app.api.schemas import (AssignClientsRequest, BulkInviteRequest,
                             CreateAccountantRequest, UpdateUserRequest)
from app.core.security import hash_password
from app.repositories import clients as clients_repo
from app.repositories import users as users_repo
from app.repositories.invoices import platform_stats
from app.repositories.system import audit, list_audit

manage_users = require_permission("users.manage")

platform = APIRouter(prefix="/platform", tags=["platform"])
team = APIRouter(prefix="/team", tags=["team"])


# ── Super admin: cross-tenant aggregates, never invoice contents ──
@platform.get("/stats")
def stats(_: dict = Depends(super_admin_only)):
    return platform_stats()


@platform.get("/firms")
def firms(_: dict = Depends(super_admin_only)):
    return users_repo.list_firms()


@platform.patch("/firms/{firm_id}")
def set_firm_status(firm_id: str, active: bool, admin: dict = Depends(super_admin_only)):
    if not users_repo.get_firm(firm_id):
        raise HTTPException(status_code=404, detail="Firm not found")
    users_repo.set_firm_active(firm_id, active)
    audit("firm.suspend" if not active else "firm.activate",
          user_id=admin["id"], firm_id=firm_id, entity_type="firm", entity_id=firm_id)
    return {"id": firm_id, "is_active": active}


@platform.get("/audit")
def platform_audit(limit: int = 100, _: dict = Depends(super_admin_only)):
    return list_audit(None, limit=min(limit, 500))


# ── Firm admin / business admin: users ──
@team.get("/accountants")
def list_accountants(admin: dict = Depends(manage_users)):
    return users_repo.list_firm_users(admin["firm_id"])


@team.post("/accountants", status_code=201)
def create_accountant(body: CreateAccountantRequest, admin: dict = Depends(manage_users)):
    """Invite a single user (any firm role)."""
    if users_repo.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    # Validate client assignments before creating the user (all-or-nothing)
    for client_id in body.client_ids:
        if not clients_repo.get_client(client_id, admin["firm_id"]):
            raise HTTPException(status_code=404, detail=f"Client not found: {client_id}")
    user = users_repo.create_user(body.email, hash_password(body.password),
                                  body.full_name, body.role, admin["firm_id"],
                                  department=body.department, phone=body.phone)
    for client_id in body.client_ids:
        clients_repo.update_client(client_id, admin["firm_id"], assigned_to=user["id"])
        audit("client.assign", user_id=admin["id"], firm_id=admin["firm_id"],
              entity_type="client", entity_id=client_id, detail=user["id"])
    audit("user.create", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="user", entity_id=user["id"], detail=body.role)
    return {k: user[k] for k in ("id", "email", "full_name", "role", "department", "is_active")}


@team.post("/invite-bulk", status_code=207)
def invite_bulk(body: BulkInviteRequest, admin: dict = Depends(manage_users)):
    """Invite many users at once; each row succeeds or fails independently."""
    results = []
    for u in body.users:
        if users_repo.get_user_by_email(u.email):
            results.append({"email": u.email, "created": False, "error": "Email already registered"})
            continue
        user = users_repo.create_user(u.email, hash_password(u.password),
                                      u.full_name, u.role, admin["firm_id"],
                                      department=u.department, phone=u.phone)
        audit("user.create", user_id=admin["id"], firm_id=admin["firm_id"],
              entity_type="user", entity_id=user["id"], detail=f"bulk:{u.role}")
        results.append({"email": u.email, "created": True, "id": user["id"]})
    return {"invited": sum(1 for r in results if r["created"]), "results": results}


@team.patch("/accountants/{user_id}")
def update_accountant(user_id: str, body: UpdateUserRequest, admin: dict = Depends(manage_users)):
    target = users_repo.get_user(user_id)
    if not target or target["firm_id"] != admin["firm_id"]:
        raise HTTPException(status_code=404, detail="User not found")   # cross-tenant => 404, never 403
    if target["id"] == admin["id"] and body.is_active is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
    users_repo.update_user(user_id, admin["firm_id"], full_name=body.full_name,
                           role=body.role, is_active=body.is_active,
                           department=body.department)
    audit("user.update", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="user", entity_id=user_id)
    return users_repo.get_user(user_id) | {"password_hash": "***"}


@team.get("/accountants/{user_id}/clients")
def accountant_clients(user_id: str, admin: dict = Depends(firm_admin_only)):
    """Client ids currently assigned to this accountant."""
    target = users_repo.get_user(user_id)
    if not target or target["firm_id"] != admin["firm_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    return [c["id"] for c in clients_repo.list_clients(admin["firm_id"], assigned_to=user_id)]


@team.put("/accountants/{user_id}/clients")
def assign_clients(user_id: str, body: AssignClientsRequest, admin: dict = Depends(firm_admin_only)):
    """Replace the set of clients assigned to this accountant; omitted ones are unassigned."""
    target = users_repo.get_user(user_id)
    if not target or target["firm_id"] != admin["firm_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    for client_id in body.client_ids:
        if not clients_repo.get_client(client_id, admin["firm_id"]):
            raise HTTPException(status_code=404, detail=f"Client not found: {client_id}")
    clients_repo.set_assigned_clients(admin["firm_id"], user_id, body.client_ids)
    audit("client.assign", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="user", entity_id=user_id, detail=",".join(body.client_ids) or "none")
    return {"user_id": user_id, "client_ids": body.client_ids}
