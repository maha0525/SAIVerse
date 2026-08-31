"""アドオンカタログ API。

GET  /api/addon-catalog/registry       - registry.json fetch (キャッシュ済み)
GET  /api/addon-catalog/installed      - 現在 installed なアドオン一覧
POST /api/addon-catalog/install        - インストール (SSE 進捗ストリーム)
POST /api/addon-catalog/update         - 更新 (SSE 進捗ストリーム)
POST /api/addon-catalog/uninstall      - アンインストール (SSE 進捗ストリーム)

設計は ``docs/intent/addon_catalog_management.md`` を参照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_manager
from saiverse.addon_installer import (
    AddonInstallError,
    AddonManifestError,
    ProgressEvent,
    install_addon,
    uninstall_addon,
    update_addon,
)
from saiverse.addon_manifest import AddonManifest, load_manifest
from saiverse.addon_registry import (
    DEFAULT_REGISTRY_URL,
    ENV_REGISTRY_URL,
    Registry,
    fetch_registry,
    get_registry_url,
    invalidate_cache,
)
from saiverse.data_paths import EXPANSION_DATA_DIR

LOGGER = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Per-addon install lock (同時 install/update/uninstall を防ぐ)
# ---------------------------------------------------------------------------

_addon_locks: Dict[str, threading.Lock] = {}
_locks_master = threading.Lock()


def _get_lock(addon_id: str) -> threading.Lock:
    with _locks_master:
        lock = _addon_locks.get(addon_id)
        if lock is None:
            lock = threading.Lock()
            _addon_locks[addon_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class InstalledAddonInfo(BaseModel):
    """installed なアドオンの現在状態。"""
    addon_id: str
    display_name: str
    version: str
    manifest_version: int
    setup_version: int


class RegistryResponse(BaseModel):
    registry_url: str
    registry: Registry


class InstallRequest(BaseModel):
    addon_id: str
    version: Optional[str] = Field(
        None,
        description="導入するバージョン (省略時は latest)",
    )


class UpdateRequest(BaseModel):
    addon_id: str
    version: Optional[str] = Field(
        None, description="更新先バージョン (省略時は latest)"
    )


class UninstallRequest(BaseModel):
    addon_id: str
    delete_data: bool = Field(
        False,
        description="True で永続データ (~/.saiverse/user_data/addon_data/<id>/) も削除",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_version(registry: Registry, addon_id: str, version: Optional[str]):
    """registry から (commit, version_entry) を解決。

    version が None なら latest を返す。404 系エラーは HTTPException で raise。
    """
    entry = registry.get_addon(addon_id)
    if entry is None:
        raise HTTPException(404, detail=f"addon '{addon_id}' not found in registry")
    target_version = version or entry.latest
    ver = entry.get_version(target_version)
    if ver is None:
        raise HTTPException(
            404,
            detail=f"version '{target_version}' not found for addon '{addon_id}'",
        )
    return entry, ver


def _read_installed_manifest(addon_id: str) -> Optional[AddonManifest]:
    addon_dir = EXPANSION_DATA_DIR / addon_id
    manifest_path = addon_dir / "addon.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        return load_manifest(data)
    except Exception:
        LOGGER.warning(
            "addon_catalog: failed to read manifest for %s", addon_id, exc_info=True
        )
        return None


def _has_api_routes(addon_id: str) -> bool:
    return (EXPANSION_DATA_DIR / addon_id / "api_routes.py").exists()


def _try_register_addon(addon_id: str, manager) -> None:
    """インストール後に addon_loader の per-addon register API を呼ぶ。

    api_routes.py を持つアドオンは動的 router 追加が不可なので、上位 (SSE
    呼び出し側) で restart_required を立てる前提。ここでは integrations と
    server_hooks の register のみ行う。
    """
    try:
        from saiverse.addon_loader import (
            register_addon_integrations,
            register_addon_server_hooks,
        )

        integration_manager = getattr(manager, "integration_manager", None)
        if integration_manager is not None:
            count_i = register_addon_integrations(integration_manager, addon_id)
            LOGGER.info(
                "addon_catalog: registered %d integration(s) for %s",
                count_i, addon_id,
            )
        count_h = register_addon_server_hooks(addon_id)
        LOGGER.info(
            "addon_catalog: registered %d server_hook(s) for %s", count_h, addon_id
        )
    except Exception:
        LOGGER.exception(
            "addon_catalog: failed to register addon '%s' (manual restart 推奨)",
            addon_id,
        )


def _try_unregister_addon(addon_id: str, manager) -> None:
    try:
        from saiverse.addon_loader import (
            unregister_addon_integrations,
            unregister_addon_server_hooks,
        )

        integration_manager = getattr(manager, "integration_manager", None)
        if integration_manager is not None:
            unregister_addon_integrations(integration_manager, addon_id)
        unregister_addon_server_hooks(addon_id)
    except Exception:
        LOGGER.exception(
            "addon_catalog: failed to unregister addon '%s' (manual restart 推奨)",
            addon_id,
        )


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------

@router.get("/registry", response_model=RegistryResponse)
def get_registry(force: bool = Query(False, description="True でキャッシュを無視して再 fetch")):
    """registry.json を fetch (キャッシュ済み) して返す。"""
    if force:
        invalidate_cache()
    try:
        registry = fetch_registry(force=force)
    except RuntimeError as e:
        raise HTTPException(503, detail=str(e)) from e
    return RegistryResponse(registry_url=get_registry_url(), registry=registry)


@router.get("/installed", response_model=List[InstalledAddonInfo])
def list_installed():
    """expansion_data/ 配下にある全アドオンの現在状態を返す。"""
    result: List[InstalledAddonInfo] = []
    if not EXPANSION_DATA_DIR.exists():
        return result
    for addon_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
        if not addon_dir.is_dir():
            continue
        manifest = _read_installed_manifest(addon_dir.name)
        if manifest is None:
            continue
        result.append(InstalledAddonInfo(
            addon_id=manifest.name,
            display_name=manifest.display_name,
            version=manifest.version,
            manifest_version=manifest.manifest_version,
            setup_version=manifest.setup_version,
        ))
    return result


# ---------------------------------------------------------------------------
# SSE: 進捗ストリーミング
# ---------------------------------------------------------------------------

def _format_sse(event: Dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=False)
    return f"data: {payload}\n\n"


async def _run_with_progress_sse(
    operation_label: str,
    addon_id: str,
    runner: "callable[[ProgressEvent.__class__], Any]",  # type: ignore[name-defined]
    require_lock: bool = True,
):
    """worker thread で installer を回し、進捗を SSE で stream するヘルパ。

    runner は ``progress_callback`` を 1 引数で受け取る関数。worker thread 内で
    呼ばれる。完了 / エラーで queue に sentinel を入れて async 側を終了させる。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL_DONE = object()

    lock = _get_lock(addon_id) if require_lock else None
    if lock is not None and not lock.acquire(blocking=False):
        raise HTTPException(
            409,
            detail=f"addon '{addon_id}' は他の install/update/uninstall 処理中です",
        )

    final_state: Dict[str, Any] = {"ok": False, "error": None, "manifest": None}

    def thread_progress(event: ProgressEvent) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("progress", event.to_dict()))

    def thread_worker() -> None:
        try:
            result = runner(thread_progress)
            final_state["ok"] = True
            if isinstance(result, AddonManifest):
                final_state["manifest"] = {
                    "name": result.name,
                    "version": result.version,
                    "setup_version": result.setup_version,
                }
        except (AddonInstallError, AddonManifestError) as e:
            final_state["error"] = str(e)
            LOGGER.warning("addon_catalog[%s]: %s failed: %s", addon_id, operation_label, e)
        except Exception as e:
            final_state["error"] = f"unexpected error: {e}"
            LOGGER.exception(
                "addon_catalog[%s]: %s unexpected failure", addon_id, operation_label
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", SENTINEL_DONE))

    threading.Thread(
        target=thread_worker,
        name=f"addon-catalog-{operation_label}-{addon_id}",
        daemon=True,
    ).start()

    async def generate():
        try:
            yield _format_sse({
                "phase": "started",
                "operation": operation_label,
                "addon_id": addon_id,
                "message": f"{operation_label} 開始: {addon_id}",
            })
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    break
                yield _format_sse({**payload, "addon_id": addon_id})

            restart_required = _has_api_routes(addon_id)
            final_event: Dict[str, Any] = {
                "phase": "finished",
                "operation": operation_label,
                "addon_id": addon_id,
                "ok": final_state["ok"],
                "restart_required": restart_required,
            }
            if final_state["error"]:
                final_event["error"] = final_state["error"]
            if final_state["manifest"]:
                final_event["manifest"] = final_state["manifest"]
            yield _format_sse(final_event)
        finally:
            if lock is not None:
                lock.release()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# POST endpoints (install / update / uninstall)
# ---------------------------------------------------------------------------

@router.post("/install")
async def post_install(req: InstallRequest, manager=Depends(get_manager)):
    """アドオンを registry 経由でインストール (SSE 進捗 stream)。"""
    try:
        registry = fetch_registry()
    except RuntimeError as e:
        raise HTTPException(503, detail=f"registry fetch failed: {e}") from e

    entry, ver = _resolve_version(registry, req.addon_id, req.version)
    addon_dir = EXPANSION_DATA_DIR / req.addon_id
    if addon_dir.exists():
        raise HTTPException(
            409,
            detail=f"addon '{req.addon_id}' は既にインストール済みです。"
                   f"更新は /update、削除は /uninstall を使ってください。",
        )

    def runner(progress_cb):
        manifest = install_addon(
            repo_url=entry.repo_url,
            commit=ver.commit,
            addon_id=req.addon_id,
            progress_callback=progress_cb,
        )
        _try_register_addon(req.addon_id, manager)
        return manifest

    return await _run_with_progress_sse(
        operation_label="install",
        addon_id=req.addon_id,
        runner=runner,
    )


@router.post("/update")
async def post_update(req: UpdateRequest, manager=Depends(get_manager)):
    """インストール済みアドオンを registry 経由で更新 (SSE 進捗 stream)。"""
    try:
        registry = fetch_registry()
    except RuntimeError as e:
        raise HTTPException(503, detail=f"registry fetch failed: {e}") from e

    addon_dir = EXPANSION_DATA_DIR / req.addon_id
    if not addon_dir.exists():
        raise HTTPException(
            404,
            detail=f"addon '{req.addon_id}' は未インストールです。/install を使ってください。",
        )
    _entry, ver = _resolve_version(registry, req.addon_id, req.version)

    def runner(progress_cb):
        manifest = update_addon(
            addon_id=req.addon_id,
            new_commit=ver.commit,
            progress_callback=progress_cb,
        )
        # update 時は再 register でいいが、unregister → register の順で
        # 古い hook を確実に消したい場合もある。Phase 2 では register のみ。
        _try_register_addon(req.addon_id, manager)
        return manifest

    return await _run_with_progress_sse(
        operation_label="update",
        addon_id=req.addon_id,
        runner=runner,
    )


@router.post("/uninstall")
async def post_uninstall(req: UninstallRequest, manager=Depends(get_manager)):
    """アドオンをアンインストール (SSE 進捗 stream)。"""
    addon_dir = EXPANSION_DATA_DIR / req.addon_id
    if not addon_dir.exists():
        raise HTTPException(404, detail=f"addon '{req.addon_id}' は未インストールです")

    def runner(progress_cb):
        # 先に unregister してから物理削除する (running な hook が file を握ら
        # ないように)
        _try_unregister_addon(req.addon_id, manager)
        uninstall_addon(
            addon_id=req.addon_id,
            delete_data=req.delete_data,
            progress_callback=progress_cb,
        )
        return None

    return await _run_with_progress_sse(
        operation_label="uninstall",
        addon_id=req.addon_id,
        runner=runner,
    )


# ---------------------------------------------------------------------------
# Debug / inspection
# ---------------------------------------------------------------------------

@router.get("/debug/registry-url")
def get_registry_debug_info():
    """現在の registry URL と env 上書きの状態を返す。"""
    return {
        "effective_url": get_registry_url(),
        "default_url": DEFAULT_REGISTRY_URL,
        "env_var": ENV_REGISTRY_URL,
        "env_override": ENV_REGISTRY_URL in __import__("os").environ,
    }
