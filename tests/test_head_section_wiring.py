"""head セクションの配線整合テスト — silent 未描画事故の恒久防波堤。

Section を registry に登録しても、head に描画されるには
(1) `sea/head_pipeline/integration.py` の SYSTEM_PROMPT_SECTION_NAMES
    (または memory_weave の特別扱い)
(2) `sea/runtime_context.py` の enabled_sections 固定集合
の**両方**に名前が載っている必要がある。片方でも漏れると「登録済みで
テストも通るのに本番では一度も描画されない」silent 故障になる —
DeskSection (P2a〜P3c①) と MemopediaIndexSection (P4-d) で二度起きた実績。

visual_context (部屋の描画) は 2026-09-06 に head から退役した — 部屋の様子の
置き場は知覚 (tail) 一つ (docs/intent/room_state_packages.md)。

本テストは実物の定義を import して両点の整合を機械検査する。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.integration import (
    MEMORY_WEAVE_SECTION_NAME,
    SYSTEM_PROMPT_SECTION_NAMES,
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

    def test_memory_weave_is_fixed_in_head(self):
        """memory_weave も固定集合に含まれる。

        以前は呼び出し側のフラグ次第で、work_session だけ欠けた head で走って
        いた (= 同じ (persona, model) で head が二種類あり prefix キャッシュが
        壊れる)。
        """
        self.assertIn(MEMORY_WEAVE_SECTION_NAME, PERSONA_HEAD_SECTIONS)

    def test_visual_context_stays_retired(self):
        """visual_context (部屋の描画) が head に復活していない。

        部屋の様子の置き場は知覚 (tail) 一つ (room_state_packages.md §2)。
        head に部屋の二枚目を作ると、二重・照合・開き直しの例外機構
        (旧 §10.8.1) が芋づるで戻ってくる。
        """
        self.assertNotIn("visual_context", PERSONA_HEAD_SECTIONS)
        self.assertNotIn("visual_context", SYSTEM_PROMPT_SECTION_NAMES)

    def test_rendering_sections_in_registry_are_composed(self):
        """registry 登録済みで render が実文を返しうるセクションが合成経路に居る。

        head に載る経路は SYSTEM_PROMPT_SECTION_NAMES / memory_weave の 2 つ
        だけ。どれにも属さないのに render を実装しているセクションは silent
        未描画 (DeskSection/MemopediaIndexSection 事故の型)。
        """
        from sea.head_pipeline import sections as sections_pkg

        composed = set(SYSTEM_PROMPT_SECTION_NAMES) | {MEMORY_WEAVE_SECTION_NAME}
        # 描画経路を持たない(=diff 通知等の裏方専用が許される)セクションは
        # ここに明示する。新設時にここへ足す場合は「本当に head に出さないのか」
        # を設計で確認すること。
        notification_only = {
            "chronicle_index",   # 差分通知専用 (render なし運用の残置)
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
                "SYSTEM_PROMPT_SECTION_NAMES / memory_weave / "
                "notification_only のどれにも属さない — 本番で一度も描画されない"
                "可能性が高い。配線するか notification_only に明示すること",
            )


if __name__ == "__main__":
    unittest.main()
