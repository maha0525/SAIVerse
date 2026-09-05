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


class CeilingResolutionError(RuntimeError):
    """止め線を解決できなかった (行が読めない / 温かい行の anchor の位置が引けない)。

    「温かい行がゼロ = 制約なし (None)」とは区別する — 解決失敗を「上端なし」へ
    潰すと、全量計画が温かい提示窓の下を掘るリスクを黙って踏む。呼び出し側
    (generate_chronicle / cost-estimate API / repair worker) は fail-closed で
    止めること。
    """


@dataclass(frozen=True)
class CompileCeiling:
    """編纂してよい上端 = 温かい anchor のうち正典順で最古の位置。

    位置は正典順序キーの素材 ``(created_at, rowid)`` で表す (W8 S7 と同じ
    物差し。created_at NULL は「全ての実時刻より前」— 共有述語
    ``_canonical_before_clause`` の族と同じ意味論)。上端の**メッセージ自身は
    温かい窓の中**なので、編纂してよいのは「このキーより厳密に古い」
    メッセージだけ。
    """

    message_id: str
    created_at: Optional[int]
    rowid: int
    #: どの (persona, model) 行が上端を決めたか (ログ用)。
    model_key: str

    @property
    def position(self) -> Tuple[Optional[int], int]:
        return (self.created_at, self.rowid)


def current_model_key_of(persona) -> Optional[str]:
    """ペルソナの現在モデル (会話の主線が使う model)。

    導出は ``sea/runtime.py`` の非常畳みと同じ
    (``resolve_execution_context(persona, None, state={}).model_key``) — 要約して
    よい上限が守る窓は、次に話しかけたとき実際に使われる model の窓なので、
    同じ式で求める。``persona`` が None なら None (現在モデル不明の呼び出し)。
    persona があるのに導出できない (例外 / 空 / "unknown") ときは
    :class:`CeilingResolutionError` — 「分からない」を「上限なし = 全域編纂可」
    へ潰すと生きている会話窓を掘る (Codex 一巡目 #2、fail-closed)。
    """
    if persona is None:
        return None
    try:
        from sea.pulse_context import resolve_execution_context
        key = resolve_execution_context(persona, None, state={}).model_key
    except Exception as exc:
        raise CeilingResolutionError(
            f"current model resolution failed for persona "
            f"{getattr(persona, 'persona_id', '?')}: {exc}"
        ) from exc
    if not key or str(key) == "unknown":
        raise CeilingResolutionError(
            f"current model of persona {getattr(persona, 'persona_id', '?')} "
            "is unknown; the ceiling cannot be determined"
        )
    return str(key)


def resolve_compile_ceiling(
    lifecycle, persona_id: Optional[str], conn,
    *,
    persona=None,
    persist_advance: bool = False,
) -> Optional[CompileCeiling]:
    """persona の「要約してよい上限」(全量計画が掘ってよい上端) を返す。

    候補 = **温かい記録** ∪ **ペルソナの現在モデルの記録 (温度不問)**、現在
    モデルに記録が無ければ代わりに**現在モデルの残す量ぶんの位置** (編纂対象の
    保存行を新しい側から数えた位置)。候補のうち正典順で最古の位置が上限で、
    編纂してよいのはそれより厳密に古いメッセージだけ。温かい記録だけを候補に
    していた頃は、最後の返答から TTL (既定 20 分) 黙ってメンテナンスのボタンを
    押すと全行が冷えていて上限が無くなり、生きている会話窓を丸ごと一次あらすじ
    にした (docs/issues/window_floor_and_refill_redesign.md ①、2026-09-03 実害)。

    - 現在モデルは ``persona`` から :func:`current_model_key_of` で求める
      (runtime の非常畳みと同じ導出。導出できなければ fail-closed)。その記録の
      位置は ``lifecycle.resolve_metabolism_anchor(persona, model_key=現在モデル,
      persist_advance=...)`` を通した位置 (§14-2 の冷えた起点の前進を済ませた
      後の起点)。実走 (generate_chronicle) は ``persist_advance=True``、
      見積もりは False — 表示と実走が違う数を言わない。
    - 現在モデルの記録が無い場合の下限は他の候補と**並べて**比べる — 他 model
      の温かい記録が新しい側にあっても、現在モデルの残す量ぶんは要約しない
      (Codex 一巡目 #3)。水位も無ければ下限は立たない。
    - ``persona`` が None (現在モデルが分からない呼び出し) は温かい記録だけを
      候補にする (従来の挙動)。

    温度判定は ``SessionLifecycle._anchor_entry_is_hot`` の一枚だけを使う
    (arasuji_levels.md §14-6-3 — 判定式を二枚にしない)。

    戻りは三状態:

    - ``CompileCeiling`` — 上限が立った。
    - ``None`` — 候補ゼロ (制約なし = 全域編纂可)。
    - ``CeilingResolutionError`` 送出 — 解決失敗 (行の読み取り失敗 /
      候補の anchor が messages に無い / 位置照会の失敗 / 現在モデルの導出
      失敗)。呼び出し側は fail-closed で全量計画を止める。「読めなかった」を
      「制約なし」へ潰さない (Codex レビュー 2026-08-31)。
    """
    if not persona_id:
        return None
    if conn is None:
        raise CeilingResolutionError("memory.db connection is unavailable")
    from sai_memory.memory.storage import (
        canonical_position_key,
        get_message_position,
    )

    try:
        entries: Dict[str, Any] = lifecycle.load_anchor_entries_strict(persona_id)
    except Exception as exc:
        raise CeilingResolutionError(
            f"session_anchor rows unreadable for {persona_id}: {exc}"
        ) from exc

    def _at(anchor_id: str, model_key: str) -> CompileCeiling:
        try:
            pos = get_message_position(conn, anchor_id)
        except Exception as exc:
            raise CeilingResolutionError(
                f"position lookup failed for anchor {anchor_id} "
                f"(model={model_key}): {exc}"
            ) from exc
        if pos is None:
            # 窓があるのにその位置が分からない — 上限を確定できないので
            # 全域編纂可へは倒さない (窓の下を掘る側の失敗が重い)。
            raise CeilingResolutionError(
                f"anchor {anchor_id} (model={model_key}) is missing "
                "from messages; the ceiling cannot be determined"
            )
        return CompileCeiling(
            message_id=anchor_id, created_at=pos[0], rowid=pos[1],
            model_key=model_key,
        )

    candidates: List[CompileCeiling] = []
    for model_key, entry in entries.items():
        anchor_id = entry.get("anchor_id")
        if not anchor_id:
            continue
        if not lifecycle._anchor_entry_is_hot(entry, str(model_key), persona_id):
            continue
        candidates.append(_at(str(anchor_id), str(model_key)))

    current_model = current_model_key_of(persona)
    current_entry = entries.get(current_model) if current_model else None
    if current_model and current_entry and current_entry.get("anchor_id"):
        # 現在モデルの**記録** (session_anchor 行) は温度不問で候補。位置は
        # 冷えた起点の前進を済ませた後の起点。
        try:
            # strict: 行の読み失敗を「行なし → 候補なし → 上限なし」に潰さない
            # (Codex 七巡目 #1)。例外は下で CeilingResolutionError に包む。
            current_anchor, _resolution = lifecycle.resolve_metabolism_anchor(
                persona, model_key=current_model, persist_advance=persist_advance,
                strict=True,
            )
        except Exception as exc:
            # 現在モデルの起点が分からないまま全量を編纂すると、生きている
            # 会話窓を掘る — fail-closed (呼び出し側が全量計画を止める)。
            raise CeilingResolutionError(
                f"current-model anchor resolution failed (model={current_model}): "
                f"{exc}"
            ) from exc
        if current_anchor:
            candidates.append(_at(str(current_anchor), current_model))
    elif current_model:
        # 記録が無い model (この model で一度も話していない) の起点は resolve が
        # 最前線 / 借用から仮に導くだけで記録ではない — 代わりに残す量ぶんの
        # 下限を候補にする。
        watermarks = lifecycle.get_metabolism_watermarks(persona, current_model)
        if watermarks is not None:
            floor = _target_floor_ceiling(conn, int(watermarks.target), current_model)
            if floor is not None:
                candidates.append(floor)

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda c: canonical_position_key(c.created_at, c.rowid),
    )


def _target_floor_ceiling(
    conn, target_chars: int, model_key: str,
) -> Optional[CompileCeiling]:
    """編纂対象の保存行を新しい側から残す量ぶん数え、そこを上限にする。

    記録 (session_anchor 行) が一つも無いペルソナ (ログインポート直後など) の
    下限。届いた行が上限 = その行より厳密に古いものだけ編纂してよい。全体が
    残す量に満たなければ最古の行が上限 (= 何も編纂しない)。編纂対象が無ければ
    None。
    """
    from sai_memory.memory.storage import (
        get_message_position,
        get_messages_for_chronicle,
    )

    messages = get_messages_for_chronicle(conn)
    if not messages:
        return None
    acc = 0
    boundary = 0
    for index in range(len(messages) - 1, -1, -1):
        acc += len(str(messages[index].content or ""))
        if acc >= target_chars:
            boundary = index
            break
    ceiling_msg = messages[boundary]
    try:
        pos = get_message_position(conn, str(ceiling_msg.id))
    except Exception as exc:
        raise CeilingResolutionError(
            f"position lookup failed for the target-floor message "
            f"{ceiling_msg.id}: {exc}"
        ) from exc
    if pos is None:
        raise CeilingResolutionError(
            f"target-floor message {ceiling_msg.id} is missing from messages"
        )
    return CompileCeiling(
        message_id=str(ceiling_msg.id), created_at=pos[0], rowid=pos[1],
        model_key=model_key,
    )


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
    from sai_memory.memory.storage import (
        _canonical_after_clause,
        canonical_position_key,
        get_message_position,
    )

    pos = get_message_position(conn, str(anchor_id))
    if pos is None:
        return []
    # 窓 = anchor 以降 (anchor 含む)。包含判定は共有述語 (_canonical_after_clause、
    # NULL created_at は全ての実時刻より前) — 窓の読み出しと同じ正典順序。
    after_sql, after_params = _canonical_after_clause(
        pos[0], pos[1], inclusive=True,
    )
    cur = conn.execute(
        f"SELECT id, created_at, rowid FROM messages WHERE {after_sql}",
        after_params,
    )
    # 値は (正典ソートキー, 生の created_at)。
    window_key: Dict[str, Tuple[Any, Optional[int]]] = {
        str(row[0]): (
            canonical_position_key(
                int(row[1]) if row[1] is not None else None, int(row[2]),
            ),
            int(row[1]) if row[1] is not None else None,
        )
        for row in cur.fetchall()
    }
    if not window_key:
        return []

    claimed_entries = {
        str(eid) for f in existing_folds for eid in f.chronicle_entry_ids
    }
    claimed_messages = {str(mid) for f in existing_folds for mid in f.message_ids}

    window_ids = sorted(window_key, key=lambda mid: window_key[mid][0])
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
        ordered = sorted(sources, key=lambda s: window_key[s][0])
        short_id = getattr(entry, "short_id", None)
        marks.append(FoldedRange(
            message_ids=ordered,
            start_at=window_key[ordered[0]][1],
            end_at=window_key[ordered[-1]][1],
            chronicle_entry_ids=[str(entry.id)],
            chronicle_short_ids=[int(short_id)] if short_id is not None else [],
            presented_raw=True,
        ))
        claimed_messages.update(ordered)
    return marks


def mark_covered_cold_windows(lifecycle, persona) -> Tuple[int, int]:
    """persona の冷えた anchor 行それぞれへ、窓を覆うエントリの印を追記する。

    被覆補修 (run_coverage_repair) の完了時に呼ぶ。冪等 — 既に同じ entry_id を
    持つ区間がある行には追記しない。温かい行は触らない (会話中の窓には触らない
    — §16-2)。書き込みは anchor 据え置きの CAS
    (``SessionLifecycle.write_folds_if_anchor_unchanged``) — 判定と書き込みの
    間に anchor が動いた行は棄却され、次回の補修が再計算する。

    Returns:
        ``(印を書いた行の数, 書けなかった行の数)``。書けなかった = 印の計算が
        例外を出した行と、書き込みが適用されなかった行 (CAS 棄却 / DB 失敗)。
        温かくてスキップした行と、追記すべき印が無かった行は数えない。
        失敗した行は次回の補修が現況から冪等に再計算する — 呼び出し側は
        失敗数をユーザーへ可視化するだけでよい (Codex レビュー 2026-08-31)。
    """
    from sea.session_window import deserialize_folds

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if not persona_id or adapter is None or not adapter.is_ready():
        return (0, 0)

    updated = 0
    failed = 0
    try:
        # 厳格に読む — 読み失敗を「行なし = 印なし (0, 0)」の成功の顔にしない
        # (Codex 八巡目 #4)。失敗は印の失敗数として補修の結果に載る。
        entries: Dict[str, Any] = lifecycle.load_anchor_entries_strict(persona_id) or {}
    except Exception:
        LOGGER.warning(
            "[coverage-repair] session_anchor rows are unreadable (persona=%s); "
            "no cold-window marks were written", persona_id, exc_info=True,
        )
        return (0, 1)
    for model_key, entry in entries.items():
        anchor_id = entry.get("anchor_id")
        if not anchor_id:
            continue
        if lifecycle._anchor_entry_is_hot(entry, str(model_key), persona_id):
            continue
        try:
            # 厳格に読む — 壊れた記録を空と見なして印を足すと、書き戻しで既存の
            # 区間が消える (七巡目の掃討)。読めない行は失敗に数えて飛ばす。
            existing = deserialize_folds(entry.get("folded_ranges"), strict=True)
        except ValueError:
            failed += 1
            LOGGER.warning(
                "[coverage-repair] folded ranges of persona=%s model=%s are "
                "unreadable; leaving the row untouched", persona_id, model_key,
                exc_info=True,
            )
            continue
        try:
            marks = coverage_marks_for_window(
                adapter.conn, str(anchor_id), existing,
            )
        except Exception:
            failed += 1
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
        else:
            failed += 1
    return (updated, failed)


# ---------------------------------------------------------------------------
# 末尾の未被覆 run の anchor 引き戻し (arasuji_tiny_run_absorption 裁定 5 改訂)
# ---------------------------------------------------------------------------

#: 引き戻し後の畳み見込みのフォールバック U (環境変数の解決に失敗したとき用)。
_FALLBACK_BAND_BUDGET = 10_000


@dataclass(frozen=True)
class TailRewindPlan:
    """末尾の未被覆 run を提示窓へ戻す引き戻し 1 回分の計画。

    見積もり (cost-estimate) と実行 (run_coverage_repair) が**この同じ計画
    関数** (:func:`plan_tail_rewind`) を通る — 畳みが起きるかの判定
    (``fold_needed``) と、その LLM コール見込みを二枚にしない
    (§16-2「表示と実走が違う数を言ってはならない」)。
    """

    #: 引き戻す行 (境界を作っている行 = 最古の温かい anchor の行。温かい行が
    #: 無ければ全行のうち最古の anchor の行 — キャッシュが無いので無料)。
    model_key: str
    #: CAS 用 — 行の現在の anchor (計画と書き込みの間に動いたら棄却)。
    expected_anchor_id: str
    #: 引き戻し先 = 帯の正典順最古のメッセージ。
    new_anchor_id: str
    #: 引き戻し後に**実際に送られる**字数 (保存行 + 送信直前に差し込まれる
    #: 知覚ブロック。2026-09-02 裁定 — あらすじの材料字数ではなく、生の提示量)。
    window_chars_after: int
    #: 引き戻し後に窓が上限 (high) を超える (合計 > high)。
    fold_needed: bool
    #: 引き戻し後の窓の**会話の行だけ**の字数 (残す量の主語 — 2026-09-03
    #: 裁定)。``fold_needed`` でもこれが残す量以下なら退場計画は保護範囲で
    #: 埋まって空 = 畳めるものが無い (超過の主は知覚の供給)。
    window_rows_chars_after: int = 0
    #: 残す量 (watermarks.target)。水位が引けなければ None。
    target_chars: Optional[int] = None
    #: 畳みの LLM コール見込み (fold_needed 時のみ > 0。概算 — 退場範囲の
    #: 材料字数 / U の切り上げ。束ねの連鎖は数えない)。
    est_fold_calls: int = 0
    #: 畳みの材料字数見込み (費用概算の入力字数用)。
    est_fold_material_chars: int = 0
    #: 引き戻す行の圧縮区間記録 (計画時点の生 payload)。実行はこれを持ち越す —
    #: 書き込み直前の行再読みは「読めない」を「fold 無し」と同一視して既存
    #: 記録を空で上書きしうる (Codex 七巡 K2 — データ喪失級)。
    folded_ranges_payload: Optional[str] = None

    @property
    def fold_evictable(self) -> bool:
        """上限超え (``fold_needed``) かつ会話の行が残す量を超えている =
        畳みが実際に何かを退場させられる。行が残す量以下なら、畳みは
        走らせても空振り (run_manual_compaction が門で "noop")。"""
        if not self.fold_needed:
            return False
        if self.target_chars is None:
            return True
        return self.window_rows_chars_after > self.target_chars


def _anchor_positions(
    lifecycle, persona_id: str, conn, entries: Dict[str, Any],
) -> Dict[str, Tuple[str, Tuple]]:
    """{model_key: (anchor_id, 正典順キー)}。位置を引けない行は含めない。"""
    from sai_memory.memory.storage import (
        canonical_position_key,
        get_message_position,
    )

    out: Dict[str, Tuple[str, Tuple]] = {}
    for model_key, entry in entries.items():
        anchor_id = entry.get("anchor_id")
        if not anchor_id:
            continue
        # 位置照会の例外は伝播させる (Codex 九巡 M2) — 接続レベルの失敗を
        # 「この行は位置が引けない」へ潰すと、境界行の選定や「帯は既に窓の中」
        # 判定が欠けた集合で下される。anchor が messages に無いだけの行
        # (pos None) は行単位の欠落として従来どおり外す。
        pos = get_message_position(conn, str(anchor_id))
        if pos is None:
            continue
        out[str(model_key)] = (
            str(anchor_id), canonical_position_key(pos[0], pos[1]),
        )
    return out


def plan_tail_rewind(
    lifecycle, persona, conn, first_message_id: str,
) -> Optional["TailRewindPlan"]:
    """帯 (末尾の未被覆 run 群) への anchor 引き戻しを計画する (読みだけ)。

    None を返すのは:

    - anchor 行が一つも無い (初会話の bootstrap §16-3 で自然治癒 — 機構を
      作らない。INFO のみ)
    - 帯が既にいずれかの窓の中 (どれかの行の anchor が帯の最古以下) —
      §16-1 は満たされていて、引き戻すものが無い
    - 帯の最古の位置が引けない / どの行の anchor 位置も引けない
    """
    from sai_memory.arasuji.generator import material_len
    from sai_memory.memory.storage import (
        canonical_position_key,
        get_message_position,
    )

    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        return None
    # strict 読み (Codex 九巡 M2): 「行なし (bootstrap に任せる)」と「読めな
    # かった (行があるかも分からない)」を区別する。読み取り失敗は例外のまま
    # 伝播 — 実行側 (run_tail_rewind) は failed、見積もり側は 500 に着地する。
    entries: Dict[str, Any] = lifecycle.load_anchor_entries_strict(persona_id) or {}
    if not entries:
        LOGGER.info(
            "[tail-rewind] persona %s has no anchor rows; nothing to rewind "
            "(the first conversation's bootstrap (§16-3) heals this shape)",
            persona_id,
        )
        return None
    zone_pos = get_message_position(conn, str(first_message_id))
    if zone_pos is None:
        LOGGER.warning(
            "[tail-rewind] zone head %s has no position; skipping",
            first_message_id,
        )
        return None
    zone_key = canonical_position_key(zone_pos[0], zone_pos[1])

    positions = _anchor_positions(lifecycle, persona_id, conn, entries)
    if not positions:
        LOGGER.warning(
            "[tail-rewind] no anchor row has a resolvable position "
            "(persona=%s); skipping", persona_id,
        )
        return None
    # 帯が既にどれかの窓の中 (窓 = anchor 以降の末尾全部) なら §16-1 は
    # 満たされている — 引き戻し不要。anchor を新しい側へ動かす形は作らない。
    if any(key <= zone_key for _aid, key in positions.values()):
        LOGGER.info(
            "[tail-rewind] the uncovered tail is already inside a window "
            "(persona=%s); no rewind needed", persona_id,
        )
        return None

    # 境界を作っている行: 最古の温かい anchor の行。温かい行が無ければ
    # 全行のうち最古の anchor の行 (冷えた行 — 引き戻しは完全無料)。
    warm = {
        mk: v for mk, v in positions.items()
        if lifecycle._anchor_entry_is_hot(entries[mk], mk, persona_id)
    }
    pool = warm or positions
    model_key = min(pool, key=lambda mk: pool[mk][1])
    expected_anchor_id = pool[model_key][0]

    # 引き戻し後の窓を実際に組んで水位を測る。物差しは「実際に送る中身」=
    # 保存行 + 送信直前に差し込まれる知覚ブロック (2026-09-02 まはー裁定。issue
    # context_accounting_excludes_injected_rows.md)。組成規則は lifecycle 経由で
    # 一点管理のものを呼ぶ (ここに二枚目を書かない)。保存行だけで測ると、引き
    # 戻した窓の実送信が上限を超えていても fold_needed=False になり、補修が
    # 「上限超えを自分のジョブの中で畳む」約束 (費用の透明性) を破って、次の
    # 会話の非常畳みへ黙って送ることになる。
    # 起点・記録を厳格に読んだ後の窓も厳格に (Codex 八巡目 #3) — 履歴の読み
    # 失敗は例外で run_tail_rewind が "failed" に写す (書かない)。
    window_after = lifecycle.get_presented_window(
        persona, model_key, str(first_message_id), strict=True,
    )
    # 「引き戻したら、の窓」= 仮定の列なので、知覚の下ろし境界は進めない
    # (advance_cutoff=False)。引き戻しを見送る回もここを通るし、実際に送られる
    # のは引き戻し後の Pulse の組み立てで、そちらが同じ判定で境界を進める
    # (2026-09-05 四巡目 #6)。
    presented_after = lifecycle.presented_with_perceptions(
        persona, window_after.presented, str(first_message_id),
        model_key=model_key, advance_cutoff=False,
    )
    from sea.eviction_plan import message_chars, stored_message_chars
    chars_after = message_chars(presented_after)
    # 残す量と比べる量は会話の行だけ (2026-09-03 裁定。機構名義の行も
    # 数えない — 2026-09-04 裁定)。
    rows_after = stored_message_chars(presented_after)
    watermarks = lifecycle.get_metabolism_watermarks(persona, model_key)
    high = getattr(watermarks, "high", None) if watermarks is not None else None
    target = (
        getattr(watermarks, "target", None) if watermarks is not None else None
    )
    fold_needed = high is not None and chars_after > high

    est_calls = 0
    est_material = 0
    if fold_needed and target is not None:
        # 畳み見込み: 既存の範囲規則「残す量より古い側」を落とすとして、
        # 退場候補の材料字数を U で割った切り上げ (束ねの連鎖は数えない概算)。
        try:
            from sai_memory.arasuji.alignment import chronicle_band_budget
            budget = chronicle_band_budget()
        except Exception:
            budget = _FALLBACK_BAND_BUDGET
        # 走査する列は本走行 (plan_eviction) と同じマージ済みの列。「残す量に
        # 届くまでにどこまで退場するか」の境目は**会話の行だけ**で決まる
        # (2026-09-03 まはー裁定: 残す量の主語は会話の行。上限 = fold_needed の
        # 判定は合計のまま。機構名義の行も数えない — 2026-09-04 裁定)。知覚
        # ブロックと機構名義の行は残す量を消費しないので remaining を
        # 減らさないが、退場する範囲に挟まったものは付記・fold で編纂に入るため
        # 材料には寄与する (機構名義の一行へ縮む material_len — 本走行の
        # material_message_chars と同じ扱い)。
        # NOTE: 本走行の削減母集合 (_reduction_basis) は fold の先頭・末尾の
        # 隙間ブロックを除くが、この概算はそこまで写さない — 誤差は縮約後の
        # 一行ぶん × 数個で、方向は費用を多めに見せる安全側 (概算の器の内。
        # Codex 指摘 2026-09-02 四巡目、却下の記録は issue)。
        from sea.eviction_plan import is_injected_perception, is_mechanism_row
        remaining = rows_after
        for msg in presented_after:
            if remaining <= target:
                break
            content = str(msg.get("content") or "")
            if not is_injected_perception(msg) and not is_mechanism_row(msg):
                remaining -= len(content)
            meta = msg.get("metadata")
            tags = meta.get("tags") if isinstance(meta, dict) else None
            est_material += material_len(
                content, tags if isinstance(tags, (list, tuple)) else (),
            )
        # 材料ゼロ (退場候補に編纂対象が無い等) なら畳みの LLM は走らない —
        # 見込み 1 を置くと実走 0 と食い違う (ローカルレビュー 2026-08-31 L5)。
        est_calls = -(-est_material // budget) if est_material else 0

    return TailRewindPlan(
        model_key=model_key,
        expected_anchor_id=expected_anchor_id,
        new_anchor_id=str(first_message_id),
        window_chars_after=chars_after,
        fold_needed=fold_needed,
        window_rows_chars_after=rows_after,
        target_chars=target,
        est_fold_calls=est_calls,
        est_fold_material_chars=est_material,
        # 圧縮区間の記録は計画時点で持ち越す (K2) — 書き込み直前に行を読み
        # 直さない。CAS (expected_anchor_id) が「計画から書き込みまでに行が
        # 動いていない」を保証するので、この payload はその時点の正本。
        folded_ranges_payload=(entries.get(model_key) or {}).get("folded_ranges"),
    )


def estimate_tail_rewind_fold(
    lifecycle, persona, conn, first_message_id: str,
) -> Tuple[int, int]:
    """cost-estimate 用: 引き戻しに伴う即時畳みの (LLM コール, 材料字数) 見込み。

    引き戻し自体は 0 LLM コール。畳みが要らない / 引き戻し自体が起きない
    計画なら (0, 0)。判定は実行と同じ :func:`plan_tail_rewind` を通す。

    計画の例外は**伝播させる** (Codex 四巡 G2 — 「表示 ≥ 実走」。0 で
    ごまかすと引き戻し後の即時畳みが表示なしで課金される)。cost-estimate
    エンドポイントはこの例外で 500 に止まる。実行側 (run_tail_rewind) の
    「解決失敗 → skip」は少なく走る方向なのでそのまま。
    """
    plan = plan_tail_rewind(lifecycle, persona, conn, first_message_id)
    if plan is None or not plan.fold_needed:
        return (0, 0)
    return (plan.est_fold_calls, plan.est_fold_material_chars)


def _resolve_uncovered_tail(lifecycle, persona, conn) -> Optional[str]:
    """帯の最古メッセージ id を現況から引き直す (実行直前の再計算)。

    generate_chronicle と同じ一点管理の関数列 (止め線 → 全量計画 →
    極小分割 → 帯検出) を読みだけで通す。帯が無ければ None。
    """
    from sai_memory.arasuji.absorption import (
        split_plan_for_absorption,
        uncovered_tail_zone,
    )
    from sai_memory.arasuji.alignment import (
        chronicle_band_budget,
        plan_alignment,
    )
    from sai_memory.memory.storage import (
        clip_messages_before_position,
        get_messages_for_chronicle,
    )

    persona_id = getattr(persona, "persona_id", None)
    # 読みだけの再計算なので前進は永続化しない (実走の generate_chronicle が
    # 直前に persist_advance=True で済ませている)。
    ceiling = resolve_compile_ceiling(
        lifecycle, persona_id, conn, persona=persona, persist_advance=False,
    )
    messages = get_messages_for_chronicle(conn)
    if ceiling is not None:
        messages = clip_messages_before_position(
            conn, messages, ceiling.created_at, ceiling.rowid,
        )
    cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) WHERE level = 1"
    )
    processed_ids = {row[0] for row in cur.fetchall()}
    plan = plan_alignment(
        messages, processed_ids, target_chars=chronicle_band_budget(),
    )
    _normal, tiny = split_plan_for_absorption(
        plan, target_chars=chronicle_band_budget(),
    )
    _ids, first_id, _idx = uncovered_tail_zone(tiny, messages)
    return first_id


def run_tail_rewind(
    lifecycle, persona,
    event_callback=None,
    cancellation_token=None,
) -> str:
    """帯への anchor 引き戻し + 必要なら即時畳み (補修ジョブの一部)。

    run_coverage_repair が編纂 (generate_chronicle) の後に呼ぶ。引き戻しは
    LLM ゼロの帳簿操作。引き戻し後の窓が上限 (high) を超えていたら、**この
    ジョブの中で**通常の畳み (run_manual_compaction — 範囲規則は「残す量より
    古い側」) を即座に走らせる — 次の会話の非常畳み (§14-3) へ黙って送らない
    (費用の透明性。見込みは実行前の cost-estimate に含まれている)。

    書き込みは §15 読み戻しと同じ ``_write_refill`` (CAS + 圧縮区間同一
    コミット) を再利用する — ただし ``raise_on_error=True`` で呼び、CAS 不一致
    (見送り) と DB 失敗 (再実行が要る) を分ける。通常の §15 refill 経路の
    梯子の安全規則 (未被覆を跨がない) はここでは適用されない — あの規則の目的は「機構が勝手に過去を
    復活させない」で、補修はユーザーの明示操作 (裁定 5 改訂)。refill 側の
    コード (sea/window_refill.py) には一切触れていない — 通常経路の規則は
    そのまま。

    Returns (ログ・テスト用の状態語):
        "none" (帯なし — 帯の解決が成功して「帯は本当に無い」と分かった) /
        "skipped" (計画なし — 行なし・既に窓の中・位置不明。意図した見送り) /
        "failed" (帯の解決失敗、計画の読み取り失敗 — strict anchor 読み・位置
        照会・DB 読みの例外 —、または**書き込み自体の失敗** (DB 例外)。呼び出し
        側は status="failed" に写像し、再実行が引き戻しだけやり直す。
        Codex 九巡 M2 / 十巡 N1 / 十一巡 P1) /
        "cas_rejected" (anchor が動いた — 次回再計画。意図した見送り。書き込みが
        生きていて CAS だけが不一致だった場合に限る) /
        "rewound" (引き戻しのみ — 畳みが不要、または合計は上限超えでも会話の
        行が残す量以下で畳めるものが無い (知覚の供給が予算超過。2026-09-03
        裁定)、または畳みを呼んだが門で "noop" だった) /
        "rewound_folded" (引き戻し + 畳み完了 = 実際に畳んだ) /
        "rewound_fold_failed" (引き戻しは完了したが窓の畳みが未完 — 呼び出し側は
        status="failed" に写像する。Codex 十巡 N2)
    """
    from sea.session_window import deserialize_folds

    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if not persona_id or adapter is None or not adapter.is_ready():
        return "skipped"
    try:
        first_id = _resolve_uncovered_tail(lifecycle, persona, adapter.conn)
    except Exception:
        # 帯の解決失敗 (strict anchor 読み・位置照会・DB 読みの例外) は
        # 「帯が本当に無い (None)」と区別して失敗として上げる (Codex 十巡 N1)。
        # "skipped" へ潰すと、引き戻すべき末尾が残ったまま補修が成功の顔で
        # 終わり、再実行が必要なことが誰にも分からなくなる (裁定 6)。
        LOGGER.warning(
            "[tail-rewind] zone resolution failed (persona=%s); the tail was "
            "not rewound — re-run the repair to retry the rewind",
            persona_id, exc_info=True,
        )
        return "failed"
    if first_id is None:
        return "none"
    try:
        plan = plan_tail_rewind(lifecycle, persona, adapter.conn, first_id)
    except Exception:
        # strict 読みの失敗 (Codex 九巡 M2) — 「行なし」へ潰さず失敗として
        # 上げる。編纂自体は確定済みなので、再実行が引き戻しだけやり直す
        # (冪等)。呼び出し側 (run_coverage_repair) は status="failed" に写像。
        LOGGER.warning(
            "[tail-rewind] planning failed (persona=%s)",
            persona_id, exc_info=True,
        )
        return "failed"
    if plan is None:
        return "skipped"

    # 既存の圧縮区間はそのまま持ち越す (引き戻しで無効になる区間は無い —
    # 窓が古い側へ広がるだけ)。記録は**計画時点の payload** (plan に同梱) を
    # 使う — 書き込み直前の行再読みは「読めない」を「fold 無し」と同一視して
    # 既存記録を空で上書きしうる (Codex 七巡 K2 — データ喪失級)。anchor と
    # 同一コミット・CAS は _write_refill。
    #
    # 受容している競合 (Codex レビュー 2026-08-31 #6、まはー裁定): CAS は
    # anchor だけを見るため、計画〜書き込みの ms 級の窓で Beat ロック外の
    # 経路 (UI のエントリ削除 → remove_folds_referencing_entry) が
    # folded_ranges を書き換えていた場合、その変更を計画時の値で上書きしうる。
    # regenerate_entry と同じ「手動 UI 操作同士の ms 級競合」として受容する
    # (docs/issues/chronicle_eviction_applier_veto_deadlock.md に記録の型)。
    # 上書きされた側は _drop_dead_folds / 次の畳みの掃除が拾う。
    try:
        # 厳格に読む — 壊れた payload を空と見なして書くと既存の区間が消える
        # (Codex 七巡目 #3)。読めなければ書かずに failed。
        existing_folds = deserialize_folds(plan.folded_ranges_payload, strict=True)
    except ValueError:
        LOGGER.warning(
            "[tail-rewind] folded ranges payload is unreadable (persona=%s "
            "model=%s); the tail was not rewound and the record was left as is",
            persona_id, plan.model_key, exc_info=True,
        )
        return "failed"
    # 書き込みは raise_on_error=True で呼ぶ (Codex 十一巡 P1): _write_refill の
    # False は既定では「CAS 不一致」と「DB 失敗」の両方を意味するので、そのまま
    # "cas_rejected" (成功系の白名簿) へ写すと、書けなかった末尾が残ったまま
    # 補修が成功の顔で終わる (裁定 6 に反する)。例外側だけ "failed" へ倒す。
    # §15 通常経路 (maybe_run_window_refill) は既定のまま = 挙動据え置き。
    try:
        written = lifecycle._write_refill(
            persona_id, plan.model_key, plan.expected_anchor_id,
            plan.new_anchor_id, existing_folds, raise_on_error=True,
        )
    except Exception:
        LOGGER.warning(
            "[tail-rewind] anchor rewind write failed (persona=%s model=%s "
            "%s -> %s); the tail was not rewound — re-run the repair to retry "
            "the rewind",
            persona_id, plan.model_key, plan.expected_anchor_id,
            plan.new_anchor_id, exc_info=True,
        )
        return "failed"
    if not written:
        return "cas_rejected"
    LOGGER.info(
        "[tail-rewind] anchor rewound (persona=%s model=%s %s -> %s); the "
        "uncovered tail is now inside the window (%d chars presented)",
        persona_id, plan.model_key, plan.expected_anchor_id,
        plan.new_anchor_id, plan.window_chars_after,
    )
    if event_callback:
        try:
            event_callback({
                "type": "metabolism",
                "status": "running",
                "content": "あらすじにできない少量の末尾を、提示窓へ戻しました。",
            })
        except Exception:
            pass

    if not plan.fold_needed:
        return "rewound"
    if not plan.fold_evictable:
        # 合計は上限超えでも会話の行が残す量以下 — 退場計画は保護範囲で埋まって
        # 空になる (残す量の主語は会話の行、2026-09-03 裁定)。畳みを呼んでも門で
        # "noop" に終わるだけなので、「古い側を畳んでいます」と言わず、畳んだ
        # 顔 ("rewound_folded") もしない。超過の主は知覚の供給 — 本走行の
        # 発火側が同じ判定で警告する (SessionLifecycle._note_perception_over_budget)。
        LOGGER.info(
            "[tail-rewind] window over the high watermark (%d chars sent) but "
            "the conversation rows (%d chars) are within the target (%s); "
            "nothing evictable — the perception supply, not the conversation, "
            "is over budget. Skipping the post-rewind fold (persona=%s)",
            plan.window_chars_after, plan.window_rows_chars_after,
            plan.target_chars, persona_id,
        )
        return "rewound"

    # 引き戻しで窓が上限を超えた — このジョブの中で即座に畳む (裁定 5 改訂の
    # 細部 2 点目)。範囲規則・claim・スルースは run_manual_compaction 経由で
    # 既存のまま。Beat ロックは同一スレッド再入 (RLock) で無害。
    if event_callback:
        try:
            event_callback({
                "type": "metabolism",
                "status": "running",
                "content": "戻した窓が上限を超えたため、古い側を畳んでいます...",
            })
        except Exception:
            pass
    try:
        fold_status = lifecycle.run_manual_compaction(
            persona, event_callback, model_key=plan.model_key,
            cancellation_token=cancellation_token,
        )
    except Exception:
        LOGGER.warning(
            "[tail-rewind] post-rewind compaction raised (persona=%s); the "
            "rewind itself is committed (the tail is inside the window) but "
            "the window stays over the high watermark — the emergency "
            "pre-compaction of the next conversation (§14-3) recovers it. "
            "Re-running the repair does NOT retry this fold (the tail is no "
            "longer uncovered)",
            persona_id, exc_info=True,
        )
        return "rewound_fold_failed"
    if fold_status == "ok":
        return "rewound_folded"
    if fold_status == "noop":
        # 門で「畳むものが無い」— 畳んではいないので "rewound_folded" (畳み完了)
        # とは言わない。引き戻し自体は完了しており成功系 (_rewind_ok) のまま。
        LOGGER.info(
            "[tail-rewind] post-rewind compaction found nothing to fold "
            "(persona=%s); the rewind is committed", persona_id,
        )
        return "rewound"
    LOGGER.warning(
        "[tail-rewind] post-rewind compaction did not complete "
        "(persona=%s status=%s); the rewind itself is committed (the tail is "
        "inside the window) but the window stays over the high watermark — "
        "the emergency pre-compaction of the next conversation (§14-3) "
        "recovers it. Re-running the repair does NOT retry this fold (the "
        "tail is no longer uncovered)",
        persona_id, fold_status,
    )
    return "rewound_fold_failed"
