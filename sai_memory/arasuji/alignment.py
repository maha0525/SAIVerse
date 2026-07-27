"""Episode-aligned chunk planning for Chronicle generation (体験の構造 工程(2)).

docs/intent/experience_structure.md §4 (圧縮七原則) / §5 (バッチ降格) の実装。
確定設計は docs/handoff/2026-07-21_w4_metabolism_ledger_handoff.md D3。

このモジュールは**純関数のみ** — LLM 呼び出しも DB 書き込みもしない。
生成経路 (sea/session_lifecycle.generate_chronicle) とコスト見積もり
(sai_memory/arasuji/estimate) が同じ整列計画を共有する (一点管理)。

sai_memory は SAIVerse 本体 (world DB) に依存しない層のため、episode の
情報は素の Mapping / Set で受け取る:

- ``episode_digests``: digest 確定済み episode の ``episode_ref ->
  (digest_message_id, digest_text)``。呼び出し側 (session_lifecycle) が
  world DB の Episode (closed + DIGEST_REF='message:{id}') と memory.db の
  digest 本文から組み立てる。digest 本文が引けない ref はこの index に
  入れない — その範囲は通常材料として圧縮される (digest が失われた土地を
  未編纂のまま放置しない)。

圧縮七原則との対応 (テストは test_arasuji_alignment.py が原則番号で固定):

- §4-1 退場時圧縮: 対象範囲の切り出しは呼び出し側 (evict 境界)。本モジュール
  は渡された範囲だけを計画する。
- §4-2 原子であって量子ではない: episode は分割しない (巨大 episode は単独
  チャンク)。結合は可 — 隣接 episode / 無帰属メッセージを target_chars まで
  束ね、端数は直前の束ねチャンクに吸収する。
- §4-3 恒等圧縮: min_llm_chars 未満で束ねる相手もいない残片は identity
  チャンク (生のまま、LLM なし)。
- §4-4 同一レベルの再圧縮禁止: digest 確定済み episode は episode_digest
  チャンク (恒等転写・単独) — 前後の束ねに巻き込まない。
- §4-5 連続束ねのみ: 編纂済み (processed) メッセージを跨いだ束ねをしない
  (run 分割)。時系列に並んだものだけを束ねる。呼び出し側が退場範囲を
  ``run_groups`` で群に分けて渡した場合は、群をまたぐ束ねもしない
  (提示コンテキストの途中を畳むと編纂範囲が不連続になるため)。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sai_memory.memory.storage import Message

LOGGER = logging.getLogger(__name__)

# チャンク種別 (metadata.digest_origin にそのまま記録される — 出自の語彙は
# experience_structure.md §3.2)
CHUNK_IDENTITY = "identity"
CHUNK_EPISODE_DIGEST = "episode"
CHUNK_LLM_BATCH = "batch"

#: 一次あらすじの標準被覆字数 U (§11-8 モック検証済み確定値)。
DEFAULT_TARGET_CHARS = 10_000
#: LLM 圧縮する最小被覆字数。未満は恒等圧縮 (Track Chronicle の 1000 字
#: スキップの一般化 — §4-3)。
DEFAULT_MIN_DIGEST_CHARS = 1_000
#: 次数の底 B (幾何級数)。列のあふれ束ね (bands.py) と共有する。
DEFAULT_BAND_BASE = 10


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


def chronicle_band_base() -> int:
    """次数の底 B (env ``SAIVERSE_CHRONICLE_BAND_BASE``)。"""
    return _env_int("SAIVERSE_CHRONICLE_BAND_BASE", DEFAULT_BAND_BASE, minimum=2)


def chronicle_min_digest_chars() -> int:
    """LLM 圧縮の最小被覆字数 (env ``SAIVERSE_CHRONICLE_MIN_DIGEST_CHARS``)。"""
    return _env_int("SAIVERSE_CHRONICLE_MIN_DIGEST_CHARS", DEFAULT_MIN_DIGEST_CHARS)


def message_episode_ref(msg: Message) -> Optional[str]:
    """メッセージの episode 帰属 (層0タグ) を読む。

    origin_episode 専用列を優先し、無ければ metadata JSON へフォールバック
    する (get_messages_for_chronicle の SELECT は 7 列で専用列を含まないため、
    実運用ではフォールバック側が主経路になる)。
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
    """被覆生ログ文字数 (束ね・次数判定の一次データ。digest 自身の長さではない)。"""
    return sum(len(m.content or "") for m in messages)


@dataclass
class PlannedChunk:
    """整列計画の 1 チャンク = 生成される一次あらすじ 1 個。"""

    kind: str  # CHUNK_IDENTITY | CHUNK_EPISODE_DIGEST | CHUNK_LLM_BATCH
    messages: List[Message]
    episode_refs: List[str] = field(default_factory=list)
    coverage_chars: int = 0
    # CHUNK_EPISODE_DIGEST のみ: 転写元 (episodes.digest_ref が指す message)
    digest_message_id: Optional[str] = None
    digest_text: Optional[str] = None

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
        return sum(1 for c in self.chunks if c.kind == CHUNK_LLM_BATCH)

    @property
    def summary(self) -> Dict[str, int]:
        """台帳 RESULT_JSON 用の要約 (D10)。"""
        counts: Dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.kind] = counts.get(chunk.kind, 0) + 1
        return {
            "chunks_total": len(self.chunks),
            "chunks_llm": counts.get(CHUNK_LLM_BATCH, 0),
            "chunks_identity": counts.get(CHUNK_IDENTITY, 0),
            "chunks_episode": counts.get(CHUNK_EPISODE_DIGEST, 0),
            "total_unprocessed": self.total_unprocessed,
        }


def plan_alignment(
    messages: Sequence[Message],
    processed_ids: Set[str],
    episode_digests: Mapping[str, Tuple[str, str]],
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    min_llm_chars: int = DEFAULT_MIN_DIGEST_CHARS,
    run_groups: Optional[Sequence[Sequence[str]]] = None,
) -> AlignmentPlan:
    """未編纂メッセージ列を episode 整列チャンク列に計画する。

    Args:
        messages: 編纂対象候補 (created_at 昇順。除外フィルタ・退場範囲の絞り込みは
            呼び出し側で適用済み)。
        processed_ids: 既に一次あらすじの source になっている message id 集合。
        episode_digests: digest 確定済み episode の
            ``episode_ref -> (digest_message_id, digest_text)``。
        target_chars: 一次あらすじ チャンクの標準被覆 (U)。
        min_llm_chars: LLM 圧縮する最小被覆 (未満は恒等圧縮)。
        run_groups: 編纂範囲の群 (呼び出し側の退場 fold ごとの message id 列)。
            **別の群に属するメッセージ同士は束ねない**。退場が episode 単位に
            なり、一度の編纂で時系列上離れた範囲を扱うようになったため、範囲の
            切れ目を明示して「離れたものを一つのあらすじに束ねない」
            (§4-5 連続束ねのみ) を守る。切れ目は各メッセージ**自身の所属**で
            判定するので、群の先頭が Chronicle 除外対象 (除外タグ /
            line_role / Stelis スレッド) で ``messages`` に居なくても境界は
            立つ。契約は「``messages`` の各 id がちょうど一つの群に属する」で、
            破れた入力は束ねない側へ倒して WARNING を出す (詳細は
            :func:`_run_group_keys`)。None なら従来どおり processed 挟みだけで
            run が切れる。

    Returns:
        AlignmentPlan。チャンクは時系列順。
    """
    # 1. 連続 run 化 (§4-5): processed を跨ぐ束ねはしない。呼び出し側が指定した
    #    範囲の群 (run_groups) が変わるところでも切る。
    #    群は「境界になる id」ではなく **所属**で持つ — 群の先頭 id を境界に
    #    使う形は、その先頭が Chronicle 除外対象で messages に居ないと境界が
    #    一度も立たず、離れた範囲が黙って一つのあらすじに混ざる
    #    (docs/issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md)。
    group_keys = _run_group_keys(messages, run_groups)
    runs: List[List[Message]] = []
    current: List[Message] = []
    current_group: object = None
    for msg in messages:
        if msg.id in processed_ids:
            if current:
                runs.append(current)
                current = []
                current_group = None
            continue
        group = group_keys.get(msg.id)
        if current and group != current_group:
            runs.append(current)
            current = []
        current.append(msg)
        current_group = group
    if current:
        runs.append(current)

    chunks: List[PlannedChunk] = []
    # 転写済み episode_ref は**計画全体で一度だけ** — processed 挟みで同じ
    # episode が複数 run に分裂しても、digest 転写チャンクは最初の 1 回に
    # 限る (同一 digest 本文の二重転写防止。Codex W4 #6)。
    transcribed: Set[str] = set()
    for run in runs:
        chunks.extend(
            _plan_run(
                run, episode_digests, transcribed,
                target_chars=target_chars, min_llm_chars=min_llm_chars,
            )
        )

    return AlignmentPlan(
        chunks=chunks,
        total_unprocessed=sum(len(r) for r in runs),
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

    倒す向きの根拠 (二つ):

    - **編纂ごと止めない**。壊れた計画が出続ける限り anchor が永久に進まず、
      「退場したものは必ず編纂されている」(§4-1 の下限) を守るための歯止めが
      退場そのものを止めてしまう。孤立させれば下限は保たれる (孤立片も
      恒等圧縮チャンクとして必ず編纂される)。
    - **§4-2 (episode 分割禁止) より §4-5 (連続束ねのみ) を優先する**。孤立は
      同一 episode を割ることがあるが、割ることは物語の質の劣化に留まる。
      対して偽の隣接は時系列の嘘であり、ペルソナの記憶に入れてはならない。
      契約違反時に両方は満たせないので、嘘を出さない側を採る。
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
    上限を超える最初のチャンクの手前で止める (episode の原子性 §4-2 を
    上限のためにも破らない)。ただし 1 個目のチャンクが単独で上限を超える
    場合はそれだけ実行する (0 件では「処理した」と言えない)。
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
    episode_digests: Mapping[str, Tuple[str, str]],
    transcribed: Set[str],
    *,
    target_chars: int,
    min_llm_chars: int,
) -> List[PlannedChunk]:
    """1 つの連続 run をチャンク列にする。

    ``transcribed`` は計画全体で共有される「digest 転写済み episode_ref」の
    可変集合 — 本関数が転写チャンクを立てるたびに追記する。
    """
    segments = _segment_by_digest(run, episode_digests, transcribed)

    out: List[PlannedChunk] = []
    pending: List[Message] = []

    def _make_batch(msgs: List[Message]) -> PlannedChunk:
        return PlannedChunk(
            kind=CHUNK_LLM_BATCH,
            messages=msgs,
            episode_refs=_distinct_refs(msgs),
            coverage_chars=coverage_chars(msgs),
        )

    def _flush_pending() -> None:
        """貯め込んだ undigested 列を llm_batch / identity として確定する。"""
        nonlocal pending
        if not pending:
            return
        msgs, pending = pending, []
        chars = coverage_chars(msgs)
        if chars >= min_llm_chars:
            out.append(_make_batch(msgs))
        elif out and out[-1].kind == CHUNK_LLM_BATCH:
            # 端数吸収 (§4-2 結合可): 直前の束ねチャンクへ足す。
            prev = out.pop()
            out.append(_make_batch(prev.messages + msgs))
        else:
            # 束ねる相手がいない豆粒 → 恒等圧縮 (§4-3)。
            out.append(PlannedChunk(
                kind=CHUNK_IDENTITY,
                messages=msgs,
                episode_refs=_distinct_refs(msgs),
                coverage_chars=chars,
            ))

    for key, seg_msgs in segments:
        if key is not None:
            # digest 確定済み episode: 手前を確定し、単独チャンク (§4-4)。
            _flush_pending()
            digest_mid, digest_text = episode_digests[key]
            out.append(PlannedChunk(
                kind=CHUNK_EPISODE_DIGEST,
                messages=seg_msgs,
                episode_refs=[key],
                coverage_chars=coverage_chars(seg_msgs),
                digest_message_id=digest_mid,
                digest_text=digest_text,
            ))
            continue
        # undigested: 束ねの原子 (digest 無し episode 丸ごと / 無帰属 1 件)
        # を順に足し、target 達成でチャンクを閉じる。episode の途中では
        # 閉じない (§4-2 分割禁止 — 原子の境界でのみ閉じる)。
        for atom in _atoms(seg_msgs):
            pending.extend(atom)
            if coverage_chars(pending) >= target_chars:
                msgs, pending = pending, []
                out.append(_make_batch(msgs))
    _flush_pending()
    return out


_SENTINEL = object()


def _segment_by_digest(
    run: List[Message],
    episode_digests: Mapping[str, Tuple[str, str]],
    transcribed: Set[str],
) -> List[Tuple[Optional[str], List[Message]]]:
    """run を「digest 確定済み episode の連続域 / それ以外」で分割する。

    戻り値の key は digest 確定済み episode_ref、undigested 域は None。

    同一 ref が分裂している場合 (間に別帰属や processed 済みメッセージが
    挟まる)、**計画全体で最初の連続域だけ**を episode_digest とし、2 個目
    以降は undigested (None) に降格する — 同じ digest の二重転写を防ぐ
    (残片は通常材料として圧縮される)。``transcribed`` は plan_alignment が
    全 run で共有する既転写 ref 集合 (Codex W4 #6)。
    """
    raw: List[Tuple[Optional[str], List[Message]]] = []
    cur_key: object = _SENTINEL
    cur_msgs: List[Message] = []
    for msg in run:
        ref = message_episode_ref(msg)
        key = ref if (ref and ref in episode_digests) else None
        if key != cur_key:
            if cur_msgs:
                raw.append((cur_key if cur_key is not _SENTINEL else None, cur_msgs))  # type: ignore[arg-type]
            cur_key = key
            cur_msgs = [msg]
        else:
            cur_msgs.append(msg)
    if cur_msgs:
        raw.append((cur_key if cur_key is not _SENTINEL else None, cur_msgs))  # type: ignore[arg-type]

    # 分裂片の降格 + 隣接 None の結合 (transcribed は計画全体で共有)
    merged: List[Tuple[Optional[str], List[Message]]] = []
    for key, msgs in raw:
        if key is not None:
            if key in transcribed:
                LOGGER.debug(
                    "[alignment] split episode segment demoted to batch material: %s "
                    "(%d messages)", key, len(msgs),
                )
                key = None
            else:
                transcribed.add(key)
        if merged and key is None and merged[-1][0] is None:
            merged[-1] = (None, merged[-1][1] + msgs)
        else:
            merged.append((key, msgs))
    return merged


def _atoms(msgs: List[Message]) -> List[List[Message]]:
    """undigested 域を束ねの原子に分解する。

    原子 = 「同一 episode_ref の連続域 (digest 無し episode は丸ごと)」または
    「無帰属メッセージ 1 件」。無帰属は episode 制約が無いのでどこでも
    切れる。同一 ref が非連続に再出現した場合は別原子 (稀ケース — 体験上は
    切れたものとして扱う)。
    """
    atoms: List[List[Message]] = []
    cur: List[Message] = []
    cur_ref: object = _SENTINEL
    for msg in msgs:
        ref = message_episode_ref(msg)
        if ref is None:
            if cur:
                atoms.append(cur)
                cur = []
            cur_ref = _SENTINEL
            atoms.append([msg])
            continue
        if ref == cur_ref:
            cur.append(msg)
        else:
            if cur:
                atoms.append(cur)
            cur = [msg]
            cur_ref = ref
    if cur:
        atoms.append(cur)
    return atoms


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
