"""head セクションの配線整合テスト — silent 未描画事故の恒久防波堤。

Section を registry に登録しても、head に描画されるには
(1) `sea/head_pipeline/integration.py` の SYSTEM_PROMPT_SECTION_NAMES
    (または memory_weave / visual_context の特別扱い)
(2) `sea/runtime_context.py` の enabled_sections 固定集合
の**両方**に名前が載っている必要がある。片方でも漏れると「登録済みで
テストも通るのに本番では一度も描画されない」silent 故障になる —
DeskSection (P2a〜P3c①) と MemopediaIndexSection (P4-d) で二度起きた実績。

本テストは実物の定義を import して両点の整合を機械検査する。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.integration import (
    MEMORY_WEAVE_SECTION_NAME,
    SYSTEM_PROMPT_SECTION_NAMES,
    VISUAL_CONTEXT_SECTION_NAME,
)

_RUNTIME_CONTEXT = Path(__file__).resolve().parents[1] / "sea" / "runtime_context.py"


def _runtime_enabled_sections() -> set:
    """runtime_context.py の恒常セクション固定集合を実ソースから抽出する。

    prepare_context 内のリテラル集合なので、import しての実行では取れない。
    `enabled_sections.update({...})` の中身を正規表現で読む (集合の書き方が
    変わったらこのテストを追従させること)。
    """
    src = _RUNTIME_CONTEXT.read_text(encoding="utf-8")
    m = re.search(r"enabled_sections\.update\(\{(.*?)\}\)", src, re.DOTALL)
    assert m, "runtime_context.py の enabled_sections.update({...}) が見つからない"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


class HeadSectionWiringTests(unittest.TestCase):
    def test_system_prompt_sections_are_all_enabled(self):
        """SYSTEM_PROMPT_SECTION_NAMES の全セクションが enabled_sections にも載っている。

        (available_playbooks だけは条件付き有効化なので除外 — reqs 依存)
        """
        enabled = _runtime_enabled_sections()
        conditional = {"available_playbooks"}
        for name in SYSTEM_PROMPT_SECTION_NAMES:
            if name in conditional:
                continue
            self.assertIn(
                name, enabled,
                f"section '{name}' は SYSTEM_PROMPT_SECTION_NAMES に居るのに "
                "runtime_context の enabled_sections に無い — 本番で描画されない",
            )

    def test_rendering_sections_in_registry_are_composed(self):
        """registry 登録済みで render が実文を返しうるセクションが合成経路に居る。

        head に載る経路は SYSTEM_PROMPT_SECTION_NAMES / memory_weave /
        visual_context の3つだけ。どれにも属さないのに render を実装している
        セクションは silent 未描画 (DeskSection/MemopediaIndexSection 事故の型)。
        """
        from sea.head_pipeline import sections as sections_pkg

        composed = set(SYSTEM_PROMPT_SECTION_NAMES) | {
            MEMORY_WEAVE_SECTION_NAME, VISUAL_CONTEXT_SECTION_NAME,
        }
        # 描画経路を持たない(=diff 通知等の裏方専用が許される)セクションは
        # ここに明示する。新設時にここへ足す場合は「本当に head に出さないのか」
        # を設計で確認すること。
        notification_only = {
            "chronicle_index",   # 差分通知専用 (render なし運用の残置)
            "building_items",    # visual_context 経由で描画される素材
            "building_occupants",
        }
        import inspect

        for attr_name in dir(sections_pkg):
            cls = getattr(sections_pkg, attr_name)
            if not inspect.isclass(cls):
                continue
            name = getattr(cls, "name", None)
            if not isinstance(name, str):
                continue
            if name in composed or name in notification_only:
                continue
            self.fail(
                f"section '{name}' ({attr_name}) は registry に居るが、"
                "SYSTEM_PROMPT_SECTION_NAMES / memory_weave / visual_context / "
                "notification_only のどれにも属さない — 本番で一度も描画されない"
                "可能性が高い。配線するか notification_only に明示すること",
            )


if __name__ == "__main__":
    unittest.main()
