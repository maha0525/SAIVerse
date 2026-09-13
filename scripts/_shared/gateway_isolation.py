"""テストの世界を本番の Discord ゲートウェイにつながせないための環境変数。

main.py や各ランナーの load_dotenv() は、未設定の環境変数をリポジトリの .env で
埋める。何もしなければ、テスト環境を指すプロセスにも .env の本番用
SAIVERSE_GATEWAY_* (トークン含む) が入る。discord_gateway/config.py も .env を
自分で読むので、変数を消すのではなく、空でない「ゲートウェイを止める値」で上書きする。

同じ値を test_fixtures/start_test_server.bat / .sh にも書いている。
値の一致は tests/test_sandbox_gateway_isolation.py が検査する。
"""
from __future__ import annotations

import os

DISABLED_GATEWAY_ENV: dict[str, str] = {
    "SAIVERSE_GATEWAY_ENABLED": "0",
    "SAIVERSE_GATEWAY_WS_URL": "ws://127.0.0.1:9/test-disabled",
    "SAIVERSE_GATEWAY_TOKEN": "test-disabled",
    "SAIVERSE_GATEWAY_CHANNEL_MAP": "[]",
}


def force_discord_gateway_off() -> None:
    """ゲートウェイを止める値で設定を上書きする。.env を読んだ前後どちらで呼んでもよい。"""
    os.environ.update(DISABLED_GATEWAY_ENV)
