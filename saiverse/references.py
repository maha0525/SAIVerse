"""参照アドレッシングの統一グラマー層 (docs/intent/reference_addressing.md)。

ペルソナがものを指す短縮参照 (``track:2`` / ``item:5`` 等) と URI
(``saiverse://self/track/2`` 等) の**書式と相互変換**を一箇所に集約する純粋な
文字列レイヤー。DB 解決 (キー → 実体) は各 kind の既存 resolver に委譲する
(本モジュールは実体を一切触らない)。

設計原則 (intent doc):
- P6 名前空間は単語で統一 (単一文字 prefix は使わない): track / task / item / …
- P7 ペルソナに依存する kind の URI はペルソナを含む (``self`` または ``city/name``)。
  URI のパスが種類を表すので、末尾は素のキー (短縮 prefix を重ねない)。
- I2 短縮参照と URI は相互変換でき、解決結果が一致する。

このモジュール自体は文字列書式だけを司る。Phase 1 の生成側は ``to_short_ref`` /
``to_uri`` を、Phase 2 の解決側は ``parse_ref`` を通して kind を判定してから各
resolver に振り分ける。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

URI_PREFIX = "saiverse://"
SELF = "self"


@dataclass(frozen=True)
class RefKind:
    """1 つの参照種別の書式上の性質。"""

    word: str            # 正典の名前空間単語 ("track", "item", …)
    persona_scoped: bool  # True: URI にペルソナ (self / city/name) を含む
    numeric_key: bool     # True: short_id (整数)、False: uuid / filename / id (不透明)


# 正典の kind 一覧。ペルソナ依存 (persona_scoped) の kind は short_id が
# per-persona なので URI にペルソナを含めないと一意にならない (I1)。item は
# world スコープ (saiverse.db に世界で1つ) なのでグローバル。
_KIND_LIST = [
    RefKind("track", True, True),
    RefKind("task", True, True),        # desire は task に統合 (状態ラベル化)
    RefKind("memopedia", True, True),
    RefKind("message", True, False),
    RefKind("chronicle", True, False),
    RefKind("item", False, True),
    RefKind("image", False, False),
    RefKind("document", False, False),
    RefKind("persona", False, False),
    RefKind("building", False, False),
]
KINDS = {k.word: k for k in _KIND_LIST}


@dataclass(frozen=True)
class ParsedRef:
    """``parse_ref`` の結果。実体解決前の、書式から読み取れる情報だけを持つ。"""

    kind: str                       # KINDS のキー
    key: str                        # 実体キー (文字列。"2" / uuid / filename)
    persona: Optional[str] = None   # None=グローバル / "self" / ペルソナ指定
                                    # ("air_city_a" 形か "city_a/air" 形。呼び出し側が
                                    #  uri_resolver で実 persona_id に解決する)
    subpath: Tuple[str, ...] = field(default_factory=tuple)  # 末尾の追加セグメント
                                    # (``persona/{id}/image`` の "image" 等)

    @property
    def is_self(self) -> bool:
        return self.persona == SELF


def _require_kind(kind: str) -> RefKind:
    k = KINDS.get(kind)
    if k is None:
        raise ValueError(f"unknown reference kind: {kind!r}")
    return k


def to_short_ref(kind: str, key) -> str:
    """``kind:key`` 形の短縮参照を作る (例: ``track:2`` / ``item:5``)。"""
    _require_kind(kind)
    return f"{kind}:{key}"


def to_uri(kind: str, key, *, persona: Optional[str] = None, subpath: Tuple = ()) -> str:
    """``saiverse://`` URI を作る。

    Args:
        persona: ペルソナ依存 kind のときの所有者。``None`` は自分 (``self``)。
            他ペルソナは ``"city_a/air"`` のような path 断片を渡す。グローバル
            kind (item/image/…) では無視される。
        subpath: 末尾に付く追加セグメント (``persona/{id}/image`` の ``("image",)``)。
    """
    k = _require_kind(kind)
    tail = "/".join(str(s) for s in subpath)
    tail = f"/{tail}" if tail else ""
    if not k.persona_scoped:
        return f"{URI_PREFIX}{kind}/{key}{tail}"
    owner = persona or SELF
    return f"{URI_PREFIX}{owner}/{kind}/{key}{tail}"


def parse_ref(text: str) -> ParsedRef:
    """短縮参照 (``track:2``) または URI (``saiverse://self/track/2``) を解析する。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty reference")
    if s.startswith(URI_PREFIX):
        return _parse_uri(s)
    return _parse_short_ref(s)


def _parse_short_ref(s: str) -> ParsedRef:
    if ":" not in s:
        raise ValueError(f"not a reference: {s!r}")
    word, key = s.split(":", 1)
    k = KINDS.get(word)
    if k is None:
        raise ValueError(f"unknown reference kind: {word!r}")
    if not key:
        raise ValueError(f"empty key in reference: {s!r}")
    # 短縮参照はペルソナ依存 kind なら自分 (self) が暗黙のスコープ。
    return ParsedRef(kind=word, key=key, persona=(SELF if k.persona_scoped else None))


def _parse_uri(s: str) -> ParsedRef:
    rest = s[len(URI_PREFIX):].split("?", 1)[0]
    segs = [seg for seg in rest.split("/") if seg != ""]
    if not segs:
        raise ValueError(f"empty URI: {s!r}")

    # 先頭がグローバル kind の単語ならグローバル参照。
    head_kind = KINDS.get(segs[0])
    if head_kind is not None and not head_kind.persona_scoped:
        if len(segs) < 2:
            raise ValueError(f"URI missing key: {s!r}")
        return ParsedRef(kind=segs[0], key=segs[1], persona=None, subpath=tuple(segs[2:]))

    # ペルソナスコープ: 先頭は "self" (1 セグメント) か "city/name" (2 セグメント)。
    if segs[0] == SELF:
        persona = SELF
        idx = 1
    else:
        if len(segs) < 4:
            raise ValueError(f"persona-scoped URI too short: {s!r}")
        persona = f"{segs[0]}/{segs[1]}"
        idx = 2

    if len(segs) < idx + 2:
        raise ValueError(f"URI missing kind/key: {s!r}")
    kind = segs[idx]
    if kind not in KINDS:
        raise ValueError(f"unknown URI kind: {kind!r} in {s!r}")
    return ParsedRef(
        kind=kind, key=segs[idx + 1], persona=persona, subpath=tuple(segs[idx + 2:])
    )
