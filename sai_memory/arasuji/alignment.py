"""Chunk planning for Chronicle generation (arasuji_levels.md §4 — レベル0 の畳み).

docs/intent/arasuji_levels.md の実装。旧「episode 整列 + 恒等圧縮 + 転写」
(experience_structure.md §4 の圧縮七原則ベース) は 2026-07-28 に世代交代した —
エピソードに畳みを止める権利は無く、恒等圧縮 (identity) と digest 転写
(episode) のチャンク種別は廃止 (すべて LLM バッチ)。

このモジュールは**純関数のみ** — LLM 呼び出しも DB 書き込みもしない。
生成経路 (sea/session_lifecycle.generate_chronicle) とコスト見積もり
(sai_memory/arasuji/estimate) が同じ計画を共有する (一点管理)。

規則:

- 渡された編纂対象を、約 ``target_chars`` (U) ずつのチャンクに刻む。
- 編纂済み (processed) メッセージを跨いだ束ねをしない (run 分割)。呼び出し側が
  退場範囲を ``run_groups`` で群に分けて渡した場合は、群をまたぐ束ねもしない
  (離れた範囲を一つのあらすじに束ねる「偽の隣接」の禁止)。
- run 末尾の U に届かない端数は直前のチャンクに吸収する。吸収先が無い
  (run 全体が U 未満の) 場合はそのまま小さなチャンクとして LLM 圧縮する —
  **小さくても要約する** (生ログを生のまま一次あらすじの席に置く恒等圧縮は
  廃止。豆粒が列を分断する問題の根だった)。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from sai_memory.memory.storage import Message, spell_group_spans

LOGGER = logging.getLogger(__name__)

# チャンク種別 (metadata.digest_origin にそのまま記録される)。現設計では
# 全チャンクが LLM バッチ — 旧語彙 (identity / episode) は既存データにだけ残る。
CHUNK_LLM_BATCH = "batch"

#: 一次あらすじの標準被覆字数 U。
DEFAULT_TARGET_CHARS = 10_000


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default
    if value < minimum:
        LOGGER.warning(
            "%s=%d below minimum %d, using default %d", name, value, minimum, default,
        )
        return default
    return value


def chronicle_band_budget() -> int:
    """一次あらすじの標準被覆字数 U (env ``SAIVERSE_CHRONICLE_BAND_BUDGET``)。"""
    return _env_int("SAIVERSE_CHRONICLE_BAND_BUDGET", DEFAULT_TARGET_CHARS)


def message_episode_ref(msg: Message) -> Optional[str]:
    """メッセージの episode 帰属 (層0タグ) を読む。

    origin_episode 専用列を優先し、無ければ metadata JSON へフォールバック
    する。get_messages_for_chronicle の SELECT は 2026-08-25 から他の読み出しと
    同じ専用列込みになったので、専用列を持つ世代の行は専用列側で解決される
    (フォールバックは専用列の導入前に書かれた行のため)。
    """
    ref = getattr(msg, "origin_episode", None)
    if ref:
        return str(ref)
    meta = getattr(msg, "metadata", None)
    if isinstance(meta, dict):
        value = meta.get("origin_episode")
        if value:
            return str(value)
    return None


def coverage_chars(messages: Sequence[Message]) -> int:
    """被覆生ログ文字数 (あらすじ→被覆元の錨・統計。digest 自身の長さではない)。"""
    return sum(len(m.content or "") for m in messages)


@dataclass
class PlannedChunk:
    """整列計画の 1 チャンク = 生成される一次あらすじ 1 個。

    ``group_key``: このチャンクが属する fold 群 (``run_groups`` 由来の所属キー。
    群なし = None)。退場付記 (executor) が「付記スパンの敷き詰めは同一群の中
    だけ」(perception_buffer.md §10.4) を守るために運ぶ — 群と群の間には生きた
    提示中の範囲が挟まりうるので、群を跨いで期間を敷き詰めると提示中の知覚を
    先取りで digest へ畳んでしまう。
    """

    kind: str  # 常に CHUNK_LLM_BATCH
    messages: List[Message]
    episode_refs: List[str] = field(default_factory=list)
    coverage_chars: int = 0
    group_key: object = None

    @property
    def message_ids(self) -> List[str]:
        return [m.id for m in self.messages]


@dataclass
class AlignmentPlan:
    """plan_alignment の出力 — 生成経路と見積もりの共通語彙。"""

    chunks: List[PlannedChunk]
    total_unprocessed: int  # 未編纂メッセージ総数 (全 run 合計)

    @property
    def llm_calls(self) -> int:
        """LLM を要するチャンク数 (コスト見積もり)。"""
        return len(self.chunks)

    @property
    def summary(self) -> Dict[str, int]:
        """台帳 RESULT_JSON 用の要約。"""
        return {
            "chunks_total": len(self.chunks),
            "chunks_llm": len(self.chunks),
            "total_unprocessed": self.total_unprocessed,
        }


def plan_alignment(
    messages: Sequence[Message],
    processed_ids: Set[str],
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    run_groups: Optional[Sequence[Sequence[str]]] = None,
) -> AlignmentPlan:
    """未編纂メッセージ列をチャンク列に計画する。

    Args:
        messages: 編纂対象候補 (created_at 昇順。除外フィルタ・退場範囲の絞り込みは
            呼び出し側で適用済み)。
        processed_ids: 既に一次あらすじの source になっている message id 集合。
        target_chars: 一次あらすじ チャンクの標準被覆 (U)。
        run_groups: 編纂範囲の群 (呼び出し側の退場 fold ごとの message id 列)。
            **別の群に属するメッセージ同士は束ねない**。切れ目は各メッセージ
            **自身の所属**で判定するので、群の先頭が Chronicle 除外対象
            (除外タグ / line_role / Stelis スレッド) で ``messages`` に居なくても
            境界は立つ。契約は「``messages`` の各 id がちょうど一つの群に属する」
            で、破れた入力は束ねない側へ倒して WARNING を出す (詳細は
            :func:`_run_group_keys`)。None なら processed 挟みだけで run が切れる。

    Returns:
        AlignmentPlan。チャンクは時系列順。

    スペルの群 (``spell_origin_id`` の印) を割らない責任は run の**内側**だけに
    ある (:func:`_plan_run`)。ここの run 分割 (processed 挟み / 群の所属変化 /
    thread 変化) が群を割る入力は、呼び出し側の絞り込みか既存データの形が
    そうなっているということで、整列側では救えない — 束ねない側へ倒す不変条件
    (偽の隣接の禁止) の方が優先する。
    """
    # 1. 連続 run 化: processed を跨ぐ束ねはしない。呼び出し側が指定した
    #    範囲の群 (run_groups) が変わるところでも切る。
    #    群は「境界になる id」ではなく **所属**で持つ — 群の先頭 id を境界に
    #    使う形は、その先頭が Chronicle 除外対象で messages に居ないと境界が
    #    一度も立たず、離れた範囲が黙って一つのあらすじに混ざる
    #    (docs/issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md)。
    #    さらに **thread 境界でも必ず切る** — created_at 一列化で並走スレッドの
    #    発話が交互に並んでいても、別スレッドの発話を一つのあらすじに束ねない
    #    (docs/issues/chronicle_cross_thread_mixing.md の下限。時系列の嘘 =
    #    「γ の後に δ をやった」という偽の隣接を生成物へ焼き込まないための
    #    安全装置で、どの上位設計 — thread 単位取得 / episode 単位ソート —
    #    を採っても成立し続ける不変条件)。
    group_keys = _run_group_keys(messages, run_groups)
    runs: List[tuple] = []  # (group_key, List[Message])
    current: List[Message] = []
    current_group: object = None
    current_thread: object = None
    for msg in messages:
        if msg.id in processed_ids:
            if current:
                runs.append((current_group, current))
                current = []
                current_group = None
                current_thread = None
            continue
        group = group_keys.get(msg.id)
        thread = getattr(msg, "thread_id", None)
        if current and (group != current_group or thread != current_thread):
            runs.append((current_group, current))
            current = []
        current.append(msg)
        current_group = group
        current_thread = thread
    if current:
        runs.append((current_group, current))

    chunks: List[PlannedChunk] = []
    for group, run in runs:
        chunks.extend(_plan_run(run, target_chars=target_chars, group_key=group))

    return AlignmentPlan(
        chunks=chunks,
        total_unprocessed=sum(len(r) for _, r in runs),
    )


def _run_group_keys(
    messages: Sequence[Message],
    run_groups: Optional[Sequence[Sequence[str]]],
) -> Dict[str, object]:
    """message id -> 所属群キー (run 分割の判定材料)。

    ``run_groups`` が None なら空 dict — 全メッセージが同じ「群なし」に
    なり、群による分割は起きない (全量整理の従来経路)。

    群を渡す側の契約は「編纂対象の各 id がちょうど一つの群に属する」
    (``EvictionPlan.folds`` は時系列順・非重複、呼び出し側は群の和集合で
    編纂対象を絞る)。契約が破れた id は**所属が決まらない** — そこで
    どちらの群に寄せても、寄せた先の隣と束ねる根拠が無い。だから
    **1 件ずつ孤立させ、前後のどちらとも束ねない**:

    - 複数の群に現れた id (所属が二つ以上ある)
    - どの群にも属さない id (所属が無い)

    どちらも呼び出し側の欠陥なので WARNING で必ず可視化する。

    倒す向きの根拠: 編纂ごと止めない (壊れた計画が出続ける限り anchor が
    永久に進まない)。孤立片も小さなチャンクとして必ず編纂されるので、
    「退場したものは必ず編纂されている」下限は保たれる。偽の隣接 (時系列の嘘)
    だけは出さない。
    """
    if run_groups is None:
        return {}

    index_of: Dict[str, int] = {}
    duplicated: Set[str] = set()
    for index, group in enumerate(run_groups):
        for mid in group:
            previous = index_of.get(mid)
            if previous is None:
                index_of[mid] = index
            elif previous != index:
                duplicated.add(mid)

    keys: Dict[str, object] = {}
    unassigned: List[str] = []
    for msg in messages:
        if msg.id in duplicated:
            # 所属が二つ以上 = 決められない。他のどの id とも一致しないキー。
            keys[msg.id] = ("ambiguous", msg.id)
        elif msg.id in index_of:
            keys[msg.id] = index_of[msg.id]
        else:
            unassigned.append(msg.id)
            keys[msg.id] = ("unassigned", msg.id)

    if duplicated:
        LOGGER.warning(
            "[alignment] run_groups の %d 件が複数の群に属している (所属を "
            "決められないので孤立させる): %s — 呼び出し側の退場計画が重なっている",
            len(duplicated), sorted(duplicated)[:10],
        )
    if unassigned:
        LOGGER.warning(
            "[alignment] 編纂対象の %d 件が run_groups のどの群にも属さない "
            "(1 件ずつ孤立させる): %s — 呼び出し側の絞り込みと群が食い違っている",
            len(unassigned), unassigned[:10],
        )
    return keys


def truncate_plan(plan: AlignmentPlan, max_messages: int) -> AlignmentPlan:
    """先頭から累計メッセージ数 ``max_messages`` 以内のチャンクに切り詰める。

    UI の手動生成 (「最大 N 件まで処理」) 用。チャンクは分割しない —
    上限を超える最初のチャンクの手前で止める。ただし 1 個目のチャンクが単独で
    上限を超える場合はそれだけ実行する (0 件では「処理した」と言えない)。
    ``max_messages <= 0`` は無制限 (そのまま返す)。
    """
    if max_messages <= 0:
        return plan
    kept: List[PlannedChunk] = []
    used = 0
    for chunk in plan.chunks:
        if kept and used + len(chunk.messages) > max_messages:
            break
        kept.append(chunk)
        used += len(chunk.messages)
        if used >= max_messages:
            break
    return AlignmentPlan(chunks=kept, total_unprocessed=plan.total_unprocessed)


def _plan_run(
    run: List[Message],
    *,
    target_chars: int,
    group_key: object = None,
) -> List[PlannedChunk]:
    """1 つの連続 run をチャンク列にする。

    被覆 ``target_chars`` (U) に達したところでチャンクを閉じる。末尾の端数は
    直前のチャンクに吸収する (無ければ小さなチャンクのまま — 小さくても
    要約する)。

    ただし**スペルの群の内側では閉じない** — 「唱え → 結果 → 結果を読んだ発話」
    のひとまとまり (``spell_origin_id`` の印) は、群の最後のメンバーまで含めて
    から閉じる。ここのチャンクの切れ目は「冷えた anchor の前進」
    (arasuji_levels.md §14-2) 経由で退場の境目になるので、群の途中で閉じると
    anchor の前進先が群の内側に落ち、唱えを失った結果だけが窓に残る。
    """
    # 群の内側 (最初のメンバー .. 最後のメンバーの 1 つ手前) では閉じない。
    # 群の最後のメンバーの位置では閉じてよい。
    blocked: Set[int] = set()
    for lo, hi in spell_group_spans(
        [(m.id, getattr(m, "spell_origin_id", None)) for m in run]
    ):
        blocked.update(range(lo, hi))

    def _make(msgs: List[Message]) -> PlannedChunk:
        return PlannedChunk(
            kind=CHUNK_LLM_BATCH,
            messages=msgs,
            episode_refs=_distinct_refs(msgs),
            coverage_chars=coverage_chars(msgs),
            group_key=group_key,
        )

    out: List[PlannedChunk] = []
    pending: List[Message] = []
    for index, msg in enumerate(run):
        pending.append(msg)
        if coverage_chars(pending) >= target_chars and index not in blocked:
            out.append(_make(pending))
            pending = []
    if pending:
        if out:
            # 端数吸収: 直前のチャンクへ足す。
            prev = out.pop()
            out.append(_make(prev.messages + pending))
        else:
            out.append(_make(pending))
    return out


def _distinct_refs(msgs: List[Message]) -> List[str]:
    """チャンクが被覆する episode_ref の一覧 (出現順・重複なし)。"""
    seen: Set[str] = set()
    refs: List[str] = []
    for msg in msgs:
        ref = message_episode_ref(msg)
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs
