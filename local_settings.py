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
import re

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
    """Ordem: EVE_CLIENT_ID → local_settings.json → config.SSO_CLIENT_ID."""
    import config  # import tardio: config não depende deste módulo

    cid = load().get("client_id")
    if not cid or cid.startswith("COLE_AQUI"):
        cid = config.SSO_CLIENT_ID
    return cid or None


def client_id_from_env() -> bool:
    """True se o client ID vem de EVE_CLIENT_ID (o arquivo é ignorado)."""
    return bool(os.environ.get("EVE_CLIENT_ID"))


_CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def save_client_id(value: str) -> str:
    """
    Grava o client ID em local_settings.json, preservando o resto do arquivo.
    O ID de um app EVE tem 32 caracteres hexadecimais.
    """
    cid = (value or "").strip().lower()
    if not _CLIENT_ID_RE.match(cid):
        raise ValueError("Client ID inválido: deve ter 32 caracteres hexadecimais (copie do developers.eveonline.com).")

    data: dict = {}
    if os.path.exists(PATH):
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    data["client_id"] = cid
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PATH)
    return cid
