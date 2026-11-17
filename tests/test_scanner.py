import pytest

import config
import scanner


def book(name, sells=None, buys=None):
    return scanner.HubBook(hub=name, station_id=0, sell_orders=sells or {}, buy_orders=buys or {})


def test_sales_tax_rate():
    assert config.sales_tax_rate(0) == pytest.approx(0.075)
    assert config.sales_tax_rate(5) == pytest.approx(0.075 * 0.45)


def test_walk_lots_stops_when_unprofitable():
    sell = [[100, 10], [150, 10]]          # compro aqui (crescente)
    buy = [[200, 5], [140, 100]]           # vendo aqui (decrescente)
    lots = scanner.walk_lots(sell, buy, tax_rate=0.0)
    # 5 un a 100→200, depois 5 un a 100→140; 150→140 já dá prejuízo
    assert [(l["buy_price"], l["sell_price"], l["qty"]) for l in lots] == [(100, 200, 5), (100, 140, 5)]


def test_walk_lots_applies_tax_on_sale():
    lots = scanner.walk_lots([[100, 1]], [[110, 1]], tax_rate=0.1)
    assert lots == []                      # 110 * 0.9 = 99 < 100
    lots = scanner.walk_lots([[100, 1]], [[120, 1]], tax_rate=0.1)
    assert lots[0]["unit_profit"] == pytest.approx(8.0)


def test_walk_lots_does_not_mutate_input():
    sell, buy = [[100, 10]], [[200, 10]]
    scanner.walk_lots(sell, buy, 0.0)
    assert sell == [[100, 10]] and buy == [[200, 10]]


def test_plan_leg_fills_by_density_and_respects_limits():
    a = book("A", sells={1: [[100, 1000]], 2: [[100, 1000]]})
    b = book("B", buys={1: [[200, 1000]], 2: [[150, 1000]]})
    volumes = {1: 10.0, 2: 1.0}            # item 2: 50 ISK/m³, item 1: 10 ISK/m³
    leg = scanner.plan_leg(a, b, 0.0, cargo_m3=100, budget_isk=1e9, volumes=volumes)
    assert leg["items"][0]["type_id"] == 2 and leg["items"][0]["qty"] == 100
    assert leg["cargo_used"] == pytest.approx(100)

    leg = scanner.plan_leg(a, b, 0.0, cargo_m3=1e9, budget_isk=1000, volumes=volumes)
    assert leg["isk_spent"] <= 1000


def test_best_circuit_order_prefers_profit_minus_jumps():
    hubs = ["A", "B", "C"]
    profit = {("A", "B"): 10, ("B", "C"): 10, ("C", "A"): 10,
              ("A", "C"): 100, ("C", "B"): 0, ("B", "A"): 0}
    jumps = {("A", "B"): 1, ("B", "C"): 1, ("A", "C"): 1}
    order, total, _ = scanner.best_circuit_order(hubs, profit, jumps)
    assert order == ["A", "C", "B"] and total == 100


def test_sales_tax_rate_rejects_invalid_accounting():
    with pytest.raises(ValueError):
        config.sales_tax_rate(10)       # daria taxa negativa e inflaria o lucro


def test_walk_lots_skips_buy_order_when_min_volume_unreachable():
    sell = [[100, 5]]
    buy = [[300, 100, 50], [200, 100, 1]]   # a melhor exige lote de 50
    lots = scanner.walk_lots(sell, buy, 0.0)
    assert [(l["sell_price"], l["qty"]) for l in lots] == [(200, 5)]


def test_walk_lots_uses_min_volume_order_when_supply_reaches_it():
    sell = [[100, 30], [110, 30]]
    buy = [[300, 100, 50]]
    lots = scanner.walk_lots(sell, buy, 0.0)
    assert sum(l["qty"] for l in lots) == 60


def test_walk_lots_min_volume_capped_by_remaining():
    # Ordem pede mínimo 50 mas só restam 10: vale vender as 10.
    lots = scanner.walk_lots([[100, 10]], [[300, 10, 50]], 0.0)
    assert sum(l["qty"] for l in lots) == 10
