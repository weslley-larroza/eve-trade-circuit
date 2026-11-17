"""
Livro de ofertas unificado: junta, para uma lista de Locations, as ordens
do feed regional (estações NPC e ordens com alcance) e — quando houver
login — o mercado das próprias estruturas.

Cada região é baixada UMA vez, não importa quantos locais estejam nela.
"""
from __future__ import annotations

from typing import Callable

import esi_client
from models import Location

Order = dict
Log = Callable[[str], None]


class OrderBookSource:
    """Baixa e guarda as ordens cruas por região e por estrutura."""

    def __init__(self, log: Log = print):
        self.log = log
        self.region_orders: dict[int, list[Order]] = {}

    def fetch_regions(self, region_ids: set[int]) -> None:
        for rid in sorted(region_ids):
            if rid in self.region_orders:
                continue
            self.log(f"Baixando ordens da região {rid}...")
            self.region_orders[rid] = esi_client.get_region_orders(rid, order_type="all")
            self.log(f"Região {rid}: {len(self.region_orders[rid])} ordens.")

    def orders_for(self, loc: Location) -> list[Order]:
        """Ordens localizadas no local (vendas e compras)."""
        return [o for o in self.region_orders.get(loc.region_id, []) if o["location_id"] == loc.id]


def collect(locations: list[Location], log: Log = print) -> tuple[OrderBookSource, dict[int, list[Order]]]:
    src = OrderBookSource(log)
    src.fetch_regions({loc.region_id for loc in locations})
    return src, {loc.id: src.orders_for(loc) for loc in locations}
