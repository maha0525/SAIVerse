"""Shared pytest fixtures and helpers for SAIVerse test suite."""

import ipaddress
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest  # noqa: F401

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Re-export for backward compatibility
from tool_loader import load_builtin_tool  # noqa: E402, F401


@pytest.fixture
def mock_provider_network(monkeypatch):
    """Opt in to synthetic DNS for explicitly named, already-mocked providers.

    Call with the public hostnames this unit test expects. URL/credential
    validation still runs, numeric addresses retain their real classification,
    and unexpected provider DNS fails. The process socket API stays untouched
    so event-loop self-pipes keep working. HTTP/SDK mocking remains the test
    caller's responsibility; this fixture is not a network sandbox.
    """
    from saiverse import provider_security

    def install(*public_hosts):
        expected_hosts = set(public_hosts)

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            # Preserve literal private, metadata and loopback destinations;
            # inventing a public result for them would hide security failures.
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if host in {"localhost", "localhost.localdomain"}:
                    address = ipaddress.ip_address("127.0.0.1")
                elif host in expected_hosts:
                    address = ipaddress.ip_address("93.184.216.34")
                else:
                    raise AssertionError(f"Unexpected DNS lookup in mocked provider test: {host}") from None
            resolved_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, resolved_family):
                raise socket.gaierror(socket.EAI_NONAME, "No synthetic address in requested family")
            sockaddr = (str(address), port, 0, 0) if address.version == 6 else (str(address), port)
            return [(resolved_family, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", sockaddr)]

        # Replacing socket.getaddrinfo on the shared module also affects every
        # other consumer. Bind only provider_security's module reference instead.
        monkeypatch.setattr(provider_security, "socket", SimpleNamespace(getaddrinfo=getaddrinfo))

    return install


@pytest.fixture(autouse=True)
def _autonomous_driving_shipped(monkeypatch):
    """テスト中だけ v0.3 の止め具を外す (自律の駆動を「出荷済み」にする)。

    ``saiverse.autonomy_wiring.AUTONOMOUS_DRIVING_SHIPPED`` は v0.3 のリリース
    範囲を「形の層」に留めるための止め具で、本番では False (= 判断点・watchdog・
    コマの再予約が発火しない)。一方で既存の自律系テストは **v0.4 で配線する運転の
    設計そのもの**を固定している資産なので、止め具に引きずられて全部「何もしない」
    を検証するテストに化けてはいけない。だからテスト中は True にして、設計の
    振る舞いを検証し続ける。

    止め具そのものの回帰は ``tests/test_v03_autonomy_gate.py`` が、この固定具を
    明示的に外して (定数を False に戻して) 検証する。
    """
    from saiverse import autonomy_wiring

    monkeypatch.setattr(autonomy_wiring, "AUTONOMOUS_DRIVING_SHIPPED", True)
