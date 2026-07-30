"""読み戻しの計画 — 残す量を下回る提示ウィンドウを開き直す (arasuji_levels.md §15)。

**純関数のみ** — DB も LLM も触らない (sea/eviction_plan.py と同じ層)。呼び出し側
(sea/session_lifecycle.py) が材料 (提示ウィンドウ・anchor より前を遡り読みした
メッセージ・被覆エントリ) を渡し、返った計画を適用する。

設計の芯 (§15):

- 編纂は「残す量へ揃える」双方向の操作。多ければ削る (eviction_plan)、少なければ
  畳んだところを開き直す (このモジュール)。
- 開き直しは帳簿の付け替えだけで LLM を呼ばない。圧縮区間の記録は消さず
  「生で見せる」印 (``presented_raw``) を付ける — head のあらすじ枠の除外名簿は
  効き続け、再畳みは印戻しで既存あらすじを再利用する。
- **引き戻しの梯子はあらすじの段だけ**: anchor を引き戻す先端は必ず一次エントリの
  被覆境界。あらすじの無い領域 (編纂なしで忘れた過去) へは降りない。段の間に
  挟まる編纂対象外メッセージは境界の内側なら一緒に生へ戻る。
- 天井は残す量 (上限との間に緩衝を残して発振を防ぐ)。区間は丸ごと単位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sea.eviction_plan import ESTIMATED_FOLD_PLACEHOLDER_CHARS
from sea.session_window import FoldedRange

LOGGER = logging.getLogger(__name__)


def _epoch(msg: Dict[str, Any]) -> Optional[int]:
    try:
        return int(msg.get("created_at"))
    except (TypeError, ValueError):
        return None


def plan_reopen(
    folds: Sequence[FoldedRange],
    raw_messages: Sequence[Dict[str, Any]],
    presented_messages: Sequence[Dict[str, Any]],
    current_chars: int,
    target_chars: int,
) -> Tuple[List[FoldedRange], int]:
    """窓内の digest 圧縮区間を新しい方から開く計画 (§15-2 の 1)。

    Args:
        folds: 現ウィンドウの圧縮区間 (呼び出し側の実体をそのまま渡す)。
        raw_messages: 提示ウィンドウの生ログ (anchor 以降、時系列昇順)。
        presented_messages: 現在の提示 (digest 置き換え済み)。開いたときに
            消える置き換えの**実際の**文字数をここから引く — 固定の見込みで
            引くと、実際の置き換えが見込みより短い区間で利得を過小評価し、
            天井 (残す量) の保証が破れる (Codex 指摘 2026-07-30)。
        current_chars: 現在の提示文字数。
        target_chars: 残す量 (天井)。

    Returns:
        (印を付けるべき fold のリスト, 開いた後の提示文字数の見込み)。
        印はここでは付けない — 選ぶだけ (純関数)。開くと提示は digest の
        置き換え分が消えて生ログ分が乗るので、見込みは
        ``+ (生の文字数 − 置き換えの実文字数)`` で数える。

    開けるのは **message_ids が現ウィンドウに完全に収まっている区間だけ**。
    一部が anchor の手前に出ている区間 (prune_folds が「一部でも提示に残る
    範囲は残す」で保持したもの) を開くと、digest が消えて head の除外も
    効き続けるため、手前に出ている分の体験が提示からもあらすじからも消える
    (Codex 指摘 2026-07-30)。
    """
    raw_len = {
        str(m.get("id")): len(str(m.get("content") or "")) for m in raw_messages
    }
    order = {str(m.get("id")): i for i, m in enumerate(raw_messages)}

    # 提示中の置き換えメッセージの実文字数。id は ``folded:<提示に残る最初の
    # メッセージ id>`` (sea/session_window.py の _placeholder)。
    placeholder_len: Dict[str, int] = {}
    for m in presented_messages:
        meta = m.get("metadata")
        if isinstance(meta, dict) and meta.get("__folded_range__"):
            mid = str(m.get("id"))
            if mid.startswith("folded:"):
                placeholder_len[mid[len("folded:"):]] = len(
                    str(m.get("content") or "")
                )

    def _last_pos(fold: FoldedRange) -> int:
        positions = [order[mid] for mid in fold.message_ids if mid in order]
        return max(positions) if positions else -1

    def _placeholder_chars(fold: FoldedRange) -> Optional[int]:
        for mid in fold.message_ids:
            if mid in raw_len:  # 提示に残る最初のメッセージ
                return placeholder_len.get(mid)
        return None

    chosen: List[FoldedRange] = []
    projected = current_chars
    for fold in sorted(folds, key=_last_pos, reverse=True):  # 新しい方から
        if fold.presented_raw or not fold.message_ids:
            continue
        if any(mid not in raw_len for mid in fold.message_ids):
            # 一部が anchor の手前に出ている区間は開けない (上記 docstring)。
            continue
        actual_placeholder = _placeholder_chars(fold)
        if actual_placeholder is None:
            # 置き換えが提示に無い = digest が引けず fail-open で既に生で
            # 出ている区間。開いても提示は 1 字も増えないので、架空の利得を
            # 計上して引き戻しの予算を削らない (Codex 指摘 2026-07-30)。
            continue
        raw_chars = sum(raw_len.get(mid, 0) for mid in fold.message_ids)
        gain = raw_chars - actual_placeholder
        if gain <= 0:
            continue
        if projected + gain > target_chars:
            # この区間は入らない。より小さい古い区間は入るかもしれないので続ける。
            continue
        chosen.append(fold)
        projected += gain
    return chosen, projected


@dataclass
class RewindPlan:
    """:func:`plan_rewind` の出力 — anchor 引き戻し 1 回分。"""

    #: 引き戻し先 (新しい anchor にするメッセージ id)。
    new_anchor_id: str
    #: 引き戻した範囲に合成する圧縮区間 (全て ``presented_raw=True``、時系列順)。
    folds: List[FoldedRange] = field(default_factory=list)
    #: 提示に加わる文字数 (境界から旧 anchor 直前までの合計)。
    restored_chars: int = 0
    #: 提示に加わるメッセージ件数。
    restored_message_count: int = 0


def plan_rewind(
    before_messages: Sequence[Dict[str, Any]],
    entries: Sequence[Any],
    window_raw_ids: Sequence[str],
    already_folded_entry_ids: Set[str],
    already_folded_message_ids: Set[str],
    eligible_message_ids: Set[str],
    budget_chars: int,
) -> Optional[RewindPlan]:
    """anchor 引き戻しの計画 (§15-2 の 2)。

    Args:
        before_messages: 現 anchor より前の提示対象メッセージ (時系列昇順。
            遡り読みは予算分で打ち切られていてよい)。
        entries: ``before_messages`` を source に持つ一次あらすじエントリ
            (``id`` / ``short_id`` / ``source_ids`` を持つもの)。
        window_raw_ids: 現ウィンドウの生メッセージ id。エントリの source が
            anchor 以降 (窓の中) へはみ出している分を「欠け」と数えないため。
        already_folded_entry_ids: 既存の圧縮区間が持つエントリ id。その
            エントリは窓側の記録が生きているので、ここでは扱わない。
        already_folded_message_ids: 既存の圧縮区間が持つメッセージ id。新しい
            段の source がここと交差すると、同じメッセージが二つの区間に属して
            digest の二重提示が起こるため、その段は開けない (壊れた段と同じ
            扱いで梯子が止まる)。
        eligible_message_ids: ``before_messages`` のうち**編纂対象**のメッセージ
            id (編纂側と同じ filter_chronicle_eligible_ids で判定したもの)。
            編纂対象なのにどの段にも被覆されないメッセージは「編纂なしで
            忘れた過去」— そこを跨いで引き戻すと忘却済みの内容が復活するため、
            梯子はその手前で止まる (Codex 指摘 2026-07-30)。編纂対象外の
            メッセージ (除外タグ等) は被覆が無いのが健全なので同乗してよい。
        budget_chars: 戻してよい文字数 (残す量 − 開き直し後の提示見込み)。

    Returns:
        RewindPlan。戻せる段が無ければ None。

    梯子の規律: 引き戻しの単位は「段」。段は二つの束ねで決まる —
    ① **source を共有するエントリの推移閉包** (before 側・窓側どちらの共有でも。
    共有したまま別区間にすると印戻し後に同じメッセージが二つの digest に属する)、
    ② before 内で位置範囲が重なる段どうしの併合 (どちらか半分だけは開けない)。
    新しい段から順に、(a) 段のエントリに提示対象から消えた source がある
    (= 段が壊れていて全体を生で見せられない)、(b) 段の source が既存の圧縮区間と
    交差する、(c) その段まで戻すと予算を超える、のどれかに当たったら止まる。
    段を飛ばして先へは降りない — 飛ばすと被覆の無い生ログが窓に入り、head の
    あらすじ枠と二重になる。
    """
    if budget_chars <= 0 or not before_messages or not entries:
        return None

    # 既存の圧縮区間の領土には踏み込まない: 遡り読みの列に既存区間の
    # メッセージが現れたら、それより古くへは降りない。踏み込むと、anchor
    # 跨ぎで部分生存になっている既存の印付き区間が「全体生存」に変わって
    # digest 表示から生表示へ切り替わり、予算に数えていない増分で天井
    # (残す量) が破れる (Codex 指摘 2026-07-30)。
    if already_folded_message_ids:
        blocked = [
            i for i, m in enumerate(before_messages)
            if str(m.get("id")) in already_folded_message_ids
        ]
        if blocked:
            before_messages = list(before_messages)[max(blocked) + 1:]
            if not before_messages:
                return None

    pos = {str(m.get("id")): i for i, m in enumerate(before_messages)}
    window_ids = {str(x) for x in window_raw_ids}

    candidates = [e for e in entries if str(e.id) not in already_folded_entry_ids]
    if not candidates:
        return None

    # ① source 共有の推移閉包 (union-find)。before 側だけで束ねると、窓側の
    # source を共有するエントリが別段に分かれ、印戻し後に共有メッセージが
    # 二つの digest に属する (Codex 指摘 2026-07-30)。
    parent = list(range(len(candidates)))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(i: int, j: int) -> None:
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[rj] = ri

    source_owner: Dict[str, int] = {}
    for i, entry in enumerate(candidates):
        for sid in (str(s) for s in entry.source_ids):
            if sid in source_owner:
                _union(i, source_owner[sid])
            else:
                source_owner[sid] = i
    groups: Dict[int, List[Any]] = {}
    for i, entry in enumerate(candidates):
        groups.setdefault(_find(i), []).append(entry)

    # 閉包 → before 内の位置範囲 (span)。source の欠け・既存区間との交差も
    # ここで判定する。
    spans: List[Tuple[int, int, List[Any], bool]] = []
    for group in groups.values():
        sources = {str(s) for e in group for s in e.source_ids}
        idxs = sorted(pos[s] for s in sources if s in pos)
        if not idxs:
            continue
        broken = any(s not in pos and s not in window_ids for s in sources)
        broken = broken or bool(sources & already_folded_message_ids)
        spans.append((idxs[0], idxs[-1], group, broken))
    if not spans:
        return None
    spans.sort(key=lambda s: (s[0], s[1]))

    # ② 位置が重なる span は一つの「段」へ併合 (どちらか半分だけは開けないため)。
    units: List[List[Any]] = []  # [low, high, [entries], has_missing]
    for low, high, group, broken in spans:
        if units and low <= units[-1][1]:
            unit = units[-1]
            unit[1] = max(unit[1], high)
            unit[2].extend(group)
            unit[3] = unit[3] or broken
        else:
            units.append([low, high, list(group), broken])

    # index i から末尾 (旧 anchor 直前) までの提示文字数の累積。
    n = len(before_messages)
    suffix_chars = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_chars[i] = suffix_chars[i + 1] + len(
            str(before_messages[i].get("content") or "")
        )

    boundary: Optional[int] = None
    accepted: List[List[Any]] = []
    accepted_sources: Set[str] = set()
    prev_boundary = n  # これまで受理した領域の下端 (初期 = anchor の直前の直後)
    for unit in reversed(units):  # 新しい段から
        low, _high, unit_entries, has_missing = unit
        if has_missing:
            # 段のエントリが提示対象から消えた source を持つ — 全体を生で
            # 見せられない段は開けない。梯子はここまで。
            break
        if suffix_chars[low] > budget_chars:
            break
        # 候補領域 [low, prev_boundary) に「編纂対象なのにどの段にも被覆され
        # ない」メッセージが居たら止まる — それは編纂なしで忘れた過去で、
        # 跨いで戻すと忘却済みの内容が復活する (docstring 参照)。
        unit_sources = {str(s) for e in unit_entries for s in e.source_ids}
        forgotten = any(
            str(before_messages[i].get("id")) in eligible_message_ids
            and str(before_messages[i].get("id")) not in unit_sources
            and str(before_messages[i].get("id")) not in accepted_sources
            for i in range(low, prev_boundary)
        )
        if forgotten:
            break
        boundary = low
        accepted.append(unit)
        accepted_sources |= unit_sources
        prev_boundary = low
    if boundary is None:
        return None

    window_pos = {str(x): i for i, x in enumerate(window_raw_ids)}
    folds: List[FoldedRange] = []
    for unit in reversed(accepted):  # 時系列順に戻す
        unit_entries = unit[2]
        # **段ごとに 1 枚の圧縮区間** — 被覆が重なるエントリを別々の区間に
        # すると、印戻し後に同じメッセージが二つの digest に属して同じ体験が
        # 二重提示される (Codex 指摘 2026-07-30)。既存の畳み側も 1 区間に
        # 複数の chronicle_entry_ids を持つ形 (_attach_chronicle_refs) で、
        # digest 解決は複数エントリを連結して返す — 同じ形に揃える。
        before_mids = sorted(
            {str(s) for e in unit_entries for s in e.source_ids if str(s) in pos},
            key=lambda x: pos[x],
        )
        if not before_mids:
            continue
        # anchor 跨ぎのエントリは、現ウィンドウ側にある source も範囲に含める
        # (欠け判定では許容した分)。before 側だけにすると、後の印戻しで digest
        # がエントリ全体を要約する一方、窓側の source が生ログのまま残り、
        # 同じ体験が二重提示になる (Codex 指摘 2026-07-30)。
        window_mids = sorted(
            {
                str(s)
                for e in unit_entries
                for s in e.source_ids
                if str(s) in window_pos
            },
            key=lambda x: window_pos[x],
        )
        short_ids = [
            int(e.short_id)
            for e in unit_entries
            if getattr(e, "short_id", None) is not None
        ]
        folds.append(
            FoldedRange(
                message_ids=before_mids + window_mids,
                start_at=_epoch(before_messages[pos[before_mids[0]]]),
                end_at=_epoch(before_messages[pos[before_mids[-1]]])
                if not window_mids else None,
                chronicle_entry_ids=[str(e.id) for e in unit_entries],
                chronicle_short_ids=short_ids,
                presented_raw=True,
            )
        )
    if not folds:
        return None
    return RewindPlan(
        new_anchor_id=str(before_messages[boundary].get("id")),
        folds=folds,
        restored_chars=suffix_chars[boundary],
        restored_message_count=n - boundary,
    )
