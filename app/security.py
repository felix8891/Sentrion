from __future__ import annotations
import hashlib, hmac, secrets
from typing import Optional
from starlette.requests import Request
from .db import get_db

ITERATIONS = 310_000

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${derived.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split('$')
        if scheme != 'pbkdf2_sha256': return False
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt_hex), int(iterations)).hex()
        return hmac.compare_digest(derived, digest_hex)
    except Exception:
        return False

def current_user_from_request(request: Request) -> Optional[dict]:
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute('SELECT id, name, email, role, is_active FROM users WHERE id=?', (user_id,)).fetchone()
    if not row or not row['is_active']:
        request.session.clear()
        return None
    return dict(row)

def ensure_csrf(request: Request) -> str:
    token = request.session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        request.session['csrf_token'] = token
    return token

def verify_csrf(request: Request, submitted: str | None) -> bool:
    token = request.session.get('csrf_token', '')
    return bool(submitted and token and hmac.compare_digest(str(submitted), str(token)))

def require_roles(user: dict, *roles: str) -> bool:
    return user.get('role') in roles

def mask_account(account: str | None) -> str:
    if not account:
        return 'Not available'
    s = str(account).strip()
    if len(s) <= 4:
        return '•' * len(s)
    return ('•' * max(4, len(s) - 4)) + s[-4:]
