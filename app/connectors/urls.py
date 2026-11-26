"""Reading product links. A pasted URL is parsed, never fetched.

`normalize_product_url` turns whatever a person pasted into one canonical form that connector
manifests write their `product_urls` patterns against: `https://host/decoded/path`, with no query,
fragment, credentials or port. Anything else is refused, so a link can never point the service at
another host, and the only thing taken from it is a product id.
"""
import re
from urllib.parse import unquote, urlsplit

MAX_URL_LENGTH = 2000
_SKU = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")


def normalize_product_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH or re.search(r"[\x00-\x1f\x7f\s]", url):
        raise ValueError("That does not look like a product link")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("Paste the full https:// address of the product page")
    if parts.username or parts.password:
        raise ValueError("The link must not contain a username or password")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("That does not look like a product link") from exc
    if port not in (None, 443) or not parts.hostname:
        raise ValueError("That does not look like a product link")
    return f"https://{parts.hostname.rstrip('.').lower()}{unquote(parts.path)}"


def match_product_url(rules: list[dict], url: str) -> str | None:
    """The product id if one of a connector's rules matches the (normalised) URL, else None."""
    normalized = normalize_product_url(url)
    for rule in rules:
        found = re.search(rule["pattern"], normalized)
        if found and _SKU.match(found.group("sku") or ""):
            return found.group("sku")
    return None
