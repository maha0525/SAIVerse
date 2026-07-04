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
    """コア記憶 1 件のスナップショット。

    ``kind`` は 'note' (書き下ろしテキスト) / 'scene' (実会話の切り抜き)。
    ``metadata`` は scene の由来参照 (anchor_id/message_ids/date_range の JSON 文字列)。
    旧 snapshot (kind/metadata を持たない) の deserialize でも既定値で復元できる
    ようにフィールドにデフォルトを与える (後方互換)。
    """
    memory_id: int
    content: str
    kind: str = "note"
    metadata: Optional[str] = None


@dataclass(frozen=True)
class CoreMemorySnapshot:
    items: tuple[CoreMemoryItem, ...]


def _render_scene(item: "CoreMemoryItem") -> str:
    """kind='scene' の項目を、現在の会話と混同されない形式で描画する。

    必須の3要素 (docs/intent/memory_architecture_v2.md §5, §4.2):
    1. フェンス (```) で囲まれた原文 — マークダウン再解釈防止・引用境界の明確化
    2. 日付
    3. 「過去の実際のやり取りの記録」であることの明示
    """
    date_label = ""
    if item.metadata:
        try:
            meta = json.loads(item.metadata)
            date_range = meta.get("date_range")
            if isinstance(date_range, list) and date_range:
                start = date_range[0]
                end = date_range[-1]
                date_label = f" ({start})" if start == end else f" ({start}〜{end})"
        except (json.JSONDecodeError, TypeError, AttributeError):
            LOGGER.debug("core_memory: failed to parse scene metadata for c:%s", item.memory_id)
    return (
        f"- [c:{item.memory_id}] 会話の記憶{date_label} — 過去の実際のやり取りの記録:\n"
        f"  ```\n"
        f"{_indent_block(item.content, '  ')}\n"
        f"  ```"
    )


def _indent_block(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.split("\n"))


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
            CoreMemoryItem(
                memory_id=r.id, content=r.content, kind=r.kind, metadata=r.metadata,
            )
            for r in rows
        )
        return CoreMemorySnapshot(items=items)

    def render(self, snapshot: CoreMemorySnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.items:
            return None  # 空なら非表示
        lines = ["## コア記憶（自分で選んで刻んだ、常に携えている知識）"]
        for item in snapshot.items:
            if item.kind == "scene":
                lines.append(_render_scene(item))
            else:
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
