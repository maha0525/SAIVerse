"""頭でのツール一覧取得 (sea/mcp_tool_refresh.py) の契約テスト。

設計は docs/intent/mcp_addon_integration.md §I。この 1 関数が Pulse root 2 本
(run_meta_user / run_work_session) と Beat 境界 (_run_spell_loop) から呼ばれる
唯一の取得点なので、ここが負う約束を固定する:

- 変化があり notify=True のときだけ検知器へ渡す (知覚バッファへ push)
- 直後に無条件の検知フェーズが走る呼び出し元は notify=False で二重検知を避ける
- 取得の失敗で例外を投げない (Pulse を止めない)
- connect の別で timeout が変わる (接続を張る回は長め、聞き直すだけは短め)
"""
from types import SimpleNamespace
from unittest import mock

from sea.mcp_tool_refresh import (
    _BEAT_HEAD_TIMEOUT_SECONDS,
    _PULSE_HEAD_TIMEOUT_SECONDS,
    refresh_mcp_tools_at_head,
)

_PERSONA = SimpleNamespace(persona_id="air_city_a")
_MANAGER = SimpleNamespace(name="manager")


def _patch(refresh_return=False, refresh_error=None, calls=None):
    def _fake_sync(persona_id, *, connect=True, timeout=None):
        if calls is not None:
            calls.append({
                "persona_id": persona_id, "connect": connect, "timeout": timeout,
            })
        if refresh_error is not None:
            raise refresh_error
        return refresh_return

    return mock.patch(
        "tools.mcp_client.refresh_persona_tools_sync", new=_fake_sync
    )


def test_change_is_handed_to_the_detector():
    """一覧が変わったら検知器へ渡す (= 知覚バッファへ積まれ、直後の flush で届く)。"""
    injected = []
    with _patch(refresh_return=True), mock.patch(
        "sea.head_pipeline.inject_diff_notifications",
        new=lambda p, m, b: injected.append((p, m, b)) or True,
    ):
        changed = refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, "b1", connect=False,
        )

    assert changed is True
    assert injected == [(_PERSONA, _MANAGER, "b1")]


def test_notify_false_skips_the_detector():
    """直後に無条件の検知フェーズが走る呼び出し元 (run_meta_user) では検知しない。

    同じ差分を二度検知しても結果は変わらないが、head 入力の組み立てと台帳の
    execution を無駄に 1 本増やす。
    """
    injected = []
    with _patch(refresh_return=True), mock.patch(
        "sea.head_pipeline.inject_diff_notifications",
        new=lambda p, m, b: injected.append(b) or True,
    ):
        changed = refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, "b1", connect=True, notify=False,
        )

    assert changed is True
    assert injected == []


def test_no_change_skips_the_detector():
    injected = []
    with _patch(refresh_return=False), mock.patch(
        "sea.head_pipeline.inject_diff_notifications",
        new=lambda p, m, b: injected.append(b) or True,
    ):
        assert refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, "b1", connect=False,
        ) is False
    assert injected == []


def test_fetch_failure_does_not_raise():
    """取得の失敗で Pulse を止めない (次の頭で再試行される)。"""
    with _patch(refresh_error=RuntimeError("mcp loop is gone")):
        assert refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, "b1", connect=True,
        ) is False


def test_detector_failure_does_not_raise():
    """検知器が落ちても Pulse は続く (差分は次の頭で拾い直される)。"""
    with _patch(refresh_return=True), mock.patch(
        "sea.head_pipeline.inject_diff_notifications",
        side_effect=RuntimeError("ledger down"),
    ):
        assert refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, "b1", connect=True,
        ) is True


def test_missing_persona_id_is_a_noop():
    calls = []
    with _patch(calls=calls):
        assert refresh_mcp_tools_at_head(
            SimpleNamespace(), _MANAGER, "b1", connect=True,
        ) is False
    assert calls == []


def test_timeout_depends_on_whether_we_connect():
    """接続を張る回は subprocess 起動込みで長め、聞き直すだけの回は短め。"""
    calls = []
    with _patch(calls=calls):
        refresh_mcp_tools_at_head(_PERSONA, _MANAGER, "b1", connect=True)
        refresh_mcp_tools_at_head(_PERSONA, _MANAGER, "b1", connect=False)

    assert [c["timeout"] for c in calls] == [
        _PULSE_HEAD_TIMEOUT_SECONDS, _BEAT_HEAD_TIMEOUT_SECONDS,
    ]
    assert [c["connect"] for c in calls] == [True, False]


def test_building_unknown_skips_the_detector_but_still_fetches():
    """現在地が確定していない呼び出しでも取得はする (検知は基準が無いので後回し)。"""
    calls = []
    injected = []
    with _patch(refresh_return=True, calls=calls), mock.patch(
        "sea.head_pipeline.inject_diff_notifications",
        new=lambda p, m, b: injected.append(b) or True,
    ):
        assert refresh_mcp_tools_at_head(
            _PERSONA, _MANAGER, None, connect=True,
        ) is True

    assert len(calls) == 1
    assert injected == []
