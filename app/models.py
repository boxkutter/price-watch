import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(120))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # user or client
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "operation", "request_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    request_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClientGrant(Base):
    __tablename__ = "client_grants"
    __table_args__ = (UniqueConstraint("client_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("credentials.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("retailer_id", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    retailer_id: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), default="SE")
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Stockholm")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(300), index=True)
    brand: Mapped[str | None] = mapped_column(String(200))
    gtin14: Mapped[str | None] = mapped_column(String(14), unique=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Where the barcode came from: "retailer" (published by the store; cannot be overridden),
    # "user" (scanned and linked by someone) or "admin". Shared by everyone using the catalogue.
    gtin_source: Mapped[str | None] = mapped_column(String(16))
    gtin_set_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    gtin_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    package_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    package_unit: Mapped[str | None] = mapped_column(String(12))


class UserStore(Base):
    """The stores a person compares prices across (their own stores)."""
    __tablename__ = "user_stores"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("retailer_id", "sku", "store_id", "channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    retailer_id: Mapped[str] = mapped_column(String(40), index=True)
    sku: Mapped[str] = mapped_column(String(120))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(300))


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (UniqueConstraint("listing_id", "run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    eligibility: Mapped[str] = mapped_column(String(16), default="public")
    account_id: Mapped[str | None] = mapped_column(String(36))
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    regular_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    unit_price_unit: Mapped[str | None] = mapped_column(String(12))
    availability: Mapped[str | None] = mapped_column(String(30))
    source_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("retailer_id", "store_id", "channel", "identity_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    retailer_id: Mapped[str] = mapped_column(String(40), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    identity_key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(400))
    terms: Mapped[str | None] = mapped_column(Text)
    eligibility: Mapped[str] = mapped_column(String(16), default="public")
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    content_hash: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="active")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferObservation(Base):
    __tablename__ = "offer_observations"
    __table_args__ = (UniqueConstraint("offer_id", "run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(400))
    terms: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    eligibility: Mapped[str | None] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorState(Base):
    """Operational state of one retailer connector, shared by the API and the worker."""
    __tablename__ = "connector_states"
    retailer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | paused
    # Administrator override of the manifest's default `enabled`; None means "use the default".
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requests_in_window: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectorPackage(Base):
    """A connector installed through the admin interface (built-in connectors have no row)."""
    __tablename__ = "connector_packages"
    retailer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    version: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str | None] = mapped_column(String(200))
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    installed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class ConnectorSetting(Base):
    """A value an administrator entered for a setting a connector declares (stored encrypted)."""
    __tablename__ = "connector_settings"
    retailer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="online")
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    notify_existing_offers: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    baseline_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when the owner removes the monitor: it stops, disappears from their list, and its events
    # are kept. Permanent deletion removes the row instead.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationState(Base):
    __tablename__ = "evaluation_states"
    __table_args__ = (UniqueConstraint("monitor_id", "subject_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitors.id"))
    subject_key: Mapped[str] = mapped_column(String(200))
    last_value: Mapped[str | None] = mapped_column(String(100))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    monitor_id: Mapped[str | None] = mapped_column(ForeignKey("monitors.id"), index=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("monitor_id", "subject_key", "type", "transition_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitors.id"))
    subject_key: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))
    transition_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    secret_hash: Mapped[str] = mapped_column(String(64))
    # Encrypted using an application key; rotate key with a migration before changing APP_SECRET.
    secret_ciphertext: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("event_id", "webhook_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id"))
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(200))


class ReferenceProduct(Base):
    """What an open reference database (Open Food Facts) says about a barcode. A cache: it is
    looked up once, politely, by the worker, and refreshed rarely. A miss is remembered too."""
    __tablename__ = "reference_products"
    gtin14: Mapped[str] = mapped_column(String(14), primary_key=True)
    source: Mapped[str] = mapped_column(String(30), default="openfoodfacts")
    status: Mapped[str] = mapped_column(String(12))                  # found | not_found
    name: Mapped[str | None] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(300))
    quantity_text: Mapped[str | None] = mapped_column(String(100))
    page_url: Mapped[str | None] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)             # categories, ingredients, ...
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProductImage(Base):
    """A reduced, compressed picture of a product. `source` says where it came from: the person's
    own upload (`user`, private to `owner_id` and preferred over the rest), the store's (`store`)
    or the reference database's (`reference`, with its credit). The id is unguessable because the
    picture is served without a login (an <img> tag cannot send one)."""
    __tablename__ = "product_images"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(12))
    content_type: Mapped[str] = mapped_column(String(30))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    credit: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProductAlias(Base):
    """A name a person gave a product besides the ones its store listings carry ("Oat drink" for
    Havredryck). Shared by everyone, like a barcode link, and recorded with who added it."""
    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("product_id", "folded"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    folded: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GlossaryEntry(Base):
    """A word people say one way and shops another (English `term`, Swedish `canonical`), added on
    top of the built-in list. Shared by everyone."""
    __tablename__ = "glossary_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    term: Mapped[str] = mapped_column(String(80), unique=True)
    canonical: Mapped[str] = mapped_column(String(80))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RetailerLogo(Base):
    """The logo an administrator uploaded for a retailer (reduced, transparency kept). Without one,
    clients draw a coloured badge from the retailer's name."""
    __tablename__ = "retailer_logos"
    retailer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(30))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobProgress(Base):
    """What a running job is doing right now ("Searching City Gross Höör"), so a client can show it.
    Written on its own connection while the job's transaction is open, hence no foreign key (it would
    wait for the job's lock) and a table of its own. Rows are tiny and purged when old."""
    __tablename__ = "job_progress"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    text: Mapped[str] = mapped_column(String(200))
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
