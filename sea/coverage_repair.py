"""被覆補修 (arasuji_levels.md §16) — 止め線と、被覆済みの窓への印。

不変条件 (§16-1): 編纂対象のメッセージは「いずれかの提示窓の中にある」か
「少なくとも一つの一次あらすじの source である」かのどちらかでなければならない。
このモジュールはその保証側 — 窓の外に取り残された未被覆領域を、既存の編纂
パイプライン (W4 計画器 = plan_alignment) で一次あらすじにするための部品を持つ。

三つの部品:

1. **止め線 (compile ceiling)**: 温かい (TTL 内の) session_anchor 行のうち
   正典順で最古の anchor 位置。全量計画 (被覆補修 / 一括生成) はこれより
   新しいメッセージを編纂しない — 会話中の窓の下を掘ると、head のあらすじ枠と
   生の提示の二重提示か、生きたキャッシュの破壊のどちらかが起きるため。
   見積もり (estimate_chronicle_generation_cost) と生成 (generate_chronicle)
   の両方が :func:`resolve_compile_ceiling` と
   ``clip_messages_before_position`` (sai_memory 側) の同じ対を通る —
   表示と実走が違う数を言ってはならない (§16-2 の裁定)。
2. **冷えた anchor 行への印** (:func:`mark_covered_cold_windows`): 補修が
   冷えた窓の下を編纂したら、その窓を覆うエントリを §15 の印
   (``presented_raw`` の圧縮区間記録) として行へ追記する。提示は生のまま
   変わらず、head のあらすじ枠の除外名簿としてだけ効く — 休眠モデルが
   目覚めても二重提示が起きない。
3. **窓の誕生時の護り** (§16-3): 新しい (persona, model) の anchor 行が
   生まれるとき、被覆済み領域の上に窓が開くなら同じ印を初期値として載せる
   (書き込み側は ``SessionLifecycle.upsert_anchor_entry``)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sea.session_window import FoldedRange

LOGGER = logging.getLogger(__name__)

#: get_entries_covering_messages へ渡す 1 回分の id 数 (SQL の IN 句の上限対策)。
_COVERING_QUERY_CHUNK = 400


@dataclass(frozen=True)
class CompileCeiling:
    """編纂してよい上端 = 温かい anchor のうち正典順で最古の位置。

    位置は正典順序キー ``(created_at, rowid)`` で表す (W8 S7 と同じ物差し)。
    上端の**メッセージ自身は温かい窓の中**なので、編纂してよいのは
    「このキーより厳密に古い」メッセージだけ。
    """

    message_id: str
    created_at: int
    rowid: int
    #: どの (persona, model) 行が上端を決めたか (ログ用)。
    model_key: str

    @property
    def position(self) -> Tuple[int, int]:
        return (self.created_at, self.rowid)


def resolve_compile_ceiling(
    lifecycle, persona_id: Optional[str], conn,
) -> Optional[CompileCeiling]:
    """persona の「編纂してよい上端」を返す。温かい行が無ければ None (全域編纂可)。

    温度判定は ``SessionLifecycle._anchor_entry_is_hot`` の一枚だけを使う
    (arasuji_levels.md §14-6-3 — 判定式を二枚にしない)。

    温かい行の anchor が messages に存在しない場合、その行の窓は提示を
    定義できない壊れた状態なので、上端の材料から外して WARNING を残す
    (壊れた行のために補修全体を止めない)。
    """
    if not persona_id or conn is None:
        return None
    from sai_memory.memory.storage import get_message_position

    entries: Dict[str, Any] = lifecycle.load_anchor_entries(persona_id) or {}
    best: Optional[CompileCeiling] = None
    for model_key, entry in entries.items():
        anchor_id = entry.get("anchor_id")
        if not anchor_id:
            continue
        if not lifecycle._anchor_entry_is_hot(entry, str(model_key), persona_id):
            continue
        pos = get_message_position(conn, str(anchor_id))
        if pos is None:
            LOGGER.warning(
                "[coverage-repair] warm anchor points at a missing message; "
                "the row cannot define a window and does not constrain the "
                "ceiling (persona=%s model=%s anchor=%s)",
                persona_id, model_key, anchor_id,
            )
            continue
        if best is None or pos < best.position:
            best = CompileCeiling(
                message_id=str(anchor_id),
                created_at=pos[0],
                rowid=pos[1],
                model_key=str(model_key),
            )
    return best


def coverage_marks_for_window(
    conn, anchor_id: str, existing_folds: List[FoldedRange],
) -> List[FoldedRange]:
    """anchor 以降の窓を覆う一次エントリを §15 の印 (presented_raw) にして返す。

    返すのは**追記分だけ** (既存の記録が持つエントリは重複させない)。印は
    提示を変えない — 範囲全体が窓に生きている限り生ログのまま提示され、
    head のあらすじ枠の除外名簿 (chronicle_entry_ids) としてだけ効く
    (sea/session_window.py apply_folds の presented_raw 分岐)。

    印にできるのは **source が全部窓の中に収まるエントリだけ**。anchor を
    跨ぐエントリに印を書くと、(a) 窓の外の source ぶんの体験が head からも
    提示からも消える (印は head 除外として効き続けるため)、(b) 窓内だけを
    範囲にしても apply_folds の「全員生存」条件が崩れて digest 提示に倒れ、
    冷えた窓の提示が黙って縮む — どちらも被覆の保存より悪い。跨ぐエントリは
    印を見送り、head との部分的な二重提示を許す (その窓の次の畳みが
    `_attach_chronicle_refs` で正規の圧縮区間にした時点で解消する)。
    """
    import sqlite3 as _sqlite3

    from sai_memory.arasuji.storage import get_entries_covering_messages
    from sai_memory.memory.storage import get_message_position

    pos = get_message_position(conn, str(anchor_id))
    if pos is None:
        return []
    cur = conn.execute(
        "SELECT id, COALESCE(created_at, 0), rowid FROM messages "
        "WHERE COALESCE(created_at, 0) > ? "
        "   OR (COALESCE(created_at, 0) = ? AND rowid >= ?)",
        (pos[0], pos[0], pos[1]),
    )
    window_key: Dict[str, Tuple[int, int]] = {
        str(row[0]): (int(row[1]), int(row[2])) for row in cur.fetchall()
    }
    if not window_key:
        return []

    claimed_entries = {
        str(eid) for f in existing_folds for eid in f.chronicle_entry_ids
    }
    claimed_messages = {str(mid) for f in existing_folds for mid in f.message_ids}

    window_ids = sorted(window_key, key=lambda mid: window_key[mid])
    entries_by_id: Dict[str, Any] = {}
    try:
        for i in range(0, len(window_ids), _COVERING_QUERY_CHUNK):
            chunk = window_ids[i:i + _COVERING_QUERY_CHUNK]
            for entry in get_entries_covering_messages(conn, chunk):
                entries_by_id.setdefault(str(entry.id), entry)
    except _sqlite3.OperationalError:
        # あらすじのテーブル自体が無い (Chronicle 実績ゼロの新規 persona) なら
        # 被覆も無い — 窓の誕生ごとに WARNING を出さず、空で返す。
        return []

    marks: List[FoldedRange] = []
    ordered_entries = sorted(
        entries_by_id.values(),
        key=lambda e: (e.start_time or 0, str(e.id)),
    )
    for entry in ordered_entries:
        if str(entry.id) in claimed_entries:
            continue
        sources = [str(s) for s in entry.source_ids]
        if not sources:
            continue
        if any(s not in window_key for s in sources):
            LOGGER.info(
                "[coverage-repair] entry %s straddles the window anchor; "
                "not marked (the next fold of this window will attach it "
                "as a regular folded range)", entry.id,
            )
            continue
        if any(s in claimed_messages for s in sources):
            LOGGER.info(
                "[coverage-repair] entry %s shares a source message with an "
                "existing folded range; not marked (one message must not "
                "belong to two ranges)", entry.id,
            )
            continue
        ordered = sorted(sources, key=lambda s: window_key[s])
        short_id = getattr(entry, "short_id", None)
        marks.append(FoldedRange(
            message_ids=ordered,
            start_at=window_key[ordered[0]][0] or None,
            end_at=window_key[ordered[-1]][0] or None,
            chronicle_entry_ids=[str(entry.id)],
            chronicle_short_ids=[int(short_id)] if short_id is not None else [],
            presented_raw=True,
        ))
        claimed_messages.update(ordered)
    return marks


def mark_covered_cold_windows(lifecycle, persona) -> int:
    """persona の冷えた anchor 行それぞれへ、窓を覆うエントリの印を追記する。

    被覆補修 (run_coverage_repair) の完了時に呼ぶ。冪等 — 既に同じ entry_id を
    持つ区間がある行には追記しない。温かい行は触らない (会話中の窓には触らない
    — §16-2)。書き込みは anchor 据え置きの CAS
    (``SessionLifecycle.write_folds_if_anchor_unchanged``) — 判定と書き込みの
    間に anchor が動いた行は棄却され、次回の補修が再計算する。

    Returns:
        印を書いた行の数。
    """
    from sea.session_window import deserialize_folds

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if not persona_id or adapter is None or not adapter.is_ready():
        return 0

    updated = 0
    entries: Dict[str, Any] = lifecycle.load_anchor_entries(persona_id) or {}
    for model_key, entry in entries.items():
        anchor_id = entry.get("anchor_id")
        if not anchor_id:
            continue
        if lifecycle._anchor_entry_is_hot(entry, str(model_key), persona_id):
            continue
        existing = deserialize_folds(entry.get("folded_ranges"))
        try:
            marks = coverage_marks_for_window(
                adapter.conn, str(anchor_id), existing,
            )
        except Exception:
            LOGGER.warning(
                "[coverage-repair] failed to compute coverage marks "
                "(persona=%s model=%s); the row keeps its current record",
                persona_id, model_key, exc_info=True,
            )
            continue
        if not marks:
            continue
        if lifecycle.write_folds_if_anchor_unchanged(
            persona_id, str(model_key), str(anchor_id), existing + marks,
        ):
            updated += 1
            LOGGER.info(
                "[coverage-repair] marked %d covered range(s) on a cold window "
                "(persona=%s model=%s anchor=%s)",
                len(marks), persona_id, model_key, anchor_id,
            )
    return updated
