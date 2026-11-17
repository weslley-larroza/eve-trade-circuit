"""
Servidor local (Flask) com a interface web do scanner — só localhost, nada
exposto pra fora da sua máquina.

Uso:
    python webapp.py
    (depois abra http://127.0.0.1:5000 no navegador)

Locais de mercado são identificados por id (station_id / structure_id). O
usuário pode marcar/desmarcar cada local ("contabilizar"); a escolha fica
salva em gui_settings.json.
"""
from __future__ import annotations

import math
import os
import threading

from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from markupsafe import escape

import config
import esi_client
import local_settings
import scanner
from auth import sso
from jsoncache import JsonCache
from markets import sources
from models import Location, default_tradable, npc_hubs
from structures import access, discover, standings

app = Flask(__name__, static_folder="web", static_url_path="")

# Só atende quem chama pelo próprio endereço local. Bloqueia DNS rebinding:
# um site malicioso apontando um domínio pra 127.0.0.1 não consegue usar
# este servidor (que agora fala com a ESI usando o seu token).
ALLOWED_HOSTS = {"127.0.0.1:5000", "localhost:5000", "localhost"}


@app.before_request
def _check_host():
    if request.host not in ALLOWED_HOSTS:
        abort(403)


_settings = JsonCache(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_settings.json"))


# --- Estado do scan ------------------------------------------------------------
class ScanState:
    """Tudo que o scan produz, protegido por um lock só."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"            # idle | running | done | error
        self.log: list[str] = []
        self.error: str | None = None
        self.books: list[scanner.LocationBook] = []
        self.locations: dict[int, Location] = {}
        self.opportunities: list[dict] = []

    def try_start(self) -> bool:
        """Marca 'running' atomicamente; False se já havia um scan rodando."""
        with self.lock:
            if self.status == "running":
                return False
            self.status, self.log, self.error = "running", [], None
            return True

    def add_log(self, msg: str) -> None:
        with self.lock:
            self.log.append(msg)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "log": list(self.log[-50:]),
                "error": self.error,
                "opportunities": list(self.opportunities),
                "locations": [l.to_dict() for l in self.locations.values()],
            }


_state = ScanState()


# --- Validação de entrada ----------------------------------------------------
class BadInput(ValueError):
    pass


def _num(data: dict, key: str, default, lo: float | None = None, hi: float | None = None, as_int: bool = False):
    raw = data.get(key, default)
    try:
        val = int(raw) if as_int else float(raw)
    except (TypeError, ValueError):
        raise BadInput(f"'{key}' precisa ser um número (recebido: {raw!r}).")
    if isinstance(val, float) and not math.isfinite(val):
        raise BadInput(f"'{key}' precisa ser um número finito.")
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        raise BadInput(f"'{key}' fora do intervalo permitido ({lo}–{hi}).")
    return val


def _accounting(data: dict) -> int:
    return _num(data, "accounting", config.ACCOUNTING_SKILL_LEVEL, 0, 5, as_int=True)


def _location_ids(data: dict, key: str) -> list[int]:
    raw = data.get(key)
    if not isinstance(raw, list):
        raise BadInput(f"'{key}' precisa ser uma lista de ids de local.")
    try:
        ids = list(dict.fromkeys(int(x) for x in raw))
    except (TypeError, ValueError):
        raise BadInput(f"'{key}' contém id inválido.")
    with _state.lock:
        unknown = [i for i in ids if i not in _state.locations]
    if unknown:
        raise BadInput(f"Locais desconhecidos (rode um scan): {unknown}")
    return ids


@app.errorhandler(BadInput)
def _bad_input(exc):
    return jsonify({"error": str(exc)}), 400


# --- Locais ------------------------------------------------------------------
def _extra_regions() -> dict[str, int]:
    """Regiões extras escolhidas na interface: {nome: region_id}."""
    return _settings.get("extra_regions", {})


def _toggles() -> dict[str, bool]:
    return _settings.get("location_toggles", {})


def _apply_toggles(locations: list[Location]) -> list[Location]:
    toggles = _toggles()
    for loc in locations:
        loc.tradable = toggles.get(str(loc.id), default_tradable(loc.category))
    return locations


def _structure_stage(src: sources.OrderBookSource, character_id: int, log) -> list[Location]:
    """
    Login ativo: escopo (região do personagem + extras) → descoberta →
    teste de acesso → classificação. Devolve as estruturas utilizáveis.
    """
    regions = discover.scope_regions(character_id, _extra_regions().values(), log=log)
    if not regions:
        log("Sem região no escopo (localização indisponível e nenhuma região extra) — só hubs NPC.")
        return []
    log(f"Escopo de estruturas: {len(regions)} região(ões).")
    src.fetch_regions(regions)

    log("Descobrindo estruturas candidatas...")
    region_orders = [o for rid in regions for o in src.region_orders.get(rid, [])]
    candidates = discover.discover(character_id, region_orders=region_orders, log=log)

    log("Testando acesso às estruturas...")
    token = lambda: sso.get_access_token(character_id)  # noqa: E731
    access.probe(character_id, candidates.keys(), token, region_ids=regions, log=log)

    classifier = standings.Classifier(character_id)
    locs = sources.structure_locations(character_id, regions, classifier.classify, candidates)
    log(f"{len(locs)} estrutura(s) com mercado acessível no escopo.")
    return locs


# --- Scan ---# --- Scan ----------------------------------------------------------------------
def _resolve_names(rows: list[dict]) -> None:
    ids = {r["type_id"] for r in rows}
    names = esi_client.resolve_type_names(ids) if ids else {}
    for r in rows:
        r["name"] = names.get(r["type_id"], f"type_id {r['type_id']}")


def _compute_opportunities(accounting_level: int) -> list[dict]:
    with _state.lock:
        books = list(_state.books)
    opps = scanner.find_opportunities(books, config.sales_tax_rate(accounting_level))
    _resolve_names(opps)
    return opps


def _run_scan(accounting_level: int, include_structures: bool) -> None:
    log = _state.add_log
    try:
        src = sources.OrderBookSource(log)
        locations = npc_hubs()
        character_id = sso.active_character_id()
        token_getter = None

        if include_structures and character_id:
            try:
                locations += _structure_stage(src, character_id, log)
                token_getter = lambda: sso.get_access_token(character_id)  # noqa: E731
            except sso.SSOError as exc:
                log(f"Estruturas puladas: {exc}")

        _apply_toggles(locations)

        def on_denied(sid: int) -> None:
            access.mark_denied(character_id, sid)

        books = scanner.build_books(locations, log=log, source=src, token_getter=token_getter, on_denied=on_denied)
        with _state.lock:
            _state.books = books
            _state.locations = {b.location.id: b.location for b in books}
        log("Calculando oportunidades...")
        opps = _compute_opportunities(accounting_level)
        with _state.lock:
            _state.opportunities = opps
            _state.status = "done"
            _state.log.append(f"{len(opps)} oportunidade(s) encontrada(s).")
    except Exception as exc:  # noqa: BLE001 - queremos reportar qualquer erro pro front
        with _state.lock:
            _state.status = "error"
            _state.error = str(exc)


def _books_or_error():
    with _state.lock:
        books = list(_state.books)
    if not books:
        raise BadInput("Rode um scan primeiro (botão 'Escanear mercado').")
    return {b.key: b for b in books}


def _candidate_volumes(book_by_id: dict, pairs, tax_rate: float) -> dict[int, float]:
    ids: set[int] = set()
    for a, b in pairs:
        ids.update(scanner.leg_lots(book_by_id[a], book_by_id[b], tax_rate).keys())
    return esi_client.get_type_volumes(ids)


def _check_tradable(ids: list[int]) -> None:
    with _state.lock:
        off = [_state.locations[i].name for i in ids if not _state.locations[i].tradable]
    if off:
        raise BadInput(f"Locais desmarcados não podem entrar no circuito: {', '.join(off)}")


# --- Rotas HTTP -------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# --- Login EVE SSO --------------------------------------------------------------
def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset='utf-8'><title>{escape(title)}</title>"
            f"<body style='background:#0f1115;color:#e6e8ec;font-family:Segoe UI,Arial;padding:30px'>"
            f"<h2>{escape(title)}</h2><p>{escape(body)}</p><p><a style='color:#4da3ff' href='/'>Voltar</a></p>")


@app.route("/login")
def login():
    """
    Botão "Entrar com EVE Online": manda direto pra página de login da CCP
    (PKCE). Sem client ID configurado, volta pra página com o painel de
    configuração aberto.
    """
    if local_settings.client_id() is None:
        return redirect("/?setup=1")
    try:
        return redirect(sso.begin_login())
    except sso.SSOError as exc:
        return _page("Login indisponível", str(exc)), 400


@app.route("/callback")
def sso_callback():
    if request.args.get("error"):
        return _page("Login cancelado", request.args.get("error_description") or request.args["error"]), 400
    code, state = request.args.get("code"), request.args.get("state")
    if not code or not state:
        return _page("Login inválido", "Faltou code/state na resposta do EVE SSO."), 400
    try:
        sso.complete_login(code, state)
    except Exception as exc:  # noqa: BLE001
        return _page("Falha no login", str(exc)), 400
    return redirect("/")


def _character_id(data: dict) -> int:
    cid = _num(data, "character_id", None, 1, as_int=True)
    if not any(c["character_id"] == cid for c in sso.list_characters()):
        raise BadInput("Personagem não está logado.")
    return cid


@app.route("/api/characters", methods=["GET"])
def characters():
    cid = local_settings.client_id()
    return jsonify({
        "configured": cid is not None,
        "characters": sso.list_characters(),
        # Dados pra configurar o app EVE direto pela página.
        "sso": {
            "client_id_hint": f"{cid[:4]}…{cid[-4:]}" if cid else None,
            "from_env": local_settings.client_id_from_env(),
            "callback_url": config.SSO_CALLBACK_URL,
            "scopes": config.SSO_SCOPES,
            "developers_url": "https://developers.eveonline.com/applications",
        },
    })


@app.route("/api/sso/client-id", methods=["POST"])
def set_client_id():
    """Salva o client ID do app EVE (colado na página) em local_settings.json."""
    if local_settings.client_id_from_env():
        raise BadInput("O client ID está vindo da variável de ambiente EVE_CLIENT_ID; altere-a lá.")
    data = request.get_json(silent=True) or {}
    try:
        local_settings.save_client_id(str(data.get("client_id", "")))
    except ValueError as exc:
        raise BadInput(str(exc))
    return characters()


@app.route("/api/characters/active", methods=["POST"])
def set_active_character():
    sso.set_active(_character_id(request.get_json(silent=True) or {}))
    return characters()


@app.route("/api/characters/logout", methods=["POST"])
def logout_character():
    sso.logout(_character_id(request.get_json(silent=True) or {}))
    return characters()


@app.route("/api/scope", methods=["GET"])
def get_scope():
    return jsonify({"extra_regions": _extra_regions()})


@app.route("/api/scope/regions", methods=["POST"])
def edit_scope_regions():
    """{add: "Nome da região"} ou {remove: "Nome da região"}."""
    data = request.get_json(silent=True) or {}
    regions = dict(_extra_regions())
    if data.get("add"):
        found = esi_client.resolve_region_ids([str(data["add"])])
        if not found:
            raise BadInput(f"Região '{data['add']}' não encontrada (use o nome em inglês, ex.: The Forge).")
        regions.update(found)
    if data.get("remove"):
        regions.pop(str(data["remove"]), None)
    _settings.set("extra_regions", regions)
    return get_scope()


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "origin_system": _settings.get("origin_system", "Jita"),
        "avoid_lowsec": _settings.get("avoid_lowsec", True),
        "max_optimize_locations": config.MAX_OPTIMIZE_LOCATIONS,
    })


@app.route("/api/scan", methods=["POST"])
def start_scan():
    data = request.get_json(silent=True) or {}
    accounting_level = _accounting(data)
    include_structures = bool(data.get("include_structures", True))
    if not _state.try_start():
        return jsonify({"error": "Scan já em andamento."}), 409
    threading.Thread(target=_run_scan, args=(accounting_level, include_structures), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan/status", methods=["GET"])
def scan_status():
    return jsonify(_state.snapshot())


@app.route("/api/locations/toggle", methods=["POST"])
def toggle_location():
    """Marca/desmarca um local e recalcula as oportunidades (sem novo download)."""
    data = request.get_json(silent=True) or {}
    lid = _location_ids({"ids": [data.get("id")]}, "ids")[0]
    tradable = bool(data.get("tradable"))
    accounting_level = _accounting(data)

    toggles = dict(_toggles())
    toggles[str(lid)] = tradable
    _settings.set("location_toggles", toggles)
    with _state.lock:
        _state.locations[lid].tradable = tradable

    opps = _compute_opportunities(accounting_level)
    with _state.lock:
        _state.opportunities = opps
    return jsonify(_state.snapshot())


@app.route("/api/route", methods=["POST"])
def calc_route():
    data = request.get_json(silent=True) or {}
    origin_name = (data.get("origin") or "").strip()
    avoid_lowsec = bool(data.get("avoid_lowsec", True))
    if not origin_name:
        raise BadInput("Informe o sistema de origem.")
    buy_id, sell_id = _location_ids({"ids": [data.get("buy_location_id"), data.get("sell_location_id")]}, "ids")

    try:
        origin_id = esi_client.resolve_system_id(origin_name)
    except ValueError as exc:
        raise BadInput(str(exc))
    _settings.update({"origin_system": origin_name, "avoid_lowsec": avoid_lowsec})

    with _state.lock:
        buy_sys = _state.locations[buy_id].system_id
        sell_sys = _state.locations[sell_id].system_id

    try:
        leg1 = esi_client.get_route(origin_id, buy_sys, avoid_lowsec)
        leg2 = esi_client.get_route(buy_sys, sell_sys, avoid_lowsec)
        system_names = esi_client.resolve_type_names(list(dict.fromkeys(leg1 + leg2)))
        return jsonify({
            "leg1": {"jumps": len(leg1) - 1, "systems": [system_names.get(i, str(i)) for i in leg1]},
            "leg2": {"jumps": len(leg2) - 1, "systems": [system_names.get(i, str(i)) for i in leg2]},
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


def _circuit_params(data: dict) -> tuple[float, float, float]:
    cargo_m3 = _num(data, "cargo_m3", 0, 0)
    budget_isk = _num(data, "budget_isk", 0, 0)
    if cargo_m3 <= 0 or budget_isk <= 0:
        raise BadInput("Informe capacidade de carga e budget maiores que zero.")
    return cargo_m3, budget_isk, config.sales_tax_rate(_accounting(data))


@app.route("/api/circuit", methods=["POST"])
def calc_circuit():
    data = request.get_json(silent=True) or {}
    order = _location_ids(data, "location_order")
    if len(order) < 2:
        raise BadInput("O circuito precisa de pelo menos 2 locais.")
    _check_tradable(order)
    cargo_m3, budget_isk, tax_rate = _circuit_params(data)
    book_by_id = _books_or_error()

    try:
        pairs = [(order[i], order[(i + 1) % len(order)]) for i in range(len(order))]
        volumes = _candidate_volumes(book_by_id, pairs, tax_rate)
        legs = scanner.plan_circuit(list(book_by_id.values()), order, tax_rate, cargo_m3, budget_isk, volumes)
        _resolve_names([it for leg in legs for it in leg["items"]])
        return jsonify({"legs": legs, "total_profit": sum(leg["leg_profit"] for leg in legs)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/circuit/optimize", methods=["POST"])
def optimize_circuit():
    data = request.get_json(silent=True) or {}
    ids = _location_ids(data, "location_ids")
    if len(ids) < 2:
        raise BadInput("O circuito precisa de pelo menos 2 locais.")
    if len(ids) > config.MAX_OPTIMIZE_LOCATIONS:
        raise BadInput(f"Otimização automática aceita no máximo {config.MAX_OPTIMIZE_LOCATIONS} locais.")
    _check_tradable(ids)
    cargo_m3, budget_isk, tax_rate = _circuit_params(data)
    jump_penalty_isk = _num(data, "jump_penalty_isk", 0, 0)
    avoid_lowsec = bool(data.get("avoid_lowsec", True))
    book_by_id = _books_or_error()

    try:
        books = list(book_by_id.values())
        pairs = [(a, b) for a in ids for b in ids if a != b]
        volumes = _candidate_volumes(book_by_id, pairs, tax_rate)
        pair_legs = scanner.all_pair_legs(books, ids, tax_rate, cargo_m3, budget_isk, volumes)
        leg_profit = {pair: leg["leg_profit"] for pair, leg in pair_legs.items()}

        with _state.lock:
            systems = {i: _state.locations[i].system_id for i in ids}
        jump_count = {}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                jump_count[tuple(sorted((a, b)))] = esi_client.get_jump_count(systems[a], systems[b], avoid_lowsec)

        order, total_profit, total_jumps = scanner.best_circuit_order(ids, leg_profit, jump_count, jump_penalty_isk)
        legs = [pair_legs[(order[i], order[(i + 1) % len(order)])] for i in range(len(order))]
        _resolve_names([it for leg in legs for it in leg["items"]])

        return jsonify({"legs": legs, "total_profit": total_profit, "order": order, "total_jumps": total_jumps})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
