import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import jwt
import keyring
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from keyring.backend import KeyringBackend

import local_settings
from auth import sso

CLIENT_ID = "test-client"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise keyring.errors.PasswordDeleteError()
        del self.store[(service, user)]


def make_jwt(char_id, name, aud=("test-client", "EVE Online"), iss="https://login.eveonline.com"):
    return jwt.encode({"sub": f"CHARACTER:EVE:{char_id}", "name": name, "aud": list(aud), "iss": iss,
                       "scp": ["esi-markets.structure_markets.v1"], "exp": int(time.time()) + 1200},
                      KEY, algorithm="RS256")


class FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        class K:
            key = KEY.public_key()
        return K()


@pytest.fixture
def env(tmp_path, monkeypatch):
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    monkeypatch.setattr(sso, "STATE_FILE", str(tmp_path / "auth_state.json"))
    monkeypatch.setattr(sso, "_jwks_client", FakeJWKS())
    monkeypatch.setattr(local_settings, "client_id", lambda: CLIENT_ID)
    sso._access.clear()
    sso._pending.clear()
    calls = []
    responses = []

    def fake_token_request(data):
        calls.append(data)
        return responses.pop(0)

    monkeypatch.setattr(sso, "_token_request", fake_token_request)
    return kr, calls, responses


def login(env, char_id, name, refresh):
    kr, calls, responses = env
    url = sso.begin_login()
    q = parse_qs(urlparse(url).query)
    responses.append({"access_token": make_jwt(char_id, name), "refresh_token": refresh, "expires_in": 1199})
    sso.complete_login("CODE", q["state"][0])
    return q


def test_pkce_challenge_matches_verifier(env):
    q = login(env, 1, "Alpha", "r1")
    verifier = env[1][0]["code_verifier"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"][0] == expected and q["code_challenge_method"][0] == "S256"
    assert "client_secret" not in env[1][0]


def test_state_is_single_use(env):
    q = login(env, 1, "Alpha", "r1")
    with pytest.raises(sso.SSOError):
        sso.complete_login("CODE", q["state"][0])


def test_refresh_token_rotation_is_saved(env):
    kr, calls, responses = env
    login(env, 1, "Alpha", "r1")
    sso._access[1]["expires"] = 0                       # força renovação
    responses.append({"access_token": make_jwt(1, "Alpha"), "refresh_token": "r2", "expires_in": 1199})
    sso.get_access_token(1)
    assert calls[-1] == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": CLIENT_ID}
    assert kr.get_password(sso.KEYRING_SERVICE, "1") == "r2"


def test_rejects_wrong_audience(env):
    _, _, responses = env
    url = sso.begin_login()
    state = parse_qs(urlparse(url).query)["state"][0]
    responses.append({"access_token": make_jwt(1, "X", aud=("outro-app",)), "refresh_token": "r", "expires_in": 1})
    with pytest.raises(jwt.InvalidAudienceError):
        sso.complete_login("CODE", state)


def test_multiple_characters_and_logout(env):
    kr = env[0]
    login(env, 1, "Main", "r1")
    login(env, 2, "Alt", "r2")
    chars = {c["character_id"]: c for c in sso.list_characters()}
    assert chars[2]["active"] and not chars[1]["active"]   # quem logou por último fica ativo
    sso.set_active(1)
    sso.logout(1)
    assert sso.active_character_id() == 2
    assert kr.get_password(sso.KEYRING_SERVICE, "1") is None
