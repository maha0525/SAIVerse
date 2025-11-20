from __future__ import annotations

from typing import List, Dict

from sai_memory.config import Settings, load_settings, load_system_prompt
from sai_memory.logging_utils import debug
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.recall import Embedder, build_context, build_context_payload
from sai_memory.memory.storage import (
    init_db,
    get_or_create_thread,
    add_message,
    replace_message_embeddings,
    set_thread_overview,
    get_thread_overview,
)
from sai_memory.providers.openai_provider import OpenAIProvider
from sai_memory.providers.gemini_provider import GeminiProvider
from sai_memory.summary import update_overview_with_llm


class Agent:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.db = init_db(self.settings.db_path) if self.settings.memory_enabled else None
        self.embedder = Embedder(
            model=self.settings.embed_model,
            local_model_path=self.settings.embed_model_path,
            model_dim=self.settings.embed_model_dim,
        )
        self.system_prompt = load_system_prompt()

        if self.settings.provider == "google":
            if not self.settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY is required for provider=google")
            self.provider = GeminiProvider(self.settings.gemini_api_key, self.settings.model, self.settings.temperature)
        else:
            if not self.settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is required for provider=openai")
            self.provider = OpenAIProvider(self.settings.openai_api_key, self.settings.model, self.settings.temperature)

    def _prepare_messages(self, context_msgs: List[Dict[str, str]], user_input: str) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = []
        msgs.extend({"role": m["role"], "content": m["content"]} for m in context_msgs)
        msgs.append({"role": "user", "content": user_input})
        return msgs

    def run(self, *, thread_id: str, user_input: str, resource_id: str | None = None) -> str:
        self.thread_id = thread_id
        rid = resource_id or self.settings.resource_id
        if self.settings.memory_enabled and self.db is not None:
            get_or_create_thread(self.db, thread_id, rid)

        if (
            self.settings.memory_enabled
            and self.db is not None
            and self.settings.summary_enabled
            and self.settings.summary_use_llm
            and self.settings.summary_prerun
        ):
            try:
                update_overview_with_llm(
                    self.db,
                    self.provider,
                    thread_id=thread_id,
                    max_chars=self.settings.summary_max_chars,
                )
            except Exception:
                pass

        if self.settings.memory_enabled and self.db is not None:
            ctx_payload = build_context_payload(
                self.db,
                self.embedder,
                thread_id=thread_id,
                resource_id=rid,
                last_messages=self.settings.last_messages,
                semantic_enabled=self.settings.semantic_recall,
                topk=self.settings.topk,
                range_before=self.settings.range_before,
                range_after=self.settings.range_after,
                scope=self.settings.scope,
                user_query=user_input,
            )
        else:
            ctx_payload = []

        msgs = self._prepare_messages(ctx_payload, user_input)

        overview = get_thread_overview(self.db, thread_id) if (self.settings.memory_enabled and self.db is not None) else None
        sys_parts = []
        if self.system_prompt:
            sys_parts.append(self.system_prompt)
        if overview:
            sys_parts.append(f"Thread Overview:\n{overview}")
        system_instruction = "\n\n".join(sys_parts) if sys_parts else None

        debug("agent:call", provider=self.settings.provider, model=self.settings.model, thread_id=thread_id)
        res = self.provider.generate(msgs, system_instruction=system_instruction)
        text = res.get("text", "")

        if self.settings.memory_enabled and self.db is not None:
            uid = add_message(self.db, thread_id=thread_id, role="user", content=user_input, resource_id=rid)
            aid = add_message(self.db, thread_id=thread_id, role="assistant", content=text, resource_id=rid)
            try:
                for mid, txt in ((uid, user_input), (aid, text)):
                    chunks = chunk_text(
                        txt,
                        min_chars=self.settings.chunk_min_chars,
                        max_chars=self.settings.chunk_max_chars,
                    )
                    payload = [c.strip() for c in chunks if c and c.strip()]
                    if payload:
                        vectors = self.embedder.embed(payload)
                        replace_message_embeddings(self.db, mid, vectors)
            except Exception:
                pass

        if (
            self.settings.memory_enabled
            and self.db is not None
            and self.settings.summary_enabled
            and self.settings.summary_use_llm
            and not self.settings.summary_prerun
        ):
            try:
                update_overview_with_llm(
                    self.db,
                    self.provider,
                    thread_id=thread_id,
                    max_chars=self.settings.summary_max_chars,
                )
            except Exception:
                pass

        return text
