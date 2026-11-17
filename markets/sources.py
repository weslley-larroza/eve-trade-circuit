"""
Livro de ofertas unificado: junta, para uma lista de Locations, as ordens
do feed regional (estações NPC e ordens com alcance) e — quando houver
login — o mercado das próprias estruturas (/markets/structures/{id}).

Cada região e cada estrutura é baixada UMA vez, não importa quantos
locais dependam dela.
"""
from __future__ import annotations

from typing import Callable

import esi_client
from models import Location, default_tradable

Order = dict
Log = Callable[[str], None]


class OrderBookSource:
    """Baixa e guarda as ordens cruas por região e por estrutura."""

    def __init__(self, log: Log = print):
        self.log = log
        self.region_orders: dict[int, list[Order]] = {}
        self.structure_orders: dict[int, list[Order]] = {}

    def fetch_regions(self, region_ids: set[int]) -> None:
        for rid in sorted(region_ids):
            if rid in self.region_orders:
                continue
            self.log(f"Baixando ordens da região {rid}...")
            self.region_orders[rid] = esi_client.get_region_orders(rid, order_type="all")
            self.log(f"Região {rid}: {len(self.region_orders[rid])} ordens.")

    def fetch_structures(
        self,
        locations: list[Location],
        token_getter: Callable[[], str],
        on_denied: Callable[[int], None] = lambda sid: None,
    ) -> list[Location]:
        """
        Baixa o mercado de cada estrutura. Se a ESI negar (ACL mudou desde o
        teste de acesso), chama on_denied e devolve a estrutura na lista de
        falhas — ela não deve entrar no scan.
        """
        failed = []
        for loc in locations:
            if loc.kind != "structure" or loc.id in self.structure_orders:
                continue
            self.log(f"Baixando mercado de {loc.name}...")
            try:
                self.structure_orders[loc.id] = esi_client.get_structure_orders(loc.id, token_getter())
            except esi_client.ESIError as exc:
                if exc.status in (403, 404):
                    self.log(f"  {loc.name}: acesso negado agora ({exc.status}) — fica de fora.")
                    on_denied(loc.id)
                    failed.append(loc)
                    continue
                raise
        return failed

    def orders_for(self, loc: Location) -> list[Order]:
        """
        Ordens utilizáveis a partir do local:
          - vendas e compras localizadas nele (para estrutura, o feed dela);
          - compras de outros locais cujo alcance cobre este (ver reaches()).
        """
        own = self.structure_orders.get(loc.id, []) if loc.kind == "structure" else []
        seen = {o["order_id"] for o in own if "order_id" in o}
        regional = [o for o in self.region_orders.get(loc.region_id, [])
                    if (o["location_id"] == loc.id or (o["is_buy_order"] and reaches(o, loc)))
                    and o.get("order_id") not in seen]
        return own + regional


def reaches(order: Order, loc: Location) -> bool:
    """
    Uma ordem de compra aceita venda feita em `loc`?

      station      → só no próprio local da ordem
      solarsystem  → qualquer local do mesmo sistema
      region       → qualquer local da região
      "1".."40"    → até N jumps. Sem o grafo do universo (Fase 2) só dá
                     pra garantir o caso do mesmo sistema (0 jumps), então
                     por enquanto conta só esse — conservador: pode perder
                     oportunidade, nunca inventa uma.
    """
    if order["location_id"] == loc.id:
        return True
    rng = order.get("range", "station")
    if rng == "region":
        return True
    if rng == "station":
        return False
    return order.get("system_id") == loc.system_id


def structure_locations(
    character_id: int,
    region_ids: set[int],
    classify: Callable[[int | None, int], str],
    sources_by_id: dict[int, list[str]] | None = None,
) -> list[Location]:
    """Estruturas com mercado acessível no escopo, já classificadas."""
    from structures import access   # import tardio: evita ciclo sso ↔ sources

    locs = []
    for sid, info in sorted(access.accessible_markets(character_id, region_ids).items()):
        category = classify(info.get("owner_id"), sid)
        locs.append(Location(
            id=sid, name=info.get("name") or f"Estrutura {sid}",
            region_id=info["region_id"], system_id=info["system_id"],
            kind="structure", category=category, owner_id=info.get("owner_id"),
            tradable=default_tradable(category),
            sources=(sources_by_id or {}).get(sid, []),
        ))
    return locs


def collect(
    locations: list[Location],
    log: Log = print,
    source: OrderBookSource | None = None,
    token_getter: Callable[[], str] | None = None,
    on_denied: Callable[[int], None] = lambda sid: None,
) -> tuple[OrderBookSource, dict[int, list[Order]], list[Location]]:
    """
    Garante que regiões/estruturas dos locais estão baixadas e devolve
    (fonte, ordens por local, locais efetivamente usados).
    """
    src = source or OrderBookSource(log)
    src.fetch_regions({loc.region_id for loc in locations})
    failed: list[Location] = []
    if token_getter and any(l.kind == "structure" for l in locations):
        failed = src.fetch_structures(locations, token_getter, on_denied)
    used = [l for l in locations if l not in failed]
    return src, {loc.id: src.orders_for(loc) for loc in used}, used
