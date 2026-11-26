"""Example Shop: the template connector. Copy this folder to start a new connector.

You write two things: `collect()` (fetch and parse what a request asks for) and, if the store
has a directory, `discover()`. The kit does the rest: argument checks, the request budget, mapping
every failure to a standard status, request counting and secret handling.

Rules a connector follows (the contract tests check them):
  - fetch only what the request asks for: a text query, specific SKUs, or specific barcodes
  - use self.client (host allowlist, spacing, no redirects), never your own HTTP client
  - skip a product whose price is unknown; never report an unknown price as 0
  - put secrets in headers, never in URLs or results
"""
from app.connectors.base import ExtractionError, HttpStatusError, PriceData, ScanRequest, StoreInfo
from app.connectors.kit import Collected, HttpJsonConnector, to_decimal, valid_gtin
from app.pricing import parse_size

BASE = "https://api.exampleshop.example.org"


def parse_product(raw: dict) -> PriceData | None:
    """One product record -> PriceData, or None when its price is unknown."""
    price = to_decimal(raw.get("price"))
    if not raw.get("sku") or price is None or price < 0:
        return None
    size = parse_size(raw.get("size"))  # understands "1,5KG", "450 g", "33CL", "6x33cl"; else None
    return PriceData(
        sku=str(raw["sku"]), name=str(raw.get("name") or raw["sku"]), current_price=price,
        regular_price=to_decimal(raw.get("regular_price")), currency=raw.get("currency") or "SEK",
        channel="online", source_url=f"https://exampleshop.example.org/p/{raw['sku']}",
        gtin=valid_gtin(raw.get("ean")),  # only a GTIN with a valid check digit is kept
        availability="in_stock" if raw.get("in_stock") is True else None,  # unknown stays unknown
        brand=raw.get("brand"),
        package_value=size[0] if size else None, package_unit=size[1] if size else None)


class ExampleShopConnector(HttpJsonConnector):
    id = "ex.exampleshop"
    store_id_pattern = r"\d+"  # how this shop writes store ids; anything else is rejected up front

    def _headers(self) -> dict:
        # require() raises AuthRequired when the setting is missing; the kit turns that into the
        # 'auth_required' status, so nothing is requested and nothing is counted as a failure.
        return {"Authorization": f"Bearer {self.require('EXAMPLESHOP_TOKEN')}"}

    def discover(self, **inputs) -> list[StoreInfo]:
        data = self.client.get_json(f"{BASE}/v1/stores", headers=self._headers())
        stores = [StoreInfo(str(s["id"]), str(s["name"])) for s in data.get("stores", [])
                  if s.get("id") and s.get("name")]
        if not stores:
            raise ExtractionError("The store directory was empty")
        return stores

    def collect(self, store: str, request: ScanRequest) -> Collected:
        found: dict[str, dict] = {}
        missing: list[str] = []
        if request.query:
            data = self.client.get_json(f"{BASE}/v1/products", {"q": request.query[:100],
                                        "store": store, "limit": 40}, self._headers())
            for raw in data.get("products", []):
                found[str(raw.get("sku"))] = raw
        for sku in request.skus:
            try:
                found[sku] = self.client.get_json(f"{BASE}/v1/products/{sku}", {"store": store},
                                                  self._headers())
            except HttpStatusError as exc:
                if exc.status != 404:
                    raise
                missing.append(sku)  # not found is unknown, not proof it was discontinued
        for gtin in request.gtins:
            data = self.client.get_json(f"{BASE}/v1/products", {"ean": gtin, "store": store},
                                        self._headers())
            hits = data.get("products", [])
            if hits:
                found[str(hits[0].get("sku"))] = hits[0]
            else:
                missing.append(gtin)
        prices = [price for raw in found.values() if (price := parse_product(raw))]
        return Collected(prices=prices, missing=tuple(missing), fetched=len(found),
                         skipped=len(found) - len(prices))
