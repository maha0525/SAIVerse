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
    """Check if an environment variable is set and non-empty."""
    value = os.environ.get(env_key, "")
    return bool(value.strip())


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
    from model_configs import (
        get_model_choices_with_display_names,
        get_model_config,
        get_model_provider
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
            is_available=is_available
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
