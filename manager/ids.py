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


def entrance_id_for(region_id: str) -> str:
    """Region の入口 Building の ID。Region ID から決定的に導く。

    導出をここに置くのは、同じ文字列を組む場所が三箇所あるため — Region ID を
    選ぶ側 (入口 ID ごと空きを予約する)、入口を作る側、削除で「自動生成された
    入口か」を判定する側。一箇所だけ変えると、予約した ID と実際に作る ID が
    ずれたり、消えるはずの入口が孤児として残る。

    ``entrance_building_id`` と名付けない — AdminService.create_region が同名の
    引数を持っており、import を影にする。
    """
    return f"entrance_{region_id}"


#: 単一のパス要素として使えない文字 (Windows の禁止文字を含む)
_UNSAFE_PATH_CHARS = frozenset('/\\:*?"<>|')


def is_safe_path_component(value: str) -> bool:
    """``value`` を 1 階層のフォルダ名としてそのまま使っても安全か。

    :func:`is_valid_identifier` の文字種契約とは**別の、より緩い検査**。
    ID を ASCII に統一するかは設計判断 (issue 論点 3) で未決だが、
    「ディレクトリを脱出できないこと」だけは決着を待たずに要る — ペルソナ ID は
    ``~/.saiverse/personas/<id>/`` のフォルダ名になるため、区切り文字や ``..`` が
    通ると保存先が SAIVERSE_HOME の外へ出る。
    """
    if not value or value in {".", ".."}:
        return False
    if any(ch in _UNSAFE_PATH_CHARS for ch in value):
        return False
    return not any(ord(ch) < 32 for ch in value)


def path_component_error(kind: str, value: str) -> str:
    """パス要素として危険な ID を拒むエラー文字列。"""
    return (
        f"Error: {kind} is used as a folder name, so it may not contain path "
        f"separators, control characters, or any of {''.join(sorted(_UNSAFE_PATH_CHARS))} "
        f"(got: '{value}')."
    )


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
    ensure_unique: bool = False,
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

    ``ensure_unique=False`` (既定) では、``head`` が slug 化できた通常経路で
    ``exists`` を呼ばない。ID 衝突は呼び出し元が既存の「already exists」エラーで
    扱う — ユーザーが名前を選んだ ID (Building / Region) はこちら。連番で黙って
    避けると、指定した名前と違う ID が無言で生まれる。

    ``ensure_unique=True`` では通常経路でも空きを確かめ、埋まっていれば連番へ
    落ちる。**ユーザーが ID を選んでいない派生 ID (ペルソナ私室) はこちらを使う。**
    slug 化は情報を落とす写像なので (``A店`` と ``A森`` はどちらも ``a``)、名前の
    一意性を検査済みでも派生 ID は衝突しうる。既存の行を指す ID を作らせない責任は
    ここにある — 呼び出し元の重複検査は別の文字列 (AIID・名前) を見ているため
    素通しになる。
    """
    lead = [s for s in [slugify_identifier(prefix or "")] if s]
    tail_slugs = [s for s in (slugify_identifier(t or "") for t in tail) if s]
    head_slug = slugify_identifier(head or "")
    if head_slug:
        candidate = "_".join([*lead, head_slug, *tail_slugs])
        if not ensure_unique or not exists(candidate):
            return candidate

    stem_slug = slugify_identifier(stem or "")
    n = 1
    while True:
        numbered = f"{stem_slug}_{n}" if stem_slug else str(n)
        candidate = "_".join([*lead, numbered, *tail_slugs])
        if not exists(candidate):
            return candidate
        n += 1
