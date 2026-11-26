"""Reading product names: brand, size and core words, and what the search does with them."""
from decimal import Decimal

from app.naming import describe, display_name, normalize_brand, parse_query
from tests.test_politeness import AXA, search


def like(name, brand, value=None, unit=None, gtin=None):
    return describe(name, brand, Decimal(value) if value is not None else None, unit).like_key(gtin)


def test_the_same_item_written_differently_by_stores_gets_one_key():
    keys = {like("Havregryn Extra Fylliga 825G AXA", "AXA", 825, "g"),
            like("Havregryn Extra Fylliga AXA 825g", "AXA", 825, "g"),
            like("Havregryn Extra Fylliga Axa", "Axa", 825, "g")}       # size only in the data
    assert len(keys) == 1 and keys.pop()[0] == "name"
    # accents, case, symbols, a store's own spelling slip, and 1000 g against 1 kg
    assert like("Sol havregryn Salta Kvarn 600 g", "Salta Kvarn", 600, "g") == \
        like("Havregryn Sol SALTÅ KVARN 0,6kg", "SALTÅ KVARN", Decimal("0.6"), "kg")
    assert like("Havregryn Gyllenhammars 1000g", "GYLLENHAMARS", 1, "kg") == \
        like("Havregryn 1KG GYLLENHAMARS", "GYLLENHAMARS", 1000, "g")
    assert like("Havredryck Oatly 1L", "Oatly®", 1, "L") == like("Oatly Havredryck 1 l", "OATLY")


def test_different_items_never_get_the_same_key():
    a = like("Havregryn 1KG AXA", "AXA", 1, "kg")
    assert a != like("Havregryn 1KG GARANT", "GARANT", 1, "kg")             # brand
    assert a != like("Havregryn 750G AXA", "AXA", 750, "g")                 # size
    assert a != like("Fiberhavregryn 1KG AXA", "AXA", 1, "kg")              # what it is
    assert a != like("Havregryn Eko 1KG AXA", "AXA", 1, "kg")               # a variant
    assert like("Havregryn 1KG", None, 1, "kg") is None                     # brand unknown: no guess
    assert like("Havregryn AXA", "AXA") is None                             # size unknown: no guess


def test_a_known_barcode_decides_the_key():
    assert like("anything", None, gtin="07310130321085") == ("barcode", "07310130321085")


def test_the_typed_query_is_split_into_words_and_a_size():
    q = parse_query("Havredryck Oatly 500 ml")
    assert q.store_text == "Havredryck Oatly" and q.terms == ("havredryck", "oatly")
    assert q.size == (Decimal("0.5"), "L")
    assert parse_query("mjölk").size is None and parse_query("mjölk").terms == ("mjolk",)
    only_size = parse_query("1,5kg")
    assert only_size.store_text == "1,5kg" and only_size.terms == () and only_size.size
    q = parse_query("oatly 1l")
    assert q.accepts("Havredryck Oatly 1L", "Oatly", (Decimal(1), "L"))
    assert not q.accepts("Havredryck Oatly 500 ml", "Oatly", (Decimal("0.5"), "L"))   # other size
    assert q.accepts("Havredryck Oatly", "Oatly", None)                                # unknown size
    assert not q.accepts("Havredryck Alpro 1L", "Alpro", (Decimal(1), "L"))            # other brand
    assert parse_query("havregryn").accepts("Havregrynsgröt Elovena 210G", "ELOVENA", None)


def test_search_keeps_what_was_typed_and_says_what_it_dropped(cg):
    template = cg.site.items[0]
    cg.site.items = [
        template,                                                       # Havregryn 1,5KG AXA
        {**template, "id": "2_ST", "gtin": "7310130321122", "subtitle": "750G AXA",
         "descriptiveSize": "750G", "netContent": {"unitOfMeasure": 0, "value": 750}},
        {**template, "id": "3_ST", "gtin": "7310130321283", "subtitle": "1KG GYLLENHAMARS",
         "brand": "GYLLENHAMARS", "descriptiveSize": "1KG",
         "netContent": {"unitOfMeasure": 0, "value": 1000}}]
    everything = search(cg, query="havregryn")["result"]
    assert len(everything["items"]) == 3 and everything["coverage"][0]["filtered_out"] == 0
    by_size = search(cg, query="havregryn 1,5kg")["result"]
    assert [i["size"] for i in by_size["items"]] == ["1.5 kg"]
    assert by_size["coverage"][0]["filtered_out"] == 2 and by_size["query"]["size"] == "1.5 kg"
    (item,) = by_size["items"]
    assert item["brand"] == "AXA" and item["like_key"].startswith("barcode:")


def test_the_display_name_is_the_product_without_brand_and_size():
    assert display_name("Havregryn Extra Fylliga 825G AXA", "AXA") == "Havregryn Extra Fylliga"
    assert display_name("Havregryn Extra Fylliga AXA 825g", "AXA") == "Havregryn Extra Fylliga"
    assert display_name("Havredryck Barista Delight Oddlygood® 1L", "Oddlygood®") == "Havredryck Barista Delight"
    assert display_name("Taco Boats Soft Original 193G 8-PACK OLD EL PASO", "OLD EL PASO") == \
        "Taco Boats Soft Original"
    assert display_name("Fiberhavregryn 800G GARANT EKO", "GARANT EKO") == "Fiberhavregryn"
    assert display_name("Havregryn Gyllenhammars 1000g", "GYLLENHAMARS") == "Havregryn"   # spelling slip
    assert display_name("Oats&Orange Glutenfritt 180G GULLON", "GULLON") == "Oats&Orange Glutenfritt"
    assert display_name("Äpple Ingrid Marie CA140G KLASS 1", None) == "Äpple Ingrid Marie CA140G KLASS 1"
    assert display_name("Axa", "AXA") == "Axa"                                   # never empty


def test_a_multipack_is_its_own_part_and_keeps_different_packs_apart():
    taco = describe("Taco Boats Soft Original 193G 8-PACK OLD EL PASO", "OLD EL PASO", Decimal(193), "g")
    assert taco.pack == "8-pack" and "pack" not in taco.core and "8" not in taco.core
    assert describe("Ägg 6 pack", "X").pack == "6-pack"
    assert describe("Gröt 13 port SEMPER", "SEMPER").pack == "13 portioner"
    four = like("Taco Boats Soft Original 193G 4-PACK", "OLD EL PASO", 193, "g")
    assert four != like("Taco Boats Soft Original 193G 8-PACK", "OLD EL PASO", 193, "g")
    assert four != like("Taco Boats Soft Original 193G", "OLD EL PASO", 193, "g")


def test_narrowing_by_brand_pack_and_weight_range():
    from app.naming import make_narrow
    kg = lambda v: (Decimal(v), "kg")
    by_brand = make_narrow("axa", None, None, None, None, "g")
    assert by_brand.accepts("Havregryn 1KG AXA", "AXA", kg(1), None)
    assert not by_brand.accepts("Havregryn 1KG GARANT", "GARANT", kg(1), None)
    assert by_brand.accepts("Havregryn AXA 1KG", None, kg(1), None)      # no brand given: the name
    assert make_narrow("old el paso", None, None, None, None, "g").accepts("x", "OLD EL PASO", None, None)
    assert make_narrow("garant", None, None, None, None, "g").accepts("x", "GARANT EKO", None, None)
    pack = make_narrow(None, 8, 8, None, None, "g")
    assert pack.accepts("Taco 8-PACK", "X", None, 8) and not pack.accepts("Taco", "X", None, None)
    assert not pack.accepts("Taco 4-PACK", "X", None, 4)
    some = make_narrow(None, 4, 8, None, None, "g")                      # 4 to 8, ends included
    assert [some.accepts("t", "X", None, n) for n in (None, 2, 4, 6, 8, 12)] == [False, False, True, True, True, False]
    assert make_narrow(None, 6, None, None, None, "g").accepts("t", "X", None, 12)        # 6 or more
    assert make_narrow(None, None, 3, None, None, "g").accepts("t", "X", None, None)      # up to 3: a single item
    assert make_narrow(None, 1, 1, None, None, "g").accepts("Taco", "X", None, None)    # 1 = not a multipack
    grams = make_narrow(None, None, None, "400", "600", "g")                  # 400 to 600 g
    assert grams.accepts("a", "X", (Decimal("0.5"), "kg"), None)
    assert grams.accepts("a", "X", (Decimal("0.4"), "kg"), None)        # the ends are included
    assert not grams.accepts("a", "X", (Decimal("0.75"), "kg"), None)
    assert not grams.accepts("a", "X", (Decimal("0.5"), "L"), None)     # a volume is not a weight
    assert grams.accepts("a", "X", None, None)                          # unknown size: not ruled out
    assert make_narrow(None, None, None, "1", None, "l").accepts("a", "X", (Decimal("1.5"), "L"), None)
    assert not make_narrow(None, None, None, None, "50", "cl").accepts("a", "X", (Decimal(1), "L"), None)


def test_search_applies_the_narrowing_and_asks_the_store_for_the_brand_too(cg):
    template = cg.site.items[0]
    cg.site.items = [template,
        {**template, "id": "2_ST", "gtin": "7310130321122", "subtitle": "750G AXA",
         "descriptiveSize": "750G", "netContent": {"unitOfMeasure": 0, "value": 750}}]
    both = search(cg, query="axa")["result"]
    assert sorted(i["size"] for i in both["items"]) == ["1.5 kg", "750 g"]
    ranged = search(cg, query="axa", weight_min="1", weight_max="2", weight_unit="kg")["result"]
    assert [i["size"] for i in ranged["items"]] == ["1.5 kg"] and ranged["coverage"][0]["filtered_out"] == 1
    assert search(cg, query="axa", brand="Garant")["result"]["items"] == []
    search(cg, query="havregryn", brand="Gyllenhamars")
    assert cg.site.calls[-1].url.params["SearchQuery"] == "havregryn gyllenhamars"
    for bad in ({"weight_unit": "stone"}, {"weight_min": "abc"}, {"weight_min": "5", "weight_max": "2"},
                {"weight_min": "-1"}, {"pack_min": 0}, {"pack_min": 5, "pack_max": 2}):
        assert cg.client.post("/v1/search/jobs", json={"query": "axa", "store_ids": [cg.store_id],
                                                       **bad}).status_code == 422, bad


def test_a_bad_barcode_says_why(cg):
    for gtin, why in (("731013032108", "check digit"), ("73101303210", "8, 12, 13 or 14")):
        r = cg.client.post("/v1/search/jobs", json={"gtin": gtin, "store_ids": [cg.store_id]})
        assert r.status_code == 422 and why in r.json()["message"]
        assert any(why in d.get("message", "") for d in r.json()["details"])


def test_brands_are_written_one_way():
    shown = {"AXA": "AXA", "Axa": "AXA", "axa": "AXA", "ICA": "ICA",                  # short: acronym
             "GARANT EKO": "Garant Eko", "OLD EL PASO": "Old El Paso", "OATLY": "Oatly",
             "NESTLÉ": "Nestlé", "BELVITA": "Belvita", "START!": "Start!",
             "salta kvarn": "Salta Kvarn", "KELLOGG'S": "Kellogg's",
             "Kellogg's": "Kellogg's", "Oddlygood®": "Oddlygood®", "iKaffe": "iKaffe",
             "Love Made": "Love Made", "  ": None, None: None}
    for given, expected in shown.items():
        assert normalize_brand(given) == expected, given


def test_a_barcode_is_linked_to_a_product_without_watching_it(cg):
    template = cg.site.items[0]
    cg.site.items = [template, {**template, "id": "9_ST", "gtin": None, "name": "Havregryn",
                                "subtitle": "1,5KG AXA"}]
    items = search(cg, query="axa")["result"]["items"]
    plain = next(i for i in items if i["barcode"] is None)
    owned = next(i for i in items if i["barcode"])
    assert plain["likely_same"][0]["barcode"] == owned["barcode"]["gtin14"]
    assert plain["likely_same"][0]["differs"] == [] and owned["likely_same"] == []
    url = f"/v1/products/{plain['product_id']}/barcode"
    assert cg.client.post(url, json={"gtin": "731013032108"}).status_code == 422        # bad check digit
    assert cg.client.post("/v1/products/missing/barcode", json={"gtin": AXA}).status_code == 404

    clash = cg.client.post(url, json={"gtin": owned["barcode"]["gtin14"]})
    assert clash.status_code == 409 and clash.json()["details"][0]["existing_product"]["id"] == owned["product_id"]
    done = cg.client.post(url, json={"gtin": owned["barcode"]["gtin14"], "merge": True})
    assert done.status_code == 200 and done.json()["outcome"] == "merged"
    assert cg.client.get("/v1/monitors").json()["items"] == []                          # nothing is watched
    again = {i["listing_id"]: i for i in search(cg, query="axa")["result"]["items"]}
    assert {i["product_id"] for i in again.values()} == {owned["product_id"]}            # one product now
    assert all(i["barcode"] and i["likely_same"] == [] for i in again.values())

    # a barcode the store publishes cannot be changed, and one a person linked can be undone
    assert cg.client.post(f"/v1/products/{owned['product_id']}/barcode",
                          json={"gtin": "4006381333931"}).status_code == 409
    assert cg.client.delete(f"/v1/products/{owned['product_id']}/barcode").status_code == 409


def test_a_products_history_can_be_asked_for_in_more_or_fewer_points(cg):
    pid = search(cg, gtin=AXA)["result"]["items"][0]["product_id"]
    search(cg, gtin=AXA)
    both = cg.client.get(f"/v1/products/{pid}/history").json()["items"]
    assert len(both) == 2 and both[0]["observed_at"] >= both[1]["observed_at"]     # newest first
    assert len(cg.client.get(f"/v1/products/{pid}/history?limit=1").json()["items"]) == 1
    assert cg.client.get(f"/v1/products/{pid}/history?limit=0").status_code == 422
    assert cg.client.get(f"/v1/products/{pid}/history?limit=2001").status_code == 422
    assert cg.client.get(f"/v1/products/{pid}").json()["brand"] == "AXA"          # shown the one way
