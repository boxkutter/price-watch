import base64
import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ClientGrant, Credential, User, aware, utcnow

hasher = PasswordHasher()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(48)


def encrypt_secret(value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest())
    return Fernet(key).encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest())
    return Fernet(key).decrypt(value.encode()).decode()


def credential(db: Session, token: str, kind: str) -> Credential | None:
    row = db.scalar(select(Credential).where(Credential.token_hash == hash_token(token)))
    if not row or row.kind != kind or row.revoked_at or (row.expires_at and aware(row.expires_at) < utcnow()):
        return None
    return row


def issue_credential(db: Session, owner: User, kind: str, name: str, scopes: list[str], days: int = 30) -> str:
    token = new_token()
    db.add(Credential(owner_id=owner.id, name=name, token_hash=hash_token(token), kind=kind,
                      scopes=scopes, expires_at=utcnow() + timedelta(days=days)))
    db.commit()
    return token


class Principal:
    def __init__(self, user: User, scopes: set[str], client_id: str | None = None):
        self.user = user
        self.scopes = scopes
        self.client_id = client_id

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "admin" not in self.scopes:
            raise HTTPException(403, "Insufficient scope")


def principal(
    authorization: str | None = Header(None),
    x_client_token: str | None = Header(None),
    db: Session = Depends(get_db),
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    user_cred = credential(db, authorization[7:], "user")
    if not user_cred:
        raise HTTPException(401, "Invalid user token")
    user = db.get(User, user_cred.owner_id)
    if not user or not user.enabled:
        raise HTTPException(401, "User disabled")
    scopes = set(user_cred.scopes)
    client_id = None
    if x_client_token:
        client = credential(db, x_client_token, "client")
        if not client:
            raise HTTPException(401, "Invalid client token")
        grant = db.scalar(select(ClientGrant).where(ClientGrant.client_id == client.id,
                                                    ClientGrant.user_id == user.id,
                                                    ClientGrant.revoked_at.is_(None)))
        if not grant:
            raise HTTPException(403, "Client is not authorized by this user")
        scopes &= set(client.scopes) & set(grant.scopes)
        client_id = client.id
    return Principal(user, scopes, client_id)


def admin(p: Principal = Depends(principal)) -> Principal:
    if not p.user.is_admin or p.client_id:
        raise HTTPException(403, "Admin user required")
    return p
