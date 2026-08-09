"""Building ID / Region ID の文字種契約と生成式。

docs/issues/building_id_no_charset_constraint.md 論点 1 (新規作成の口を塞ぐ)。

Building ID はログのフォルダパス (``~/.saiverse/cities/<city>/buildings/<id>/``)、
``saiverse://building/<id>/image`` URI、API のパス引数へ素で入る永続キーなので、
ASCII 英数字 + ``_`` + ``-`` (先頭は英数字) だけを許す。Region ID も入口 Building の
ID (``entrance_<region_id>``) の材料になるため、同じ契約に従わせる。

**契約と生成式をこの 1 枚へ集約する。** 各作成経路が自前の f-string で ID を
組むと、片方だけ塞いだ穴が残る — 2026-08-09 に実際そうなった: create_building
だけ契約を持ち、Region 入口・ペルソナ個室・ブループリント個室の 3 経路が無検証の
まま日本語 ID を作れる状態で残っていた。
"""

import re
from typing import Callable

#: 永続キーとして許す文字種。先頭は英数字、以降は英数字と ``_`` ``-``。
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def slugify_identifier(text: str) -> str:
    """名前を ID 用の ASCII slug に落とす (小文字化・空白→'_'・対象外文字は除去)。

    日本語名のように ASCII が残らない入力では空文字列を返す — 呼び出し元が
    :func:`build_identifier` の連番フォールバックに切り替える。
    """
    out = []
    for ch in text.lower().strip():
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    slug = re.sub(r"_{2,}", "_", "".join(out))
    return slug.strip("_-")


def is_valid_identifier(value: str) -> bool:
    """``value`` が文字種契約を満たすか。"""
    return bool(value) and bool(IDENTIFIER_RE.match(value))


def charset_error(kind: str, value: str) -> str:
    """契約違反を伝えるエラー文字列 (API・ツールの戻り値にそのまま出る)。"""
    return (
        f"Error: {kind} may contain only ASCII letters, digits, '_' and '-', "
        f"and must start with a letter or digit (got: '{value}')."
    )


def build_identifier(
    head: str,
    *tail: str,
    exists: Callable[[str], bool],
    prefix: str = "",
    stem: str = "",
) -> str:
    """``head`` (名前由来) を slug 化し、``tail`` と ``_`` で連結した ID を返す。

    ``head`` の slug が空になる名前 (日本語名など) では、名前の位置に連番を据え、
    ``exists`` が False を返す最初の候補を採る。読み変換 (ローマ字化) は導入
    しない — issue 論点 1 の裁定。

    ``prefix`` は全候補の先頭に付く語。``stem`` は連番のときだけ名前の位置に置く
    語で、空なら連番だけが立つ。前者は ID の見出し、後者は「名前が無いもの」の
    呼び名なので別々に持つ::

        Building: stem="building"    → tea_house_city_a  / building_1_city_a
        Region:   prefix="region"    → region_mist_valley_city_a / region_1_city_a

    ``head`` が slug 化できた通常経路では ``exists`` を呼ばない。ID 衝突は
    呼び出し元が既存の「already exists」エラーで扱う (連番で黙って避けると、
    ユーザーが指定した名前と違う ID が無言で生まれる)。
    """
    lead = [s for s in [slugify_identifier(prefix or "")] if s]
    tail_slugs = [s for s in (slugify_identifier(t or "") for t in tail) if s]
    head_slug = slugify_identifier(head or "")
    if head_slug:
        return "_".join([*lead, head_slug, *tail_slugs])

    stem_slug = slugify_identifier(stem or "")
    n = 1
    while True:
        numbered = f"{stem_slug}_{n}" if stem_slug else str(n)
        candidate = "_".join([*lead, numbered, *tail_slugs])
        if not exists(candidate):
            return candidate
        n += 1
