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
- **A1 入口は広く、出口は一本** (2026-07-15 追加。intent doc の Q4 を上書きする):
  旧 prefix (``m:`` / ``ch:`` / ``p:`` / ``c:``) は ``RefKind.aliases`` として
  **受理だけ**する。``to_short_ref`` / ``to_uri`` が出すのは常に正典単語なので、
  世界に流れる新しい文字列は一種類に保たれる。

  Q4 は当初「旧記法の受理は残さない (中間状態を作らない)」と裁定していた。これは
  **全ての生産者を切替側が制御できる**という前提の上に立っていた裁定で、その前提は
  破れた — ペルソナ自身の記憶 (memory.db のメッセージ本文) に旧 prefix が焼き付いて
  おり、これは移行できない「データ」であって、ペルソナは自分の過去ログを読んで
  旧記法を再生産し続ける。制御できない生産者が存在する以上、受理を閉じると
  「正しく書いたのに蹴られる」住人が出る (2026-07-15、``memopedia:45`` を撃った
  ペルソナが Atlas に蹴られた件が発端)。Q4 の判断が誤りだったのではなく、
  適用範囲の外の事態が起きた。詳細は docs/intent/reference_addressing.md の Q4 追記。

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
    aliases: Tuple[str, ...] = ()  # 受理のみする旧 prefix ("m" → memopedia 等)。
                                   # 出力は必ず word 側 (下記 A1 を参照)


# 正典の kind 一覧。ペルソナ依存 (persona_scoped) の kind は short_id が
# per-persona なので URI にペルソナを含めないと一意にならない (I1)。item は
# world スコープ (saiverse.db に世界で1つ) なのでグローバル。
_KIND_LIST = [
    RefKind("track", True, True),
    RefKind("task", True, True),        # desire は task に統合 (状態ラベル化)
    RefKind("episode", True, True),     # 出来事 (life_concept_map.md §8.1。saiverse/episodes.py)
    RefKind("memopedia", True, True, aliases=("m",)),
    RefKind("message", True, False),
    # P2a (2026-07-10): arasuji_entries に short_id を追加 (Memory Atlas の
    # 短縮参照)。memopedia と同じ経緯で numeric_key を True に更新。
    RefKind("chronicle", True, True, aliases=("ch",)),
    # Memory Atlas のクリップ (sai_memory/clips.py) とコア記憶
    # (sai_memory/core_memory.py)。2026-07-15 に Atlas 独自の軽量 prefix 系列を
    # 本レジストリへ吸収した際に登記 (下記 A1)。コア記憶の key は per-persona で
    # never-reuse な core_id (int)。素の ``core`` (全件) は kind:key 形ではない
    # ので参照ではなく、Atlas 側の特殊ページ扱いのまま。
    RefKind("clip", True, True, aliases=("p",)),
    RefKind("core", True, True, aliases=("c",)),
    RefKind("item", False, True),
    RefKind("image", False, False),
    RefKind("document", False, False),
    RefKind("persona", False, False),
    RefKind("building", False, False),
]
KINDS = {k.word: k for k in _KIND_LIST}

#: 別名 → 正典単語。受理側だけが引く (出力は常に正典単語 = A1)。
_ALIAS_TO_WORD = {
    alias: k.word for k in _KIND_LIST for alias in k.aliases
}


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


def normalize_short_ref(text: str) -> str:
    """参照を正典の短縮形へ寄せる (``m:45`` → ``memopedia:45``)。

    永続化された ref (机の主キー・クリップの ``pasted_to`` 等) を新表記へ揃える
    backfill 用。**解釈できない文字列はそのまま返す** — 正規化ループが未知の値を
    壊さないため。他ペルソナを指す URI も、短縮形にすると所有者が落ちて別人の
    ページに化けるのでそのまま返す。
    """
    s = (text or "").strip()
    if not s:
        return text
    try:
        parsed = parse_ref(s)
    except ValueError:
        return text
    if parsed.persona is not None and not parsed.is_self:
        return text
    return to_short_ref(parsed.kind, parsed.key)


def parse_ref(text: str) -> ParsedRef:
    """短縮参照 (``track:2``) または URI (``saiverse://self/track/2``) を解析する。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty reference")
    if s.startswith(URI_PREFIX):
        return _parse_uri(s)
    return _parse_short_ref(s)


def _canonical_word(word: str) -> str:
    """別名 (``m`` / ``ch`` / ``p`` / ``c``) を正典単語へ寄せる。未知の語はそのまま。"""
    return _ALIAS_TO_WORD.get(word, word)


def _parse_short_ref(s: str) -> ParsedRef:
    if ":" not in s:
        raise ValueError(f"not a reference: {s!r}")
    word, key = s.split(":", 1)
    word = _canonical_word(word)
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
    head_word = _canonical_word(segs[0])
    head_kind = KINDS.get(head_word)
    if head_kind is not None and not head_kind.persona_scoped:
        if len(segs) < 2:
            raise ValueError(f"URI missing key: {s!r}")
        return ParsedRef(kind=head_word, key=segs[1], persona=None, subpath=tuple(segs[2:]))

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
    kind = _canonical_word(segs[idx])
    if kind not in KINDS:
        raise ValueError(f"unknown URI kind: {kind!r} in {s!r}")
    return ParsedRef(
        kind=kind, key=segs[idx + 1], persona=persona, subpath=tuple(segs[idx + 2:])
    )
