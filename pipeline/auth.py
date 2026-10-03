"""Targeto Lead Pipeline — auth layer.

Session-based login (Flask signed cookies), PBKDF2-SHA256 password hashes
(stdlib only, no extra deps), role decorators. First run auto-creates the
admin account from TARGETO_ADMIN_USER/TARGETO_ADMIN_PASS (defaults
saroj / change-me-now — CHANGE IT).
"""
import hashlib
import hmac
import os
import secrets

from functools import wraps
from flask import redirect, request, session

from . import get_conn

SESSION_COOKIE = "targeto_session"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, ref = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(dk.hex(), ref)
    except Exception:
        return False


def ensure_admin():
    """Create the bootstrap admin if no users exist at all."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM users")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (%s,%s,TRUE)",
                (os.environ.get("TARGETO_ADMIN_USER", "saroj"),
                 hash_password(os.environ.get("TARGETO_ADMIN_PASS", "change-me-now"))))
            conn.commit()
    finally:
        conn.close()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, is_admin FROM users WHERE id = %s AND active", (uid,))
            row = cur.fetchone()
        return {"id": row[0], "username": row[1], "is_admin": row[2]} if row else None
    finally:
        conn.close()


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                from flask import jsonify
                return jsonify(ok=False, error="login required"), 401
            return redirect("/login?next=" + request.path)
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return redirect("/login?next=" + request.path)
        if not u["is_admin"]:
            from flask import abort
            return abort(403)
        return fn(*args, **kwargs)
    return wrapper
