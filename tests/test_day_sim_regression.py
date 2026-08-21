"""一日シミュレータの回帰スイート (mock・LLM コストゼロ)。

同梱シナリオを端から端まで実走し、「一日が正しく終わる」ための不変条件を
アサートする。自律行動まわり (day_plan / judgment_points / work_session /
day_scenario / day_report) に手を入れたら、このスイートが先に壊れて教えてくれる。

不変条件 (docs/intent/autonomous_behavior_v2.md の設計原則に対応):
- 全判断点が submitted (黙って落ちない)
- 全コマが終端状態に到達し、skipped には skip_reason が残る (正直な提示)
- 「作る」コマは実在の Item (成果物) を生む (接地原則)
- 日次予算の消費が総枠を超えない
- 一日新聞と生データが破綻なく生成できる
"""
import json
import unittest
from pathlib import Path

from saiverse import clock
from scripts.run_day_sim import generate_raw_log, run_mock_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "test_fixtures" / "scenarios"


def _load(name: str):
    from saiverse.day_scenario import load_scenario
    return load_scenario(SCENARIO_DIR / name)


def _load_plan(manager, persona_id: str, plan_date) -> dict:
    from database.models import PersonaDayPlan
    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter(PersonaDayPlan.persona_id == persona_id)
            .filter(PersonaDayPlan.plan_date == str(plan_date))
            .first()
        )
        assert row is not None, "persona_day_plan 行が無い (day_open が保存に失敗)"
        return {
            "slots": json.loads(row.slots_json),
            "meta": json.loads(row.meta_json) if row.meta_json else {},
        }
    finally:
        db.close()


class DayStandardRegressionTest(unittest.TestCase):
    """day_standard.json — 起床→作業→会話→イベント→就寝の全経路。"""

    @classmethod
    def setUpClass(cls):
        cls.scenario = _load("day_standard.json")
        cls.manager, cls.result, cls.markers = run_mock_scenario(cls.scenario)
        cls.plan = _load_plan(cls.manager, cls.scenario.persona_id, cls.scenario.plan_date)

    @classmethod
    def tearDownClass(cls):
        clock.disable_virtual()

    def test_all_judgments_submitted(self):
        failures = [j for j in self.result.judgments if not j.get("submitted")]
        self.assertEqual(failures, [], f"submitted でない判断点: {failures}")

    def test_judgment_sequence_brackets_the_day(self):
        kinds = [j["kind"] for j in self.result.judgments]
        self.assertEqual(kinds[0], "day_open")
        self.assertEqual(kinds[-1], "day_close")
        self.assertIn("post_session", kinds)
        self.assertIn("on_event", kinds)
        # 会話終了判断は 2026-08-16 に退役 (退室は帳簿処理だけ)
        self.assertNotIn("post_conversation", kinds)

    def test_all_slots_reach_terminal_status(self):
        non_terminal = [
            s for s in self.plan["slots"] if s.get("status") not in ("done", "skipped")
        ]
        self.assertEqual(non_terminal, [], f"終端に達していないコマ: {non_terminal}")

    def test_skipped_slots_carry_reason(self):
        for slot in self.plan["slots"]:
            if slot.get("status") == "skipped":
                self.assertTrue(
                    slot.get("skip_reason"),
                    f"skip_reason の無い skipped コマ: {slot}",
                )

    def test_create_slots_produce_real_items(self):
        """「随筆を書く」コマの成果物が実在する (接地原則 — やったフリ検知)。"""
        from database.models import Item
        create_slots = [
            s for s in self.plan["slots"]
            if s.get("kind") == "随筆を書く" and s.get("status") == "done"
        ]
        self.assertGreater(len(create_slots), 0, "シナリオに done の随筆を書くコマが無い")
        db = self.manager.SessionLocal()
        try:
            item_count = db.query(Item).count()
        finally:
            db.close()
        self.assertGreaterEqual(item_count, len(create_slots))

    def test_budget_within_limit(self):
        meta = self.plan["meta"]
        self.assertIn("budget_total_rounds", meta)
        self.assertLessEqual(meta["budget_used_rounds"], meta["budget_total_rounds"])

    def test_day_close_leaves_handoff_for_tomorrow(self):
        meta = self.plan["meta"]
        self.assertTrue(meta.get("tomorrow_memo"))
        # 旧 day_digest の保存コピーは撤去済み (2026-07-29) — 明日への引き継ぎは
        # tomorrow_memo だけが正
        self.assertNotIn("day_digest", meta)

    def test_session_digests_are_committed(self):
        """作業セッションの committed ダイジェストが残る (volatile だけで消えない)。"""
        persona = self.manager.personas[self.scenario.persona_id]
        digests = [
            p for p in persona.sai_memory.messages
            if "session_digest" in ((p.get("metadata") or {}).get("tags") or [])
        ]
        self.assertGreater(len(digests), 0)
        for d in digests:
            self.assertEqual(d.get("scope"), "committed")

    def test_day_report_generates(self):
        from saiverse.day_report import generate_day_report
        report = generate_day_report(
            self.manager, self.scenario.persona_id, self.scenario.plan_date)
        self.assertIn(str(self.scenario.plan_date), report)

    def test_raw_log_generates_with_all_sections(self):
        raw = generate_raw_log(
            self.manager, self.scenario, self.result, self.markers, mode="mock")
        for section in ("## 判断点", "## ペルソナ記憶", "## 建物メッセージ",
                        "## タスク / 欲求スナップショット", "## 時間割", "## アイテム"):
            self.assertIn(section, raw)
        # 生データには volatile の作業ログ (スペル往復) が入る
        self.assertIn("Spell Result", raw)


class DayAbsentRegressionTest(unittest.TestCase):
    """day_absent.json — 終日不在 + 空バックログ。何も起きない日が静かに終わる。"""

    @classmethod
    def setUpClass(cls):
        cls.scenario = _load("day_absent.json")
        cls.manager, cls.result, cls.markers = run_mock_scenario(cls.scenario)
        cls.plan = _load_plan(cls.manager, cls.scenario.persona_id, cls.scenario.plan_date)

    @classmethod
    def tearDownClass(cls):
        clock.disable_virtual()

    def test_day_opens_and_closes(self):
        kinds = [j["kind"] for j in self.result.judgments]
        self.assertEqual(kinds[0], "day_open")
        self.assertEqual(kinds[-1], "day_close")
        failures = [j for j in self.result.judgments if not j.get("submitted")]
        self.assertEqual(failures, [])

    def test_all_slots_terminal(self):
        non_terminal = [
            s for s in self.plan["slots"] if s.get("status") not in ("done", "skipped")
        ]
        self.assertEqual(non_terminal, [])

    def test_raw_log_generates(self):
        raw = generate_raw_log(
            self.manager, self.scenario, self.result, self.markers, mode="mock")
        self.assertIn("## 判断点", raw)


if __name__ == "__main__":
    unittest.main()
