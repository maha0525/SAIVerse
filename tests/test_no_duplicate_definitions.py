"""同じスコープに同名の関数・クラスを二度定義していないか — リポジトリ全体の検査。

Python は後の定義で前の定義を黙って上書きする。前の定義を意図して書かれた
呼び出しも、実際には後の版で解決される。2026-09-28、``saiverse/day_plan.py``
に ``_building_display_name`` が二つ並んでいた (``building_map`` を引く版と
``buildings`` を引く版) のが見つかり、テストのほうが「実効定義に合わせる」と
注記して偶然の挙動を固定していた。

ruff の F811 (redefined-while-unused) はこれを捕まえない。前の定義が別の関数の
本体から参照されていると「使われている」と数えるため、上書きを正当な再定義と
みなす。だからここで機械的に止める。

許す再定義は、同名を意図して重ねる書き方だけ:

- ``@name.setter`` / ``@name.getter`` / ``@name.deleter`` (property の組)
- ``@overload`` / ``@typing.overload`` (型のための宣言)

``if`` / ``try`` の中の定義 (環境ごとの切り替え) は直下の本体ではないので
対象外。
"""
import ast
import pathlib
import subprocess
from typing import List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _decorator_allows_redefinition(dec: ast.expr, name: str) -> bool:
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id == "overload"
    if isinstance(dec, ast.Attribute):
        if dec.attr == "overload":
            return True
        if (
            dec.attr in ("setter", "getter", "deleter")
            and isinstance(dec.value, ast.Name)
            and dec.value.id == name
        ):
            return True
    return False


def find_duplicate_definitions(source: str, label: str = "<src>") -> List[str]:
    """同じスコープ (モジュール直下 / クラス直下) の同名定義を列挙する。"""
    tree = ast.parse(source)
    found: List[str] = []

    def scan(body: list, scope: str) -> None:
        first_line: dict = {}
        for node in body:
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if isinstance(node, ast.ClassDef):
                scan(node.body, f"{scope}{node.name}.")
            decorators = getattr(node, "decorator_list", [])
            if any(_decorator_allows_redefinition(d, node.name) for d in decorators):
                # 意図して重ねる定義 (setter / overload) は数えない。
                # overload の後に続く実装本体が「一つ目」として登録される。
                continue
            if node.name in first_line:
                found.append(
                    f"{label}: {scope}{node.name} "
                    f"(lines {first_line[node.name]} and {node.lineno})"
                )
            else:
                first_line[node.name] = node.lineno

    scan(tree.body, "")
    return found


def _tracked_python_files() -> List[pathlib.Path]:
    # 未追跡でも ignore されていない新規ファイルは検査に含める (新しいファイル
    # ほど初見の重複を持ちやすい)。
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files",
         "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line.strip()]


def test_no_duplicate_definitions_in_repository():
    files = _tracked_python_files()
    assert files, "git ls-files returned no Python files"
    duplicates: List[str] = []
    for path in files:
        if not path.exists():  # 作業ツリーで削除済みで未コミット
            continue
        source = path.read_text(encoding="utf-8-sig")
        rel = path.relative_to(REPO_ROOT).as_posix()
        duplicates.extend(find_duplicate_definitions(source, rel))
    assert duplicates == [], (
        "同じスコープに同名の定義があり、後の定義が前の定義を黙って上書き"
        "しています。一本化するか名前を分けてください:\n"
        + "\n".join(duplicates)
    )


# --- 検査そのものの検算 (素通しの検査になっていないこと) ---


def test_detector_flags_module_level_redefinition():
    src = "def f():\n    return 1\n\ndef g():\n    return f()\n\ndef f():\n    return 2\n"
    assert find_duplicate_definitions(src) == ["<src>: f (lines 1 and 7)"]


def test_detector_flags_method_redefinition():
    src = "class C:\n    def m(self):\n        pass\n    def m(self):\n        pass\n"
    assert find_duplicate_definitions(src) == ["<src>: C.m (lines 2 and 4)"]


def test_detector_scopes_are_independent():
    """別クラスの同名メソッド・クラス内とモジュール直下の同名は重複ではない。"""
    src = (
        "def run():\n    pass\n"
        "class A:\n    def run(self):\n        pass\n"
        "class B:\n    def run(self):\n        pass\n"
    )
    assert find_duplicate_definitions(src) == []


def test_detector_allows_property_setter_and_overload():
    src = (
        "from typing import overload\n"
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return 1\n"
        "    @x.setter\n"
        "    def x(self, v):\n"
        "        pass\n"
        "@overload\n"
        "def f(a: int) -> int: ...\n"
        "@overload\n"
        "def f(a: str) -> str: ...\n"
        "def f(a):\n"
        "    return a\n"
    )
    assert find_duplicate_definitions(src) == []
