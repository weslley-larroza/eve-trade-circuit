"""
Teste de acesso a estruturas de jogadores, com cache.

Não existe endpoint "estruturas que posso acessar": a única forma é tentar.
  GET /universe/structures/{id}   200 → docking ok (nome, sistema, dono)
                                  403 → sem docking access
                                  404 → estrutura não existe mais
  GET /markets/structures/{id}    200 → mercado acessível
                                  403 → sem acesso ao mercado / sem módulo

Cada 403/404 conta no error limit da ESI (100 por janela; estourou → 420 em
TODA chamada). Por isso:
  - o resultado fica em structure_access.json, POR PERSONAGEM (a ACL de
    uma estrutura pode liberar um personagem e barrar outro), com TTL
    (ok 6h, negado 24h, sumiu 7d — ver config.ACCESS_TTL_*);
  - o probe para quando gasta config.ACCESS_MAX_ERRORS_PER_WINDOW erros ou
    quando o orçamento restante da ESI fica baixo, e espera a janela virar.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Iterable

import config
import esi_client
from jsoncache import JsonCache

_cache = JsonCache(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "structure_access.json"))

STRUCTURE_ID_MIN = 1_000_000_000_000   # IDs de estrutura são > 1e12; estações NPC ~60M


def is_structure_id(location_id: int) -> bool:
    return int(location_id) >= STRUCTURE_ID_MIN


def load_cache(character_id: int) -> dict[str, dict]:
    """{structure_id: entrada} do personagem."""
    return dict(_cache.get(str(character_id), {}))


def _save_entry(character_id: int, structure_id: int, entry: dict) -> None:
    with _cache.lock:
        data = dict(_cache.get(str(character_id), {}))
        data[str(structure_id)] = entry
        _cache.set(str(character_id), data)


def mark_denied(character_id: int, structure_id: int) -> None:
    """A ESI negou o mercado agora (ACL mudou): registra como negada."""
    _save_entry(character_id, structure_id, {"status": "denied", "checked_at": time.time()})


def _ttl(entry: dict) -> int:
    return {
        "ok": config.ACCESS_TTL_OK,
        "denied": config.ACCESS_TTL_DENIED,
        "gone": config.ACCESS_TTL_GONE,
    }.get(entry.get("status"), 0)


def is_fresh(entry: dict | None, region_ids: set[int] | None = None) -> bool:
    if not entry or time.time() - entry.get("checked_at", 0) >= _ttl(entry):
        return False
    # Acesso ok mas mercado nunca testado (estava fora do escopo) e agora a
    # região entrou no escopo → precisa testar o mercado.
    if entry.get("status") == "ok" and entry.get("market") is None and region_ids is not None:
        return entry.get("region_id") not in region_ids
    return True


def probe_one(structure_id: int, token: str, region_ids: set[int] | None = None) -> tuple[dict, int]:
    """
    Testa uma estrutura. Retorna (entrada do cache, nº de erros gastos).
    Se region_ids for dado e a estrutura estiver fora dessas regiões, não
    testa o mercado (market=None) — economiza chamada e erro.
    """
    errors = 0
    entry: dict = {"checked_at": time.time()}

    r = esi_client.request("GET", f"universe/structures/{structure_id}", token=token, ok_statuses=(403, 404))
    if r.status_code == 404:
        return {**entry, "status": "gone"}, 1
    if r.status_code == 403:
        return {**entry, "status": "denied"}, 1

    info = r.json()
    entry.update({
        "status": "ok",
        "name": info.get("name"),
        "system_id": info.get("solar_system_id"),
        "region_id": esi_client.get_system_info(info["solar_system_id"])["region_id"],
        "owner_id": info.get("owner_id"),
        "type_id": info.get("type_id"),
        "market": None,
    })
    if region_ids is not None and entry["region_id"] not in region_ids:
        return entry, errors

    m = esi_client.request(
        "GET", f"markets/structures/{structure_id}", params={"page": 1}, token=token, ok_statuses=(403, 404)
    )
    entry["market"] = m.status_code == 200
    if m.status_code != 200:
        errors += 1
    return entry, errors


def probe(
    character_id: int,
    structure_ids: Iterable[int],
    token_getter: Callable[[], str],
    region_ids: set[int] | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, dict]:
    """
    Testa, para o personagem, as estruturas ainda não conhecidas (ou com
    cache vencido) e devolve o cache dele atualizado. Grava a cada passo,
    então pode ser interrompido sem perder o que já foi testado.
    """
    cache = load_cache(character_id)
    todo = [int(s) for s in dict.fromkeys(structure_ids)
            if force or not is_fresh(cache.get(str(s)), region_ids)]
    log(f"{len(todo)} estrutura(s) pra testar ({len(cache)} já no cache).")

    errors_in_window = 0
    window_started = time.time()

    for i, sid in enumerate(todo, 1):
        if should_stop():
            log("Teste de acesso interrompido.")
            break

        # Janela do error limit da ESI é de ~60s.
        if time.time() - window_started > 60:
            errors_in_window, window_started = 0, time.time()
        if errors_in_window >= config.ACCESS_MAX_ERRORS_PER_WINDOW or esi_client.error_budget_remaining() < 30:
            log("Orçamento de erros da ESI no limite — aguardando a janela virar...")
            esi_client.wait_error_window()
            errors_in_window, window_started = 0, time.time()

        try:
            entry, errs = probe_one(sid, token_getter(), region_ids)
        except esi_client.ESIError as exc:
            log(f"  {sid}: erro inesperado ({exc.status}), pulando.")
            errors_in_window += 1
            continue

        errors_in_window += errs
        _save_entry(character_id, sid, entry)

        if entry["status"] == "ok" and entry["market"] is not None:
            log(f"  [{i}/{len(todo)}] {entry['name']} — acesso OK, mercado: {'sim' if entry['market'] else 'não'}")
        elif i % 10 == 0:
            log(f"  [{i}/{len(todo)}] testadas...")

    return load_cache(character_id)


def accessible_markets(character_id: int, region_ids: set[int] | None = None) -> dict[int, dict]:
    """Estruturas com mercado acessível para o personagem (último teste)."""
    return {int(k): v for k, v in load_cache(character_id).items()
            if v.get("status") == "ok" and v.get("market")
            and (region_ids is None or v.get("region_id") in region_ids)}
