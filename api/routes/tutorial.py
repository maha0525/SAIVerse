"""Tutorial API endpoints for initial setup wizard."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime
import os
import logging

from api.deps import get_manager, get_db
from database.models import UserSettings, User, City, AI
from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)
router = APIRouter()


# --- Pydantic Models ---

class TutorialStatusResponse(BaseModel):
    tutorial_completed: bool
    needs_initial_setup: bool
    last_tutorial_version: int


class CompleteTutorialRequest(BaseModel):
    version: int = 1


class ApiKeyStatusResponse(BaseModel):
    provider: str
    env_key: str
    is_set: bool
    display_name: str
    description: str
    free_label: Optional[str] = None
    free_note: Optional[str] = None


class ModelAvailability(BaseModel):
    id: str
    display_name: str
    provider: str
    is_available: bool
    supports_structured_output: bool = True


class AvailableModelsResponse(BaseModel):
    models: List[ModelAvailability]


# --- Provider Configuration ---

PROVIDER_CONFIG = [
    {
        "provider": "openai",
        "env_key": "OPENAI_API_KEY",
        "display_name": "OpenAI",
        "description": "GPT-4o、GPT-5.1、o3 など",
        "free_label": "無料あり?",
        "free_note": "2026年2月現在、OpenAIのプログラムにより、一部のモデルに毎日無料トークンが付与される場合があります。付与の基準は明示されていないため、プログラム対象になっているかご自身でご確認ください。",
    },
    {
        "provider": "gemini_free",
        "env_key": "GEMINI_FREE_API_KEY",
        "display_name": "Gemini (無料)",
        "description": "Gemini 3 Flash、Gemini 2.5 Flash、Gemini 2.5 Pro など（無料枠）",
        "free_label": "無料!",
    },
    {
        "provider": "gemini",
        "env_key": "GEMINI_API_KEY",
        "display_name": "Gemini (有料)",
        "description": "Gemini 3 Pro、Gemini 3 Flash、Gemini 2.5 Pro など",
    },
    {
        "provider": "anthropic",
        "env_key": "CLAUDE_API_KEY",
        "display_name": "Anthropic",
        "description": "Claude Sonnet 4.5、Claude Opus 4.6 など",
    },
    {
        "provider": "grok",
        "env_key": "XAI_API_KEY",
        "display_name": "Grok (xAI)",
        "description": "Grok 4.1 fast、Grok 4 など",
    },
    {
        "provider": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "display_name": "OpenRouter",
        "description": "複数プロバイダーへの統合アクセス",
        "free_label": "無料あり?",
        "free_note": "キャンペーン等で一部のモデルが無料で提供されていることがあります。",
    },
    {
        "provider": "nvidia",
        "env_key": "NVIDIA_API_KEY",
        "display_name": "Nvidia NIM",
        "description": "Qwen3 Coder、Mistral Large 3、GLM 4.7 など",
        "free_label": "無料!",
    },
]

# Provider to env key mapping
PROVIDER_ENV_KEYS = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GEMINI_FREE_API_KEY"],
    "ollama": [],  # Local, always available
    "llama_cpp": [],  # Local, always available
    "nvidia_nim": ["NVIDIA_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
}


def _is_env_key_set(env_key: str) -> bool:
    """Check if an environment variable is set and non-empty.

    Returns False for common placeholder patterns from .env.example
    (e.g., 'sk-...', 'AIza...', 'nvapi-...', 'xai-...').
    """
    value = os.environ.get(env_key, "").strip()
    if not value:
        return False
    # Reject common placeholder patterns (prefix + only dots)
    if value.endswith("...") and len(value) < 20:
        return False
    return True


def _is_provider_available(provider: str, api_key_env: Optional[str] = None) -> bool:
    """Check if a provider is available based on API key configuration.

    Args:
        provider: The provider name (e.g., "openai", "anthropic")
        api_key_env: Optional specific env key from model config

    Returns:
        True if API key is set or provider is local
    """
    # If specific api_key_env is provided in model config, check that
    if api_key_env:
        return _is_env_key_set(api_key_env)

    # Otherwise, check provider's default env keys
    env_keys = PROVIDER_ENV_KEYS.get(provider, [])
    if not env_keys:
        return True  # Local provider

    return any(_is_env_key_set(key) for key in env_keys)


# --- API Endpoints ---

@router.get("/status", response_model=TutorialStatusResponse)
def get_tutorial_status(db: Session = Depends(get_db)):
    """Get tutorial completion status."""
    user_settings = db.query(UserSettings).filter(UserSettings.USERID == 1).first()

    # Check if initial setup is needed (no cities or no AIs)
    city_count = db.query(City).count()
    ai_count = db.query(AI).count()
    needs_initial_setup = city_count == 0 or ai_count == 0

    if not user_settings:
        return TutorialStatusResponse(
            tutorial_completed=False,
            needs_initial_setup=needs_initial_setup,
            last_tutorial_version=0
        )

    return TutorialStatusResponse(
        tutorial_completed=user_settings.TUTORIAL_COMPLETED,
        needs_initial_setup=needs_initial_setup,
        last_tutorial_version=user_settings.LAST_TUTORIAL_VERSION
    )


@router.post("/complete")
def complete_tutorial(req: CompleteTutorialRequest, db: Session = Depends(get_db)):
    """Mark tutorial as completed."""
    user_settings = db.query(UserSettings).filter(UserSettings.USERID == 1).first()

    if not user_settings:
        # Create new settings record
        user_settings = UserSettings(
            USERID=1,
            TUTORIAL_COMPLETED=True,
            TUTORIAL_COMPLETED_AT=datetime.now(),
            LAST_TUTORIAL_VERSION=req.version
        )
        db.add(user_settings)
    else:
        user_settings.TUTORIAL_COMPLETED = True
        user_settings.TUTORIAL_COMPLETED_AT = datetime.now()
        user_settings.LAST_TUTORIAL_VERSION = req.version

    db.commit()
    return {"success": True}


@router.post("/reset")
def reset_tutorial(db: Session = Depends(get_db)):
    """Reset tutorial completion status (for re-running tutorial)."""
    user_settings = db.query(UserSettings).filter(UserSettings.USERID == 1).first()

    if user_settings:
        user_settings.TUTORIAL_COMPLETED = False
        db.commit()

    return {"success": True}


@router.get("/api-keys/status", response_model=List[ApiKeyStatusResponse])
def get_api_keys_status():
    """Get API key configuration status for each provider."""
    result = []

    for config in PROVIDER_CONFIG:
        is_set = _is_env_key_set(config["env_key"])
        result.append(ApiKeyStatusResponse(
            provider=config["provider"],
            env_key=config["env_key"],
            is_set=is_set,
            display_name=config["display_name"],
            description=config["description"],
            free_label=config.get("free_label"),
            free_note=config.get("free_note"),
        ))

    return result


@router.get("/available-models", response_model=AvailableModelsResponse)
def get_available_models():
    """Get list of models with availability status based on API keys."""
    from saiverse.model_configs import (
        get_model_choices_with_display_names,
        get_model_config,
        get_model_provider,
        supports_structured_output,
    )

    models = []

    for model_id, display_name in get_model_choices_with_display_names():
        config = get_model_config(model_id)
        provider = get_model_provider(model_id)

        # Check if api_key_env is specified in model config
        api_key_env = config.get("api_key_env")
        is_available = _is_provider_available(provider, api_key_env)

        models.append(ModelAvailability(
            id=model_id,
            display_name=display_name,
            provider=provider,
            is_available=is_available,
            supports_structured_output=supports_structured_output(model_id),
        ))

    return AvailableModelsResponse(models=models)


@router.get("/env-key-mapping")
def get_env_key_mapping():
    """Get mapping of provider names to environment variable names.

    Useful for frontend to know which env key to set for which provider.
    """
    return {
        "mapping": {
            config["provider"]: config["env_key"]
            for config in PROVIDER_CONFIG
        }
    }


# --- Model Role Presets ---

# The 6 model roles that can be auto-configured
MODEL_ROLES = {
    "default_model": "SAIVERSE_DEFAULT_MODEL",
    "lightweight_model": "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL",
    "agentic_model": "SAIVERSE_AGENTIC_MODEL",
    "memory_weave_model": "MEMORY_WEAVE_MODEL",
    "image_summary_model": "SAIVERSE_IMAGE_SUMMARY_MODEL",
    "task_creation_model": "SAIVERSE_TASK_CREATION_MODEL",
}

MODEL_ROLE_DESCRIPTIONS = {
    "default_model": {
        "label": "標準モデル",
        "description": "会話や複雑な推論に使用するメインモデル",
    },
    "lightweight_model": {
        "label": "軽量モデル",
        "description": "ルーティングやツール判断に使用する高速・安価なモデル",
    },
    "agentic_model": {
        "label": "エージェントモデル",
        "description": "構造化出力を使うエージェントタスク用モデル",
    },
    "memory_weave_model": {
        "label": "Memory Weaveモデル",
        "description": "クロニクル・メモペディアの生成に使用するモデル",
    },
    "image_summary_model": {
        "label": "画像要約モデル",
        "description": "画像・ドキュメント要約生成用モデル（Vision対応モデル推奨）",
    },
    "task_creation_model": {
        "label": "タスク生成モデル",
        "description": "タスクの自動生成に使用するモデル",
    },
}

# Provider presets: values are config keys (filename stems).
# image_summary_model is also a config key (resolved via find_model_config at runtime).
# None means "not applicable for this provider" — will fall back to Gemini default if available.
PROVIDER_PRESETS: Dict[str, Dict[str, Optional[str]]] = {
    "gemini_paid": {
        "default_model": "gemini-3-flash-preview-paid",
        "lightweight_model": "gemini-2.5-flash-lite-preview-09-2025-paid",
        "agentic_model": "gemini-2.5-flash-lite-preview-09-2025-paid",
        "memory_weave_model": "gemini-2.5-flash-lite-preview-09-2025-paid",
        "image_summary_model": "gemini-2.5-flash-lite-preview-09-2025-paid",
        "task_creation_model": "gemini-3-flash-preview-paid",
    },
    "gemini_free": {
        "default_model": "gemini-3-flash-preview",
        "lightweight_model": "gemini-2.5-flash-lite-preview-09-2025",
        "agentic_model": "gemini-2.5-flash-lite-preview-09-2025",
        "memory_weave_model": "gemini-2.5-flash-lite-preview-09-2025",
        "image_summary_model": "gemini-2.5-flash-lite-preview-09-2025",
        "task_creation_model": "gemini-3-flash-preview",
    },
    "anthropic": {
        "default_model": "claude-sonnet-4-5",
        "lightweight_model": "claude-haiku-4-5",
        "agentic_model": "claude-haiku-4-5",
        "memory_weave_model": "claude-haiku-4-5",
        "image_summary_model": "claude-haiku-4-5",
        "task_creation_model": "claude-haiku-4-5",
    },
    "openai": {
        "default_model": "gpt-4o-2024-11-20",
        "lightweight_model": "gpt-5-nano",
        "agentic_model": "gpt-5-mini",
        "memory_weave_model": "gpt-5-mini",
        "image_summary_model": "gpt-5-nano",
        "task_creation_model": "gpt-5-mini",
    },
    "grok": {
        "default_model": "grok-4-1-fast-reasoning",
        "lightweight_model": "grok-4-1-fast-reasoning",
        "agentic_model": "grok-4-1-fast-reasoning",
        "memory_weave_model": "grok-4-1-fast-reasoning",
        "image_summary_model": "grok-4-1-fast-reasoning",
        "task_creation_model": "grok-4-1-fast-reasoning",
    },
    "openrouter": {
        "default_model": "openrouter-kimi-k2.5",
        "lightweight_model": "openrouter-qwen3-next-80b-a3b-instruct",
        "agentic_model": "openrouter-qwen3-next-80b-a3b-instruct",
        "memory_weave_model": "openrouter-qwen3-next-80b-a3b-instruct",
        "image_summary_model": "openrouter-kimi-k2.5",
        "task_creation_model": "openrouter-kimi-k2.5",
    },
    "openrouter_free": {
        "default_model": "openrouter-qwen3-coder-480b-a35b-free",
        "lightweight_model": "openrouter-qwen3-next-80b-a3b-instruct-free",
        "agentic_model": "openrouter-qwen3-next-80b-a3b-instruct-free",
        "memory_weave_model": "openrouter-qwen3-next-80b-a3b-instruct-free",
        "image_summary_model": None,
        "task_creation_model": "openrouter-qwen3-coder-480b-a35b-free",
    },
    "nvidia": {
        "default_model": "nim-qwen3-coder-480b-a35b-instruct",
        "lightweight_model": "nim-qwen3-next-80b-a3b-instruct",
        "agentic_model": "nim-qwen3-next-80b-a3b-instruct",
        "memory_weave_model": "nim-qwen3-next-80b-a3b-instruct",
        "image_summary_model": "nim-kimi-k2.5",
        "task_creation_model": "nim-qwen3-coder-480b-a35b-instruct",
    },
    "ollama": {
        "default_model": "ollama-qwen3-next-80b",
        "lightweight_model": "ollama-gpt-oss-20b",
        "agentic_model": "ollama-gpt-oss-20b",
        "memory_weave_model": "ollama-qwen3-next-80b",
        "image_summary_model": None,
        "task_creation_model": "ollama-qwen3-next-80b",
    },
}

# Auto-detection priority order
PROVIDER_PRIORITY = [
    "gemini_paid", "gemini_free", "anthropic", "openai",
    "grok", "openrouter", "openrouter_free", "nvidia", "ollama",
]

# Which env key to check for each preset provider
PROVIDER_DETECTION: Dict[str, Optional[str]] = {
    "gemini_paid": "GEMINI_API_KEY",
    "gemini_free": "GEMINI_FREE_API_KEY",
    "anthropic": "CLAUDE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openrouter_free": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "ollama": None,  # Always available (local)
}

# Display names for preset providers
PROVIDER_DISPLAY_NAMES = {
    "gemini_paid": "Gemini (有料)",
    "gemini_free": "Gemini (無料)",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "grok": "Grok (xAI)",
    "openrouter": "OpenRouter",
    "openrouter_free": "OpenRouter (無料)",
    "nvidia": "Nvidia NIM",
    "ollama": "Ollama (ローカル)",
}

# Default Gemini image summary model (used as fallback for non-Gemini providers)
_GEMINI_IMAGE_SUMMARY_DEFAULT = "gemini-2.5-flash-lite-preview-09-2025"


def _detect_best_provider() -> str:
    """Detect the highest-priority provider with an API key set."""
    for provider in PROVIDER_PRIORITY:
        env_key = PROVIDER_DETECTION.get(provider)
        if env_key is None:
            # Local provider (ollama) — always available, but lowest priority
            continue
        if _is_env_key_set(env_key):
            return provider
    return "ollama"


def _has_any_gemini_key() -> bool:
    """Check if any Gemini API key (free or paid) is available."""
    return _is_env_key_set("GEMINI_API_KEY") or _is_env_key_set("GEMINI_FREE_API_KEY")


# --- Model Preset Pydantic Models ---

class AutoConfigureRequest(BaseModel):
    provider: Optional[str] = None  # None = auto-detect


class ModelRoleAssignment(BaseModel):
    role: str
    label: str
    description: str
    env_key: str
    model_id: str
    display_name: str


class AutoConfigureResponse(BaseModel):
    provider: str
    provider_display: str
    assignments: List[ModelRoleAssignment]
    warnings: List[str]


# --- Model Preset Endpoints ---

@router.post("/auto-configure-models", response_model=AutoConfigureResponse)
def auto_configure_models(
    req: AutoConfigureRequest,
    manager=Depends(get_manager),
):
    """Auto-configure all 6 model role env vars based on available API keys.

    If provider is not specified, auto-detects the highest-priority provider
    with an API key set. Writes to .env, updates os.environ, and updates
    the base default model for personas without an explicit DB override.
    """
    from api.routes.admin import write_env_updates
    from saiverse.model_configs import get_model_display_name

    # 1. Determine provider
    provider = req.provider or _detect_best_provider()
    if provider not in PROVIDER_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    preset = PROVIDER_PRESETS[provider]
    provider_display = PROVIDER_DISPLAY_NAMES.get(provider, provider)

    # 2. Build env var updates and assignment list
    env_updates: Dict[str, str] = {}
    assignments: List[ModelRoleAssignment] = []
    warnings: List[str] = []

    for role, env_key in MODEL_ROLES.items():
        model_id = preset.get(role)

        # Handle image_summary_model fallback for non-Gemini providers
        if model_id is None and role == "image_summary_model":
            if _has_any_gemini_key():
                model_id = _GEMINI_IMAGE_SUMMARY_DEFAULT
            else:
                warnings.append(
                    "画像要約モデルはGemini専用のため、Gemini APIキーがない場合は機能しません。"
                )
                continue

        if model_id is None:
            continue

        env_updates[env_key] = model_id

        display_name = get_model_display_name(model_id)
        if not display_name or display_name == model_id:
            display_name = model_id

        role_desc = MODEL_ROLE_DESCRIPTIONS.get(role, {})
        assignments.append(ModelRoleAssignment(
            role=role,
            label=role_desc.get("label", role),
            description=role_desc.get("description", ""),
            env_key=env_key,
            model_id=model_id,
            display_name=display_name,
        ))

    # 3. Write to .env and os.environ
    if env_updates:
        write_env_updates(env_updates)

    # 4. Update base default model (without global override)
    default_model = preset.get("default_model")
    if default_model:
        try:
            manager.update_default_model(default_model)
        except Exception as exc:
            LOGGER.warning("Failed to update default model to %s: %s", default_model, exc)

    LOGGER.info(
        "Auto-configured models for provider=%s: %s",
        provider,
        {k: v for k, v in env_updates.items()},
    )

    return AutoConfigureResponse(
        provider=provider,
        provider_display=provider_display,
        assignments=assignments,
        warnings=warnings,
    )


@router.get("/model-roles")
def get_model_roles():
    """Get current model role assignments and available provider presets."""
    from saiverse.model_configs import get_model_display_name

    current = {}
    for role, env_key in MODEL_ROLES.items():
        value = os.environ.get(env_key, "")
        role_desc = MODEL_ROLE_DESCRIPTIONS.get(role, {})
        display_name = ""
        if value:
            display_name = get_model_display_name(value)
            if not display_name or display_name == value:
                display_name = value

        current[role] = {
            "env_key": env_key,
            "value": value,
            "display_name": display_name,
            "label": role_desc.get("label", role),
            "description": role_desc.get("description", ""),
        }

    available_presets = []
    for provider_key in PROVIDER_PRIORITY:
        detection_key = PROVIDER_DETECTION.get(provider_key)
        is_available = detection_key is None or _is_env_key_set(detection_key)
        available_presets.append({
            "provider": provider_key,
            "display_name": PROVIDER_DISPLAY_NAMES.get(provider_key, provider_key),
            "is_available": is_available,
        })

    return {
        "current": current,
        "presets": available_presets,
    }
