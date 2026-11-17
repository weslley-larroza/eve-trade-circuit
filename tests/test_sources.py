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
