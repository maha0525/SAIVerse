"""ChronicleIndexSection — Chronicle エントリ追加の差分検知。

旧 ``DynamicStateManager`` の chronicle 差分計算ロジックを Section interface に
移植。head には何も render しない。差分通知は b.captured_at 以降に追加された
エントリを Level 別件数でまとめて 1 ラベルにする (= 旧通知と同等)。

詳細: docs/intent/cached_head_architecture.md §5.1
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)

CHRONICLE_FETCH_LIMIT = 50


@dataclass(frozen=True)
class ChronicleEntryItem:
    entry_id: str
    level: int
    created_at: int


@dataclass(frozen=True)
class ChronicleIndexSnapshot:
    captured_at: float
    entries: tuple[ChronicleEntryItem, ...]   # 最新 N 件


class ChronicleIndexSection:
    name = "chronicle_index"
    order = 1300
    refresh_on_events = frozenset()  # Metabolism のみ

    def capture(self, ctx: LineHeadInput) -> ChronicleIndexSnapshot:
        persona = ctx.persona
        if persona is None:
            return ChronicleIndexSnapshot(captured_at=time.time(), entries=())

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not getattr(sai_mem, "conn", None):
            return ChronicleIndexSnapshot(captured_at=time.time(), entries=())

        entries: list[ChronicleEntryItem] = []
        try:
            cur = sai_mem.conn.execute(
                "SELECT id, level, created_at FROM arasuji_entries "
                "ORDER BY created_at DESC LIMIT ?",
                (CHRONICLE_FETCH_LIMIT,),
            )
            for row in cur.fetchall():
                entries.append(ChronicleEntryItem(
                    entry_id=str(row[0]),
                    level=int(row[1] or 1),
                    created_at=int(row[2] or 0),
                ))
        except Exception:
            LOGGER.debug(
                "chronicle_index: failed to query arasuji_entries", exc_info=True,
            )
        return ChronicleIndexSnapshot(captured_at=time.time(), entries=tuple(entries))

    def render(self, snapshot: ChronicleIndexSnapshot) -> Optional[RenderedSection]:
        return None

    def diff_to_notifications(
        self,
        old: Optional[ChronicleIndexSnapshot],
        new: Optional[ChronicleIndexSnapshot],
    ) -> list[NotificationLabel]:
        # 件数ラベル diff は退役 (beat_execution_context.md §3.2 / §3.3、§6-5)。
        # Chronicle の可視化は「model の節目の構造交換」が担保する — 退役 (anchor
        # 前進) の瞬間に、その model の head 再 capture で Chronicle が生ログと
        # 入れ替わりに見える (intent §3.2)。窓が生ログでカバーしている間は情報
        # 欠落がないため、「新しいエントリが追加されました (Level N × M件)」の
        # 操作ラベル通知は重複ノイズでしかない。intent §3.3 の「memory_weave /
        # chronicle_index の diff 未整理も本工事で潰す」の実装形。
        return []

    def serialize_snapshot(self, snapshot: ChronicleIndexSnapshot) -> str:
        return json.dumps(
            {
                "captured_at": snapshot.captured_at,
                "entries": [asdict(e) for e in snapshot.entries],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> ChronicleIndexSnapshot:
        payload = json.loads(data)
        entries = tuple(
            ChronicleEntryItem(**e) for e in payload.get("entries", [])
        )
        return ChronicleIndexSnapshot(
            captured_at=float(payload.get("captured_at", 0.0)),
            entries=entries,
        )
