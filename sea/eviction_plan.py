"""退場計画 — episode 単位・文字数三水位で「何を畳んで退場させるか」を決める。

docs/intent/chronicle_eviction.md の §3 (規範) / §4 (三水位) / §5 (手続き) の実装。
**純関数のみ** — DB も LLM も触らない。呼び出し側 (sea/session_lifecycle.py) が
提示中の提示コンテキストと open episode 集合を渡し、返った計画を適用する。

設計の芯 (chronicle_eviction.md §1〜§3):

- 守るのは「軽量化」ではなく**記憶の連続性**。退場したものは必ず編纂されている
  (下限) — だから退場は必ず digest 化とセットで計画する。
- 混ぜてよい境界は時刻の一本線ではなく **episode の開閉状態**。open episode は
  単独で畳み (pulse 関節で刻む §6)、closed 同士は episode をまたいで束ねてよい。
- 扱いを分ける根拠は kind (会話か作業か) ではなく **量 (U に達したか)**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Watermarks:
    """Metabolism の三水位 (文字数)。chronicle_eviction.md §4。

    - ``low``: 直近の保護範囲。最新から遡ってこの文字数は退場させない。
    - ``target``: Metabolism で軽くする到達点。
    - ``high``: 提示コンテキストがこれを超えたら発火。None = 文字数では発火せず
      ``token_triggered`` のみ。
    """

    low: int
    target: int
    high: Optional[int] = None


@dataclass
class Fold:
    """一度に畳んで退場させる連続メッセージ列 = 一次あらすじ 1 個ぶんの範囲。"""

    messages: List[Dict[str, Any]]
    #: この fold が「1 つの open episode の部分退場」ならその episode_ref。
    #: 設定されているとき、呼び出し側は §6 の pulse 関節細分 (子 episode 化) を行う。
    open_episode_ref: Optional[str] = None
    #: 束ねた closed / 無帰属の episode_ref 一覧 (ログ・記録用)。
    episode_refs: List[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(str(m.get("content") or "")) for m in self.messages)

    @property
    def message_ids(self) -> List[str]:
        return [str(m.get("id")) for m in self.messages]

    @property
    def start_at(self) -> Optional[int]:
        return _epoch(self.messages[0]) if self.messages else None

    @property
    def end_at(self) -> Optional[int]:
        return _epoch(self.messages[-1]) if self.messages else None


@dataclass
class EvictionPlan:
    """:func:`plan_eviction` の出力。"""

    #: 畳んで退場させる範囲 (時系列順、互いに重ならない)。
    folds: List[Fold] = field(default_factory=list)
    #
    # どの fold が anchor 前進になり、どれが提示コンテキストの中の圧縮区間になるかは**ここでは
    # 決めない** — 判定には置き換え前の生ログの並びが要り、それを持っているのは
    # 適用側 (sea/session_lifecycle.py の ``_apply_eviction_plan``) だから。
    # 同じ真実を二箇所に置かない。
    #
    #: U に届かない範囲を「最後の手段」として畳んだか (§5-5)。
    #: 観測用 — この経路が定常運転になっていないかをログで見るための印。
    used_last_resort_fold: bool = False
    #: 計画適用後に残る提示文字数の見込み。
    projected_chars: int = 0
    #: 適用前の提示文字数。
    total_chars: int = 0
    #: 保護範囲の開始インデックス (この位置以降は退場させない)。
    protected_from: int = 0

    @property
    def evicted_count(self) -> int:
        return sum(len(f.messages) for f in self.folds)

    @property
    def is_empty(self) -> bool:
        return not self.folds


#: 畳んだ範囲が提示コンテキストに残す置き換えメッセージの見込み文字数。
#:
#: 畳んでも提示は 0 にならない — その位置に「あらすじ + 圧縮マークの注釈」が立つ
#: (§6)。実際の長さは生成してみるまで分からないので、計画では固定の見込みで
#: 差し引く。**多めに見積もる**方が安全 (少なく見るとその分だけ目標に届かず、
#: 次のターンでまた発火する)。注釈が約 150 字、あらすじ本文が U の 1 割弱。
ESTIMATED_FOLD_PLACEHOLDER_CHARS = 1_200


def message_chars(messages: Sequence[Dict[str, Any]]) -> int:
    """提示文字数 (水位判定の一次データ)。"""
    return sum(len(str(m.get("content") or "")) for m in messages)


def _net_reduction(messages: Sequence[Dict[str, Any]]) -> int:
    """この範囲を畳んで**実際に減る**提示文字数。

    畳んだ位置には置き換えが 1 通立つので、生ログの文字数まるごとは減らない。
    見込みが元より大きい (極端に小さい範囲) 場合は 0 に丸める。
    """
    return max(0, message_chars(messages) - ESTIMATED_FOLD_PLACEHOLDER_CHARS)


def _epoch(msg: Dict[str, Any]) -> Optional[int]:
    try:
        return int(msg.get("created_at"))
    except (TypeError, ValueError):
        return None


def episode_ref_of(msg: Dict[str, Any]) -> Optional[str]:
    """メッセージの episode 帰属 (層0タグ origin_episode)。無帰属は None。"""
    meta = msg.get("metadata")
    ref = meta.get("origin_episode") if isinstance(meta, dict) else None
    if not ref:
        ref = msg.get("origin_episode")
    return str(ref) if ref else None


def _pulse_of(msg: Dict[str, Any]) -> Optional[str]:
    value = msg.get("pulse_id")
    return str(value) if value else None


def _protection_boundary(
    messages: Sequence[Dict[str, Any]], low_chars: int,
) -> int:
    """直近の保護範囲の開始インデックスを返す (§5-1、§5-4)。

    最新側から遡って ``low_chars`` 分を保護する。境界が pulse の途中に落ちたら
    **古い側へ** 関節まで下げる — メッセージ単位でぶつ切りにせず、保護範囲を広げる
    方向に倒す (「刻むときは pulse を丸ごと」experience_structure.md §6)。
    """
    if low_chars <= 0:
        return len(messages)
    acc = 0
    boundary = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += len(str(messages[i].get("content") or ""))
        if acc >= low_chars:
            boundary = i
            break
    else:
        # 提示コンテキスト全体でも低水位に届かない = 全部が保護範囲。
        return 0
    # pulse 関節へスナップ (古い側 = 保護範囲を広げる向き)。
    pulse = _pulse_of(messages[boundary])
    if pulse is not None:
        while boundary > 0 and _pulse_of(messages[boundary - 1]) == pulse:
            boundary -= 1
    return boundary


def _is_folded_placeholder(msg: Dict[str, Any]) -> bool:
    """既に digest へ置き換わっている位置か (sea/session_window.py の置き換え)。"""
    meta = msg.get("metadata")
    return bool(isinstance(meta, dict) and meta.get("__folded_range__"))


@dataclass
class _Unit:
    """退場候補範囲を「同一 episode 帰属の連続域」で切った束ねの原子。"""

    messages: List[Dict[str, Any]]
    ref: Optional[str]
    is_open: bool
    #: 既に畳まれた digest = 壁。**同一レベルでは**再圧縮しないし (§4-4 — 一次あらすじの
    #: digest から別の一次あらすじを作らない。上の次数への束ねは bands.py の仕事で正当)、
    #: これを跨いで前後を束ねることもしない (跨ぐと非連続な束ね §4-5 になる)。
    is_wall: bool = False

    @property
    def chars(self) -> int:
        return message_chars(self.messages)


def _build_units(
    candidates: Sequence[Dict[str, Any]], open_refs: Set[str],
) -> List[_Unit]:
    """退場候補範囲を束ねの原子に割る。

    原子 = 「同一 episode 帰属の連続域」または「無帰属メッセージ 1 件」。無帰属は
    episode の制約が無いのでどこでも切れる (sai_memory/arasuji/alignment.py の
    ``_atoms`` と同じ規約 — 束ねの粒度を二箇所で食い違わせない)。
    """
    units: List[_Unit] = []
    for msg in candidates:
        if _is_folded_placeholder(msg):
            units.append(_Unit(messages=[msg], ref=None, is_open=False, is_wall=True))
            continue
        ref = episode_ref_of(msg)
        if (
            ref is not None
            and units
            and units[-1].ref == ref
            and not units[-1].is_wall
        ):
            units[-1].messages.append(msg)
            continue
        units.append(
            _Unit(messages=[msg], ref=ref, is_open=bool(ref and ref in open_refs))
        )
    return units


def _pulse_groups(messages: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """連続メッセージを pulse (認知の一巡) 単位に束ねる。

    pulse_id が無いメッセージは単独の群として扱う (関節を勝手に作らない)。
    """
    groups: List[List[Dict[str, Any]]] = []
    current_pulse: Optional[str] = None
    for msg in messages:
        pulse = _pulse_of(msg)
        if groups and pulse is not None and pulse == current_pulse:
            groups[-1].append(msg)
            continue
        groups.append([msg])
        current_pulse = pulse
    return groups


def _anchor_blocked_at(units: Sequence["_Unit"], upto: int, consumed: Set[int]) -> bool:
    """``upto`` の位置が「今回 anchor を止めている先頭」か (最後の手段の前提)。

    真になる条件は二つとも要る:

    - 手前が**既存の置き換え (壁) だけ**であること。畳めない生ログが手前に残って
      いると、そこを畳んでも anchor は前進しないうえ、作った置き換えが壁になって
      手前は以後どの束ねにも入れなくなる (新しいメッセージは末尾に来るので手前は
      成長しない) = anchor が**恒久的に**詰まる。
    - 手前に**今回のラウンドで畳んだものが無い**こと。既に畳めているなら anchor は
      その分だけ前進するので、最後の手段は要らない。ここを ``consumed`` も吸収済み
      に数えると、普通に畳めた回でも候補の末尾の端数を畳んでしまい、**日常経路で
      小粒の一次あらすじを作る** (issue chronicle_undersized_lv1_chunks が消そうと
      しているもの)。観測用の ``used_last_resort_fold`` も嘘になる。
    """
    if any(j in consumed for j in range(upto)):
        return False
    return all(units[j].is_wall for j in range(upto))


def _run_ends_at(units: Sequence["_Unit"], idx: int) -> bool:
    """``idx`` で closed の連続 run が終わるか (次が壁 / open / 末尾)。"""
    nxt = idx + 1
    if nxt >= len(units):
        return True
    return units[nxt].is_wall or units[nxt].is_open


def plan_eviction(
    messages: Sequence[Dict[str, Any]],
    open_episode_refs: Set[str],
    watermarks: Watermarks,
    *,
    target_chars: int,
) -> EvictionPlan:
    """提示中の提示コンテキストから「今回退場させる範囲」を計画する (chronicle_eviction.md §5)。

    Args:
        messages: 提示中の提示コンテキスト (created_at 昇順、既に畳んだ圧縮区間は digest に置換済み)。
        open_episode_refs: いま開いている episode_ref の集合。
        watermarks: 三水位 (低 = 保護範囲 / 目標 = 到達点 / 高 = 発火。発火判定は
            呼び出し側の責務で、ここでは low / target だけを使う)。
        target_chars: 一次あらすじの標準被覆 U。1 つの fold が目指す大きさ。

    計画は二段で立てる (§5-5)。

    1. **U 以上のまとまりだけ**を畳んで目標水位を狙う。届いたら終わり。
    2. 届かなければ、**未畳みの先頭にある範囲を種類も大きさも問わず畳む**。一回だけ。

    二段目が要る理由: 提示コンテキストの先頭に「まだ終わっていない episode の、
    U に届かない端数」が居座ると、その後ろをいくら畳んでも **anchor が永久に
    進まない**。後ろの畳みは置き換えを残すだけなので、提示は減らないまま
    圧縮区間が積もる。U に届いたら普通に畳むのに、届かないときだけ生で死守する
    のは判断基準として一貫していない — **U は「優先度」の材料であって「畳んで
    いいか」の材料ではない**。

    一回だけで足りる理由: 適用側は「先頭から連続して畳まれている分」を丸ごと
    anchor で飲み込む。だから詰まっていた先頭が畳めた瞬間、後ろに溜まっていた
    圧縮区間ごと連鎖して提示コンテキストから出る。二段目の削減量は計画の算術に
    は現れない (端数は恒等圧縮で正味 0 になりうる) ので、**削減量で価値を測って
    飛ばしてはいけない**。

    Returns:
        EvictionPlan。
    """
    total = message_chars(messages)
    plan = EvictionPlan(total_chars=total, projected_chars=total)
    if not messages:
        return plan
    if target_chars <= ESTIMATED_FOLD_PLACEHOLDER_CHARS:
        # U が置き換えの見込みより小さいと、1 束あたりの正味削減が常に 0 になり、
        # 「減らないのに畳み続ける」= 候補範囲を出し切る過剰退場になる。設定ミス
        # (SAIVERSE_CHRONICLE_BAND_BUDGET を極端に下げた等) なので退場を見送る。
        LOGGER.warning(
            "[eviction] target_chars=%d is not larger than the fold placeholder "
            "estimate (%d); skipping eviction (check SAIVERSE_CHRONICLE_BAND_BUDGET)",
            target_chars, ESTIMATED_FOLD_PLACEHOLDER_CHARS,
        )
        return plan

    boundary = _protection_boundary(messages, watermarks.low)
    plan.protected_from = boundary
    if boundary <= 0:
        # 退場候補範囲が無い = 直近の保護範囲だけで提示コンテキストが埋まっている。削れない。
        return plan

    candidates = list(messages[:boundary])
    units = _build_units(candidates, open_episode_refs)

    remaining = total
    folds: List[Fold] = []
    consumed: Set[int] = set()   # 消費済み unit index
    #: open episode の unit は先頭から順に食う (途中まで畳んだ位置を覚える)
    open_offset: Dict[int, int] = {}

    # 二段構え: まず U 以上だけ (allow_undersized_open=False)、それで目標に
    # 届かなければ U 未満の open も一回だけ許す (True)。
    for allow_undersized_open in (False, True):
        if remaining <= watermarks.target:
            break
        last_resort_used = False
        progressed = True
        while remaining > watermarks.target and progressed:
            progressed = False
            pending: List[Dict[str, Any]] = []
            pending_refs: List[str] = []
            pending_idx: List[int] = []

            for idx, unit in enumerate(units):
                if idx in consumed:
                    # 既に畳んだ位置 = 置き換えが立つ = 壁。透明に素通りさせると
                    # 前後の closed がこの位置を飛び越えて束ねられてしまう
                    # (§4-5 偽の隣接)。**途中まで畳んだ open は consumed に入らない**
                    # ので、そちらは is_open の分岐が同じ役目を果たす (必ず
                    # pending をリセットする) — 残りは計画対象に残る。
                    pending, pending_refs, pending_idx = [], [], []
                    continue

                if unit.is_wall:
                    # 畳み済みの digest は再圧縮しない (§4-4)。跨いだ束ねも作らない。
                    pending, pending_refs, pending_idx = [], [], []
                    continue

                if unit.is_open:
                    # open episode は単独で畳む (§3) — ここまで貯めた closed 束ねは
                    # U 未満のまま打ち切り、捨てる。open を挟んで前後をつなぐと
                    # 非連続な束ね (§4-5 違反) になるため、またがせない。
                    pending, pending_refs, pending_idx = [], [], []

                    offset = open_offset.get(idx, 0)
                    rest = unit.messages[offset:]
                    if not rest:
                        consumed.add(idx)
                        continue
                    # pulse 関節で刻む (§6): 丸ごと退場済みの pulse 群だけを畳む。
                    taken: List[Dict[str, Any]] = []
                    for group in _pulse_groups(rest):
                        taken.extend(group)
                        if message_chars(taken) >= target_chars:
                            break
                    if message_chars(taken) < target_chars:
                        # U に届かない端数。一段目では畳まず次の unit を見る。
                        if not allow_undersized_open or last_resort_used:
                            continue
                        # 手前に畳めない範囲が残っているなら、ここを畳んでも
                        # anchor は前進しない。しかも作った置き換えが壁になって
                        # 手前の範囲は以後どことも束ねられなくなる = anchor が
                        # **恒久的に**詰まる。端数圧縮は「手前が全部吸収済み」の
                        # open に限る (Codex レビュー P1)。
                        if not _anchor_blocked_at(units, idx, consumed):
                            continue
                        # 二段目 = 最後の手段。ここを畳まないと anchor が進まない。
                        # 一回だけ (先頭が畳めれば適用側の anchor 前進が後ろの
                        # 圧縮区間ごと連鎖して飲み込むので、これで足りる)。
                        last_resort_used = True
                        plan.used_last_resort_fold = True
                        LOGGER.info(
                            "[eviction] folding an undersized open episode as a last "
                            "resort (ref=%s, %d chars < U=%d): otherwise the anchor "
                            "cannot advance past it",
                            unit.ref, message_chars(taken), target_chars,
                        )
                    folds.append(
                        Fold(messages=taken, open_episode_ref=unit.ref,
                             episode_refs=[unit.ref] if unit.ref else [])
                    )
                    open_offset[idx] = offset + len(taken)
                    if open_offset[idx] >= len(unit.messages):
                        consumed.add(idx)
                    remaining -= _net_reduction(taken)
                    progressed = True
                    break

                # closed / 無帰属: episode をまたいで束ねてよい (§3)。
                pending.extend(unit.messages)
                pending_idx.append(idx)
                if unit.ref:
                    pending_refs.append(unit.ref)
                reached_u = message_chars(pending) >= target_chars
                last_resort_run = (
                    allow_undersized_open
                    and not last_resort_used
                    and bool(pending)
                    and _run_ends_at(units, idx)
                    and _anchor_blocked_at(units, pending_idx[0], consumed)
                )
                if reached_u or last_resort_run:
                    if not reached_u:
                        # U に届かない closed の run。ここが未畳みの先頭なので、
                        # 畳まないと anchor が進まない。open だけを最後の手段に
                        # するのは根拠のない線引きで、「先頭が U 未満の closed +
                        # 直後が U 未満の open」で永久に手詰まりになる
                        # (Codex レビュー二巡目 P1)。
                        last_resort_used = True
                        plan.used_last_resort_fold = True
                        LOGGER.info(
                            "[eviction] folding an undersized closed run as a last "
                            "resort (%d chars < U=%d): otherwise the anchor cannot "
                            "advance past it", message_chars(pending), target_chars,
                        )
                    folds.append(
                        Fold(messages=list(pending), episode_refs=list(pending_refs))
                    )
                    consumed.update(pending_idx)
                    remaining -= _net_reduction(pending)
                    progressed = True
                    break

    # 二段構えなので **発見順 ≠ 時系列順**: 一段目で後ろの範囲を畳み、二段目で
    # 先頭の端数を畳むと [後, 先] の順で積まれる。folds の契約は時系列順で、
    # 適用側はこの順に子 episode を刻み short_id を採る — 逆順のまま返すと
    # 記録の並びが体験の並びと食い違う (レビュー三巡目 P2)。元の位置で並べ直す。
    position = {str(m.get("id")): i for i, m in enumerate(messages)}
    folds.sort(
        key=lambda f: min(
            (position.get(mid, len(messages)) for mid in f.message_ids),
            default=len(messages),
        )
    )
    plan.folds = folds
    plan.projected_chars = remaining
    return plan
