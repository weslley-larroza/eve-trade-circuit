import json

import pytest

import config
import local_settings
import webapp

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(local_settings, "PATH", str(tmp_path / "local_settings.json"))
    monkeypatch.delenv("EVE_CLIENT_ID", raising=False)
    monkeypatch.setattr(config, "SSO_CLIENT_ID", "")
    monkeypatch.setattr(webapp.sso, "list_characters", lambda: [])
    return webapp.app.test_client()


def test_login_without_client_id_opens_setup(client):
    r = client.get("/login")
    assert r.status_code == 302 and r.headers["Location"].endswith("/?setup=1")


def test_save_client_id_from_page_then_login_redirects_to_eve(client, tmp_path):
    (tmp_path / "local_settings.json").write_text(json.dumps({"manual_structure_ids": [5]}), encoding="utf-8")
    r = client.post("/api/sso/client-id", json={"client_id": GOOD.upper()})
    data = r.get_json()
    assert data["configured"] and data["sso"]["client_id_hint"] == "0123…cdef"
    saved = json.loads((tmp_path / "local_settings.json").read_text(encoding="utf-8"))
    assert saved == {"manual_structure_ids": [5], "client_id": GOOD}       # resto do arquivo preservado

    r = client.get("/login")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://login.eveonline.com/v2/oauth/authorize")
    assert f"client_id={GOOD}" in r.headers["Location"]


@pytest.mark.parametrize("bad", ["", "abc", "x" * 32, GOOD + "0"])
def test_invalid_client_id_rejected(client, bad):
    r = client.post("/api/sso/client-id", json={"client_id": bad})
    assert r.status_code == 400 and not client.get("/api/characters").get_json()["configured"]


def test_client_id_priority(client, monkeypatch):
    monkeypatch.setattr(config, "SSO_CLIENT_ID", "padrao")
    assert local_settings.client_id() == "padrao"
    local_settings.save_client_id(GOOD)
    assert local_settings.client_id() == GOOD
    monkeypatch.setenv("EVE_CLIENT_ID", "doenv")
    assert local_settings.client_id() == "doenv"
    assert client.post("/api/sso/client-id", json={"client_id": GOOD}).status_code == 400
