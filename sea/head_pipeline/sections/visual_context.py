"""VisualContextSection — visual context (Building / Persona images + Items) を head に。

`get_visual_context` (`builtin_data/tools/get_visual_context.py`) のロジックを
snapshot 化する thin wrapper。capture 時に 1 回 get_visual_context を呼び、
text と media (画像 path / mime_type) を frozen snapshot として焼く。

旧 `sea/runtime_context.py` の visual_context cache (anchor キーの persona 属性
``_visual_context_cache`` / ``_visual_context_anchor``) を本 Section の snapshot
機構が吸収する。

詳細: docs/intent/cached_head_architecture.md §5.2
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    MediaRef,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisualMediaEntry:
    path: str
    mime_type: str
    role: str


@dataclass(frozen=True)
class VisualContextSnapshot:
    text: str
    media: tuple[VisualMediaEntry, ...]


class VisualContextSection:
    name = "visual_context"
    order = 800  # system prompt 系より後ろ、history より前
    # NOTE: 当初 BUILDING_ENTERED を含めていたが、これだと移動の瞬間に
    # head が refresh されて cache が壊れる (= "cache 中変えない" 原則違反)。
    # 移動の事実は auto_ingest の入室通知 + flush_diffs (Building/Items/Occupants
    # Section の diff label) で末尾通知される。次の Metabolism まで head には
    # 古い Building の visual_context が残るのは意図通りのトレードオフ。
    refresh_on_events = frozenset({EventType.APPEARANCE_CHANGED})

    def capture(self, ctx: LineHeadInput) -> VisualContextSnapshot:
        manager = ctx.manager
        persona = ctx.persona
        if manager is None or persona is None:
            return VisualContextSnapshot(text="", media=())

        persona_id = getattr(persona, "persona_id", None) or ctx.persona_id
        persona_dir = getattr(persona, "persona_dir", None)

        try:
            from tools.context import persona_context
            from builtin_data.tools.get_visual_context import get_visual_context
        except Exception:
            LOGGER.warning(
                "visual_context: failed to import get_visual_context", exc_info=True,
            )
            return VisualContextSnapshot(text="", media=())

        try:
            with persona_context(persona_id, persona_dir, manager):
                messages = get_visual_context(building_id=ctx.current_building_id)
        except Exception:
            LOGGER.warning(
                "visual_context: get_visual_context raised persona=%s building=%s",
                persona_id, ctx.current_building_id, exc_info=True,
            )
            return VisualContextSnapshot(text="", media=())

        if not messages:
            return VisualContextSnapshot(text="", media=())

        text_parts: list[str] = []
        media_entries: list[VisualMediaEntry] = []
        seen_media_keys: set[tuple[str, str]] = set()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            metadata = msg.get("metadata") or {}
            media_list = metadata.get("media") or []
            if not isinstance(media_list, list):
                continue
            for media_item in media_list:
                if not isinstance(media_item, dict):
                    continue
                path = str(media_item.get("path") or "")
                if not path:
                    continue
                mime = str(media_item.get("mime_type") or "")
                role = str(media_item.get("type") or media_item.get("role") or "image")
                key = (path, mime)
                if key in seen_media_keys:
                    continue
                seen_media_keys.add(key)
                media_entries.append(VisualMediaEntry(
                    path=path, mime_type=mime, role=role,
                ))

        text = "\n\n".join(text_parts).strip()
        return VisualContextSnapshot(text=text, media=tuple(media_entries))

    def render(self, snapshot: VisualContextSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        if not snapshot.text and not snapshot.media:
            return None
        media_refs = [
            MediaRef(path=m.path, mime_type=m.mime_type, role=m.role)
            for m in snapshot.media
        ]
        return RenderedSection(text=snapshot.text or None, media=media_refs)

    def diff_to_notifications(
        self,
        old: Optional[VisualContextSnapshot],
        new: Optional[VisualContextSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        labels: list[NotificationLabel] = []
        old_paths = {m.path for m in old.media}
        new_paths = {m.path for m in new.media}
        added = new_paths - old_paths
        removed = old_paths - new_paths
        if added or removed:
            labels.append(NotificationLabel(
                kind="visual_context_media_changed",
                label="周囲の見え方が変わりました",
            ))
        elif old.text != new.text:
            labels.append(NotificationLabel(
                kind="visual_context_text_changed",
                label="周囲の状況に変化がありました",
            ))
        return labels

    def serialize_snapshot(self, snapshot: VisualContextSnapshot) -> str:
        return json.dumps(
            {
                "text": snapshot.text,
                "media": [asdict(m) for m in snapshot.media],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> VisualContextSnapshot:
        payload = json.loads(data)
        media = tuple(VisualMediaEntry(**m) for m in payload.get("media", []))
        return VisualContextSnapshot(text=payload.get("text", ""), media=media)
