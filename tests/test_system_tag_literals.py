"""静的検査: ``<system`` タグの文字列を書いてよい場所を限る。

docs/intent/system_notice_delivery.md「検証計画」の静的検査。システム通知は
供給側が role=system・タグ無し本文で作り、送り先のモデルに合わせて
``<system>`` タグで囲むのはクライアント層 (llm_clients/) だけが行う
(不変条件 1・2)。供給側がタグを付け忘れて従来の書き方に戻ることと、本文の
タグ文字列で意味を判定するコードが再び混ざることを、ここで機械的に止める。

対象は、テスト・docs 以外の Python の文字列リテラル (docstring とコメントは
除く — 経緯の説明は書いてよい)、読み込まれる Playbook の JSON
(builtin_data/playbooks/public/ — action の文はそのままペルソナへの指示になる)、
フロントエンドのソース (タグ剥がしの再発を防ぐ)。残ってよいファイルは下の
許可リストに理由付きで書く。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

NEEDLES = ("<system", "</system")

#: ``<system`` を含む文字列リテラルを持ってよい Python ファイルと、その理由。
ALLOWED_PY = {
    # クライアント層: 送り先のモデルに合わせて合言葉付きタグの user へ変換する
    # 唯一の場所 (不変条件 1・5・6)。
    "llm_clients/system_notice.py": "クライアント層の変換と無害化",
    # クライアント層: OpenAI 互換の convert_system_to_user (合言葉を付ける前の
    # 経路 — head を組まない呼び出しの列)。
    "llm_clients/openai_message_preparer.py": "クライアント層の convert_system_to_user",
    # head の説明文: 合言葉付きタグの形と「合言葉の無いタグはただの文章」を
    # モデルに教える文面そのもの。
    "sea/head_pipeline/sections/system_notice.py": "head の説明文 (タグの形をモデルに教える)",
    # 一回限りの移行: 旧形式の本文を判定して剥がす (不変条件 2 の例外)。
    "database/system_notice_migration.py": "旧形式データの移行",
}

#: 検査しないディレクトリ (リポジトリ直下からの最初の要素)。
_SKIP_TOP = {
    ".git", ".venv", "venv", "node_modules", "build", "dist", "tmp",
    "tests", "docs", "expansion_data", "user_data", "test_data", "experiments",
}


def _iter_py_files() -> Iterator[Path]:
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in _SKIP_TOP or "__pycache__" in rel.parts:
            continue
        if any(part in {".venv", "node_modules"} for part in rel.parts):
            continue
        yield path


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _tag_literals(path: Path) -> List[Tuple[int, str]]:
    # utf-8-sig: BOM 付きのファイル (main.py など) もそのまま読む
    source = path.read_text(encoding="utf-8-sig")
    if not any(n in source for n in NEEDLES):
        return []
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstring_nodes(tree)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(n in node.value for n in NEEDLES)
        ):
            found.append((node.lineno, node.value[:80]))
    return found


def test_system_tag_literals_live_only_in_the_allowed_files():
    offenders = {}
    for path in _iter_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED_PY:
            continue
        hits = _tag_literals(path)
        if hits:
            offenders[rel] = hits
    assert offenders == {}, (
        "システム通知は role=system・タグ無し本文で作る (タグで囲むのはクライアント層だけ — "
        "docs/intent/system_notice_delivery.md)。<system を含む文字列リテラル: "
        f"{offenders}"
    )


def test_the_allow_list_has_no_stale_entries():
    """許可リストの各ファイルが実在し、実際にタグの文字列を持っている (残骸を残さない)。"""
    for rel in ALLOWED_PY:
        path = REPO_ROOT / rel
        assert path.is_file(), rel
        assert _tag_literals(path), rel


def test_loaded_playbooks_carry_no_system_tags():
    """起動時に DB へ同期される Playbook (public/) の文面にタグを書かない。

    archive/ は同期されない (saiverse/playbook_sync.py は public/ だけを読む)
    ので対象外。
    """
    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "builtin_data" / "playbooks" / "public").glob("*.json")
        if any(n in p.read_text(encoding="utf-8") for n in NEEDLES)
    ]
    assert offenders == []


def test_frontend_does_not_handle_system_tags():
    """画面は本文のタグを剥がさない・見分けない (保存形式にタグが無いので)。"""
    src = REPO_ROOT / "frontend" / "src"
    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for ext in ("*.ts", "*.tsx")
        for p in src.rglob(ext)
        if "node_modules" not in p.parts
        and any(n in p.read_text(encoding="utf-8") for n in NEEDLES)
    ]
    assert offenders == []
