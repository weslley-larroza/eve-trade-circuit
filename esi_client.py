"""
Cliente da ESI (EVE Swagger Interface).

Endpoints públicos (mercado regional, universo, rotas) e, quando recebe um
access token, autenticados (mercado de estruturas, contatos, assets...).

Segue as regras atuais da ESI:
  - sem /latest: a versão é escolhida pelo header X-Compatibility-Date;
  - User-Agent identificável (app/versão + contato);
  - rate limit por grupo (429 + Retry-After) e error limit (100 respostas
    de erro por janela → 420 em tudo). O estado do error limit fica em
    ERROR_LIMIT pra quem faz muitas chamadas que podem dar 403 (probe de
    estruturas) decidir quando parar.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Iterable

import requests

import config

BASE_URL = "https://esi.evetech.net"


HEADERS = {
    "User-Agent": f"eve-trade-circuit/{config.APP_VERSION} (+https://github.com/weslley-larroza/eve-trade-circuit)",
    "X-Compatibility-Date": config.ESI_COMPATIBILITY_DATE,
    "Accept": "application/json",
}

SYSTEM_CACHE_FILE = os.path.join(os.path.dirname(__file__), "system_cache.json")

# Reaproveitar conexão TCP entre requisições — bem mais rápido que abrir
# uma conexão nova a cada página, especialmente em regiões com muitas.
_session = requests.Session()
_session.headers.update(HEADERS)

# Último estado conhecido do error limit (atualizado a cada resposta).
_err_lock = threading.Lock()
ERROR_LIMIT = {"remain": 100, "reset_at": 0.0}


class ESIError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"ESI {status} em {url}: {body[:200]}")
        self.status = status


def _track_limits(resp: requests.Response) -> None:
    remain = resp.headers.get("X-ESI-Error-Limit-Remain")
    reset = resp.headers.get("X-ESI-Error-Limit-Reset")
    if remain is None:
        return
    with _err_lock:
        ERROR_LIMIT["remain"] = int(remain)
        ERROR_LIMIT["reset_at"] = time.time() + int(reset or 60)


def error_budget_remaining() -> int:
    """Erros que ainda cabem na janela atual (100 se a janela já virou)."""
    with _err_lock:
        if time.time() >= ERROR_LIMIT["reset_at"]:
            return 100
        return ERROR_LIMIT["remain"]


def wait_error_window() -> None:
    """Dorme até a janela do error limit virar."""
    with _err_lock:
        delay = ERROR_LIMIT["reset_at"] - time.time()
    if delay > 0:
        time.sleep(delay + 1)


def request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body=None,
    token: str | None = None,
    ok_statuses: tuple[int, ...] = (),
) -> requests.Response:
    """
    Request com retry de 420/429/5xx. `path` é relativo a BASE_URL ou URL
    absoluta. Statuses em `ok_statuses` (ex.: 403, 404) voltam sem exceção
    pra quem chamou interpretar.
    """
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"} if token else None

    for attempt in range(5):
        resp = _session.request(method, url, params=params, json=json_body, headers=headers, timeout=30)
        _track_limits(resp)

        # Desacelera ANTES de tomar o 420.
        if error_budget_remaining() < 10:
            print("  [aviso] perto do error limit da ESI, pausando até a janela virar...")
            wait_error_window()

        if resp.status_code in (420, 429):
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"  [rate limit {resp.status_code}] aguardando {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code in (502, 503, 504) and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in ok_statuses or resp.ok:
            return resp
        raise ESIError(resp.status_code, url, resp.text)
    raise RuntimeError(f"Falhou após múltiplas tentativas: {url}")


def _get(url: str, params: dict | None = None, token: str | None = None) -> requests.Response:
    return request("GET", url, params=params, token=token)


def get_paged(path: str, params: dict | None = None, token: str | None = None, label: str = "") -> list:
    """
    Baixa todas as páginas (header X-Pages) de um endpoint paginado.

    Se o cache da ESI virar no meio do download (Last-Modified muda entre
    páginas), as páginas seriam de snapshots diferentes — com ordens
    duplicadas ou faltando. Nesse caso recomeça do zero (até 3 vezes).
    """
    for attempt in range(3):
        items: list = []
        page, total_pages = 1, 1
        snapshot = None
        start = time.time()
        restarted = False
        while page <= total_pages:
            resp = _get(path, params={**(params or {}), "page": page}, token=token)
            lm = resp.headers.get("Last-Modified")
            if snapshot is None:
                snapshot = lm
            elif lm and lm != snapshot:
                print(f"  {label or path}: dados mudaram no meio da paginação, recomeçando...")
                restarted = True
                break
            items.extend(resp.json())
            total_pages = int(resp.headers.get("X-Pages", "1"))
            if label and (page == 1 or page % 5 == 0 or page == total_pages):
                print(f"  {label}: página {page}/{total_pages} — {len(items)} itens ({time.time() - start:.0f}s)")
            page += 1
        if not restarted:
            return items
    return items


def get_region_orders(region_id: int, order_type: str = "all") -> list[dict]:
    """Baixa TODAS as páginas de ordens de uma região (todas as estações nela)."""
    return get_paged(f"markets/{region_id}/orders", params={"order_type": order_type}, label=f"região {region_id}")


def get_structure_orders(structure_id: int, token: str) -> list[dict]:
    """Livro de ofertas de uma estrutura (exige docking + structure_markets)."""
    return get_paged(f"markets/structures/{structure_id}", token=token, label=f"estrutura {structure_id}")


def resolve_type_names(type_ids: Iterable[int]) -> dict[int, str]:
    """
    Resolve IDs -> nome (itens, sistemas, corps...) via /universe/names, em
    lotes de até 1000. Um único ID inválido faz a ESI responder 404 pro lote
    inteiro; nesse caso o lote é dividido ao meio até isolar o culpado.
    """
    ids = list(dict.fromkeys(int(i) for i in type_ids))
    names: dict[int, str] = {}

    def resolve(batch: list[int]) -> None:
        if not batch:
            return
        resp = request("POST", "universe/names", json_body=batch, ok_statuses=(404,))
        if resp.status_code == 404:
            if len(batch) > 1:
                mid = len(batch) // 2
                resolve(batch[:mid])
                resolve(batch[mid:])
            return
        for entry in resp.json():
            names[entry["id"]] = entry["name"]

    for i in range(0, len(ids), 1000):
        resolve(ids[i : i + 1000])
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

    resp = _get(f"universe/types/{type_id}")
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

    resp = request("POST", "universe/ids", json_body=[key])
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
    Lista de system_id da rota, na ordem (incluindo origem e destino).

    avoid_lowsec=True usa preference "Safer" (prefere hisec, mas ainda passa
    por low/null se não existir caminho só hisec); False usa "Shorter".
    """
    resp = request(
        "POST",
        f"route/{origin_id}/{destination_id}",
        json_body={"preference": "Safer" if avoid_lowsec else "Shorter"},
    )
    return resp.json()["route"]


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
