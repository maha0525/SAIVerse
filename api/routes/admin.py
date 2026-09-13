from dataclasses import dataclass, field
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

@dataclass
class EnvWriteResult:
    """環境変数の保存の結果。

    Attributes:
        notices: 画面に出す知らせ (人が読む日本語)。保存しなかったモデル設定と、
            新しい標準モデルに切り替えられなかったペルソナ。無ければ空。
        rejected_keys: 設定ファイルの無いモデルの名前だったので保存しなかった変数。
    """

    notices: List[str] = field(default_factory=list)
    rejected_keys: List[str] = field(default_factory=list)


def _drop_persona_llm_clients(manager: Any) -> int:
    count = 0
    for persona in manager.personas.values():
        drop = getattr(persona, "drop_llm_clients", None)
        if callable(drop):
            drop()
        else:
            persona._llm_client = None
            persona._lightweight_llm_client = None
            persona._lightweight_llm_client_initialized = False
        count += 1
    return count


def write_env_updates(updates: Dict[str, str]) -> EnvWriteResult:
    """Write environment variable updates to .env file and os.environ.

    This function can be called from other modules (e.g., tutorial.py) to
    persist env var changes without going through the HTTP endpoint.

    The whole write runs under the model settings lock
    (saiverse/persona_model_selection.py MODEL_SETTINGS_LOCK), whichever screen
    calls it, so two saves arriving together cannot drop each other's variables
    from .env, and the speaking models they decide cannot end up reversed
    (docs/intent/persona_model_selection.md, decision 5).

    A model role variable (``MODEL_ROLES``) whose non-empty value has no model
    definition is not written; the other variables in the same request are
    (decision 6). After a model role variable is written, every persona's
    speaking model is decided again (decision 1).
    """
    from saiverse.persona_model_selection import (
        MODEL_SETTINGS_LOCK,
        model_role_env_keys,
        reapply_speaking_models,
        rejected_global_model_message,
        split_undefined_model_updates,
    )

    result = EnvWriteResult()
    with MODEL_SETTINGS_LOCK:
        from saiverse import app_state

        manager = getattr(app_state, "manager", None)
        accepted, rejected = split_undefined_model_updates(updates)
        for item in rejected:
            LOGGER.warning(
                "Not saving %s=%r: no model definition with that name", item.env_key, item.value,
            )
            result.rejected_keys.append(item.env_key)
            result.notices.append(rejected_global_model_message(item, manager))
        if not accepted:
            return result

        current_data = read_env_file()
        new_lines = []
        updated_keys: set[str] = set()

        for key, value, original in current_data:
            if not key:
                new_lines.append(original)
            elif key in accepted:
                new_val = accepted[key]
                if " " in new_val or "=" in new_val:
                    new_lines.append(f'{key}="{new_val}"')
                else:
                    new_lines.append(f"{key}={new_val}")
                updated_keys.add(key)
            else:
                new_lines.append(original)

        for key, val in accepted.items():
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
        for key, val in accepted.items():
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
        if accepted.keys() & _GEMINI_ENV_KEYS:
            try:
                from saiverse.llm_router import rebuild_clients
                rebuild_clients()
            except Exception as e:
                LOGGER.warning("Failed to rebuild router Gemini clients: %s", e)

        # Invalidate cached LLM clients on all personas when API keys change
        _API_KEY_KEYWORDS = {"KEY", "TOKEN", "SECRET"}
        changed_api_keys = {k for k in accepted if any(kw in k.upper() for kw in _API_KEY_KEYWORDS)}
        if changed_api_keys:
            LOGGER.info("API key env vars changed: %s — invalidating persona LLM clients", changed_api_keys)
            try:
                if manager is not None:
                    count = _drop_persona_llm_clients(manager)
                    LOGGER.info("Invalidated LLM clients on %d personas (will re-create on next use)", count)
            except Exception as e:
                LOGGER.warning("Failed to invalidate persona LLM clients: %s", e)

            try:
                from saiverse.media_summary import invalidate_summary_client
                invalidate_summary_client()
            except Exception as e:
                LOGGER.warning("Failed to invalidate media summary client: %s", e)

        # モデルの役割の設定が変わったら、動いているペルソナの話すモデルをその場で
        # 決め直す (再起動しなくても効く — 決まったこと 1)。
        if manager is not None and accepted.keys() & model_role_env_keys():
            try:
                reapplied = reapply_speaking_models(manager)
                result.notices.extend(reapplied.notices())
            except Exception:
                LOGGER.warning(
                    "Failed to re-decide persona speaking models after saving model settings",
                    exc_info=True,
                )
    return result


@router.post("/env")
def update_env_vars(req: EnvUpdateRequest):
    """Update environment variables in .env file and runtime os.environ.

    The response's ``notices`` lists what the screen should show: model settings
    that were not saved because no model with that name exists, and personas
    that could not be switched to the new default model. It is an empty list
    when there is nothing to report. Refusing some variables still returns 200.
    """
    try:
        result = write_env_updates(req.updates)
        return {
            "success": True,
            "message": "Environment variables updated.",
            "notices": list(result.notices),
            # 保存しなかった変数。画面が「選んだ値に変わった」と表示し間違えないように返す
            "rejected_keys": list(result.rejected_keys),
        }
    except Exception as e:
        LOGGER.error(f"Failed to update .env: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
