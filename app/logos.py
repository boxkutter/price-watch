"""Retailer logos: saving one, and fetching one from the retailer's own website.

Fetching is an administrator's deliberate click, run by the worker like every other contact with a
retailer: one page (the home page) and a few icons, spaced out, over https, on the retailer's own
website (and its image hosts on the same domain), never anywhere else. It looks for the site's
touch icon or favicon, keeps the largest usable one, and says where it came from so a wrong or
tiny result can be replaced or removed.
"""
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app import connector_state
from app.config import settings
from app.connectors import registry
from app.connectors.base import AccessBlocked, ConnectorError, RateLimited
from app.connectors.http import PoliteClient, host_allowed
from app.images import BadImage, reduce_image
from app.models import Job, RetailerLogo, utcnow

LOGO_SIDE = 160
GOOD_ENOUGH = 120        # stop looking once an icon is this big (pixels on its longest side)
TOO_SMALL = 48           # smaller than this is a favicon, used only if nothing better exists
MAX_REQUESTS = 6         # the home page and up to five icons
MIN_INTERVAL = 2.0       # seconds between requests to the website

_transport = None        # a test seam: replaces the network


def save_logo(db: Session, retailer_id: str, data: bytes, width: int, height: int,
              user_id: str | None) -> RetailerLogo:
    row = db.get(RetailerLogo, retailer_id)
    if row is None:
        row = RetailerLogo(retailer_id=retailer_id)
        db.add(row)
    row.content_type, row.data, row.width, row.height = "image/webp", data, width, height
    row.updated_by, row.updated_at = user_id, utcnow()
    return row


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            link = {k: (v or "") for k, v in attrs}
            rel = link.get("rel", "").lower().split()
            if any(r in ("icon", "apple-touch-icon", "apple-touch-icon-precomposed") for r in rel):
                self.links.append({**link, "rel": rel})


def icon_candidates(html: str, page_url: str) -> list[tuple[int, str]]:
    """Icon links on a page, best first: touch icons before favicons, bigger before smaller. SVG
    icons are skipped (they cannot be reduced here). The usual file names are tried last."""
    parser = _IconLinks()
    parser.feed(html[:400_000])
    found: dict[str, int] = {}
    for link in parser.links:
        href = link.get("href", "").strip()
        if not href or href.startswith("data:"):
            continue
        url = urljoin(page_url, href)
        if urlsplit(url).scheme != "https" or urlsplit(url).path.lower().endswith(".svg") \
                or "svg" in link.get("type", "").lower():
            continue
        sizes = [int(n) for n in "".join(c if c.isdigit() else " " for c in link.get("sizes", "")).split()]
        touch = any(r.startswith("apple-touch-icon") for r in link["rel"])
        found.setdefault(url, (1000 if touch else 0) + (max(sizes) if sizes else 0))
    for path, score in (("/apple-touch-icon.png", 900), ("/favicon.ico", 1)):
        found.setdefault(urljoin(page_url, path), score)
    return sorted(((score, url) for url, score in found.items()), reverse=True)


def site_origin(info: dict) -> str | None:
    """The retailer's own website, taken from what its manifest already says about it: the host of a
    product page example, else a plain allowed host. None when it declares neither."""
    hosts = [urlsplit(rule["example"]).hostname for rule in info.get("product_urls", [])
             if rule.get("example")]
    hosts += [h for h in info.get("allowed_hosts", []) if not h.startswith("*.")
              and not h.startswith(("api.", "external."))]
    host = next((h for h in hosts if h), None)
    return f"https://{host}/" if host else None


def _allowed_hosts(home: str, info: dict) -> list[str]:
    host = urlsplit(home).hostname or ""
    labels = host.split(".")
    site = ".".join(labels[-2:]) if len(labels) >= 2 else host
    return sorted({host, site, f"*.{site}", *[h for h in info.get("allowed_hosts", [])]})


def _size_of(raw: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            return picture.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def process_logo_fetch(db: Session, job: Job) -> dict:
    retailer_id = (job.payload or {}).get("retailer_id", "")
    info = registry.retailer(retailer_id)
    home = site_origin(info) if info else None
    if not info or not home:
        return {"status": "unsupported", "detail": "This connector names no website to fetch a logo from"}
    refused = connector_state.refusal(db, retailer_id, info.get("max_requests_per_day", 10**9))
    if refused:
        return {"status": refused[0], "detail": refused[1]}
    client = PoliteClient(_allowed_hosts(home, info), MIN_INTERVAL, timeout=10, user_agent=settings.connector_user_agent, transport=_transport,
                          follow_redirects=True)
    client.begin_scan(MAX_REQUESTS)
    best: tuple[int, bytes, str, tuple[int, int]] | None = None     # (longest side, bytes, url, size)
    tried = 0
    try:
        page = client.get_text(home)
        for _score, url in icon_candidates(page, home):
            if tried >= MAX_REQUESTS - 1:
                break
            if not _host_ok(client, url):
                continue                                   # an icon on another site: not ours to fetch
            tried += 1
            try:
                raw = client.request_bytes("GET", url, headers={"Accept": "image/*"})
            except (AccessBlocked, RateLimited):
                raise
            except ConnectorError:
                continue
            size = _size_of(raw)
            if size and (best is None or max(size) > best[0]):
                best = (max(size), raw, url, size)
            if best and best[0] >= GOOD_ENOUGH:
                break
    except RateLimited:
        return {"status": "rate_limited", "detail": "The website asked us to slow down; try again later"}
    except AccessBlocked as exc:
        return {"status": "access_blocked", "detail": f"The website refused the request ({exc})"}
    except ConnectorError as exc:
        return {"status": "failed", "detail": f"Could not read the website: {exc}"}
    finally:
        connector_state.add_requests(db, retailer_id, client.requests_made)
    if best is None:
        return {"status": "not_found", "detail": "No usable icon was found on the website (SVG icons "
                                                 "cannot be used). Upload a logo instead."}
    try:
        data, width, height = reduce_image(best[1], LOGO_SIDE, True)
    except BadImage:
        return {"status": "not_found", "detail": "The icon could not be read. Upload a logo instead."}
    save_logo(db, retailer_id, data, width, height, job.owner_id)
    return {"status": "success", "source_url": best[2], "width": best[3][0], "height": best[3][1],
            "small": best[0] < TOO_SMALL}


def _host_ok(client: PoliteClient, url: str) -> bool:
    return host_allowed(urlsplit(url).hostname or "", client.allowed_hosts)
