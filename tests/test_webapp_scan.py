"""Scan completo do webapp com login e ESI simulados."""
import pytest

import webapp
from auth import sso
from jsoncache import JsonCache
from models import Location
from structures import access, discover, standings

REGION = 10000002
FRIEND = 1_000_000_000_001
ENEMY = 1_000_000_000_002


def o(oid, loc, type_id, price, qty, buy, rng="station"):
    return {"order_id": oid, "location_id": loc, "system_id": 30000142, "type_id": type_id, "price": price,
            "volume_remain": qty, "min_volume": 1, "is_buy_order": buy, "range": rng}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_settings", JsonCache(str(tmp_path / "s.json")))
    monkeypatch.setattr(webapp, "_state", webapp.ScanState())
    monkeypatch.setattr(webapp, "npc_hubs", lambda: [Location(id=60003760, name="Jita", region_id=REGION, system_id=30000142)])
    monkeypatch.setattr(webapp.esi_client, "get_region_orders", lambda rid, order_type="all": [
        o(1, 60003760, 34, 5.0, 1000, False),          # Tritanium barato em Jita
    ])
    monkeypatch.setattr(webapp.esi_client, "get_structure_orders", lambda sid, tok: {
        FRIEND: [o(2, FRIEND, 34, 9.0, 1000, True)],   # amiga compra caro
        ENEMY: [o(3, ENEMY, 34, 50.0, 1000, True)],    # inimiga paga muito mais
    }[sid])
    monkeypatch.setattr(webapp.esi_client, "resolve_type_names", lambda ids: {34: "Tritanium"})
    monkeypatch.setattr(sso, "active_character_id", lambda: 42)
    monkeypatch.setattr(sso, "get_access_token", lambda cid=None: "tok")
    monkeypatch.setattr(discover, "scope_regions", lambda cid, log=None: {REGION})
    monkeypatch.setattr(discover, "discover", lambda cid, region_orders=(), log=None: {FRIEND: ["corp"], ENEMY: ["public"]})
    monkeypatch.setattr(access, "probe", lambda *a, **k: {})
    monkeypatch.setattr(access, "accessible_markets", lambda cid, regions=None: {
        FRIEND: {"name": "Casa", "region_id": REGION, "system_id": 30000142, "owner_id": 1},
        ENEMY: {"name": "Base inimiga", "region_id": REGION, "system_id": 30000142, "owner_id": 2},
    })

    class FakeClassifier:
        def __init__(self, cid):
            pass

        def classify(self, owner, sid=None):
            return {1: "corp", 2: "red"}[owner]

    monkeypatch.setattr(standings, "Classifier", FakeClassifier)
    return webapp.app.test_client()


def test_scan_includes_structures_and_keeps_red_as_reference(app):
    webapp._run_scan(0, include_structures=True)
    s = app.get("/api/scan/status").get_json()
    assert s["status"] == "done", s["error"]
    cats = {l["name"]: (l["category"], l["tradable"]) for l in s["locations"]}
    assert cats == {"Jita": ("npc", True), "Casa": ("corp", True), "Base inimiga": ("red", False)}
    pairs = {(x["buy_hub"], x["sell_hub"]) for x in s["opportunities"]}
    assert pairs == {("Jita", "Casa")}            # a red paga mais, mas não entra

    # usuário marca a red para contabilizar: passa a aparecer
    s = app.post("/api/locations/toggle", json={"id": ENEMY, "tradable": True}).get_json()
    assert ("Jita", "Base inimiga") in {(x["buy_hub"], x["sell_hub"]) for x in s["opportunities"]}


def test_scan_without_structures_only_hubs(app):
    webapp._run_scan(0, include_structures=False)
    s = app.get("/api/scan/status").get_json()
    assert [l["name"] for l in s["locations"]] == ["Jita"]
