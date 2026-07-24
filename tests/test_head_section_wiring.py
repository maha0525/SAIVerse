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
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.integration import (
    MEMORY_WEAVE_SECTION_NAME,
    SYSTEM_PROMPT_SECTION_NAMES,
    VISUAL_CONTEXT_SECTION_NAME,
)
from sea.runtime_context import PERSONA_HEAD_SECTIONS


class HeadSectionWiringTests(unittest.TestCase):
    def test_system_prompt_sections_are_all_enabled(self):
        """SYSTEM_PROMPT_SECTION_NAMES の全セクションが head の固定集合にも載っている。

        2026-07-23 以前は呼び出し側のフラグで章を出し入れできたため、
        ここは「条件付き有効化 (available_playbooks) は除外」という例外を持って
        いた。フラグを撤去して PERSONA_HEAD_SECTIONS に固定したので例外は無い —
        全セクションが常に載る。
        """
        for name in SYSTEM_PROMPT_SECTION_NAMES:
            self.assertIn(
                name, PERSONA_HEAD_SECTIONS,
                f"section '{name}' は SYSTEM_PROMPT_SECTION_NAMES に居るのに "
                "PERSONA_HEAD_SECTIONS に無い — 本番で描画されない",
            )

    def test_memory_weave_and_visual_context_are_fixed_in_head(self):
        """memory_weave / visual_context も固定集合に含まれる。

        以前はこの 2 つが呼び出し側のフラグ次第で、work_session だけ欠けた head で
        走っていた (= 同じ (persona, model) で head が二種類あり prefix キャッシュが
        壊れる)。visual_context には BuildingItemsSection の素材も乗るため、欠けると
        作業セッションから部屋のアイテムが見えなくなる副作用もあった。
        """
        self.assertIn(MEMORY_WEAVE_SECTION_NAME, PERSONA_HEAD_SECTIONS)
        self.assertIn(VISUAL_CONTEXT_SECTION_NAME, PERSONA_HEAD_SECTIONS)

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
