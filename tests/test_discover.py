import esi_client
import local_settings
from auth import sso
from structures import discover


def test_discover_merges_sources_and_tolerates_failures(monkeypatch):
    monkeypatch.setattr(local_settings, "load", lambda: {"manual_structure_ids": [1_000_000_000_009],
                                                         "extra_region_ids": [], "overrides": {}})
    monkeypatch.setattr(discover, "from_public", lambda: {1_000_000_000_001, 1_000_000_000_002})
    monkeypatch.setattr(discover, "from_assets", lambda cid: {1_000_000_000_002})
    monkeypatch.setattr(discover, "from_search", lambda cid, t: set())

    def corp_fails(cid):
        raise esi_client.ESIError(500, "corp")

    monkeypatch.setattr(discover, "from_corp", corp_fails)
    orders = [{"location_id": 60003760}, {"location_id": 1_000_000_000_001}]

    got = discover.discover(42, region_orders=orders, log=lambda m: None)
    assert got[1_000_000_000_001] == ["orders", "public"]
    assert got[1_000_000_000_002] == ["public", "assets"]
    assert got[1_000_000_000_009] == ["manual"]
    assert 60003760 not in got                       # estação NPC não é estrutura


def test_scope_regions_uses_character_location_plus_extras(monkeypatch):
    monkeypatch.setattr(local_settings, "load", lambda: {"extra_region_ids": [10000043]})
    monkeypatch.setattr(discover, "character_region", lambda cid: 10000002)
    assert discover.scope_regions(42, log=lambda m: None) == {10000002, 10000043}
    assert discover.scope_regions(None, log=lambda m: None) == {10000043}


def test_sources_needing_scope_are_skipped_without_it(monkeypatch):
    monkeypatch.setattr(sso, "has_scope", lambda scope, cid=None: False)
    assert discover.from_assets(1) == set()
    assert discover.from_corp(1) == set()
    assert discover.character_region(1) is None
