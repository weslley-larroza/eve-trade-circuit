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
from jsoncache import JsonCache

BASE_URL = "https://esi.evetech.net"


HEADERS = {
    "User-Agent": f"eve-trade-circuit/{config.APP_VERSION} (+https://github.com/weslley-larroza/eve-trade-circuit)",
    "X-Compatibility-Date": config.ESI_COMPATIBILITY_DATE,
    "Accept": "application/json",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_system_cache = JsonCache(os.path.join(_HERE, "system_cache.json"))

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


# packaged_volume: é o volume que conta no cargo (naves e containers são
# vendidos empacotados; o "volume" montado deles é muito maior). Arquivo
# novo porque o antigo (volume_cache.json) guardava o volume montado.
_volume_cache = JsonCache(os.path.join(_HERE, "packaged_volume_cache.json"))


def _fetch_type_volume(type_id: int) -> float:
    data = _get(f"universe/types/{type_id}").json()
    return float(data.get("packaged_volume") or data.get("volume") or 0.0)


def get_type_volume(type_id: int) -> float:
    """Volume empacotado (m³) de uma unidade. Cacheado: nunca muda."""
    key = str(type_id)
    if key not in _volume_cache:
        _volume_cache.set(key, _fetch_type_volume(type_id))
    return _volume_cache.get(key)


def get_type_volumes(type_ids: Iterable[int]) -> dict[int, float]:
    """Volume de vários itens; busca na ESI só os que faltam e grava uma vez."""
    ids = list(dict.fromkeys(int(t) for t in type_ids))
    missing = {str(t): _fetch_type_volume(t) for t in ids if str(t) not in _volume_cache}
    if missing:
        _volume_cache.update(missing)
    return {t: _volume_cache.get(str(t)) for t in ids}


def resolve_system_id(system_name: str) -> int:
    """Nome do sistema solar → ID (cache em system_cache.json)."""
    key = system_name.strip()
    hit = _system_cache.get(key.lower()) or _system_cache.get(key)
    if hit:
        return hit

    data = request("POST", "universe/ids", json_body=[key]).json()
    systems = data.get("systems", [])
    if not systems:
        raise ValueError(f"Sistema '{system_name}' não encontrado. Confira a grafia (em inglês).")
    system_id = systems[0]["id"]
    _system_cache.set(key.lower(), system_id)
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


_distance_cache = JsonCache(os.path.join(_HERE, "distance_cache.json"))


def get_jump_count(system_a: int, system_b: int, avoid_lowsec: bool = True) -> int:
    """
    Número de jumps entre dois sistemas (simétrico → cacheia por par
    ordenado). Usado pra pesar lucro vs distância no circuito.
    """
    if system_a == system_b:
        return 0
    pref = "safer" if avoid_lowsec else "shorter"   # v2: rotas da ESI nova
    key = f"v2-{min(system_a, system_b)}-{max(system_a, system_b)}-{pref}"
    if key not in _distance_cache:
        _distance_cache.set(key, len(get_route(system_a, system_b, avoid_lowsec)) - 1)
    return _distance_cache.get(key)


_system_info_cache = JsonCache(os.path.join(_HERE, "system_info_cache.json"))


def get_system_info(system_id: int) -> dict:
    """Nome, segurança e região de um sistema (cache permanente)."""
    key = str(system_id)
    hit = _system_info_cache.get(key)
    if hit:
        return hit
    sys_data = _get(f"universe/systems/{system_id}").json()
    const = _get(f"universe/constellations/{sys_data['constellation_id']}").json()
    hit = {
        "name": sys_data["name"],
        "security_status": sys_data.get("security_status"),
        "constellation_id": sys_data["constellation_id"],
        "region_id": const["region_id"],
    }
    _system_info_cache.set(key, hit)
    return hit


def resolve_region_ids(names: Iterable[str]) -> dict[str, int]:
    """Nomes de região → IDs (os que não existirem ficam de fora)."""
    names = [n.strip() for n in names if n.strip()]
    if not names:
        return {}
    data = request("POST", "universe/ids", json_body=names).json()
    return {r["name"]: r["id"] for r in data.get("regions", [])}
