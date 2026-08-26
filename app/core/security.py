"""Auth primitives — stdlib only (hashlib PBKDF2 + HMAC-SHA256 JWT).

No external crypto deps means this runs anywhere and is unit-testable
offline. The JWT format is standard HS256, so any client library can
verify/decode the tokens.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from app.core.config import JWT_SECRET

ACCESS_TTL = 60 * 60            # 1h
REFRESH_TTL = 60 * 60 * 24 * 14  # 14d
_PBKDF2_ITERATIONS = 200_000


# ── Passwords ──
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


# ── JWT (HS256) ──
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(sub: str, role: str, firm_id: str | None, token_type: str = "access") -> str:
    ttl = ACCESS_TTL if token_type == "access" else REFRESH_TTL
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": sub, "role": role, "firm_id": firm_id,
        "type": token_type, "exp": int(time.time()) + ttl,
    }).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = _b64(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def decode_token(token: str, expected_type: str = "access") -> dict:
    """Return the payload or raise ValueError (bad signature / expired / wrong type)."""
    try:
        header, payload, sig = token.split(".")
    except ValueError:
        raise ValueError("Malformed token")
    signing_input = f"{header}.{payload}".encode()
    expected = _b64(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid token signature")
    claims = json.loads(_unb64(payload))
    if claims.get("exp", 0) < time.time():
        raise ValueError("Token expired")
    if claims.get("type") != expected_type:
        raise ValueError(f"Expected {expected_type} token")
    return claims
