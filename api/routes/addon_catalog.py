"""アドオンカタログ API。

GET  /api/addon-catalog/registry                     - registry.json fetch (キャッシュ済み)
GET  /api/addon-catalog/installed                    - 現在 installed なアドオン一覧
POST /api/addon-catalog/install/prepare              - 導入の一段目 (取得 + 質問と step の一覧)
POST /api/addon-catalog/install/confirm              - 導入の二段目 (SSE 進捗ストリーム)
POST /api/addon-catalog/install/cancel               - 導入の prepare を取り消す
POST /api/addon-catalog/update/prepare               - 更新の一段目
POST /api/addon-catalog/update/confirm               - 更新の二段目 (SSE 進捗ストリーム)
POST /api/addon-catalog/update/cancel                - 更新の prepare を取り消す
GET  /api/addon-catalog/installed/{addon_id}/options - 導入済みアドオンの質問の出し直し
POST /api/addon-catalog/installed/{addon_id}/options - 選択肢を足して反映 (SSE 進捗ストリーム)
POST /api/addon-catalog/uninstall                    - アンインストール (SSE 進捗ストリーム)

設計は ``docs/intent/addon_catalog_management.md`` を参照 (二段構えと質問は
「導入時の質問と、アドオン専用の Python 環境」の節)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_manager
from saiverse.addon_installer import (
    AddonAnswersError,
    AddonInstallError,
    AddonManifestError,
    AddonStateError,
    AddonVersionError,
    ProgressEvent,
    cancel_install,
    cancel_update,
    execute_install_plan,
    execute_options_plan,
    execute_update_plan,
    get_installed_options,
    plan_install_confirm,
    plan_options_apply,
    plan_update_confirm,
    prepare_install,
    prepare_update,
    uninstall_addon,
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


# addon_id の形式 (addon.json の name と同じ規則 — saiverse/addon_manifest.py)。
# installer の深い所 (get_addon_install_dir 等) でも不正なパスは ValueError で
# 止まるが、それは呼び出し順にたまたま守られている形なので、API の入口で
# 検査して 400 を返す。addon_id はこの後ファイルパスの組み立てに使われる。
_ADDON_ID_RE = re.compile(r"^[a-z][a-z0-9\-_]*$")


def _check_addon_id(addon_id: str) -> None:
    if not _ADDON_ID_RE.match(addon_id or ""):
        raise HTTPException(400, detail=f"invalid addon_id: {addon_id!r}")


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


class PrepareRequest(BaseModel):
    addon_id: str
    version: Optional[str] = Field(
        None,
        description="導入 / 更新するバージョン (省略時は latest)",
    )


class ConfirmRequest(BaseModel):
    addon_id: str
    answers: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="{質問 id: [選択肢 id, ...]} (一つだけ選ぶ質問も要素 1 の一覧)",
    )


class CancelRequest(BaseModel):
    addon_id: str


class OptionsApplyRequest(BaseModel):
    answers: Dict[str, List[str]] = Field(default_factory=dict)


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
            # manifest の display_name は {"ja": ..., "en": ...} の辞書でもよい
            # (AddonManifest.display_name)。ここは文字列の欄なので、辞書のまま
            # 渡すと検証で落ち、1 件でも辞書のアドオンがあると一覧全体が 500 になる。
            display_name=manifest.get_display_name(),
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
    held_lock: Optional[threading.Lock] = None,
):
    """worker thread で installer を回し、進捗を SSE で stream するヘルパ。

    runner は ``progress_callback`` を 1 引数で受け取る関数。worker thread 内で
    呼ばれる。完了 / エラーで queue に sentinel を入れて async 側を終了させる。
    ``held_lock`` には、呼び出し側が既に取っている per-addon lock を渡せる
    (confirm 系 — 計画と実行の間に cancel が割り込まないよう、計画の前から取る)。
    どちらの形でも、解放はストリーム終了時にこの関数が行う。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL_DONE = object()

    if held_lock is not None:
        lock: Optional[threading.Lock] = held_lock
    else:
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
# 二段構えの導入・更新、質問の出し直し
# ---------------------------------------------------------------------------
#
# prepare は取得 (git fetch) と manifest の検証だけを行い、質問と step の一覧を
# 返す (確認ダイアログに出す)。confirm は答えを受け取って checkout と step の
# 実行を SSE で流す。prepare と confirm の間の「どの commit を指しているか」は
# installer が ~/.saiverse/addon_install/<id>/pending.json に残す。


def _http_error(e: AddonInstallError) -> HTTPException:
    """installer の例外を HTTP エラーへ。"""
    if isinstance(e, AddonStateError):
        return HTTPException(409, detail=str(e))
    if isinstance(e, (AddonAnswersError, AddonVersionError, AddonManifestError)):
        return HTTPException(400, detail=str(e))
    # 残りは主に git の取得失敗 (ネットワーク・取得元)
    return HTTPException(502, detail=str(e))


def _with_lock(addon_id: str, fn):
    """per-addon lock を取って fn() を実行する (取れなければ 409)。"""
    lock = _get_lock(addon_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            409,
            detail=f"addon '{addon_id}' は他の install/update/uninstall 処理中です",
        )
    try:
        return fn()
    except AddonInstallError as e:
        raise _http_error(e) from e
    finally:
        lock.release()


def _fetch_registry_or_503() -> Registry:
    try:
        return fetch_registry()
    except RuntimeError as e:
        raise HTTPException(503, detail=f"registry fetch failed: {e}") from e


def _plan_or_http_error(fn):
    try:
        return fn()
    except AddonInstallError as e:
        raise _http_error(e) from e


def _plan_with_held_lock(addon_id: str, plan_fn):
    """confirm の計画を、実行まで持ち続ける鍵の中で立てる。

    計画 (pending の読み取り) と実行の間に cancel 等が割り込むと、計画が指す
    フォルダが消えてから実行が走る。鍵を計画の前に取り、実行側
    (``_run_with_progress_sse`` の ``held_lock``) まで持ち越すことで一続きにする。
    返値は (plan, lock)。計画で失敗したら鍵を放して HTTP エラーにする。
    """
    lock = _get_lock(addon_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            409,
            detail=f"addon '{addon_id}' は他の install/update/uninstall 処理中です",
        )
    try:
        return _plan_or_http_error(plan_fn), lock
    except BaseException:
        lock.release()
        raise


@router.post("/install/prepare")
def post_install_prepare(req: PrepareRequest):
    """導入の一段目: 取得して、質問と (この OS で当てはまる) step の一覧を返す。"""
    _check_addon_id(req.addon_id)
    registry = _fetch_registry_or_503()
    entry, ver = _resolve_version(registry, req.addon_id, req.version)

    def run():
        return prepare_install(
            repo_url=entry.repo_url,
            commit=ver.commit,
            addon_id=req.addon_id,
            min_saiverse_version=ver.min_saiverse_version,
            expansion_dir=EXPANSION_DATA_DIR,
        )

    prepared = _with_lock(req.addon_id, run)
    payload = prepared.to_dict()
    payload.pop("needs_setup", None)
    return payload


@router.post("/install/confirm")
async def post_install_confirm(req: ConfirmRequest, manager=Depends(get_manager)):
    """導入の二段目: 答えで選んだ step を実行する (SSE 進捗 stream)。"""
    _check_addon_id(req.addon_id)
    plan, lock = _plan_with_held_lock(
        req.addon_id,
        lambda: plan_install_confirm(req.addon_id, req.answers, EXPANSION_DATA_DIR),
    )

    def runner(progress_cb):
        manifest = execute_install_plan(plan, progress_callback=progress_cb)
        _try_register_addon(req.addon_id, manager)
        return manifest

    try:
        return await _run_with_progress_sse(
            operation_label="install",
            addon_id=req.addon_id,
            runner=runner,
            held_lock=lock,
        )
    except BaseException:
        lock.release()
        raise


@router.post("/install/cancel")
def post_install_cancel(req: CancelRequest):
    """導入の prepare で作ったフォルダを消す。"""
    _check_addon_id(req.addon_id)
    _with_lock(req.addon_id, lambda: cancel_install(req.addon_id, EXPANSION_DATA_DIR))
    return {"addon_id": req.addon_id, "cancelled": True}


@router.post("/update/prepare")
def post_update_prepare(req: PrepareRequest):
    """更新の一段目: カタログの repo_url から取得して、setup のやり直しの要否と、
    出し直す質問 (新しい質問・選択肢が増えた質問、答えが無ければ全部) を返す。"""
    _check_addon_id(req.addon_id)
    registry = _fetch_registry_or_503()
    addon_dir = EXPANSION_DATA_DIR / req.addon_id
    if not addon_dir.exists():
        raise HTTPException(
            404,
            detail=f"addon '{req.addon_id}' は未インストールです。/install/prepare を使ってください。",
        )
    entry, ver = _resolve_version(registry, req.addon_id, req.version)

    def run():
        return prepare_update(
            addon_id=req.addon_id,
            repo_url=entry.repo_url,
            new_commit=ver.commit,
            min_saiverse_version=ver.min_saiverse_version,
            expansion_dir=EXPANSION_DATA_DIR,
        )

    return _with_lock(req.addon_id, run).to_dict()


@router.post("/update/confirm")
async def post_update_confirm(req: ConfirmRequest, manager=Depends(get_manager)):
    """更新の二段目: checkout して、setup_version が上がっていれば setup をやり直す
    (SSE 進捗 stream)。"""
    _check_addon_id(req.addon_id)
    plan, lock = _plan_with_held_lock(
        req.addon_id,
        lambda: plan_update_confirm(req.addon_id, req.answers, EXPANSION_DATA_DIR),
    )

    def runner(progress_cb):
        manifest = execute_update_plan(plan, progress_callback=progress_cb)
        # update 時は再 register でいいが、unregister → register の順で
        # 古い hook を確実に消したい場合もある。Phase 2 では register のみ。
        _try_register_addon(req.addon_id, manager)
        return manifest

    try:
        return await _run_with_progress_sse(
            operation_label="update",
            addon_id=req.addon_id,
            runner=runner,
            held_lock=lock,
        )
    except BaseException:
        lock.release()
        raise


@router.post("/update/cancel")
def post_update_cancel(req: CancelRequest):
    """更新の prepare を取り消す (fetch しただけなので、記録を消すだけ)。"""
    _check_addon_id(req.addon_id)
    _with_lock(req.addon_id, lambda: cancel_update(req.addon_id))
    return {"addon_id": req.addon_id, "cancelled": True}


@router.get("/installed/{addon_id}/options")
def get_installed_addon_options(addon_id: str):
    """導入済みアドオンの質問 (保存済みの答えに selected: true) と、答えを足したときに
    実行の候補になる step を返す。"""
    _check_addon_id(addon_id)
    prepared = _plan_or_http_error(
        lambda: get_installed_options(addon_id, EXPANSION_DATA_DIR)
    )
    return {"questions": prepared.questions, "steps": prepared.steps}


@router.post("/installed/{addon_id}/options")
async def post_installed_addon_options(
    addon_id: str, req: OptionsApplyRequest, manager=Depends(get_manager)
):
    """選択肢を足して、新しく実行の条件を満たした step だけを実行する (SSE 進捗 stream)。"""
    _check_addon_id(addon_id)
    plan, lock = _plan_with_held_lock(
        addon_id,
        lambda: plan_options_apply(addon_id, req.answers, EXPANSION_DATA_DIR),
    )

    def runner(progress_cb):
        manifest = execute_options_plan(plan, progress_callback=progress_cb)
        _try_register_addon(addon_id, manager)
        return manifest

    try:
        return await _run_with_progress_sse(
            operation_label="options",
            addon_id=addon_id,
            runner=runner,
            held_lock=lock,
        )
    except BaseException:
        lock.release()
        raise


@router.post("/uninstall")
async def post_uninstall(req: UninstallRequest, manager=Depends(get_manager)):
    """アドオンをアンインストール (SSE 進捗 stream)。"""
    _check_addon_id(req.addon_id)
    addon_dir = EXPANSION_DATA_DIR / req.addon_id
    if not addon_dir.exists():
        raise HTTPException(404, detail=f"addon '{req.addon_id}' は未インストールです")

    def runner(progress_cb):
        # 先に unregister してから物理削除する (running な hook が file を握ら
        # ないように)
        _try_unregister_addon(req.addon_id, manager)
        # 専用の環境と答え (addon_install/<id>/) は uninstall_addon が必ず消す
        uninstall_addon(
            addon_id=req.addon_id,
            delete_data=req.delete_data,
            progress_callback=progress_cb,
            expansion_dir=EXPANSION_DATA_DIR,
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
