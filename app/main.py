import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audit import record as audit
from app.connectors.registry import RETAILERS
from app.db import get_db
from app.gtin import normalize_gtin
from app.models import (
    AuditLog,
    ClientGrant,
    Credential,
    Delivery,
    Event,
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
    Store,
    User,
    Webhook,
    aware,
    utcnow,
)
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


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
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
    details = [{"field": ".".join(map(str, error["loc"])), "type": error["type"]}
               for error in exc.errors()]
    return JSONResponse(status_code=422,
                        content={"code": "invalid_request", "message": "Invalid request",
                                 "details": details, "request_id": request.state.request_id})


ADMIN_PAGE = Path(__file__).resolve().parent.parent / "static" / "admin.html"


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(ADMIN_PAGE)


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


class WebhookRequest(BaseModel):
    url: str


class WebhookUpdate(BaseModel):
    enabled: bool


class SearchRequest(BaseModel):
    query: str | None = Field(default=None, min_length=2, max_length=100)
    gtin: str | None = None
    store_ids: list[str] = Field(min_length=1, max_length=5)
    channel: str = "online"

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


def monitor_dict(row: Monitor) -> dict:
    return {"id": row.id, "name": row.name, "type": row.type, "store_id": row.store_id,
            "channel": row.channel, "conditions": row.conditions, "enabled": row.enabled,
            "interval_minutes": row.interval_minutes,
            "last_success_at": row.last_success_at, "next_due_at": row.next_due_at,
            "baseline_complete": row.baseline_complete}


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
            "first_seen": row.first_seen, "last_seen": row.last_seen}


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


@app.get("/v1/retailers")
def retailers(p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": RETAILERS}


@app.get("/v1/connectors")
def connectors(p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": RETAILERS}


@app.get("/v1/connectors/{retailer_id}/health")
def connector_health(retailer_id: str, db: Session = Depends(get_db),
                           p: Principal = Depends(principal)):
    p.require("catalogue:read")
    retailer = next((item for item in RETAILERS if item["id"] == retailer_id), None)
    if not retailer:
        raise HTTPException(404, "Connector not found")
    last_job = db.scalar(select(Job).join(Monitor, Job.monitor_id == Monitor.id)
        .join(Store, Monitor.store_id == Store.id)
        .where(Store.retailer_id == retailer_id)
        .order_by(Job.created_at.desc()).limit(1))
    return {"id": retailer_id, "enabled": retailer["enabled"],
            "access_review": retailer["access_review"],
            "status": "disabled" if not retailer["enabled"] else (
                last_job.state if last_job else "no_checks"),
            "last_job_id": last_job.id if last_job else None,
            "last_result": last_job.result if last_job else None}


@app.get("/v1/stores")
def stores(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": [{"id": s.id, "name": s.name, "retailer_id": s.retailer_id,
                       "country": s.country} for s in db.scalars(select(Store))]}


@app.get("/v1/retailers/{retailer_id}/stores")
def retailer_stores(retailer_id: str, db: Session = Depends(get_db),
                          p: Principal = Depends(principal)):
    p.require("catalogue:read")
    return {"items": [{"id": s.id, "name": s.name, "retailer_id": s.retailer_id,
                       "country": s.country, "timezone": s.timezone} for s in
                      db.scalars(select(Store).where(Store.retailer_id == retailer_id))]}


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
    return {"id": product.id, "name": product.name, "brand": product.brand,
            "gtin14": product.gtin14, "verified": product.verified,
            "package_quantity": {"value": str(product.package_value), "unit": product.package_unit}
            if product.package_value is not None else None,
            "listings": [{"id": x.id, "retailer_id": x.retailer_id, "sku": x.sku,
                          "store_id": x.store_id, "channel": x.channel} for x in listings]}


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
def product_history(product_id: str, db: Session = Depends(get_db),
                          p: Principal = Depends(principal)):
    p.require("catalogue:read")
    if not db.get(Product, product_id):
        raise HTTPException(404, "Product not found")
    rows = db.execute(select(PriceObservation, Listing).join(Listing,
        PriceObservation.listing_id == Listing.id).where(
        Listing.product_id == product_id, PriceObservation.account_id.is_(None),
        PriceObservation.eligibility == "public")
        .order_by(PriceObservation.observed_at.desc()).limit(200)).all()
    return {"items": [price_dict(observation, listing) for observation, listing in rows],
            "coverage": "observed_only"}


@app.post("/v1/admin/products/{product_id}/verified-gtin")
def verify_gtin(product_id: str, body: GtinMappingRequest,
                      db: Session = Depends(get_db), p: Principal = Depends(admin)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    try:
        normalized = normalize_gtin(body.gtin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    other = db.scalar(select(Product.id).where(Product.gtin14 == normalized, Product.id != product_id))
    if other:
        raise HTTPException(409, "GTIN is already mapped")
    product.gtin14 = normalized
    product.verified = True
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
        return monitor_dict(db.get(Monitor, existing_id))
    store = db.get(Store, body.store_id)
    if not store:
        raise HTTPException(404, "Store not found")
    retailer = next((item for item in RETAILERS if item["id"] == store.retailer_id), None)
    if not retailer or not retailer["enabled"]:
        raise HTTPException(422, "Retailer connector has no verified enabled capability")
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
        return monitor_dict(db.get(Monitor, existing_id))
    return monitor_dict(monitor)


@app.get("/v1/monitors")
def list_monitors(db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:read")
    return {"items": [monitor_dict(x) for x in db.scalars(
        select(Monitor).where(Monitor.owner_id == p.user.id).limit(100))]}


@app.get("/v1/monitors/{monitor_id}")
def get_monitor(monitor_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:read")
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    return monitor_dict(row)


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
def delete_monitor(monitor_id: str, db: Session = Depends(get_db), p: Principal = Depends(principal)):
    p.require("monitors:write")
    row = db.get(Monitor, monitor_id)
    if not row or row.owner_id != p.user.id:
        raise HTTPException(404, "Monitor not found")
    row.enabled = False
    audit(db, p.user.id, "monitor.disabled", "monitor", row.id)
    db.commit()


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
    p.require("catalogue:read" if job.kind == "search" else "monitors:read")
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
    if row.state not in {"failed", "partial"}:
        raise HTTPException(409, "Only failed or partial jobs can be retried")
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
