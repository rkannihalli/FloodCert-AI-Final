import os
import secrets
import string
from passlib.context import CryptContext
from fastapi import Request

SUPER_ADMIN_EMAIL = "rishu.kannihalli@gmail.com"
SECRET_KEY = os.getenv("SECRET_KEY", "fema-flood-cert-session-key-change-in-prod-9f8a7b6c")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

PUBLIC_PATHS = {"/login", "/request-access"}


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static") or path == "/favicon.ico"


def hash_password(password: str) -> str:
    # bcrypt max is 72 bytes; truncate to avoid ValueError from passlib/bcrypt>=4
    secret = password.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return pwd_context.hash(secret)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        secret = plain.encode("utf-8")[:72].decode("utf-8", errors="ignore")
        return pwd_context.verify(secret, hashed)
    except Exception:
        return False


def generate_temp_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#"
    for _ in range(100):
        pwd = "".join(secrets.choice(chars) for _ in range(length))
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
        ):
            return pwd
    return secrets.token_urlsafe(length)


def get_session_user(request: Request) -> dict | None:
    return request.session.get("user")
