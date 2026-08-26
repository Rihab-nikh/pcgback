"""Roles & permissions matrix.

Defaults live in code (PERMISSION_DEFAULTS); a firm's toggles are stored as
overrides in role_permissions. firm_admin is the owner role: it always has
every permission and cannot be toggled off.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_permission, user_admin
from app.api.schemas import PermissionToggleRequest
from app.core.db import execute, query
from app.repositories.system import audit

router = APIRouter(prefix="/permissions", tags=["permissions"])

ROLES = ["business_admin", "firm_admin", "accountant", "reviewer", "employee"]

# permission -> roles allowed by default
PERMISSION_DEFAULTS: dict[str, set[str]] = {
    "users.manage":      {"business_admin", "firm_admin"},
    "permissions.manage": {"business_admin", "firm_admin"},
    "clients.manage":    {"business_admin", "firm_admin"},
    "invoices.upload":   {"business_admin", "firm_admin", "accountant"},
    "invoices.review":   {"business_admin", "firm_admin", "accountant", "reviewer"},
    "journal.view":      {"business_admin", "firm_admin", "accountant", "reviewer"},
    "reports.view":      {"business_admin", "firm_admin", "accountant", "reviewer"},
    "treasury.manage":   {"business_admin", "firm_admin", "accountant"},
    "expenses.submit":   set(ROLES),
    "expenses.approve":  {"business_admin", "firm_admin", "reviewer"},
}
PERMISSIONS = list(PERMISSION_DEFAULTS)


def matrix_for_firm(firm_id: str) -> dict[str, dict[str, bool]]:
    overrides = {(r["role"], r["permission"]): bool(r["allowed"])
                 for r in query("SELECT role, permission, allowed FROM role_permissions WHERE firm_id = ?",
                                (firm_id,))}
    return {role: {perm: (True if role == "firm_admin"
                          else overrides.get((role, perm), role in PERMISSION_DEFAULTS[perm]))
                   for perm in PERMISSIONS}
            for role in ROLES}


def has_permission(firm_id: str, role: str, permission: str) -> bool:
    """Enforcement helper for other routers."""
    if role == "firm_admin":
        return True
    return matrix_for_firm(firm_id).get(role, {}).get(permission, False)


@router.get("")
def get_matrix(admin: dict = Depends(user_admin)):
    # Viewing stays role-based so the Users page can derive per-role columns.
    return {"roles": ROLES, "permissions": PERMISSIONS,
            "matrix": matrix_for_firm(admin["firm_id"])}


@router.put("")
def toggle(body: PermissionToggleRequest,
           admin: dict = Depends(require_permission("permissions.manage"))):
    if body.permission not in PERMISSION_DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown permission")
    if body.role == "firm_admin":
        raise HTTPException(status_code=400, detail="firm_admin permissions cannot be changed")
    execute("""INSERT INTO role_permissions (firm_id, role, permission, allowed) VALUES (?,?,?,?)
               ON CONFLICT (firm_id, role, permission) DO UPDATE SET allowed = excluded.allowed""",
            (admin["firm_id"], body.role, body.permission, 1 if body.allowed else 0))
    audit("permission.toggle", user_id=admin["id"], firm_id=admin["firm_id"],
          entity_type="permission", entity_id=f"{body.role}:{body.permission}",
          detail="on" if body.allowed else "off")
    return {"role": body.role, "permission": body.permission, "allowed": body.allowed}
