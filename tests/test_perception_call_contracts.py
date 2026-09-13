"""知覚ブロックの組成を呼ぶ側が守る契約を、呼び出し箇所ごと機械で検査する。

対象は「送る中身」を組む/測る四つの入口:

- ``sea.runtime_context.list_presented_perception_blocks`` (組成の一点)
- ``SessionLifecycle.perception_blocks_for``
- ``SessionLifecycle.presented_with_perceptions``
- ``SessionLifecycle.presented_chars``

契約は「一箇所直しても、隣で同じ欠陥が生き残る」型なので、呼び出し**全件**を
走査する検査にしてある (grep の目視ではなく AST)。

**``model_key`` を明示で渡す** (2026-09-05 Codex 三巡 #2)。この四つは「どの
Session の窓を組む・測るか」を扱うので、実行 model は呼び出し側が明示する。
渡し忘れた呼び出しだけが ``persona.model`` へ落ち、勘定と提示が別の Session の
model で静かに動く。引数を足したときに新しい呼び出しを一つ書き漏らす、が実際に
起きた形。

旧契約「API 層は ``advance_cutoff=False``」(2026-09-05 四巡目 #6) は 2026-09-09 に
不要になった — 知覚の合計上限を廃止して組成から書き込み経路そのものが消えたので、
「読み取り専用の画面が境界を進める」形は構造的に作れない (docs/intent/
presented_context_reduction.md 設計 3)。

``cold_precompaction`` 系 (``cold_precompaction_status`` /
``run_cold_precompaction``) は ``model_key`` の値を ``persona.model`` から取る —
生きた Session が無い背景の tick なので、それが正しい実行 model になる。渡し方
自体は明示なので、この検査の例外にはあたらない (許可リストは空のまま)。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Iterator, List, Tuple

#: 検査対象の呼び出し名 (属性呼び出し・関数呼び出しのどちらも末尾の名前で見る)。
_WATCHED = {
    "list_presented_perception_blocks",
    "perception_blocks_for",
    "presented_with_perceptions",
    "presented_chars",
}

#: 走査するソースの根 (配布物のコード。テストと作業用ディレクトリは対象外)。
_ROOTS = (
    "api", "sea", "saiverse", "saiverse_memory", "sai_memory", "persona",
    "manager", "tools", "database", "llm_clients", "scripts",
)

#: model_key を渡さないことが意図的だと裁定された呼び出し (file, line)。
#: 空のまま = 例外なし。埋めるときは理由をここに書くこと。
_MODEL_KEY_EXEMPT: set = set()

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _source_files() -> Iterator[Path]:
    for root in _ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            yield path


def _watched_calls() -> List[Tuple[Path, ast.Call, str]]:
    found: List[Tuple[Path, ast.Call, str]] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - 保険
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _WATCHED:
                found.append((path, node, name))
    return found


class PerceptionCallContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.calls = _watched_calls()

    def _where(self, path: Path, node: ast.Call) -> str:
        return f"{path.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"

    def test_the_scan_actually_finds_the_call_sites(self):
        """走査が空振りしていないことを先に確かめる (根やパターンの腐り検知)。"""
        self.assertGreaterEqual(len(self.calls), 8)
        names = {name for _p, _n, name in self.calls}
        self.assertEqual(names, _WATCHED)

    def test_every_call_passes_model_key_explicitly(self):
        missing = [
            f"{self._where(path, node)} ({name})"
            for path, node, name in self.calls
            if not any(kw.arg == "model_key" for kw in node.keywords)
            and self._where(path, node) not in _MODEL_KEY_EXEMPT
        ]
        self.assertEqual(missing, [], (
            "組成と勘定はその回の実行 model のものとして扱う。model_key を "
            "渡さない呼び出しは persona.model へ落ちて、別の Session の model で "
            "静かに動く:\n"
            + "\n".join(missing)
        ))

    def test_no_call_site_still_passes_the_retired_advance_cutoff(self):
        """廃止した ``advance_cutoff`` が残っていたら TypeError になる前に教える。"""
        stale = [
            f"{self._where(path, node)} ({name})"
            for path, node, name in self.calls
            if any(kw.arg == "advance_cutoff" for kw in node.keywords)
        ]
        self.assertEqual(stale, [], (
            "advance_cutoff は 2026-09-09 に廃止した (組成は読み取り専用に "
            "なった)。渡している呼び出し:\n" + "\n".join(stale)
        ))

    def test_the_exempt_list_has_no_stale_entries(self):
        live = {self._where(path, node) for path, node, _name in self.calls}
        self.assertEqual(_MODEL_KEY_EXEMPT - live, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
