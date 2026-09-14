"""スペル機構が使えるか (``AI.SPELL_ENABLED``) の判定と、テンプレートの条件ブロック。

``SPELL_ENABLED=false`` のペルソナには「スペルがあるから意味を持つ文章」を一切
渡さない (docs/intent/spell_disabled_mode.md)。判定を節ごとに書き写すと片方だけ
直した日に食い違うので、head の各 section はここの :func:`resolve_spell_enabled`
だけを使う。

判定は **capture 時**に解決して snapshot へ焼き込むこと。render 時に DB を引くと
head が (persona, model) 固定でなくなり、prefix cache の前提が壊れる
(docs/intent/cached_head_architecture.md §5.3)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

#: common.txt の条件ブロック。strip 後にその行全体が一致する行だけをマーカーと
#: して扱う (行の途中に現れた同じ文字列は本文)。両方向あり — 有効側 (if_spell_
#: enabled) はスペルが使えるときだけ載り、無効側 (if_spell_disabled) は使えない
#: ときだけ載る。
SPELL_BLOCK_OPEN = "{if_spell_enabled}"
SPELL_BLOCK_CLOSE = "{end_if_spell_enabled}"
NO_SPELL_BLOCK_OPEN = "{if_spell_disabled}"
NO_SPELL_BLOCK_CLOSE = "{end_if_spell_disabled}"

#: 開きマーカー → (閉じマーカー, このブロックが載る spell_enabled の値)
_BLOCKS = {
    SPELL_BLOCK_OPEN: (SPELL_BLOCK_CLOSE, True),
    NO_SPELL_BLOCK_OPEN: (NO_SPELL_BLOCK_CLOSE, False),
}
_ALL_MARKERS = (
    SPELL_BLOCK_OPEN, SPELL_BLOCK_CLOSE,
    NO_SPELL_BLOCK_OPEN, NO_SPELL_BLOCK_CLOSE,
)


def resolve_spell_enabled(ctx: Any) -> bool:
    """``ctx`` (LineHeadInput) のペルソナでスペル機構が有効か。

    manager / persona_id が無い、DB を引けない、行が無い — どの場合も False
    (= スペル前提の文章を出さない側) に倒す。元は
    ``SpellListSection._resolve_enabled`` にあった実装。
    """
    return resolve_spell_enabled_for(
        getattr(ctx, "manager", None), getattr(ctx, "persona_id", None),
    )


def resolve_spell_enabled_for(manager: Any, persona_id: Optional[str]) -> bool:
    """manager + persona_id から ``AI.SPELL_ENABLED`` を引く (ctx を持たない呼び手用)。

    manager / persona_id / SessionLocal が無い、セッションが作れない、問い合わせが
    失敗する — どの場合も False に倒し、例外は呼び手へ漏らさない。
    """
    if not manager or not persona_id:
        return False
    session_factory = getattr(manager, "SessionLocal", None)
    if not session_factory:
        return False
    # セッションを作るところも try の中に入れる。ここで例外が漏れると、この関数を
    # 頭で呼ぶ側 (get_system_prompt など) のプロンプト取得そのものが落ちる —
    # 「DB を引けないときは False に倒す」という約束は、セッションが作れない場合も
    # 含む。
    db = None
    try:
        db = session_factory()
        from database.models import AI as AIModel
        ai = db.query(AIModel).filter_by(AIID=persona_id).first()
        return bool(ai.SPELL_ENABLED) if ai else False
    except Exception:
        LOGGER.warning(
            "spell_gate: failed to resolve SPELL_ENABLED for persona=%s",
            persona_id, exc_info=True,
        )
        return False
    finally:
        if db is not None:
            db.close()


def _marker_of(line: str) -> Optional[str]:
    """``line`` がマーカー行ならそのマーカー文字列を、本文行なら ``None`` を返す。

    マーカーと認めるのは「前後の空白と先頭の BOM を取り除いた行全体が、マーカー
    そのもの」の場合だけ (行の途中に現れた同じ文字列は本文)。BOM (``\\ufeff``) は
    ``str.strip()`` では落ちないので明示的に取り除く — BOM 付きで保存された
    テンプレートの 1 行目が本文扱いになると、その先の閉じが「閉じだけ」と判定
    されて fail-safe に落ち、無効のペルソナにスペル前提の文章が残ってしまう。

    判定規則はこの関数だけが持つ。検査 (:func:`_markers_are_well_formed`) と展開
    (:func:`apply_spell_markers`) が別々の物差しを持つと、片方だけ直した日に
    食い違う。
    """
    stripped = line.strip().lstrip("\ufeff").strip()
    return stripped if stripped in _ALL_MARKERS else None


def _markers_are_well_formed(lines: list[str]) -> bool:
    """開き/閉じが 1 対 1 で、入れ子も交差もしていないか。

    交差 = ``{if_spell_enabled}`` … ``{end_if_spell_disabled}`` のように、開いた
    ブロックと違う種類の閉じで閉じようとする形。
    """
    open_marker: Optional[str] = None
    for line in lines:
        marker = _marker_of(line)
        if marker is None:
            continue
        if marker in _BLOCKS:
            if open_marker is not None:   # 入れ子
                return False
            open_marker = marker
        else:                             # 閉じマーカー
            if open_marker is None:       # 閉じだけ
                return False
            if _BLOCKS[open_marker][0] != marker:   # 交差
                return False
            open_marker = None
    return open_marker is None


def apply_spell_markers(
    template: str, spell_enabled: bool, *, source: str = "common.txt",
) -> str:
    """条件ブロックを解決する (両方向)。

    - ``{if_spell_enabled}`` … ``{end_if_spell_enabled}``:
      有効時はマーカー行だけ取り除いて中身を残し、無効時はブロックごと落とす。
    - ``{if_spell_disabled}`` … ``{end_if_spell_disabled}``: その逆。
    - マーカーを 1 つも含まないテンプレート (ユーザー上書き) は素通し
      — 文字列として一切変わらない。
    - **fail-safe**: 対応が取れないマーカー (閉じ忘れ・閉じだけ・入れ子・交差) を
      見つけたら、中身は全部残してマーカー行だけを落とし、警告を出す。人格を担う
      文章をテンプレートの書き損じで黙って落とさないため
      (docs/intent/spell_disabled_mode.md §4-4)。
    - マーカー行の見分けは :func:`_marker_of` に一本化してある — 先頭行に UTF-8
      BOM が付いたテンプレートでもマーカーとして働く。本文行は BOM も含めて
      1 文字も変えずにそのまま出す。

    有効側の展開結果は「テンプレートからマーカー行と無効側ブロックを取り除いた
    もの」と厳密に一致する — これが「スペル有効のペルソナのプロンプトは 1 バイトも
    変わらない」不変条件の実装側の姿 (同 §4-2)。
    """
    if not any(marker in template for marker in _ALL_MARKERS):
        return template

    lines = template.split("\n")
    keep_all = not _markers_are_well_formed(lines)
    if keep_all:
        LOGGER.warning(
            "spell_gate: unbalanced conditional markers in %s; keeping all body text",
            source,
        )

    out: list[str] = []
    keep_body = True          # ブロックの外は常に残す
    for line in lines:
        marker = _marker_of(line)
        if marker in _BLOCKS:
            keep_body = keep_all or (_BLOCKS[marker][1] is spell_enabled)
            continue
        if marker is not None:            # 閉じマーカー
            keep_body = True
            continue
        if keep_body:
            out.append(line)
    return "\n".join(out)
