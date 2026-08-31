"""multi-city 凍結 — 入口封鎖の回帰テスト。

2026-07-16 まはー裁定 (docs/handoff/2026-07-15_persona_city_building_separation_audit.md):
inter-city 移動は dispatch 確定処理が呼ばれず実質機能していないため、修正ではなく
機能凍結 + 入口の明示封鎖で対応した。本テストは封鎖された入口が

1. inter-city / persona-proxy API: 503 + 凍結メッセージを返すこと
2. VisitingAI / ThinkingRequest の DB polling: SAIVerseManager が登録しないこと
3. dispatch_persona / return_visiting_persona: 封鎖メッセージで即 return すること

を固定する。封鎖が黙って外れる (= 凍結対象の欠陥コードが再稼働する) 退行を検知
するのが目的。復活時はこのテストごと再設計する。
"""
from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from database import api_server
from manager.visitors import MULTI_CITY_FREEZE_MESSAGE, VisitorMixin


# ---- 1. API 入口: 503 + 凍結メッセージ ----

def _client() -> TestClient:
    # raise_server_exceptions は不要 — 封鎖は HTTPException(503) で返す
    return TestClient(api_server.app)


def test_request_move_in_is_blocked_with_503():
    profile = {
        "persona_id": "p1",
        "persona_name": "Visitor",
        "target_building_id": "b1",
        "saiverse_version": "0.0.0",
    }
    res = _client().post("/inter-city/request-move-in", json=profile)
    assert res.status_code == 503
    assert "凍結" in res.json()["detail"]
    assert "2026-07-16" in res.json()["detail"]


def test_inter_city_buildings_is_blocked_with_503():
    res = _client().get("/inter-city/buildings")
    assert res.status_code == 503
    assert "凍結" in res.json()["detail"]


def test_persona_proxy_think_is_blocked_with_503():
    context = {
        "building_id": "b1",
        "occupants": [],
        "recent_history": [],
        "user_online": False,
    }
    res = _client().post("/persona-proxy/p1/think", json=context)
    assert res.status_code == 503
    assert "凍結" in res.json()["detail"]


# ---- 2. DB polling: SAIVerseManager が登録しない ----

def test_db_polling_is_not_scheduled_by_manager():
    """__init__ が _db_polling_tick を EventScheduler へ積む配線が復活していないこと。

    (フル SAIVerseManager の構築はテストでは重すぎるため、登録行の不在を
    ソースで固定するトリップワイヤ。凍結解除は意図的な再設計を伴うはずで、
    その際はこのテストごと書き換える。)
    """
    import saiverse.saiverse_manager as sm

    src = inspect.getsource(sm)
    assert "callback=self._db_polling_tick" not in src, (
        "inter-city DB polling の登録が復活している。multi-city は凍結中 "
        "(2026-07-16 裁定) — 復活は監査の修正方針を正典に再設計すること。"
    )
    # ポーリング関数本体は残置している (削除ではなく封鎖) こと
    assert hasattr(sm.SAIVerseManager, "_db_polling_tick")


# ---- 3. dispatch / return の入口ガード ----

def test_dispatch_persona_returns_freeze_message():
    mixin = VisitorMixin()
    ok, message = mixin.dispatch_persona("p1", "city_b", "b1")
    assert ok is False
    assert message == MULTI_CITY_FREEZE_MESSAGE
    assert "凍結" in message


def test_return_visiting_persona_returns_freeze_message():
    mixin = VisitorMixin()
    ok, message = mixin.return_visiting_persona("p1", "city_a", "b1")
    assert ok is False
    assert message == MULTI_CITY_FREEZE_MESSAGE
