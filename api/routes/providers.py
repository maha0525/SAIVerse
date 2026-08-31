"""Provider management API endpoints.

Allows the UI to list, create, edit, delete, and test connectivity for
LLM providers stored in builtin_data/providers/ and ~/.saiverse/user_data/providers/.

UI-driven creation is restricted to compatible-API protocols (openai_compat,
ollama_compat). Native protocols (anthropic, gemini, etc.) are builtin-only.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from saiverse import provider_configs
from saiverse.provider_security import validate_provider_config, validate_provider_url

LOGGER = logging.getLogger(__name__)
router = APIRouter()


# Protocols a user can create or edit from the UI. Native protocols
# (anthropic_native, gemini_native, etc.) require code support in
# llm_clients/factory.py and are intentionally builtin-only.
VALID_UI_PROTOCOLS = {"openai_compat", "ollama_compat"}

# All known protocols accepted on update. Used to validate that update
# requests don't introduce unknown protocol names by typo.
ALL_PROTOCOLS = {
    "openai_compat", "ollama_compat", "anthropic_native", "gemini_native",
    "xai_native", "nvidia_nim", "openai_codex",
}

CONNECTION_TEST_TIMEOUT = 5.0


class ProviderInfo(BaseModel):
    """Provider info returned to the UI."""
    id: str
    display_name: str
    protocol: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    builtin: bool = False
    # Whether the api_key_env variable is set in the environment.
    # None means no api_key_env is configured (e.g., local Ollama).
    api_key_configured: Optional[bool] = None
    # False for backends that accept any key (local servers such as LM Studio
    # or llama.cpp). Such providers stay usable with no key configured.
    api_key_required: Optional[bool] = None
    default_request_kwargs: Optional[dict[str, Any]] = None
    default_convert_system_to_user: Optional[bool] = None
    default_supports_images: Optional[bool] = None
    default_max_image_bytes: Optional[int] = None


class ProviderCreateRequest(BaseModel):
    id: str
    display_name: str
    protocol: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_required: Optional[bool] = None
    default_request_kwargs: Optional[dict[str, Any]] = None
    default_convert_system_to_user: Optional[bool] = None
    default_supports_images: Optional[bool] = None
    default_max_image_bytes: Optional[int] = None


class ProviderUpdateRequest(BaseModel):
    """All fields optional; only provided fields are updated."""
    display_name: Optional[str] = None
    protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_required: Optional[bool] = None
    default_request_kwargs: Optional[dict[str, Any]] = None
    default_convert_system_to_user: Optional[bool] = None
    default_supports_images: Optional[bool] = None
    default_max_image_bytes: Optional[int] = None


class ConnectionTestResponse(BaseModel):
    success: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    # Discovered model IDs from the provider's listing endpoint
    models: Optional[list[str]] = None
    elapsed_ms: Optional[int] = None


class InlineConnectionTestRequest(BaseModel):
    """Test a provider connection with inline config, without saving first.

    Used by the UI to verify settings during create/edit before persisting.
    """
    protocol: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    provider_id: Optional[str] = None


def _to_provider_info(pid: str, cfg: dict) -> ProviderInfo:
    api_key_env = cfg.get("api_key_env")
    api_key_configured: Optional[bool] = None
    if api_key_env:
        api_key_configured = bool(os.environ.get(api_key_env))
    return ProviderInfo(
        id=pid,
        display_name=cfg.get("display_name", pid),
        protocol=cfg.get("protocol", "unknown"),
        base_url=cfg.get("base_url"),
        api_key_env=api_key_env,
        builtin=cfg.get("source") == provider_configs.SOURCE_BUILTIN,
        api_key_configured=api_key_configured,
        api_key_required=cfg.get("api_key_required"),
        default_request_kwargs=cfg.get("default_request_kwargs"),
        default_convert_system_to_user=cfg.get("default_convert_system_to_user"),
        default_supports_images=cfg.get("default_supports_images"),
        default_max_image_bytes=cfg.get("default_max_image_bytes"),
    )


@router.get("", response_model=list[ProviderInfo])
def list_providers():
    """List all providers (builtin + user_data)."""
    return [
        _to_provider_info(pid, cfg)
        for pid, cfg in provider_configs.PROVIDER_CONFIGS.items()
    ]


@router.get("/{provider_id}", response_model=ProviderInfo)
def get_provider(provider_id: str):
    cfg = provider_configs.get_provider(provider_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider_id}")
    return _to_provider_info(provider_id, cfg)


@router.post("", response_model=ProviderInfo, status_code=201)
def create_provider(req: ProviderCreateRequest):
    """Create a new user_data provider.

    UI is restricted to protocols in VALID_UI_PROTOCOLS. Other protocols
    can only be added by manually placing files under builtin_data/providers/.
    """
    if req.protocol not in VALID_UI_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Protocol '{req.protocol}' is not creatable from the UI. "
                f"Allowed: {sorted(VALID_UI_PROTOCOLS)}"
            ),
        )
    if provider_configs.get_provider(req.id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Provider '{req.id}' already exists. Use PUT to update.",
        )
    payload = req.model_dump(exclude={"id"}, exclude_none=True)
    try:
        # save_provider always writes to user_data, so validate it as the
        # layer it is about to land in.
        validate_provider_config(
            req.id, {**payload, "source": provider_configs.SOURCE_USER_DATA}
        )
        provider_configs.save_provider(req.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cfg = provider_configs.get_provider(req.id)
    return _to_provider_info(req.id, cfg)


@router.put("/{provider_id}", response_model=ProviderInfo)
def update_provider(provider_id: str, req: ProviderUpdateRequest):
    """Update a provider.

    Builtin providers automatically get a user_data override on update
    (since save_provider always writes to user_data). The override then
    takes priority on next reload.
    """
    existing = provider_configs.get_provider(provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider_id}")

    # Merge the patch onto the existing config; unspecified fields keep their values
    merged = dict(existing)
    update_data = req.model_dump(exclude_none=True)
    merged.update(update_data)
    # The edit always lands in user_data, whatever layer it was read from.
    # save_provider() strips this again before writing.
    merged["source"] = provider_configs.SOURCE_USER_DATA

    if merged.get("protocol") not in ALL_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid protocol: {merged.get('protocol')!r}",
        )

    try:
        validate_provider_config(provider_id, merged)
        provider_configs.save_provider(provider_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    cfg = provider_configs.get_provider(provider_id)
    return _to_provider_info(provider_id, cfg)


@router.delete("/{provider_id}", status_code=204)
def delete_provider(provider_id: str):
    """Delete a user_data provider.

    Builtin-only providers cannot be deleted (returns 403). Deleting a
    user_data override of a builtin restores the builtin on next reload.
    """
    try:
        provider_configs.delete_provider(provider_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider_id}")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return None


@router.get("/{provider_id}/models", response_model=list[str])
def list_models_for_provider(provider_id: str):
    """List model config keys that reference this provider via provider_ref.

    Used by the UI to warn before deleting a provider that is in use.
    """
    if provider_configs.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider_id}")
    return provider_configs.list_models_using_provider(provider_id)


@router.post("/reload", response_model=list[ProviderInfo])
def reload_providers():
    """Reload provider configurations from disk.

    Models inline their provider's endpoint and credential at load time, so
    they are re-resolved here too; otherwise they would keep pointing at the
    definition this call just replaced.
    """
    provider_configs.reload_configs()
    provider_configs.reload_models_after_provider_change()
    return [
        _to_provider_info(pid, cfg)
        for pid, cfg in provider_configs.PROVIDER_CONFIGS.items()
    ]


def _run_connection_test(
    protocol: str,
    base_url: Optional[str],
    api_key_env: Optional[str],
    *,
    provider_id: Optional[str] = None,
    source: Optional[str] = None,
) -> ConnectionTestResponse:
    """Execute the actual connection probe. Shared by inline and saved-provider routes."""
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return ConnectionTestResponse(
            success=False,
            error="base_url が設定されていません",
        )

    try:
        if api_key_env:
            # A credential really does leave the machine here, so the full
            # policy applies: this name must be allowed to reach this host.
            validate_provider_config(
                provider_id or "inline",
                {
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                    "source": source,
                },
            )
        else:
            # Nothing is sent, so only the destination is checked. This answers
            # "can I reach this endpoint", not "would this config be accepted on
            # save" — a definition that saving would reject can still be probed,
            # as long as the probe carries no credential.
            validate_provider_url(base_url)
    except ValueError as exc:
        return ConnectionTestResponse(success=False, error=str(exc))

    start = time.monotonic()

    try:
        if protocol == "openai_compat":
            url = f"{base_url}/models"
            headers: dict[str, str] = {}
            if api_key_env:
                api_key = os.environ.get(api_key_env, "")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
            with httpx.Client(timeout=CONNECTION_TEST_TIMEOUT) as client:
                resp = client.get(url, headers=headers)
        elif protocol == "ollama_compat":
            url = f"{base_url}/api/tags"
            with httpx.Client(timeout=CONNECTION_TEST_TIMEOUT) as client:
                resp = client.get(url)
        else:
            return ConnectionTestResponse(
                success=False,
                error=f"このプロトコルは接続テスト未対応: {protocol}",
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            return ConnectionTestResponse(
                success=False,
                status_code=resp.status_code,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                elapsed_ms=elapsed_ms,
            )

        # Parse response to extract model IDs (different per protocol)
        models: list[str] = []
        try:
            data = resp.json()
            if protocol == "openai_compat":
                # OpenAI format: {"data": [{"id": "..."}, ...]}
                models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            elif protocol == "ollama_compat":
                # Ollama format: {"models": [{"name": "..."}, ...]}
                models = [m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
        except Exception as exc:
            LOGGER.warning("Failed to parse provider test response: %s", exc)

        return ConnectionTestResponse(
            success=True,
            status_code=resp.status_code,
            models=models,
            elapsed_ms=elapsed_ms,
        )
    except httpx.TimeoutException:
        return ConnectionTestResponse(
            success=False,
            error=f"接続タイムアウト ({CONNECTION_TEST_TIMEOUT}s)",
        )
    except httpx.ConnectError as exc:
        return ConnectionTestResponse(
            success=False,
            error=f"接続失敗: {exc}",
        )
    except Exception as exc:
        LOGGER.exception("Provider connection test failed")
        return ConnectionTestResponse(
            success=False,
            error=f"予期しないエラー: {exc}",
        )


@router.post("/test", response_model=ConnectionTestResponse)
def test_inline_connection(req: InlineConnectionTestRequest):
    """Test a provider connection without saving (used by create/edit forms).

    The UI sends the current form values; we run the same probe logic as
    /providers/{id}/test but against the inline config.

    Note: must be registered BEFORE /{provider_id}/test so FastAPI does not
    treat "test" as a path parameter.
    """
    existing = provider_configs.get_provider(req.provider_id) if req.provider_id else None

    # Unlike saving, this route reads the named environment variable and sends
    # it to the requested host, and a loopback start has no owner
    # authentication in front of it. The provider_id is just a value in the
    # request body, so naming a saved provider must not by itself confer that
    # provider's trust — otherwise "provider_id: openrouter" plus someone
    # else's base_url would ship a shipped key anywhere. Trust is inherited
    # only when the payload still points where the saved provider points, with
    # the same credential. Anything else is probed as untrusted, which allows
    # no credential beyond this id's own namespaced variable (a probe with no
    # api_key_env sends nothing and stays allowed).
    def _same(a: object, b: object) -> bool:
        return str(a or "").rstrip("/") == str(b or "").rstrip("/")

    unchanged = bool(existing) and _same(req.base_url, existing.get("base_url")) \
        and _same(req.api_key_env, existing.get("api_key_env"))

    return _run_connection_test(
        req.protocol,
        req.base_url,
        req.api_key_env,
        provider_id=req.provider_id,
        source=existing.get("source") if unchanged else None,
    )


@router.post("/{provider_id}/test", response_model=ConnectionTestResponse)
def test_provider_connection(provider_id: str):
    """Test connectivity to a saved provider's endpoint."""
    cfg = provider_configs.get_provider(provider_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider_id}")
    return _run_connection_test(
        protocol=cfg.get("protocol", ""),
        base_url=cfg.get("base_url"),
        api_key_env=cfg.get("api_key_env"),
        provider_id=provider_id,
        source=cfg.get("source"),
    )
