"""
Modelo de "local de mercado": estação NPC ou estrutura de jogador.

Tudo no scanner/webapp identifica locais por `id` (station_id ou
structure_id), nunca pelo nome — nomes de estrutura são escolhidos por
jogadores, podem repetir e mudar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import config

# Categoria em relação ao personagem. "npc" = estação NPC.
CATEGORIES = ("npc", "corp", "alliance", "blue", "neutral", "red")
WARN_CATEGORIES = {
    "neutral": "Estrutura neutra: o dono pode mudar o acesso (ACL) a qualquer momento.",
    "red": "Estrutura INIMIGA: risco de perder acesso com itens dentro (asset safety) e de camp no undock.",
}


@dataclass
class Location:
    id: int
    name: str
    region_id: int
    system_id: int
    kind: str = "npc"              # npc | structure
    category: str = "npc"          # ver CATEGORIES
    owner_id: int | None = None    # corp dona (estruturas)
    # Se entra nas contas como origem/destino de trade. O usuário pode
    # desmarcar qualquer local; red vem desmarcado por padrão
    # (config.RED_STRUCTURE_POLICY).
    tradable: bool = True
    sources: list[str] = field(default_factory=list)  # de onde foi descoberta

    @property
    def warning(self) -> str | None:
        return WARN_CATEGORIES.get(self.category)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["warning"] = self.warning
        return d


def default_tradable(category: str) -> bool:
    if category == "red":
        return config.RED_STRUCTURE_POLICY == "pass_through"
    return True


def npc_hubs() -> list[Location]:
    """Os 5 hubs NPC de config.HUBS como Locations (lista padrão)."""
    return [
        Location(id=cfg["station_id"], name=name, region_id=cfg["region_id"], system_id=cfg["system_id"])
        for name, cfg in config.HUBS.items()
    ]
