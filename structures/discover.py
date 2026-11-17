"""
Descoberta de estruturas candidatas — junta várias fontes, porque nenhuma
sozinha é completa:

  public   /universe/structures?filter=market   (públicas com mercado, universo todo)
  orders   location_id de ordens no feed regional das regiões do escopo
           (ordens de compra com alcance aparecem lá mesmo estando em estrutura)
  assets   onde o personagem tem itens (esi-assets.read_assets.v1)
  corp     /corporations/{id}/structures (esi-corporations.read_structures.v1
           + cargo Station Manager; sem o cargo a ESI dá 403 e a fonte é pulada)
  search   busca por nome (esi-search.search_structures.v1)
  manual   local_settings.json → manual_structure_ids

Ser candidata não garante acesso: o próximo passo é structures.access.probe(),
que também descobre a região de cada uma e filtra pelo escopo.

Escopo de regiões = região onde o personagem está + extra_region_ids do
local_settings.json. (Raio em jumps e alcance de jump freighter: Fase 2.)
"""
from __future__ import annotations

from typing import Callable, Iterable

import esi_client
import local_settings
from auth import sso
from structures.access import is_structure_id

Log = Callable[[str], None]


def character_region(character_id: int) -> int | None:
    """Região onde o personagem está agora (esi-location.read_location.v1)."""
    if not sso.has_scope("esi-location.read_location.v1", character_id):
        return None
    loc = esi_client._get(f"characters/{character_id}/location", token=sso.get_access_token(character_id)).json()
    return esi_client.get_system_info(loc["solar_system_id"])["region_id"]


def scope_regions(character_id: int | None, log: Log = print) -> set[int]:
    regions = {int(r) for r in local_settings.load()["extra_region_ids"]}
    if character_id:
        try:
            here = character_region(character_id)
            if here:
                regions.add(here)
        except (esi_client.ESIError, sso.SSOError) as exc:
            log(f"Não deu pra ler a localização do personagem: {exc}")
    return regions


def from_public() -> set[int]:
    return set(esi_client._get("universe/structures", params={"filter": "market"}).json())


def from_region_orders(region_orders: Iterable[dict]) -> set[int]:
    return {o["location_id"] for o in region_orders if is_structure_id(o["location_id"])}


def from_assets(character_id: int) -> set[int]:
    if not sso.has_scope("esi-assets.read_assets.v1", character_id):
        return set()
    assets = esi_client.get_paged(f"characters/{character_id}/assets", token=sso.get_access_token(character_id))
    return {a["location_id"] for a in assets
            if a.get("location_type") == "item" and a.get("location_flag") == "Hangar"
            and is_structure_id(a["location_id"])}


def from_corp(character_id: int) -> set[int]:
    if not sso.has_scope("esi-corporations.read_structures.v1", character_id):
        return set()
    corp_id = esi_client._get(f"characters/{character_id}").json()["corporation_id"]
    try:
        rows = esi_client.get_paged(f"corporations/{corp_id}/structures", token=sso.get_access_token(character_id))
    except esi_client.ESIError as exc:
        if exc.status == 403:   # sem cargo Station Manager
            return set()
        raise
    return {s["structure_id"] for s in rows}


def from_search(character_id: int, terms: Iterable[str]) -> set[int]:
    if not sso.has_scope("esi-search.search_structures.v1", character_id):
        return set()
    found: set[int] = set()
    for term in terms:
        if len(term.strip()) < 3:
            continue
        r = esi_client._get(f"characters/{character_id}/search",
                            params={"categories": "structure", "search": term.strip()},
                            token=sso.get_access_token(character_id))
        found.update(r.json().get("structure", []))
    return found


def discover(
    character_id: int | None,
    region_orders: Iterable[dict] = (),
    search_terms: Iterable[str] = (),
    include_public: bool = True,
    log: Log = print,
) -> dict[int, list[str]]:
    """{structure_id: [fontes]} de todas as fontes disponíveis."""
    sources: dict[int, list[str]] = {}

    def add(ids: set[int], tag: str) -> None:
        log(f"  fonte {tag}: {len(ids)} estrutura(s)")
        for i in ids:
            sources.setdefault(int(i), []).append(tag)

    add(from_region_orders(region_orders), "orders")
    add({int(i) for i in local_settings.load()["manual_structure_ids"]}, "manual")

    fetchers: list[tuple[str, Callable[[], set[int]]]] = []
    if include_public:
        fetchers.append(("public", from_public))
    if character_id:
        fetchers += [
            ("assets", lambda: from_assets(character_id)),
            ("corp", lambda: from_corp(character_id)),
            ("search", lambda: from_search(character_id, search_terms)),
        ]
    for tag, fn in fetchers:
        try:
            add(fn(), tag)
        except (esi_client.ESIError, sso.SSOError) as exc:
            log(f"  fonte {tag}: falhou ({exc})")
    return sources
