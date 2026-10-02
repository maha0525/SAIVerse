"""The opt-in unit fixture must not disguise destination/security mistakes."""

import socket

import pytest

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
    assert socket.getaddrinfo(host.strip("[]"), 1234)[0][4][0] in {"127.0.0.1", "::1"}


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_real_connections_are_blocked_even_to_loopback(mock_provider_network, method):
    mock_provider_network()
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="Real socket connection"):
            getattr(sock, method)(("127.0.0.1", 9))
