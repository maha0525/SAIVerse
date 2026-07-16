"""Security policy binding provider credentials to their network destination."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit


def provider_credential_env(provider_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
    return f"SAIVERSE_PROVIDER_{normalized}_API_KEY"


def model_credential_env(model_key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", model_key).upper()
    return f"SAIVERSE_MODEL_{normalized}_API_KEY"


def _explicitly_allowed_hosts() -> set[str]:
    return {
        value.strip().lower()
        for value in os.getenv("SAIVERSE_PROVIDER_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }


def validate_provider_url(base_url: str) -> None:
    """Reject credential destinations that can reach undeclared local services."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provider base_url must not contain credentials, query, or fragment")

    hostname = parsed.hostname.lower()
    allowed = _explicitly_allowed_hosts()
    loopback_names = {"localhost", "localhost.localdomain"}
    if parsed.scheme == "http" and hostname not in allowed and hostname not in loopback_names:
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("Plain HTTP provider URLs require loopback or an explicit allowed host")
        except ValueError as exc:
            if str(exc).startswith("Plain HTTP"):
                raise
            raise ValueError("Plain HTTP provider URLs require loopback or an explicit allowed host") from exc

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except OSError as exc:
        raise ValueError(f"Provider host could not be resolved: {hostname}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback and (hostname in loopback_names or hostname in allowed or hostname == address):
            continue
        if hostname in allowed:
            continue
        if not ip.is_global:
            raise ValueError(
                f"Provider host resolves to a non-public address ({address}); "
                "add the host to SAIVERSE_PROVIDER_ALLOWED_HOSTS to permit it"
            )


def validate_provider_config(provider_id: str, config: dict) -> None:
    base_url = config.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        validate_provider_url(base_url.strip())
    if config.get("builtin"):
        return
    api_key_env = config.get("api_key_env")
    if api_key_env and api_key_env != provider_credential_env(provider_id):
        raise ValueError(
            f"Custom provider credentials must use {provider_credential_env(provider_id)}"
        )


def validate_model_config_connection(model_key: str, config: dict) -> None:
    """Ensure a model cannot pair a known secret with an unrelated endpoint."""
    from saiverse.provider_configs import PROVIDER_CONFIGS, get_provider

    base_url = config.get("base_url")
    api_key_env = config.get("api_key_env")
    provider_ref = config.get("provider_ref")

    if provider_ref:
        provider = get_provider(provider_ref)
        if provider is None:
            raise ValueError(f"Unknown provider_ref: {provider_ref}")
        validate_provider_config(provider_ref, provider)
        if api_key_env and api_key_env != provider.get("api_key_env"):
            raise ValueError("Model api_key_env must match its provider_ref credential")
        if base_url and str(base_url).rstrip("/") != str(provider.get("base_url") or "").rstrip("/"):
            raise ValueError("Model base_url must match its provider_ref destination")
        return

    if isinstance(base_url, str) and base_url.strip():
        validate_provider_url(base_url.strip())
    if not api_key_env:
        return

    if api_key_env == model_credential_env(model_key):
        return
    for provider_id, provider in PROVIDER_CONFIGS.items():
        if not provider.get("builtin"):
            continue
        if provider.get("api_key_env") != api_key_env:
            continue
        if str(provider.get("base_url") or "").rstrip("/") == str(base_url or "").rstrip("/"):
            return
    raise ValueError(
        f"Model credential {api_key_env!r} is not bound to this endpoint; "
        f"use provider_ref or {model_credential_env(model_key)}"
    )
