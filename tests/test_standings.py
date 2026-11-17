import pytest

from structures import standings

MY_CORP, MY_ALLY = 100, 900
CORPS = {
    100: {"alliance_id": 900},   # minha corp
    101: {"alliance_id": 900},   # outra corp da minha aliança
    200: {"alliance_id": None},  # blue pessoal
    300: {"alliance_id": 950},   # aliança red no contato da aliança
    301: {"alliance_id": 950},   # mesma aliança red, mas a corp tem contato +5 na minha corp
    400: {"alliance_id": None},  # sem contato
    500: {"alliance_id": None},  # override manual
}


@pytest.fixture
def clf(monkeypatch):
    monkeypatch.setattr(standings, "corp_info", lambda cid: CORPS[cid])
    c = standings.Classifier.__new__(standings.Classifier)
    c.character_id, c.corp_id, c.alliance_id = 1, MY_CORP, MY_ALLY
    c.overrides = {"structures": {"77": "red"}, "corporations": {"500": "red"}, "alliances": {}}
    c.contacts = [
        {200: 10.0},              # pessoal
        {301: 5.0},               # corp
        {950: -10.0, 400: 0.0},   # aliança
    ]
    return c


@pytest.mark.parametrize("owner,expected", [
    (100, "corp"), (101, "alliance"), (200, "blue"), (300, "red"),
    (301, "blue"),     # contato da corp (nível corp) vem antes do contato da aliança
    (400, "neutral"), (500, "red"), (None, "neutral"),
])
def test_classify(clf, owner, expected):
    assert clf.classify(owner) == expected


def test_structure_override_wins_over_everything(clf):
    assert clf.classify(100, structure_id=77) == "red"
