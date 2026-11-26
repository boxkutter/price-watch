"""One item, several names: the shared word list, alternative names, and "probably the same"."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import glossary
from app.db import SessionLocal
from app.models import GlossaryEntry, Product, ProductAlias
from app.names import similarity
from app.naming import describe, parse_query
from tests.test_politeness import AXA, search


@pytest.fixture(autouse=True)
def clean_glossary():
    glossary.set_custom({})
    yield
    glossary.set_custom({})


def core(name, brand, value, unit):
    return describe(name, brand, Decimal(value), unit).core


def test_english_and_swedish_grocery_words_are_the_same_word():
    assert glossary.canonical_tokens("Oat Drink 1L") == ["havredryck", "1l"]
    assert glossary.canonical_tokens("Whole Milk") == ["standardmjolk"]      # a phrase beats its words
    assert glossary.canonical_tokens("Mjölk") == ["mjolk"]
    assert glossary.translate("oat drink 1l") == "havredryck 1l"
    assert glossary.translate("Mjölk fresh") == "Mjölk färsk"
    assert glossary.translate("havregryn") == "havregryn"
    assert core("Oat Drink 1L 1,5% OATLY", "OATLY", 1, "L") == core("Havredryck Oatly 1L", "Oatly", 1, "L")
    q = parse_query("oat drink")
    assert q.accepts("Havredryck Oatly 1L", "Oatly", None) and q.accepts("Oat Drink 1L OATLY", "OATLY", None)
    assert not q.accepts("Mjölk 1L", "Arla", None)


def test_added_words_work_like_the_built_in_ones():
    glossary.set_custom({"oat flakes": "havregryn", "pancake mix": "pannkaksmix"})
    assert glossary.translate("Oat flakes 1kg") == "havregryn 1kg"
    assert glossary.translate("pancake mix") == "pannkaksmix"
    assert parse_query("oat flakes").accepts("Havregryn 1,5KG AXA", "AXA", None)


def test_how_alike_two_names_are_and_what_differs():
    same, differs = similarity(core("Havredryck Oatly 1L", "Oatly", 1, "L"),
                               core("Oat Drink 1L 1,5% OATLY", "OATLY", 1, "L"))
    assert same == 1.0 and differs == []
    score, differs = similarity(core("Havredryck IKaffe Oatly 1L", "Oatly", 1, "L"),
                                core("Ikaffe Barista Edition 1L HAVREDRYCK 3% OATLY", "OATLY", 1, "L"))
    assert score >= 0.5 and differs == ["barista", "edition"]
    score, differs = similarity(core("Havredryck Barista Oatly 1L", "Oatly", 1, "L"),
                                core("Havredryck Oatly 1L", "Oatly", 1, "L"))
    assert score >= 0.5 and differs == ["barista"]                 # offered, and the difference shown
    assert similarity(core("Vaniljsås 250ML OATLY", "OATLY", 250, "ml"),
                      core("Havredryck Oatly 250ml", "Oatly", 250, "ml"))[0] == 0.0


def two_products(cg):
    """AXA oats with its barcode, and the same item at the store without one."""
    template = cg.site.items[0]
    cg.site.items = [template, {**template, "id": "9_ST", "gtin": None, "name": "Fin Havregryn",
                                "subtitle": "1,5KG AXA"}]
    items = search(cg, query="axa")["result"]["items"]
    return (next(i for i in items if i["barcode"] is None), next(i for i in items if i["barcode"]))


def test_probably_the_same_is_offered_with_what_differs_and_confirmed_without_a_barcode(cg):
    plain, owned = two_products(cg)
    (guess,) = plain["likely_same"]
    assert guess["product_id"] == owned["product_id"] and guess["differs"] == ["fin"]
    assert guess["barcode"] == owned["barcode"]["gtin14"] and owned["likely_same"] == []

    done = cg.client.post(f"/v1/products/{plain['product_id']}/same-as",
                          json={"product_id": owned["product_id"]})
    assert done.status_code == 200 and done.json()["outcome"] == "merged"
    assert done.json()["product"]["id"] == owned["product_id"]      # the one with the barcode is kept
    assert done.json()["product"]["barcode"]["gtin14"] == owned["barcode"]["gtin14"]
    assert cg.client.get("/v1/monitors").json()["items"] == []
    again = search(cg, query="axa")["result"]["items"]
    assert {i["product_id"] for i in again} == {owned["product_id"]}
    assert cg.client.post(f"/v1/products/{owned['product_id']}/same-as",
                          json={"product_id": owned["product_id"]}).json()["outcome"] == "unchanged"
    assert cg.client.post(f"/v1/products/{owned['product_id']}/same-as",
                          json={"product_id": "missing"}).status_code == 404


def test_two_products_with_different_barcodes_are_never_merged(cg):
    plain, owned = two_products(cg)
    cg.client.post(f"/v1/products/{plain['product_id']}/barcode", json={"gtin": "4006381333931"})
    clash = cg.client.post(f"/v1/products/{plain['product_id']}/same-as",
                           json={"product_id": owned["product_id"]})
    assert clash.status_code == 409 and "differ" in clash.json()["message"]


def test_a_merge_keeps_the_names_and_pictures_that_hang_off_the_product(cg):
    from tests.test_reference import picture
    plain, owned = two_products(cg)
    cg.client.post(f"/v1/products/{plain['product_id']}/names", json={"name": "Rolled oats"})
    cg.client.put(f"/v1/products/{plain['product_id']}/image", content=picture())
    cg.client.post(f"/v1/products/{plain['product_id']}/same-as", json={"product_id": owned["product_id"]})
    names = {n["name"] for n in cg.client.get(f"/v1/products/{owned['product_id']}/names").json()["items"]}
    assert "Rolled oats" in names
    assert cg.client.get(f"/v1/products/{owned['product_id']}").json()["image"]["source"] == "user"


def test_alternative_names_are_shared_listed_and_removed_by_their_author_or_an_admin(cg):
    _plain, owned = two_products(cg)
    url = f"/v1/products/{owned['product_id']}/names"
    listed = cg.client.get(url).json()["items"]
    assert [n["source"] for n in listed] == ["store", "store"] or {"store"} == {n["source"] for n in listed}
    added = cg.client.post(url, json={"name": "Rolled oats"})
    assert added.status_code == 201
    mine = next(n for n in added.json()["items"] if n["source"] == "user")
    assert mine["name"] == "Rolled oats"
    again = cg.client.post(url, json={"name": "rolled  OATS"})              # already goes by that name
    assert again.status_code == 200 and sum(n["source"] == "user" for n in again.json()["items"]) == 1
    assert cg.client.post(url, json={"name": "x"}).status_code == 422
    assert cg.client.post("/v1/products/missing/names", json={"name": "Oats"}).status_code == 404
    shown = next(i for i in search(cg, query="axa")["result"]["items"] if i["product_id"] == owned["product_id"])
    assert "Rolled oats" in [n["name"] for n in shown["also_called"]]
    assert cg.client.delete(f"{url}/{mine['id']}").status_code == 204
    assert cg.client.delete(f"{url}/{mine['id']}").status_code == 404
    with SessionLocal() as db:
        assert db.scalars(select(ProductAlias)).all() == []


def test_a_typed_alternative_name_finds_the_product_in_a_shop_that_words_it_differently(cg):
    _plain, owned = two_products(cg)
    assert search(cg, query="rolled oat flakes")["result"]["items"] == []    # nothing knows that name
    cg.client.post(f"/v1/products/{owned['product_id']}/names", json={"name": "Rolled oat flakes"})
    found = search(cg, query="rolled oat flakes")["result"]
    assert [i["via"] for i in found["items"]] == ["alternative name"]
    assert found["items"][0]["product_id"] == owned["product_id"]
    assert found["coverage"][0]["filtered_out"] == 0


def test_english_words_are_also_asked_in_swedish(cg):
    cg.client.post("/v1/glossary", json={"term": "oat flakes", "canonical": "havregryn"})
    found = search(cg, query="Oat flakes")["result"]
    assert found["query"]["also_searched_as"] == "havregryn" and found["items"]
    asked = [c.url.params.get("SearchQuery") for c in cg.site.calls if "SearchQuery" in c.url.params]
    assert "oat flakes" in asked and "havregryn" in asked


def test_the_shared_word_list_can_be_added_to_and_removed_by_its_author(cg):
    first = cg.client.get("/v1/glossary").json()["items"]
    assert any(e["term"] == "oat drink" and e["source"] == "builtin" for e in first)
    added = cg.client.post("/v1/glossary", json={"term": "Pancake Mix", "canonical": "Pannkaksmix"})
    assert added.status_code == 201
    mine = next(e for e in added.json()["items"] if e["source"] == "custom")
    assert (mine["term"], mine["canonical"]) == ("pancake mix", "pannkaksmix")
    assert glossary.translate("pancake mix") == "pannkaksmix"
    assert cg.client.post("/v1/glossary", json={"term": "pancake mix", "canonical": "x y"}).status_code == 409
    assert cg.client.post("/v1/glossary", json={"term": "oat drink", "canonical": "havredryck"}).status_code == 409
    assert cg.client.post("/v1/glossary", json={"term": "same", "canonical": "Same"}).status_code == 422
    assert cg.client.post("/v1/glossary", json={"term": "a<b", "canonical": "ab"}).status_code == 422
    assert cg.client.delete(f"/v1/glossary/{mine['id']}").status_code == 204
    assert glossary.translate("pancake mix") == "pancake mix"
    with SessionLocal() as db:
        assert db.scalars(select(GlossaryEntry)).all() == [] and db.scalars(select(Product)).all() is not None
    assert cg.client.delete(f"/v1/glossary/{mine['id']}").status_code == 404


def test_the_axa_barcode_constant_is_still_the_stores_own(cg):
    assert search(cg, gtin=AXA)["result"]["items"][0]["barcode"]["source"] == "retailer"
