"""A broad search stays quick: stores are asked at the same time, one page each, with "Show more"."""
import copy
import threading

from app.config import settings
from app.db import SessionLocal
from app.models import Store
from app.search import Retrieved, SearchRun
from tests.test_citygross import products
from tests.test_politeness import job_of, search


def many(cg, count=10):
    """`count` distinct products that all match "havregryn"."""
    template = next(iter(products().values()))
    items = []
    for n in range(count):
        item = copy.deepcopy(template)
        item.update(id=f"9000{n}_ST", gtin=f"73000000000{n:02d}", name=f"Havregryn nr {n}", subtitle="")
        items.append(item)
    cg.site.items = items


def test_a_broad_search_gives_one_page_then_shows_more_on_request(cg, monkeypatch):
    monkeypatch.setattr(settings, "search_page_size", 4)
    many(cg)
    first = search(cg, query="havregryn")["result"]
    assert len(first["items"]) == 4
    scope = first["coverage"][0]
    assert scope["truncated"] is True and scope["next_skip"] == 4
    asked = [c for c in cg.site.calls if "SearchQuery" in c.url.params]
    assert len(asked) == 1 and asked[0].url.params["take"] == "4"          # one request, one page
    second = job_of(cg, cg.client.post("/v1/search/jobs", json={
        "query": "havregryn", "store_ids": [cg.store_id], "skip": {cg.store_id: scope["next_skip"]}}))["result"]
    names = {i["name"] for i in first["items"]} | {i["name"] for i in second["items"]}
    assert len(second["items"]) == 4 and len(names) == 8                      # the next four, none repeated
    last = job_of(cg, cg.client.post("/v1/search/jobs", json={
        "query": "havregryn", "store_ids": [cg.store_id], "skip": {cg.store_id: 8}}))["result"]
    assert len(last["items"]) == 2 and last["coverage"][0]["next_skip"] is None   # the end: nothing more to show


def test_a_search_that_fits_one_page_offers_no_more(cg):
    many(cg, 3)
    scope = search(cg, query="havregryn")["result"]["coverage"][0]
    assert scope["truncated"] is False and scope["next_skip"] is None


def test_the_skip_must_name_a_searched_store(cg):
    body = {"query": "havregryn", "store_ids": [cg.store_id]}
    assert cg.client.post("/v1/search/jobs", json={**body, "skip": {"other": 4}}).status_code == 422
    assert cg.client.post("/v1/search/jobs", json={**body, "skip": {cg.store_id: -1}}).status_code == 422


def test_swedish_is_asked_only_when_the_first_search_gave_little(cg, monkeypatch):
    cg.client.post("/v1/glossary", json={"term": "oat flakes", "canonical": "havregryn"})
    many(cg, 5)
    for item in cg.site.items:
        item["name"] = item["name"].replace("Havregryn", "Oat flakes")
    monkeypatch.setattr(settings, "search_translate_below", 3)          # five answers are plenty
    found = search(cg, query="oat flakes")["result"]
    assert found["query"]["also_searched_as"] is None
    assert {c.url.params["SearchQuery"] for c in cg.site.calls if "SearchQuery" in c.url.params} == {"oat flakes"}
    cg.site.calls.clear()
    monkeypatch.setattr(settings, "search_translate_below", 8)          # five is too few: ask in Swedish too
    found = search(cg, query="oat flakes")["result"]
    assert found["query"]["also_searched_as"] == "havregryn"


def test_a_search_out_of_time_says_which_stores_it_did_not_reach(cg, monkeypatch):
    monkeypatch.setattr(settings, "search_time_budget_seconds", -1)
    result = search(cg, query="havregryn")["result"]
    assert result["status"] == "partial" and result["items"] == []
    assert result["coverage"][0]["status"] == "timed_out" and "ran out of time" in result["coverage"][0]["detail"]
    assert not [c for c in cg.site.calls if "SearchQuery" in c.url.params]    # nothing was asked


def test_stores_of_different_retailers_are_asked_at_the_same_time(cg, monkeypatch):
    with SessionLocal.begin() as db:
        other = Store(retailer_id="se.coop", external_id="1", name="Coop Kista")
        db.add(other)
        db.flush()
        other_id = other.id
    meeting = threading.Barrier(2, timeout=5)          # both retailers must be in progress together
    threads = set()

    def stub(self, store_ids):
        threads.add(threading.get_ident())
        meeting.wait()
        return {s: Retrieved(s, outcome={"store_id": s, "status": "unsupported"}) for s in store_ids}

    monkeypatch.setattr(SearchRun, "retrieve_group", stub)
    result = job_of(cg, cg.client.post("/v1/search/jobs", json={
        "query": "havregryn", "store_ids": [cg.store_id, other_id]}))
    assert [c["store_id"] for c in result["result"]["coverage"]] == [cg.store_id, other_id]   # asked order kept
    assert len(threads) == 2
