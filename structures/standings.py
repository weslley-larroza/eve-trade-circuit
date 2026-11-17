"""
Classifica o dono de uma estrutura em relação ao personagem logado:

    corp      → estrutura da sua própria corporação
    alliance  → de uma corp da sua aliança
    blue      → contato com standing > 0 (pessoal, da corp ou da aliança)
    red       → contato com standing < 0
    neutral   → nenhum dos anteriores

Ordem de decisão:
  1. overrides manuais em local_settings.json (estrutura > corp > aliança)
     — ex.: corp em guerra com você = "red", mesmo sem contato no jogo;
  2. mesma corp / mesma aliança;
  3. contatos: pessoal > corp > aliança; em cada nível, contato da corp
     dona vale antes do contato da aliança dela.
"""
from __future__ import annotations

import os
import time

import esi_client
import local_settings
from auth import sso
from jsoncache import JsonCache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_corp_cache = JsonCache(os.path.join(ROOT, "corp_cache.json"))
_contacts_cache = JsonCache(os.path.join(ROOT, "contacts_cache.json"))
CORP_TTL = 24 * 3600
CONTACTS_TTL = 3600

CATEGORIES = ("corp", "alliance", "blue", "neutral", "red")


def corp_info(corp_id: int) -> dict:
    """Nome + aliança de uma corporação (público, cache 24h)."""
    hit = _corp_cache.get(str(corp_id))
    if hit and time.time() - hit["fetched"] < CORP_TTL:
        return hit
    data = esi_client._get(f"corporations/{corp_id}").json()
    hit = {"name": data.get("name"), "ticker": data.get("ticker"),
           "alliance_id": data.get("alliance_id"), "fetched": time.time()}
    _corp_cache.set(str(corp_id), hit)
    return hit


class Classifier:
    """Carrega uma vez o contexto do personagem e classifica vários donos."""

    def __init__(self, character_id: int | None = None):
        self.character_id = character_id or sso.active_character_id()
        if not self.character_id:
            raise sso.SSOError("Faça login pra classificar estruturas.")
        me = esi_client._get(f"characters/{self.character_id}").json()
        self.corp_id = me["corporation_id"]
        self.alliance_id = me.get("alliance_id") or corp_info(self.corp_id).get("alliance_id")
        self.overrides = local_settings.load()["overrides"]
        self.contacts = self._load_contacts()

    def _load_contacts(self) -> list[dict[int, float]]:
        """[pessoal, corp, aliança] — cada um é {contact_id: standing}."""
        key = str(self.character_id)
        hit = _contacts_cache.get(key)
        if hit and time.time() - hit["fetched"] < CONTACTS_TTL:
            return [{int(k): v for k, v in lvl.items()} for lvl in hit["levels"]]

        sources = [
            ("esi-characters.read_contacts.v1", f"characters/{self.character_id}/contacts"),
            ("esi-corporations.read_contacts.v1", f"corporations/{self.corp_id}/contacts"),
            ("esi-alliances.read_contacts.v1",
             f"alliances/{self.alliance_id}/contacts" if self.alliance_id else None),
        ]
        levels: list[dict[int, float]] = []
        for scope, path in sources:
            lvl: dict[int, float] = {}
            if path and sso.has_scope(scope, self.character_id):
                try:
                    for c in esi_client.get_paged(path, token=sso.get_access_token(self.character_id)):
                        lvl[int(c["contact_id"])] = float(c["standing"])
                except esi_client.ESIError as exc:
                    print(f"  [standings] não deu pra ler {path}: {exc.status}")
            levels.append(lvl)

        _contacts_cache.set(key, {"fetched": time.time(),
                                  "levels": [{str(k): v for k, v in l.items()} for l in levels]})
        return levels

    def classify(self, owner_corp_id: int | None, structure_id: int | None = None) -> str:
        ov = self.overrides
        if structure_id is not None and str(structure_id) in ov["structures"]:
            return ov["structures"][str(structure_id)]
        if owner_corp_id is None:
            return "neutral"
        if str(owner_corp_id) in ov["corporations"]:
            return ov["corporations"][str(owner_corp_id)]

        owner_ally = corp_info(owner_corp_id).get("alliance_id")
        if owner_ally and str(owner_ally) in ov["alliances"]:
            return ov["alliances"][str(owner_ally)]

        if owner_corp_id == self.corp_id:
            return "corp"
        if owner_ally and owner_ally == self.alliance_id:
            return "alliance"

        for lvl in self.contacts:
            for entity in (owner_corp_id, owner_ally):
                if entity and entity in lvl:
                    s = lvl[entity]
                    return "blue" if s > 0 else "red" if s < 0 else "neutral"
        return "neutral"
