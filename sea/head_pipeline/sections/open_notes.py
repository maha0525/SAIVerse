"""OpenNotesSection — 開いているノートを head に注入する。

autonomous_desire.md §7.3 の「open-notes → head 注入」を担当する。2 種類を焼く:

1. **desire ノート（候補プール）の候補 Task** — Track 非依存・全 Track 横断で常に open。
2. **現在の running Track に attach された open ノート**（person/project/vocation）—
   `TrackOpenNote` 紐づき。

cache 安定性について（visual_context / memory_weave と同じ手法）:
    head スナップショットは Metabolism 時に凍結され、その窓内で Track は切り替わりうる。
    capture 時点の running Track の open ノートを焼き、Track が切り替わっても head は
    凍結したまま（古い Track の open ノートを保持）、切替の事実は末尾通知で流し、次の
    Metabolism で初めて再 capture する。これは VisualContextSection が
    ``current_building_id`` を焼いて building 移動に追従するのと同じトレードオフ
    （``refresh_on_events`` から移動/切替イベントを意図的に外す）。

中身: ノートのポインタ（type/title/description）と desire 候補 Task のリストのみ。
ページ/メッセージ本文は memory_weave / recall の責務なのでここには出さない。

詳細: docs/intent/persona_cognition/autonomous_desire.md §5, §7
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

# 候補プールに出す Task の状態 (終了済み = completed/cancelled は除外)。
_CANDIDATE_STATUSES = ("pending", "active", "paused")


@dataclass(frozen=True)
class DesireCandidateItem:
    """desire ノート内の候補 Task 1 件。"""
    task_ref: str   # "task:3"
    title: str
    status: str


@dataclass(frozen=True)
class OpenNoteItem:
    """現 Track に attach された open ノート 1 件のポインタ。"""
    note_type: str
    title: str
    description: str


@dataclass(frozen=True)
class OpenNotesSnapshot:
    desire_note_title: str                            # "" なら desire ノート未生成
    candidates: tuple[DesireCandidateItem, ...]
    open_notes: tuple[OpenNoteItem, ...]              # 現 Track の attach ノート (desire 除く)


class OpenNotesSection:
    name = "open_notes"
    order = 720  # memory_weave(700) の直後、visual_context(800) の前
    # refresh_on_events 空 = Metabolism のみ。Track 切替/ノート開閉では cache を
    # 切らない (visual_context が building 移動で切らないのと同じ)。変動は diff 通知へ。
    refresh_on_events = frozenset()

    def capture(self, ctx: LineHeadInput) -> OpenNotesSnapshot:
        manager = ctx.manager
        empty = OpenNotesSnapshot(desire_note_title="", candidates=(), open_notes=())
        if manager is None:
            return empty
        session_factory = getattr(manager, "SessionLocal", None)
        if session_factory is None:
            return empty

        try:
            from saiverse.note_manager import NOTE_TYPE_DESIRE, NoteManager
            from saiverse.persona_task_manager import PersonaTaskManager
            from saiverse.track_manager import TrackManager
        except Exception:
            LOGGER.warning("open_notes: failed to import managers", exc_info=True)
            return empty

        nm = NoteManager(session_factory)

        # --- 1. desire ノートの候補 Task ---
        desire_title = ""
        candidates: list[DesireCandidateItem] = []
        try:
            desire_notes = nm.list_for_persona(ctx.persona_id, note_type=NOTE_TYPE_DESIRE)
        except Exception:
            LOGGER.warning(
                "open_notes: failed to list desire notes persona=%s",
                ctx.persona_id, exc_info=True,
            )
            desire_notes = []
        if desire_notes:
            desire = desire_notes[0]
            desire_title = str(desire.title or "やりたいこと")
            try:
                ptm = PersonaTaskManager(session_factory)
                tasks = ptm.list_tasks(
                    ctx.persona_id, note_id=desire.note_id,
                    statuses=_CANDIDATE_STATUSES, include_steps=False,
                )
            except Exception:
                LOGGER.warning(
                    "open_notes: failed to list candidate tasks persona=%s",
                    ctx.persona_id, exc_info=True,
                )
                tasks = []
            for t in tasks:
                ref, title = t.get("task_ref"), t.get("title")
                if not ref or not title:
                    continue
                candidates.append(DesireCandidateItem(
                    task_ref=str(ref), title=str(title),
                    status=str(t.get("status") or ""),
                ))
            candidates.sort(key=lambda i: i.task_ref)

        # --- 2. 現 running Track の attach ノート (desire 除く) ---
        open_notes: list[OpenNoteItem] = []
        try:
            running = TrackManager(session_factory).get_running(ctx.persona_id)
            track_id = getattr(running, "track_id", None) if running else None
            if track_id:
                for note in nm.list_open_notes(track_id):
                    if note.note_type == NOTE_TYPE_DESIRE:
                        continue  # desire は §1 で別途扱う
                    open_notes.append(OpenNoteItem(
                        note_type=str(note.note_type or ""),
                        title=str(note.title or ""),
                        description=str(note.description or ""),
                    ))
                open_notes.sort(key=lambda n: (n.note_type, n.title))
        except Exception:
            LOGGER.warning(
                "open_notes: failed to resolve attached notes persona=%s",
                ctx.persona_id, exc_info=True,
            )

        return OpenNotesSnapshot(
            desire_note_title=desire_title,
            candidates=tuple(candidates),
            open_notes=tuple(open_notes),
        )

    def render(self, snapshot: OpenNotesSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        if not snapshot.desire_note_title and not snapshot.open_notes:
            return None
        blocks: list[str] = []

        if snapshot.desire_note_title:
            lines = [
                f"【やりたいこと候補（{snapshot.desire_note_title}）】",
                "いつか取り組みたいと温めている候補のプールです。"
                "自律制御モードではここから Track を作れます。"
                "自律作業モードでは新しい候補を書き加えられます。",
            ]
            if snapshot.candidates:
                lines.extend(f"- {c.task_ref} {c.title}" for c in snapshot.candidates)
            else:
                lines.append("（まだ候補はありません）")
            blocks.append("\n".join(lines))

        if snapshot.open_notes:
            lines = ["【いま開いているノート】"]
            for n in snapshot.open_notes:
                head = f"- [{n.note_type}] {n.title}" if n.note_type else f"- {n.title}"
                if n.description:
                    head += f" — {n.description}"
                lines.append(head)
            blocks.append("\n".join(lines))

        return RenderedSection(text="\n\n".join(blocks))

    def diff_to_notifications(
        self,
        old: Optional[OpenNotesSnapshot],
        new: Optional[OpenNotesSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        labels: list[NotificationLabel] = []

        added = {c.task_ref for c in new.candidates} - {c.task_ref for c in old.candidates}
        if added:
            labels.append(NotificationLabel(
                kind="desire_candidate_added",
                label=f"やりたいこと候補が {len(added)} 件増えました",
            ))

        old_notes = {(n.note_type, n.title) for n in old.open_notes}
        new_notes = {(n.note_type, n.title) for n in new.open_notes}
        if old_notes != new_notes:
            labels.append(NotificationLabel(
                kind="open_notes_changed",
                label="いま開いているノートが変わりました",
            ))
        return labels

    def serialize_snapshot(self, snapshot: OpenNotesSnapshot) -> str:
        return json.dumps(
            {
                "desire_note_title": snapshot.desire_note_title,
                "candidates": [asdict(c) for c in snapshot.candidates],
                "open_notes": [asdict(n) for n in snapshot.open_notes],
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> OpenNotesSnapshot:
        payload = json.loads(data)
        candidates = tuple(
            DesireCandidateItem(**c) for c in payload.get("candidates", [])
        )
        open_notes = tuple(
            OpenNoteItem(**n) for n in payload.get("open_notes", [])
        )
        return OpenNotesSnapshot(
            desire_note_title=str(payload.get("desire_note_title", "")),
            candidates=candidates,
            open_notes=open_notes,
        )
