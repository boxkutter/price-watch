"""The example user: a regular, non-admin user with a scoped API client, for the /demo page.

The page then calls the API exactly as a real client app would (a user token plus a client token,
never an admin endpoint), so an administrator can see the service from a client's side. Opening a
session rotates the client credential and issues a short-lived user token; the user's data (stores,
monitors) persists until it is reset.
"""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit import record as audit
from app.models import (
    ClientGrant,
    Credential,
    Delivery,
    Event,
    IdempotencyRecord,
    Job,
    Monitor,
    User,
    UserStore,
    Webhook,
    utcnow,
)
from app.products import delete_monitor_permanently
from app.security import hash_token, hasher, issue_credential, new_token

EXAMPLE_EMAIL = "example.user@example.invalid"
CLIENT_NAME = "Example client app"
SESSION_HOURS = 12


def open_session(db: Session, admin_id: str, user_scopes: list[str]) -> dict:
    user = db.scalar(select(User).where(User.email == EXAMPLE_EMAIL))
    if not user:
        # A random password nobody knows: the example user is reached through a token, not a login.
        user = User(email=EXAMPLE_EMAIL, password_hash=hasher.hash(new_token()), is_admin=False)
        db.add(user)
        db.flush()
        audit(db, admin_id, "example_user.created", "user", user.id)
    for old in db.scalars(select(Credential).where(
            Credential.kind == "client", Credential.name == CLIENT_NAME,
            Credential.revoked_at.is_(None))):
        old.revoked_at = utcnow()
    client_scopes = [s for s in user_scopes if s != "clients:manage"]
    client_token = new_token()
    client = Credential(owner_id=admin_id, name=CLIENT_NAME, token_hash=hash_token(client_token),
                        kind="client", scopes=client_scopes)
    db.add(client)
    db.flush()
    db.add(ClientGrant(client_id=client.id, user_id=user.id, scopes=client_scopes))
    audit(db, admin_id, "example_user.session_opened", "user", user.id)
    db.commit()
    user_token = issue_credential(db, user, "user", "example client session", user_scopes,
                                  days=SESSION_HOURS / 24)
    return {"user": {"id": user.id, "email": user.email}, "user_token": user_token,
            "client_token": client_token, "scopes": client_scopes,
            "expires_in": SESSION_HOURS * 3600}


def reset_data(db: Session, admin_id: str) -> dict:
    """Forget everything the example user did: monitors, events, stores, lookups, webhooks. The
    shared catalogue (products, prices) is kept."""
    user = db.scalar(select(User).where(User.email == EXAMPLE_EMAIL))
    if not user:
        return {"monitors": 0, "stores": 0, "jobs": 0}
    monitors = db.scalars(select(Monitor).where(Monitor.owner_id == user.id)).all()
    for monitor in monitors:
        delete_monitor_permanently(db, monitor)
    stores = db.execute(delete(UserStore).where(UserStore.user_id == user.id)).rowcount
    hook_ids = select(Webhook.id).where(Webhook.owner_id == user.id)
    db.execute(delete(Delivery).where(Delivery.webhook_id.in_(hook_ids)))
    db.execute(delete(Webhook).where(Webhook.owner_id == user.id))
    db.execute(delete(Event).where(Event.owner_id == user.id))
    db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.owner_id == user.id))
    jobs = db.execute(delete(Job).where(Job.owner_id == user.id)).rowcount
    audit(db, admin_id, "example_user.reset", "user", user.id,
          {"monitors": len(monitors), "stores": stores, "jobs": jobs})
    db.commit()
    return {"monitors": len(monitors), "stores": stores, "jobs": jobs}
