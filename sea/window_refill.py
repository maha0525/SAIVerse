"""読み戻しの部品 — 目標量を下回る提示ウィンドウであらすじを開き直す (arasuji_levels.md §15)。

**純関数のみ** — DB も LLM も触らない (sea/eviction_plan.py と同じ層)。読み戻しの
本体 (どのあらすじを開くかのループ) は sea/session_lifecycle.py の
``_plan_window_refill`` にあり、ここはその部品 — 「窓の中で開ける区間を新しい順に
並べる」と「開いた範囲を一枚の圧縮区間に組む」だけを提供する。

設計の芯 (2026-09-05 まはー裁定、
docs/issues/refill_reads_by_budget_instead_of_arasuji_unit.md §裁定の確定):

- 窓の会話文が目標量 (残す量) を下回っていたら、あらすじがどこにあるかを
  問わず — 窓の中で digest 表示中の圧縮区間も、起点をまたぐ区間も、起点より
  古い側の一次あらすじも — **いちばん新しいあらすじから順に丸ごと開く**。
  一つ開くたびに提示を組み直して会話文を測り、目標量に達したら終了。
  読む範囲を字数で切る「予算」は無い。目標量の超過は問題ない (目標量は
  下限であって上限ではない)。
- 開き直しは帳簿の付け替えだけで LLM を呼ばない。圧縮区間の記録は消さず
  「生で見せる」印 (``presented_raw``) を付ける — head のあらすじ枠の除外名簿は
  効き続け、再畳みは印戻しで既存あらすじを再利用する。
- 材料を共有する・範囲が重なるあらすじは一枚の圧縮区間にまとめて開く
  (同じ行が二つの区間に属すると digest が二重提示になる)。
- 材料に読めない行があるあらすじでも止まらない — 読める行を全部生で戻して開く。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sea.session_window import FOLDED_MARKER, FoldedRange

LOGGER = logging.getLogger(__name__)


def _epoch(msg: Dict[str, Any]) -> Optional[int]:
    try:
        return int(msg.get("created_at"))
    except (TypeError, ValueError):
        return None


def openable_folds_newest_first(
    folds: Sequence[FoldedRange],
    raw_messages: Sequence[Dict[str, Any]],
    presented_messages: Sequence[Dict[str, Any]],
) -> List[FoldedRange]:
    """窓の圧縮区間のうち「いま digest 表示になっているもの」(= 開ける対象) を新しい順に返す。

    Args:
        folds: 現ウィンドウの圧縮区間 (呼び出し側の実体をそのまま渡す —
            選ぶだけで印は付けない)。
        raw_messages: 提示ウィンドウの生ログ (anchor 以降、時系列昇順)。
        presented_messages: 現在の提示 (digest 置き換え済み)。置き換えが実際に
            立っているかの判定に使う。

    対象になるのは:

    - **起点をまたぐ区間** (``message_ids`` の一部が窓の生ログに無いもの) —
      常に対象。提示は digest に倒れていて (apply_folds は部分生存の区間の
      ``presented_raw`` を尊重しない)、またいだ側の行は起点を戻さないと提示に
      現れない。
    - **窓に完全に収まる区間**のうち、``presented_raw`` が付いておらず、
      置き換え (digest) が実際に提示に立っているもの。置き換えが提示に無い
      区間はあらすじが引けず既に生で出ている (fail-open) — 開くものが無い。

    並び順は「区間が覆う、提示中でいちばん新しい行」の新しい順。どの区間も
    起点より古い側の一次あらすじより新しい内容を覆っている (窓の行は起点より
    新しい) ので、呼び出し側はこのリストを先に開いてから古い側へ進めばよい。
    """
    order = {str(m.get("id")): i for i, m in enumerate(raw_messages)}

    # 提示中の置き換えメッセージ。id は ``folded:<提示に残る最初のメッセージ id>``
    # (sea/session_window.py の _placeholder)。
    placeholder_heads = set()
    for m in presented_messages:
        meta = m.get("metadata")
        if isinstance(meta, dict) and meta.get(FOLDED_MARKER):
            mid = str(m.get("id"))
            if mid.startswith("folded:"):
                placeholder_heads.add(mid[len("folded:"):])

    chosen: List[FoldedRange] = []
    for fold in folds:
        if not fold.message_ids:
            continue
        straddling = any(mid not in order for mid in fold.message_ids)
        if straddling:
            chosen.append(fold)
            continue
        if fold.presented_raw:
            continue  # 既に生で見せている — 開くものが無い
        head = next((mid for mid in fold.message_ids if mid in order), None)
        if head is None or head not in placeholder_heads:
            continue  # digest が提示に立っていない = 既に生 (fail-open)
        chosen.append(fold)

    def _newest_pos(fold: FoldedRange) -> int:
        positions = [order[mid] for mid in fold.message_ids if mid in order]
        return max(positions) if positions else -1

    chosen.sort(key=_newest_pos, reverse=True)
    return chosen


def merge_refill_fold(
    entries: Sequence[Any],
    ordered_messages: Sequence[Dict[str, Any]],
    existing_folds: Sequence[FoldedRange] = (),
) -> Tuple[Optional[FoldedRange], List[FoldedRange]]:
    """開いた範囲を覆うあらすじ群から、一枚の ``presented_raw`` の圧縮区間を組む。

    Args:
        entries: 開いた範囲 (生へ戻した行 + 現ウィンドウ) に材料を持つ一次
            あらすじ (``id`` / ``short_id`` / ``source_ids`` を持つもの)。
            材料を共有する・範囲が重なるあらすじをまとめて一枚に束ねる —
            同じ行が二つの区間に属すると、再畳み (印戻し) 後に digest が
            二重提示になるため。digest の解決は 1 区間の複数エントリを連結して
            返す (既存の畳み側と同じ形)。
        ordered_messages: 提示対象の行 (開いた行 + 窓の生ログ、時系列昇順)。
            材料のうちここに無い id (読めない行) は区間の行には**載せない** —
            載せると印付きの区間が部分生存と見なされて digest 提示に倒れ、
            開いたはずの行が縮む。あらすじ id は載せる (head の除外名簿として
            効かせる)。
        existing_folds: 窓の既存の圧縮区間。行またはあらすじ id を共有するものは
            新しい区間へ**併合**し、戻り値の absorbed で知らせる — 呼び出し側は
            窓の記録からそれらを外し、組んだ一枚に置き換える。

    Returns:
        ``(組んだ区間 or None, 併合された既存区間のリスト)``。読める行が
        一つも無ければ区間は組めない (None)。
    """
    pos = {str(m.get("id")): i for i, m in enumerate(ordered_messages)}
    source_ids = {str(s) for e in entries for s in e.source_ids}
    entry_id_set = {str(e.id) for e in entries}

    absorbed = [
        f for f in existing_folds
        if (source_ids & {str(mid) for mid in f.message_ids})
        or (entry_id_set & {str(eid) for eid in f.chronicle_entry_ids})
    ]

    mids = sorted(
        {sid for sid in source_ids if sid in pos}
        | {
            str(mid)
            for f in absorbed
            for mid in f.message_ids
            if str(mid) in pos
        },
        key=lambda x: pos[x],
    )
    if not mids:
        return None, []

    entry_ids: List[str] = []
    short_ids: List[int] = []
    for fold in absorbed:  # 既存区間のあらすじを先に (記録の連続性)
        for eid in fold.chronicle_entry_ids:
            if str(eid) not in entry_ids:
                entry_ids.append(str(eid))
        for sid in fold.chronicle_short_ids:
            if int(sid) not in short_ids:
                short_ids.append(int(sid))
    for e in entries:
        if str(e.id) not in entry_ids:
            entry_ids.append(str(e.id))
        if getattr(e, "short_id", None) is not None and int(e.short_id) not in short_ids:
            short_ids.append(int(e.short_id))

    return FoldedRange(
        message_ids=mids,
        start_at=_epoch(ordered_messages[pos[mids[0]]]),
        end_at=_epoch(ordered_messages[pos[mids[-1]]]),
        chronicle_entry_ids=entry_ids,
        chronicle_short_ids=short_ids,
        presented_raw=True,
    ), absorbed
