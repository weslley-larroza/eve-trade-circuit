"""
Login EVE SSO (OAuth2 Authorization Code + PKCE, sem client secret).

Fluxo:
  1. begin_login()      → URL de autorização (abra no navegador).
  2. EVE redireciona pra http://127.0.0.1:5000/callback?code=...&state=...
  3. complete_login()   → troca o code por tokens, valida o JWT e guarda o
                          refresh token no cofre do sistema (keyring).
  4. get_access_token() → access token válido, renovando quando expira.
     O refresh token PODE mudar a cada renovação: sempre salvamos o novo.

Só metadados não secretos (id, nome, escopos) ficam em auth_state.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import jwt
import keyring
import keyring.errors
import requests

import config
import local_settings

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
JWKS_URL = "https://login.eveonline.com/oauth/jwks"
ISSUERS = ("https://login.eveonline.com", "login.eveonline.com")
KEYRING_SERVICE = "eve-trade-circuit"

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth_state.json")

_lock = threading.Lock()
_pending: dict[str, dict] = {}            # state -> {"verifier", "created"}
_access: dict[int, dict] = {}             # character_id -> {"token", "expires"}
_jwks_client: jwt.PyJWKClient | None = None


class SSOError(RuntimeError):
    pass


# --- estado em disco (não secreto) ------------------------------------------
def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"characters": {}, "active": None}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _require_client_id() -> str:
    cid = local_settings.client_id()
    if not cid:
        raise SSOError(
            "Client ID não configurado. Registre o app em developers.eveonline.com "
            "e coloque o client_id em local_settings.json (veja local_settings.example.json)."
        )
    return cid


# --- PKCE ----------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def begin_login(scopes: list[str] | None = None) -> str:
    client_id = _require_client_id()
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(16)

    with _lock:
        now = time.time()
        for s in [s for s, p in _pending.items() if now - p["created"] > 600]:
            del _pending[s]
        _pending[state] = {"verifier": verifier, "created": now}

    params = {
        "response_type": "code",
        "redirect_uri": config.SSO_CALLBACK_URL,
        "client_id": client_id,
        "scope": " ".join(scopes or config.SSO_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _token_request(data: dict) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "login.eveonline.com"},
        timeout=20,
    )
    if not resp.ok:
        raise SSOError(f"Falha no token endpoint ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _validate(access_token: str, client_id: str) -> dict:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(JWKS_URL)
    key = _jwks_client.get_signing_key_from_jwt(access_token)
    claims = jwt.decode(
        access_token,
        key.key,
        algorithms=["RS256"],
        audience=[client_id, "EVE Online"],
        options={"verify_iss": False},
    )
    if claims.get("iss") not in ISSUERS:
        raise SSOError(f"Issuer inesperado no JWT: {claims.get('iss')}")
    return claims


def _store_tokens(tokens: dict, client_id: str) -> dict:
    claims = _validate(tokens["access_token"], client_id)
    character_id = int(claims["sub"].split(":")[-1])
    scopes = claims.get("scp") or []
    if isinstance(scopes, str):
        scopes = [scopes]

    # O refresh token pode ter sido rotacionado — sempre grava o recebido.
    keyring.set_password(KEYRING_SERVICE, str(character_id), tokens["refresh_token"])
    with _lock:
        _access[character_id] = {
            "token": tokens["access_token"],
            "expires": time.time() + int(tokens.get("expires_in", 1199)) - 60,
        }

    info = {"character_id": character_id, "name": claims.get("name"), "scopes": scopes}
    state = _load_state()
    state["characters"][str(character_id)] = info
    state["active"] = int(state.get("active") or character_id)
    _save_state(state)
    return info


def complete_login(code: str, state: str) -> dict:
    client_id = _require_client_id()
    with _lock:
        pending = _pending.pop(state, None)
    if not pending:
        raise SSOError("State inválido ou expirado — comece o login de novo.")

    tokens = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": pending["verifier"],
    })
    info = _store_tokens(tokens, client_id)
    set_active(info["character_id"])   # quem acabou de logar vira o ativo
    return info


def get_access_token(character_id: int | None = None) -> str:
    character_id = character_id or active_character_id()
    if not character_id:
        raise SSOError("Nenhum personagem logado.")

    with _lock:
        cached = _access.get(character_id)
        if cached and cached["expires"] > time.time():
            return cached["token"]

    client_id = _require_client_id()
    refresh = keyring.get_password(KEYRING_SERVICE, str(character_id))
    if not refresh:
        raise SSOError("Refresh token não encontrado no cofre — faça login de novo.")
    tokens = _token_request({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
    _store_tokens(tokens, client_id)
    return _access[character_id]["token"]


def active_character_id() -> int | None:
    a = _load_state().get("active")
    return int(a) if a else None


def active_character() -> dict | None:
    st = _load_state()
    a = st.get("active")
    return st["characters"].get(str(a)) if a else None


def list_characters() -> list[dict]:
    st = _load_state()
    active = int(st.get("active") or 0)
    return [{**c, "active": int(cid) == active} for cid, c in st["characters"].items()]


def set_active(character_id: int) -> None:
    st = _load_state()
    if str(character_id) not in st["characters"]:
        raise SSOError("Personagem não está logado.")
    st["active"] = int(character_id)
    _save_state(st)


def has_scope(scope: str, character_id: int | None = None) -> bool:
    st = _load_state()
    cid = str(character_id or st.get("active") or "")
    return scope in st["characters"].get(cid, {}).get("scopes", [])


def logout(character_id: int | None = None) -> None:
    character_id = int(character_id or active_character_id() or 0)
    if not character_id:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, str(character_id))
    except keyring.errors.PasswordDeleteError:
        pass
    with _lock:
        _access.pop(character_id, None)
    st = _load_state()
    st["characters"].pop(str(character_id), None)
    if int(st.get("active") or 0) == character_id:
        nxt = next(iter(st["characters"]), None)
        st["active"] = int(nxt) if nxt else None
    _save_state(st)
