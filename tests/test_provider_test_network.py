"""The opt-in unit fixture must not disguise destination/security mistakes."""

import socket

import pytest

from saiverse import provider_security
from saiverse.provider_security import validate_provider_url


def test_only_the_declared_public_hosts_resolve(mock_provider_network):
    mock_provider_network("provider.example")
    validate_provider_url("https://provider.example/v1")
    with pytest.raises(AssertionError, match="Unexpected DNS lookup.*other.example"):
        validate_provider_url("https://other.example/v1")


@pytest.mark.parametrize("address", ["10.0.0.1", "192.168.1.1", "169.254.169.254", "fd00::1"])
def test_private_and_metadata_literals_remain_non_public(mock_provider_network, monkeypatch, address):
    monkeypatch.delenv("SAIVERSE_PROVIDER_ALLOWED_HOSTS", raising=False)
    mock_provider_network()
    hostname = f"[{address}]" if ":" in address else address
    with pytest.raises(ValueError, match="non-public address"):
        validate_provider_url(f"https://{hostname}/v1")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_remains_loopback(mock_provider_network, host):
    mock_provider_network()
    validate_provider_url(f"http://{host}:1234/v1")
    # A name may only receive the public address when explicitly declared.
    assert provider_security.socket.getaddrinfo(host.strip("[]"), 1234)[0][4][0] in {"127.0.0.1", "::1"}


def test_fixture_leaves_the_process_socket_api_unchanged(mock_provider_network):
    original = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex)
    mock_provider_network("provider.example")
    assert (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex) == original
    validate_provider_url("https://provider.example/v1")


def test_testclient_works_with_windows_style_socketpair(mock_provider_network, monkeypatch):
    """Exercise the TCP self-pipe used on Windows, even on a Linux runner."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fallback_calls = []

    def fallback_socketpair():
        # Mirror the TCP connection used by CPython's Windows fallback using
        # public APIs: older supported Python versions do not expose the helper.
        fallback_calls.append(True)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.setblocking(False)
                try:
                    client.connect(listener.getsockname())
                except (BlockingIOError, InterruptedError):
                    pass
                client.setblocking(True)
                server, _ = listener.accept()
            except BaseException:
                client.close()
                raise
        return server, client

    monkeypatch.setattr(socket, "socketpair", fallback_socketpair)
    mock_provider_network("provider.example")
    app = FastAPI()

    @app.get("/validate")
    def validate():
        validate_provider_url("https://provider.example/v1")
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/validate").json() == {"ok": True}
    assert fallback_calls, "The event loop must exercise the TCP socketpair fallback"


@pytest.mark.parametrize("address", ["10.0.0.1", "169.254.169.254", "127.0.0.1", "fd00::1"])
def test_private_dns_answers_remain_rejected(mock_provider_network, monkeypatch, address):
    monkeypatch.delenv("SAIVERSE_PROVIDER_ALLOWED_HOSTS", raising=False)
    mock_provider_network("provider.example")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    answer = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    monkeypatch.setattr(
        provider_security.socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", answer)],
    )
    with pytest.raises(ValueError, match="non-public address"):
        validate_provider_url("https://provider.example/v1")
