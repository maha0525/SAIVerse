"""退場計画 — レベル0 (生ログ) の「予算超過で古い側を畳む」を決める。

docs/intent/arasuji_levels.md §3 / §4 の実装 (レベル0 の並び)。
**純関数のみ** — DB も LLM も触らない。呼び出し側 (sea/session_lifecycle.py) が
提示中の提示コンテキストを渡し、返った計画を適用する。

設計の芯 (arasuji_levels.md):

- 規則は全レベル共通の一本 — 「予算の上限を超えたら発火し、古い側を
  『残す量』に収まるまで畳んで 1 つ上のレベル (一次あらすじ) へ送る」。
- 畳み材料は約 U (一次あらすじの標準被覆、既定 1 万字) ずつに刻む。切り位置は
  発言の切れ目 (関節) に寄せる — エピソードには**畳みを止める権利は無い**
  (開いているエピソードも畳む。守っていた需要の引受先は
  docs/issues/open_episode_context_after_veto_removal.md)。
- **U に達したかは材料字数で測る** (2026-08-29 まはー裁定)。あらすじを作る理由は
  圧縮にあり、長い機構名義の行 (スペル結果等) は材料を組む時に決定論の一行へ
  縮む (sai_memory/arasuji/generator.py の長さ規則) — 生の字数で測ると、
  スペルを呼ぶ流れだけで「圧縮の意義が薄いあらすじ区間」が量産される。
  一方、**残す量 (保護境界) と削減見込み (projected_chars) は生の提示字数の
  まま** — こちらは提示コスト経済の水位であって、材料の勘定ではない。
- **スペルの群は退場の境目で割れない** — 「唱え → 結果 → 結果を読んだ発話」の
  ひとまとまり (``spell_origin_id`` の印) の内側に、保護境界も fold の切れ目も
  落とさない。割れると窓が ``<system>[Spell Result: ...]`` から始まり、唱えた
  記憶が無いのに結果だけがある状態 = 記憶の捏造になる。関節の単位は
  :func:`_joint_units` が pulse の run とスペルの群を併合して決める。
- 末尾の U に届かない端数は畳まず残す — 次の Metabolism で新しい生ログと
  地続きのまま次の畳みに入るので、小さい一次あらすじを作らない。
- 発火判定 (上限) は呼び出し側の責務。ここは「残す量」だけを使う。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from sai_memory.arasuji.generator import material_len
from sai_memory.memory.storage import spell_group_spans

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
    #: 末尾に畳まず残した端数 (次回持ち越し分) の**材料字数**。fold が一つも
    #: 閉じなかったとき、「あと材料何字たまれば畳めるか」を UI (context-status)
    #: が出すための報告値。ここで算数を再実装させない。
    pending_material_chars: int = 0

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


def _payload_tags(msg: Dict[str, Any]) -> tuple:
    """提示 payload の tags (metadata.tags)。無ければ空。

    payload には SAIMemory 行の metadata が丸ごと乗る
    (saiverse_memory/adapter.py::_payload_from_message_locked) ので、機構名義の
    印はここから読める — 追加の配線は要らない。
    """
    meta = msg.get("metadata")
    tags = meta.get("tags") if isinstance(meta, dict) else None
    return tuple(tags) if isinstance(tags, (list, tuple)) else ()


def material_message_chars(messages: Sequence[Dict[str, Any]]) -> int:
    """材料としての字数 (U 判定の物差し — 2026-08-29 まはー裁定)。

    長さ規則の実体は sai_memory/arasuji/generator.py の
    :func:`~sai_memory.arasuji.generator.material_text_for` 一枚 — Message
    オブジェクト用の material_chars と、この payload 用の勘定が同じ関数を呼ぶ
    (閾値・縮んだ一行の長さを二重実装しない)。
    """
    return sum(
        material_len(str(m.get("content") or ""), _payload_tags(m))
        for m in messages
    )


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


def _spell_spans(messages: Sequence[Dict[str, Any]]) -> List[tuple]:
    """提示コンテキスト上のスペル群が占める添字の区間 (開始昇順)。

    群の範囲を決める規則そのものは ``spell_origin_id`` 列の持ち主
    (:func:`sai_memory.memory.storage.spell_group_spans`) にある — ここは
    提示 payload から ``(id, spell_origin_id)`` の列を作って渡すだけ
    (規則の二枚目を作らない)。
    """
    return spell_group_spans(
        [(str(m.get("id")), m.get("spell_origin_id")) for m in messages]
    )


def _spell_group_key(
    messages: Sequence[Dict[str, Any]], lo: int, hi: int,
) -> str:
    """区間 ``[lo, hi]`` が表すスペル群の鍵 (= 起点行の message id)。

    起点行自身の ``spell_origin_id`` は NULL なので、区間の中で最初に
    ``spell_origin_id`` を持つ行からその値を読む。1 件も無い (起点行しか
    区間に居ない) 場合は区間先頭の id を鍵として返す。ログ用。
    """
    for i in range(lo, hi + 1):
        origin = messages[i].get("spell_origin_id")
        if origin:
            return str(origin)
    return str(messages[lo].get("id"))


def _joint_units(messages: Sequence[Dict[str, Any]]) -> List[tuple]:
    """「切ってよい関節」で区切った単位を、添字の閉区間 ``(開始, 終了)`` で返す。

    単位の内側に境目 (保護境界・fold の切れ目) は落とさない。単位は 2 段で作る:

    1. 連続する同一 ``pulse_id`` の run。``pulse_id`` が無い行は単独の単位
       (関節を勝手に作らない)。
    2. スペルの群 (:func:`_spell_spans`) と重なる単位は、群の区間ごと 1 つの
       単位へ併合する。群の内側に割り込んだ行 (``pulse_id`` を持たない
       event_message、別 pulse の committed なメタ判断) も同じ単位に入る —
       この割り込みで pulse の連続が切れることが、境目が群の内側に落ちる
       (唱えが退場済みなのに結果の行だけ窓に残る) 欠陥の発生源だった。

    単に隣り合っているだけの単位は併合しない (隣接する単位の境目は正当な境目)。
    """
    units: List[List[int]] = []
    current_pulse: Optional[str] = None
    for index, msg in enumerate(messages):
        pulse = _pulse_of(msg)
        if units and pulse is not None and pulse == current_pulse:
            units[-1][1] = index
            continue
        units.append([index, index])
        current_pulse = pulse

    for lo, hi in _spell_spans(messages):
        first = _unit_index_of(units, lo)
        last = _unit_index_of(units, hi)
        if first is None or last is None or first == last:
            continue
        units[first:last + 1] = [[units[first][0], units[last][1]]]

    return [(start, end) for start, end in units]


def _unit_index_of(units: Sequence[Sequence[int]], position: int) -> Optional[int]:
    """``position`` を含む単位の添字 (単位は隙間なく並ぶので線形走査で足りる)。"""
    for i, (start, end) in enumerate(units):
        if start <= position <= end:
            return i
    return None


def _protection_boundary(
    messages: Sequence[Dict[str, Any]],
    keep_chars: int,
    units: Optional[Sequence[tuple]] = None,
) -> int:
    """残す量 (保護範囲) の開始インデックスを返す。

    最新側から遡って ``keep_chars`` 分を保護する。境界が関節の単位
    (:func:`_joint_units` — pulse の run にスペルの群を併合したもの) の途中に
    落ちたら **古い側へ** 単位の先頭まで下げる — メッセージ単位でぶつ切りに
    せず、保護範囲を広げる方向に倒す (「刻むときは pulse を丸ごと」
    experience_structure.md §6)。

    **脱出弁** — 古い側へ寄せると境界が消える (素の境界を含む単位が提示の先頭
    ``index 0`` から始まっている) 場合だけ、寄せる向きを変える:

    - 原則は **新しい側へ寄せる** (``unit_end + 1``)。その単位はまるごと退場候補に
      入り、境目は単位の外に落ちる。保護範囲は残す量より狭くなるが、単位
      (スペルの群を含みうる) を割らずに前進できる — 字数の厳密さより
      「境目を単位の内側に落とさない」不変条件を優先する。``protected_from`` は
      報告用の値で、残す量を保証する契約は誰も持っていない。
    - 例外は **その単位が提示コンテキスト全体** のとき (``unit_end + 1`` が
      提示の外)。新しい側へ寄せると保護範囲が空になり anchor の指す先が無く
      なるので、このときだけ素の境界で切る = 単位を割る (割った位置がスペル群の
      内側なら WARNING)。

    どちらの向きでも境界は 1 以上になるので、退場候補が空になって
    「上限をどれだけ超えても退場が一切進まない」状態にはならない
    (arasuji_levels.md §4-1 の「上限を超えたら必ず前進する」)。
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
    # 関節へスナップ (古い側 = 保護範囲を広げる向き)。
    raw_boundary = boundary
    if units is None:
        units = _joint_units(messages)
    unit_index = _unit_index_of(units, boundary)
    if unit_index is None:
        # どの単位にも属さない位置 (単位は隙間なく並ぶので通常は起きない)。
        return boundary
    unit_start, unit_end = units[unit_index]
    boundary = unit_start
    if boundary > 0 or raw_boundary <= 0:
        return boundary

    # --- 脱出弁 ---
    # 古い側へ寄せると境界が消えた = 素の境界 (raw_boundary) を含む単位が提示の
    # 先頭 (index 0) から始まっている。このまま 0 を返すと退場候補が空になり、
    # 上限をどれだけ超えても退場が一切進まない (Codex レビュー 2026-07-28
    # medium。arasuji_levels.md §4-1 の「上限を超えたら必ず前進する」)。
    if unit_end + 1 < len(messages):
        # 原則: 新しい側へ寄せる。単位はまるごと退場候補に入り、境目は単位の外に
        # 落ちる — 単位 (スペルの群を含みうる) を割らずに前進できる。保護範囲が
        # 残す量より狭くなるのは承知の上で、不変条件の方を採る。
        LOGGER.info(
            "[eviction] 脱出弁: 先頭から始まる関節単位 (添字 %d..%d) が残す量を"
            "覆っているので、境界を単位の外 (index %d) へ新しい側に寄せる — "
            "単位はまるごと退場候補に入り、群は割れない",
            unit_start, unit_end, unit_end + 1,
        )
        return unit_end + 1
    # 例外: その単位が提示コンテキスト全体。新しい側へ寄せると保護範囲が空に
    # なり anchor の指す先が無くなるので、ここだけは素の境界で切る = 単位を割る。
    broken = _spell_span_broken_at(messages, raw_boundary)
    if broken is not None:
        lo, hi = broken
        LOGGER.warning(
            "[eviction] 脱出弁: 提示コンテキスト全体が 1 つの関節単位なので "
            "index %d で切るが、その位置はスペル群 %s (添字 %d..%d) の"
            "内側にある — 唱えと結果が退場の境目で割れる",
            raw_boundary, _spell_group_key(messages, lo, hi), lo, hi,
        )
    else:
        LOGGER.info(
            "[eviction] 脱出弁: 提示コンテキスト全体が 1 つの関節単位なので "
            "index %d で単位を割って切る (新しい側へ寄せると保護範囲が空に"
            "なるため)", raw_boundary,
        )
    return raw_boundary


def _spell_span_broken_at(
    messages: Sequence[Dict[str, Any]], position: int,
) -> Optional[tuple]:
    """``position`` で切るとスペル群を割ることになるなら、その区間を返す。

    区間 ``(lo, hi)`` の**内側**、つまり ``lo < position <= hi`` の位置で切ると
    群のメンバーが境目の両側に分かれる。``position == lo`` は群の手前で切るので
    割らない。
    """
    for lo, hi in _spell_spans(messages):
        if lo < position <= hi:
            return (lo, hi)
    return None


def _is_folded_placeholder(msg: Dict[str, Any]) -> bool:
    """既に digest へ置き換わっている位置か (sea/session_window.py の置き換え)。"""
    meta = msg.get("metadata")
    return bool(isinstance(meta, dict) and meta.get("__folded_range__"))


def _pending_tail_split_unit(
    messages: Sequence[Dict[str, Any]],
    units: Sequence[tuple],
    pending: Sequence[Dict[str, Any]],
) -> Optional[tuple]:
    """端数 ``pending`` の末尾が関節単位の途中で切れているなら、その単位を返す。

    非常畳み (close_undersized_tail) の観測用。単位 (:func:`_joint_units` —
    pulse の run + スペルの群) の末尾まで含んで閉じるなら None。途中で切れて
    いる = 単位の残り (保護範囲側) と別のあらすじに分かれる、のときだけ
    その単位の閉区間 ``(開始, 終了)`` を返す。この切れ方が生まれるのは
    :func:`_protection_boundary` の脱出弁例外 (窓全体が一つの単位) だけ。
    """
    if not pending:
        return None
    tail_id = str(pending[-1].get("id"))
    tail_index = next(
        (i for i, m in enumerate(messages) if str(m.get("id")) == tail_id),
        None,
    )
    if tail_index is None:
        return None
    unit_index = _unit_index_of(units, tail_index)
    if unit_index is None:
        return None
    unit_start, unit_end = units[unit_index]
    if unit_end > tail_index:
        return (unit_start, unit_end)
    return None


def _units_before(
    messages: Sequence[Dict[str, Any]],
    units: Sequence[tuple],
    boundary: int,
) -> List[List[Dict[str, Any]]]:
    """保護境界より古い側を、関節の単位ごとのメッセージ列にして返す。

    境界が単位の途中に落ちるのは、脱出弁の例外経路 (提示コンテキスト全体が
    1 つの単位で、素の境界で割るしかない場合。:func:`_protection_boundary`) だけ。
    その場合は最後の単位を境界で切り詰める。
    """
    out: List[List[Dict[str, Any]]] = []
    for start, end in units:
        if start >= boundary:
            break
        out.append(list(messages[start:min(end + 1, boundary)]))
    return out


def plan_eviction(
    messages: Sequence[Dict[str, Any]],
    open_episode_refs: Set[str],
    watermarks: Watermarks,
    *,
    target_chars: int,
    close_undersized_tail: bool = False,
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
            **達したかは材料字数で測る** (2026-08-29 まはー裁定 —
            :func:`material_message_chars`)。
        close_undersized_tail: 非常経路 (§14-3 の非常畳み) 専用。True なら、
            fold が一つも閉じられなかったときに限り、末尾の端数 (材料 U 未満)
            をそのまま 1 fold として閉じる — 「生は巨大だが材料が薄い」期間で
            提示が痩せないまま高水位超過が続く併走を断つための最後の手段
            (小粒のあらすじは非常時にのみ許す)。ただし閉じるのは**提示が実際に
            減るとき** (端数の生字数が置き換えの見込みを上回るとき) だけ —
            減らないのに閉じると、LLM を呼んで entry を作るのに提示が 1 字も
            痩せない無駄骨になる (Codex 指摘 2026-08-29)。既定 False。

    計画: 残す量より古い側を、古い順に U (材料字数) ずつの範囲に刻んで全部畳む。

    - 切り位置は関節 (発言の切れ目) に寄せる — U に達したら、いまの関節の単位
      (:func:`_joint_units`: pulse の run + スペルの群) を最後まで含めてそこで
      切る。
    - 既に畳まれた置き換え (壁) は材料に入れない。壁の手前に U 未満の端数が
      残る場合だけ、端数のまま畳む (残すと壁に挟まれて永久に取り残されるため。
      旧世代データでのみ起きる — 現設計の適用は畳んだ先頭を anchor が
      すぐ飲み込むので、新しい壁は候補範囲に現れない)。
    - 末尾 (保護範囲の直前) の U 未満の端数は畳まず残す — 次回、新しい生ログと
      地続きのまま畳まれる (例外は ``close_undersized_tail``)。

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

    units = _joint_units(messages)
    boundary = _protection_boundary(messages, watermarks.target, units)
    plan.protected_from = boundary
    if boundary <= 0:
        # 退場候補範囲が無い = 残す量だけで提示コンテキストが埋まっている。
        return plan

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
    for group in _units_before(messages, units, boundary):
        if any(_is_folded_placeholder(m) for m in group):
            # 壁 (畳み済みの置き換え)。材料に入れない。手前の端数は、残すと
            # 壁に挟まれて永久に取り残されるので、端数のまま畳む。
            if pending:
                if material_message_chars(pending) < target_chars:
                    LOGGER.info(
                        "[eviction] folding an undersized run (%d material chars "
                        "< U=%d) stranded before an already-folded range "
                        "(legacy wall)",
                        material_message_chars(pending), target_chars,
                    )
                _close(pending)
                pending = []
            continue
        pending.extend(group)
        # U 到達の判定は材料字数 (2026-08-29 裁定) — 生の字数ではない。
        if material_message_chars(pending) >= target_chars:
            _close(pending)
            pending = []
    # 末尾の端数は畳まない (次回、新しい生ログと地続きで畳まれる)。
    # 例外: 非常経路 (close_undersized_tail=True) で fold が一つも閉じられ
    # なかったときだけ、端数のまま閉じて前進を保証する — 「生は巨大だが材料が
    # 薄い」期間で提示が痩せない併走を断つ最後の手段 (小粒は非常時のみ)。
    # ただし提示が実際に減るときだけ (減らない閉じは LLM 代の無駄骨 —
    # Codex 指摘 2026-08-29)。
    if pending and close_undersized_tail and not folds:
        if _net_reduction(pending) <= 0:
            LOGGER.info(
                "[eviction] 非常経路でも減量ゼロのため見送り: 端数の生 %d 字は"
                "置き換えの見込み (%d 字) 以下で、閉じても提示が 1 字も減らない "
                "(材料 %d 字, %d 件は従来どおり次回へ持ち越す)",
                message_chars(pending), ESTIMATED_FOLD_PLACEHOLDER_CHARS,
                material_message_chars(pending), len(pending),
            )
        else:
            # 端数が関節単位 (pulse の run + スペルの群) の途中で切れている =
            # 群を割って閉じることがある。Codex レビュー (2026-08-29) は
            # 「単位の途中なら閉じずに defer せよ」と勧めたが**採らない** —
            # 境界が単位の内側に落ちるのは「窓全体が一つの単位」の脱出弁例外
            # (:func:`_protection_boundary`) だけで、そこで defer すると畳める
            # ものが永久に現れず、提示が痩せない手詰まりが再導入される。
            # 割ること自体は 2026-08-25 に設計として受け入れ済み。代わりに、
            # 割った事実を脱出弁と同じ格の WARNING で観測できるようにする。
            split = _pending_tail_split_unit(messages, units, pending)
            if split is not None:
                lo, hi = split
                LOGGER.warning(
                    "[eviction] 非常経路: スペル群/pulse 単位 (添字 %d..%d) を"
                    "割って端数の fold を閉じた — 窓全体が一つの単位のときの"
                    "脱出弁例外と同じ割れ方 (単位の残りは保護範囲側に生で残る)",
                    lo, hi,
                )
            LOGGER.info(
                "[eviction] 非常経路: 材料 U 未満の fold を閉じた "
                "(材料 %d 字 < U=%d, 生 %d 字, %d 件)",
                material_message_chars(pending), target_chars,
                message_chars(pending), len(pending),
            )
            _close(pending)
            pending = []

    plan.folds = folds
    plan.projected_chars = remaining
    plan.pending_material_chars = material_message_chars(pending)
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
