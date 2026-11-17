"""
Servidor local (Flask) que substitui a interface Tkinter por uma página
web renderizada no seu próprio navegador — só localhost, nada exposto
pra fora da sua máquina.

Uso:
    python webapp.py
    (depois abra http://127.0.0.1:5000 no navegador)
"""
from __future__ import annotations

import json
import math
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

import config
import esi_client
import scanner

app = Flask(__name__, static_folder="web", static_url_path="")

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "gui_settings.json")

# --- Estado do scan em background -----------------------------------------
_scan_lock = threading.Lock()
_scan_state = {
    "status": "idle",       # idle | running | done | error
    "log": [],
    "opportunities": [],
    "error": None,
}
_books: list = []  # último resultado de scanner.build_hub_book por hub, reaproveitado pelo /api/circuit


def _load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"origin_system": "Jita", "avoid_lowsec": True}


def _save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def _run_scan(accounting_level: int) -> None:
    with _scan_lock:
        _scan_state["status"] = "running"
        _scan_state["log"] = []
        _scan_state["opportunities"] = []
        _scan_state["error"] = None

    def log(msg: str) -> None:
        with _scan_lock:
            _scan_state["log"].append(msg)

    try:
        tax_rate = config.sales_tax_rate(accounting_level)
        books = []
        for hub, cfg in config.HUBS.items():
            log(f"Baixando ordens de {hub}...")
            book = scanner.build_hub_book(hub, cfg["region_id"], cfg["station_id"])
            books.append(book)
            log(f"{hub}: {len(book.sell_orders)} itens à venda, {len(book.buy_orders)} itens com compra ativa.")

        log("Calculando oportunidades...")
        opportunities = scanner.find_opportunities(books, tax_rate)

        all_type_ids = {o["type_id"] for o in opportunities}
        names = esi_client.resolve_type_names(all_type_ids) if all_type_ids else {}

        for o in opportunities:
            o["name"] = names.get(o["type_id"], f"type_id {o['type_id']}")

        global _books
        _books = books

        with _scan_lock:
            _scan_state["opportunities"] = opportunities
            _scan_state["status"] = "done"

    except Exception as exc:  # noqa: BLE001 - queremos reportar qualquer erro pro front
        with _scan_lock:
            _scan_state["status"] = "error"
            _scan_state["error"] = str(exc)


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


@app.errorhandler(BadInput)
def _bad_input(exc):
    return jsonify({"error": str(exc)}), 400


# --- Rotas HTTP -------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(_load_settings())


@app.route("/api/scan", methods=["POST"])
def start_scan():
    with _scan_lock:
        if _scan_state["status"] == "running":
            return jsonify({"error": "Scan já em andamento."}), 409

    accounting_level = _accounting(request.get_json(silent=True) or {})
    threading.Thread(target=_run_scan, args=(accounting_level,), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan/status", methods=["GET"])
def scan_status():
    with _scan_lock:
        return jsonify(dict(_scan_state))


@app.route("/api/route", methods=["POST"])
def calc_route():
    data = request.get_json(silent=True) or {}
    origin_name = (data.get("origin") or "").strip()
    buy_hub = data.get("buy_hub")
    sell_hub = data.get("sell_hub")
    avoid_lowsec = bool(data.get("avoid_lowsec", True))

    if not origin_name or buy_hub not in config.HUBS or sell_hub not in config.HUBS:
        return jsonify({"error": "Parâmetros inválidos."}), 400

    settings = _load_settings()
    settings["origin_system"] = origin_name
    settings["avoid_lowsec"] = avoid_lowsec
    _save_settings(settings)

    try:
        origin_id = esi_client.resolve_system_id(origin_name)
        buy_system_id = config.HUBS[buy_hub]["system_id"]
        sell_system_id = config.HUBS[sell_hub]["system_id"]

        leg1 = esi_client.get_route(origin_id, buy_system_id, avoid_lowsec)
        leg2 = esi_client.get_route(buy_system_id, sell_system_id, avoid_lowsec)

        all_ids = list(dict.fromkeys(leg1 + leg2))
        system_names = esi_client.resolve_type_names(all_ids)

        return jsonify({
            "leg1": {"jumps": len(leg1) - 1, "systems": [system_names.get(i, str(i)) for i in leg1]},
            "leg2": {"jumps": len(leg2) - 1, "systems": [system_names.get(i, str(i)) for i in leg2]},
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/circuit", methods=["POST"])
def calc_circuit():
    data = request.get_json(silent=True) or {}
    hub_order = data.get("hub_order") or list(config.HUBS.keys())
    cargo_m3 = _num(data, "cargo_m3", 0, 0)
    budget_isk = _num(data, "budget_isk", 0, 0)
    accounting_level = _accounting(data)

    if not _books:
        return jsonify({"error": "Rode um scan primeiro (botão 'Escanear mercado')."}), 400

    invalid = [h for h in hub_order if h not in config.HUBS]
    if invalid or len(hub_order) < 2:
        return jsonify({"error": f"Hubs inválidos ou circuito muito curto: {invalid}"}), 400

    if cargo_m3 <= 0 or budget_isk <= 0:
        return jsonify({"error": "Informe capacidade de carga e budget maiores que zero."}), 400

    tax_rate = config.sales_tax_rate(accounting_level)

    try:
        book_by_hub = {b.hub: b for b in _books}

        # Busca volume só dos candidatos que aparecem nas pernas do circuito
        # (evita bater na ESI pra milhares de itens desnecessariamente).
        all_candidate_ids = set()
        for i in range(len(hub_order)):
            hub_a, hub_b = hub_order[i], hub_order[(i + 1) % len(hub_order)]
            items_lots = scanner.leg_lots(book_by_hub[hub_a], book_by_hub[hub_b], tax_rate)
            all_candidate_ids.update(items_lots.keys())

        volumes = esi_client.get_type_volumes(all_candidate_ids)
        legs = scanner.plan_circuit(_books, hub_order, tax_rate, cargo_m3, budget_isk, volumes)

        _resolve_names_for_legs(legs)

        total_profit = sum(leg["leg_profit"] for leg in legs)
        return jsonify({"legs": legs, "total_profit": total_profit})

    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


def _resolve_names_for_legs(legs: list[dict]) -> None:
    all_item_ids = {i["type_id"] for leg in legs for i in leg["items"]}
    names = esi_client.resolve_type_names(all_item_ids) if all_item_ids else {}
    for leg in legs:
        for item in leg["items"]:
            item["name"] = names.get(item["type_id"], f"type_id {item['type_id']}")


@app.route("/api/circuit/optimize", methods=["POST"])
def optimize_circuit():
    data = request.get_json(silent=True) or {}
    hubs = data.get("hubs") or list(config.HUBS.keys())
    cargo_m3 = _num(data, "cargo_m3", 0, 0)
    budget_isk = _num(data, "budget_isk", 0, 0)
    accounting_level = _accounting(data)
    jump_penalty_isk = _num(data, "jump_penalty_isk", 0, 0)
    avoid_lowsec = bool(data.get("avoid_lowsec", True))

    if not _books:
        return jsonify({"error": "Rode um scan primeiro (botão 'Escanear mercado')."}), 400

    hubs = list(dict.fromkeys(hubs))  # remove duplicatas mantendo ordem
    invalid = [h for h in hubs if h not in config.HUBS]
    if invalid or len(hubs) < 2:
        return jsonify({"error": f"Hubs inválidos ou circuito muito curto: {invalid}"}), 400

    if cargo_m3 <= 0 or budget_isk <= 0:
        return jsonify({"error": "Informe capacidade de carga e budget maiores que zero."}), 400

    tax_rate = config.sales_tax_rate(accounting_level)

    try:
        book_by_hub = {b.hub: b for b in _books}

        # Volume dos candidatos de TODOS os pares direcionados (não só do
        # loop escolhido, já que ainda não sabemos qual ordem vai vencer).
        all_candidate_ids = set()
        for a in hubs:
            for b in hubs:
                if a == b:
                    continue
                items_lots = scanner.leg_lots(book_by_hub[a], book_by_hub[b], tax_rate)
                all_candidate_ids.update(items_lots.keys())

        volumes = esi_client.get_type_volumes(all_candidate_ids)
        pair_legs = scanner.all_pair_legs(_books, hubs, tax_rate, cargo_m3, budget_isk, volumes)
        leg_profit = {pair: leg["leg_profit"] for pair, leg in pair_legs.items()}

        jump_count = {}
        for i, a in enumerate(hubs):
            for b in hubs[i + 1:]:
                sys_a = config.HUBS[a]["system_id"]
                sys_b = config.HUBS[b]["system_id"]
                jump_count[tuple(sorted((a, b)))] = esi_client.get_jump_count(sys_a, sys_b, avoid_lowsec)

        order, total_profit, total_jumps = scanner.best_circuit_order(
            hubs, leg_profit, jump_count, jump_penalty_isk
        )

        legs = [pair_legs[(order[i], order[(i + 1) % len(order)])] for i in range(len(order))]
        _resolve_names_for_legs(legs)

        return jsonify({
            "legs": legs,
            "total_profit": total_profit,
            "order": order,
            "total_jumps": total_jumps,
        })

    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
