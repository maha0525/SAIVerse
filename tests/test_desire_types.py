"""欲求の六型拡張 (自律行動 v2 §5) のテスト。

- purpose_seed (旧 desire_add 後継、P2c-4a で撤去) の type/source 付き追加と
  後方互換 (type/source なしでも従来どおり)
- 減衰の時間発展 (仮想クロックで日を進めて fresh → fading → 期限切れ)
- 再訪 (touch) → 昇格候補 (promotion_candidates)
- 就寝判断 desire_reviews (keep/fading/fulfilled) の反映
- desire_summary_for_prompt の形式
- 時間割コマ発火 (day_plan._fire_slot) → touch_desire の配線

teardown で engine.dispose() + clock.disable_virtual() を必ず行う。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock, day_plan, desire_engine
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import (
    DESIRE_STATE_EXPIRED,
    DESIRE_STATE_FADING,
    DESIRE_STATE_FRESH,
    PARENT_NOTE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
)
from saiverse.track_manager import TrackManager

PERSONA_ID = "air"
T0 = datetime(2026, 7, 1, 9, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Air"))
        db.commit()
    finally:
        db.close()
    yield Session
    engine.dispose()


@pytest.fixture(autouse=True)
def _virtual_clock():
    clock.enable_virtual(T0)
    yield
    clock.disable_virtual()


@pytest.fixture
def manager(session_factory):
    return SimpleNamespace(SessionLocal=session_factory)


@pytest.fixture
def ptm(session_factory):
    return PersonaTaskManager(session_factory)


@pytest.fixture
def nm(session_factory):
    return NoteManager(session_factory)


@pytest.fixture
def desire_add_mod(session_factory, nm, ptm, tmp_path):
    """purpose_seed ツール (旧 desire_add 後継) を temp DB 版 singleton で動的ロードする。"""
    from tool_loader import load_builtin_tool

    mod = load_builtin_tool("purpose_seed")
    mod._note_manager = nm
    mod._task_manager = ptm
    persona_dir = Path(tmp_path) / PERSONA_ID
    persona_dir.mkdir(parents=True, exist_ok=True)
    return mod, persona_dir


def _add_desire(nm, ptm, title="言葉の標本集を作りたい", **kwargs):
    note_id = nm.ensure_desire_note(PERSONA_ID)
    return ptm.create_task(
        persona_id=PERSONA_ID, title=title,
        parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        **kwargs,
    )


def _advance_days(days: int):
    clock.advance_to(T0 + timedelta(days=days))


# ---------------------------------------------------------------------------
# purpose_seed (旧 desire_add 後継): type/source 付き追加と後方互換
# ---------------------------------------------------------------------------


class TestDesireAddSpell:
    def test_add_with_type_and_source(self, desire_add_mod, nm, ptm):
        from tools.context import persona_context

        mod, persona_dir = desire_add_mod
        with persona_context(PERSONA_ID, persona_dir):
            out = mod.purpose_seed(
                title="言葉の標本集を作りたい",
                type="作る",
                source="図書館で読んだ語源の記事",
            )
        # 表記は判断点の enum と同じ task:N に統一 (desire: 二重 prefix は廃止)。
        assert "task:1" in out
        assert "desire:1" not in out
        assert "作る" in out
        note_id = nm.ensure_desire_note(PERSONA_ID)
        tasks = ptm.list_tasks(PERSONA_ID, note_id=note_id)
        assert len(tasks) == 1
        t = tasks[0]
        assert t["desire_type"] == "作る"
        assert t["desire_source"] == "図書館で読んだ語源の記事"
        # 帳簿の初期化: 鮮度の起点 = 作成時刻 (仮想クロック)、再訪 0、fresh。
        assert t["desire_state"] == DESIRE_STATE_FRESH
        assert t["touch_count"] == 0
        assert datetime.fromisoformat(t["last_touched_at"]) == T0

    def test_add_without_type_is_backward_compatible(self, desire_add_mod, nm, ptm):
        """既存呼び出し (track_autonomous 等) の type/source なし呼びは従来どおり動く。"""
        from tools.context import persona_context

        mod, persona_dir = desire_add_mod
        with persona_context(PERSONA_ID, persona_dir):
            out = mod.purpose_seed(title="新しい言語を学びたい", goal="表現の幅を広げる")
        assert "task:1" in out
        assert "未分類" in out
        assert not out.startswith("Error")
        note_id = nm.ensure_desire_note(PERSONA_ID)
        tasks = ptm.list_tasks(PERSONA_ID, note_id=note_id)
        assert len(tasks) == 1
        assert tasks[0]["desire_type"] is None
        assert tasks[0]["desire_source"] is None
        # 未分類でも帳簿は付く (減衰・再訪の対象になる)。
        assert tasks[0]["touch_count"] == 0
        assert tasks[0]["last_touched_at"] is not None

    def test_add_with_invalid_type_returns_error(self, desire_add_mod, nm, ptm):
        from tools.context import persona_context

        mod, persona_dir = desire_add_mod
        with persona_context(PERSONA_ID, persona_dir):
            out = mod.purpose_seed(title="なにか", type="遊ぶ")
        assert out.startswith("Error")
        note_id = nm.ensure_desire_note(PERSONA_ID)
        assert ptm.list_tasks(PERSONA_ID, note_id=note_id) == []

    def test_schema_declares_six_types(self, desire_add_mod):
        mod, _ = desire_add_mod
        schema = mod.schema()
        props = schema.parameters["properties"]
        assert tuple(props["type"]["enum"]) == desire_engine.DESIRE_TYPES
        assert "source" in props
        # 後方互換: 必須は title のみ。
        assert schema.parameters["required"] == ["title"]


# ---------------------------------------------------------------------------
# 減衰の時間発展 (fresh → fading → 期限切れ)
# ---------------------------------------------------------------------------


class TestDecay:
    def test_no_decay_within_week(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        _advance_days(6)
        result = desire_engine.decay_desires(manager, PERSONA_ID)
        assert result == {"checked": 1, "faded": [], "expired": []}
        assert ptm.get_task(task["id"])["desire_state"] == DESIRE_STATE_FRESH

    def test_fading_after_seven_days(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        _advance_days(desire_engine.FADING_AFTER_DAYS)
        result = desire_engine.decay_desires(manager, PERSONA_ID)
        assert result["faded"] == [task["task_ref"]]
        assert result["expired"] == []
        updated = ptm.get_task(task["id"])
        assert updated["desire_state"] == DESIRE_STATE_FADING
        # fading はまだ候補プールに残る (summary にも出る)。
        assert "薄れつつある" in desire_engine.desire_summary_for_prompt(manager, PERSONA_ID)

    def test_expiry_after_fourteen_days_archives_not_deletes(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        # 時間発展: 8 日で fading → 15 日で期限切れ。
        _advance_days(8)
        assert desire_engine.decay_desires(manager, PERSONA_ID)["faded"] == [task["task_ref"]]
        _advance_days(15)
        result = desire_engine.decay_desires(manager, PERSONA_ID)
        assert result["expired"] == [task["task_ref"]]
        updated = ptm.get_task(task["id"])
        # 論理アーカイブ: 行は残り (物理削除しない)、status で候補プールから消える。
        assert updated["status"] == STATUS_CANCELLED
        assert updated["desire_state"] == DESIRE_STATE_EXPIRED
        assert desire_engine.desire_summary_for_prompt(manager, PERSONA_ID) == (
            "やりたいこと候補はありません。"
        )
        # 期限切れは履歴つき (接地の証跡と経緯が追える)。
        events = [h["event_type"] for h in ptm.fetch_history(task["id"])]
        assert "update_task_status" in events

    def test_decay_is_idempotent_per_day(self, manager, nm, ptm):
        _add_desire(nm, ptm)
        _advance_days(8)
        first = desire_engine.decay_desires(manager, PERSONA_ID)
        assert len(first["faded"]) == 1
        # 同日再実行しても「新たに fading になったもの」は無い。
        second = desire_engine.decay_desires(manager, PERSONA_ID)
        assert second == {"checked": 1, "faded": [], "expired": []}


# ---------------------------------------------------------------------------
# 再訪 (touch) → 鮮度回復・昇格候補
# ---------------------------------------------------------------------------


class TestTouchAndPromotion:
    def test_touch_updates_ledger_and_restores_freshness(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        _advance_days(8)
        desire_engine.decay_desires(manager, PERSONA_ID)
        assert ptm.get_task(task["id"])["desire_state"] == DESIRE_STATE_FADING

        touched = desire_engine.touch_desire(manager, PERSONA_ID, task["task_ref"])
        assert touched["touch_count"] == 1
        assert touched["desire_state"] == DESIRE_STATE_FRESH
        assert datetime.fromisoformat(touched["last_touched_at"]) == T0 + timedelta(days=8)
        # 鮮度が戻ったので同日の decay では変化しない。
        result = desire_engine.decay_desires(manager, PERSONA_ID)
        assert result == {"checked": 1, "faded": [], "expired": []}

    def test_touch_accepts_desire_ref_alias(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        ref = task["task_ref"].replace("task:", "desire:")
        touched = desire_engine.touch_desire(manager, PERSONA_ID, ref)
        assert touched["touch_count"] == 1

    def test_touch_non_candidate_is_noop(self, manager, ptm):
        task = ptm.create_task(
            persona_id=PERSONA_ID, title="Track の小目標",
            parent_kind="track", track_id="track-1", auto_activate=False,
        )
        assert desire_engine.touch_desire(manager, PERSONA_ID, task["task_ref"]) is None
        assert ptm.get_task(task["id"])["touch_count"] is None

    def test_promotion_after_threshold_touches(self, manager, nm, ptm):
        task = _add_desire(nm, ptm, desire_type="作る")
        other = _add_desire(nm, ptm, title="散歩したい")
        for _ in range(desire_engine.PROMOTION_TOUCH_THRESHOLD - 1):
            desire_engine.touch_desire(manager, PERSONA_ID, task["task_ref"])
        # 閾値未満はまだ昇格候補にならない。
        assert desire_engine.promotion_candidates(manager, PERSONA_ID) == []
        desire_engine.touch_desire(manager, PERSONA_ID, task["task_ref"])
        candidates = desire_engine.promotion_candidates(manager, PERSONA_ID)
        assert [c["task_ref"] for c in candidates] == [task["task_ref"]]
        assert candidates[0]["desire_type"] == "作る"
        assert candidates[0]["touch_count"] == desire_engine.PROMOTION_TOUCH_THRESHOLD
        assert other["task_ref"] not in [c["task_ref"] for c in candidates]


# ---------------------------------------------------------------------------
# 就寝判断 desire_reviews の反映
# ---------------------------------------------------------------------------


class TestDesireReviews:
    def test_fulfilled_completes_task(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        result = desire_engine.apply_desire_reviews(
            manager, PERSONA_ID,
            [{"desire_ref": task["task_ref"], "verdict": "fulfilled"}],
        )
        assert result["fulfilled"] == [task["task_ref"]]
        assert ptm.get_task(task["id"])["status"] == STATUS_COMPLETED

    def test_fading_verdict_accelerates_decay(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        result = desire_engine.apply_desire_reviews(
            manager, PERSONA_ID,
            [{"desire_ref": task["task_ref"], "verdict": "fading"}],
        )
        assert result["fading"] == [task["task_ref"]]
        assert ptm.get_task(task["id"])["desire_state"] == DESIRE_STATE_FADING
        # 減衰加速: 残り (EXPIRE - FADING) 日で期限切れになる。
        _advance_days(desire_engine.EXPIRE_AFTER_DAYS - desire_engine.FADING_AFTER_DAYS)
        decay = desire_engine.decay_desires(manager, PERSONA_ID)
        assert decay["expired"] == [task["task_ref"]]

    def test_keep_leaves_desire_untouched(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        before = ptm.get_task(task["id"])
        result = desire_engine.apply_desire_reviews(
            manager, PERSONA_ID,
            [{"desire_ref": task["task_ref"], "verdict": "keep"}],
        )
        assert result["kept"] == [task["task_ref"]]
        after = ptm.get_task(task["id"])
        assert after["desire_state"] == before["desire_state"]
        assert after["last_touched_at"] == before["last_touched_at"]
        assert after["touch_count"] == before["touch_count"]

    def test_unresolvable_ref_and_unknown_verdict_are_skipped(self, manager, nm, ptm):
        task = _add_desire(nm, ptm)
        result = desire_engine.apply_desire_reviews(
            manager, PERSONA_ID,
            [
                {"desire_ref": "task:999", "verdict": "keep"},
                {"desire_ref": task["task_ref"], "verdict": "nonsense"},
            ],
        )
        assert result["skipped"] == ["task:999", task["task_ref"]]


# ---------------------------------------------------------------------------
# 状況テキスト用一覧 (desire_summary_for_prompt)
# ---------------------------------------------------------------------------


class TestSummary:
    def test_empty_pool_message(self, manager):
        assert desire_engine.desire_summary_for_prompt(manager, PERSONA_ID) == (
            "やりたいこと候補はありません。"
        )

    def test_summary_lists_ref_type_title_freshness_touches(self, manager, nm, ptm):
        typed = _add_desire(
            nm, ptm, title="言葉の標本集を作りたい",
            desire_type="作る", desire_source="図書館の記事",
        )
        _add_desire(nm, ptm, title="散歩したい")
        desire_engine.touch_desire(manager, PERSONA_ID, typed["task_ref"])

        text = desire_engine.desire_summary_for_prompt(manager, PERSONA_ID)
        assert text.startswith("やりたいこと候補:")
        # ref の表記は enum (collect_slot_ref_enum) と同じ task:N に統一 (desire: は廃止)。
        ref = typed["task_ref"]
        assert ref.startswith("task:")
        assert f"- {ref} [作る] 言葉の標本集を作りたい (鮮度: 新鮮 / 再訪: 1回)" in text
        assert "[未分類] 散歩したい (鮮度: 新鮮 / 再訪: 0回)" in text
        assert "desire:" not in text


# ---------------------------------------------------------------------------
# 時間割コマ発火 → touch の配線 (day_plan._fire_slot)
# ---------------------------------------------------------------------------


class TestDayPlanTouchWiring:
    def test_fire_slot_with_desire_ref_touches_desire(self, session_factory, nm, ptm):
        manager = SimpleNamespace(
            SessionLocal=session_factory,
            personas={},
            track_manager=TrackManager(session_factory=session_factory),
        )
        task = _add_desire(nm, ptm, desire_type="作る")
        plan_date = clock.now().date().isoformat()
        day_plan.save_day_plan(manager, PERSONA_ID, plan_date, [{
            "start": "09:00", "kind": "作る",
            # 欲求も task:N に統一。_fire_slot は task: ref で touch_desire を呼ぶ。
            "ref": task["task_ref"],
            "facility": "own_room", "budget_rounds": 2, "note": "標本集",
        }])
        session_result = SimpleNamespace(
            ended_reason="completed", rounds_used=1, artifacts=[],
        )
        with patch("sea.work_session.run_work_session", return_value=session_result):
            day_plan._fire_slot(manager, PERSONA_ID, plan_date, 0)

        updated = ptm.get_task(task["id"])
        assert updated["touch_count"] == 1
        assert updated["desire_state"] == DESIRE_STATE_FRESH


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
