"""極小 run の隣人吸収 (docs/issues/arasuji_tiny_run_absorption.md、2026-08-31 裁定).

歯抜け被覆 (編纂済みに挟まれた孤立メッセージ) が作る極小 run (材料 0.5U 未満)
に単独の LLM 編纂を走らせると、材料が薄すぎて参考文脈からの借用 = 捏造あらすじが
生まれる (裁定の実証記録は issue)。本モジュールはその機械化:

- **検出** (:func:`split_plan_for_absorption`): 整列計画 (plan_alignment) の
  チャンクから材料 0.5U 未満のもの = 極小 run を選り分ける。閾値は
  ``target_chars // 2`` で導出する (U はチャンク計画と同じ物差し — 裁定 2)。
- **計画** (:func:`plan_absorption`): 極小 run ごとに開く隣人 Lv1 を
  **後ろ (新しい側) → 居なければ同じスレッドの前側**の順で割り当て、同じ
  隣人に割り当たった run を一つの item に束ねる (機構 D、
  docs/intent/chronicle_coverage_gaps.md — 開き直しと再生成は隣人 1 個に
  つき必ず一回)。隣人自体が極小なら、合計材料が 0.5U に届くまでさらに
  後ろへ連鎖する。両方向とも隣人が居ない run は、発話 (実会話フィルタ) を
  含むなら通常の生成へ回して独立の一次あらすじにし
  (``standalone_chunks`` — 機構 E)、発話ゼロ (機構の記録だけ) なら自動では
  書かせず件数だけ数える (``silent_runs`` — 解消はユーザー操作に返す)。
- **末尾の端数は anchor 引き戻しの対象** (裁定 5 改訂 — 「見送り」の概念は
  廃止): 後ろに編纂済みが何も無い末尾の極小 run 群
  (:func:`uncovered_tail_zone`) は、境界の anchor 行を run の最古まで引き
  戻して提示窓の中へ戻す — 窓の中にあれば §16-1 の不変条件は満たされる
  (LLM 不要の帳簿操作)。引き戻しの実行は sea 層
  (sea/coverage_repair.run_tail_rewind — anchor 行は world DB にある) で、
  本モジュールは対象の検出だけを持つ。
- **実行** (:func:`run_absorption`): 隣人を開き直し (source の生ログに戻し)、
  run と合わせた連続範囲で新しい Lv1 を生成して差し替える。生成が先・削除が後
  (storage.regenerate_entry と同じ generate-then-swap) — LLM 失敗時は旧が無傷。
- **上位の連鎖再生成** (裁定 3・4): 差し替えた Lv1 の先祖 (Lv2+) は
  ``content_stale`` の印を親子帳簿の更新と同時に受け、「その上位の被覆範囲から
  時系列が抜けた時点」で本文を再生成する (:func:`regenerate_upper_entry` —
  id は変えず本文だけ更新するので、親子リンクは常に整合し、失敗しても
  「本文だけ古い」で止まる)。ジョブ末尾で未 flush の先祖を全部 flush する。
- **未完了の可視化** (裁定 6): 吸収の仕事が始まる前に arasuji_progress へ
  「repair_incomplete」の印を置き、全部 (吸収 + flush) が済んでから外す。
  途中で落ちれば印が残り、Chronicle タブの帯が「前回の処理が完了していません」
  を出す。再実行は processed_ids (吸収済みの run は新エントリの source) と
  ``content_stale`` の走査で続きから直る。

適用範囲は**再編纂経路のみ** (裁定 5): generate_chronicle の全量計画
(compile_groups=None = API repair モード) と build_arasuji スクリプト。
通常の Metabolism 畳み (退場範囲の編纂) には入れない。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set

from sai_memory.arasuji.alignment import AlignmentPlan, PlannedChunk
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    _get_chronicle_page_row,
    _parse_chronicle_meta,
    delete_entry_and_update_parent,
    get_entries_covering_messages,
    get_entry,
    is_missing_table_error,
)

LOGGER = logging.getLogger(__name__)

#: 単独編纂を止める閾値の分母 (X = U / 2 = 0.5U — 2026-08-31 まはー裁定 2)。
TINY_RUN_DIVISOR = 2


class AbsorptionError(RuntimeError):
    """吸収・連鎖再生成の失敗 (LLM 応答空・帳簿更新失敗など)。

    呼び出し側 (generate_chronicle / スクリプト) は status="failed" に写像する。
    確定済みの差し替えはそのまま残り、再実行は processed_ids と content_stale
    走査で続きから進む (冪等)。
    """


def tiny_run_threshold(target_chars: int) -> int:
    """極小 run の閾値 (材料字数)。ハードコードせず U から導出する。"""
    return max(1, int(target_chars) // TINY_RUN_DIVISOR)


def split_plan_for_absorption(
    plan: AlignmentPlan, *, target_chars: int,
) -> tuple:
    """整列計画を (通常チャンクの計画, 極小チャンク列) に分ける。

    _plan_run は run 末尾の端数を直前のチャンクへ吸収するため、U 未満で閉じた
    チャンクは「run 全体が U 未満で単独チャンクになったもの」だけ — つまり
    「チャンク材料 < 0.5U」⟺「run 材料 < 0.5U」が成り立つ (検出はチャンク粒度
    でよい)。返す計画の total_unprocessed は元のまま (件数表示の意味を変えない)。
    """
    threshold = tiny_run_threshold(target_chars)
    tiny: List[PlannedChunk] = []
    normal: List[PlannedChunk] = []
    for chunk in plan.chunks:
        (tiny if chunk.coverage_chars < threshold else normal).append(chunk)
    return (
        AlignmentPlan(chunks=normal, total_unprocessed=plan.total_unprocessed),
        tiny,
    )


@dataclass
class AbsorptionItem:
    """吸収 1 件 = 開く隣人 1 組 + そこへ合流する極小 run 群。

    機構 D (chronicle_coverage_gaps): 計画の単位は「開く隣人ごと」。前の穴から
    後ろの隣人として割り当たった run も、後ろの穴から前側の隣人として割り
    当たった run も、同じ item の材料に入る — 開き直しと再生成は隣人 1 個に
    つき必ず一回。
    """

    run_message_ids: List[str]
    absorbed_entry_ids: List[str]
    material_chars: int
    #: 時系列処理と flush 判定に使う、この範囲の先頭時刻。
    start_at: int = 0


@dataclass
class AbsorptionPlan:
    """吸収の計画 (決定論・LLM なし)。見積もりと実行が同じ数を共有する。"""

    items: List[AbsorptionItem] = field(default_factory=list)
    #: anchor 引き戻しの対象 (末尾の未被覆 run 群の message id、正典順)。
    #: 実行は sea 層 (run_tail_rewind) — LLM ゼロなので llm_calls に入らない。
    rewind_run_ids: List[str] = field(default_factory=list)
    #: 引き戻し先 (rewind_run_ids の正典順最古のメッセージ id)。
    rewind_first_message_id: Optional[str] = None
    #: 機構 E: 両方向とも吸収先が無く、発話 (実会話フィルタ) を含む run。
    #: 通常の生成 (参考文脈つき) で独立の一次あらすじにする — 呼び出し側が
    #: :func:`merge_standalone_chunks` で整列計画へ合流させる。llm_calls には
    #: 数えない (合流先の計画が数える — 二重計上しない)。
    standalone_chunks: List[Any] = field(default_factory=list)
    #: 機構 E: 両方向とも吸収先が無く、発話ゼロ (機構の記録だけ) の run の数。
    #: 自動では書かせない — 解消は第二段の修復 UI (ユーザー操作) に返す。
    silent_runs: int = 0
    #: 同、メッセージ数 (完了報告の「残り K 件」— 機構 G)。
    silent_message_count: int = 0
    #: 再生成が見込まれる上位 (吸収で汚れる先祖 + 既存の content_stale)。
    stale_upper_ids: List[str] = field(default_factory=list)

    @property
    def llm_calls(self) -> int:
        return len(self.items) + len(self.stale_upper_ids)

    @property
    def material_chars(self) -> int:
        return sum(i.material_chars for i in self.items)


def merge_standalone_chunks(
    plan: AlignmentPlan, absorption_plan, ordered_messages: Sequence,
) -> AlignmentPlan:
    """吸収できない発話あり run (機構 E) を通常の整列計画へ合流させる。

    executor は計画のチャンクを時系列順で処理する前提なので、合流後も先頭
    メッセージの**正典順** (created_at, rowid) で並べ直す。Message は rowid を
    持たないので、rowid の代わりに ``ordered_messages`` (plan_alignment に
    渡したのと同じ、正典順の編纂候補列) の位置で同一秒を分ける — created_at
    だけで並べると、同一秒のメッセージが大量に並ぶインポート環境 (本設計の
    主対象) で既存チャンクと standalone の順序が崩れ、実行順と付記範囲の計算
    (チャンク順前提) が時系列とずれる (Codex 二巡採用 3)。

    ``absorption_plan`` が None (吸収を見送った回) や standalone が空の回は
    計画をそのまま返す。
    """
    if absorption_plan is None or not absorption_plan.standalone_chunks:
        return plan
    position = {m.id: idx for idx, m in enumerate(ordered_messages)}

    def _key(chunk) -> tuple:
        if not chunk.messages:
            return (0, -1)
        first = chunk.messages[0]
        # 位置が引けない先頭 (通常は無い) は同一秒の末尾側へ — created_at が
        # 第一キーなので並びの大枠は保たれる。
        return (first.created_at or 0, position.get(first.id, len(position)))

    chunks = sorted(
        list(plan.chunks) + list(absorption_plan.standalone_chunks),
        key=_key,
    )
    return AlignmentPlan(chunks=chunks, total_unprocessed=plan.total_unprocessed)


def _load_messages(conn: sqlite3.Connection, message_ids: Sequence[str]):
    from sai_memory.memory.storage import get_messages_by_ids
    return get_messages_by_ids(conn, [str(m) for m in message_ids])


def list_stale_upper_ids(conn: sqlite3.Connection) -> List[str]:
    """content_stale の印を持つ上位エントリ id (level 昇順 → end_time 昇順)。

    前回の吸収ジョブが flush 前に落ちた場合の拾い直し口 — 親子リンクは差し替え
    時点で整合済みなので、残る仕事は本文の再生成だけ。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM memopedia_pages "
            "WHERE category = 'chronicle' "
            "AND json_extract(metadata, '$.content_stale') = 1 "
            "ORDER BY CAST(json_extract(metadata, '$.level') AS INTEGER) ASC, "
            "CAST(json_extract(metadata, '$.end_time') AS INTEGER) ASC",
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if is_missing_table_error(exc):
            return []  # Chronicle 実績ゼロの新規 DB
        raise  # ロック等は握らない (Codex 二巡 R3)
    return [str(r[0]) for r in rows]


def uncovered_tail_zone(
    tiny_chunks: Sequence[PlannedChunk],
    ordered_messages: Sequence,
) -> tuple:
    """末尾の未被覆帯 = anchor 引き戻しの対象、の検出 (純関数)。

    帯 (zone) は、編纂候補列 (ordered_messages) の**極小 run のメッセージだけ
    から成る最長の末尾** — つまり「その後ろに編纂済みも通常 run も何も無い」
    極小 run 群。run は processed / thread 境界でしか切れないので、帯に
    部分的に掛かる run は構造上存在しない (丸ごと入るか入らないか)。

    引き戻しの根拠 (裁定 5 改訂): この帯は止め線 (境界の anchor) まで未編纂が
    続く末尾の端数で、隣は提示窓 — anchor を帯の最古まで引き戻せば帯は窓の
    中に入り、§16-1 の不変条件が LLM ゼロで満たされる。

    Returns:
        ``(zone_run_ids, first_message_id, zone_chunk_indices)``。帯が無ければ
        ``([], None, set())``。zone_run_ids は正典順。
    """
    membership: Dict[str, int] = {}
    for idx, chunk in enumerate(tiny_chunks):
        for m in chunk.messages:
            membership[m.id] = idx
    zone_ids: List[str] = []
    zone_indices: Set[int] = set()
    for m in reversed(list(ordered_messages)):
        idx = membership.get(m.id)
        if idx is None:
            break
        zone_ids.append(m.id)
        zone_indices.add(idx)
    if not zone_ids:
        return ([], None, set())
    zone_ids.reverse()
    return (zone_ids, zone_ids[0], zone_indices)


def plan_absorption(
    conn: sqlite3.Connection,
    tiny_chunks: Sequence[PlannedChunk],
    ordered_messages: Sequence,
    processed_ids: Set[str],
    *,
    target_chars: int,
    excluded_entry_ids: FrozenSet[str] = frozenset(),
) -> AbsorptionPlan:
    """極小 run の吸収を「開く隣人ごと」に計画する。決定論・LLM なし。

    二段で計画する (機構 D、docs/intent/chronicle_coverage_gaps.md):

    1. **割り当て**: 各極小 run に開く隣人を割り当てる — 後ろ (新しい側) の
       隣人 → 居なければ**同じスレッドの前側**の隣人。スレッド境界の停止は
       両向きで維持 (別スレッドの発話を一つの一次あらすじに合体させない)。
    2. **束ね**: 同じ隣人に割り当たった run を一つの item に束ねる。開き直しと
       再生成は隣人 1 個につき必ず一回 — 前から来た run も後ろから来た run も
       同じ item の材料に入る (旧 ``absorbed_global`` の「後から来た方を break
       で諦めさせる」は、既存 item への相乗りに置き換え — 見送りを新たに
       生まない)。

    どちらの向きにも隣人が居ない run は (機構 E):

    - 発話 (storage の実会話フィルタ ``_conversation_exclusion``) を 1 件でも
      含むなら ``standalone_chunks`` へ — 通常の生成で独立の一次あらすじに
      する (呼び出し側が :func:`merge_standalone_chunks` で整列計画へ合流)。
    - 発話ゼロ (機構の記録だけ) なら ``silent_runs`` に数えるだけで自動では
      書かせない — 解消は第二段の修復 UI (ユーザー操作) に返す。

    Args:
        conn: persona memory.db (読むだけ)。
        tiny_chunks: split_plan_for_absorption が選り分けた極小チャンク。
        ordered_messages: 編纂候補の全列 (plan_alignment に渡したのと同じ、
            正典順)。隣人の探索はこの並びを歩く。
        processed_ids: 既に一次あらすじの source になっている id 集合。
        excluded_entry_ids: 圧縮区間として提示中の digest id 集合。提示中の
            エントリは開き直せない。

    連鎖の停止規則 (吸収は連続範囲しか作らない):

    - 提示中 (excluded) のエントリに当たったら止まる。
    - **スレッドが変わったら止まる** (§3 の thread 境界)。
    - 親境界 (parent_id の変化) を跨がない (Codex レビュー 2026-08-31 採用 3)。
    - source が一部欠けた隣人は**開いてよい** (機構 F — 生存分 + 穴の材料で
      再生成する。欠けは WARNING で可視化)。「処理済みメッセージに被覆 Lv1 が
      引けない」停止は別の防御で、従来どおり残す。
    - **末尾の未被覆帯 (uncovered_tail_zone) は吸収の対象外** — anchor 引き
      戻しの対象として ``rewind_run_ids`` に載せる (裁定 5 改訂)。
    """
    from sai_memory.arasuji.generator import material_chars
    from sai_memory.memory.storage import filter_real_conversation_ids

    threshold = tiny_run_threshold(target_chars)
    plan = AbsorptionPlan()
    if not tiny_chunks:
        plan.stale_upper_ids = list_stale_upper_ids(conn)
        return plan

    ordered = list(ordered_messages)
    position = {m.id: i for i, m in enumerate(ordered)}
    tiny_ordered = sorted(
        range(len(tiny_chunks)),
        key=lambda i: position.get(tiny_chunks[i].messages[0].id, 0),
    )
    membership: Dict[str, int] = {}
    for idx, chunk in enumerate(tiny_chunks):
        for m in chunk.messages:
            membership[m.id] = idx

    consumed: Set[int] = set()
    # 末尾の未被覆帯は吸収の walk に入れない — anchor 引き戻しの対象 (LLM ゼロ)。
    zone_ids, zone_first, zone_indices = uncovered_tail_zone(
        tiny_chunks, ordered,
    )
    if zone_first is not None:
        plan.rewind_run_ids = zone_ids
        plan.rewind_first_message_id = zone_first
        consumed |= zone_indices
        LOGGER.info(
            "[absorption] %d message(s) in %d tiny run(s) at the uncovered "
            "tail; they are a rewind target (anchor pull-back, no LLM), "
            "not an absorption target",
            len(zone_ids), len(zone_indices),
        )

    entry_cache: Dict[str, ArasujiEntry] = {}
    sources_cache: Dict[str, List[Any]] = {}

    def _neighbor_sources(entry: ArasujiEntry) -> List[Any]:
        """隣人の**生存している** source メッセージ (機構 F — 欠けは開く側へ倒す)。"""
        if entry.id not in sources_cache:
            sources = _load_messages(conn, entry.source_ids)
            missing = len({str(s) for s in entry.source_ids}) - len(sources)
            if missing > 0:
                LOGGER.warning(
                    "[absorption] neighbor %s has %d missing source "
                    "message(s); opening it anyway and regenerating from the "
                    "survivors (entry=%s)",
                    entry.id[:8], missing, entry.id,
                )
            sources_cache[entry.id] = sources
        return sources_cache[entry.id]

    def _covering_entry(message_id: str) -> Optional[ArasujiEntry]:
        entries = get_entries_covering_messages(conn, [message_id])
        entry = entries[0] if entries else None
        if entry is None:
            # この防御は撤去しない (機構 F の対象外): 被覆の帳簿そのものが
            # 引けない状態で開き直すと、どの範囲を差し替えるかが決められない。
            LOGGER.warning(
                "[absorption] processed message %s has no covering lv1 "
                "entry; it cannot serve as an absorption neighbor", message_id,
            )
            return None
        return entry_cache.setdefault(entry.id, entry)

    def _openable(entry: ArasujiEntry, thread: object) -> bool:
        """隣人として開けるか — 提示中でなく、生存 source が同スレッド。"""
        if entry.id in excluded_entry_ids:
            LOGGER.info(
                "[absorption] neighbor %s is presented as a folded digest; "
                "it cannot be reopened this round", entry.id[:8],
            )
            return False
        sources = _neighbor_sources(entry)
        if any(getattr(s, "thread_id", None) != thread for s in sources):
            return False  # thread 境界 — 別スレッドと合体しない (両向きで維持)
        return True

    # --- 段 1: 各極小 run へ開く隣人を割り当てる (後ろ優先 → 前側) ---
    assignment: Dict[int, str] = {}
    for idx in tiny_ordered:
        if idx in consumed:
            continue
        chunk = tiny_chunks[idx]
        thread = getattr(chunk.messages[0], "thread_id", None)
        target: Optional[ArasujiEntry] = None
        rear_pos = position.get(chunk.messages[-1].id, -1) + 1
        if 0 < rear_pos < len(ordered) and ordered[rear_pos].id in processed_ids:
            entry = _covering_entry(ordered[rear_pos].id)
            if entry is not None and _openable(entry, thread):
                target = entry
        if target is None:
            front_pos = position.get(chunk.messages[0].id, 0) - 1
            if front_pos >= 0 and ordered[front_pos].id in processed_ids:
                entry = _covering_entry(ordered[front_pos].id)
                if entry is not None and _openable(entry, thread):
                    target = entry
        if target is not None:
            assignment[idx] = target.id

    runs_by_entry: Dict[str, List[int]] = {}
    for idx, eid in assignment.items():
        runs_by_entry.setdefault(eid, []).append(idx)

    absorbed_global: Set[str] = set()
    item_by_entry: Dict[str, AbsorptionItem] = {}
    dirty_uppers: List[str] = []
    dirty_seen: Set[str] = set()

    def _mark_dirty_chain(entry: ArasujiEntry) -> None:
        pid = entry.parent_id
        while pid and pid not in dirty_seen:
            dirty_seen.add(pid)
            dirty_uppers.append(pid)
            parent = get_entry(conn, pid)
            pid = parent.parent_id if parent else None

    # 発話判定 (機構 E) は一括で引いておく — run ごとの点照会を避ける。
    unassigned_ids = [
        m.id
        for idx in tiny_ordered
        if idx not in consumed and idx not in assignment
        for m in tiny_chunks[idx].messages
    ]
    real_utterance_ids = (
        filter_real_conversation_ids(conn, unassigned_ids)
        if unassigned_ids else set()
    )

    # --- 段 2: 開く隣人ごとに一つの item へ束ねる ---
    for idx in tiny_ordered:
        if idx in consumed:
            continue
        chunk = tiny_chunks[idx]
        thread = getattr(chunk.messages[0], "thread_id", None)
        eid = assignment.get(idx)

        if eid is None:
            # 機構 E: 両方向とも吸収先が無い。
            consumed.add(idx)
            if any(m.id in real_utterance_ids for m in chunk.messages):
                plan.standalone_chunks.append(chunk)
                LOGGER.info(
                    "[absorption] tiny run (%d msgs, %d chars) has no "
                    "absorbable neighbor; compiling it standalone (it "
                    "contains real utterances)",
                    len(chunk.messages), int(chunk.coverage_chars),
                )
            else:
                plan.silent_runs += 1
                plan.silent_message_count += len(chunk.messages)
                LOGGER.info(
                    "[absorption] tiny run (%d msgs) has no absorbable "
                    "neighbor and no real utterance; leaving it to the "
                    "user-driven repair (not auto-compiled)",
                    len(chunk.messages),
                )
            continue

        if eid in absorbed_global:
            # 割り当て先が既に別 item で開かれている — 相乗り (機構 D:
            # 見送りを新たに生まない)。通常は _absorb の claim が先に消費
            # するので、ここへ来るのは防御。万一 item が引けない形 (想定外)
            # は、二重に開く item を作らず機構 E の受け皿へ倒す。
            item = item_by_entry.get(eid)
            consumed.add(idx)
            if item is not None:
                item.run_message_ids.extend(m.id for m in chunk.messages)
                item.run_message_ids.sort(key=lambda mid: position.get(mid, 0))
                item.material_chars += int(chunk.coverage_chars)
                item.start_at = min(
                    item.start_at,
                    min((m.created_at or 0) for m in chunk.messages),
                )
            elif filter_real_conversation_ids(
                conn, [m.id for m in chunk.messages],
            ):
                # 割り当て済み run は一括の発話判定 (real_utterance_ids) に
                # 含まれていないので、ここだけ点で引き直す。
                LOGGER.warning(
                    "[absorption] run assigned to an already-opened neighbor "
                    "%s has no item to ride; compiling it standalone",
                    eid[:8],
                )
                plan.standalone_chunks.append(chunk)
            else:
                plan.silent_runs += 1
                plan.silent_message_count += len(chunk.messages)
            continue

        entry = entry_cache[eid]
        item_runs: List[int] = []
        absorbed: List[ArasujiEntry] = []
        covered: Set[str] = set()
        material = 0

        def _claim_run(j: int) -> None:
            nonlocal material
            if j in consumed:
                return
            consumed.add(j)
            item_runs.append(j)
            material += int(tiny_chunks[j].coverage_chars)

        def _absorb(e: ArasujiEntry) -> None:
            nonlocal material
            absorbed.append(e)
            absorbed_global.add(e.id)
            covered.update(str(s) for s in e.source_ids)
            material += sum(material_chars(s) for s in _neighbor_sources(e))
            _mark_dirty_chain(e)
            # この隣人に割り当たった run は全部この item へ (相乗り) —
            # span の内側の穴も、前側から来た run も、後ろから来た run も。
            for j in runs_by_entry.get(e.id, ()):
                _claim_run(j)

        def _max_pos() -> int:
            pos = -1
            for e in absorbed:
                for s in e.source_ids:
                    p = position.get(str(s))
                    if p is not None and p > pos:
                        pos = p
            for j in item_runs:
                p = position.get(tiny_chunks[j].messages[-1].id)
                if p is not None and p > pos:
                    pos = p
            return pos

        _claim_run(idx)
        _absorb(entry)

        # 開いた範囲 (隣人の span + claim 済み run) の直後から後ろへ歩く。
        # 隣接の極小 run は閾値と無関係にすくい取り (2026-08-31 検収指摘の
        # 挙動を維持)、0.5U の閾値が縛るのは「さらに先の別の隣人へ連鎖
        # するか」だけ。
        i = _max_pos() + 1
        while i < len(ordered):
            m = ordered[i]
            if m.id in covered:
                i += 1
                continue
            if m.id in processed_ids:
                if material >= threshold:
                    break  # 0.5U に達した — さらに先の隣人へは連鎖しない
                nxt = _covering_entry(m.id)
                if nxt is None:
                    break  # 被覆 Lv1 が引けない (この防御は維持)
                if nxt.id in excluded_entry_ids:
                    LOGGER.info(
                        "[absorption] neighbor %s is presented as a folded "
                        "digest; chain stops", nxt.id[:8],
                    )
                    break
                if nxt.id in absorbed_global:
                    break  # 既に別 item が開く隣人 — 連続範囲はここまで
                if nxt.parent_id != absorbed[0].parent_id:
                    # 親境界を跨ぐ連鎖はしない (Codex レビュー 2026-08-31
                    # 採用 3): 合体エントリの所有権 (どの親の子になるか) が
                    # 曖昧になる形を計画が作らない。
                    break
                sources = _neighbor_sources(nxt)
                if any(
                    getattr(s, "thread_id", None) != thread for s in sources
                ):
                    break  # thread 境界 — 別スレッドと合体しない
                _absorb(nxt)
                i = max(i + 1, _max_pos() + 1)
                continue
            j = membership.get(m.id)
            if j is not None:
                if j in consumed:
                    break  # 別 item / 帯の run — 連続範囲はここまで
                other = tiny_chunks[j]
                if getattr(other.messages[0], "thread_id", None) != thread:
                    break
                _claim_run(j)
                i = position.get(other.messages[-1].id, i) + 1
                continue
            break  # 通常 run / 未知の未編纂 — 連続範囲はここまで

        run_ids = [
            m.id for j in item_runs for m in tiny_chunks[j].messages
        ]
        # 相乗り (span 内の穴・前側から来た run) で追加順が時系列と一致しなく
        # なるので、正典順に整えてから item にする。
        run_ids.sort(key=lambda mid: position.get(mid, 0))
        item = AbsorptionItem(
            run_message_ids=run_ids,
            absorbed_entry_ids=[e.id for e in absorbed],
            material_chars=material,
            start_at=min(
                (m.created_at or 0)
                for j in item_runs for m in tiny_chunks[j].messages
            ),
        )
        plan.items.append(item)
        for e in absorbed:
            item_by_entry[e.id] = item

    # 前側割り当て (run が隣人より後ろ) があると item の生成順が時系列と
    # 一致しないことがある — 実行 (run_absorption) は start_at 昇順で flush
    # 判定するので、ここで整えておく。
    plan.items.sort(key=lambda it: it.start_at)

    existing_stale = [
        sid for sid in list_stale_upper_ids(conn) if sid not in dirty_seen
    ]
    plan.stale_upper_ids = dirty_uppers + existing_stale
    return plan


# ---------------------------------------------------------------------------
# 未完了の印 (arasuji_progress の器を使う — 再起動を跨いで残る最小の印)
# ---------------------------------------------------------------------------

REPAIR_INCOMPLETE_PROGRESS_ID = "repair_incomplete"

#: 印は理由ごとに別の行 (Codex 二巡採用 1 — 繋ぎ直し失敗の印を吸収の
#: 未完了の印から分離する)。一行に相乗りさせると、吸収の no_work が
#: 「仕事ゼロ = 完了」で印を外すとき、無関係な reconnect の残債まで一緒に
#: 消える。clear は自分の理由の行だけを消し、is_repair_incomplete (読み手)
#: はどの理由でも True のまま。"absorption" の行 id は旧来のままなので、
#: 既存 DB に残っている印もそのまま吸収の印として読める。
_REPAIR_REASON_IDS = {
    "absorption": REPAIR_INCOMPLETE_PROGRESS_ID,
    "reconnect": "repair_incomplete_reconnect",
}


def _repair_reason_id(reason: str) -> str:
    try:
        return _REPAIR_REASON_IDS[reason]
    except KeyError:
        raise ValueError(f"unknown repair-incomplete reason: {reason!r}")


def set_repair_incomplete(
    conn: sqlite3.Connection, *, reason: str = "absorption",
) -> None:
    import time
    conn.execute(
        "INSERT INTO arasuji_progress "
        "(id, last_processed_message_id, last_processed_at) VALUES (?, NULL, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_processed_at = excluded.last_processed_at",
        (_repair_reason_id(reason), int(time.time())),
    )
    conn.commit()


def clear_repair_incomplete(
    conn: sqlite3.Connection, *, reason: str = "absorption",
) -> None:
    """自分の理由の印**だけ**を消す — 他の理由の残債は残す。

    吸収 (run_absorption) は no_work / flush 完了で "absorption" を、
    繋ぎ直し (bands.run_band_overflow) は reconnect が例外なく走り切った回に
    "reconnect" を消す。
    """
    conn.execute(
        "DELETE FROM arasuji_progress WHERE id = ?",
        (_repair_reason_id(reason),),
    )
    conn.commit()


def is_repair_incomplete(conn: sqlite3.Connection) -> bool:
    """前回の補修の仕事が完了していないか (UI の帯・cost-estimate 応答用)。

    理由 (吸収 / 繋ぎ直し) を問わず、どれか一つでも印が残っていれば True —
    帯の「前回の処理が完了していません。再実行してください」はどの残債にも
    同じ促しでよい。印の行に加えて content_stale の残骸も見る — 印だけを
    信じると、印の書き込みより後・flush より前に落ちた稀な並びで嘘の
    「完了」になる。
    """
    reason_ids = tuple(_REPAIR_REASON_IDS.values())
    placeholders = ", ".join("?" for _ in reason_ids)
    try:
        row = conn.execute(
            f"SELECT 1 FROM arasuji_progress WHERE id IN ({placeholders}) "
            "LIMIT 1",
            reason_ids,
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if is_missing_table_error(exc):
            return False  # Chronicle 実績ゼロの新規 DB
        raise  # ロック等は握らない (Codex 二巡 R3)
    if row is not None:
        return True
    return bool(list_stale_upper_ids(conn))


# ---------------------------------------------------------------------------
# 上位 (Lv2+) の再生成 — id は変えず本文だけ更新する
# ---------------------------------------------------------------------------


def regenerate_upper_entry(
    conn: sqlite3.Connection,
    client,
    entry_id: str,
    *,
    persona_id: Optional[str] = None,
    db_lock: Optional[Any] = None,
) -> bool:
    """Lv2 以上のエントリの本文を、子エントリの content 列から再生成する。

    材料 = source_ids が指す子エントリの content (時系列順)。プロンプトは
    既存のレベル昇格 (bands._build_consolidation_prompt) を再利用する —
    新しいプロンプトは発明しない (裁定 3 の実装指示)。

    id は変えない (親子リンクを揺らさない)。成功時に content と span 系の
    metadata を更新し、``content_stale`` の印と古い埋め込みを外す。

    Returns:
        True = 再生成した / もう対象が無い (エントリ消失・空の親の解体)。
        False = LLM 失敗・本文空 (印は残る — 再実行で続きから直る)。
        None = 子集合が生成中に変わった (並行操作)。書き込みは破棄し、印も
        残る — 呼び出し側 (_flush_pending_uppers) は pending に残して次の
        flush で現況から再試行する (Codex レビュー 2026-08-31 採用 5)。
    """
    from sai_memory.arasuji.bands import _build_consolidation_prompt, _digest_origins
    from sai_memory.arasuji.generator import generate_text_with_empty_retry

    # 材料の読み取りとプロンプト組成は錠の内側 (Codex 七巡 K1 — 共有 conn の
    # 読みを他 writer と交錯させない)。LLM 呼び出しだけが錠の外。
    with (db_lock or nullcontext()):
        entry = get_entry(conn, entry_id)
        if entry is None:
            return True
        if entry.level < 2:
            # 通常経路で Lv1 に content_stale は付かないが、迷い込んだ印を放置
            # すると list_stale_upper_ids が拾い続けて flush が永久に失敗する
            # (ローカルレビュー 2026-08-31 L4)。印だけ外して完了扱いにする。
            LOGGER.warning(
                "[absorption] level-%d entry %s carries a stray content_stale "
                "mark; clearing it without regeneration",
                entry.level, entry.id[:8],
            )
            import json as _json
            row = _get_chronicle_page_row(conn, entry.id)
            if row is not None:
                meta = _parse_chronicle_meta(row[3])
                if meta.pop("content_stale", None) is not None:
                    conn.execute(
                        "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                        (_json.dumps(meta, ensure_ascii=False), entry.id),
                    )
                    conn.commit()
            return True

        stored_ids = [str(s) for s in entry.source_ids]
        children = []
        for sid in stored_ids:
            child = get_entry(conn, sid)
            if child is not None:
                children.append(child)
        if not children:
            # 子が全部消えた親は解体する (空の親を語り直す材料が無い)。
            delete_entry_and_update_parent(conn, entry.id)
            return True
        if len(children) != len(set(stored_ids)):
            # 死んだ source id の脱落 (Codex 五巡 H2): stale の後に子が消えた
            # 形。そのまま進むと本文は生存子から作られるのに source_ids に
            # 死んだ id が残り、stale だけが消える。生成の前に帳簿を生存子で
            # 引き直す (source_ids / span / counts —
            # refresh_ancestor_bookkeeping の一枚の規則。既に付いている
            # content_stale はそのまま残る)。
            from sai_memory.arasuji.storage import refresh_ancestor_bookkeeping
            refresh_ancestor_bookkeeping(conn, [entry.id], mark_stale=False)
            entry = get_entry(conn, entry.id)
            if entry is None:
                return True
        children.sort(key=lambda c: (c.start_time or 0, c.end_time or 0))
        # 子集合の指紋 (LLM の間の並行変更の検出 — 書き込み直前に読み直して
        # 照合)。引き直し後の stored 集合と比較する。
        fingerprint = tuple(str(s) for s in entry.source_ids)

        origins = _digest_origins(conn, [c.id for c in children])
        prompt = _build_consolidation_prompt(children, origins, conn)
    try:
        # 空応答は helper が規定回数まで試し直す (usage も試行ごとに記録)。
        # 使い切った EmptyResponseError も他の失敗と同じく False (印は残り、
        # 次の flush が続きから直す)。
        content = generate_text_with_empty_retry(
            client,
            [{"role": "user", "content": prompt}],
            purpose="absorption",
            persona_id=persona_id,
            usage_node_type=f"chronicle_level{entry.level}",
        )
    except Exception:
        LOGGER.exception(
            "[absorption] upper regeneration LLM failed (entry=%s level=%d)",
            entry.id[:8], entry.level,
        )
        return False

    import json
    with (db_lock or nullcontext()):
        row = _get_chronicle_page_row(conn, entry.id)
        if row is None:
            return True
        meta = _parse_chronicle_meta(row[3])
        # 指紋照合 (Codex レビュー 2026-08-31 採用 5): LLM の間に子集合が
        # 変わっていたら (吸収 swap / 並行 API 操作)、古い子から書いた本文で
        # 上書きせず、stale の印も残す — pending 経由で次の flush が現況から
        # 語り直す。
        current = tuple(str(s) for s in (meta.get("source_ids") or []))
        if current != fingerprint:
            LOGGER.info(
                "[absorption] children of %s changed during regeneration "
                "(%d -> %d source ids); discarding this output and keeping "
                "the stale mark for the next flush",
                entry.id[:8], len(fingerprint), len(current),
            )
            return None
        meta.pop("content_stale", None)
        starts = [c.start_time for c in children if c.start_time is not None]
        ends = [c.end_time for c in children if c.end_time is not None]
        if starts:
            meta["start_time"] = min(starts)
        if ends:
            meta["end_time"] = max(ends)
        meta["source_count"] = len(children)
        meta["message_count"] = sum(c.message_count for c in children)
        conn.execute(
            "UPDATE memopedia_pages SET content = ?, metadata = ? WHERE id = ?",
            (content, json.dumps(meta, ensure_ascii=False), entry.id),
        )
        _delete_embedding_rows(conn, [entry.id])
        conn.commit()
    LOGGER.info(
        "[absorption] regenerated upper entry %s (level=%d, %d children)",
        entry.id[:8], entry.level, len(children),
    )
    return True


def _delete_embedding_rows(
    conn: sqlite3.Connection, entry_ids: Sequence[str],
) -> None:
    """arasuji_embeddings の道連れ削除 (本文が変わった / 消えたエントリの分)。"""
    ids = [str(e) for e in entry_ids if e]
    if not ids:
        return
    try:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"DELETE FROM arasuji_embeddings WHERE entry_id IN ({placeholders})",
            ids,
        )
    except sqlite3.OperationalError as exc:
        if not is_missing_table_error(exc):
            raise  # ロック等は握らない (Codex 二巡 R3)
        # 埋め込みテーブルの無い DB (旧テスト等) は許容


def _mark_ancestors_stale(
    conn: sqlite3.Connection, parent_ids: Sequence[str],
) -> List[str]:
    """親 (と先祖) の帳簿を現況の子から引き直し、content_stale の印を付ける。

    帳簿の整合が本文の再生成より先 (裁定): source_ids / span 系はここで確定し、
    本文だけが「古い」状態で flush を待つ。実体は
    storage.refresh_ancestor_bookkeeping (Codex 五巡 H1 で手動削除の追随と
    共用化 — あちらは mark_stale=False)。
    """
    from sai_memory.arasuji.storage import refresh_ancestor_bookkeeping
    return refresh_ancestor_bookkeeping(conn, parent_ids, mark_stale=True)


def list_broken_parent_ids(conn: sqlite3.Connection) -> List[str]:
    """存在しない子 id を指す上位 (Lv2+) の id — **検出だけ** (純読み)。

    :func:`_sweep_broken_parents` (処置つき) と見積もり
    (sai_memory.arasuji.estimate) の一点共有。見積もりは書けないので、検出を
    処置から切り離してこちらを呼ぶ — 二枚の検知クエリを保守すると表示と実走が
    食い違う。
    """
    try:
        # 検出は SQL 一発 (Codex 二巡 R5 — 親×子の点照会ループを json_each +
        # NOT EXISTS に畳む)。存在検査は互換 VIEW (arasuji_entries) に対して
        # 行う — get_entry と同じ「chronicle カテゴリ・未削除」の意味論。
        rows = conn.execute(
            """
            SELECT DISTINCT a.id
            FROM arasuji_entries a, json_each(a.source_ids_json) s
            WHERE a.level >= 2
              AND NOT EXISTS (
                SELECT 1 FROM arasuji_entries c WHERE c.id = s.value
              )
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if is_missing_table_error(exc):
            return []  # Chronicle 実績ゼロの新規 DB
        raise  # ロック等は握らない (Codex 二巡 R3)
    return [str(r[0]) for r in rows]


def list_sweep_regen_ids(conn: sqlite3.Connection) -> List[str]:
    """sweep が content_stale を付けることになる上位の全量 — 純読み。

    壊れた親**とその先祖**。処置 (:func:`_mark_ancestors_stale` →
    storage.refresh_ancestor_bookkeeping) は queue で親の親まで遡って印を
    付けるので、後段の flush はその全部を語り直す = LLM コール。見積もりが
    壊れた親だけを数えると「表示 < 実走」になる (§16-2 が禁じる向き)。

    子が全部消えた親は処置側で解体されて再生成されない — その分はここが
    数え過ぎる。「表示 ≥ 実走」の側なので許容する。
    """
    ids: List[str] = []
    seen: Set[str] = set()
    queue: List[str] = list(list_broken_parent_ids(conn))
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        ids.append(pid)
        entry = get_entry(conn, pid)
        if entry is not None and entry.parent_id:
            queue.append(str(entry.parent_id))
    return ids


def _sweep_broken_parents(conn: sqlite3.Connection) -> List[str]:
    """存在しない子 id を指す上位 (Lv2+) を検出し、現況の子から引き直す。

    非原子な差し替え (storage 層は 1 操作 1 commit — 単一 tx 化は層の作り直しに
    なるため却下、Codex レビュー 2026-08-31 採用 1) の残骸と、**素の
    delete_entry** (UI の個別削除 API が使う — 親の source_ids から子を外さ
    ない) が残す死んだ子 id の受け皿。壊れた親は :func:`_mark_ancestors_stale`
    と同じ規則で帳簿を引き直して content_stale を付け (子ゼロなら解体)、後段の
    flush が本文を語り直す。冪等 — 壊れた親が無ければ何も書かない。
    """
    broken = list_broken_parent_ids(conn)
    if broken:
        LOGGER.warning(
            "[absorption] %d upper entr(ies) reference missing children "
            "(crash residue or plain-delete leftovers); re-deriving their "
            "bookkeeping from surviving children: %s",
            len(broken), ",".join(b[:8] for b in broken),
        )
        _mark_ancestors_stale(conn, broken)
    return broken


def _sweep_dead_message_sources(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """Lv1 の source_ids から、消えたメッセージを指す id を落とす。

    :func:`_sweep_broken_parents` の **Lv1 → メッセージ版** (2026-09-01 まはー
    裁定)。片側だけ受け皿があるのは非対称だった: UI の削除は「上位あらすじ →
    消えた下位あらすじ」も「Lv1 → 消えたメッセージ」も同じように参照を直さ
    ないのに、掃除は前者にしか無かった。

    孤児参照そのものは被覆計算に効かないが、吸収の再開検査 (
    :func:`plan_absorption` の「missing source messages」) を落とす。落ちると
    その隣の未被覆断片は永久に取り残される (本番で実害)。掃いた後は source が
    全生存になるので、同じ隣人が次の計画で開き直せる。

    処置は storage.prune_dead_message_sources (LLM ゼロの帳簿補修 — 本文には
    触らない)。書き換えた Lv1 の親は、子の counts が変わった分だけ帳簿が
    ずれるので ``mark_stale=False`` で引き直す (手動削除の追随と同じ規則 —
    本文の語り直しは起こさないので、見積もりの LLM 回数は変わらない)。
    冪等 — 孤児が無ければ何も書かない。
    """
    from sai_memory.arasuji.storage import (
        prune_dead_message_sources,
        refresh_ancestor_bookkeeping,
    )

    removed = prune_dead_message_sources(conn)
    if not removed:
        return {}
    for entry_id, dead_ids in removed.items():
        LOGGER.info(
            "[absorption] level-1 entry %s referenced %d deleted message(s); "
            "dropped from its source_ids and counts re-derived: %s",
            entry_id[:8], len(dead_ids), ",".join(dead_ids),
        )
    parent_ids = []
    for entry_id in removed:
        entry = get_entry(conn, entry_id)
        if entry is not None and entry.parent_id:
            parent_ids.append(str(entry.parent_id))
    if parent_ids:
        refresh_ancestor_bookkeeping(conn, parent_ids, mark_stale=False)
    return removed


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------


@dataclass
class AbsorptionResult:
    merged_entries: List[ArasujiEntry] = field(default_factory=list)
    reopened_entry_ids: List[str] = field(default_factory=list)
    regenerated_upper_ids: List[str] = field(default_factory=list)
    skipped_items: int = 0
    #: 隣のあらすじへ合流した run メッセージの数 (完了報告の内訳 — 機構 G)。
    absorbed_run_message_count: int = 0
    #: skip の結果**未被覆のまま残った** run メッセージの数 (機構 G の「残り」)。
    #: 冪等スキップ (run が既に被覆済みで仕事が無かった) は含まない — あれは
    #: 完了であって残りではない。部分被覆の skip は未被覆分だけを数える。
    skipped_run_message_count: int = 0
    #: 同、理由別の内訳 {理由: メッセージ数}。完了報告のログ・台帳用。
    skipped_reasons: Dict[str, int] = field(default_factory=dict)
    cancelled: bool = False

    def note_skipped_uncovered(self, reason: str, count: int) -> None:
        """skip で未被覆のまま残ったメッセージ数を理由つきで積む (機構 G)。"""
        if count <= 0:
            return
        self.skipped_run_message_count += count
        self.skipped_reasons[reason] = (
            self.skipped_reasons.get(reason, 0) + count
        )


def _repoint_fragments(
    conn: sqlite3.Connection, old_id: str, new_id: str,
) -> List[str]:
    """memopedia_fragments.chronicle_entry_id を旧→新へ付け替える (消さない)。

    戻りは動かした fragment id (失敗時の巻き戻し用)。テーブルの無い DB は空。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM memopedia_fragments WHERE chronicle_entry_id = ?",
            (old_id,),
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE memopedia_fragments SET chronicle_entry_id = ? "
            "WHERE chronicle_entry_id = ?",
            (new_id, old_id),
        )
        conn.commit()
        return [str(r[0]) for r in rows]
    except sqlite3.DatabaseError as exc:
        # 捕捉は DatabaseError の幅で (Codex 十二巡 Q2 — 縮退の判定を
        # OperationalError に限らず、他の DB 例外も同じ道を通す)。
        if is_missing_table_error(exc):
            return []  # Fragment テーブルの無い DB (旧テスト等)
        raise  # ロック等 — フェーズ 1 の巻き戻し → AbsorptionError へ乗せる (R3)


def _repoint_fragments_back(
    conn: sqlite3.Connection, fragment_ids: Sequence[str], old_id: str,
) -> None:
    if not fragment_ids:
        return
    try:
        placeholders = ",".join("?" for _ in fragment_ids)
        conn.execute(
            f"UPDATE memopedia_fragments SET chronicle_entry_id = ? "
            f"WHERE id IN ({placeholders})",
            (old_id, *[str(f) for f in fragment_ids]),
        )
        conn.commit()
    except sqlite3.DatabaseError as exc:
        if is_missing_table_error(exc):
            return
        # 巻き戻しは best-effort — ここで raise すると取り下げ (_withdraw) まで
        # 止まる。握らず WARNING で可視化だけする (R3 の趣旨は「黙って成功の
        # 顔をしない」)。捕捉は DatabaseError の幅で (Codex 十二巡 Q2) —
        # OperationalError だけだと他の DB 例外が撤去処理ごと止める。
        LOGGER.warning(
            "[absorption] fragment repoint rollback failed (target=%s); "
            "some fragments may keep pointing at the withdrawn entry",
            old_id, exc_info=True,
        )


def _repoint_batches(
    conn: sqlite3.Connection, batch_ids: Sequence[int],
    from_id: str, to_id: str,
) -> int:
    """付記印を id 指定で付け替える (reassign_batches_annexed の的を絞った形)。

    複数の旧エントリが一つの新エントリへ合流するため、from/to の全件付け替え
    では失敗時にどの印をどこへ戻すか区別できない — id を明示して可逆にする。
    """
    if not batch_ids:
        return 0
    placeholders = ",".join("?" for _ in batch_ids)
    cur = conn.execute(
        f"UPDATE perception_batches SET annexed_entry_id = ? "
        f"WHERE id IN ({placeholders}) AND annexed_entry_id = ?",
        (to_id, *[int(b) for b in batch_ids], from_id),
    )
    conn.commit()
    return cur.rowcount


#: 指紋 CAS 棄却 (並行変更) の同一上位エントリへの再試行上限 (1 ジョブあたり)。
#: 競合が続く限り item ごとの flush が同じエントリへ LLM を撃ち続けると、
#: 見積もりを超えて課金が伸びる (Codex 二巡 R2)。超過分は content_stale の
#: まま次回のジョブへ延期する。
_UPPER_CONFLICT_RETRY_LIMIT = 2


def _covered_run_count(
    conn: sqlite3.Connection, run_ids: Sequence[str],
    *, exclude_entry_id: Optional[str] = None,
) -> int:
    """run の message id のうち、既にいずれかの Lv1 の source になっている数。

    ``exclude_entry_id``: 数えから外すエントリ。確定直前の再検査 (R1) は
    生成済みの自分の合体エントリを渡す — 除外しないと自分の source を
    「別経路の被覆」と誤認して全 merge が自分を取り下げる。
    """
    ids = [str(m) for m in run_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT COUNT(DISTINCT json_each.value) "
        "FROM arasuji_entries a, json_each(a.source_ids_json) "
        f"WHERE a.level = 1 AND json_each.value IN ({placeholders})"
    )
    params: List[Any] = list(ids)
    if exclude_entry_id is not None:
        sql += " AND a.id != ?"
        params.append(str(exclude_entry_id))
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _flush_pending_uppers(
    conn: sqlite3.Connection,
    client,
    pending: Set[str],
    result: AbsorptionResult,
    *,
    before_start: Optional[int],
    persona_id: Optional[str],
    db_lock: Optional[Any],
    conflict_counts: Optional[Dict[str, int]] = None,
) -> None:
    """pending の上位のうち被覆範囲を抜けたものを再生成する (level 昇順・1 回ずつ)。

    ``before_start=None`` はジョブ末尾の全 flush。失敗 (False) は
    AbsorptionError — 「親子リンクは整合・本文だけ古い」で止まり、印
    (content_stale / repair_incomplete) が残って再実行が続きを拾う。
    """
    candidates = []
    # 候補選定の読みも錠の内側 (Codex 八巡)。再生成本体 (regenerate_upper_entry)
    # は自分で錠を取る — RLock なので同一スレッドの入れ子も安全だが、LLM を
    # 錠の外に置くためループはこのブロックの外で回す。
    with (db_lock or nullcontext()):
        for pid in list(pending):
            entry = get_entry(conn, pid)
            if entry is None:
                pending.discard(pid)
                continue
            if before_start is not None and (
                entry.end_time is None or entry.end_time >= before_start
            ):
                continue  # まだ範囲の中 (または範囲不明 = 保守側で待つ)
            candidates.append(entry)
    candidates.sort(key=lambda e: (e.level, e.end_time or 0))
    for entry in candidates:
        outcome = regenerate_upper_entry(
            conn, client, entry.id, persona_id=persona_id, db_lock=db_lock,
        )
        if outcome is None:
            # 並行変更との競合 — pending に残して次の flush で現況から再試行。
            # ただし 1 ジョブの再試行は上限まで (Codex 二巡 R2 — 競合が続く限り
            # 同じエントリへ LLM を撃ち続けると見積もりを超える)。超過したら
            # content_stale のまま次回のジョブへ延期する (印と未完了マーカーが
            # 残り、帯が再実行を促す)。
            if conflict_counts is not None:
                count = conflict_counts.get(entry.id, 0) + 1
                conflict_counts[entry.id] = count
                if count >= _UPPER_CONFLICT_RETRY_LIMIT:
                    pending.discard(entry.id)
                    LOGGER.warning(
                        "[absorption] upper %s hit the conflict retry limit "
                        "(%d) this job; deferring to the next repair run "
                        "with content_stale kept",
                        entry.id[:8], count,
                    )
            continue
        if not outcome:
            raise AbsorptionError(
                f"upper regeneration failed for {entry.id} (level={entry.level})"
            )
        pending.discard(entry.id)
        result.regenerated_upper_ids.append(entry.id)


def run_absorption(
    conn: sqlite3.Connection,
    client,
    plan: Optional[AbsorptionPlan],
    *,
    persona_id: Optional[str] = None,
    db_lock: Optional[Any] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    batch_callback: Optional[Callable] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> AbsorptionResult:
    """吸収の計画を実行する (LLM あり)。

    plan=None または items 空でも呼んでよい — その場合は前回の未完了
    (content_stale の残り) の flush だけを行う。仕事が何も無ければ印にも
    触らずに返る。

    途中失敗は AbsorptionError / LLMError として上がる。確定済みの差し替えは
    残り、repair_incomplete の印が立ったままになる — 再実行が続きから直す。

    ``progress_callback(phase, done, total)`` は画面へ進行を伝えるための任意の
    フック (2026-09-01)。この関数は run 一件ごとに LLM を呼ぶので、実機では
    ここだけで数分〜数十分かかる — 呼び出し側が何も報告しないと、本編
    (execute_plan) の進捗だけを見ている画面が「(0/N) のまま凍った」ように
    見える。粒度は粗く、``phase`` は:

    - ``"absorb"`` ... 極小 run の合体 (done = 何件目 / total = 計画の件数)
    - ``"regenerate"`` ... ジョブ末尾の上位あらすじ語り直し (total = 対象数)

    フックの例外は呼び出し側の責任 — ここでは捕まえない (握り潰すと、報告が
    壊れていることに誰も気づけない)。
    """
    from sai_memory.arasuji.generator import generate_level1_arasuji
    from sai_memory.perception_buffer import list_batches_annexed_to

    result = AbsorptionResult()
    items = list(plan.items) if plan is not None else []
    # ジョブ冒頭の整合性検査〜マーカー操作は一つの錠の中で原子的に行う
    # (Codex 八巡 — sweep の書き込み・stale 判定・印の上げ下げが他 writer と
    # 交錯すると、判定と操作の間に世界が変わる)。
    with (db_lock or nullcontext()):
        # 整合性検査 (Codex レビュー 2026-08-31 採用 1): 非原子な差し替えの
        # crash 残骸や素の delete_entry が残した「死んだ子 id を指す親」を、
        # 走る前に冪等に引き直して flush の対象に載せる。
        _sweep_broken_parents(conn)
        # 同じ理由 (素の削除が参照を直さない) の Lv1 側 — 消えたメッセージを
        # 指す source_ids を落として、隣人が再び開けるようにする。
        _sweep_dead_message_sources(conn)
        pending: Set[str] = set(list_stale_upper_ids(conn))
        no_work = not items and not pending
        if no_work:
            # 仕事ゼロ = 未完了状態は残っていない。前回の印が残っていたら外す
            # (ローカルレビュー 2026-08-31 L1 — フェーズ 3 の例外などで
            # 「差し替え確定・stale ゼロ」のまま印だけ残ると、帯の「前回の
            # 処理が完了していません」が永久表示になり、再実行しても no-op で
            # 消えない)。
            clear_repair_incomplete(conn)
        else:
            set_repair_incomplete(conn)
    if no_work:
        return result

    # 指紋 CAS 棄却の再試行回数 (1 ジョブあたり — Codex 二巡 R2)。
    conflict_counts: Dict[str, int] = {}

    for index, item in enumerate(items):
        if cancel_check and cancel_check():
            result.cancelled = True
            return result  # 印は残す — 再実行が続きを拾う

        if progress_callback:
            progress_callback("absorb", index + 1, len(items))

        # flush: この item の開始時刻より手前で被覆範囲が閉じた上位を先に
        # 語り直す (裁定 4 — 後続の Lv1 生成の「これまでの流れ」を新しくする)。
        _flush_pending_uppers(
            conn, client, pending, result,
            before_start=item.start_at, persona_id=persona_id, db_lock=db_lock,
            conflict_counts=conflict_counts,
        )

        # 冪等スキップ (Codex レビュー 2026-08-31 採用 2 — 全 id で判定):
        # run の**全部**が既にいずれかの Lv1 の source なら前回の実行が完了
        # している = skip。**一部だけ**被覆済み (crash 残骸・並行変更) なら、
        # 中途半端な材料で合体を強行せず item を破棄して警告する — 残りは
        # 次回の補修が現況から再計画する。
        run_ids = [str(m) for m in item.run_message_ids]
        with (db_lock or nullcontext()):
            covered = _covered_run_count(conn, run_ids)
        if covered == len(set(run_ids)):
            # 冪等スキップ — run は既に被覆済み (前回の実行が完了している)。
            # 残りには数えない。
            result.skipped_items += 1
            continue
        if covered > 0:
            result.skipped_items += 1
            result.note_skipped_uncovered(
                "partially_covered", len(set(run_ids)) - covered,
            )
            LOGGER.warning(
                "[absorption] %d of %d run message(s) are already covered "
                "(crash residue or concurrent change); discarding this item — "
                "the next repair run re-plans the remainder",
                covered, len(set(run_ids)),
            )
            continue

        # 材料の DB 読み取りは錠の内側 (Codex 七巡 K1 — 共有 conn の読みを
        # 他 writer の書き込みと交錯させない)。LLM は錠の外。
        with (db_lock or nullcontext()):
            # 開き直す隣人のスナップショット (CAS の基準)。消えていたら世界が
            # 変わっている — この item は見送り、次回の再計画に任せる。
            snapshots: List[ArasujiEntry] = []
            missing = False
            for eid in item.absorbed_entry_ids:
                entry = get_entry(conn, eid)
                if entry is None:
                    missing = True
                    break
                snapshots.append(entry)
            if not missing:
                # 材料 = run の生ログ + 開き直す隣人の source 生ログ (連続範囲)。
                material_ids = list(item.run_message_ids)
                for entry in snapshots:
                    material_ids.extend(str(s) for s in entry.source_ids)
                messages = _load_messages(conn, material_ids)
                # 旧隣人の材料だった知覚バッチも材料として渡す
                # (regenerate_entry と同型)。
                old_batches: Dict[str, List] = {}
                extra_items: List[dict] = []
                for entry in snapshots:
                    try:
                        batches = list_batches_annexed_to(conn, entry.id)
                    except sqlite3.OperationalError as exc:
                        if not is_missing_table_error(exc):
                            raise  # ロック等は fail-closed (Codex 二巡 R3)
                        batches = []  # 知覚台帳の無い DB (旧テスト等)
                    old_batches[entry.id] = batches
                    extra_items.extend(
                        {"at": int(b.consumed_at), "text": b.rendered_text}
                        for b in batches
                    )
                extra_items.sort(key=lambda x: x["at"])
        if missing:
            result.skipped_items += 1
            result.note_skipped_uncovered("neighbor_missing", len(set(run_ids)))
            LOGGER.info(
                "[absorption] neighbor disappeared before the merge; "
                "item skipped (re-planned next run)",
            )
            continue
        if len(messages) < len(set(material_ids)):
            # 機構 F (chronicle_coverage_gaps): 材料の一部が DB に無くても
            # 生存分で進める — 「開かない」柵は計画側と実行側の両方から撤去。
            # 読めないメッセージの最有力の由来はユーザー自身のログ削除で、
            # 開き直しで削除分があらすじから消えるのは削除の完遂 (2026-09-08
            # まはー裁定)。全滅 (生存ゼロ) だけは生成の材料が無いので skip。
            missing_count = len(set(material_ids)) - len(messages)
            if not messages:
                result.skipped_items += 1
                result.note_skipped_uncovered(
                    "material_missing", len(set(run_ids)),
                )
                LOGGER.warning(
                    "[absorption] all %d material message(s) are missing; "
                    "item skipped (nothing to generate from)", missing_count,
                )
                continue
            LOGGER.warning(
                "[absorption] %d material message(s) missing (entries=%s); "
                "proceeding with the %d survivor(s)",
                missing_count,
                ",".join(e.id[:8] for e in snapshots),
                len(messages),
            )
        messages.sort(key=lambda m: (m.created_at or 0))

        # 生成が先 (旧隣人は生きたまま)。失敗したら何も失わずに止まる。
        # 文脈の読み取りと保存 (create/commit) は generator 側が db_lock の
        # 内側で行う (K1) — LLM 呼び出しだけが錠の外。
        new_entry = generate_level1_arasuji(
            client, conn, messages,
            persona_id=persona_id,
            extra_items=extra_items or None,
            db_lock=db_lock,
        )
        if new_entry is None:
            raise AbsorptionError(
                f"merged lv1 generation failed ({len(messages)} messages)"
            )

        def _withdraw() -> None:
            # 撤去も錠の内側 (Codex 八巡 — 削除は複数 UPDATE + commit の列)。
            # 無条件では消さない (Codex 九巡 M1): 生成直後の素の状態
            # (親なし・未統合・content 一致) から変わっていたら、別の書き手が
            # 採用した形跡 — 壊さずに残し、次回補修の再計画に任せる。重い
            # 指紋機構は作らない — 条件はこの一点だけ (まはー裁定)。
            try:
                with (db_lock or nullcontext()):
                    current = get_entry(conn, new_entry.id)
                    if current is None:
                        return
                    if (
                        current.parent_id is not None
                        or current.is_consolidated
                        or current.content != new_entry.content
                    ):
                        LOGGER.warning(
                            "[absorption] withdrawal target %s shows signs "
                            "of adoption by another writer (parent/"
                            "consolidated/edited); leaving it in place — "
                            "the next repair run re-plans around it",
                            new_entry.id[:8],
                        )
                        return
                    delete_entry_and_update_parent(conn, new_entry.id)
            except Exception:
                LOGGER.warning(
                    "[absorption] failed to withdraw merged entry %s; a "
                    "duplicate row may remain", new_entry.id, exc_info=True,
                )

        # CAS: 生成の間に隣人が動いていたら差し替えない (並行操作の方が正)。
        # 再読みも錠の内側 (K1)。
        conflicted = False
        with (db_lock or nullcontext()):
            for snap in snapshots:
                current = get_entry(conn, snap.id)
                if (
                    current is None
                    or current.content != snap.content
                    or current.source_ids != snap.source_ids
                    or current.parent_id != snap.parent_id
                ):
                    conflicted = True
                    break
        if conflicted:
            _withdraw()
            result.skipped_items += 1
            result.note_skipped_uncovered("neighbor_changed", len(set(run_ids)))
            LOGGER.info(
                "[absorption] neighbor changed during generation; merge "
                "withdrawn (re-planned next run)",
            )
            continue

        # --- 差し替え。フェーズ 1 (可逆): Fragment と付記印の付け替え。 ---
        moved_fragments: List[tuple] = []  # (old_id, [fragment_ids])
        moved_batches: List[tuple] = []    # (old_id, [batch_ids])
        repoint_failed = False
        recheck_conflict = False
        recheck_covered = 0
        with (db_lock or nullcontext()):
            # 確定直前の run 被覆再検査 (Codex 二巡 R1): Beat ロックは本番内の
            # 並走を直列化するが、CLI (ロック外プロセス) との同時実行は塞がない。
            # LLM の間に run が別経路で被覆されていたら、この合体は二重被覆に
            # なる — 取り下げて次回の再計画に任せる。
            recheck_covered = _covered_run_count(
                conn, run_ids, exclude_entry_id=new_entry.id,
            )
            if recheck_covered > 0:
                recheck_conflict = True
            else:
                try:
                    for snap in snapshots:
                        frag_ids = _repoint_fragments(
                            conn, snap.id, new_entry.id,
                        )
                        if frag_ids:
                            moved_fragments.append((snap.id, frag_ids))
                    for snap in snapshots:
                        ids = [b.id for b in old_batches.get(snap.id, [])]
                        if not ids:
                            continue
                        moved = _repoint_batches(
                            conn, ids, snap.id, new_entry.id,
                        )
                        moved_batches.append((snap.id, ids))
                        if moved != len(ids):
                            raise AbsorptionError(
                                f"perception stamp repoint mismatch for {snap.id}"
                            )
                except Exception:
                    repoint_failed = True
                    LOGGER.warning(
                        "[absorption] bookkeeping repoint failed; reverting and "
                        "withdrawing the merged entry", exc_info=True,
                    )
                    # 未確定の UPDATE を明示的に巻き戻す (Codex 十二巡 Q2)。
                    # ``conn.commit()`` 自体が失敗した回は、付け替えが
                    # トランザクションに残ったまま下の巻き戻し (と撤去) の
                    # commit に相乗りして確定する — Fragment / 付記印が撤去済み
                    # の合体エントリを指す。錠の内側で行う。
                    try:
                        conn.rollback()
                    except Exception:
                        LOGGER.warning(
                            "[absorption] rollback of the pending repoint "
                            "failed", exc_info=True,
                        )
                    for old_id, ids in moved_batches:
                        try:
                            _repoint_batches(conn, ids, new_entry.id, old_id)
                        except Exception:
                            pass
                    for old_id, frag_ids in moved_fragments:
                        _repoint_fragments_back(conn, frag_ids, old_id)
        if recheck_conflict:
            _withdraw()
            result.skipped_items += 1
            # 別経路が被覆した分は残りではない — 未被覆の残余だけ数える。
            result.note_skipped_uncovered(
                "concurrently_covered", len(set(run_ids)) - recheck_covered,
            )
            LOGGER.warning(
                "[absorption] run became covered during generation "
                "(concurrent CLI / other writer); merge withdrawn — the next "
                "repair run re-plans from the current state",
            )
            continue
        if repoint_failed:
            _withdraw()
            raise AbsorptionError("bookkeeping repoint failed; merge aborted")

        # --- フェーズ 2 (確定): 旧隣人の削除。 ---
        # delete_entry_and_update_parent が親の source_ids からの除去・
        # recall_edges・埋め込みの道連れ削除まで面倒を見る。途中失敗は
        # 二重被覆 (旧新の共存) で止まる — 被覆の穴は開けない。
        parent_ids: List[str] = []
        for snap in snapshots:
            if snap.parent_id and snap.parent_id not in parent_ids:
                parent_ids.append(snap.parent_id)
        with (db_lock or nullcontext()):
            for snap in snapshots:
                # 差し替えの途中では先祖を引き直さない — 合体エントリを親へ
                # 繋ぐ前に引き直すと一人っ子の親が解体される。最終形の引き直しは
                # フェーズ 3 の _mark_ancestors_stale が行う (Codex 六巡 J5)。
                ok, _pid = delete_entry_and_update_parent(
                    conn, snap.id, refresh_ancestors=False,
                )
                if not ok:
                    raise AbsorptionError(
                        f"failed to delete reopened neighbor {snap.id}"
                    )
            # --- フェーズ 3: 親リンクと先祖の印。 ---
            if parent_ids:
                from sai_memory.arasuji.storage import add_to_parent_source_ids
                if not add_to_parent_source_ids(
                    conn, new_entry.id, parent_ids[0],
                ):
                    # 親が並行操作で消えていた — regenerate_entry の親消失時と
                    # 同じく、root 直下の未束ねとして残す (兄弟と整合。
                    # ローカルレビュー 2026-08-31 L2 — 黙って握らない)。
                    LOGGER.warning(
                        "[absorption] parent %s disappeared before linking "
                        "the merged entry %s; leaving it unconsolidated at "
                        "the root",
                        parent_ids[0][:8], new_entry.id[:8],
                    )
            marked = _mark_ancestors_stale(conn, parent_ids)
        pending.update(marked)

        result.merged_entries.append(new_entry)
        result.reopened_entry_ids.extend(e.id for e in snapshots)
        result.absorbed_run_message_count += len(set(run_ids))
        LOGGER.info(
            "[absorption] merged tiny run into neighbor(s): run=%d msgs, "
            "reopened=%d entries, material=%d chars -> entry %s",
            len(item.run_message_ids), len(snapshots),
            item.material_chars, new_entry.id[:8],
        )

        # 新エントリからの Fragment 新規抽出 (既存機構のまま。重複登録は受容)。
        if batch_callback:
            try:
                batch_callback(messages, new_entry.id)
            except Exception:
                try:
                    from sai_memory.memory.entity_extractor import (
                        record_extraction_failure,
                    )
                    record_extraction_failure(
                        conn, new_entry.id, db_lock=db_lock,
                    )
                except Exception:
                    LOGGER.error(
                        "[absorption] 抽出失敗の付箋を残せませんでした "
                        "(entry=%s)", new_entry.id[:8], exc_info=True,
                    )
                LOGGER.exception(
                    "[absorption] fragment extraction failed for merged "
                    "entry %s; continuing", new_entry.id[:8],
                )

    # ジョブ末尾: 未 flush の先祖を全部 flush する。
    if progress_callback and pending:
        progress_callback("regenerate", 0, len(pending))
    _flush_pending_uppers(
        conn, client, pending, result,
        before_start=None, persona_id=persona_id, db_lock=db_lock,
        conflict_counts=conflict_counts,
    )

    # 全部済んだ — content_stale が残っていないことを確かめてから印を外す。
    # 判定と clear は同じ錠の中で原子的に (Codex 八巡 — 間に別 writer が
    # stale を足すと、付いたばかりの印ごと「完了」の顔で消える)。
    with (db_lock or nullcontext()):
        if not list_stale_upper_ids(conn):
            clear_repair_incomplete(conn)
    return result
