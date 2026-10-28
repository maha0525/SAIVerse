"""システム通知の供給側の保存形式 (docs/intent/system_notice_delivery.md)。

固定する不変条件 (不変条件 1): 供給側はシステム通知を role=system・タグ無し本文
で保存し、metadata に識別の印 (``SYSTEM_NOTICE_KEY``) を書く。読み出しのときに
``<system>`` で包む処理 (saiverse_memory/adapter.py) はこの束では変えない。

ここで見るのは代表の系統だけ — スケジュール・外部イベントの Pulse 入力
(sea/runtime.py の run_meta_user と、その組み立て側)、Playbook の MEMORIZE。
建物の出来事は tests/test_building_ingest_m8.py、子ラインの結果報告は
tests/test_subplay_line.py、HistoryManager の保存は tests/test_history_manager.py
が見ている。
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# スケジュール・外部イベントの Pulse 入力
# ---------------------------------------------------------------------------


def _run_schedule_pulse(user_input: str, metadata=None):
    """run_meta_user を最小の替え玉で schedule Pulse として 1 回走らせる。"""
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    history_manager = SimpleNamespace(
        add_message=Mock(), add_to_persona_only=Mock(return_value=("synced", "m1")),
    )
    persona = SimpleNamespace(
        persona_name="p", persona_id="p1", model="m", llm_client=object(),
        history_manager=history_manager, execution_state={},
        sai_memory=SimpleNamespace(flush_perception_buffer=lambda **kwargs: None),
        current_building_id="b1",
    )
    playbook = SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    )
    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._load_playbook_for = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=["ok"])
    runtime.session_lifecycle.maybe_run_metabolism = Mock()
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="skip")

    with patch(
        "saiverse.dynamic_state.DynamicStateManager.maybe_inject_event_messages",
        new=lambda *a, **kw: False,
    ), patch(
        "builtin_data.tools.get_building_messages.auto_ingest_building_messages",
        new=lambda *a, **kw: 0,
    ), patch(
        "sea.head_pipeline.inject_copresence_recall",
        new=lambda *a, **kw: None,
    ):
        runtime.run_meta_user(
            persona=persona, user_input=user_input, building_id="b1",
            metadata=metadata, meta_playbook="meta_user/exec",
            pulse_type="schedule",
        )
    return history_manager


def test_schedule_pulse_input_is_recorded_as_role_system_notice():
    hm = _run_schedule_pulse(
        "スケジュールの実行時刻です。",
        metadata={"source": "schedule", "schedule_id": 7},
    )
    hm.add_to_persona_only.assert_called_once()
    recorded = hm.add_to_persona_only.call_args.args[0]
    assert recorded["role"] == "system"
    assert recorded["content"] == "スケジュールの実行時刻です。"
    # 既存の印 (with) と呼び出し元の metadata は残したまま、識別の印を足す
    assert recorded["metadata"] == {
        "with": ["system"],
        "source": "schedule",
        "schedule_id": 7,
        SYSTEM_NOTICE_KEY: "schedule",
    }


def test_schedule_pulse_caller_metadata_cannot_override_the_notice_key():
    hm = _run_schedule_pulse(
        "スケジュールの実行時刻です。", metadata={SYSTEM_NOTICE_KEY: "other"},
    )
    recorded = hm.add_to_persona_only.call_args.args[0]
    assert recorded["metadata"][SYSTEM_NOTICE_KEY] == "schedule"


def test_schedule_prompt_has_no_system_tags():
    from saiverse.schedule_manager import ScheduleManager

    fake_self = SimpleNamespace(
        _get_persona_timezone=lambda persona_id, session: timezone(timedelta(hours=9)),
    )
    schedule = SimpleNamespace(
        SCHEDULE_ID=1, SCHEDULE_TYPE="interval", INTERVAL_SECONDS=600,
        DAYS_OF_WEEK=None, TIME_OF_DAY=None, SCHEDULED_DATETIME=None,
        DESCRIPTION="テスト",
    )
    prompt = ScheduleManager._generate_schedule_prompt(fake_self, schedule, None, "p1")
    assert "<system" not in prompt and "</system" not in prompt
    assert prompt.startswith("スケジュールの実行時刻です。")
    assert prompt.endswith("スケジュールの説明: テスト")


def test_resumption_prompt_has_no_system_tags():
    from sea.pulse_controller import PulseController

    request = SimpleNamespace(original_prompt="スケジュールの実行時刻です。")
    prompt = PulseController._build_resumption_prompt(object(), request)
    assert "<system" not in prompt and "</system" not in prompt
    assert prompt.startswith("[前回の処理が中断されました]")
    assert prompt.endswith("\n\nスケジュールの実行時刻です。")


# ---------------------------------------------------------------------------
# Playbook の MEMORIZE
# ---------------------------------------------------------------------------


def _run_memorize(node_def, state):
    from sea.runtime import SEARuntime

    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}))
    runtime._store_memory = Mock(return_value=True)
    persona = SimpleNamespace(persona_id="pid", persona_name="p")
    playbook = SimpleNamespace(name="pb", display_name="PB")
    node = runtime._runtime_engine.lg_memorize_node(node_def, persona, playbook, [])
    asyncio.run(node(state))
    return runtime._store_memory


def _memorize_def(**kwargs):
    base = dict(
        id="record", action="{last}", role="system", tags=["playbook_result"],
        line_role=None, scope=None, metadata_key=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_memorize_role_system_carries_the_notice_key():
    store = _run_memorize(_memorize_def(), {"last": "移動しました"})
    store.assert_called_once()
    assert store.call_args.args[1] == "移動しました"
    kwargs = store.call_args.kwargs
    assert kwargs["role"] == "system"
    assert kwargs["metadata"] == {SYSTEM_NOTICE_KEY: "playbook_memorize"}
    assert kwargs["tags"] == ["playbook_result"]


def test_memorize_role_system_keeps_state_metadata_without_mutating_it():
    state_meta = {"media": [{"path": "a.png", "mime_type": "image/png"}]}
    store = _run_memorize(
        _memorize_def(action="{text}", metadata_key="metadata"),
        {"text": "画像を作りました", "metadata": state_meta},
    )
    kwargs = store.call_args.kwargs
    assert kwargs["metadata"] == {
        "media": [{"path": "a.png", "mime_type": "image/png"}],
        SYSTEM_NOTICE_KEY: "playbook_memorize",
    }
    assert SYSTEM_NOTICE_KEY not in state_meta


def test_memorize_role_assistant_does_not_get_the_notice_key():
    store = _run_memorize(_memorize_def(role="assistant"), {"last": "こんにちは"})
    metadata = store.call_args.kwargs["metadata"]
    assert not (isinstance(metadata, dict) and SYSTEM_NOTICE_KEY in metadata)


#: ツールの結果・作業の完了を記録する MEMORIZE (ファイル名 → ノード id)。
_SYSTEM_MEMORIZE_NODES = {
    "building_move_playbook.json": {"record"},
    "create_building_playbook.json": {"announce_result"},
    "document_create_playbook.json": {"record"},
    "document_search_playbook.json": {"record_search", "record_read"},
    "generate_image_playbook.json": {"record"},
    "novel_writing.json": {"memorize_result"},
    "schedule_management_playbook.json": {
        "record_list_result", "record_add_result", "record_delete_result",
    },
}


@pytest.mark.parametrize("filename", sorted(_SYSTEM_MEMORIZE_NODES))
def test_builtin_playbook_notice_memorize_is_role_system_without_tags(filename):
    """ツールの結果・作業の完了を記録する MEMORIZE は role=system・タグ無し本文。"""
    path = REPO_ROOT / "builtin_data" / "playbooks" / "public" / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in data["nodes"] if n.get("type") == "memorize"}
    for node_id in _SYSTEM_MEMORIZE_NODES[filename]:
        node = nodes[node_id]
        assert node.get("role") == "system", (filename, node_id)
    # どの MEMORIZE の本文にも <system> タグを埋め込まない (不変条件 1)
    for node_id, node in nodes.items():
        action = node.get("action") or ""
        assert "<system" not in action and "</system" not in action, (filename, node_id)
