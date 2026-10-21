"""Email + password sessions with four roles: owner > admin > editor > viewer."""
import hashlib
import hmac
import os
import secrets as pysecrets
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from .db import SessionLocal, User

RANK = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000).hex(), dk)
    except ValueError:
        return False


def new_token() -> str:
    return pysecrets.token_urlsafe(24)


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    with SessionLocal() as s:
        u = s.get(User, uid)
        return u if u and u.active else None


def need(role: str):
    def dep(request: Request) -> User:
        u = current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first")
        if RANK[u.role] < RANK[role]:
            raise HTTPException(403, f"Needs {role} access")
        return u
    return dep


def login(email: str, pw: str) -> User | None:
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == email.strip().lower())).first()
        if u and u.active and check_pw(pw, u.password_hash):
            return u
    return None
