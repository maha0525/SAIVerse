"""LAN IP 自動検出 helper。

MCP gateway などが「この SAIVerse ホスト宛に LAN 内デバイスから戻り
HTTP / WS 接続する宛先 IP」 を必要とするとき (例: stackchan-mcp の
VISION_HOST = ESP32 から PC への avatar 配信 URL host) に使う。

ユーザーが Wi-Fi を切り替えるたびに UI で IP を書き換える運用を避ける
ため、 ``${runtime.lan_ip}`` placeholder (= tools/mcp_config.py で解決)
の実装本体として呼ばれる。
"""
from __future__ import annotations

import logging
import socket
from typing import Optional

LOGGER = logging.getLogger(__name__)


def get_local_ip() -> Optional[str]:
    """この PC の外向きルーティングで使う LAN IP を返す。

    実装: UDP ソケットを 8.8.8.8:80 に向けて ``connect`` し (パケットは
    実際には送られない、 OS の routing decision だけ走る)、 ``getsockname()``
    で「OS がこの宛先に対して使うことに決めた送信元 IP」 を取る。 複数
    NIC でもデフォルトルートのインターフェースが選ばれるので意図通り。

    Returns:
        IPv4 string (e.g. ``"192.168.0.9"``) または ``None`` (オフライン、
        サンドボックスでネットワーク不可、 IP が ``0.0.0.0`` 等の異常時)。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError as exc:
        LOGGER.warning(
            "lan_ip: failed to detect local IP via 8.8.8.8 probe: %s", exc,
        )
        return None
    if not ip or ip == "0.0.0.0":
        LOGGER.warning("lan_ip: probe returned invalid address %r", ip)
        return None
    return ip
