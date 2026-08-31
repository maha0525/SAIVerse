"""設置物 (Fixture) 欄の feed_stand 描画のテスト。

feed_manager.update_fixture_display が STATE_JSON の ``feed_stand`` キーへ書く
表示情報 (購読タイトル + 直近見出し) は観測値形式 (value_num/value_text) では
ないため、get_visual_context の設置物欄に専用の描画がある
(issue feed_arrival_pulse_cannot_see_articles ②)。

- 購読フィードと新着見出しが描画される
- 観測値形式の他キー (ObserverManager の metric) は従来どおり描画される
- feed_stand 内部の updated_at はプロンプトに載せない
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from builtin_data.tools import get_visual_context as gvc


class FeedStandFixtureRenderingTest(unittest.TestCase):
    def _build_env(self, state_json: str):
        fixture = SimpleNamespace(
            NAME="ニューススタンド",
            TYPE="feed_stand",
            FIXTURE_ID="fx-1",
            DESCRIPTION="報道各社のニュースが届く新聞スタンド。",
            STATE_JSON=state_json,
        )
        persona = SimpleNamespace(
            persona_name="テスター", buildings={}, current_building_id="b1",
        )
        manager = SimpleNamespace(
            occupants={"b1": []},
            all_personas={"p1": persona},
            observer_manager=SimpleNamespace(
                get_building_fixtures=lambda bid: [fixture],
            ),
            get_all_items_in_building=lambda bid: [],
        )
        return manager

    def _render(self, state_json: str) -> str:
        manager = self._build_env(state_json)
        with patch.object(gvc, "get_active_persona_id", return_value="p1"), \
                patch.object(gvc, "get_active_manager", return_value=manager):
            msgs = gvc.get_visual_context(
                building_id="b1",
                include_self=False,
                include_building=False,
                include_other_personas=False,
                for_perception=True,
            )
        self.assertTrue(msgs)
        return msgs[0]["content"]

    def test_feed_stand_titles_and_latest_rendered(self):
        content = self._render(json.dumps({
            "feed_stand": {
                "subscriptions": ["テストフィード", "第二フィード"],
                "latest": ["記事A", "記事B"],
                "updated_at": "2026-08-08T00:00:00+00:00",
            },
            "temperature": {"value_num": 21.5},
        }))
        self.assertIn("ニューススタンド", content)
        self.assertIn("購読フィード: テストフィード / 第二フィード", content)
        self.assertIn("新着記事の見出し:", content)
        self.assertIn("- 記事A", content)
        self.assertIn("- 記事B", content)
        # 観測値形式の他キーは従来どおり
        self.assertIn("最新観測値: temperature=21.5", content)
        # feed_stand 内部の帳簿情報はプロンプトに載せない
        self.assertNotIn("updated_at", content)
        self.assertNotIn("2026-08-08T00:00:00", content)

    def test_feed_stand_empty_display_renders_nothing_extra(self):
        content = self._render(json.dumps({
            "feed_stand": {"subscriptions": [], "latest": [],
                           "updated_at": "2026-08-08T00:00:00+00:00"},
        }))
        self.assertIn("ニューススタンド", content)
        self.assertNotIn("購読フィード", content)
        self.assertNotIn("新着記事の見出し", content)

    def test_metric_only_state_unchanged(self):
        content = self._render(json.dumps({
            "temperature": {"value_num": 20.0},
        }))
        self.assertIn("最新観測値: temperature=20.0", content)
        self.assertNotIn("購読フィード", content)


if __name__ == "__main__":
    unittest.main()
