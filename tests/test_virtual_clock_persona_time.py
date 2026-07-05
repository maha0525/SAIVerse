"""ペルソナに見える時刻が仮想クロックを尊重することのテスト (異常 #3 回帰)。

2026-07-05 の実 LLM 一日シム再走で、判断プロンプトの「現在時刻」は仮想時刻
なのに head のリアルタイム情報が ``datetime.now()`` の実時刻を注入しており、
ペルソナの世界像が実時計に引っ張られた (起床 09:00 なのに時間割が 15:00
始まり)。対象は「ペルソナの文脈に乗る時刻」のみ:

- sea/runtime.py ``SEARuntime._build_realtime_context`` (head の現在時刻)
- saiverse/meta_layer.py ``MetaLayer._format_relative`` (状況テキストの相対時刻)
- saiverse/schedule_manager.py ``_generate_schedule_prompt`` (現在の日時)

仮想クロック無効時は従来挙動 (実時刻) と同値 = 本番挙動不変。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from saiverse import clock

VIRTUAL_NOW = datetime(2026, 7, 5, 9, 0, 0)  # 日曜日


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.disable_virtual()


# ---------------------------------------------------------------------------
# head のリアルタイム情報 (SEARuntime._build_realtime_context)
# ---------------------------------------------------------------------------


def _build_realtime_context(persona):
    from sea.runtime import SEARuntime

    fake_self = SimpleNamespace(
        manager=SimpleNamespace(unity_gateway=None),
        _is_realtime_info_enabled_for_persona=lambda p: True,
    )
    return SEARuntime._build_realtime_context(fake_self, persona, "b1", [])


def _persona():
    return SimpleNamespace(
        persona_id="p1",
        persona_name="Persona",
        timezone=timezone(timedelta(hours=9)),
    )


def test_realtime_context_uses_virtual_clock_when_enabled():
    clock.enable_virtual(VIRTUAL_NOW)
    msg = _build_realtime_context(_persona())
    assert msg is not None
    assert "現在時刻: 2026年07月05日(日) 09:00" in msg["content"]


def test_realtime_context_uses_real_time_when_virtual_disabled():
    persona = _persona()
    msg = _build_realtime_context(persona)
    assert msg is not None
    expected = datetime.now(persona.timezone)
    # 分単位の比較 (テスト実行の跨ぎ余裕として ±1 分を許す)
    shown = msg["content"].split("現在時刻: ")[1].split("\n")[0]
    candidates = [
        expected + timedelta(minutes=delta) for delta in (-1, 0, 1)
    ]
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    formatted = [
        c.strftime(f"%Y年%m月%d日({weekday_names[c.weekday()]}) %H:%M")
        for c in candidates
    ]
    assert shown in formatted


# ---------------------------------------------------------------------------
# メタ判断の状況テキストの相対時刻 (MetaLayer._format_relative)
# ---------------------------------------------------------------------------


def test_meta_layer_relative_time_uses_virtual_clock():
    from saiverse.meta_layer import MetaLayer

    clock.enable_virtual(VIRTUAL_NOW)
    # 仮想時刻の 5 分前 → 実時刻がいつでも「5分前」になる
    dt = VIRTUAL_NOW - timedelta(minutes=5)
    assert MetaLayer._format_relative(None, dt) == "5分前"


# ---------------------------------------------------------------------------
# スケジュール実行プロンプトの「現在の日時」 (_generate_schedule_prompt)
# ---------------------------------------------------------------------------


def test_schedule_prompt_uses_virtual_clock():
    from saiverse.schedule_manager import ScheduleManager

    clock.enable_virtual(VIRTUAL_NOW)
    fake_self = SimpleNamespace(
        _get_persona_timezone=lambda persona_id, session: timezone(
            timedelta(hours=9)
        ),
    )
    schedule = SimpleNamespace(
        SCHEDULE_ID=1,
        SCHEDULE_TYPE="interval",
        INTERVAL_SECONDS=600,
        DAYS_OF_WEEK=None,
        TIME_OF_DAY=None,
        SCHEDULED_DATETIME=None,
        DESCRIPTION="テスト",
    )
    prompt = ScheduleManager._generate_schedule_prompt(
        fake_self, schedule, None, "p1"
    )
    assert "現在の日時: 2026年07月05日 09:00" in prompt
