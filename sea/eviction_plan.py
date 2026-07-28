"""退場計画 — レベル0 (生ログ) の「予算超過で古い側を畳む」を決める。

docs/intent/arasuji_levels.md §3 / §4 の実装 (レベル0 の並び)。
**純関数のみ** — DB も LLM も触らない。呼び出し側 (sea/session_lifecycle.py) が
提示中の提示コンテキストを渡し、返った計画を適用する。

設計の芯 (arasuji_levels.md):

- 規則は全レベル共通の一本 — 「予算の上限を超えたら発火し、古い側を
  『残す量』に収まるまで畳んで 1 つ上のレベル (一次あらすじ) へ送る」。
- 畳み材料は約 U (一次あらすじの標準被覆、既定 1 万字) ずつに刻む。切り位置は
  発言の切れ目 (pulse 関節) に寄せる — エピソードには**畳みを止める権利は無い**
  (開いているエピソードも畳む。守っていた需要の引受先は
  docs/issues/open_episode_context_after_veto_removal.md)。
- 末尾の U に届かない端数は畳まず残す — 次の Metabolism で新しい生ログと
  地続きのまま次の畳みに入るので、小さい一次あらすじを作らない。
- 発火判定 (上限) は呼び出し側の責務。ここは「残す量」だけを使う。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Watermarks:
    """Metabolism の水位 (文字数)。arasuji_levels.md §9。

    - ``target``: **残す量** — 畳んだ後、提示コンテキストに残す直近の文字数。
      退場はこの水準まで畳む (= 最新から遡ってこの分は退場させない)。
    - ``high``: **上限** — 提示コンテキストがこれを超えたら発火。None = 文字数
      では発火せず ``token_triggered`` のみ。
    - ``low``: 旧三水位の名残り (保護範囲)。現設計では**使わない** — 残す量が
      保護を兼ねる。モデル設定との互換のため受け取るだけ。
    """

    low: int
    target: int
    high: Optional[int] = None


@dataclass
class Fold:
    """一度に畳んで退場させる連続メッセージ列 = 一次あらすじ 1 個ぶんの範囲。"""

    messages: List[Dict[str, Any]]
    #: 旧設計 (エピソード単独畳み) の名残り。現設計の計画は設定しない —
    #: 適用側の部分エピソード記録 (_record_partial_episode) は発火しなくなる
    #: (存廃は intent §12-5 の未決事項)。
    open_episode_ref: Optional[str] = None
    #: この範囲が覆うエピソード ref 一覧 (ログ・記録用)。
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
    # どの fold が anchor 前進になり、どれが提示コンテキストの中の圧縮区間に
    # なるかは**ここでは決めない** — 判定には置き換え前の生ログの並びが要り、
    # それを持っているのは適用側 (sea/session_lifecycle.py の
    # ``_apply_eviction_plan``) だから。同じ真実を二箇所に置かない。
    #
    #: 旧設計 (二段構えの最後の手段) の名残り。現設計の計画は立てない —
    #: 常に False。観測ログの互換のため残す。
    used_last_resort_fold: bool = False
    #: 計画適用後に残る提示文字数の見込み。
    projected_chars: int = 0
    #: 適用前の提示文字数。
    total_chars: int = 0
    #: 保護範囲 (残す量) の開始インデックス (この位置以降は退場させない)。
    protected_from: int = 0

    @property
    def evicted_count(self) -> int:
        return sum(len(f.messages) for f in self.folds)

    @property
    def is_empty(self) -> bool:
        return not self.folds


#: 畳んだ範囲が提示コンテキストに残す置き換えメッセージの見込み文字数。
#:
#: 畳んでも提示は 0 にならない — その位置に「あらすじ + 圧縮マークの注釈」が
#: 立ちうる。実際の長さは生成してみるまで分からないので、計画では固定の見込みで
#: 差し引く。**多めに見積もる**方が安全。
ESTIMATED_FOLD_PLACEHOLDER_CHARS = 1_200


def message_chars(messages: Sequence[Dict[str, Any]]) -> int:
    """提示文字数 (水位判定の一次データ)。"""
    return sum(len(str(m.get("content") or "")) for m in messages)


def _net_reduction(messages: Sequence[Dict[str, Any]]) -> int:
    """この範囲を畳んで**実際に減る**提示文字数。"""
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
    messages: Sequence[Dict[str, Any]], keep_chars: int,
) -> int:
    """残す量 (保護範囲) の開始インデックスを返す。

    最新側から遡って ``keep_chars`` 分を保護する。境界が pulse の途中に落ちたら
    **古い側へ** 関節まで下げる — メッセージ単位でぶつ切りにせず、保護範囲を
    広げる方向に倒す (「刻むときは pulse を丸ごと」experience_structure.md §6)。
    """
    if keep_chars <= 0:
        return len(messages)
    acc = 0
    boundary = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += len(str(messages[i].get("content") or ""))
        if acc >= keep_chars:
            boundary = i
            break
    else:
        # 提示コンテキスト全体でも残す量に届かない = 全部が保護範囲。
        return 0
    # pulse 関節へスナップ (古い側 = 保護範囲を広げる向き)。
    raw_boundary = boundary
    pulse = _pulse_of(messages[boundary])
    if pulse is not None:
        while boundary > 0 and _pulse_of(messages[boundary - 1]) == pulse:
            boundary -= 1
    if boundary <= 0 < raw_boundary:
        # スナップで候補がゼロになった = 1 つの pulse が残す量を超えている。
        # 関節の綺麗さより前進を優先し、素の境界で切る — でないと巨大 pulse が
        # 居座る間、上限をどれだけ超えても退場が一切進まない
        # (Codex レビュー 2026-07-28 medium)。
        LOGGER.info(
            "[eviction] a single pulse exceeds the keep amount; cutting "
            "mid-pulse at index %d to keep eviction moving", raw_boundary,
        )
        return raw_boundary
    return boundary


def _is_folded_placeholder(msg: Dict[str, Any]) -> bool:
    """既に digest へ置き換わっている位置か (sea/session_window.py の置き換え)。"""
    meta = msg.get("metadata")
    return bool(isinstance(meta, dict) and meta.get("__folded_range__"))


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


def plan_eviction(
    messages: Sequence[Dict[str, Any]],
    open_episode_refs: Set[str],
    watermarks: Watermarks,
    *,
    target_chars: int,
) -> EvictionPlan:
    """提示中の提示コンテキストから「今回退場させる範囲」を計画する。

    Args:
        messages: 提示中の提示コンテキスト (created_at 昇順、既に畳んだ圧縮区間は
            digest に置換済み)。
        open_episode_refs: 旧設計 (エピソード単独畳み) の名残り。現設計では
            使わない — エピソードに畳みを止める権利は無い (intent §4-1)。
        watermarks: 水位。``target`` = 残す量だけを使う (発火判定 ``high`` は
            呼び出し側の責務、``low`` は旧設計互換で未使用)。
        target_chars: 一次あらすじの標準被覆 U。1 つの fold が目指す大きさ。

    計画: 残す量より古い側を、古い順に U ずつの範囲に刻んで全部畳む。

    - 切り位置は pulse 関節 (発言の切れ目) に寄せる — U に達したら、いまの
      pulse を最後まで含めてそこで切る。
    - 既に畳まれた置き換え (壁) は材料に入れない。壁の手前に U 未満の端数が
      残る場合だけ、端数のまま畳む (残すと壁に挟まれて永久に取り残されるため。
      旧世代データでのみ起きる — 現設計の適用は畳んだ先頭を anchor が
      すぐ飲み込むので、新しい壁は候補範囲に現れない)。
    - 末尾 (保護範囲の直前) の U 未満の端数は畳まず残す — 次回、新しい生ログと
      地続きのまま畳まれる。

    Returns:
        EvictionPlan。
    """
    total = message_chars(messages)
    plan = EvictionPlan(total_chars=total, projected_chars=total)
    if not messages:
        return plan
    if target_chars <= ESTIMATED_FOLD_PLACEHOLDER_CHARS:
        # U が置き換えの見込みより小さいと、1 束あたりの正味削減が常に 0 になり、
        # 「減らないのに畳み続ける」= 過剰退場になる。設定ミス
        # (SAIVERSE_CHRONICLE_BAND_BUDGET を極端に下げた等) なので退場を見送る。
        LOGGER.warning(
            "[eviction] target_chars=%d is not larger than the fold placeholder "
            "estimate (%d); skipping eviction (check SAIVERSE_CHRONICLE_BAND_BUDGET)",
            target_chars, ESTIMATED_FOLD_PLACEHOLDER_CHARS,
        )
        return plan

    boundary = _protection_boundary(messages, watermarks.target)
    plan.protected_from = boundary
    if boundary <= 0:
        # 退場候補範囲が無い = 残す量だけで提示コンテキストが埋まっている。
        return plan

    candidates = list(messages[:boundary])
    folds: List[Fold] = []
    remaining = total

    def _close(pending: List[Dict[str, Any]]) -> None:
        nonlocal remaining
        refs: List[str] = []
        for m in pending:
            ref = episode_ref_of(m)
            if ref and ref not in refs:
                refs.append(ref)
        folds.append(Fold(messages=list(pending), episode_refs=refs))
        remaining -= _net_reduction(pending)

    pending: List[Dict[str, Any]] = []
    for group in _pulse_groups(candidates):
        if any(_is_folded_placeholder(m) for m in group):
            # 壁 (畳み済みの置き換え)。材料に入れない。手前の端数は、残すと
            # 壁に挟まれて永久に取り残されるので、端数のまま畳む。
            if pending:
                if message_chars(pending) < target_chars:
                    LOGGER.info(
                        "[eviction] folding an undersized run (%d chars < U=%d) "
                        "stranded before an already-folded range (legacy wall)",
                        message_chars(pending), target_chars,
                    )
                _close(pending)
                pending = []
            continue
        pending.extend(group)
        if message_chars(pending) >= target_chars:
            _close(pending)
            pending = []
    # 末尾の端数は畳まない (次回、新しい生ログと地続きで畳まれる)。

    plan.folds = folds
    plan.projected_chars = remaining
    return plan


def compile_groups_from_folds(
    folds: Sequence["Fold"],
    presented: Sequence[Dict[str, Any]],
) -> List[List[str]]:
    """fold 群を編纂側へ渡す「束ねてよい範囲」の列に変換する。

    ``Fold`` の契約は「時系列順・互いに重ならない連続範囲」で、正しい計画なら
    fold と範囲は一対一。この関数はその契約を**提示コンテキストの実際の並びで
    検算**し、破れていたら範囲を割ってから渡す。

    割る理由 (experience_structure.md §4-5 連続束ねのみ): fold の内側に
    「今回退場しないメッセージ」が挟まっていると、そのメッセージは生ログの
    まま提示コンテキストに残る。前後を一つのあらすじにすれば、間に生ログが
    あるのに地続きに語る**偽の隣接** = 時系列の嘘になる。

    検算をここで行うのは、**提示コンテキストの完全な並びを持っているのがこの層
    だけ**だから。編纂側 (generate_chronicle) が見るのは Chronicle 除外
    (除外タグ / line_role / Stelis) を落とした後の列で、除外メッセージが
    退場せずに残る形の抜けは原理的に見えない。

    契約違反を例外にしないのは、退場ごと止めると壊れた計画が出続ける限り
    anchor が永久に進まないため。割れば偽の隣接は出ず、範囲は全て編纂される。

    Args:
        folds: :attr:`EvictionPlan.folds`。
        presented: 提示コンテキストの全メッセージ (時系列順、Chronicle 除外分も含む)。

    Returns:
        編纂側へ渡す message id 群の列 (時系列順)。
    """
    order = [str(m.get("id")) for m in presented]
    position = {mid: i for i, mid in enumerate(order)}
    selected = {mid for fold in folds for mid in fold.message_ids}

    # 区間 (a, b) に「退場しないメッセージ」が居るかを O(1) で引くための累積和。
    retained = [0] * (len(order) + 1)
    for i, mid in enumerate(order):
        retained[i + 1] = retained[i] + (0 if mid in selected else 1)

    out: List[List[str]] = []
    for fold in folds:
        ids = fold.message_ids
        placed = sorted((position[mid], mid) for mid in ids if mid in position)
        # 提示コンテキストに居ない id (契約違反) は落とさないが、並び上の位置が
        # 分からない以上どれとも隣接の根拠が無い → 1 件ずつ独立の片にする
        # (編纂側 _run_group_keys の「所属不明は孤立」と同じ倒し方)。
        unplaced = [mid for mid in ids if mid not in position]
        segments: List[List[str]] = [[mid] for mid in unplaced]
        current: List[str] = []
        previous: Optional[int] = None
        for index, mid in placed:
            if previous is not None and retained[index] > retained[previous + 1]:
                segments.append(current)
                current = []
            current.append(mid)
            previous = index
        if current:
            segments.append(current)
        if unplaced:
            LOGGER.warning(
                "[eviction] fold に提示コンテキスト外の id が %d 件 (位置不明なので "
                "1 件ずつ孤立させる): %s",
                len(unplaced), unplaced[:5],
            )
        if len(segments) - len(unplaced) > 1:
            LOGGER.warning(
                "[eviction] fold が退場しないメッセージをまたいでいる: %d 片に "
                "割って編纂へ渡す %s (計画が連続していない — §4-5 の偽の隣接を防ぐ)",
                len(segments), [seg[:3] for seg in segments][:5],
            )
        out.extend(seg for seg in segments if seg)
    return out
