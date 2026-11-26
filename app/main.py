import hashlib
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import connector_admin, connector_state, declared_stores, glossary, logos, names
from app.audit import record as audit
from app.barcodes import (
    BarcodeConflict,
    BarcodeError,
    BarcodeTaken,
    clean_barcode,
    clear_barcode,
    merge_products,
    product_for_gtin,
    product_summary,
    set_barcode,
)
from app.compare import known_rows, summarize
from app.config import settings
from app.connectors import packages, registry
from app.connectors.urls import match_product_url
from app.db import get_db
from app.example_user import open_session as open_example_session
from app.example_user import reset_data as reset_example_data
from app.gtin import normalize_gtin
from app.images import BadImage, reduce_image
from app.models import (
    AuditLog,
    ClientGrant,
    ConnectorPackage,
    ConnectorSetting,
    ConnectorState,
    Credential,
    Delivery,
    Event,
    GlossaryEntry,
    IdempotencyRecord,
    Invitation,
    Job,
    Listing,
    LoginAttempt,
    Monitor,
    Offer,
    OfferObservation,
    PriceObservation,
    Product,
    ProductAlias,
    ProductImage,
    RetailerLogo,
    Store,
    User,
    UserStore,
    Webhook,
    aware,
    utcnow,
)
from app.naming import describe, normalize_brand
from app.openapi import install as install_openapi
from app.products import delete_monitor_permanently, existing_monitor, parse_threshold
from app.ratelimit import SlidingWindow
from app.reference import best_image, reference_info
from app.security import (
    Principal,
    admin,
    credential,
    encrypt_secret,
    hash_token,
    hasher,
    issue_credential,
    new_token,
    principal,
)
from app.webhooks import validate_destination

app = FastAPI(title="Retail Monitor", version="0.1.0")
install_openapi(app)
# Per-credential (or per-address, when unauthenticated) request cap. It is a backstop: the
# expensive operations that can reach retailers also have their own quotas below.
api_limiter = SlidingWindow(settings.api_rate_limit_per_minute)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    if not request.url.path.startswith("/health"):
        auth = request.headers.get("authorization")
        key = ("t:" + hashlib.sha256(auth.encode()).hexdigest()[:16] if auth
               else "ip:" + (request.client.host if request.client else "unknown"))
        wait = api_limiter.check(key)
        if wait:
            return JSONResponse(status_code=429, headers={"Retry-After": str(wait),
                                                         "X-Request-ID": request.state.request_id},
                                content={"code": "rate_limited", "message": "Too many requests",
                                         "details": [], "request_id": request.state.request_id,
                                         "retry_after": wait})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


# Registered for Starlette's class so router-level 404/405 responses share the envelope.
@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
            409: "conflict", 422: "invalid_request", 429: "rate_limited",
            503: "unavailable"}.get(exc.status_code, "request_failed")
    return JSONResponse(status_code=exc.status_code,
                        content={"code": code, "message": str(exc.detail),
                                 "details": [], "request_id": request.state.request_id},
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details, reasons = [], []
    for error in exc.errors():
        detail = {"field": ".".join(map(str, error["loc"])), "type": error["type"]}
        if error["type"] == "value_error":      # our own validators: the reason is safe and useful
            reason = str(error["msg"]).removeprefix("Value error, ")
            detail["message"] = reason
            reasons.append(reason)
        details.append(detail)
    return JSONResponse(status_code=422,
                        content={"code": "invalid_request",
                                 "message": "Invalid request" + (f": {reasons[0]}" if reasons else ""),
                                 "details": details, "request_id": request.state.request_id})


STATIC = Path(__file__).resolve().parent.parent / "static"


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


LLMS_TXT = """# Retail Monitor

> Tracks prices and promotions for the specific products you choose, across stores. A JSON REST API.

## Docs

- [API guide](/api-guide): authentication, asynchronous jobs, limits, workflows, events, webhooks, endpoint reference (Markdown)
- [OpenAPI spec](/openapi.json): the machine-readable description of every endpoint
- [Interactive reference](/docs)

## Key facts for an agent

- Send `Authorization: Bearer <user token>` on every /v1 call; an app also sends `X-Client-Token`.
- Anything that contacts a store returns 202 with a job: poll GET /v1/jobs/{job_id} every 1-2 seconds.
- Money is a decimal string, times are ISO 8601 UTC, unknown is null (never zero).
- Prices are per store: always send store_id. Never call /v1/admin/*.
- Errors are {code, message, details, request_id}. On 429 wait Retry-After. Use Idempotency-Key on creating calls.
- To try things without real stores, use the Demo Shop stores (their prices move every minute).
"""


@app.get("/api-guide", include_in_schema=False)
def api_guide():
    """The API guide (docs/api.md), served by the app so a deployed instance documents itself."""
    return PlainTextResponse((STATIC.parent / "docs" / "api.md").read_text(encoding="utf-8"),
                             media_type="text/markdown; charset=utf-8")


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    return PlainTextResponse(LLMS_TXT, media_type="text/plain; charset=utf-8")


@app.get("/demo", include_in_schema=False)
def demo_page():
    """The example client: a regular user working through the public API (see app/example_user.py)."""
    return FileResponse(STATIC / "demo.html")


LIVE_LOOKUP_KINDS = ("search", "compare")  # share one per-user budget of live retailer lookups
RETRYABLE_JOB_STATES = {"failed", "partial", "paused", "rate_limited", "access_blocked",
                        "extraction_failed", "not_found", "invalid_data", "auth_required"}
USER_SCOPES = ["catalogue:read", "monitors:read", "monitors:write", "events:read",
               "webhooks:manage", "clients:manage"]


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class InviteRequest(BaseModel):
    email: str = Field(max_length=320)


class AcceptRequest(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(min_length=16, max_length=1024)


class ClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str]


class GrantRequest(BaseModel):
    client_id: str
    scopes: list[str]


class MonitorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str
    store_id: str
    channel: str = "online"
    conditions: dict = Field(default_factory=dict)
    notify_existing_offers: bool = False
    interval_minutes: int = Field(default=60, ge=15, le=10080)

    @field_validator("type")
    @classmethod
    def check_type(cls, value: str) -> str:
        if value not in {"store", "exact_product", "generic_product"}:
            raise ValueError("Unsupported monitor type")
        return value

    @field_validator("channel")
    @classmethod
    def check_channel(cls, value: str) -> str:
        if value not in {"online", "physical"}:
            raise ValueError("Unsupported channel")
        return value


class SettingValue(BaseModel):
    value: str = Field(min_length=1, max_length=2000)


class BarcodeLinkRequest(BaseModel):
    gtin: str = Field(min_length=8, max_length=24)
    merge: bool = False


class CompareRequest(BaseModel):
    gtin: str = Field(min_length=8, max_length=24)
    here_store_id: str | None = None
    store_ids: list[str] | None = Field(default=None, max_length=50)
    refresh: bool = True


class ProductLinkRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2000)
    store_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    interval_minutes: int = Field(default=60, ge=15, le=10080)
    maximum_price: str | None = None
    notify_existing_offers: bool = False


class WebhookRequest(BaseModel):
    url: str


class WebhookUpdate(BaseModel):
    enabled: bool


class SearchRequest(BaseModel):
    query: str | None = Field(default=None, min_length=2, max_length=100)
    gtin: str | None = None
    store_ids: list[str] = Field(min_length=1, max_length=5)
    channel: str = "online"
    # Optional narrowing of a name search, apart from the words typed (ignored for a barcode search)
    brand: str | None = Field(default=None, max_length=60)
    pack_min: int | None = Field(default=None, ge=1, le=100)
    pack_max: int | None = Field(default=None, ge=1, le=100)
    weight_min: str | None = None
    weight_max: str | None = None
    weight_unit: str = "g"

    @model_validator(mode="after")
    def narrowing(self):
        if self.weight_unit.lower() not in {"g", "kg", "ml", "cl", "dl", "l"}:
            raise ValueError("weight_unit must be g, kg, ml, cl, dl or l")
        bounds = []
        for value in (self.weight_min, self.weight_max):
            try:
                number = Decimal(value) if value else None
            except InvalidOperation:
                raise ValueError("weight_min and weight_max must be numbers") from None
            if number is not None and (not number.is_finite() or number <= 0):
                raise ValueError("weight_min and weight_max must be positive")
            bounds.append(number)
        if None not in bounds and bounds[0] > bounds[1]:
            raise ValueError("weight_min must not be above weight_max")
        if None not in (self.pack_min, self.pack_max) and self.pack_min > self.pack_max:
            raise ValueError("pack_min must not be above pack_max")
        if self.brand is not None and not self.brand.strip():
            self.brand = None
        return self

    @model_validator(mode="after")
    def query_or_gtin(self):
        if not self.query and not self.gtin:
            raise ValueError("query or gtin required")
        if self.gtin:
            normalize_gtin(self.gtin)
        return self

    @field_validator("channel")
    @classmethod
    def search_channel(cls, value: str) -> str:
        if value not in {"online", "physical"}:
            raise ValueError("Unsupported channel")
        return value


class GtinMappingRequest(BaseModel):
    gtin: str


def monitor_dict(row: Monitor, db: Session) -> dict:
    """A monitor as a client wants it: the product, its barcode, and the latest price and how it
    moved, so a watchlist can be drawn from this one response."""
    product, listing, latest = None, None, None
    if row.type == "exact_product":
        listing = db.get(Listing, str(row.conditions.get("listing_id")))
        product = db.get(Product, listing.product_id) if listing and listing.product_id else None
        seen = db.scalars(select(PriceObservation).where(
            PriceObservation.listing_id == (listing.id if listing else None),
            PriceObservation.account_id.is_(None), PriceObservation.eligibility == "public")
            .order_by(PriceObservation.observed_at.desc()).limit(2)).all() if listing else []
        if seen and seen[0].current_price is not None:
            latest = {"current_price": str(seen[0].current_price), "currency": seen[0].currency,
                      "previous_price": str(seen[1].current_price) if len(seen) > 1 and seen[1].current_price is not None else None,
                      "unit_price": ({"amount": str(seen[0].unit_price), "unit": seen[0].unit_price_unit}
                                     if seen[0].unit_price is not None else None),
                      "availability": seen[0].availability, "observed_at": seen[0].observed_at}
    store = db.get(Store, row.store_id)
    return {"product_id": product.id if product else None,
            "product_name": listing.name if listing else None,
            "store_name": store.name if store else None,
            "retailer_id": store.retailer_id if store else None,
            "image": best_image(db, product.id, row.owner_id) if product else None,
            "latest": latest,
            "barcode": ({"gtin14": product.gtin14, "source": product.gtin_source}
                        if product and product.gtin14 else None),
            **_monitor_fields(row)}


def _monitor_fields(row: Monitor) -> dict:
    return {"id": row.id, "name": row.name, "type": row.type, "store_id": row.store_id,
            "channel": row.channel, "conditions": row.conditions, "enabled": row.enabled,
            "interval_minutes": row.interval_minutes,
            "last_success_at": row.last_success_at, "next_due_at": row.next_due_at,
            "baseline_complete": row.baseline_complete, "removed_at": row.removed_at}


def public_listing(db: Session, listing_id: str) -> Listing:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Listing not found")
    return listing


def price_dict(row: PriceObservation, listing: Listing) -> dict:
    return {"id": row.id, "listing_id": listing.id, "store_id": listing.store_id,
            "channel": listing.channel, "eligibility": row.eligibility,
            "currency": row.currency,
            "current_price": str(row.current_price) if row.current_price is not None else None,
            "regular_price": str(row.regular_price) if row.regular_price is not None else None,
            "unit_price": {"amount": str(row.unit_price), "unit": row.unit_price_unit}
            if row.unit_price is not None else None,
            "availability": row.availability, "observed_at": row.observed_at,
            "source_url": row.source_url}


def offer_dict(row: Offer) -> dict:
    return {"id": row.id, "retailer_id": row.retailer_id, "store_id": row.store_id,
            "channel": row.channel, "title": row.title, "terms": row.terms,
            "eligibility": row.eligibility, "price": str(row.price) if row.price is not None else None,
            "currency": row.currency, "status": row.status, "source_url": row.source_url,
            "first_seen": row.first_seen, "last_seen": row.last_seen,
            "valid_from": row.valid_from, "valid_to": row.valid_to}


def event_dict(row: Event) -> dict:
    return {"id": row.id, "type": row.type, "monitor_id": row.monitor_id,
            "payload": row.payload, "created_at": row.created_at}


def request_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def idempotent_resource(db: Session, owner_id: str, operation: str,
                        key: str | None, digest: str) -> str | None:
    if key is None:
        return None
    if not key.strip() or len(key) > 200:
        raise HTTPException(422, "Invalid Idempotency-Key")
    row = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == owner_id,
        IdempotencyRecord.operation == operation, IdempotencyRecord.request_key == key))
    if not row:
        return None
    if row.request_hash != digest:
        raise HTTPException(409, "Idempotency-Key reused with a different request")
    return row.resource_id


def remember_request(db: Session, owner_id: str, operation: str,
                     key: str | None, digest: str, resource_id: str) -> None:
    if key is not None:
        db.add(IdempotencyRecord(owner_id=owner_id, operation=operation,
                                 request_key=key, request_hash=digest,
                                 resource_id=resource_id))


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    expected = ScriptDirectory.from_config(config).get_current_head()
    try:
        actual = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database migration required") from exc
    if actual != expected:
        raise HTTPException(503, "Database migration required")
    return {"status": "ok", "schema_version": actual}


@app.post("/v1/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    window_start = utcnow() - timedelta(minutes=15)
    db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < window_start))
    db.commit()
    failures = db.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.email == email, LoginAttempt.attempted_at >= window_start))
    if failures >= 5:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "900"})
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.enabled:
        db.add(LoginAttempt(email=email))
        db.commit()
        raise HTTPException(401, "Invalid credentials")
    try:
        hasher.verify(user.password_hash, body.password)
    except VerifyMismatchError:
        db.add(LoginAttempt(email=email))
        db.commit()
        raise HTTPException(401, "Invalid credentials") from None
    db.execute(delete(LoginAttempt).where(LoginAttempt.email == email))
    db.commit()
    scopes = USER_SCOPES + (["admin"] if user.is_admin else [])
    return {"access_token": issue_credential(db, user, "user", "login", scopes),
            "token_type": "bearer", "expires_in": 30 * 86400, "user_id": user.id}


@app.post("/v1/admin/invitations", status_code=201)
def invite(body: InviteRequest, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    email = body.email.strip().lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "User already exists")
    token = new_token()
    row = Invitation(email=email, token_hash=hash_token(token),
                     expires_at=utcnow() + timedelta(days=7), created_by=p.user.id)
    db.add(row)
    db.flush()
    audit(db, p.user.id, "invitation.created", "invitation", row.id)
    db.commit()
    return {"id": row.id, "invitation_token": token, "expires_at": row.expires_at}


@app.get("/v1/admin/users")
def admin_users(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Everyone with an account: when they joined and last signed in, whether they can sign in, how
    many active sessions they have and what they use (monitors, stores, apps they granted access)."""
    now = utcnow()
    sessions = db.execute(select(Credential.owner_id, func.count(), func.max(Credential.created_at))
                          .where(Credential.kind == "user").group_by(Credential.owner_id)).all()
    last_seen = {owner: seen for owner, _count, seen in sessions}
    active = dict(db.execute(select(Credential.owner_id, func.count()).where(
        Credential.kind == "user", Credential.revoked_at.is_(None),
        (Credential.expires_at.is_(None)) | (Credential.expires_at > now)).group_by(Credential.owner_id)).all())
    monitors = dict(db.execute(select(Monitor.owner_id, func.count()).where(
        Monitor.enabled.is_(True)).group_by(Monitor.owner_id)).all())
    stores = dict(db.execute(select(UserStore.user_id, func.count()).group_by(UserStore.user_id)).all())
    apps = dict(db.execute(select(ClientGrant.user_id, func.count()).where(
        ClientGrant.revoked_at.is_(None)).group_by(ClientGrant.user_id)).all())
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    return {"items": [{"id": u.id, "email": u.email, "is_admin": u.is_admin, "enabled": u.enabled,
                       "created_at": u.created_at, "last_signed_in_at": last_seen.get(u.id),
                       "active_sessions": active.get(u.id, 0), "monitors": monitors.get(u.id, 0),
                       "stores": stores.get(u.id, 0), "apps": apps.get(u.id, 0)} for u in rows]}


@app.post("/v1/admin/users/{user_id}/disable", status_code=204)
def disable_user(user_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Stop a user from signing in and end their sessions. Their data is kept; enable them again to
    let them back in. You cannot disable yourself."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == p.user.id:
        raise HTTPException(409, "You cannot disable your own account")
    user.enabled = False
    for row in db.scalars(select(Credential).where(
            Credential.owner_id == user.id, Credential.kind == "user", Credential.revoked_at.is_(None))):
        row.revoked_at = utcnow()
    audit(db, p.user.id, "user.disabled", "user", user.id)
    db.commit()


@app.post("/v1/admin/users/{user_id}/enable", status_code=204)
def enable_user(user_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Let a disabled user sign in again."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.enabled = True
    audit(db, p.user.id, "user.enabled", "user", user.id)
    db.commit()


@app.get("/v1/admin/invitations")
def admin_invitations(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Invitations and what became of them: `pending`, `used` (someone registered) or `expired`."""
    now = utcnow()
    emails = dict(db.execute(select(User.id, User.email)).all())
    rows = db.scalars(select(Invitation).order_by(Invitation.expires_at.desc())).all()
    return {"items": [{"id": r.id, "email": r.email, "expires_at": r.expires_at, "used_at": r.used_at,
                       "invited_by": emails.get(r.created_by),
                       "status": "used" if r.used_at else "expired" if aware(r.expires_at) < now else "pending"}
                      for r in rows]}


@app.delete("/v1/admin/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Withdraw an invitation that has not been used yet; its token stops working."""
    row = db.get(Invitation, invitation_id)
    if not row:
        raise HTTPException(404, "Invitation not found")
    if row.used_at:
        raise HTTPException(409, "That invitation was already used")
    row.expires_at = utcnow()
    audit(db, p.user.id, "invitation.revoked", "invitation", row.id)
    db.commit()


@app.get("/v1/admin/clients")
def admin_clients(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Client applications (the apps people sign in through) and how many users have let each in."""
    grants = dict(db.execute(select(ClientGrant.client_id, func.count()).where(
        ClientGrant.revoked_at.is_(None)).group_by(ClientGrant.client_id)).all())
    rows = db.scalars(select(Credential).where(Credential.kind == "client")
                      .order_by(Credential.created_at)).all()
    return {"items": [{"id": c.id, "name": c.name, "scopes": c.scopes, "created_at": c.created_at,
                       "revoked_at": c.revoked_at, "users": grants.get(c.id, 0)} for c in rows]}


@app.get("/v1/admin/audit")
def audit_log(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc())
                      .limit(200)).all()
    return {"items": [{"id": row.id, "actor_id": row.actor_id,
                       "action": row.action, "target_type": row.target_type,
                       "target_id": row.target_id, "details": row.details,
                       "created_at": row.created_at} for row in rows]}


@app.post("/v1/auth/accept-invitation", status_code=201)
def accept_invitation(body: AcceptRequest, db: Session = Depends(get_db)):
    invite = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(body.token))
                       .with_for_update())
    if not invite or invite.used_at or aware(invite.expires_at) < utcnow():
        raise HTTPException(400, "Invalid or expired invitation")
    if db.scalar(select(User.id).where(User.email == invite.email)):
        raise HTTPException(409, "User already exists")
    user = User(email=invite.email, password_hash=hasher.hash(body.password))
    db.add(user)
    db.flush()
    audit(db, user.id, "user.created", "user", user.id)
    invite.used_at = utcnow()
    db.commit()
    return {"user_id": user.id}


@app.get("/v1/me")
def me(p: Principal = Depends(principal)):
    return {"id": p.user.id, "email": p.user.email, "is_admin": p.user.is_admin,
            "client_id": p.client_id, "scopes": sorted(p.scopes)}


@app.post("/v1/auth/logout", status_code=204)
def logout(authorization: str = Header(), db: Session = Depends(get_db),
                 p: Principal = Depends(principal)):
    row = credential(db, authorization[7:], "user")
    if row and row.owner_id == p.user.id:
        row.revoked_at = utcnow()
        db.commit()


@app.post("/v1/clients", status_code=201)
def create_client(body: ClientRequest, db: Session = Depends(get_db),
                  p: Principal = Depends(admin)):
    allowed = set(USER_SCOPES) - {"clients:manage"}
    if not set(body.scopes) <= allowed:
        raise HTTPException(422, "Invalid client scopes")
    token = new_token()
    client = Credential(owner_id=p.user.id, name=body.name, token_hash=hash_token(token),
                        kind="client", scopes=body.scopes)
    db.add(client)
    db.flush()
    audit(db, p.user.id, "client.created", "client", client.id)
    db.commit()
    return {"client_id": client.id, "client_token": token, "scopes": client.scopes}


@app.delete("/v1/clients/{client_id}", status_code=204)
def revoke_client_credential(client_id: str, db: Session = Depends(get_db),
                                   p: Principal = Depends(admin)):
    row = db.get(Credential, client_id)
    if not row or row.kind != "client":
        raise HTTPException(404, "Client not found")
    row.revoked_at = utcnow()
    audit(db, p.user.id, "client.revoked", "client", row.id)
    db.commit()


@app.post("/v1/me/client-grants", status_code=201)
def grant_client(body: GrantRequest, db: Session = Depends(get_db),
                 p: Principal = Depends(principal)):
    p.require("clients:manage")
    client = db.get(Credential, body.client_id)
    if not client or client.kind != "client" or client.revoked_at:
        raise HTTPException(404, "Client not found")
    if not set(body.scopes) <= set(client.scopes) & p.scopes:
        raise HTTPException(422, "Invalid grant scopes")
    existing = db.scalar(select(ClientGrant).where(ClientGrant.client_id == client.id,
                                                   ClientGrant.user_id == p.user.id))
    if existing:
        existing.scopes = body.scopes
        existing.revoked_at = None
        grant = existing
    else:
        grant = ClientGrant(client_id=client.id, user_id=p.user.id, scopes=body.scopes)
        db.add(grant)
    db.flush()
    audit(db, p.user.id, "client_grant.updated", "client_grant", grant.id)
    db.commit()
    return {"grant_id": grant.id, "scopes": grant.scopes}


@app.delete("/v1/me/client-grants/{client_id}", status_code=204)
def revoke_client(client_id: str, db: Session = Depends(get_db),
                  p: Principal = Depends(principal)):
    p.require("clients:manage")
    grant = db.scalar(select(ClientGrant).where(ClientGrant.client_id == client_id,
                                               ClientGrant.user_id == p.user.id))
    if not grant:
        raise HTTPException(404, "Grant not found")
    grant.revoked_at = utcnow()
    audit(db, p.user.id, "client_grant.revoked", "client_grant", grant.id)
    db.commit()


PUBLIC_CONNECTOR_FIELDS = ("id", "name", "country", "description", "enabled", "status",
                            "capabilities", "connector_version", "access_review",
                            "minimum_monitor_interval_minutes", "store_discovery_inputs", "logo_url")


def public_connectors(db: Session) -> list[dict]:
    return [{key: item[key] for key in PUBLIC_CONNECTOR_FIELDS}
            for item in connector_admin.admin_view(db)["items"]]


@app.get("/v1/retailers")
def retailers(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": public_connectors(db)}


@app.get("/v1/connectors")
def connectors(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": public_connectors(db)}


@app.get("/v1/connectors/{retailer_id}/health")
def connector_health(retailer_id: str, db: Session = Depends(get_db),
                     p: Principal = Depends(principal)):
    p.require("catalogue:read")
    retailer = registry.retailer(retailer_id)
    if not retailer:
        raise HTTPException(404, "Connector not found")
    last_job = db.scalar(select(Job).join(Monitor, Job.monitor_id == Monitor.id)
        .join(Store, Monitor.store_id == Store.id)
        .where(Store.retailer_id == retailer_id)
        .order_by(Job.created_at.desc()).limit(1))
    state = db.get(ConnectorState, retailer_id)
    paused = bool(state and state.state == "paused")
    return {"id": retailer_id, "enabled": connector_state.is_enabled(db, retailer_id),
            "access_review": retailer["access_review"], "paused": paused,
            "pause_reason": state.reason if paused else None,
            "cooldown_until": state.cooldown_until if state else None,
            "requests_in_window": state.requests_in_window if state else 0,
            "daily_request_limit": retailer["max_requests_per_day"],
            "status": "disabled" if not connector_state.is_enabled(db, retailer_id)
            else "paused" if paused else (last_job.state if last_job else "no_checks"),
            "last_job_id": last_job.id if last_job else None,
            "last_result": last_job.result if last_job else None}


@app.post("/v1/admin/connectors/{retailer_id}/pause", status_code=204)
def pause_connector(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    if not registry.retailer(retailer_id):
        raise HTTPException(404, "Connector not found")
    connector_state.pause(db, retailer_id, "Paused by an administrator")
    audit(db, p.user.id, "connector.paused", "connector", retailer_id)
    db.commit()


@app.post("/v1/admin/connectors/{retailer_id}/resume", status_code=204)
def resume_connector(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    if not registry.retailer(retailer_id):
        raise HTTPException(404, "Connector not found")
    connector_state.resume(db, retailer_id)
    audit(db, p.user.id, "connector.resumed", "connector", retailer_id)
    db.commit()


@app.post("/v1/admin/example-user", tags=["Administration"])
def example_user_session(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Open a session as the example user (a regular, non-admin user with a scoped API client) for
    the /demo page. Creates the user the first time, revokes the previous example client credential
    and returns a fresh user token and client token, both shown only here."""
    return open_example_session(db, p.user.id, USER_SCOPES)


@app.delete("/v1/admin/example-user/data", tags=["Administration"])
def reset_example_user(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Forget everything the example user did (monitors, events, stores, lookups). The catalogue stays."""
    return reset_example_data(db, p.user.id)


@app.get("/v1/admin/connectors")
def admin_connectors(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Every installed connector with its state, settings (never values) and usage, plus any
    packages that failed to load and stores left behind by connectors that were removed."""
    return connector_admin.admin_view(db)


def _loaded_connector(retailer_id: str):
    entry = registry.entry(retailer_id)
    if entry is None:
        raise HTTPException(404, "Connector not found")
    return entry


@app.post("/v1/admin/connectors/{retailer_id}/enable", status_code=204)
def enable_connector(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    entry = _loaded_connector(retailer_id)
    if entry.instance is None or entry.error:
        raise HTTPException(409, entry.error or "This connector has no implementation to enable")
    connector_state.set_enabled(db, retailer_id, True)
    audit(db, p.user.id, "connector.enabled", "connector", retailer_id)
    db.commit()


@app.post("/v1/admin/connectors/{retailer_id}/disable", status_code=204)
def disable_connector(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    _loaded_connector(retailer_id)
    connector_state.set_enabled(db, retailer_id, False)
    audit(db, p.user.id, "connector.disabled", "connector", retailer_id)
    db.commit()


@app.put("/v1/admin/connectors/{retailer_id}/settings/{name}", status_code=204)
def put_connector_setting(retailer_id: str, name: str, body: SettingValue,
                          db: Session = Depends(get_db), p: Principal = Depends(admin)):
    _loaded_connector(retailer_id)
    if not connector_admin.declared_setting(retailer_id, name):
        raise HTTPException(404, "This connector does not declare that setting")
    connector_admin.set_setting(db, retailer_id, name, body.value.strip())
    audit(db, p.user.id, "connector.setting_set", "connector", retailer_id, {"setting": name})
    db.commit()


@app.delete("/v1/admin/connectors/{retailer_id}/settings/{name}", status_code=204)
def delete_connector_setting(retailer_id: str, name: str, db: Session = Depends(get_db),
                             p: Principal = Depends(admin)):
    _loaded_connector(retailer_id)
    if not connector_admin.clear_setting(db, retailer_id, name):
        raise HTTPException(404, "No administrator-entered value for that setting")
    audit(db, p.user.id, "connector.setting_cleared", "connector", retailer_id, {"setting": name})
    db.commit()


async def package_body(request: Request) -> bytes:
    limit = settings.max_package_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"The package is too large (limit {limit} bytes)")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"The package is too large (limit {limit} bytes)")
    return bytes(data)


@app.post("/v1/admin/connector-packages/inspect", status_code=201)
async def inspect_connector_package(data: bytes = Depends(package_body),
                                    p: Principal = Depends(admin)):
    """Upload a connector zip (as the request body). Nothing is executed or installed; the
    response is a preview to review, with an `upload_id` to install it with."""
    try:
        token, info = await run_in_threadpool(packages.stage, data)
    except packages.PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"upload_id": token, "package": info.summary()}


@app.post("/v1/admin/connector-packages/{upload_id}/install", status_code=201)
def install_connector_package(upload_id: str, db: Session = Depends(get_db),
                              p: Principal = Depends(admin)):
    """Install a previewed upload. A new connector starts disabled; enabling is a separate,
    deliberate step. A package that fails to load is rolled back and reported."""
    try:
        info = packages.install_staged(upload_id)
    except packages.PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    retailer_id = info.manifest.id
    row = db.get(ConnectorPackage, retailer_id)
    if row:
        row.version, row.sha256, row.installed_by, row.installed_at = (
            info.manifest.connector_version, info.sha256, p.user.id, utcnow())
    else:
        db.add(ConnectorPackage(retailer_id=retailer_id, version=info.manifest.connector_version,
                                sha256=info.sha256, installed_by=p.user.id))
        connector_state.set_enabled(db, retailer_id, False)
    declared_stores.seed(db)
    audit(db, p.user.id, "connector.installed", "connector", retailer_id,
          {"version": info.manifest.connector_version, "sha256": info.sha256,
           "replaced": info.replaces is not None})
    db.commit()
    return {"package": info.summary(), "status": connector_admin.status_of(
        db, registry.retailer(retailer_id))}


@app.delete("/v1/admin/connector-packages/{retailer_id}", status_code=204)
def uninstall_connector_package(retailer_id: str, disable_monitors: bool = False,
                                db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Remove an installed connector. Its stores, prices and history are kept; monitors that
    use it must be disabled first (or pass disable_monitors=true)."""
    entry = registry.entry(retailer_id)
    if entry is None or entry.source != "installed":
        raise HTTPException(404, "No installed connector with that id")
    active = db.scalars(select(Monitor).join(Store, Monitor.store_id == Store.id).where(
        Store.retailer_id == retailer_id, Monitor.enabled.is_(True))).all()
    if active and not disable_monitors:
        raise HTTPException(409, f"{len(active)} active monitor(s) use this connector; "
                                 "disable them first or pass disable_monitors=true")
    for monitor in active:
        monitor.enabled = False
    try:
        packages.uninstall(retailer_id)
    except packages.PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.execute(delete(ConnectorPackage).where(ConnectorPackage.retailer_id == retailer_id))
    db.execute(delete(ConnectorSetting).where(ConnectorSetting.retailer_id == retailer_id))
    connector_state.set_enabled(db, retailer_id, None)
    audit(db, p.user.id, "connector.uninstalled", "connector", retailer_id,
          {"monitors_disabled": len(active)})
    db.commit()


@app.post("/v1/admin/retailers/{retailer_id}/stores/discover", status_code=202)
def discover_stores(retailer_id: str, body: dict[str, str] | None = None,
                    db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Queue a store lookup (one small request, run by the worker).

    The body is a flat object of the inputs the connector's manifest declares under
    `store_discovery_inputs` (Coop needs `{"postcode": "11120"}`; City Gross needs none).
    """
    retailer = registry.retailer(retailer_id)
    if not retailer or not connector_state.is_enabled(db, retailer_id):
        raise HTTPException(404, "Connector not found or not enabled")
    if retailer["capabilities"].get("store_discovery") != "supported":
        raise HTTPException(422, "This connector does not support store discovery")
    inputs = {key: value.strip() for key, value in (body or {}).items()}
    declared = {spec["name"]: spec for spec in retailer["store_discovery_inputs"]}
    for key in inputs:
        if key not in declared:
            raise HTTPException(422, f"Unknown input '{key}'; this connector takes: "
                                     f"{', '.join(declared) or 'none'}")
    for name, spec in declared.items():
        label = spec["label"] or name
        if not inputs.get(name):
            if spec["required"]:
                raise HTTPException(422, f"Input '{name}' ({label}) is required")
        elif spec["pattern"] and not re.fullmatch(spec["pattern"], inputs[name]):
            raise HTTPException(422, f"Input '{name}' ({label}) has an invalid format")
    inputs = {name: value.replace(" ", "") if name == "postcode" else value
              for name, value in inputs.items()}
    payload = {"retailer_id": retailer_id, "inputs": inputs}
    in_flight = db.scalars(select(Job).where(Job.kind == "discover_stores",
        Job.state.in_(["queued", "running"]))).all()
    pending = next((job for job in in_flight if job.payload == payload), None)
    if pending:
        return {"job_id": pending.id, "status": pending.state}
    job = Job(owner_id=p.user.id, kind="discover_stores", payload=payload)
    db.add(job)
    db.flush()
    audit(db, p.user.id, "stores.discovery_queued", "connector", retailer_id)
    db.commit()
    return {"job_id": job.id, "status": job.state}


@app.get("/v1/stores")
def stores(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": [{"id": s.id, "name": s.name, "retailer_id": s.retailer_id,
                       "country": s.country}
                      for s in db.scalars(select(Store).where(
                          Store.retailer_id.in_(connector_state.enabled_ids(db)))
                          .order_by(Store.retailer_id, Store.name))]}


@app.get("/v1/retailers/{retailer_id}/stores")
def retailer_stores(retailer_id: str, db: Session = Depends(get_db),
                          p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": [{"id": s.id, "name": s.name, "retailer_id": s.retailer_id,
                       "country": s.country, "timezone": s.timezone} for s in
                      db.scalars(select(Store).where(
                          Store.retailer_id == retailer_id,
                          Store.retailer_id.in_(connector_state.enabled_ids(db))))]}


@app.get("/v1/stores/{store_id}")
def get_store(store_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "Store not found")
    return {"id": store.id, "retailer_id": store.retailer_id,
            "external_id": store.external_id, "name": store.name,
            "country": store.country, "timezone": store.timezone}


@app.get("/v1/products/search")
def search_products(q: str = "", db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    items = db.scalars(select(Product).where(Product.name.ilike(f"%{q[:100]}%")).limit(50)).all()
    return {"items": [{"id": x.id, "name": x.name, "brand": x.brand,
                       "gtin14": x.gtin14, "verified": x.verified} for x in items]}


@app.post("/v1/search/jobs", status_code=202)
def create_search_job(body: SearchRequest, db: Session = Depends(get_db),
                            p: Principal = Depends(principal),
                            idempotency_key: str | None = Header(None)):
    p.require("catalogue:read")
    if len(set(body.store_ids)) != len(body.store_ids):
        raise HTTPException(422, "Duplicate store ID")
    for store_id in body.store_ids:
        if not db.get(Store, store_id):
            raise HTTPException(404, "Store not found")
    digest = request_digest(body.model_dump())
    existing_id = idempotent_resource(db, p.user.id, "search_job", idempotency_key, digest)
    if existing_id:
        job = db.get(Job, existing_id)
        return {"job_id": job.id, "status": job.state,
                "status_url": f"/v1/jobs/{job.id}"}
    pending = db.scalar(select(func.count()).select_from(Job).where(
        Job.owner_id == p.user.id, Job.kind.in_(LIVE_LOOKUP_KINDS),
        Job.state.in_(["queued", "running"])))
    if pending >= settings.max_pending_searches_per_user:
        raise HTTPException(429, "Too many searches in progress", headers={"Retry-After": "60"})
    recent = db.scalar(select(func.count()).select_from(Job).where(
        Job.owner_id == p.user.id, Job.kind.in_(LIVE_LOOKUP_KINDS),
        Job.created_at >= utcnow() - timedelta(hours=1)))
    if recent >= settings.max_searches_per_hour:
        raise HTTPException(429, "Search limit reached for this hour", headers={"Retry-After": "600"})
    job = Job(owner_id=p.user.id, kind="search", payload=body.model_dump())
    db.add(job)
    db.flush()
    remember_request(db, p.user.id, "search_job", idempotency_key, digest, job.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key is None:
            raise
        existing_id = idempotent_resource(db, p.user.id, "search_job", idempotency_key, digest)
        if not existing_id:
            raise
        job = db.get(Job, existing_id)
    return {"job_id": job.id, "status": job.state,
            "status_url": f"/v1/jobs/{job.id}"}


@app.get("/v1/products/barcode/{gtin}")
def barcode(gtin: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    try:
        normalized = normalize_gtin(gtin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    product = db.scalar(select(Product).where(Product.gtin14 == normalized, Product.verified.is_(True)))
    if not product:
        raise HTTPException(404, "No verified mapping")
    return {"id": product.id, "name": product.name, "gtin14": product.gtin14}


@app.get("/v1/products/{product_id}")
def get_product(product_id: str, db: Session = Depends(get_db),
                      p: Principal = Depends(principal)):
    p.require("catalogue:read")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    listings = db.scalars(select(Listing).where(Listing.product_id == product.id)).all()
    return {"id": product.id, "name": product.name, "brand": normalize_brand(product.brand),
            "gtin14": product.gtin14, "verified": product.verified,
            "gtin_source": product.gtin_source, "gtin_set_at": product.gtin_set_at,
            "package_quantity": {"value": str(product.package_value), "unit": product.package_unit}
            if product.package_value is not None else None,
            "image": best_image(db, product.id, p.user.id),
            "reference": reference_info(db, product.gtin14, describe(
                product.name, product.brand, product.package_value, product.package_unit)),
            "listings": [{"id": x.id, "retailer_id": x.retailer_id, "sku": x.sku,
                          "store_id": x.store_id, "channel": x.channel} for x in listings]}


async def image_body(request: Request) -> bytes:
    limit = settings.max_image_upload_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"The picture is too large (limit {limit} bytes)")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"The picture is too large (limit {limit} bytes)")
    return bytes(data)


@app.put("/v1/products/{product_id}/image")
async def upload_product_image(product_id: str, data: bytes = Depends(image_body),
                               db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Use your own picture of this product (the request body is the image: JPEG, PNG, WebP, ...).
    It is reduced and compressed, location and camera data are dropped, and it replaces any earlier
    picture of yours. Only you see it; it is preferred over the store's and the reference's."""
    p.require("monitors:write")
    if not db.get(Product, product_id):
        raise HTTPException(404, "Product not found")
    try:
        reduced, width, height = await run_in_threadpool(reduce_image, data)
    except BadImage as exc:
        raise HTTPException(422, str(exc)) from exc
    for old in db.scalars(select(ProductImage).where(
            ProductImage.product_id == product_id, ProductImage.owner_id == p.user.id)):
        db.delete(old)
    image = ProductImage(product_id=product_id, owner_id=p.user.id, source="user",
                         content_type="image/webp", data=reduced, width=width, height=height)
    db.add(image)
    audit(db, p.user.id, "product.image_uploaded", "product", product_id)
    db.commit()
    return {"image": {"url": f"/images/{image.id}", "source": "user", "credit": None}}


@app.delete("/v1/products/{product_id}/image", status_code=204)
def remove_product_image(product_id: str, db: Session = Depends(get_db),
                         p: Principal = Depends(principal)):
    """Remove your own picture of this product; the store's or the reference's shows again."""
    p.require("monitors:write")
    db.execute(delete(ProductImage).where(ProductImage.product_id == product_id,
                                          ProductImage.owner_id == p.user.id))
    db.commit()


class SameAsRequest(BaseModel):
    product_id: str


class NameRequest(BaseModel):
    name: str = Field(min_length=2, max_length=names.MAX_ALIAS_LENGTH)


class GlossaryRequest(BaseModel):
    term: str = Field(min_length=2, max_length=80, description="The word as people (or other shops) say it")
    canonical: str = Field(min_length=2, max_length=80, description="The word the shops use")

    @field_validator("term", "canonical")
    @classmethod
    def plain_words(cls, value: str) -> str:
        value = " ".join(value.split())
        if not re.fullmatch(r"[\w][\w \-]*", value):
            raise ValueError("Use letters, digits, spaces and hyphens only")
        return value.lower()


@app.post("/v1/products/{product_id}/same-as")
def products_are_the_same(product_id: str, body: SameAsRequest, db: Session = Depends(get_db),
                          p: Principal = Depends(principal)):
    """Say that two products are the same item (for example after a "probably the same" suggestion):
    the listings of one move to the other, price history is kept. The one with a barcode is kept;
    two products with different barcodes are never merged."""
    p.require("monitors:write")
    one, other = db.get(Product, product_id), db.get(Product, body.product_id)
    if not one or not other:
        raise HTTPException(404, "Product not found")
    if one.id == other.id:
        return {"outcome": "unchanged", "merged_listings": 0, "product": product_summary(db, one)}
    if one.gtin14 and other.gtin14:
        raise HTTPException(409, "Both products already have a barcode, and they differ: "
                                 "they are not the same item.")
    counts = {x.id: db.scalar(select(func.count()).select_from(Listing)
                              .where(Listing.product_id == x.id)) for x in (one, other)}
    # keep the one that has a barcode, else the one with more listings
    target, source = ((one, other) if one.gtin14 else (other, one) if other.gtin14
                      else (one, other) if counts[one.id] > counts[other.id] else (other, one))
    try:
        moved = merge_products(db, source, target)
    except BarcodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(db, p.user.id, "product.merged", "product", target.id,
          {"from": source.id, "listings_moved": moved})
    db.commit()
    return {"outcome": "merged", "merged_listings": moved, "product": product_summary(db, target)}


def _names_view(db: Session, product: Product) -> dict:
    return {"items": [{"name": n.name, "source": n.source, "id": n.id, "retailer_id": n.retailer_id}
                      for n in names.product_names(db, product)]}


@app.get("/v1/products/{product_id}/names")
def product_names_list(product_id: str, db: Session = Depends(get_db),
                       p: Principal = Depends(principal)):
    """Every name a product goes by: its store listings' names, the reference database's, and the
    alternative names people added (`source` is `store`, `reference` or `user`)."""
    p.require("catalogue:read")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    return _names_view(db, product)


@app.post("/v1/products/{product_id}/names", status_code=201)
def add_product_name(product_id: str, body: NameRequest, response: Response,
                     db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Add an alternative name (for example the English one). Shared by everyone, and recorded with
    who added it. A search for it then finds this product in shops that call it something else."""
    p.require("monitors:write")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    row = names.add_alias(db, product, body.name, p.user.id)
    if row is None:
        response.status_code = 200                 # it already goes by that name
    else:
        audit(db, p.user.id, "product.name_added", "product", product.id, {"name": row.name})
        db.commit()
    return _names_view(db, product)


@app.delete("/v1/products/{product_id}/names/{name_id}", status_code=204)
def remove_product_name(product_id: str, name_id: str, db: Session = Depends(get_db),
                        p: Principal = Depends(principal)):
    """Remove an alternative name. Only the person who added it, or an administrator, can."""
    p.require("monitors:write")
    row = db.get(ProductAlias, name_id)
    if not row or row.product_id != product_id:
        raise HTTPException(404, "Name not found")
    if row.created_by != p.user.id and not p.user.is_admin:
        raise HTTPException(403, "Only the person who added a name, or an administrator, can remove it")
    audit(db, p.user.id, "product.name_removed", "product", product_id, {"name": row.name})
    db.delete(row)
    db.commit()


def _glossary_view(db: Session) -> dict:
    custom = [{"id": r.id, "term": r.term, "canonical": r.canonical, "source": "custom",
               "created_by": r.created_by} for r in db.scalars(
        select(GlossaryEntry).order_by(GlossaryEntry.term))]
    return {"items": custom + [{"id": None, **entry} for entry in glossary.entries()]}


@app.get("/v1/glossary")
def glossary_list(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """The English and Swedish words treated as the same in searches and comparisons: the ones people
    added (`custom`, with an id) and the built-in list."""
    p.require("catalogue:read")
    return _glossary_view(db)


@app.post("/v1/glossary", status_code=201)
def glossary_add(body: GlossaryRequest, db: Session = Depends(get_db),
                 p: Principal = Depends(principal)):
    """Add a word pair, for example `{"term": "oat drink", "canonical": "havredryck"}`. Shared by
    everyone: searches for the term are also asked in the shops' word, and names that use either are
    treated as the same."""
    p.require("monitors:write")
    if glossary.fold(body.term) == glossary.fold(body.canonical):
        raise HTTPException(422, "The two words are the same")
    known = {glossary.fold(k) for k in [*glossary.BUILTIN, *glossary.CUSTOM]}
    if glossary.fold(body.term) in known:
        raise HTTPException(409, "That word already has a translation")
    row = GlossaryEntry(term=body.term, canonical=body.canonical, created_by=p.user.id)
    db.add(row)
    audit(db, p.user.id, "glossary.added", "glossary", row.id, {"term": row.term})
    db.commit()
    glossary.refresh(db)
    return _glossary_view(db)


@app.delete("/v1/glossary/{entry_id}", status_code=204)
def glossary_remove(entry_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Remove a word pair that was added. Only the person who added it, or an administrator."""
    p.require("monitors:write")
    row = db.get(GlossaryEntry, entry_id)
    if not row:
        raise HTTPException(404, "Entry not found")
    if row.created_by != p.user.id and not p.user.is_admin:
        raise HTTPException(403, "Only the person who added it, or an administrator, can remove it")
    audit(db, p.user.id, "glossary.removed", "glossary", entry_id, {"term": row.term})
    db.delete(row)
    db.commit()
    glossary.refresh(db)


@app.put("/v1/admin/connectors/{retailer_id}/logo")
async def upload_retailer_logo(retailer_id: str, data: bytes = Depends(image_body),
                               db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Set the logo shown for a retailer (the request body is the image: PNG, JPEG, WebP, ...). It is
    reduced to at most 160 px and keeps its transparency. Without one, clients draw a coloured badge
    from the retailer's name."""
    if not registry.retailer(retailer_id):
        raise HTTPException(404, "Connector not found")
    try:
        reduced, width, height = await run_in_threadpool(reduce_image, data, 160, True)
    except BadImage as exc:
        raise HTTPException(422, str(exc)) from exc
    row = logos.save_logo(db, retailer_id, reduced, width, height, p.user.id)
    audit(db, p.user.id, "connector.logo_set", "connector", retailer_id)
    db.commit()
    return {"logo_url": f"/logos/{retailer_id}?v={int(row.updated_at.timestamp())}"}


@app.post("/v1/admin/connectors/{retailer_id}/logo/fetch", status_code=202)
def fetch_retailer_logo(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Fetch a retailer's logo from its own website (its touch icon or favicon), as a worker job.
    It contacts only the retailer's website, a handful of requests, and keeps the largest usable
    icon; the job result says where it came from. The connector must be enabled and not paused.
    Replace or remove a poor result with the logo endpoints."""
    info = registry.retailer(retailer_id)
    if not info or not connector_state.is_enabled(db, retailer_id):
        raise HTTPException(404, "Connector not found or not enabled")
    if not logos.site_origin(info):
        raise HTTPException(422, "This connector names no website to fetch a logo from; upload one")
    payload = {"retailer_id": retailer_id}
    pending = next((j for j in db.scalars(select(Job).where(
        Job.kind == "logo_fetch", Job.state.in_(["queued", "running"]))) if j.payload == payload), None)
    if pending:
        return {"job_id": pending.id, "status": pending.state}
    job = Job(owner_id=p.user.id, kind="logo_fetch", payload=payload)
    db.add(job)
    db.flush()
    audit(db, p.user.id, "connector.logo_fetch_queued", "connector", retailer_id)
    db.commit()
    return {"job_id": job.id, "status": job.state}


@app.post("/v1/admin/connectors/{retailer_id}/stores/remove-unused")
def remove_unused_stores(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Remove a connector's stores that nothing uses: no price history, offers or monitors, and not a
    store the connector itself declares. Their entries in people's "my stores" go with them. Use it to
    start over before running "Find stores" again for particular places."""
    keep = db.scalar(select(func.count()).select_from(Store).where(Store.retailer_id == retailer_id)) or 0
    gone = connector_admin.removable_stores(db, retailer_id)
    removed = connector_admin.delete_stores(db, gone)
    audit(db, p.user.id, "stores.removed_unused", "connector", retailer_id, {"removed": removed})
    db.commit()
    return {"removed": removed, "kept": keep - removed}


@app.delete("/v1/admin/stores/{store_id}", status_code=204)
def remove_store(store_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Remove one store. Refused (409) when it has price history, offers or monitors, or when its
    connector declares it."""
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "Store not found")
    if not any(s.id == store.id for s in connector_admin.removable_stores(db, store.retailer_id)):
        raise HTTPException(409, "This store has price history, offers or monitors, or its connector "
                                 "declares it, so it cannot be removed")
    connector_admin.delete_stores(db, [store])
    audit(db, p.user.id, "store.removed", "store", store_id, {"name": store.name})
    db.commit()


@app.delete("/v1/admin/connectors/{retailer_id}/logo", status_code=204)
def remove_retailer_logo(retailer_id: str, db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Remove a retailer's uploaded logo (the coloured badge is used again)."""
    row = db.get(RetailerLogo, retailer_id)
    if row:
        audit(db, p.user.id, "connector.logo_removed", "connector", retailer_id)
        db.delete(row)
        db.commit()


@app.get("/logos/{retailer_id}")
def retailer_logo(retailer_id: str, db: Session = Depends(get_db)):
    """A retailer's uploaded logo (WebP). No login, so a page can show it in an <img> tag; the logo
    is the same for everyone. The `v` in the link changes whenever the logo does."""
    row = db.get(RetailerLogo, retailer_id)
    if not row:
        raise HTTPException(404, "No logo")
    return Response(row.data, media_type=row.content_type, headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/images/{image_id}")
def product_image(image_id: str, db: Session = Depends(get_db)):
    """A stored product picture (WebP). No login, because an <img> tag cannot send one: the id
    is unguessable, which is the only protection a picture someone uploaded has."""
    image = db.get(ProductImage, image_id)
    if not image:
        raise HTTPException(404, "Picture not found")
    return Response(image.data, media_type=image.content_type, headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@app.get("/v1/products/{product_id}/prices")
def product_prices(product_id: str, db: Session = Depends(get_db),
                         p: Principal = Depends(principal)):
    p.require("catalogue:read")
    if not db.get(Product, product_id):
        raise HTTPException(404, "Product not found")
    listings = db.scalars(select(Listing).where(Listing.product_id == product_id)).all()
    items = []
    for listing in listings:
        row = db.scalar(select(PriceObservation).where(
            PriceObservation.listing_id == listing.id, PriceObservation.account_id.is_(None),
            PriceObservation.eligibility == "public")
            .order_by(PriceObservation.observed_at.desc()).limit(1))
        if row:
            items.append(price_dict(row, listing))
    return {"items": items, "coverage": "observed_only"}


@app.get("/v1/products/{product_id}/history")
def product_history(product_id: str, limit: int = Query(200, ge=1, le=2000),
                    db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Public prices seen for this product in every store, newest first (`limit` of them, up to 2000)."""
    p.require("catalogue:read")
    if not db.get(Product, product_id):
        raise HTTPException(404, "Product not found")
    rows = db.execute(select(PriceObservation, Listing).join(Listing,
        PriceObservation.listing_id == Listing.id).where(
        Listing.product_id == product_id, PriceObservation.account_id.is_(None),
        PriceObservation.eligibility == "public")
        .order_by(PriceObservation.observed_at.desc()).limit(limit)).all()
    return {"items": [price_dict(observation, listing) for observation, listing in rows],
            "coverage": "observed_only"}


@app.post("/v1/admin/products/{product_id}/verified-gtin")
def verify_gtin(product_id: str, body: GtinMappingRequest,
                db: Session = Depends(get_db), p: Principal = Depends(admin)):
    """Administrator override: link a barcode to a product (same rules and provenance as a person
    linking one, recorded as source "admin")."""
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    try:
        set_barcode(db, product, clean_barcode(body.gtin), "admin", p.user.id)
    except BarcodeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BarcodeTaken as exc:
        raise HTTPException(409, "GTIN is already mapped") from exc
    except BarcodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(db, p.user.id, "product.gtin_verified", "product", product.id)
    db.commit()
    return {"id": product.id, "gtin14": product.gtin14, "verified": True}


@app.get("/v1/listings/{listing_id}/prices")
def prices(listing_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    listing = public_listing(db, listing_id)
    rows = db.scalars(select(PriceObservation).where(PriceObservation.listing_id == listing.id,
        PriceObservation.account_id.is_(None), PriceObservation.eligibility == "public")
        .order_by(PriceObservation.observed_at.desc()).limit(100)).all()
    return {"listing": {"id": listing.id, "name": listing.name, "store_id": listing.store_id,
                        "channel": listing.channel},
            "items": [price_dict(x, listing) for x in rows]}


@app.get("/v1/stores/{store_id}/offers")
def store_offers(store_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    rows = db.scalars(select(Offer).where(Offer.store_id == store_id,
        Offer.eligibility == "public").order_by(Offer.last_seen.desc()).limit(100)).all()
    return {"items": [offer_dict(x) for x in rows], "coverage": "observed_only"}


@app.get("/v1/stores/{store_id}/offers/history")
def store_offer_history(store_id: str, db: Session = Depends(get_db),
                              p: Principal = Depends(principal)):
    p.require("catalogue:read")
    rows = db.execute(select(OfferObservation, Offer).join(Offer,
        OfferObservation.offer_id == Offer.id).where(
        Offer.store_id == store_id, Offer.eligibility == "public")
        .order_by(OfferObservation.observed_at.desc()).limit(200)).all()
    return {"items": [{"offer_id": offer.id, "content_hash": observation.content_hash,
                       "title": observation.title, "terms": observation.terms,
                       "price": str(observation.price) if observation.price is not None else None,
                       "currency": observation.currency, "eligibility": observation.eligibility,
                       "valid_from": observation.valid_from, "valid_to": observation.valid_to,
                       "source_url": observation.source_url,
                       "observed_at": observation.observed_at} for observation, offer in rows],
            "coverage": "observed_only"}


@app.get("/v1/offers")
def offers(store_id: str | None = None, db: Session = Depends(get_db),
                 p: Principal = Depends(principal)):
    p.require("catalogue:read")
    query = select(Offer).where(Offer.eligibility == "public")
    if store_id:
        query = query.where(Offer.store_id == store_id)
    rows = db.scalars(query.order_by(Offer.last_seen.desc()).limit(100)).all()
    return {"items": [offer_dict(x) for x in rows], "coverage": "observed_only"}


@app.get("/v1/offers/{offer_id}")
def get_offer(offer_id: str, db: Session = Depends(get_db),
                    p: Principal = Depends(principal)):
    p.require("catalogue:read")
    row = db.get(Offer, offer_id)
    if not row or row.eligibility != "public":
        raise HTTPException(404, "Offer not found")
    return offer_dict(row)


@app.post("/v1/monitors", status_code=201)
def create_monitor(body: MonitorRequest, db: Session = Depends(get_db),
                         p: Principal = Depends(principal),
                         idempotency_key: str | None = Header(None)):
    p.require("monitors:write")
    digest = request_digest(body.model_dump())
    existing_id = idempotent_resource(db, p.user.id, "create_monitor", idempotency_key, digest)
    if existing_id:
        return monitor_dict(db.get(Monitor, existing_id), db)
    store = db.get(Store, body.store_id)
    if not store:
        raise HTTPException(404, "Store not found")
    retailer = registry.retailer(store.retailer_id)
    if not retailer or not connector_state.is_enabled(db, store.retailer_id):
        raise HTTPException(422, "The retailer's connector is disabled or not installed")
    if body.interval_minutes < retailer["minimum_monitor_interval_minutes"]:
        raise HTTPException(
            422, f"The minimum check interval for this retailer is "
                 f"{retailer['minimum_monitor_interval_minutes']} minutes")
    active_monitors = db.scalar(select(func.count()).select_from(Monitor).where(
        Monitor.owner_id == p.user.id, Monitor.enabled.is_(True)))
    if active_monitors >= settings.max_monitors_per_user:
        raise HTTPException(429, "Monitor limit reached; disable one before adding another")
    required = ("physical_store_offers" if body.channel == "physical" else "store_offers") if body.type == "store" else (
        "physical_store_prices" if body.channel == "physical" else "online_prices")
    if retailer["capabilities"].get(required) != "supported":
        raise HTTPException(422, "Selected connector does not support this operation")
    if body.type == "exact_product" and not body.conditions.get("listing_id"):
        raise HTTPException(422, "listing_id required")
    if body.type == "exact_product":
        listing = db.get(Listing, body.conditions["listing_id"])
        if not listing or listing.store_id != store.id or listing.channel != body.channel:
            raise HTTPException(422, "Listing is not in the selected store and channel")
    if body.type == "generic_product" and not str(body.conditions.get("query", "")).strip():
        raise HTTPException(422, "query required")
    if body.type == "generic_product" and retailer["capabilities"].get("product_search") != "supported":
        raise HTTPException(422, "This retailer cannot be searched, so a search monitor is not "
                                 "possible; add its products by their page link")
    if len(str(body.conditions.get("query", ""))) > 100:
        raise HTTPException(422, "query must be at most 100 characters")
    allowed_conditions = {
        "store": set(),
        "exact_product": {"listing_id", "maximum_price"},
        "generic_product": {"query", "brand", "excluded_terms", "maximum_price",
                            "minimum_package_weight_kg", "maximum_package_weight_kg",
                            "maximum_unit_price"},
    }
    if not set(body.conditions) <= allowed_conditions[body.type]:
        raise HTTPException(422, "Unsupported monitor condition")
    if "excluded_terms" in body.conditions and (not isinstance(body.conditions["excluded_terms"], list)
        or any(not isinstance(term, str) for term in body.conditions["excluded_terms"])):
        raise HTTPException(422, "excluded_terms must be a list of strings")
    if "brand" in body.conditions and not isinstance(body.conditions["brand"], str):
        raise HTTPException(422, "brand must be a string")
    for key in ("minimum_package_weight_kg", "maximum_package_weight_kg"):
        if key in body.conditions:
            try:
                value = Decimal(str(body.conditions[key]))
                if not value.is_finite() or value <= 0:
                    raise ValueError
            except (InvalidOperation, ValueError, TypeError):
                raise HTTPException(422, f"Invalid {key}") from None
    if "maximum_unit_price" in body.conditions:
        unit_limit = body.conditions["maximum_unit_price"]
        if not isinstance(unit_limit, dict) or unit_limit.get("unit") not in ("kg", "L", "count"):
            raise HTTPException(422, "Invalid maximum_unit_price unit")
        try:
            value = Decimal(str(unit_limit.get("amount")))
            if not value.is_finite() or value < 0:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(422, "Invalid maximum_unit_price amount") from None
    if "maximum_price" in body.conditions:
        try:
            value = Decimal(str(body.conditions["maximum_price"]))
            if not value.is_finite() or value < 0:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(422, "Invalid maximum_price") from None
    monitor = Monitor(owner_id=p.user.id, name=body.name, type=body.type,
                      store_id=body.store_id, channel=body.channel,
                      conditions=body.conditions, interval_minutes=body.interval_minutes,
                      notify_existing_offers=body.notify_existing_offers)
    db.add(monitor)
    db.flush()
    audit(db, p.user.id, "monitor.created", "monitor", monitor.id)
    remember_request(db, p.user.id, "create_monitor", idempotency_key, digest, monitor.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key is None:
            raise
        existing_id = idempotent_resource(db, p.user.id, "create_monitor", idempotency_key, digest)
        if not existing_id:
            raise
        return monitor_dict(db.get(Monitor, existing_id), db)
    return monitor_dict(monitor, db)


@app.post("/v1/monitors/from-url", status_code=202)
def add_product_from_url(body: ProductLinkRequest, response: Response,
                         db: Session = Depends(get_db), p: Principal = Depends(principal),
                         idempotency_key: str | None = Header(None)):
    """Start monitoring a product from its page link on a supported store.

    The link is only read for the store's own product id; the page is never fetched. A job looks
    the product up once and, if the store lists it, creates the product and its monitor together
    (poll `status_url`; the result holds `monitor_id` and the product). If you already monitor the
    product, the existing monitor is returned straight away with 200.
    """
    p.require("monitors:write")
    digest = request_digest(body.model_dump())
    existing_id = idempotent_resource(db, p.user.id, "add_product", idempotency_key, digest)
    if existing_id:
        job = db.get(Job, existing_id)
        return {"job_id": job.id, "status": job.state, "status_url": f"/v1/jobs/{job.id}"}
    try:
        threshold = parse_threshold(body.maximum_price)
        matches = [(info, sku) for info in registry.retailers() if info["product_urls"]
                   if (sku := match_product_url(info["product_urls"], body.url))]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not matches:
        supported = ", ".join(i["name"] for i in registry.retailers() if i["product_urls"])
        raise HTTPException(422, "That link is not a product page on a supported store"
                                 f" (supported: {supported or 'none'})")
    retailer, sku = matches[0]
    retailer_id = retailer["id"]
    if not connector_state.is_enabled(db, retailer_id):
        raise HTTPException(422, f"The {retailer['name']} connector is disabled or not installed")
    if retailer["capabilities"].get("online_prices") != "supported":
        raise HTTPException(422, f"{retailer['name']} online prices are not supported")
    if body.interval_minutes < retailer["minimum_monitor_interval_minutes"]:
        raise HTTPException(422, f"The minimum check interval for {retailer['name']} is "
                                 f"{retailer['minimum_monitor_interval_minutes']} minutes")
    if body.store_id:
        store = db.get(Store, body.store_id)
        if not store:
            raise HTTPException(404, "Store not found")
        if store.retailer_id != retailer_id:
            raise HTTPException(422, f"That link is a {retailer['name']} product, but the "
                                     "store you chose belongs to a different retailer")
    else:
        stores = db.scalars(select(Store).where(Store.retailer_id == retailer_id)).all()
        if len(stores) != 1:
            raise HTTPException(422, f"Prices differ by store: send store_id. {retailer['name']} has "
                                     f"{len(stores)} registered store(s)" + (
                                         "; register one first" if not stores else ""))
        store = stores[0]
    listing = db.scalar(select(Listing).where(
        Listing.retailer_id == retailer_id, Listing.sku == sku, Listing.store_id == store.id,
        Listing.channel == "online"))
    already = existing_monitor(db, p.user.id, store.id, listing.id) if listing else None
    if already:
        response.status_code = 200
        return {"already_monitored": True, "monitor": monitor_dict(already, db)}
    active = db.scalar(select(func.count()).select_from(Monitor).where(
        Monitor.owner_id == p.user.id, Monitor.enabled.is_(True)))
    if active >= settings.max_monitors_per_user:
        raise HTTPException(429, "Monitor limit reached; remove one before adding another")
    payload = {"retailer_id": retailer_id, "sku": sku, "store_id": store.id,
               "name": body.name, "interval_minutes": body.interval_minutes,
               "maximum_price": threshold, "notify_existing_offers": body.notify_existing_offers}
    pending = next((j for j in db.scalars(select(Job).where(
        Job.owner_id == p.user.id, Job.kind == "add_product",
        Job.state.in_(["queued", "running"]))) if j.payload == payload), None)
    if pending:
        return {"job_id": pending.id, "status": pending.state, "status_url": f"/v1/jobs/{pending.id}"}
    job = Job(owner_id=p.user.id, kind="add_product", payload=payload)
    db.add(job)
    db.flush()
    remember_request(db, p.user.id, "add_product", idempotency_key, digest, job.id)
    audit(db, p.user.id, "monitor.add_from_link_queued", "job", job.id, {"retailer": retailer_id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = idempotent_resource(db, p.user.id, "add_product", idempotency_key, digest)
        if not replay:
            raise
        job = db.get(Job, replay)
    return {"job_id": job.id, "status": job.state, "status_url": f"/v1/jobs/{job.id}"}


@app.get("/v1/monitors")
def list_monitors(include_removed: bool = False, db: Session = Depends(get_db),
                  p: Principal = Depends(principal)):
    """Your monitors. Removed ones are hidden unless `include_removed=true`."""
    p.require("monitors:read")
    query = select(Monitor).where(Monitor.owner_id == p.user.id)
    if not include_removed:
        query = query.where(Monitor.removed_at.is_(None))
    return {"items": [monitor_dict(x, db) for x in db.scalars(query.order_by(Monitor.name).limit(100))]}


@app.get("/v1/monitors/{monitor_id}")
def get_monitor(monitor_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:read")
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    return monitor_dict(row, db)


@app.get("/v1/monitors/{monitor_id}/events")
def monitor_events(monitor_id: str, db: Session = Depends(get_db),
                         p: Principal = Depends(principal)):
    p.require("events:read")
    monitor = db.get(Monitor, monitor_id)
    if not monitor or monitor.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    rows = db.scalars(select(Event).where(Event.monitor_id == monitor_id)
                      .order_by(Event.created_at.desc(), Event.id.desc()).limit(100)).all()
    return {"items": [event_dict(row) for row in rows]}


@app.delete("/v1/monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: str, permanent: bool = False, db: Session = Depends(get_db),
                   p: Principal = Depends(principal)):
    """Remove a monitor: it stops, leaves your list, and its events are kept (it can still be read
    by id or with `include_removed=true`). With `permanent=true` the monitor and its events are
    deleted for good. The product's price history is kept either way."""
    p.require("monitors:write")
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    if permanent:
        delete_monitor_permanently(db, row)
        audit(db, p.user.id, "monitor.deleted", "monitor", monitor_id)
    elif row.removed_at is None:
        row.enabled, row.removed_at = False, utcnow()
        audit(db, p.user.id, "monitor.removed", "monitor", row.id)
    db.commit()


def _monitor_product(db: Session, p: Principal, monitor_id: str) -> Product:
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    if row.type != "exact_product":
        raise HTTPException(422, "A barcode belongs to one specific product; this monitor follows "
                                 "a search or a whole store")
    listing = db.get(Listing, str(row.conditions.get("listing_id")))
    product = db.get(Product, listing.product_id) if listing and listing.product_id else None
    if not product:
        raise HTTPException(422, "This monitor has no product to attach a barcode to")
    return product


def _link_barcode(db: Session, p: Principal, request: Request, product: Product,
                  body: BarcodeLinkRequest, monitor_id: str | None = None):
    try:
        gtin14 = clean_barcode(body.gtin)
    except BarcodeError as exc:
        raise HTTPException(422, str(exc)) from exc
    merged = 0
    try:
        outcome = set_barcode(db, product, gtin14, "user", p.user.id)
    except BarcodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except BarcodeTaken as taken:
        if not body.merge:
            return JSONResponse(status_code=409, content={
                "code": "conflict", "request_id": request.state.request_id,
                "message": f"That barcode already belongs to '{taken.product.name}'. If they are "
                           "the same item, resend with merge=true.",
                "details": [{"existing_product": product_summary(db, taken.product)}]})
        try:
            merged = merge_products(db, product, taken.product)
        except BarcodeConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        product, outcome = taken.product, "merged"
    audit(db, p.user.id, "product.barcode_" + outcome, "product", product.id,
          {"gtin14": gtin14, "monitor_id": monitor_id, "listings_moved": merged})
    db.commit()
    return {"outcome": outcome, "merged_listings": merged, "product": product_summary(db, product)}


@app.post("/v1/monitors/{monitor_id}/barcode")
def link_barcode(monitor_id: str, body: BarcodeLinkRequest, request: Request,
                 db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Link the barcode you scanned to this monitor's product. The link is shared: it lets the
    product be found by barcode and compared with the same item in other stores.

    If the barcode already belongs to another product you get 409 naming it; resend with
    `merge: true` to say the two are the same item (your product's listings then move to it).
    A barcode published by the store itself cannot be changed.
    """
    p.require("monitors:write")
    return _link_barcode(db, p, request, _monitor_product(db, p, monitor_id), body, monitor_id)


@app.post("/v1/products/{product_id}/barcode")
def link_product_barcode(product_id: str, body: BarcodeLinkRequest, request: Request,
                         db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """The same as linking a barcode through a monitor, for a product you are not watching (for
    example one found by a search). Same rules: a barcode that already belongs to another product
    answers 409 unless you resend with `merge: true`, and a store-published barcode cannot be
    changed. The link is shared by everyone using the catalogue and is recorded with your name."""
    p.require("monitors:write")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    return _link_barcode(db, p, request, product, body)


@app.delete("/v1/monitors/{monitor_id}/barcode", status_code=204)
def unlink_barcode(monitor_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """Remove a barcode that was linked by a person (for example after a wrong scan). One the
    store publishes cannot be removed."""
    p.require("monitors:write")
    product = _monitor_product(db, p, monitor_id)
    gtin14 = product.gtin14
    try:
        clear_barcode(db, product)
    except BarcodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(db, p.user.id, "product.barcode_cleared", "product", product.id, {"gtin14": gtin14})
    db.commit()


@app.delete("/v1/products/{product_id}/barcode", status_code=204)
def unlink_product_barcode(product_id: str, db: Session = Depends(get_db),
                           p: Principal = Depends(principal)):
    """Remove a barcode that a person linked, for a product you are not watching. A barcode the
    store publishes cannot be removed."""
    p.require("monitors:write")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    gtin14 = product.gtin14
    try:
        clear_barcode(db, product)
    except BarcodeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(db, p.user.id, "product.barcode_cleared", "product", product.id, {"gtin14": gtin14})
    db.commit()


@app.get("/v1/me/stores")
def my_stores(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    """The stores you compare prices across (see POST /v1/compare/barcode)."""
    p.require("monitors:read")
    rows = db.execute(select(Store).join(UserStore, UserStore.store_id == Store.id)
                      .where(UserStore.user_id == p.user.id,
                             Store.retailer_id.in_(connector_state.enabled_ids(db)))
                      .order_by(Store.name)).scalars()
    return {"items": [{"store_id": s.id, "name": s.name, "retailer_id": s.retailer_id} for s in rows]}


@app.put("/v1/me/stores/{store_id}", status_code=204)
def add_my_store(store_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:write")
    if not db.get(Store, store_id):
        raise HTTPException(404, "Store not found")
    if db.get(UserStore, (p.user.id, store_id)):
        return
    count = db.scalar(select(func.count()).select_from(UserStore).where(UserStore.user_id == p.user.id))
    if count >= settings.max_my_stores:
        raise HTTPException(429, f"You can compare across at most {settings.max_my_stores} stores")
    db.add(UserStore(user_id=p.user.id, store_id=store_id))
    db.commit()


@app.delete("/v1/me/stores/{store_id}", status_code=204)
def remove_my_store(store_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:write")
    db.execute(delete(UserStore).where(UserStore.user_id == p.user.id, UserStore.store_id == store_id))
    db.commit()


@app.post("/v1/compare/barcode", status_code=202)
def compare_barcode(body: CompareRequest, response: Response, db: Session = Depends(get_db),
                    p: Principal = Depends(principal),
                    idempotency_key: str | None = Header(None)):
    """Standing in a store with a barcode: find the same item in your other stores and compare.

    The stores are `store_ids`, or else the ones in `GET /v1/me/stores` (plus `here_store_id`, the
    store you are standing in, which is listed first). You get what the catalogue already knows
    immediately (`known`, each with its age), and a job that checks every store live (`status_url`;
    one request per store, spaced and cached like any lookup, at most `max_compare_stores`
    stores). With `refresh: false` you get the known prices only, at once, with status 200.
    """
    p.require("catalogue:read")
    digest = request_digest(body.model_dump())
    existing_id = idempotent_resource(db, p.user.id, "compare", idempotency_key, digest)
    if existing_id:
        job = db.get(Job, existing_id)
        return {"job_id": job.id, "status": job.state, "status_url": f"/v1/jobs/{job.id}"}
    try:
        gtin14 = clean_barcode(body.gtin)
    except BarcodeError as exc:
        raise HTTPException(422, str(exc)) from exc
    ids = list(body.store_ids) if body.store_ids else [
        row.store_id for row in db.scalars(select(UserStore).where(UserStore.user_id == p.user.id))]
    if body.here_store_id:
        ids = [body.here_store_id] + [i for i in ids if i != body.here_store_id]
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise HTTPException(422, "Choose the stores to compare first: PUT /v1/me/stores/{store_id} "
                                 "for each, or pass store_ids")
    if len(ids) > settings.max_compare_stores:
        raise HTTPException(422, f"At most {settings.max_compare_stores} stores can be compared "
                                 "at once")
    stores = [db.get(Store, sid) for sid in ids]
    if any(store is None for store in stores):
        raise HTTPException(404, "Store not found")
    rows = known_rows(db, gtin14, stores)
    summary = summarize(rows, body.here_store_id)
    product = product_for_gtin(db, gtin14)
    shown = {"gtin14": gtin14, "here_store_id": body.here_store_id,
             "product": product_summary(db, product) if product else None,
             "known": rows, **summary}
    if not body.refresh:
        response.status_code = 200
        return shown
    pending = db.scalar(select(func.count()).select_from(Job).where(
        Job.owner_id == p.user.id, Job.kind.in_(LIVE_LOOKUP_KINDS),
        Job.state.in_(["queued", "running"])))
    if pending >= settings.max_pending_searches_per_user:
        raise HTTPException(429, "Too many lookups in progress", headers={"Retry-After": "60"})
    recent = db.scalar(select(func.count()).select_from(Job).where(
        Job.owner_id == p.user.id, Job.kind.in_(LIVE_LOOKUP_KINDS),
        Job.created_at >= utcnow() - timedelta(hours=1)))
    if recent >= settings.max_searches_per_hour:
        raise HTTPException(429, "Lookup limit reached for this hour", headers={"Retry-After": "600"})
    payload = {"gtin14": gtin14, "given": body.gtin, "store_ids": ids,
               "here_store_id": body.here_store_id}
    job = Job(owner_id=p.user.id, kind="compare", payload=payload)
    db.add(job)
    db.flush()
    remember_request(db, p.user.id, "compare", idempotency_key, digest, job.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = idempotent_resource(db, p.user.id, "compare", idempotency_key, digest)
        if not replay:
            raise
        job = db.get(Job, replay)
    return {"job_id": job.id, "status": job.state, "status_url": f"/v1/jobs/{job.id}", **shown}


@app.post("/v1/monitors/{monitor_id}/check", status_code=202)
def check_monitor(monitor_id: str, db: Session = Depends(get_db),
                        p: Principal = Depends(principal),
                        idempotency_key: str | None = Header(None)):
    p.require("monitors:write")
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id or not row.enabled:
        raise HTTPException(404, "Monitor not found")
    digest = request_digest({"monitor_id": monitor_id})
    existing_id = idempotent_resource(db, p.user.id, "check_monitor", idempotency_key, digest)
    if existing_id:
        job = db.get(Job, existing_id)
        return {"job_id": job.id, "status": job.state}
    pending = db.scalar(select(Job).where(Job.monitor_id == row.id,
        Job.state.in_(["queued", "running"])).order_by(Job.created_at.desc()).limit(1))
    if pending:
        remember_request(db, p.user.id, "check_monitor", idempotency_key, digest, pending.id)
        db.commit()
        return {"job_id": pending.id, "status": pending.state}
    last = db.scalar(select(Job.created_at).where(Job.monitor_id == row.id)
                     .order_by(Job.created_at.desc()).limit(1))
    if last:
        wait = settings.min_manual_check_interval_seconds - (utcnow() - aware(last)).total_seconds()
        if wait > 0:
            raise HTTPException(429, "This monitor was checked very recently",
                                headers={"Retry-After": str(int(wait) + 1)})
    job = Job(monitor_id=row.id, kind="scan")
    db.add(job)
    db.flush()
    remember_request(db, p.user.id, "check_monitor", idempotency_key, digest, job.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key is None:
            raise
        existing_id = idempotent_resource(db, p.user.id, "check_monitor", idempotency_key, digest)
        if not existing_id:
            raise
        job = db.get(Job, existing_id)
    return {"job_id": job.id, "status": job.state}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    job = db.get(Job, job_id)
    monitor = db.get(Monitor, job.monitor_id) if job and job.monitor_id else None
    if not job or (job.owner_id != p.user.id and (not monitor or monitor.owner_id != p.user.id)):
        raise HTTPException(404, "Job not found")
    p.require("catalogue:read" if job.kind in LIVE_LOOKUP_KINDS else "monitors:read")
    return {"id": job.id, "state": job.state, "result": job.result,
            "attempts": job.attempts, "created_at": job.created_at}


@app.get("/v1/admin/jobs")
def admin_jobs(db: Session = Depends(get_db), p: Principal = Depends(admin)):
    rows = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all()
    return {"items": [{"id": row.id, "kind": row.kind, "state": row.state,
                       "monitor_id": row.monitor_id, "owner_id": row.owner_id,
                       "attempts": row.attempts, "result": row.result,
                       "created_at": row.created_at} for row in rows]}


@app.post("/v1/admin/jobs/{job_id}/retry", status_code=202)
def admin_retry_job(job_id: str, db: Session = Depends(get_db),
                          p: Principal = Depends(admin)):
    row = db.get(Job, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    if row.state not in RETRYABLE_JOB_STATES:
        raise HTTPException(409, "Only finished jobs that did not succeed can be retried")
    row.state = "queued"
    row.attempts = 0
    row.lease_until = None
    row.run_after = utcnow()
    audit(db, p.user.id, "job.retried", "job", row.id)
    db.commit()
    return {"id": row.id, "state": row.state}


@app.get("/v1/events")
def events(after: str | None = None, limit: int = 100,
                 db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("events:read")
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    query = select(Event).where(Event.owner_id == p.user.id)
    if after:
        cursor = db.get(Event, after)
        if not cursor or cursor.owner_id != p.user.id:
            raise HTTPException(404, "Cursor not found")
        query = query.where(tuple_(Event.created_at, Event.id) >
                            tuple_(cursor.created_at, cursor.id))
    rows = db.scalars(query.order_by(Event.created_at, Event.id).limit(limit + 1)).all()
    page = rows[:limit]
    return {"items": [event_dict(row) for row in page],
            "next_cursor": page[-1].id if len(rows) > limit else None}


@app.post("/v1/webhooks", status_code=201)
def create_webhook(body: WebhookRequest, db: Session = Depends(get_db),
                   p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    try:
        validate_destination(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    secret = new_token()
    hook = Webhook(owner_id=p.user.id, url=body.url, secret_hash=hash_token(secret),
                   secret_ciphertext=encrypt_secret(secret))
    db.add(hook)
    db.flush()
    audit(db, p.user.id, "webhook.created", "webhook", hook.id)
    db.commit()
    return {"id": hook.id, "url": hook.url, "signing_secret": secret}


@app.get("/v1/webhooks")
def list_webhooks(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    return {"items": [{"id": x.id, "url": x.url, "enabled": x.enabled} for x in
                      db.scalars(select(Webhook).where(Webhook.owner_id == p.user.id))]}


@app.delete("/v1/webhooks/{hook_id}", status_code=204)
def delete_webhook(hook_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    hook = db.get(Webhook, hook_id)
    if not hook or hook.owner_id != p.user.id:
        raise HTTPException(404, "Webhook not found")
    hook.enabled = False
    audit(db, p.user.id, "webhook.disabled", "webhook", hook.id)
    db.commit()


@app.patch("/v1/webhooks/{hook_id}")
def update_webhook(hook_id: str, body: WebhookUpdate,
                         db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    hook = db.get(Webhook, hook_id)
    if not hook or hook.owner_id != p.user.id:
        raise HTTPException(404, "Webhook not found")
    hook.enabled = body.enabled
    audit(db, p.user.id, "webhook.updated", "webhook", hook.id)
    db.commit()
    return {"id": hook.id, "url": hook.url, "enabled": hook.enabled}


@app.get("/v1/webhooks/{hook_id}/deliveries")
def deliveries(hook_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    hook = db.get(Webhook, hook_id)
    if not hook or hook.owner_id != p.user.id:
        raise HTTPException(404, "Webhook not found")
    rows = db.scalars(select(Delivery).where(Delivery.webhook_id == hook_id)
                      .order_by(Delivery.next_attempt_at.desc()).limit(100)).all()
    return {"items": [{"id": x.id, "event_id": x.event_id, "state": x.state,
                       "attempts": x.attempts, "last_error": x.last_error} for x in rows]}


@app.post("/v1/webhooks/{hook_id}/deliveries/{delivery_id}/retry", status_code=202)
def retry_delivery(hook_id: str, delivery_id: str, db: Session = Depends(get_db),
                         p: Principal = Depends(principal)):
    p.require("webhooks:manage")
    hook = db.get(Webhook, hook_id)
    delivery = db.get(Delivery, delivery_id)
    if not hook or hook.owner_id != p.user.id or not delivery or delivery.webhook_id != hook_id:
        raise HTTPException(404, "Delivery not found")
    if delivery.state != "failed":
        raise HTTPException(409, "Only failed deliveries can be retried")
    delivery.state = "queued"
    delivery.attempts = 0
    delivery.last_error = None
    delivery.next_attempt_at = utcnow()
    audit(db, p.user.id, "delivery.retried", "delivery", delivery.id)
    db.commit()
    return {"id": delivery.id, "state": delivery.state}
