"""テスト環境の起動経路が、本番の Discord ゲートウェイ設定を「ゲートウェイを止める値」で上書きしているかの契約テスト。

cmd の set "VAR=" は変数を消し、消えた変数は load_dotenv() が .env の本番値で
埋め直す。だから値は空であってはならず、起動スクリプト (bat / sh) と
Python ランナーが入れる値は一致していなければならない。
"""
from __future__ import annotations

import re
from pathlib import Path

from discord_gateway.config import GatewaySettings
from discord_gateway.integration import _gateway_enabled
from discord_gateway.mapping import ChannelMapping
from scripts._shared.gateway_isolation import (
    DISABLED_GATEWAY_ENV,
    force_discord_gateway_off,
)

ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "test_fixtures" / "start_test_server.bat"
SH = ROOT / "test_fixtures" / "start_test_server.sh"
RUNNERS = [
    ROOT / "scripts" / "run_conversation.py",
    ROOT / "scripts" / "run_day_sim.py",
]

_PRODUCTION_LIKE_ENV = {
    "SAIVERSE_GATEWAY_ENABLED": "1",
    "SAIVERSE_GATEWAY_WS_URL": "wss://production-gateway.invalid/ws",
    "SAIVERSE_GATEWAY_TOKEN": "PRODUCTION-TOKEN-SENTINEL",
    "SAIVERSE_GATEWAY_CHANNEL_MAP": (
        '[{"channel_id": "prod-channel", "city_id": "city_a",'
        ' "building_id": "room", "host_user_id": "host"}]'
    ),
}


def test_disabled_values_are_non_empty():
    # 空値は cmd で「変数を消す」になり、.env の本番値が戻ってくる
    assert all(DISABLED_GATEWAY_ENV.values())


def test_bat_launcher_sets_the_same_values():
    # cmd は .bat を CP932 で読むので、非 ASCII の行はコマンドとして実行されてしまう
    text = BAT.read_bytes().decode("ascii")
    found = dict(
        re.findall(r'^set "(SAIVERSE_GATEWAY_\w+)=(.*)"\s*$', text, re.MULTILINE)
    )
    assert found == DISABLED_GATEWAY_ENV


def test_sh_launcher_sets_the_same_values():
    text = SH.read_text(encoding="utf-8")
    found = {
        name: value.strip('"')
        for name, value in re.findall(
            r"^export (SAIVERSE_GATEWAY_\w+)=(\S*)\s*$", text, re.MULTILINE
        )
    }
    assert found == DISABLED_GATEWAY_ENV


def test_runners_force_the_gateway_off_before_building_the_manager():
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        call = source.find("force_discord_gateway_off()")
        build = source.find("SAIVerseManager(")
        assert call != -1, f"{path.name} does not call force_discord_gateway_off()"
        assert build != -1, f"{path.name} no longer builds SAIVerseManager"
        assert call < build, f"{path.name} builds the manager before forcing the gateway off"


def test_production_env_is_overridden(monkeypatch):
    for name, value in _PRODUCTION_LIKE_ENV.items():
        monkeypatch.setenv(name, value)
    assert _gateway_enabled() is True  # 上書き前は本番の設定が効いている

    force_discord_gateway_off()

    assert _gateway_enabled() is False
    settings = GatewaySettings(_env_file=None)
    assert settings.bot_ws_url == DISABLED_GATEWAY_ENV["SAIVERSE_GATEWAY_WS_URL"]
    assert (
        settings.handshake_token.get_secret_value()
        == DISABLED_GATEWAY_ENV["SAIVERSE_GATEWAY_TOKEN"]
    )
    assert ChannelMapping.from_environment().get("prod-channel") is None
