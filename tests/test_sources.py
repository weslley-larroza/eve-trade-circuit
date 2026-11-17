from markets.sources import OrderBookSource, reaches
from models import Location

HUB = Location(id=60003760, name="Jita", region_id=10000002, system_id=30000142)


def order(loc_id, system_id, rng, is_buy=True):
    return {"location_id": loc_id, "system_id": system_id, "range": rng, "is_buy_order": is_buy,
            "type_id": 34, "price": 5.0, "volume_remain": 10, "min_volume": 1}


def test_reaches_by_range():
    assert reaches(order(HUB.id, HUB.system_id, "station"), HUB)
    assert not reaches(order(60000001, HUB.system_id, "station"), HUB)
    assert reaches(order(60000001, HUB.system_id, "solarsystem"), HUB)
    assert not reaches(order(60000001, 30000144, "solarsystem"), HUB)
    assert reaches(order(60000001, 30009999, "region"), HUB)
    assert reaches(order(60000001, HUB.system_id, "5"), HUB)       # mesmo sistema = 0 jumps
    assert not reaches(order(60000001, 30000144, "5"), HUB)        # sem grafo: não arrisca


def test_orders_for_never_borrows_sell_orders_from_elsewhere():
    src = OrderBookSource(log=lambda m: None)
    src.region_orders[HUB.region_id] = [
        order(60000001, 30009999, "region", is_buy=False),          # venda em outro lugar
        order(60000001, 30009999, "region", is_buy=True),           # compra com alcance região
    ]
    got = src.orders_for(HUB)
    assert len(got) == 1 and got[0]["is_buy_order"]


import esi_client
from markets import sources
from structures import access

STRUCT = Location(id=1_000_000_000_001, name="Perimeter - Tower", region_id=10000002,
                  system_id=30000144, kind="structure", category="blue")


def test_structure_orders_are_deduplicated_with_regional_feed():
    src = OrderBookSource(log=lambda m: None)
    shared = {**order(STRUCT.id, STRUCT.system_id, "region"), "order_id": 7}
    src.structure_orders[STRUCT.id] = [shared, {**order(STRUCT.id, STRUCT.system_id, "station", is_buy=False), "order_id": 8}]
    src.region_orders[STRUCT.region_id] = [shared]                     # mesma ordem aparece no feed regional
    got = src.orders_for(STRUCT)
    assert sorted(o["order_id"] for o in got) == [7, 8]


def test_structure_denied_at_download_is_dropped(monkeypatch):
    def deny(sid, token):
        raise esi_client.ESIError(403, "x")

    monkeypatch.setattr(esi_client, "get_structure_orders", deny)
    denied = []
    src = OrderBookSource(log=lambda m: None)
    src.region_orders[HUB.region_id] = []
    _, by_loc, used = sources.collect([HUB, STRUCT], log=lambda m: None, source=src,
                                      token_getter=lambda: "t", on_denied=denied.append)
    assert used == [HUB] and denied == [STRUCT.id] and STRUCT.id not in by_loc


def test_structure_locations_red_starts_untradable(monkeypatch):
    monkeypatch.setattr(access, "accessible_markets", lambda cid, regions: {
        1: {"name": "Amiga", "region_id": 10000002, "system_id": 1, "owner_id": 10},
        2: {"name": "Inimiga", "region_id": 10000002, "system_id": 1, "owner_id": 20},
    })
    cats = {10: "corp", 20: "red"}
    locs = sources.structure_locations(42, {10000002}, lambda owner, sid: cats[owner])
    by_name = {l.name: l for l in locs}
    assert by_name["Amiga"].tradable and not by_name["Inimiga"].tradable
    assert by_name["Inimiga"].warning
