from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from saiverse import addon_registry


def _payload() -> dict:
    return {
        "schema_version": 1,
        "updated_at": "2026-07-16T00:00:00Z",
        "addons": [
            {
                "id": "safe-addon",
                "display_name": "Safe Addon",
                "repo_url": "https://example.com/safe-addon.git",
                "latest": "1.0.0",
                "versions": [
                    {
                        "version": "1.0.0",
                        "commit": "a" * 40,
                    }
                ],
            }
        ],
    }


def _signed_document(payload: dict) -> tuple[dict, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature = private_key.sign(addon_registry._canonical_registry_payload(payload))
    return (
        {
            "signed": payload,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "official-v1",
                "value": base64.b64encode(signature).decode("ascii"),
            },
        },
        base64.b64encode(public_key).decode("ascii"),
    )


def test_official_registry_requires_and_reports_valid_signature() -> None:
    document, public_key = _signed_document(_payload())
    addon_registry.invalidate_cache()
    with patch.dict(
        addon_registry.os.environ,
        {addon_registry.ENV_REGISTRY_PUBLIC_KEY: public_key},
        clear=False,
    ), patch.object(addon_registry, "_http_fetch_registry", return_value=document):
        registry = addon_registry.fetch_registry(
            url=addon_registry.DEFAULT_REGISTRY_URL,
            force=True,
        )

    assert registry.trust_level == "official"
    assert registry.publisher_key_id == "official-v1"
    assert registry.get_addon("safe-addon") is not None


def test_tampered_official_registry_is_rejected() -> None:
    document, public_key = _signed_document(_payload())
    document["signed"]["addons"][0]["display_name"] = "Tampered"
    addon_registry.invalidate_cache()
    with patch.dict(
        addon_registry.os.environ,
        {addon_registry.ENV_REGISTRY_PUBLIC_KEY: public_key},
        clear=False,
    ), patch.object(addon_registry, "_http_fetch_registry", return_value=document):
        with pytest.raises(RuntimeError, match="signature verification failed"):
            addon_registry.fetch_registry(
                url=addon_registry.DEFAULT_REGISTRY_URL,
                force=True,
            )


def test_unsigned_remote_registry_requires_explicit_operator_opt_in() -> None:
    url = "https://third-party.example/registry.json"
    addon_registry.invalidate_cache()
    with patch.dict(addon_registry.os.environ, {}, clear=False), patch.object(
        addon_registry,
        "_http_fetch_registry",
        return_value=_payload(),
    ):
        addon_registry.os.environ.pop(addon_registry.ENV_ALLOW_UNSIGNED_REGISTRY, None)
        with pytest.raises(RuntimeError, match="requires explicit"):
            addon_registry.fetch_registry(url=url, force=True)

        addon_registry.os.environ[addon_registry.ENV_ALLOW_UNSIGNED_REGISTRY] = "true"
        registry = addon_registry.fetch_registry(url=url, force=True)

    assert registry.trust_level == "unsigned"
