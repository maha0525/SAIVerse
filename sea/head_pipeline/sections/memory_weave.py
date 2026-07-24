"""MemoryWeaveSection — Memory Weave (Chronicle + Memopedia) context を head に。

`sea/runtime_context.py` 旧 Memory Weave 経路 (`get_memory_weave_context`) を
snapshot 化する thin wrapper。capture 時に 1 回呼んで結果を frozen 化、render は
snapshot のみから組み立てる。

旧経路は Chronicle / Track Chronicle / Memopedia の 3 種類を **別々の user message**
として流していて、preview UI もそれを ``__memory_weave_type__`` メタデータで
ラベル分けしていた。本 Section は 3 種類を独立 entry として snapshot に保持し、
composition (integration.py) 側でそれぞれ別 message として展開する形を維持する。

記憶アーキv2 §7.1 (2026-07-04): Memopedia 索引の head 常時掲示は既定で廃止し、
知識への接触は自動想起 (ゾーンC) + 深掘りスペルに一本化した。

P4-d (2026-07-11): Memopedia 索引は MemopediaIndexSection に一本化した。
``MEMOPEDIA_INDEX_ENABLED`` トグルは MemopediaIndexSection 側で読む。
WeaveSection は Memopedia 索引を一切掲示しない。旧後方互換経路
(WeaveSection 側の _resolve_memopedia_index_enabled / include_memopedia 引数) は
廃止済み——2026-07-14 に get_memory_weave_context 自体から
include_memopedia 引数と _get_memopedia_context を削除し、死にコードを一掃した。

refresh_on_events は空 (Metabolism のみ)。Chronicle / Memopedia の動的更新は
将来 dynamic_state 連携で末尾通知に流す前提。

詳細: docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryWeaveEntry:
    """1 種類 (chronicle / track_chronicle / memopedia) 分のコンテキスト。"""
    kind: str       # "chronicle" / "track_chronicle" / "memopedia"
    content: str    # message content (intro + 本文を結合済み)


@dataclass(frozen=True)
class MemoryWeaveSnapshot:
    enabled: bool
    entries: tuple[MemoryWeaveEntry, ...]


class MemoryWeaveSection:
    name = "memory_weave"
    order = 700
    refresh_on_events = frozenset()  # default: Metabolism のみ

    def capture(self, ctx: LineHeadInput) -> MemoryWeaveSnapshot:
        manager = ctx.manager
        persona = ctx.persona
        if manager is None or persona is None:
            return MemoryWeaveSnapshot(enabled=False, entries=())

        enabled = self._resolve_enabled(manager, ctx.persona_id)
        if not enabled:
            return MemoryWeaveSnapshot(enabled=False, entries=())

        try:
            from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
            from tools.context import persona_context
        except Exception:
            LOGGER.warning(
                "memory_weave: failed to import get_memory_weave_context",
                exc_info=True,
            )
            return MemoryWeaveSnapshot(enabled=True, entries=())

        sai_mem = getattr(persona, "sai_memory", None)
        persona_dir_path = getattr(sai_mem, "persona_dir", None) if sai_mem else None
        persona_dir = str(persona_dir_path) if persona_dir_path else None

        # metabolism anchor を渡して、track_chronicle の生メッセージダンプから
        # 「履歴 (anchor 以降) に既に載っている分」を除外させる (重複トークン削減)。
        # 正は session_anchor 行 (persona, ctx.model_key) — 旧
        # history_manager.metabolism_anchor_message_id (persona 単一可変属性) は
        # 廃止 (beat_execution_context.md §3.2)。読めない環境 (テストスタブ等) は
        # None = 除外なし (従来のフォールバックと同じ縮退)。
        anchor_id = None
        # 窓の中で digest に置き換えて見せている範囲のあらすじは head から外す
        # (chronicle_eviction.md §6 — 同じあらすじが窓と head に二重で出ると、
        #  同じ出来事が二度あったかのような時系列の錯覚をペルソナに招く)。
        folded_entry_ids: list[str] = []
        try:
            sea_runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
            lifecycle = getattr(sea_runtime, "session_lifecycle", None)
            load_entry = getattr(lifecycle, "load_anchor_entry", None)
            model_key = getattr(ctx, "model_key", None)
            if callable(load_entry) and model_key:
                entry = load_entry(ctx.persona_id, str(model_key))
                anchor_id = entry.get("anchor_id") if entry else None
                from sea.session_window import deserialize_folds
                for fold in deserialize_folds(entry.get("folded_ranges") if entry else None):
                    folded_entry_ids.extend(fold.chronicle_entry_ids)
        except Exception:
            LOGGER.debug(
                "memory_weave: anchor row read failed persona=%s", ctx.persona_id,
                exc_info=True,
            )

        # P4-d: Memopedia 索引は MemopediaIndexSection が担当する。
        # get_memory_weave_context 自体が Memopedia 索引に一切関与しなくなった
        # (2026-07-14、include_memopedia 引数ごと削除) ため、ここでは
        # Chronicle / Track Chronicle のみを取得する。
        try:
            with persona_context(ctx.persona_id, persona_dir, manager):
                mw_messages = get_memory_weave_context(
                    persona_id=ctx.persona_id, persona_dir=persona_dir,
                    history_anchor_message_id=anchor_id,
                    exclude_chronicle_entry_ids=folded_entry_ids or None,
                )
        except Exception:
            LOGGER.warning(
                "memory_weave: get_memory_weave_context raised persona=%s",
                ctx.persona_id, exc_info=True,
            )
            return MemoryWeaveSnapshot(enabled=True, entries=())

        if not mw_messages:
            return MemoryWeaveSnapshot(enabled=True, entries=())

        entries: list[MemoryWeaveEntry] = []
        for msg in mw_messages:
            if not isinstance(msg, dict):
                continue
            metadata = msg.get("metadata") or {}
            kind = str(metadata.get("__memory_weave_type__") or "").strip()
            if not kind:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            entries.append(MemoryWeaveEntry(kind=kind, content=content))
        return MemoryWeaveSnapshot(enabled=True, entries=tuple(entries))

    def render(self, snapshot: MemoryWeaveSnapshot) -> Optional[RenderedSection]:
        # render は head の concat 用フォールバック (preview の section ラベル分けは
        # composition 側で別経路で行う、こちらの戻り値はフォールバック表示にのみ使う)。
        # snapshot を確認して空なら None を返し、それ以外なら全 entry を結合して返す。
        if snapshot is None or not snapshot.enabled or not snapshot.entries:
            return None
        text = "\n\n".join(e.content for e in snapshot.entries).strip()
        if not text:
            return None
        return RenderedSection(text=text)

    def diff_to_notifications(
        self,
        old: Optional[MemoryWeaveSnapshot],
        new: Optional[MemoryWeaveSnapshot],
    ) -> list[NotificationLabel]:
        # Phase 2 段階では「Chronicle / Memopedia は世界状態の動的部分」の通知は
        # 既存 dynamic_state 側に任せる。本 Section は head 凍結だけを担当し、
        # diff 通知は出さない (= 末尾通知の責務は dynamic_state Section が引き継ぐ
        # 想定、Phase 3 で整理)。
        return []

    def serialize_snapshot(self, snapshot: MemoryWeaveSnapshot) -> str:
        return json.dumps(
            {
                "enabled": snapshot.enabled,
                "entries": [asdict(e) for e in snapshot.entries],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> MemoryWeaveSnapshot:
        payload = json.loads(data)
        if "entries" not in payload:
            # 旧 schema (text フィールド単体だった頃) を検出。再 capture させるため
            # 例外を投げて store.load 経路で snapshot を捨てる。
            raise ValueError(
                "MemoryWeaveSnapshot: legacy schema detected (no 'entries' key), "
                "snapshot will be re-captured",
            )
        entries = tuple(
            MemoryWeaveEntry(**e) for e in payload.get("entries", [])
        )
        return MemoryWeaveSnapshot(
            enabled=bool(payload.get("enabled", False)),
            entries=entries,
        )

    # ---- 内部ヘルパー ----

    def _resolve_enabled(self, manager, persona_id: str) -> bool:
        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return False
        db = session_factory()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return bool(ai.MEMORY_WEAVE_CONTEXT) if ai else False
        except Exception:
            LOGGER.warning(
                "memory_weave: failed to resolve MEMORY_WEAVE_CONTEXT persona=%s",
                persona_id, exc_info=True,
            )
            return False
        finally:
            db.close()
