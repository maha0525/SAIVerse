"""提示窓の「畳まれた範囲」 — 元の時系列位置で digest に置き換えて見せる。

docs/intent/chronicle_eviction.md §6 (見せ方) の実装。

退場が episode 単位になったことで、窓の**途中**が畳まれる事態が生まれた
(古い会話 A を生で残したまま、新しい作業 B の古い部分だけを畳む)。畳んだ範囲を
head の Chronicle 枠に寄せると、新しい B のあらすじが古い A より前に立って時系列
が嘘になる。だから畳んだ範囲は**元の位置**で digest に置き換えて提示する。

置き換えには必ず圧縮マークの注釈を添える。注釈が無いと、その digest が「いま
システムから届いた新しいメッセージ」なのか「圧縮された自分の過去」なのか
ペルソナに区別がつかず、過去が新規情報に化けて記憶の連続性が壊れるため。

**畳んだ範囲は (persona, model) の持ち物** — 退役は model ごと
(beat_execution_context.md §3.2) なので、session_anchor 行に載せる。編纂
(Chronicle エントリ) 自体は persona に一度なので memory.db 側に一本で置く。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

LOGGER = logging.getLogger(__name__)

#: 置き換えメッセージであることの目印 (UI / 再入力の識別用)。
FOLDED_MARKER = "__folded_range__"


@dataclass
class SessionWindow:
    """いま提示されている窓の一式。

    ``raw`` (anchor 以降の生ログ) と ``presented`` (畳んだ範囲を digest に
    置き換えた実際の提示) を**両方**持つ。退場計画は提示を見て立てるが、
    「先頭から連続して畳まれた分は anchor が飲み込む」の判定は生ログの並びで
    しかできないため、片方だけでは足りない。
    """

    anchor_id: Optional[str]
    raw: List[Dict[str, Any]]
    presented: List[Dict[str, Any]]
    folds: List["FoldedRange"]

    @property
    def raw_ids(self) -> List[str]:
        return [str(m.get("id")) for m in self.raw]


@dataclass
class FoldedRange:
    """窓の中で digest に畳まれた 1 範囲。"""

    message_ids: List[str]
    start_at: Optional[int] = None
    end_at: Optional[int] = None
    #: 被覆する Chronicle エントリ (級1) の id。
    chronicle_entry_ids: List[str] = field(default_factory=list)
    #: 同エントリの ``ch:N`` 短縮参照の N (ペルソナへの案内に使う)。
    chronicle_short_ids: List[int] = field(default_factory=list)
    #: この範囲が 1 つの open episode の部分退場だった場合の episode_ref。
    episode_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_ids": list(self.message_ids),
            "start_at": self.start_at,
            "end_at": self.end_at,
            "chronicle_entry_ids": list(self.chronicle_entry_ids),
            "chronicle_short_ids": list(self.chronicle_short_ids),
            "episode_ref": self.episode_ref,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FoldedRange":
        return cls(
            message_ids=[str(x) for x in (data.get("message_ids") or [])],
            start_at=data.get("start_at"),
            end_at=data.get("end_at"),
            chronicle_entry_ids=[str(x) for x in (data.get("chronicle_entry_ids") or [])],
            chronicle_short_ids=[int(x) for x in (data.get("chronicle_short_ids") or [])],
            episode_ref=data.get("episode_ref"),
        )


def serialize_folds(folds: Sequence[FoldedRange]) -> Optional[str]:
    """session_anchor 行に載せる JSON。空なら None (列を NULL に戻す)。"""
    if not folds:
        return None
    return json.dumps([f.to_dict() for f in folds], ensure_ascii=False)


def deserialize_folds(raw: Optional[str]) -> List[FoldedRange]:
    """session_anchor 行の JSON を読む。壊れていたら空 (= 生ログ提示に縮退)。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("[window] folded ranges JSON is broken; falling back to raw log")
        return []
    if not isinstance(data, list):
        return []
    out: List[FoldedRange] = []
    for item in data:
        if isinstance(item, dict):
            try:
                out.append(FoldedRange.from_dict(item))
            except (TypeError, ValueError):
                LOGGER.warning("[window] skipping malformed folded range entry")
    return out


def prune_folds(
    folds: Sequence[FoldedRange], window_message_ids: Sequence[str],
) -> List[FoldedRange]:
    """窓から出た (anchor より古くなった) 範囲を落とす。

    anchor が前進すると、その範囲はもう提示されない = 穴として持ち続ける意味が
    無い。**一部でも窓に残っている範囲は残す** — 途中まで提示に効いている穴を
    消すと、その分の生ログが復活して窓が膨らむため。
    """
    live = set(window_message_ids)
    return [f for f in folds if any(mid in live for mid in f.message_ids)]


def _build_notice(fold: FoldedRange, digest_text: str) -> str:
    """圧縮マークつきの置き換え本文を組む (§6)。

    伝えるべきは二つ — ①ここに本来あった体験が圧縮されて概要だけ見えている
    ②思い出したければスペルで中身を再確認できる。過去を消されたのではなく
    畳まれているだけだ、が伝わる文面にする。
    """
    count = len(fold.message_ids)
    if fold.chronicle_short_ids:
        refs = " / ".join(f"ch:{n}" for n in fold.chronicle_short_ids)
        how = f"中身を思い出したいときは「記憶のページを机に開く」で {refs} を開いてください。"
    else:
        how = "中身を思い出したいときは Chronicle の検索・詳細読みで辿れます。"
    body = digest_text.strip()
    return (
        "<system>\n"
        "【この位置の記憶は圧縮されています】\n"
        f"長い出来事だったため、ここにあった {count} 件の記録はあらすじに畳まれました。"
        "消えたのではなく、畳まれているだけです。"
        f"{how}\n\n"
        "--- あらすじ ---\n"
        f"{body}\n"
        "</system>"
    )


def apply_folds(
    messages: Sequence[Dict[str, Any]],
    folds: Sequence[FoldedRange],
    resolve_digest: Callable[[FoldedRange], Optional[str]],
) -> List[Dict[str, Any]]:
    """窓の生ログを、畳まれた範囲だけ digest 置き換えに差し替えて返す。

    Args:
        messages: 窓のメッセージ (created_at 昇順)。
        folds: 畳まれた範囲。
        resolve_digest: 範囲の digest 本文を引く関数。None を返したら
            **その範囲は生ログのまま残す** — 記憶の連続性が軽量化より上位なので、
            あらすじが引けないときに黙って体験を消すことはしない (§2 不変条件)。

    Returns:
        置き換え後のメッセージ列 (時系列順)。
    """
    if not folds:
        return list(messages)

    # 置き換えを立てる位置は「その範囲のうち**窓に残っている最初のメッセージ**」。
    # 範囲の先頭 (message_ids[0]) 固定にすると、anchor が範囲の途中まで前進した
    # 穴で先頭が窓の外に出て、置き換えも生ログも出ない = 体験が黙って消える。
    live_ids = {str(m.get("id")) for m in messages}
    head_of: Dict[str, FoldedRange] = {}
    member_of: Dict[str, FoldedRange] = {}
    for fold in folds:
        if not fold.message_ids:
            continue
        for mid in fold.message_ids:
            member_of[mid] = fold
        for mid in fold.message_ids:
            if mid in live_ids:
                head_of[mid] = fold
                break

    resolved: Dict[int, Optional[str]] = {}

    def _digest_for(fold: FoldedRange) -> Optional[str]:
        key = id(fold)
        if key not in resolved:
            try:
                resolved[key] = resolve_digest(fold)
            except Exception:
                LOGGER.warning(
                    "[window] failed to resolve digest for folded range; "
                    "keeping raw log", exc_info=True,
                )
                resolved[key] = None
        return resolved[key]

    out: List[Dict[str, Any]] = []
    for msg in messages:
        mid = str(msg.get("id"))
        fold = member_of.get(mid)
        if fold is None:
            out.append(msg)
            continue
        digest = _digest_for(fold)
        if digest is None:
            # 引けなかった範囲は生のまま通す (fail-open = 体験を消さない)。
            out.append(msg)
            continue
        if head_of.get(mid) is fold:
            out.append(_placeholder(fold, digest, msg))
        # 先頭以外は落とす (= 畳まれた)
    return out


def _placeholder(
    fold: FoldedRange, digest_text: str, first_msg: Dict[str, Any],
) -> Dict[str, Any]:
    """置き換えメッセージ。

    role は ``user`` + ``<system>`` 包み — SAIMemory の system メッセージが提示面へ
    出るときと同じ形 (saiverse_memory/adapter.py の payload 変換) に揃える。機構の
    声を機構の声として、ペルソナの発話と混ぜない。
    """
    return {
        # 位置は「窓に残っている最初のメッセージ」なので id / 時刻もそれに合わせる
        # (範囲の先頭が窓の外に出ていても、置き換えは窓の中の正しい位置に立つ)。
        "id": f"folded:{first_msg.get('id')}",
        "role": "user",
        "content": _build_notice(fold, digest_text),
        "created_at": first_msg.get("created_at", fold.start_at),
        "line_role": "main_line",
        "scope": "committed",
        "metadata": {
            FOLDED_MARKER: True,
            "message_count": len(fold.message_ids),
            "chronicle_entry_ids": list(fold.chronicle_entry_ids),
            "chronicle_short_ids": list(fold.chronicle_short_ids),
            "episode_ref": fold.episode_ref,
        },
    }
