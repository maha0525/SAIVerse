"""Pulse タイムライン API: SAIMemory messages を pulse_id でグルーピングして
自律 Track の動作を可視化する。

設計: docs/intent/persona_cognition/debug_controller.md (可視化セクション)。
pulse_logs テーブル (node 実行イベント) ではなく messages を軸にするのは、
自律 sub_line Pulse を含む全 Pulse が messages.pulse_id に確実に記録されるため
(pulse_logs への記録有無に依存しない)。
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.deps import get_manager

from .utils import get_adapter

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class PulseTimelineMessage(BaseModel):
    entry_id: str
    role: str
    content: str
    created_at: Optional[float]
    line_role: Optional[str]
    scope: Optional[str]
    origin_track_id: Optional[str]
    spell_origin_id: Optional[str]
    spell_seq: Optional[int]


class PulseTimelineItem(BaseModel):
    pulse_id: str
    line_roles: List[str]
    message_count: int
    first_created_at: Optional[float]
    last_created_at: Optional[float]


class PulseTimelineResponse(BaseModel):
    items: List[PulseTimelineItem]
    total: int


class PulsePromptEntry(BaseModel):
    node_id: Optional[str]
    content: str  # 送信 messages の JSON dump (Pulse 開始時の入力プロンプト)
    created_at: Optional[float]


class GapMessage(BaseModel):
    entry_id: str
    role: str
    content: str
    created_at: Optional[float]


class PulseDetailResponse(BaseModel):
    pulse_id: str
    messages: List[PulseTimelineMessage]
    prompts: List[PulsePromptEntry]
    gap_messages: List[GapMessage]
    total: int


_VALID_LINE_ROLES = {"main_line", "sub_line", "meta_judgment"}
_VALID_SCOPES = {"committed", "volatile", "discardable"}


class MessagePatchItem(BaseModel):
    entry_id: str
    line_role: Optional[str] = None
    scope: Optional[str] = None


class MessagePatchRequest(BaseModel):
    updates: List[MessagePatchItem]


class MessagePatchResponse(BaseModel):
    updated: int


def _thread_prefix(persona_id: str) -> str:
    # SAIMemory thread_id は persona_id をキーにする (e.g. 'air_city_a:__persona__')
    return f"{persona_id}:%"


# Track の題・種別・連番の欄は束 6c (2026-08-22) で撤去した — 書き手 (Pulse への
# origin_track_id 刻印) が 2026-08-21 に退役して、新しい Pulse では常に空だったため
# (track_retirement.md §8.6)。
_SUMMARY_SQL = """
    SELECT pulse_id,
           MIN(created_at) AS first_ca,
           MAX(created_at) AS last_ca,
           COUNT(*) AS cnt,
           GROUP_CONCAT(DISTINCT line_role) AS roles
    FROM messages
    WHERE thread_id LIKE ? AND pulse_id IS NOT NULL
    GROUP BY pulse_id
    ORDER BY last_ca DESC
    LIMIT ?
"""

_DETAIL_SQL = """
    SELECT id, role, content, created_at, line_role, scope, origin_track_id,
           paired_action_text, spell_origin_id, spell_seq
    FROM messages
    WHERE thread_id LIKE ? AND pulse_id = ?
    ORDER BY created_at ASC, rowid ASC
"""

_PREV_PULSE_MAX_CA_SQL = """
    SELECT MAX(created_at) FROM messages
    WHERE thread_id LIKE ? AND pulse_id IS NOT NULL AND pulse_id != ?
      AND created_at < ?
"""

_GAP_MESSAGES_SQL = """
    SELECT id, role, content, created_at FROM messages
    WHERE thread_id LIKE ? AND pulse_id IS NULL
      AND created_at > ? AND created_at < ?
    ORDER BY created_at ASC, rowid ASC
"""

_GAP_FALLBACK_SQL = """
    SELECT id, role, content, created_at FROM messages
    WHERE thread_id LIKE ? AND pulse_id IS NULL AND created_at < ?
    ORDER BY created_at DESC, rowid DESC LIMIT 1
"""


@router.get("/{persona_id}/pulse-timeline", response_model=PulseTimelineResponse)
def get_pulse_timeline(
    persona_id: str,
    limit: int = Query(200, ge=1, le=1000),
    manager=Depends(get_manager),
):
    """messages を pulse_id でグルーピングした Pulse サマリ一覧 (新しい順)。"""
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            cur = adapter.conn.execute(_SUMMARY_SQL, (_thread_prefix(persona_id), limit))
            rows = cur.fetchall()

    items: List[PulseTimelineItem] = []
    for r in rows:
        pulse_id, first_ca, last_ca, cnt, roles = r
        items.append(
            PulseTimelineItem(
                pulse_id=pulse_id,
                line_roles=sorted((roles or "").split(",")) if roles else [],
                message_count=int(cnt),
                first_created_at=float(first_ca) if first_ca is not None else None,
                last_created_at=float(last_ca) if last_ca is not None else None,
            )
        )
    return PulseTimelineResponse(items=items, total=len(items))


@router.get("/{persona_id}/pulse-timeline/{pulse_id}", response_model=PulseDetailResponse)
def get_pulse_detail(
    persona_id: str,
    pulse_id: str,
    manager=Depends(get_manager),
):
    """指定 Pulse の messages 全件 (discardable 含む、時系列順)。

    discardable も返すのは、メタ判断の破棄分も可視化したいため (storage_layers
    が discardable を除外するのと意図的に異なる)。
    """
    thread_pat = _thread_prefix(persona_id)
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            cur = adapter.conn.execute(_DETAIL_SQL, (thread_pat, pulse_id))
            rows = cur.fetchall()

            # gap messages: 前回 Pulse 終了 〜 今回 Pulse 開始の間のメッセージ
            first_ca = min((float(r[3]) for r in rows if r[3] is not None), default=None)
            gap_rows: list = []
            if first_ca is not None:
                prev_cur = adapter.conn.execute(
                    _PREV_PULSE_MAX_CA_SQL, (thread_pat, pulse_id, first_ca),
                )
                prev_max_ca = prev_cur.fetchone()[0]
                if prev_max_ca is not None:
                    gap_cur = adapter.conn.execute(
                        _GAP_MESSAGES_SQL, (thread_pat, float(prev_max_ca), first_ca),
                    )
                    gap_rows = gap_cur.fetchall()
                else:
                    fb_cur = adapter.conn.execute(
                        _GAP_FALLBACK_SQL, (thread_pat, first_ca),
                    )
                    gap_rows = fb_cur.fetchall()

    messages = [
        PulseTimelineMessage(
            entry_id=r[0],
            role=r[1],
            content=r[2] or "",
            created_at=float(r[3]) if r[3] is not None else None,
            line_role=r[4],
            scope=r[5],
            origin_track_id=r[6],
            spell_origin_id=r[8],
            spell_seq=int(r[9]) if r[9] is not None else None,
        )
        for r in rows
    ]
    prompts: List[PulsePromptEntry] = []
    for r in rows:
        pat = r[7]
        if pat:
            prompts.append(
                PulsePromptEntry(
                    node_id=r[0],
                    content=pat,
                    created_at=float(r[3]) if r[3] is not None else None,
                )
            )
    gap_messages = [
        GapMessage(
            entry_id=r[0],
            role=r[1],
            content=r[2] or "",
            created_at=float(r[3]) if r[3] is not None else None,
        )
        for r in gap_rows
    ]
    return PulseDetailResponse(
        pulse_id=pulse_id, messages=messages, prompts=prompts,
        gap_messages=gap_messages, total=len(messages),
    )


@router.patch("/{persona_id}/messages", response_model=MessagePatchResponse)
def patch_messages(
    persona_id: str,
    body: MessagePatchRequest,
    manager=Depends(get_manager),
):
    """SAIMemory messages の line_role / scope を一括更新する。"""
    updated = 0
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            for item in body.updates:
                sets: List[str] = []
                vals: List[str] = []
                if item.line_role is not None:
                    if item.line_role not in _VALID_LINE_ROLES:
                        continue
                    sets.append("line_role = ?")
                    vals.append(item.line_role)
                if item.scope is not None:
                    if item.scope not in _VALID_SCOPES:
                        continue
                    sets.append("scope = ?")
                    vals.append(item.scope)
                if not sets:
                    continue
                vals.append(item.entry_id)
                sql = f"UPDATE messages SET {', '.join(sets)} WHERE id = ?"
                cur = adapter.conn.execute(sql, vals)
                updated += cur.rowcount
            adapter.conn.commit()
    LOGGER.info(
        "[pulse-timeline] patch_messages: persona=%s, requested=%d, updated=%d",
        persona_id, len(body.updates), updated,
    )
    return MessagePatchResponse(updated=updated)
