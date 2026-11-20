from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuthToken, User, now

ITER = 200_000


def hash_secret(secret: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), ITER)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, salt, _h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_secret(secret, salt), stored)


def issue_token(db: Session, user: User, hours: int = 12) -> str:
    tok = secrets.token_urlsafe(32)
    db.add(AuthToken(token=tok, user_id=user.id, expires_at=now() + dt.timedelta(hours=hours)))
    db.commit()
    return tok


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    tok = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    if not tok:
        raise HTTPException(401, "not authenticated")
    row = db.get(AuthToken, tok)
    if not row or row.expires_at < now():
        raise HTTPException(401, "session expired")
    user = db.get(User, row.user_id)
    if not user:
        raise HTTPException(401, "unknown user")
    return user


def user_for_token(db: Session, tok: str) -> User | None:
    row = db.get(AuthToken, tok) if tok else None
    if not row or row.expires_at < now():
        return None
    return db.get(User, row.user_id)
