"""API keys added from the Admin page are stored encrypted (Fernet, derived from SECRET_KEY). Env vars always win."""
import base64
import hashlib
import os
from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY
from .db import SessionLocal, get_setting, set_setting

_f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
KNOWN = ["GEMINI_API_KEY", "SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON"]


def set_secret(name: str, value: str):
    with SessionLocal() as s:
        keys = dict(get_setting(s, "secrets", {}) or {})
        keys[name] = _f.encrypt(value.encode()).decode()
        set_setting(s, "secrets", keys)
        s.commit()


def get_secret(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    with SessionLocal() as s:
        token = (get_setting(s, "secrets", {}) or {}).get(name)
    if not token:
        return ""
    try:
        return _f.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def masked() -> list[dict]:
    out = []
    for n in KNOWN:
        v = get_secret(n)
        out.append({"name": n, "set": bool(v), "from_env": bool(os.environ.get(n)),
                    "masked": ("•" * 10 + v[-2:]) if v and len(v) > 12 and n != "GOOGLE_SERVICE_ACCOUNT_JSON" else ("set" if v else "")})
    return out
