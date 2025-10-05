import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def load_system_prompt() -> str | None:
    sp = os.getenv("SAIMEMORY_SYSTEM_PROMPT")
    if sp:
        return sp
    path = os.getenv("SAIMEMORY_SYSTEM_PROMPT_FILE")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None
    return None


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    openai_api_key: str | None
    gemini_api_key: str | None
    temperature: float
    db_path: str
    resource_id: str
    embed_model: str

    memory_enabled: bool
    last_messages: int
    semantic_recall: bool
    topk: int
    range_before: int
    range_after: int
    scope: str
    chunk_min_chars: int
    chunk_max_chars: int

    summary_enabled: bool
    summary_use_llm: bool
    summary_prerun: bool
    summary_max_chars: int

    debug: bool


def load_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    model = os.getenv("LLM_MODEL", "gpt-5").strip()
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    temperature = _get_float("SAIMEMORY_TEMPERATURE", 0.2)
    # Default to current working directory so 
    # placing .env inside sai_memory doesn't create sai_memory/sai_memory/...
    db_path = os.getenv("SAIMEMORY_DB_PATH", "memory.db")
    resource_id = os.getenv("SAIMEMORY_RESOURCE_ID", "default")
    # Embedding model (fastembed). Keep current default for compatibility.
    embed_model = os.getenv("SAIMEMORY_EMBED_MODEL", "BAAI/bge-small-en-v1.5").strip()

    memory_enabled = _get_bool("SAIMEMORY_MEMORY", True)
    last_messages = _get_int("SAIMEMORY_MEMORY_LAST_MESSAGES", 8)
    semantic_recall = _get_bool("SAIMEMORY_MEMORY_SEMANTIC_RECALL", True)
    topk = _get_int("SAIMEMORY_MEMORY_TOPK", 5)
    range_before = _get_int("SAIMEMORY_MEMORY_RANGE_BEFORE", 1)
    range_after = _get_int("SAIMEMORY_MEMORY_RANGE_AFTER", 1)
    scope = os.getenv("SAIMEMORY_MEMORY_SCOPE", "resource").strip().lower()
    chunk_min_chars = _get_int("SAIMEMORY_MEMORY_CHUNK_MIN_CHARS", 120)
    chunk_max_chars = _get_int("SAIMEMORY_MEMORY_CHUNK_MAX_CHARS", 480)
    if chunk_min_chars < 0:
        chunk_min_chars = 0
    if chunk_max_chars <= 0:
        chunk_max_chars = 1
    if chunk_min_chars > chunk_max_chars:
        chunk_min_chars = chunk_max_chars

    summary_enabled = _get_bool("SAIMEMORY_SUMMARY", True)
    summary_use_llm = _get_bool("SAIMEMORY_SUMMARY_USE_LLM", True)
    summary_prerun = _get_bool("SAIMEMORY_SUMMARY_PRERUN", False)
    summary_max_chars = _get_int("SAIMEMORY_SUMMARY_MAX_CHARS", 1200)

    debug = _get_bool("SAIMEMORY_DEBUG", False)

    return Settings(
        provider=provider,
        model=model,
        openai_api_key=openai_key,
        gemini_api_key=gemini_key,
        temperature=temperature,
        db_path=db_path,
        resource_id=resource_id,
        embed_model=embed_model,
        memory_enabled=memory_enabled,
        last_messages=last_messages,
        semantic_recall=semantic_recall,
        topk=topk,
        range_before=range_before,
        range_after=range_after,
        scope=scope,
        chunk_min_chars=chunk_min_chars,
        chunk_max_chars=chunk_max_chars,
        summary_enabled=summary_enabled,
        summary_use_llm=summary_use_llm,
        summary_prerun=summary_prerun,
        summary_max_chars=summary_max_chars,
        debug=debug,
    )
