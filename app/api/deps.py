"""Route dependencies: authentication, role guards, tenant scoping.

The isolation model:
- super_admin: platform endpoints only, no firm data access.
- firm_admin:  everything within their own firm_id.
- accountant:  firm endpoints, but client/invoice queries are additionally
               restricted to clients assigned to them.
"""
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token
from app.core.config import ACCESS_COOKIE_NAME
from app.repositories import users as users_repo

bearer = HTTPBearer(auto_error=False)


def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    token = creds.credentials if creds is not None else request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    try:
        claims = decode_token(token, expected_type="access")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = users_repo.get_user(claims["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="User not found or deactivated")
    if user["firm_id"]:
        firm = users_repo.get_firm(user["firm_id"])
        if not firm or not firm["is_active"]:
            raise HTTPException(status_code=403, detail="Firm is suspended")
    return user


def require_roles(*roles: str):
    def guard(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail=f"Requires role: {' or '.join(roles)}")
        return user
    return guard


FIRM_ROLES = ("business_admin", "firm_admin", "accountant", "reviewer", "employee")

firm_member = require_roles("firm_admin", "accountant")
firm_admin_only = require_roles("firm_admin")
super_admin_only = require_roles("super_admin")
# v7: user administration is open to business_admin too; expenses to every firm role.
user_admin = require_roles("firm_admin", "business_admin")
firm_any = require_roles(*FIRM_ROLES)


def require_permission(permission: str):
    """Guard driven by the firm's permission matrix (role_permissions table).
    firm_admin always passes; other roles pass if the matrix allows them —
    so toggling a permission in the UI actually changes authorization."""
    def guard(user: dict = Depends(current_user)) -> dict:
        if user["role"] == "super_admin":
            raise HTTPException(status_code=403, detail="Platform accounts have no firm data access")
        from app.api.routers.permissions import has_permission  # late import: router imports deps
        if not has_permission(user["firm_id"], user["role"], permission):
            raise HTTPException(status_code=403, detail=f"Requires permission: {permission}")
        return user
    return guard


def accountant_scope(user: dict) -> str | None:
    """Extra filter for accountants: only their assigned clients."""
    return user["id"] if user["role"] == "accountant" else None
