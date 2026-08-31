"""ミックスインと土台の契約 — self. で読む状態が、どの土台でも解決できるか。

`PersonaMixin` / `BlueprintMixin` / `HistoryMixin` は **二つの土台**に載る:

- `SAIVerseManager` (世界そのもの。状態の実体を持つ)
- `AdminService` (CRUD の窓口。状態は manager / CoreState から受け取る)

ミックスインのコードは `self.X` で状態を読むので、**片方の土台にしか X が無いと、
その土台で実行されたときだけ AttributeError になる。** しかも生成系では commit の
後で落ちるため、「DB には作られたのに API は失敗を返す」形で壊れる。

2026-08-09 にこの形の欠陥が二度見つかった (`_on_persona_registered` は UI からの
ペルソナ作成を実際に壊していた)。一件ずつ潰すとレビューの往復が終わらないので、
**族ごと機械で検査する**のがこのテスト。新しく `self.X` を足した人は、両方の土台で
解決できるようにするか、ここが落ちて気づく。

## この検査が覆っていない範囲 (承知の上で外している)

- **`RuntimeService`**: `PersonaMixin` を継承するが、未解決の読み出しが 10 件ある。
  ただし RuntimeService がそれらを読むメソッドを実際に実行するかは未確認で、
  確かめずに委譲を 10 本足すのは意味のない模倣になる。**未検証の空白として残す。**
- **`SAIVerseManager`**: 状態の実体を持つ土台。属性が多数のミックスインの
  `__init__` や `_init_*` に散っており、静的走査では誤検出が出るため対象外。
- **動的参照**: `getattr(self, name)` や名前を組み立てる参照は捕まえられない。
"""
import ast
import pathlib
import unittest

from manager.admin import AdminService

#: AdminService が継承し、その状態を self. で読むミックスインの実装ファイル
MIXIN_MODULES = [
    "manager/persona.py",
    "manager/blueprints.py",
    "manager/history.py",
]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _init_assigned_names(module_path: str, class_name: str) -> set:
    """``ClassName.__init__`` が ``self.X = ...`` で用意する名前。"""
    tree = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                for sub in ast.walk(item):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.ctx, ast.Store)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"
                    ):
                        names.add(sub.attr)
    return names


def _self_reads(module_path: str) -> dict:
    """モジュール内の ``self.X`` 読み出しと、最初に現れた行番号。"""
    tree = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
    reads = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            reads.setdefault(node.attr, node.lineno)
    return reads


class MixinHostContractTestCase(unittest.TestCase):
    def test_admin_service_provides_every_state_its_mixins_read(self):
        provided = _init_assigned_names("manager/admin.py", "AdminService")
        provided |= set(dir(AdminService))  # メソッド・クラス属性

        missing = []
        for module_path in MIXIN_MODULES:
            for attr, lineno in sorted(_self_reads(module_path).items()):
                if attr.startswith("__") or attr in provided:
                    continue
                missing.append(f"  {module_path}:{lineno} — self.{attr}")

        self.assertEqual(
            missing,
            [],
            "AdminService に載るミックスインが self. で読むのに、AdminService が\n"
            "用意していない状態があります。この経路で実行されると AttributeError に\n"
            "なり、生成系では commit の後で落ちるため『DB には作られたのに失敗が\n"
            "返る』形で壊れます。AdminService.__init__ で manager / CoreState から\n"
            "委譲するか、そもそも self. で読まない形にしてください:\n"
            + "\n".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
