import pytest

import config
import esi_client
from jsoncache import JsonCache
from structures import access

OK_ID, DENIED_ID, GONE_ID, NOMARKET_ID = 1_000_000_000_001, 1_000_000_000_002, 1_000_000_000_003, 1_000_000_000_004


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


@pytest.fixture
def fake_esi(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_cache", JsonCache(str(tmp_path / "acc.json")))
    calls = []

    def request(method, path, **kw):
        calls.append(path)
        sid = int(path.rstrip("/").split("/")[-1])
        if path.startswith("universe/structures"):
            if sid == DENIED_ID:
                return Resp(403)
            if sid == GONE_ID:
                return Resp(404)
            return Resp(200, {"name": f"S{sid}", "solar_system_id": 30000142, "owner_id": 98000001, "type_id": 35832})
        return Resp(403 if sid == NOMARKET_ID else 200, [])

    monkeypatch.setattr(esi_client, "request", request)
    monkeypatch.setattr(esi_client, "error_budget_remaining", lambda: 100)
    monkeypatch.setattr(esi_client, "get_system_info", lambda sid: {"region_id": 10000002})
    return calls


def test_probe_classifies_and_caches(fake_esi):
    ids = [OK_ID, DENIED_ID, GONE_ID, NOMARKET_ID]
    cache = access.probe(1, ids, lambda: "tok", log=lambda m: None)
    assert cache[str(OK_ID)]["status"] == "ok" and cache[str(OK_ID)]["market"] is True
    assert cache[str(DENIED_ID)]["status"] == "denied"
    assert cache[str(GONE_ID)]["status"] == "gone"
    assert cache[str(NOMARKET_ID)]["market"] is False
    assert set(access.accessible_markets(1)) == {OK_ID}

    n = len(fake_esi)
    access.probe(1, ids, lambda: "tok", log=lambda m: None)   # tudo fresco: nenhuma chamada
    assert len(fake_esi) == n


def test_cache_is_per_character(fake_esi):
    access.probe(1, [OK_ID], lambda: "tok", log=lambda m: None)
    assert access.load_cache(2) == {}


def test_denied_expires_after_ttl(fake_esi):
    access.probe(1, [DENIED_ID], lambda: "tok", log=lambda m: None)
    entry = access.load_cache(1)[str(DENIED_ID)]
    assert access.is_fresh(entry)
    entry["checked_at"] -= config.ACCESS_TTL_DENIED + 1
    assert not access.is_fresh(entry)


def test_pauses_when_error_budget_is_spent(fake_esi, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_MAX_ERRORS_PER_WINDOW", 2)
    waits = []
    monkeypatch.setattr(esi_client, "wait_error_window", lambda: waits.append(1))
    denied = [2_000_000_000_000 + i for i in range(5)]
    monkeypatch.setattr(esi_client, "request",
                        lambda m, p, **k: Resp(403))
    access.probe(1, denied, lambda: "tok", log=lambda m: None)
    assert len(waits) >= 2


def test_out_of_scope_skips_market_until_region_enters_scope(fake_esi):
    access.probe(1, [OK_ID], lambda: "tok", region_ids={10000043}, log=lambda m: None)
    entry = access.load_cache(1)[str(OK_ID)]
    assert entry["market"] is None and not any(p.startswith("markets/") for p in fake_esi)
    assert access.accessible_markets(1) == {}

    access.probe(1, [OK_ID], lambda: "tok", region_ids={10000002}, log=lambda m: None)
    assert access.load_cache(1)[str(OK_ID)]["market"] is True
    assert set(access.accessible_markets(1, {10000002})) == {OK_ID}
    assert access.accessible_markets(1, {10000043}) == {}
