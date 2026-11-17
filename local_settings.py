"""
Configuração local do usuário (local_settings.json, fora do git).

Formato: veja local_settings.example.json. A variável de ambiente
EVE_CLIENT_ID tem prioridade sobre o arquivo.

"overrides" força a categoria de uma estrutura/corp/aliança, ignorando os
contatos do jogo — ex.: corp em guerra com você → "red".
"""
from __future__ import annotations

import json
import os

PATH = os.path.join(os.path.dirname(__file__), "local_settings.json")


def load() -> dict:
    data: dict = {}
    if os.path.exists(PATH):
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    if os.environ.get("EVE_CLIENT_ID"):
        data["client_id"] = os.environ["EVE_CLIENT_ID"]
    data.setdefault("manual_structure_ids", [])
    data.setdefault("extra_region_ids", [])
    ov = data.setdefault("overrides", {})
    for k in ("structures", "corporations", "alliances"):
        ov.setdefault(k, {})
    return data


def client_id() -> str | None:
    cid = load().get("client_id")
    if not cid or cid.startswith("COLE_AQUI"):
        return None
    return cid
