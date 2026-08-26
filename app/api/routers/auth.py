"""Auth: firm signup, login, token refresh, current user.

Login is rate-limited (in-memory sliding window): after MAX_FAILURES failed
attempts per email+IP within WINDOW_SECONDS, further attempts get 429.
Every failure is written to the audit log (auth.login_failed).
"""
import time as _time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.deps import current_user
from app.api.schemas import LoginRequest, RefreshRequest, RegisterFirmRequest
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.core.config import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, COOKIE_SECURE, COOKIE_SAMESITE
from app.repositories import users as users_repo
from app.repositories.system import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "email", "full_name", "role", "firm_id", "last_login_at")}


def _token_pair(u: dict) -> dict:
    return {
        "access_token": create_token(u["id"], u["role"], u["firm_id"], "access"),
        "refresh_token": create_token(u["id"], u["role"], u["firm_id"], "refresh"),
        "token_type": "bearer",
        "user": _public_user(u),
    }


def _set_auth_cookies(response: Response, tokens: dict) -> None:
    # Access is short-lived; refresh is narrower and never readable by JS.
    response.set_cookie(ACCESS_COOKIE_NAME, tokens["access_token"], httponly=True, secure=COOKIE_SECURE,
                        samesite=COOKIE_SAMESITE, path="/", max_age=15 * 60)
    response.set_cookie(REFRESH_COOKIE_NAME, tokens["refresh_token"], httponly=True, secure=COOKIE_SECURE,
                        samesite="strict", path="/auth", max_age=30 * 24 * 3600)


def _issue(u: dict, response: Response) -> dict:
    tokens = _token_pair(u)
    _set_auth_cookies(response, tokens)
    return tokens


@router.post("/register-firm", status_code=201)
def register_firm(body: RegisterFirmRequest, response: Response):
    """Create an accounting firm plus its first firm_admin user."""
    if users_repo.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    firm = users_repo.create_firm(body.firm_name,
                                  accounting_software=body.accounting_software,
                                  country=body.country.upper(), currency=body.currency.upper(),
                                  logo=body.logo)
    user = users_repo.create_user(body.email, hash_password(body.password),
                                  body.full_name, "firm_admin", firm["id"])
    audit("firm.register", user_id=user["id"], firm_id=firm["id"],
          entity_type="firm", entity_id=firm["id"], detail=body.firm_name)
    return _issue(user, response)


# ── Login rate limiting (in-memory; per-process — move to Redis with scale-out) ──
MAX_FAILURES = 5
WINDOW_SECONDS = 60
_failures: dict[str, list[float]] = defaultdict(list)


def _rate_key(email: str, request: Request) -> str:
    ip = request.client.host if request.client else "?"
    return f"{email.lower().strip()}|{ip}"


def _is_rate_limited(key: str) -> bool:
    cutoff = _time.monotonic() - WINDOW_SECONDS
    _failures[key] = [t for t in _failures[key] if t > cutoff]
    return len(_failures[key]) >= MAX_FAILURES


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    key = _rate_key(body.email, request)
    if _is_rate_limited(key):
        audit("auth.rate_limited", user_id=None, firm_id=None, detail=body.email)
        raise HTTPException(status_code=429,
                            detail="Too many failed attempts — try again in a minute")
    user = users_repo.get_user_by_email(body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        _failures[key].append(_time.monotonic())
        audit("auth.login_failed", user_id=user["id"] if user else None,
              firm_id=user["firm_id"] if user else None, detail=body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account deactivated")
    _failures.pop(key, None)  # success clears the window
    users_repo.touch_login(user["id"])
    audit("auth.login", user_id=user["id"], firm_id=user["firm_id"])
    return _issue(user, response)


@router.post("/refresh")
def refresh(body: RefreshRequest, request: Request, response: Response):
    token = body.refresh_token or request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    try:
        claims = decode_token(token, expected_type="refresh")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = users_repo.get_user(claims["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="User not found or deactivated")
    return _issue(user, response)


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth")


@router.get("/me")
def me(user: dict = Depends(current_user)):
    out = _public_user(user)
    if user["firm_id"]:
        out["firm"] = users_repo.get_firm(user["firm_id"])
    return out
