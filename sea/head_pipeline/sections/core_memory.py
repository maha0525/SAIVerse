"""CoreMemorySection — ペルソナのコア記憶 (記憶アーキv2 ゾーン A) を head に注入する。

コア記憶＝ペルソナが自分で選んで刻む恒常知識。head (システムプロンプト部) に常駐し、
Metabolism 時のみ更新が反映される。編集主体はペルソナ自身 (core_memory_add /
core_memory_update / core_memory_remove スペル)。

cache 安定性: ``refresh_on_events = frozenset()`` = Metabolism のみ再 capture。
ペルソナがスペルでコア記憶を編集しても、head は次の Metabolism まで凍結したまま
(open_notes / memory_weave と同じトレードオフ)。編集主体がペルソナ自身なので、
変更を本人に通知する意味はなく ``diff_to_notifications`` は空を返す。

配置: ``persona_self`` (自分は誰か) の直後。「自分は誰か」→「自分がずっと覚えて
いること」の順で並ぶ (order=250, persona_self=200 < building=300 の間)。

詳細: docs/intent/memory_architecture_v2.md §5
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
class CoreMemoryItem:
    """コア記憶 1 件のスナップショット。"""
    memory_id: int
    content: str


@dataclass(frozen=True)
class CoreMemorySnapshot:
    items: tuple[CoreMemoryItem, ...]


class CoreMemorySection:
    name = "core_memory"
    order = 250  # persona_self(200) の直後、building(300) の前
    # refresh_on_events 空 = Metabolism のみ。スペルによる編集では cache を切らない
    # (open_notes / memory_weave と同じ)。反映は次の Metabolism から。
    refresh_on_events = frozenset()

    def capture(self, ctx: LineHeadInput) -> CoreMemorySnapshot:
        persona = ctx.persona
        empty = CoreMemorySnapshot(items=())
        if persona is None:
            return empty

        sai_mem = getattr(persona, "sai_memory", None)
        conn = getattr(sai_mem, "conn", None) if sai_mem else None
        db_lock = getattr(sai_mem, "_db_lock", None) if sai_mem else None
        if conn is None:
            return empty

        try:
            from sai_memory.core_memory import list_core_memories
            if db_lock is not None:
                with db_lock:
                    rows = list_core_memories(conn)
            else:
                rows = list_core_memories(conn)
        except Exception:
            LOGGER.warning(
                "core_memory: failed to list core memories persona=%s",
                ctx.persona_id, exc_info=True,
            )
            return empty

        items = tuple(
            CoreMemoryItem(memory_id=r.id, content=r.content) for r in rows
        )
        return CoreMemorySnapshot(items=items)

    def render(self, snapshot: CoreMemorySnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.items:
            return None  # 空なら非表示
        lines = ["## コア記憶（自分で選んで刻んだ、常に携えている知識）"]
        for item in snapshot.items:
            lines.append(f"- [c:{item.memory_id}] {item.content}")
        return RenderedSection(text="\n".join(lines))

    def diff_to_notifications(
        self,
        old: Optional[CoreMemorySnapshot],
        new: Optional[CoreMemorySnapshot],
    ) -> list[NotificationLabel]:
        # 編集主体がペルソナ自身なので、変更を本人に通知する意味はない。
        return []

    def serialize_snapshot(self, snapshot: CoreMemorySnapshot) -> str:
        return json.dumps(
            {"items": [asdict(i) for i in snapshot.items]},
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> CoreMemorySnapshot:
        payload = json.loads(data)
        items = tuple(
            CoreMemoryItem(**i) for i in payload.get("items", [])
        )
        return CoreMemorySnapshot(items=items)
