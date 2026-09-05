"""VisualContextSection — visual context (Building / Persona images + Items) を head に。

`get_visual_context` (`builtin_data/tools/get_visual_context.py`) のロジックを
snapshot 化する thin wrapper。capture 時に世界を 1 回だけ読み
(`build_visual_contexts`)、head 用の text と media (画像 path / mime_type)、
および同じ瞬間の知覚記法の姿 (`room_text`) を frozen snapshot として焼く。

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
    #: この snapshot が見せている Building。head は移動では撮り直さない (下の
    #: refresh_on_events を参照) ので、ペルソナの現在地とは限らない。
    building_id: Optional[str] = None
    #: 同じ瞬間の同じ部屋を、**知覚バッファの記法**で書いたもの
    #: (``for_perception=True`` / 自分の外見とインベントリ抜き)。入室時に積む
    #: 「移動先の様子」と同じ書式なので、そのまま差分の土台にできる
    #: (sai_memory/room_state.py — head の一覧と知覚の全文が二重になる問題)。
    #: head の描画には**使わない** (render は text だけを出す)。
    room_text: str = ""


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
            from builtin_data.tools.get_visual_context import (
                HEAD_VIEW,
                PERCEPTION_VIEW,
                build_visual_contexts,
            )
        except Exception:
            LOGGER.warning(
                "visual_context: failed to import build_visual_contexts",
                exc_info=True,
            )
            return VisualContextSnapshot(text="", media=())

        # head 用の姿と知覚記法の姿を **一度の読み**から作る。二回読むと、その
        # 間にアイテムが増えたり誰かが出入りしたりして、head が見せている部屋と
        # 差分の土台になる room_text が別時点のものになる (2026-09-05 Codex
        # 指摘)。build_visual_contexts は読みを一度だけ行い、描き分けだけを姿
        # ごとにする。
        try:
            with persona_context(persona_id, persona_dir, manager):
                messages, room_messages = build_visual_contexts(
                    ctx.current_building_id, (HEAD_VIEW, PERCEPTION_VIEW),
                )
        except Exception:
            LOGGER.warning(
                "visual_context: build_visual_contexts raised persona=%s building=%s",
                persona_id, ctx.current_building_id, exc_info=True,
            )
            return VisualContextSnapshot(text="", media=())

        # 知覚記法の姿。入室時に積む「移動先の様子」と同じ書式なので、head が
        # 既に見せている姿とそのまま突き合わせられる (sai_memory/room_state.py
        # の head 土台の差分)。head の描画には使わない。
        room_text = ""
        if room_messages and isinstance(room_messages[0], dict):
            room_text = str(room_messages[0].get("content") or "").strip()

        if not messages:
            # head に何も出ない = この部屋を「見せている」ことにはならない。
            # building_id を記録すると、提示側が「head が見せているから差分の
            # ままでよい」と読んでしまう (全体像がどこにも無い状態)。
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
        return VisualContextSnapshot(
            text=text, media=tuple(media_entries),
            building_id=ctx.current_building_id, room_text=room_text,
        )

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
        # 中身ゼロの「周囲の見え方が変わりました」フラグは廃止 (2026-07-09)。
        # 移動時の視覚変化は on_building_entered が「移動先の様子」(内装画像・アイテム・
        # 他ペルソナ外見) を知覚バッファへ push して中身ごと届ける。外見変化は
        # refresh_on_events={APPEARANCE_CHANGED} で head 本体が更新され反映される。
        # よって tail の中身ゼロ通知は不要 (ごちゃつきの原因だった)。
        # 詳細: docs/intent/perception_buffer.md §5.4
        return []

    def serialize_snapshot(self, snapshot: VisualContextSnapshot) -> str:
        return json.dumps(
            {
                "text": snapshot.text,
                "media": [asdict(m) for m in snapshot.media],
                "building_id": snapshot.building_id,
                "room_text": snapshot.room_text,
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> VisualContextSnapshot:
        # building_id / room_text を持たない旧行は None / "" で復元される
        # (= head 土台の差分は使えず、従来どおり全文を積む)。次の capture で入る。
        payload = json.loads(data)
        media = tuple(VisualMediaEntry(**m) for m in payload.get("media", []))
        return VisualContextSnapshot(
            text=payload.get("text", ""),
            media=media,
            building_id=payload.get("building_id"),
            room_text=payload.get("room_text", "") or "",
        )
