import hashlib
import hmac
import ipaddress
import json
import socket
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Delivery, Event, Webhook, utcnow
from app.security import decrypt_secret


def validate_destination(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ({"https", "http"} if settings.allow_http_webhooks else {"https"}):
        raise ValueError("HTTPS webhook URL required")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("Invalid webhook URL")
    host = parts.hostname.lower()
    if host in {part.strip().lower() for part in settings.webhook_private_hosts.split(",") if part.strip()}:
        return
    try:
        addresses = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise ValueError("Webhook host cannot be resolved") from exc
    if not addresses:
        raise ValueError("Webhook host cannot be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Private webhook destination is not allowed")


def signed_headers(secret: str, body: bytes, delivery_id: str, timestamp: int) -> dict[str, str]:
    message = f"{timestamp}.".encode() + body
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Retail-Timestamp": str(timestamp),
            "X-Retail-Delivery": delivery_id, "X-Retail-Signature": "v1=" + signature}


def send_delivery(db: Session, delivery: Delivery) -> None:
    hook = db.get(Webhook, delivery.webhook_id)
    event = db.get(Event, delivery.event_id)
    if not hook or not hook.enabled or not event:
        delivery.state = "cancelled"
        return
    delivery.attempts += 1
    try:
        validate_destination(hook.url)
        body = json.dumps({"id": event.id, "type": event.type,
                           "occurred_at": event.created_at.isoformat(), "data": event.payload},
                          separators=(",", ":")).encode()
        headers = signed_headers(decrypt_secret(hook.secret_ciphertext), body,
                                 delivery.id, int(time.time()))
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(hook.url, content=body, headers=headers)
        if 200 <= response.status_code < 300:
            delivery.state = "delivered"
            delivery.last_error = None
            return
        if response.status_code not in (408, 429) and response.status_code < 500:
            delivery.state = "failed"
        else:
            delivery.state = "retry_wait"
        delivery.last_error = f"HTTP {response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        delivery.state = "retry_wait" if isinstance(exc, httpx.HTTPError) else "failed"
        delivery.last_error = type(exc).__name__
    if delivery.state == "retry_wait":
        if delivery.attempts >= 8:
            delivery.state = "failed"
        else:
            delivery.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 2 ** delivery.attempts * 15))


def due_delivery(db: Session) -> Delivery | None:
    return db.scalar(select(Delivery).where(Delivery.state.in_(["queued", "retry_wait"]),
        Delivery.next_attempt_at <= utcnow()).order_by(Delivery.next_attempt_at)
        .limit(1).with_for_update(skip_locked=True))
