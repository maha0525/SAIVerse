"""グローバル設定「ペルソナが喋っている間、自動でルームビューに切り替える」の API の回帰テスト。

- GET /api/config/room-view-auto-switch は呼ばれるたびに SAIVERSE_ROOM_VIEW_AUTO_SWITCH を読む (既定 OFF)
- POST は .env に "true" / "false" を書き、保存直後の GET が新しい値を返す
- 本物の .env は書かない (write_env_updates を差し替える)
"""
from unittest.mock import patch

import pytest

from api.routes.config import (
    RoomViewAutoSwitchRequest,
    get_room_view_auto_switch,
    set_room_view_auto_switch,
)

ENV_KEY = "SAIVERSE_ROOM_VIEW_AUTO_SWITCH"


def test_get_defaults_to_off(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    assert get_room_view_auto_switch() == {"enabled": False}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),
        ("TRUE", True),
        (" 1 ", True),
        ("yes", True),
        ("On", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("", False),
        ("  ", False),
        ("enabled", False),
    ],
)
def test_get_reads_env_spelling(monkeypatch, raw, expected):
    monkeypatch.setenv(ENV_KEY, raw)
    assert get_room_view_auto_switch() == {"enabled": expected}


def test_post_persists_and_get_follows(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    written: dict = {}

    def _fake_write_env_updates(updates: dict):
        written.update(updates)
        for k, v in updates.items():
            monkeypatch.setenv(k, v)

    with patch("api.routes.admin.write_env_updates", side_effect=_fake_write_env_updates):
        res_on = set_room_view_auto_switch(RoomViewAutoSwitchRequest(enabled=True))
        assert res_on == {"success": True, "enabled": True}
        assert written[ENV_KEY] == "true"
        assert get_room_view_auto_switch() == {"enabled": True}

        res_off = set_room_view_auto_switch(RoomViewAutoSwitchRequest(enabled=False))
        assert res_off == {"success": True, "enabled": False}
        assert written[ENV_KEY] == "false"
        assert get_room_view_auto_switch() == {"enabled": False}
