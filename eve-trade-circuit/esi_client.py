"""
Cliente simples para a ESI (EVE Swagger Interface) pública.
Não usa nenhum endpoint autenticado — só dados de mercado e de universo,
que são públicos.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterable

import requests

BASE_URL = "https://esi.evetech.net/latest"
# ESI pede um User-Agent identificável — não deixe genérico.
HEADERS = {
    "User-Agent": "eve-market-scanner (uso pessoal, contato: <preencha seu email/discord>)"
}

SYSTEM_CACHE_FILE = os.path.join(os.path.dirname(__file__), "system_cache.json")

# Reaproveitar conexão TCP entre requisições — bem mais rápido que abrir
# uma conexão nova a cada página, especialmente em regiões com muitas.
_session = requests.Session()
_session.headers.update(HEADERS)


def _get(url: str, params: dict | None = None) -> requests.Response:
    """GET com respeito ao rate limit da ESI (erro 420 = error limited)."""
    for attempt in range(5):
        resp = _session.get(url, params=params, timeout=20)

        # A ESI também avisa PROATIVAMENTE quando você está perto do limite
        # de erros (não precisa esperar tomar o 420 pra desacelerar).
        remain = resp.headers.get("X-ESI-Error-Limit-Remain")
        if remain is not None and int(remain) < 10:
            reset = int(resp.headers.get("X-ESI-Error-Limit-Reset", "5"))
            print(f"  [aviso] perto do limite de erros da ESI, pausando {reset}s...")
            time.sleep(reset)

        if resp.status_code == 420:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"  [rate limit] aguardando {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Falhou após múltiplas tentativas: {url}")


def get_region_orders(region_id: int, order_type: str = "all") -> list[dict]:
    """Baixa TODAS as páginas de ordens de uma região (todas as estações nela)."""
    orders: list[dict] = []
    page = 1
    total_pages = 1
    start = time.time()

    while page <= total_pages:
        resp = _get(
            f"{BASE_URL}/markets/{region_id}/orders/",
            params={"order_type": order_type, "page": page},
        )
        orders.extend(resp.json())
        total_pages = int(resp.headers.get("X-Pages", "1"))

        if page == 1 or page % 5 == 0 or page == total_pages:
            elapsed = time.time() - start
            print(f"  página {page}/{total_pages} — {len(orders)} ordens até agora ({elapsed:.0f}s)")

        page += 1

    return orders


def resolve_type_names(type_ids: Iterable[int]) -> dict[int, str]:
    """Resolve type_id -> nome do item, em lotes de até 1000 (limite da ESI)."""
    ids = list(dict.fromkeys(type_ids))  # dedup preservando ordem
    names: dict[int, str] = {}

    for i in range(0, len(ids), 1000):
        batch = ids[i : i + 1000]
        resp = _session.post(f"{BASE_URL}/universe/names/", json=batch, timeout=20)
        resp.raise_for_status()
        for entry in resp.json():
            names[entry["id"]] = entry["name"]

    return names


VOLUME_CACHE_FILE = os.path.join(os.path.dirname(__file__), "volume_cache.json")


def get_type_volume(type_id: int) -> float:
    """
    Volume (m³) de uma unidade do item, usado pra caber no cargo hold.
    Cacheado em disco porque volume de um item nunca muda.
    """
    if os.path.exists(VOLUME_CACHE_FILE):
        with open(VOLUME_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {}

    key = str(type_id)
    if key in cache:
        return cache[key]

    resp = _get(f"{BASE_URL}/universe/types/{type_id}/")
    volume = float(resp.json().get("volume", 0.0))
    cache[key] = volume

    with open(VOLUME_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    return volume


def get_type_volumes(type_ids: Iterable[int]) -> dict[int, float]:
    """Busca volume de vários itens (usa cache, só bate na ESI pros que faltam)."""
    return {tid: get_type_volume(tid) for tid in type_ids}


def resolve_system_id(system_name: str) -> int:
    """
    Resolve o nome de um sistema solar pro seu ID, com cache local em
    system_cache.json (diferente de estações NPC, a ESI resolve nomes de
    sistema de forma confiável via /universe/ids/).
    """
    if os.path.exists(SYSTEM_CACHE_FILE):
        with open(SYSTEM_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {}

    key = system_name.strip()
    if key in cache:
        return cache[key]

    resp = _session.post(f"{BASE_URL}/universe/ids/", json=[key], timeout=20)
    resp.raise_for_status()
    data = resp.json()
    systems = data.get("systems", [])

    if not systems:
        raise ValueError(f"Sistema '{system_name}' não encontrado. Confira a grafia (em inglês).")

    system_id = systems[0]["id"]
    cache[key] = system_id

    with open(SYSTEM_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    return system_id


def get_route(origin_id: int, destination_id: int, avoid_lowsec: bool = True) -> list[int]:
    """
    Retorna a lista de system_id da rota, na ordem (incluindo origem e
    destino). flag='secure' evita low-sec/null-sec quando possível;
    'shortest' ignora segurança e pega o caminho mais curto.
    """
    flag = "secure" if avoid_lowsec else "shortest"
    resp = _get(
        f"{BASE_URL}/route/{origin_id}/{destination_id}/",
        params={"flag": flag},
    )
    return resp.json()


DISTANCE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "distance_cache.json")


def get_jump_count(system_a: int, system_b: int, avoid_lowsec: bool = True) -> int:
    """
    Número de jumps entre dois sistemas (distância é a mesma nos dois
    sentidos, então cacheia por par ordenado). Usado pra pesar lucro vs
    distância na otimização do circuito.
    """
    if system_a == system_b:
        return 0

    if os.path.exists(DISTANCE_CACHE_FILE):
        with open(DISTANCE_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {}

    key = f"{min(system_a, system_b)}-{max(system_a, system_b)}-{avoid_lowsec}"
    if key in cache:
        return cache[key]

    route = get_route(system_a, system_b, avoid_lowsec)
    jumps = len(route) - 1
    cache[key] = jumps

    with open(DISTANCE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    return jumps
