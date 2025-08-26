from __future__ import annotations

"""
Cognee-backed memory adapter for SAIVerse.

This provides a drop-in replacement of the minimal MemoryCore interface used by
persona_core.py: remember(text, conv_id, speaker, meta) and recall(query, k).

Design:
- Per persona dataset: use the persona_id as dataset name so each persona builds
  its own knowledge graph and vector index.
- Remember: ingest text via cognee.add(..., dataset_name=persona_id) and trigger
  cognify in background (incremental) so processing doesn't block.
- Recall: perform cognee.search with CHUNKS (LLM不要) 優先。必要に応じて INSIGHTS にも切替可能。

Note: Cognee APIs are async. This adapter runs them synchronously using asyncio.run
for simplicity. In UI runtime, calls are short and cognify runs in background.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
import threading
import os
import sys
import types
from pathlib import Path

logger = logging.getLogger(__name__)


def _patch_lancedb_connect_redirect(target_dir: Path) -> None:
    """Redirect lancedb.connect() to a persona-scoped path.

    Some Cognee versions connect to a package-scoped DB under
    <venv>/site-packages/cognee/.cognee_system/databases by default.
    This patch rewrites these default paths to `target_dir` so each persona
    is isolated on disk.
    """
    try:
        import lancedb  # type: ignore
    except Exception:
        return
    try:
        # Idempotent: only patch once per process
        if getattr(lancedb.connect, "_saiverse_redirect", False):  # type: ignore[attr-defined]
            return
    except Exception:
        pass

    try:
        # Ensure directory exists
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Build a small matcher for known default roots
    def _default_roots() -> list[str]:
        roots: list[Path] = []
        try:
            import importlib
            mod = importlib.import_module("cognee")
            pkg = Path(getattr(mod, "__file__", "")).resolve().parent
            roots.append(pkg / ".cognee_system" / "databases")
        except Exception:
            pass
        roots.append(Path.home() / ".cognee" / "databases")
        roots.append(Path.home() / ".saiverse" / "cognee" / "databases")
        return [str(p) for p in roots]

    defaults = _default_roots()
    orig_connect = getattr(lancedb, "connect", None)
    orig_connect_async = getattr(lancedb, "connect_async", None)

    persona_root_str = str(target_dir.parent)
    def _should_redirect(path_str: str) -> bool:
        p = (path_str or "")
        pl = p.lower()
        # If already pointing at this persona's databases dir, do not redirect
        if persona_root_str and p.startswith(persona_root_str):
            return False
        # Redirect anything clearly under common cognee default roots
        for root in defaults:
            if p.startswith(root):
                return True
        # Heuristics: any path that mentions 'cognee' or endswith cognee.lancedb
        if "cognee" in pl or p.endswith("cognee.lancedb"):
            return True
        return False

    def wrapped_connect(path, *args, **kwargs):  # type: ignore[override]
        try:
            p = str(path) if path is not None else ""
        except Exception:
            p = ""
        if _should_redirect(p):
            try:
                return orig_connect(str(target_dir))
            except Exception:
                return orig_connect(path, *args, **kwargs)
        return orig_connect(path, *args, **kwargs)

    try:
        if callable(orig_connect):
            wrapped_connect._saiverse_redirect = True  # type: ignore[attr-defined]
            lancedb.connect = wrapped_connect  # type: ignore[assignment]
    except Exception:
        pass

    async def wrapped_connect_async(path, *args, **kwargs):  # type: ignore[override]
        try:
            p = str(path) if path is not None else ""
        except Exception:
            p = ""
        if _should_redirect(p):
            try:
                return await orig_connect_async(str(target_dir), *args, **kwargs)
            except Exception:
                return await orig_connect_async(path, *args, **kwargs)
        return await orig_connect_async(path, *args, **kwargs)

    try:
        if callable(orig_connect_async):
            lancedb.connect_async = wrapped_connect_async  # type: ignore[assignment]
    except Exception:
        pass


def _patch_kuzu_adapter_redirect(target_path: Path) -> None:
    """Redirect KuzuAdapter to a persona-scoped graph database path.

    Cognee's graph engine sometimes defaults to a package-level directory such as
    <venv>/site-packages/cognee/.cognee_system/databases.  This patch ensures that
    the `db_path` passed to KuzuAdapter is rewritten to ``target_path`` so that
    each persona uses its own isolated graph database.
    """
    try:
        import importlib
        kuzu_adapter = importlib.import_module(
            "cognee.infrastructure.databases.graph.kuzu.adapter"
        )
    except Exception:
        return
    try:
        if getattr(kuzu_adapter.KuzuAdapter.__init__, "_saiverse_redirect", False):
            return
    except Exception:
        pass

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    defaults: list[Path] = []
    try:
        mod = importlib.import_module("cognee")
        pkg = Path(getattr(mod, "__file__", "")).resolve().parent
        defaults.append(pkg / ".cognee_system" / "databases")
    except Exception:
        pass
    defaults.append(Path.home() / ".cognee" / "databases")
    defaults.append(Path.home() / ".saiverse" / "cognee" / "databases")
    persona_root_str = str(target_path.parent)
    default_strs = [str(p) for p in defaults]

    def _should_redirect(path_str: str) -> bool:
        p = path_str or ""
        if persona_root_str and p.startswith(persona_root_str):
            return False
        for root in default_strs:
            if p.startswith(root):
                return True
        if "cognee" in p.lower() or p.endswith("cognee_graph_kuzu"):
            return True
        return False

    orig_init = kuzu_adapter.KuzuAdapter.__init__

    def wrapped_init(self, db_path: str, *args, **kwargs):
        try:
            path_str = str(db_path or "")
        except Exception:
            path_str = ""
        if _should_redirect(path_str):
            db_path = str(target_path)
        return orig_init(self, db_path, *args, **kwargs)

    wrapped_init._saiverse_redirect = True  # type: ignore[attr-defined]
    kuzu_adapter.KuzuAdapter.__init__ = wrapped_init  # type: ignore[assignment]


def _patch_relational_engine_redirect(target_dir: Path) -> None:
    """Redirect create_relational_engine to a persona-scoped SQLite path."""
    try:
        import importlib
        rel_mod = importlib.import_module(
            "cognee.infrastructure.databases.relational.create_relational_engine"
        )
    except Exception:
        return
    try:
        if getattr(rel_mod.create_relational_engine, "_saiverse_redirect", False):
            return
    except Exception:
        pass

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    defaults: list[Path] = []
    try:
        mod = importlib.import_module("cognee")
        pkg = Path(getattr(mod, "__file__", "")).resolve().parent
        defaults.append(pkg / ".cognee_system" / "databases")
    except Exception:
        pass
    defaults.append(Path.home() / ".cognee" / "databases")
    defaults.append(Path.home() / ".saiverse" / "cognee" / "databases")
    persona_root_str = str(target_dir)
    default_strs = [str(p) for p in defaults]

    def _should_redirect(path_str: str) -> bool:
        p = path_str or ""
        if persona_root_str and p.startswith(persona_root_str):
            return False
        for root in default_strs:
            if p.startswith(root):
                return True
        if "cognee" in p.lower() or p.endswith("cognee_db") or p.endswith(".sqlite"):
            return True
        return False

    orig_create = rel_mod.create_relational_engine

    def wrapped(db_path: str, db_name: str, db_host: str, db_port: str, db_username: str, db_password: str, db_provider: str):
        if db_provider == "sqlite" and _should_redirect(str(db_path)):
            db_path = str(target_dir)
        return orig_create(db_path, db_name, db_host, db_port, db_username, db_password, db_provider)

    wrapped._saiverse_redirect = True  # type: ignore[attr-defined]
    rel_mod.create_relational_engine = wrapped  # type: ignore[assignment]


class CogneeMemory:
    def __init__(self, persona_id: str) -> None:
        _tune_third_party_logging()
        _install_google_generativeai_shim()
        _patch_gemini_embedding_batch_limit()
        _patch_gemini_structured_output_json()
        if (os.getenv("SAIVERSE_DISABLE_LANCEDB_FILTER") or "").strip().lower() not in ("1", "true", "yes"):
            _patch_lancedb_create_data_points_filter()
        _patch_text_loader_passthrough_for_txt()
        _patch_skip_edge_index()
        _patch_profiling_hooks()
        self.persona_id = persona_id
        # Prefer a readable dataset alias; can be switched to UUID later if needed
        self.dataset_name = persona_id
        # Ensure persona-scoped system/data directories are set early, even if no provider env
        env_boot: Optional[dict] = None
        try:
            persona_root = Path.home() / ".saiverse" / "personas" / str(self.persona_id) / "cognee_system"
            persona_root.mkdir(parents=True, exist_ok=True)
            (persona_root / "databases").mkdir(parents=True, exist_ok=True)
            os.environ["SYSTEM_ROOT_DIRECTORY"] = str(persona_root)
            os.environ["DATA_ROOT_DIRECTORY"] = str(persona_root / "data")
            _patch_lancedb_connect_redirect(persona_root / "databases" / "cognee.lancedb")
            _patch_kuzu_adapter_redirect(
                persona_root / "databases" / "cognee_graph_kuzu"
            )
            _patch_relational_engine_redirect(persona_root / "databases")
            env_boot = {
                "SYSTEM_ROOT_DIRECTORY": str(persona_root),
                "DATA_ROOT_DIRECTORY": str(persona_root / "data"),
            }
        except Exception:
            pass
        # Defer module init until we have provider env; attempt once using current env
        self._cognee_ok = False
        self._SearchType = None
        self._cognee = None
        self._last_debug: Dict[str, object] = {}
        # Dedicated asyncio loop running in a background thread to avoid
        # cross-event-loop errors from third-party libs (e.g., kuzu adapter locks)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        try:
            env = self._provider_env()
            if env is None:
                env = env_boot
            self._ensure_cognee(env)
        except Exception as e:
            logger.warning("Cognee init deferred: %s", e)

    # ---- Public API (drop-in) ----
    def get_debug(self) -> Dict[str, object]:
        return dict(self._last_debug)
    def _ensure_cognee(self, env_updates: Optional[dict]):
        if self._cognee_ok and self._cognee is not None and self._SearchType is not None:
            return
        try:
            import importlib
            if env_updates:
                with _EnvPatch(env_updates):
                    # Clear cached configs so they pick up patched env
                    try:
                        cfg_mod = importlib.import_module("cognee.infrastructure.llm.config")
                        if hasattr(cfg_mod, "get_llm_config") and hasattr(cfg_mod.get_llm_config, "cache_clear"):
                            cfg_mod.get_llm_config.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        e_mod = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
                        if hasattr(e_mod, "get_embedding_config") and hasattr(e_mod.get_embedding_config, "cache_clear"):
                            e_mod.get_embedding_config.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    # Also clear base and vector db configs which determine storage paths
                    try:
                        b_mod = importlib.import_module("cognee.base_config")
                        if hasattr(b_mod, "get_base_config") and hasattr(b_mod.get_base_config, "cache_clear"):
                            b_mod.get_base_config.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        v_mod = importlib.import_module("cognee.infrastructure.databases.vector.config")
                        if hasattr(v_mod, "get_vectordb_config") and hasattr(v_mod.get_vectordb_config, "cache_clear"):
                            v_mod.get_vectordb_config.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        r_mod = importlib.import_module("cognee.infrastructure.databases.relational.config")
                        if hasattr(r_mod, "get_relational_config") and hasattr(r_mod.get_relational_config, "cache_clear"):
                            r_mod.get_relational_config.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        cve_mod = importlib.import_module("cognee.infrastructure.databases.vector.create_vector_engine")
                        if hasattr(cve_mod, "create_vector_engine") and hasattr(cve_mod.create_vector_engine, "cache_clear"):
                            cve_mod.create_vector_engine.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        cre_mod = importlib.import_module("cognee.infrastructure.databases.relational.create_relational_engine")
                        if hasattr(cre_mod, "create_relational_engine") and hasattr(cre_mod.create_relational_engine, "cache_clear"):
                            cre_mod.create_relational_engine.cache_clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    cognee = importlib.import_module("cognee")  # type: ignore
                    api_search = importlib.import_module("cognee.api.v1.search")  # type: ignore
                    SearchType = getattr(api_search, "SearchType")
            else:
                cognee = importlib.import_module("cognee")  # type: ignore
                api_search = importlib.import_module("cognee.api.v1.search")  # type: ignore
                SearchType = getattr(api_search, "SearchType")
            self._cognee = cognee
            self._SearchType = SearchType
            self._cognee_ok = True
            # Install prompt directory override if provided via env
            if env_updates and env_updates.get("SAIVERSE_COGNEE_PROMPTS_DIR"):
                _install_cognee_prompt_overrides(env_updates.get("SAIVERSE_COGNEE_PROMPTS_DIR"))
        except Exception as e:
            logger.warning("Cognee import failed: %s", e)
            self._cognee_ok = False
            self._cognee = None
            self._SearchType = None
    def _provider_env(self) -> Optional[dict]:
        """Compute environment overrides for Cognee based on selected provider.
        - Gemini: if LLM_PROVIDER=gemini and GEMINI_* key exists -> set LLM_PROVIDER/LLM_API_KEY and clear OPENAI_API_KEY
        - OpenAI: if OPENAI_API_KEY exists -> set LLM_PROVIDER=openai and LLM_API_KEY
        - Else: return None (disable LLM-dependent ops)
        """
        provider = (os.getenv("LLM_PROVIDER") or os.getenv("SAIVERSE_KW_LLM_PROVIDER") or "").strip().lower()
        gem_free = (os.getenv("GEMINI_FREE_API_KEY") or "").strip()
        gem_paid = (os.getenv("GEMINI_API_KEY") or "").strip()
        openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        # Persona-scoped Cognee system dir under ~/.saiverse/personas/<persona_id>/cognee_system
        persona_root = Path.home() / ".saiverse" / "personas" / str(self.persona_id) / "cognee_system"
        sys_root = str(persona_root)
        data_root = str(persona_root / "data")
        try:
            persona_root.mkdir(parents=True, exist_ok=True)
            (persona_root / "databases").mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        if provider == "gemini" and (gem_free or gem_paid):
            # Select which Gemini key to use based on preference
            key_pref = (os.getenv("SAIVERSE_GEMINI_KEY_PREF") or os.getenv("SAIVERSE_GEMINI_KEY_PREFERENCE") or "auto").strip().lower()
            key, key_kind = _select_gemini_key(gem_free, gem_paid, key_pref)
            if not key:
                logger.warning("Gemini selected but no API key matches preference '%s'", key_pref)
                return None
            model = (os.getenv("SAIVERSE_COGNEE_GEMINI_MODEL") or "gemini/gemini-2.0-flash").strip()
            # Ensure gemini/ prefix for LiteLLM routing
            if not model.startswith("gemini/"):
                model = f"gemini/{model}"
            emb_model = (os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_MODEL") or "gemini/text-embedding-004").strip()
            if not emb_model.startswith("gemini/"):
                emb_model = f"gemini/{emb_model}"
            return {
                "LLM_PROVIDER": "gemini",
                "LLM_API_KEY": key,
                "GEMINI_API_KEY": key,  # LiteLLM expects this
                "LLM_MODEL": model,
                "MODEL": model,
                # Persona-scoped storage roots for Cognee
                "SYSTEM_ROOT_DIRECTORY": sys_root,
                "DATA_ROOT_DIRECTORY": data_root,
                # Prompts override
                "GRAPH_PROMPT_PATH": os.path.abspath(os.path.join(os.getcwd(), "integrations/cognee_prompts/generate_graph_prompt_ja.txt")),
                "SAIVERSE_COGNEE_PROMPTS_DIR": os.path.abspath(os.path.join(os.getcwd(), "integrations/cognee_prompts")),
                # Embedding config
                "EMBEDDING_PROVIDER": "gemini",
                "EMBEDDING_MODEL": emb_model,
                "EMBEDDING_DIMENSIONS": os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_DIM", "768"),
                "EMBEDDING_API_KEY": key,
                "HUGGINGFACE_TOKENIZER": os.getenv("HUGGINGFACE_TOKENIZER", "none"),
                # Prevent LiteLLM/OpenAI from taking over
                "OPENAI_API_KEY": None,
                # Surface the decision for debugging if needed
                "SAIVERSE_GEMINI_KEY_KIND": key_kind,
            }
        if openai_key:
            model = (os.getenv("SAIVERSE_COGNEE_OPENAI_MODEL") or "openai/gpt-4o-mini").strip()
            if not model.startswith("openai/"):
                model = f"openai/{model}"
            emb_model = (os.getenv("SAIVERSE_COGNEE_OPENAI_EMBED_MODEL") or "openai/text-embedding-3-large").strip()
            if not emb_model.startswith("openai/"):
                emb_model = f"openai/{emb_model}"
            return {
                "LLM_PROVIDER": "openai",
                "LLM_API_KEY": openai_key,
                "OPENAI_API_KEY": openai_key,
                "LLM_MODEL": model,
                "MODEL": model,
                # Persona-scoped storage roots for Cognee
                "SYSTEM_ROOT_DIRECTORY": sys_root,
                "DATA_ROOT_DIRECTORY": data_root,
                # Embedding config
                "EMBEDDING_PROVIDER": "openai",
                "EMBEDDING_MODEL": emb_model,
                "EMBEDDING_DIMENSIONS": os.getenv("SAIVERSE_COGNEE_OPENAI_EMBED_DIM", "3072"),
                "EMBEDDING_API_KEY": openai_key,
                "HUGGINGFACE_TOKENIZER": os.getenv("HUGGINGFACE_TOKENIZER", "none"),
            }
        hf_model = (os.getenv("SAIVERSE_COGNEE_HF_EMBED_MODEL") or "").strip()
        if hf_model:
            return {
                # Persona-scoped storage roots for Cognee
                "SYSTEM_ROOT_DIRECTORY": sys_root,
                "DATA_ROOT_DIRECTORY": data_root,
                # Embedding config for local Hugging Face model
                "EMBEDDING_PROVIDER": "huggingface",
                "EMBEDDING_MODEL": hf_model,
                "EMBEDDING_DIMENSIONS": os.getenv("SAIVERSE_COGNEE_HF_EMBED_DIM", "768"),
                "HUGGINGFACE_TOKENIZER": "none",
                # Ensure no remote LLM provider is selected
                "LLM_PROVIDER": None,
                "LLM_API_KEY": None,
                "OPENAI_API_KEY": None,
                "GEMINI_API_KEY": None,
            }
        return None

    def remember(self, text: str, conv_id: str = "default", speaker: str = "user", meta: Optional[Dict] = None):
        """Ingest a turn of text into the persona's dataset and trigger background processing.

        Returns a lightweight echo dict for compatibility (MemoryEntry-like not required by caller).
        """
        env = self._provider_env()
        self._ensure_cognee(env)
        if not self._cognee_ok:
            logger.info("remember() skipped: Cognee not available")
            return {"id": None, "raw_text": text, "speaker": speaker}

        # 1) Ensure dataset and raw data are ingested synchronously (block until add finishes)
        def _ingest_blocking():
            async def _add():
                await self._cognee.add(
                    data=text,
                    dataset_name=self.dataset_name,
                    node_set=[f"conv:{conv_id}", f"speaker:{speaker}"] if conv_id or speaker else None,
                )
            try:
                if env:
                    logger.info("[cognee] add with provider=%s model=%s", env.get("LLM_PROVIDER"), env.get("LLM_MODEL"))
                    self._last_debug.update({
                        "provider": env.get("LLM_PROVIDER"),
                        "model": env.get("LLM_MODEL"),
                        "embedding_model": env.get("EMBEDDING_MODEL"),
                        "phase": "remember",
                    })
                self._run_on_loop(_add(), env_updates=env, wait=True)
            except Exception as e:
                logger.warning("Cognee add failed: %s", e)
        _ingest_blocking()

        # 2) Kick off background cognify using the selected provider (Gemini or OpenAI)
        def _cognify_bg():
            async def _cg():
                env_updates = self._provider_env()
                if not env_updates:
                    logger.info("Skipping cognee.cognify: no suitable provider key (Gemini/OpenAI) configured")
                    return
                provider = (env_updates.get("LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "").lower()
                # For Gemini, avoid background to keep env patch for the entire run
                run_bg = not (provider == "gemini")
                with _EnvPatch(env_updates):
                    self._ensure_cognee(env_updates)
                    try:
                        logger.info("[cognee] cognify provider=%s model=%s background=%s", env_updates.get("LLM_PROVIDER"), env_updates.get("LLM_MODEL"), run_bg)
                        self._last_debug.update({
                            "provider": env_updates.get("LLM_PROVIDER"),
                            "model": env_updates.get("LLM_MODEL"),
                            "embedding_model": env_updates.get("EMBEDDING_MODEL"),
                            "phase": "cognify",
                        })
                        await self._cognee.cognify(
                            datasets=self.dataset_name,
                            run_in_background=run_bg,
                            incremental_loading=True,
                        )
                    except Exception as e:
                        logger.warning("Cognee cognify(background=%s) failed: %s", run_bg, e)
            # schedule on persistent loop (fire-and-forget)
            self._run_on_loop(_cg(), env_updates=None, wait=False)
        # Allow disabling per-remember cognify during bulk ingest to reduce duplicate work/logs
        autocg = (os.getenv("SAIVERSE_COGNEE_AUTOCG") or "1").strip().lower()
        if autocg not in ("0", "false", "no"):
            _cognify_bg()
        return {"id": None, "raw_text": text, "speaker": speaker}

    def recall(self, text: str, k: int = 5) -> Dict:
        """Search previously cognified knowledge. Prefer CHUNKS (no LLM) for offline use.

        Returns a dict with keys: texts, topics, entries — matching existing caller expectations.
        """
        env = self._provider_env()
        self._ensure_cognee(env)
        if not self._cognee_ok:
            logger.info("recall() skipped: Cognee not available")
            return {"texts": [], "topics": [], "entries": []}

        async def _search_chunks() -> List:
            try:
                return await self._cognee.search(
                    query_text=text,
                    query_type=self._SearchType.CHUNKS,
                    datasets=self.dataset_name,
                    top_k=int(k),
                )
            except Exception as e:
                logger.warning("Cognee search(CHUNKS) failed: %s", e)
                return []

        # Run async search synchronously in a helper thread
        if env:
            logger.info("[cognee] search(CHUNKS) provider=%s model=%s", env.get("LLM_PROVIDER"), env.get("LLM_MODEL"))
        results = self._run_on_loop(_search_chunks(), env_updates=env, wait=True) or []
        # Normalize to list of text snippets
        snippets: List[str] = []
        for r in results:
            try:
                # r may be str or dict-like
                if isinstance(r, str):
                    s = r.strip()
                elif isinstance(r, dict):
                    # try common fields
                    s = (r.get("text") or r.get("chunk") or r.get("content") or "").strip()
                    if not s and "metadata" in r:
                        s = str(r["metadata"])[:300]
                else:
                    s = str(r)
                if s:
                    snippets.append(s)
            except Exception:
                continue

        if env:
            self._last_debug.update({
                "provider": env.get("LLM_PROVIDER"),
                "model": env.get("LLM_MODEL"),
                "embedding_model": env.get("EMBEDDING_MODEL"),
                "phase": "recall",
                "recall_k": int(k),
                "recall_found": len(snippets),
                # Keep a short preview of top snippets for UI debug
                "snippets": [s[:200] for s in snippets[:3]],
            })
        return {"texts": snippets, "topics": [], "entries": []}

    def finalize(self, wait: bool = True) -> None:
        """Ensure Cognee has processed the current persona dataset (cognify).

        - If wait=True, blocks until cognify completes.
        - If no provider/API key is configured, returns silently.
        """
        env = self._provider_env()
        self._ensure_cognee(env)
        if not self._cognee_ok or self._cognee is None:
            return
        async def _cg():
            # Prune stale Data rows that reference missing files for this dataset
            try:
                await _prune_missing_files_for_dataset(self.dataset_name)
            except Exception as e:
                logger.warning("Prune missing files failed: %s", e)
            await self._cognee.cognify(
                datasets=self.dataset_name,
                run_in_background=False,
                incremental_loading=True,
            )
        if wait:
            self._run_on_loop(_cg(), env_updates=env, wait=True)
        else:
            self._run_on_loop(_cg(), env_updates=env, wait=False)

    # ---- Internal: dedicated loop runner ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop and self._loop.is_running():
            return self._loop
        # start loop in background thread
        def loop_runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                except Exception:
                    pass
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
        self._loop_ready.clear()
        t = threading.Thread(target=loop_runner, name=f"cognee-loop-{self.persona_id}", daemon=True)
        t.start()
        self._loop_thread = t
        self._loop_ready.wait(timeout=5.0)
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("failed to start background event loop")
        return self._loop

    def _run_on_loop(self, coro, env_updates: Optional[dict], wait: bool):
        loop = self._ensure_loop()
        async def wrapper():
            if env_updates:
                with _EnvPatch(env_updates):
                    return await coro
            return await coro
        fut = asyncio.run_coroutine_threadsafe(wrapper(), loop)
        if wait:
            return fut.result()
        return None


class _EnvPatch:
    """Context manager to patch environment variables temporarily (thread-safe usage)."""
    def __init__(self, updates: dict[str, str | None]):
        self.updates = updates
        self.prev: dict[str, Optional[str]] = {}
    def __enter__(self):
        for k, v in self.updates.items():
            self.prev[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self
    def __exit__(self, exc_type, exc, tb):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_in_thread(coro, env_updates: Optional[dict] = None):
    """Run an async coroutine to completion in a separate thread, blocking caller until done.
    If env_updates is provided, apply them only within the thread during execution.
    """
    result: Dict[str, object] = {}
    err: Dict[str, BaseException] = {}

    def target():
        try:
            if env_updates:
                with _EnvPatch(env_updates):
                    result["value"] = asyncio.run(coro)
            else:
                result["value"] = asyncio.run(coro)
        except BaseException as e:
            err["e"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "e" in err:
        raise err["e"]
    return result.get("value")


def _safe_async_run(coro):
    try:
        asyncio.run(coro)
    except Exception as e:
        logging.getLogger(__name__).warning("async run failed: %s", e)


def _tune_third_party_logging():
    """Reduce noisy logs from third-party libs used by Cognee/LiteLLM.
    - Set litellm/httpx/httpcore and related proxy modules to ERROR (configurable)
    - Disable propagation to root to avoid duplicate stack traces
    - Hint Langfuse SDK to stay silent if present
    """
    try:
        # Suppress Langfuse SDK init noise if library is present
        os.environ.setdefault("LANGFUSE_SDK_DISABLED", "1")
    except Exception:
        pass

    level_name = (os.getenv("SAIVERSE_THIRDPARTY_LOG_LEVEL") or "ERROR").upper()
    level = getattr(logging, level_name, logging.ERROR)
    noisy_loggers = (
        # LiteLLM core + proxy helpers
        "litellm",
        "litellm.litellm_core_utils",
        "litellm.litellm_core_utils.litellm_logging",
        "litellm.proxy",
        "litellm.proxy.proxy_server",
        "litellm.proxy.spend_tracking",
        # HTTP stack
        "httpx",
        "httpcore",
        "urllib3",
        # Cognee base loggers (suppress INFO banner/noise)
        "cognee",
        "cognee.shared.logging_utils",
    )
    for name in noisy_loggers:
        try:
            lg = logging.getLogger(name)
            lg.setLevel(level)
            lg.propagate = False
        except Exception:
            continue
    # Attempt to disable LiteLLM standard logging and cold storage hooks which import proxy deps
    try:
        import importlib
        ll = importlib.import_module("litellm.litellm_core_utils.litellm_logging")
        # Patch noisy functions to no-op and avoid importing proxy server
        def _noop(*args, **kwargs):
            return None
        try:
            ll.get_standard_logging_object_payload = _noop  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            slps = getattr(ll, "StandardLoggingPayloadSetup", None)
            if slps is not None:
                setattr(slps, "get_standard_logging_metadata", staticmethod(lambda *a, **k: {}))
        except Exception:
            pass
    except Exception:
        pass
    try:
        import importlib
        ch = importlib.import_module("litellm.proxy.spend_tracking.cold_storage_handler")
        if hasattr(ch, "ColdStorageHandler"):
            cls = ch.ColdStorageHandler
            setattr(cls, "_get_configured_cold_storage_custom_logger", staticmethod(lambda: None))
            setattr(cls, "_get_configured_cold_storage", staticmethod(lambda: None))
    except Exception:
        pass


def _install_google_generativeai_shim():
    """Install a runtime shim for the deprecated `google.generativeai` API using google.genai.
    Some Cognee components import `google.generativeai`. This shim bridges to the new
    `google.genai` client so we don't need the old package installed.
    """
    try:
        if 'google.generativeai' in sys.modules:
            return
        from google import genai as _genai  # type: ignore
        import importlib.machinery as _machinery
        import importlib.abc as _abc
    except Exception:
        return

    _state = {"client": None}

    def _get_client(api_key: Optional[str] = None):
        if _state["client"] is not None and api_key is None:
            return _state["client"]
        # Respect key preference when both FREE/PAID are present
        if api_key:
            key = api_key.strip()
        else:
            free = (os.getenv("GEMINI_FREE_API_KEY") or "").strip()
            paid = (os.getenv("GEMINI_API_KEY") or "").strip()
            pref = (os.getenv("SAIVERSE_GEMINI_KEY_PREF") or os.getenv("SAIVERSE_GEMINI_KEY_PREFERENCE") or "auto").strip().lower()
            key, _ = _select_gemini_key(free, paid, pref)
        try:
            _state["client"] = _genai.Client(api_key=key if key else None)
        except Exception:
            _state["client"] = _genai.Client()
        return _state["client"]

    def configure(api_key: Optional[str] = None, **kwargs):  # compat
        _get_client(api_key)

    def embed_content(model: Optional[str] = None, content: Optional[str] = None, **kwargs):  # compat
        client = _get_client()
        m = (model or "text-embedding-004").strip()
        if m.startswith("models/"):
            m = m.split("/", 1)[1]
        # google.genai expects a content structure; accept plain text for simplicity
        try:
            resp = client.models.embed_content(model=m, contents=[{"role": "user", "parts": [{"text": content or ""}]}])
        except TypeError:
            # fallback signature if contents -> content
            resp = client.models.embed_content(model=m, content=content or "")
        try:
            vec = list(resp.embedding.values)  # type: ignore[attr-defined]
        except Exception:
            vec = []
        return vec

    # Minimal loader to satisfy import system expectations
    class _ShimLoader(_abc.Loader):
        def create_module(self, spec):  # noqa: D401
            return None
        def exec_module(self, module):  # noqa: D401
            return

    shim = types.ModuleType("google.generativeai")
    shim.configure = configure
    shim.embed_content = embed_content
    # Provide ModuleSpec so importlib doesn't complain
    try:
        shim.__spec__ = _machinery.ModuleSpec(name="google.generativeai", loader=_ShimLoader())
        shim.__package__ = "google"
    except Exception:
        pass
    sys.modules['google.generativeai'] = shim


def _install_cognee_prompt_overrides(base_dir: Optional[str]):
    """If a base_dir is provided, patch Cognee prompt readers to load templates from there.
    This affects summarize_content.txt / classify_content.txt. Knowledge graph prompt is
    controlled via GRAPH_PROMPT_PATH env (absolute path).
    """
    if not base_dir:
        return
    try:
        import importlib
        prompts_pkg = importlib.import_module("cognee.infrastructure.llm.prompts.read_query_prompt")
        # Wrap the read_query_prompt(prompt_file_name, base_directory=None)
        orig = prompts_pkg.read_query_prompt
        def _wrapped(prompt_file_name: str, base_directory: str = None):
            return orig(prompt_file_name=prompt_file_name, base_directory=base_dir)
        prompts_pkg.read_query_prompt = _wrapped  # type: ignore[assignment]
    except Exception:
        pass


def _select_gemini_key(free: str, paid: str, pref: str) -> Tuple[str, str]:
    """Select which Gemini key to use.
    - pref: 'free' | 'paid' | 'auto'
      - free: use free if present; else ''
      - paid: use paid if present; else ''
      - auto (default): prefer free if present, else paid
    Returns (key, kind) where kind is 'free' or 'paid'. Empty key if not found.
    """
    pref = (pref or "auto").lower()
    if pref == "free":
        return (free, "free") if free else ("", "free")
    if pref == "paid":
        return (paid, "paid") if paid else ("", "paid")
    # auto
    if free:
        return (free, "free")
    if paid:
        return (paid, "paid")
    return ("", "auto")


def _patch_gemini_embedding_batch_limit(max_batch: int = 100) -> None:
    """Monkeypatch LiteLLM's Gemini batch embeddings to respect service limit (<=100).

    Splits larger requests into chunks and concatenates the resulting EmbeddingResponse.data.
    """
    try:
        import importlib
        from litellm import EmbeddingResponse  # type: ignore
        be_mod = importlib.import_module(
            "litellm.llms.vertex_ai.gemini_embeddings.batch_embed_content_handler"
        )
        cls = getattr(be_mod, "GoogleBatchEmbeddings", None)
        if cls is None:
            return

        # Allow overriding batch size via env (e.g., set to 1 to fully serialize)
        try:
            max_batch = int(os.getenv("SAIVERSE_EMBED_MAX_BATCH") or max_batch)
        except Exception:
            pass
        orig_async = getattr(cls, "async_batch_embeddings", None)
        orig_sync = getattr(cls, "batch_embeddings", None)

        async def patched_async(self, model, api_base, url, data, model_response, input, timeout, headers={}, client=None):  # type: ignore[no-redef]
            try:
                reqs = list(data.get("requests", []))
                if len(reqs) <= max_batch:
                    resp = await orig_async(self, model, api_base, url, data, model_response, input, timeout, headers, client)
                    try:
                        _normalize_embedding_response(resp, expected=len(reqs))
                    except Exception:
                        pass
                    return resp
                combined = None
                import asyncio as _asyncio
                try:
                    delay_ms = int(os.getenv("SAIVERSE_EMBED_BATCH_SLEEP_MS", "0"))
                except Exception:
                    delay_ms = 0
                start = 0
                while start < len(reqs):
                    chunk = {"requests": reqs[start : start + max_batch]}
                    resp = await orig_async(self, model, api_base, url, chunk, model_response, input, timeout, headers, client)
                    try:
                        _normalize_embedding_response(resp, expected=len(chunk["requests"]))
                    except Exception:
                        pass
                    if combined is None:
                        combined = resp
                    else:
                        try:
                            combined.data.extend(resp.data)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    if delay_ms > 0:
                        await _asyncio.sleep(delay_ms / 1000.0)
                    start += max_batch
                return combined
            except Exception:
                # Fallback to original
                return await orig_async(self, model, api_base, url, data, model_response, input, timeout, headers, client)

        def patched_sync(self, model, input, print_verbose, model_response, custom_llm_provider, optional_params, logging_obj, api_key=None, api_base=None, encoding=None, vertex_project=None, vertex_location=None, vertex_credentials=None, aembedding=False, timeout=300, client=None):  # type: ignore[no-redef]
            try:
                # Helper to run async in a fresh thread loop and return result
                def _run_async(coro):
                    import threading, asyncio as _asyncio
                    res = {}
                    err = {}
                    def target():
                        try:
                            res['v'] = _asyncio.run(coro)
                        except BaseException as e:
                            err['e'] = e
                    t = threading.Thread(target=target, daemon=True)
                    t.start(); t.join()
                    if 'e' in err:
                        raise err['e']
                    return res.get('v')
                # We need to transform input to request to know length; call the original transformation
                # by delegating to original and intercepting the HTTP call is complex; instead rely on async patch
                # for most paths; if sync path used and input is a list, chunk by size and call orig per chunk.
                if not isinstance(input, (list, tuple)) or len(input) <= max_batch:
                    # If aembedding=True, delegate to async and wait here to avoid un-awaited coroutine warnings
                    if aembedding:
                        # Transform the request like orig_sync would, by calling async path directly once
                        # Construct minimal request body via the transformation function
                        try:
                            tr_mod = importlib.import_module('litellm.llms.vertex_ai.gemini_embeddings.batch_embed_content_transformation')
                            transform = getattr(tr_mod, 'transform_openai_input_gemini_content')
                            data = transform(input=input, model=model, optional_params=(optional_params or {}))
                        except Exception:
                            data = {'requests': [{'model': f'models/{model}', 'content': {'parts': [{'text': (input if isinstance(input, str) else '\n'.join(map(str, input)))}]}}]}
                        resp = _run_async(patched_async(self, model, api_base, '', data, model_response, input, timeout, headers={}, client=client))
                    else:
                        resp = orig_sync(self, model, input, print_verbose, model_response, custom_llm_provider, optional_params, logging_obj, api_key, api_base, encoding, vertex_project, vertex_location, vertex_credentials, aembedding, timeout, client)
                    try:
                        _normalize_embedding_response(resp, expected=len(input))
                    except Exception:
                        pass
                    return resp
                combined = None
                start = 0
                while start < len(input):
                    sub_input = list(input)[start : start + max_batch]
                    if aembedding:
                        try:
                            tr_mod = importlib.import_module('litellm.llms.vertex_ai.gemini_embeddings.batch_embed_content_transformation')
                            transform = getattr(tr_mod, 'transform_openai_input_gemini_content')
                            data = transform(input=sub_input, model=model, optional_params=(optional_params or {}))
                        except Exception:
                            data = {'requests': [{'model': f'models/{model}', 'content': {'parts': [{'text': t}]}} for t in sub_input]}
                        resp = _run_async(patched_async(self, model, api_base, '', data, model_response, sub_input, timeout, headers={}, client=client))
                    else:
                        resp = orig_sync(self, model, sub_input, print_verbose, model_response, custom_llm_provider, optional_params, logging_obj, api_key, api_base, encoding, vertex_project, vertex_location, vertex_credentials, aembedding, timeout, client)
                    try:
                        _normalize_embedding_response(resp, expected=len(sub_input))
                    except Exception:
                        pass
                    if combined is None:
                        combined = resp
                    else:
                        try:
                            combined.data.extend(resp.data)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    start += max_batch
                return combined
            except Exception:
                return orig_sync(self, model, input, print_verbose, model_response, custom_llm_provider, optional_params, logging_obj, api_key, api_base, encoding, vertex_project, vertex_location, vertex_credentials, aembedding, timeout, client)

        if callable(orig_async):
            setattr(cls, "async_batch_embeddings", patched_async)
        if callable(orig_sync):
            setattr(cls, "batch_embeddings", patched_sync)
    except Exception:
        # Best-effort patching; ignore failures
        pass


def _patch_gemini_structured_output_json() -> None:
    """Force Gemini structured output to use JSON mime-type and sanitize code fences.

    This addresses empty-content failures where `response_format` alone yields blank
    content. We inject extra_body with response_mime_type and strip ```json fences
    before model_validate_json.
    """
    try:
        import importlib
        gm = importlib.import_module(
            "cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm.gemini.adapter"
        )
        GeminiAdapter = getattr(gm, "GeminiAdapter", None)
        if GeminiAdapter is None:
            return
        orig = getattr(GeminiAdapter, "acreate_structured_output", None)
        if orig is None:
            return

        import re

        async def wrapped(self, text_input, system_prompt, response_model):  # type: ignore[no-redef]
            # Prefer JSON output; fall back to original behavior on hard failure
            try:
                from litellm import acompletion
                if response_model is str:
                    response_schema = {"type": "string"}
                else:
                    response_schema = response_model
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text_input},
                ]
                resp = await acompletion(
                    model=f"{self.model}",
                    messages=messages,
                    api_key=self.api_key,
                    max_tokens=getattr(self, "max_tokens", 2048),
                    temperature=0,
                    response_format=response_schema,
                    timeout=100,
                    num_retries=getattr(self, "MAX_RETRIES", 5),
                    extra_body={"response_mime_type": "application/json"},
                )
                content = None
                try:
                    if resp.choices and resp.choices[0].message.content:
                        content = resp.choices[0].message.content
                except Exception:
                    content = None
                if not content:
                    raise ValueError("empty content")
                # Strip common code fences
                content_str = str(content).strip()
                fence = re.compile(r"^```(json)?\s*|\s*```$", re.IGNORECASE)
                content_str = fence.sub("", content_str)
                if response_model is str:
                    return content_str
                return response_model.model_validate_json(content_str)
            except Exception:
                return await orig(self, text_input, system_prompt, response_model)

        setattr(GeminiAdapter, "acreate_structured_output", wrapped)
    except Exception:
        # best-effort
        pass


def _patch_lancedb_create_data_points_filter() -> None:
    """Filter invalid/empty embeddable texts before embedding to prevent length mismatch.

    This ensures we only call embed_text on non-empty strings and construct a 1:1 mapping
    between vectors and original data points.
    """
    try:
        import importlib
        lm = importlib.import_module(
            "cognee.infrastructure.databases.vector.lancedb.LanceDBAdapter"
        )
        Adapter = getattr(lm, "LanceDBAdapter", None)
        if Adapter is None:
            return
        orig_cdp = getattr(Adapter, "create_data_points", None)
        if orig_cdp is None:
            return

        from cognee.infrastructure.engine.models import DataPoint as _DP  # type: ignore

        async def filtered_create(self, collection_name: str, data_points):  # type: ignore[no-redef]
            # Build list of (dp, text) for valid embeddables
            valid: list[tuple] = []
            for dp in list(data_points or []):
                text_val = None
                try:
                    # Support IndexSchema-style objects with .text
                    if hasattr(dp, "text") and isinstance(getattr(dp, "text"), str):
                        text_val = getattr(dp, "text")
                    else:
                        text_val = _DP.get_embeddable_data(dp)
                except Exception:
                    text_val = None
                if isinstance(text_val, str) and text_val.strip():
                    valid.append((dp, text_val.strip()))
            if not valid:
                # Defer to original behavior if nothing was extracted
                return await orig_cdp(self, collection_name, data_points)
            texts = [t for _, t in valid]
            vecs = await self.embed_data(texts)
            # Length should match; if not, align greedily on available
            k = min(len(valid), len(vecs))
            if k <= 0:
                # Fall back to original call to avoid dropping inserts entirely
                return await orig_cdp(self, collection_name, data_points)
            # Reconstruct minimal LanceDataPoint set via existing logic per item
            # Defer to original per-item by calling its inner pieces: we reuse orig by slicing
            try:
                # Monkey-call original with aligned subset by recreating DataPoints list
                aligned_dps = [valid[i][0] for i in range(k)]
                # Temporarily patch embed_data to return our aligned vecs for this call
                saved = self.embed_data
                async def _ret_vec(_):
                    return vecs[:k]
                self.embed_data = _ret_vec  # type: ignore
                try:
                    return await orig_cdp(self, collection_name, aligned_dps)
                finally:
                    self.embed_data = saved  # type: ignore
            except Exception:
                # As last resort, silently drop
                return await orig_cdp(self, collection_name, data_points)

        setattr(Adapter, "create_data_points", filtered_create)
    except Exception:
        pass

    # Also harden Cognee's LiteLLMEmbeddingEngine against count mismatches by falling back to per-item
    try:
        import importlib
        eng_mod = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine")
        LiteEng = getattr(eng_mod, "LiteLLMEmbeddingEngine", None)
        if LiteEng is not None:
            orig_embed = getattr(LiteEng, "embed_text", None)
            if orig_embed is not None:
                async def safe_embed(self, text_list):  # type: ignore[no-redef]
                    try:
                        vecs = await orig_embed(self, text_list)
                        # If count matches, normalize dims and return
                        if isinstance(vecs, list) and len(vecs) == len(text_list):
                            try:
                                dim = int(self.dimensions or 0)
                            except Exception:
                                dim = 0
                            if dim <= 0:
                                try:
                                    dim = int(os.getenv("EMBEDDING_DIMENSIONS") or os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_DIM") or 768)
                                except Exception:
                                    dim = 768
                            for i in range(len(vecs)):
                                v = vecs[i]
                                if not isinstance(v, list):
                                    continue
                                if len(v) > dim:
                                    del v[dim:]
                                elif len(v) < dim:
                                    v.extend([0.0] * (dim - len(v)))
                            return vecs
                        # Fallback to per-item embedding to preserve alignment
                        out = []
                        for t in text_list:
                            ti = t if (isinstance(t, str) and t.strip() != "") else (os.getenv("SAIVERSE_EMBED_EMPTY_PLACEHOLDER") or " ")
                            try:
                                subv = await orig_embed(self, [ti])
                                v = subv[0] if isinstance(subv, list) and subv else []
                            except Exception:
                                v = []
                            # pad/trunc to dim
                            try:
                                dim = int(self.dimensions or 0)
                            except Exception:
                                dim = 0
                            if dim <= 0:
                                try:
                                    dim = int(os.getenv("EMBEDDING_DIMENSIONS") or os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_DIM") or 768)
                                except Exception:
                                    dim = 768
                            if not isinstance(v, list):
                                v = [0.0] * dim
                            else:
                                if len(v) > dim:
                                    del v[dim:]
                                elif len(v) < dim:
                                    v.extend([0.0] * (dim - len(v)))
                            out.append(v)
                        return out
                    except Exception:
                        # On hard failure, serialize calls to ensure progress
                        out = []
                        for t in text_list:
                            ti = t if (isinstance(t, str) and t.strip() != "") else (os.getenv("SAIVERSE_EMBED_EMPTY_PLACEHOLDER") or " ")
                            try:
                                subv = await orig_embed(self, [ti])
                                v = subv[0] if isinstance(subv, list) and subv else []
                            except Exception:
                                v = []
                            if not isinstance(v, list):
                                v = []
                            out.append(v)
                        return out
                setattr(LiteEng, "embed_text", safe_embed)
    except Exception:
        pass

def _normalize_embedding_response(resp, expected: int) -> None:
    """Ensure EmbeddingResponse.data length matches expected. Truncate or pad with zero-vectors.
    This prevents downstream index errors when mapping inputs to vectors.
    """
    try:
        from litellm.types.utils import Embedding  # type: ignore
    except Exception:
        Embedding = None  # type: ignore
    data = getattr(resp, "data", None)
    if not isinstance(data, list):
        return
    cur = len(data)
    if cur == expected:
        # ensure each vector has consistent dimension
        try:
            dim = len(getattr(data[0], "embedding", []) or []) if cur > 0 else 0
        except Exception:
            dim = 0
        if dim <= 0:
            try:
                dim = int(os.getenv("EMBEDDING_DIMENSIONS") or os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_DIM") or 768)
            except Exception:
                dim = 768
        for i in range(cur):
            try:
                vec = getattr(data[i], "embedding", None)
                if not isinstance(vec, list):
                    continue
                if len(vec) > dim:
                    del vec[dim:]
                elif len(vec) < dim:
                    vec.extend([0.0] * (dim - len(vec)))
            except Exception:
                continue
        return
    if cur > expected:
        del data[expected:]
        return
    # pad with zeros
    dim = 0
    try:
        if cur > 0:
            dim = len(getattr(data[0], "embedding", []) or [])
    except Exception:
        dim = 0
    if dim <= 0:
        try:
            dim = int(os.getenv("EMBEDDING_DIMENSIONS") or os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_DIM") or 768)
        except Exception:
            dim = 768
    zeros = [0.0] * dim
    for i in range(expected - cur):
        if Embedding is not None:
            data.append(Embedding(embedding=zeros, index=cur + i, object="embedding"))  # type: ignore
        else:
            data.append(type("E", (), {"embedding": zeros, "index": cur + i, "object": "embedding"}))


def _patch_text_loader_passthrough_for_txt() -> None:
    """Avoid re-saving plain text files under a different hash name.

    When the original data is already a text file created by save_data_to_file
    (e.g., text_<md5>.txt), the loader step may compute a new name and cause
    mismatches. This patch short-circuits data_item_to_text_file for .txt paths
    to simply return the existing file path and a text_loader instance.
    """
    try:
        import importlib
        mod = importlib.import_module("cognee.tasks.ingestion.data_item_to_text_file")
        if not hasattr(mod, "data_item_to_text_file"):
            return
        orig = getattr(mod, "data_item_to_text_file")

        from pathlib import Path
        try:
            gdfp = importlib.import_module("cognee.infrastructure.files.utils.get_data_file_path")
            get_data_file_path = getattr(gdfp, "get_data_file_path")
        except Exception:
            get_data_file_path = None

        async def passthrough(path: str, preferred_loaders):  # type: ignore[no-redef]
            try:
                raw = path
                if isinstance(raw, str) and raw.startswith("file://") and callable(get_data_file_path):
                    fs_path = get_data_file_path(raw)
                else:
                    fs_path = raw
                p = Path(fs_path)
                if isinstance(fs_path, str) and p.suffix.lower() == ".txt":
                    # Return file:// absolute path and a text_loader instance for metadata
                    from cognee.infrastructure.loaders import get_loader_engine
                    eng = get_loader_engine()
                    loader = eng.get_loader(str(p))
                    return "file://" + str(p.resolve()), loader
            except Exception:
                pass
            return await orig(path, preferred_loaders)

        setattr(mod, "data_item_to_text_file", passthrough)
        # Also replace the reference imported inside ingest_data module
        try:
            ing = importlib.import_module("cognee.tasks.ingestion.ingest_data")
            if getattr(ing, "data_item_to_text_file", None) is not None:
                setattr(ing, "data_item_to_text_file", passthrough)
        except Exception:
            pass
    except Exception:
        pass


async def _prune_missing_files_for_dataset(dataset_name: str) -> None:
    """Remove Data rows whose raw_data_location file is missing on disk for the dataset.

    This prevents pipeline failures when storage was manually cleaned while DB retained rows.
    """
    try:
        import importlib
        import os
        from pathlib import Path
        b_mod = importlib.import_module("cognee.base_config")
        base = b_mod.get_base_config()
        # Ensure directories exist
        Path(base.system_root_directory).mkdir(parents=True, exist_ok=True)
        Path(os.path.join(base.system_root_directory, "databases")).mkdir(parents=True, exist_ok=True)

        users = importlib.import_module("cognee.modules.users.methods")
        get_default_user = getattr(users, "get_default_user")
        user = await get_default_user()

        data_methods = importlib.import_module("cognee.modules.data.methods")
        get_authorized_existing_datasets = getattr(data_methods, "get_authorized_existing_datasets")
        get_dataset_data = getattr(data_methods, "get_dataset_data")
        delete_data = getattr(data_methods, "delete_data")

        datasets = await get_authorized_existing_datasets(user=user, permission_type="write", datasets=[dataset_name])
        if isinstance(datasets, list) and datasets:
            dataset = datasets[0]
            rows = await get_dataset_data(dataset.id)
            if not rows:
                return
            # Helper to resolve filesystem path
            gdfp = importlib.import_module("cognee.infrastructure.files.utils.get_data_file_path")
            get_data_file_path = getattr(gdfp, "get_data_file_path")
            removed = 0
            for d in list(rows):
                try:
                    raw = getattr(d, "raw_data_location", "") or ""
                    fs = get_data_file_path(raw)
                    if not (fs and os.path.exists(fs)):
                        await delete_data(d)
                        removed += 1
                except Exception:
                    continue
            if removed:
                logger.info("Pruned %d stale data files for dataset '%s'", removed, dataset_name)
    except Exception:
        # best-effort
        return


def _patch_skip_edge_index() -> None:
    """Optionally skip costly edge re-indexing during ingest.

    If SAIVERSE_COGNEE_SKIP_EDGE_INDEX in (1,true,yes), monkey-patch
    cognee.tasks.storage.index_graph_edges.index_graph_edges to a no-op.
    """
    try:
        flag = (os.getenv("SAIVERSE_COGNEE_SKIP_EDGE_INDEX") or "").strip().lower()
        if flag not in ("1", "true", "yes"):
            return
        import importlib
        mod = importlib.import_module("cognee.tasks.storage.index_graph_edges")
        orig = getattr(mod, "index_graph_edges", None)
        if not callable(orig):
            return
        async def _noop(batch_size: int = 1024):  # type: ignore[no-redef]
            return None
        setattr(mod, "index_graph_edges", _noop)
        logger.info("SAIVERSE_COGNEE_SKIP_EDGE_INDEX=on: index_graph_edges is disabled")
    except Exception:
        return


def _patch_profiling_hooks() -> None:
    """Install lightweight profiling hooks controlled by env flags.

    - SAIVERSE_COGNEE_PROFILE_TASKS: time each pipeline task (start/end, seconds)
    - SAIVERSE_COGNEE_PROFILE_LLM: time litellm acompletion/aembedding calls
    """
    try:
        import importlib, time
        # Task-level profiling
        if (os.getenv("SAIVERSE_COGNEE_PROFILE_TASKS") or "").strip().lower() in ("1", "true", "yes"):
            mod = importlib.import_module("cognee.modules.pipelines.operations.run_tasks_base")
            orig = getattr(mod, "handle_task", None)
            if callable(orig) and not getattr(orig, "_saiverse_profiled", False):
                async def prof_handle_task(running_task, args, leftover_tasks, next_task_batch_size, user, context=None):  # type: ignore[no-redef]
                    name = getattr(running_task.executable, "__name__", str(running_task))
                    t0 = time.time()
                    print(f"[PROFILE][task][start] {name}")
                    try:
                        async for result in orig(running_task, args, leftover_tasks, next_task_batch_size, user, context):
                            yield result
                    finally:
                        dt = time.time() - t0
                        print(f"[PROFILE][task][end] {name}: {dt:.2f}s")
                setattr(prof_handle_task, "_saiverse_profiled", True)
                setattr(mod, "handle_task", prof_handle_task)
        # LLM-level profiling
        if (os.getenv("SAIVERSE_COGNEE_PROFILE_LLM") or "").strip().lower() in ("1", "true", "yes"):
            import litellm
            # acompletion
            acompl = getattr(litellm, "acompletion", None)
            if callable(acompl) and not getattr(acompl, "_saiverse_profiled", False):
                async def prof_acompletion(*args, **kwargs):  # type: ignore[no-redef]
                    import time as _t
                    model = kwargs.get("model") or (args[0] if args else "")
                    t0 = _t.time()
                    try:
                        resp = await acompl(*args, **kwargs)
                        return resp
                    finally:
                        dt = _t.time() - t0
                        print(f"[PROFILE][llm][acompletion] model={model} {dt:.2f}s")
                setattr(acompl, "_saiverse_profiled", True)
                litellm.acompletion = prof_acompletion  # type: ignore
            # aembedding
            aemb = getattr(litellm, "aembedding", None)
            if callable(aemb) and not getattr(aemb, "_saiverse_profiled", False):
                async def prof_aembedding(*args, **kwargs):  # type: ignore[no-redef]
                    import time as _t
                    model = kwargs.get("model") or (args[0] if args else "")
                    t0 = _t.time()
                    try:
                        resp = await aemb(*args, **kwargs)
                        return resp
                    finally:
                        dt = _t.time() - t0
                        print(f"[PROFILE][llm][aembedding] model={model} {dt:.2f}s")
                setattr(aemb, "_saiverse_profiled", True)
                litellm.aembedding = prof_aembedding  # type: ignore
    except Exception:
        return
