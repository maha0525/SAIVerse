from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from api.deps import get_manager
import os
import sys
import re
from pathlib import Path
import logging

LOGGER = logging.getLogger(__name__)
router = APIRouter()

ENV_FILE_PATH = Path(".env")
SENSITIVE_KEYWORDS = ["KEY", "TOKEN", "SECRET", "PASSWORD"]

class EnvVar(BaseModel):
    key: str
    value: str
    is_sensitive: bool

class EnvUpdateRequest(BaseModel):
    updates: Dict[str, str]

def is_sensitive(key: str) -> bool:
    return any(k in key.upper() for k in SENSITIVE_KEYWORDS)

def read_env_file() -> List[tuple[str, str, str]]:
    if not ENV_FILE_PATH.exists():
        return []
    result = []
    try:
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                original = line.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    result.append(("", "", original))
                    continue
                match = re.match(r'^([^=]+)=(.*)$', stripped)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    result.append((key, value, original))
                else:
                    result.append(("", "", original))
    except Exception as e:
        LOGGER.error(f"Failed to read .env: {e}")
    return result

@router.get("/env", response_model=List[EnvVar])
def get_env_vars():
    """Get environment variables from .env file."""
    raw = read_env_file()
    vars_list = []
    seen_keys = set()
    
    for key, value, _ in raw:
        if key and key not in seen_keys:
            vars_list.append(EnvVar(
                key=key,
                value="********" if is_sensitive(key) and value else value,
                is_sensitive=is_sensitive(key)
            ))
            seen_keys.add(key)
    
    # Sort by key
    vars_list.sort(key=lambda x: x.key)
    return vars_list

def write_env_updates(updates: Dict[str, str]) -> None:
    """Write environment variable updates to .env file and os.environ.

    This function can be called from other modules (e.g., tutorial.py) to
    persist env var changes without going through the HTTP endpoint.
    """
    current_data = read_env_file()
    new_lines = []
    updated_keys: set[str] = set()

    for key, value, original in current_data:
        if not key:
            new_lines.append(original)
        elif key in updates:
            new_val = updates[key]
            if " " in new_val or "=" in new_val:
                new_lines.append(f'{key}="{new_val}"')
            else:
                new_lines.append(f"{key}={new_val}")
            updated_keys.add(key)
        else:
            new_lines.append(original)

    for key, val in updates.items():
        if key not in updated_keys:
            if " " in val or "=" in val:
                new_lines.append(f'{key}="{val}"')
            else:
                new_lines.append(f"{key}={val}")

    with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))
        if new_lines:
            f.write("\n")

    # Ensure .env is only readable by owner (Linux/macOS)
    if os.name != "nt":
        os.chmod(ENV_FILE_PATH, 0o600)

    # Also update os.environ so changes take effect immediately
    for key, val in updates.items():
        os.environ[key] = val

    # 冷えたウィンドウの見張りは「前回と同じ状態なら結果も同じ」で素通しする。
    # 環境変数で失敗していたペルソナは行も水位も動かないので、設定を直しても
    # 記録が残っている限り二度と試されない。鍵かどうかで絞らず、更新が成った
    # 全ての変数で記録を捨てる — 失効は「全員をもう一回だけ再検査させる」だけの
    # 安い操作で、絞る精度より漏れの無さが要る (例: 接続先の許可ホストの変更は
    # 鍵ではないが LLM 接続の成否を変える)。
    from sea.session_lifecycle import invalidate_cold_sweep_fingerprints

    invalidate_cold_sweep_fingerprints()

    # Rebuild router Gemini clients if relevant keys changed
    _GEMINI_ENV_KEYS = {"GEMINI_FREE_API_KEY", "GEMINI_API_KEY"}
    if updates.keys() & _GEMINI_ENV_KEYS:
        try:
            from saiverse.llm_router import rebuild_clients
            rebuild_clients()
        except Exception as e:
            LOGGER.warning("Failed to rebuild router Gemini clients: %s", e)

    # Invalidate cached LLM clients on all personas when API keys change
    _API_KEY_KEYWORDS = {"KEY", "TOKEN", "SECRET"}
    changed_api_keys = {k for k in updates if any(kw in k.upper() for kw in _API_KEY_KEYWORDS)}
    if changed_api_keys:
        LOGGER.info("API key env vars changed: %s — invalidating persona LLM clients", changed_api_keys)
        try:
            from saiverse.app_state import manager
            if manager is not None:
                count = 0
                for persona in manager.personas.values():
                    persona._llm_client = None
                    persona._lightweight_llm_client = None
                    persona._lightweight_llm_client_initialized = False
                    count += 1
                LOGGER.info("Invalidated LLM clients on %d personas (will re-create on next use)", count)
        except Exception as e:
            LOGGER.warning("Failed to invalidate persona LLM clients: %s", e)

        try:
            from saiverse.media_summary import invalidate_summary_client
            invalidate_summary_client()
        except Exception as e:
            LOGGER.warning("Failed to invalidate media summary client: %s", e)


@router.post("/env")
def update_env_vars(req: EnvUpdateRequest, manager=Depends(get_manager)):
    """Update environment variables in .env file and runtime os.environ.

    When the update sets SAIVERSE_DEFAULT_MODEL to a model whose definition
    exists, running personas without their own default model are switched to it
    as well, so they do not keep talking with the previous model until a restart.
    """
    try:
        write_env_updates(req.updates)
    except Exception as e:
        LOGGER.error(f"Failed to update .env: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    _apply_default_model_to_running_personas(manager, req.updates)
    return {"success": True, "message": "Environment variables updated."}


def _apply_default_model_to_running_personas(manager, updates: Dict[str, str]) -> None:
    """グローバル設定の標準モデルの変更を、動いているペルソナへ反映する。

    write_env_updates は .env と os.environ を書き換えるだけで、読み込み済みの
    ペルソナのモデルは変えない。反映しないと、個別の標準モデルを持たない
    ペルソナは再起動まで古いモデルで話し続けるのに、モデル設定の警告
    (GET /api/config/startup-warnings) はいまの環境変数を見るので消えてしまう。
    チュートリアルのプリセット適用 (api/routes/tutorial.py の
    auto_configure_models) と同じく update_default_model を呼ぶ。

    定義があるかは、起動時に標準モデルを引くのと同じ引き方 (設定キーの完全一致) で
    判定する。定義が無い値は反映しない — ペルソナはいまのモデルで動き続け、
    定義が無いことは警告が伝える。
    """
    from saiverse.model_defaults import MODEL_ROLES, role_model_is_defined

    value = updates.get(MODEL_ROLES["default_model"])
    if not value:
        return
    try:
        if not role_model_is_defined("default_model", value):
            LOGGER.warning(
                "SAIVERSE_DEFAULT_MODEL was set to '%s', which has no model definition; "
                "running personas keep their current model.",
                value,
            )
            return
        manager.update_default_model(value)
    except Exception:
        # .env と os.environ は書き換え済みなので、保存自体は失敗として返さない。
        LOGGER.warning(
            "Saved SAIVERSE_DEFAULT_MODEL='%s' but failed to switch running personas to it.",
            value,
            exc_info=True,
        )

@router.post("/restart")
def restart_server(background_tasks: BackgroundTasks):
    """Restart the server process."""
    def _restart():
        import time
        time.sleep(1)  # Give time for response to be sent

        # os.execv() replaces the process immediately without running
        # any Python cleanup (atexit, finally, shutdown hooks).
        # We must explicitly run shutdown() to save building histories,
        # stop background threads, persist session metadata, etc.
        try:
            from saiverse.app_state import manager
            if manager is not None:
                LOGGER.info("Running manager shutdown before restart...")
                manager.shutdown()
        except Exception as e:
            LOGGER.error("Failed to run shutdown before restart: %s", e, exc_info=True)

        LOGGER.warning("Restarting server via API request...")
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    background_tasks.add_task(_restart)
    return {"success": True, "message": "Server restarting..."}


class BackfillRequest(BaseModel):
    building_id: Optional[str] = None
    persona_id: Optional[str] = None
    dry_run: bool = False


@router.post("/backfill-item-descriptions")
def backfill_item_descriptions(req: BackfillRequest, manager=Depends(get_manager)) -> Dict[str, Any]:
    """Batch-generate descriptions for picture items with placeholder text."""
    try:
        result = manager.backfill_item_descriptions(
            building_id=req.building_id or None,
            persona_id=req.persona_id or None,
            dry_run=req.dry_run,
        )
        return result
    except Exception as exc:
        LOGGER.error("Backfill failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
