import logging
import os
from dataclasses import dataclass
from pathlib import Path


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


# --- Embedding model (fixed; docs/intent/embedding_gemma2_migration.md §1) ---
# Only one embedding model is supported: the 4-bit quantised ONNX export of
# EmbeddingGemma 2 (text part), pinned to one HuggingFace revision. There is no
# env / CLI switch to pick another model (2026-10-12 ruling).
EMBED_HF_REPO = "onnx-community/embeddinggemma-2-ONNX"
EMBED_HF_REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
# Identity recorded in embed_metadata.embed_model and compared at startup.
# It carries name + quantisation + revision so that a change of any of them
# is detected (comparing the bare model name would miss a quantisation swap).
EMBED_MODEL_ID = "embeddinggemma-2-q4@daa72c51"
EMBED_MODEL_DIRNAME = "embeddinggemma-2-q4"
EMBED_MODEL_ONNX = "onnx/model_q4.onnx"
EMBED_TOKENIZER = "tokenizer.json"
EMBED_MODEL_FILES = (
    EMBED_MODEL_ONNX,
    "onnx/model_q4.onnx_data",
    EMBED_TOKENIZER,
    "tokenizer_config.json",
    "config.json",
)
# Stored vector dimension (the graph's sentence_embedding output, not truncated).
EMBED_DIM = 768
# Token cap per input text (prompt included); longer inputs are truncated.
# Only the text fed to the model is cut; the stored memory text is untouched.
EMBED_MAX_TOKENS = 512


def default_embed_model_dir() -> Path:
    """Where the embedding model lives: ``<repo>/sbert/embeddinggemma-2-q4``."""
    return Path(__file__).resolve().parents[1] / "sbert" / EMBED_MODEL_DIRNAME


def missing_embed_model_files(directory: Path) -> list[str]:
    """Return the required model files that are absent from ``directory``."""
    return [rel for rel in EMBED_MODEL_FILES if not (directory / rel).is_file()]


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
    embed_model_path: str | None

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
    # Embedding model: fixed, no env override. embed_model_path is the local
    # model directory when it holds every required file; otherwise None, and
    # the Embedder downloads the pinned files into default_embed_model_dir()
    # on first load.
    embed_model = EMBED_MODEL_ID
    embed_model_path: str | None = None
    preferred_dir = default_embed_model_dir()
    if preferred_dir.exists():
        missing = missing_embed_model_files(preferred_dir)
        if not missing:
            embed_model_path = str(preferred_dir)
        else:
            logging.getLogger(__name__).warning(
                "SAIMemory embedding model directory %s is incomplete (missing: %s). "
                "The missing files will be downloaded when the model is loaded.",
                preferred_dir,
                ", ".join(missing),
            )

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
        embed_model_path=embed_model_path,
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
