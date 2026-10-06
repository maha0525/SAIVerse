"""カタログの操作 (install / update / options / uninstall) の鍵と、状態の問い合わせ口。

2026-10-06、voice-tts の 30 分の更新で、進捗の SSE が途中で切れ (Next.js の rewrites の
中継は 30 秒間データが流れないと上流との接続を切る)、画面が完了を受け取れずに
回り続けた。あわせて、鍵 (per-addon lock) をストリームの終わりで放していたため、
接続が切れた瞬間に、まだ走っている操作へ別の操作が割り込める状態だった。

- 鍵は worker が処理を終えるまで持ち続ける (ストリームが閉じられても放さない)
- GET /operations と GET /operations/{addon_id} で、実行中か・どう終わったかが分かる
- 進捗が出ない間も SSE のコメント行 (keepalive) が流れる

docs/issues/addon_install_progress_dialog_stuck_on_long_installs.md
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_manager
from api.routes import addon_catalog
from saiverse.addon_installer import AddonInstallError

ADDON_ID = "test-op-status"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """鍵と操作の記録はモジュールの大域状態なので、テストごとに空にする。"""
    monkeypatch.setattr(addon_catalog, "_addon_locks", {})
    monkeypatch.setattr(addon_catalog, "_running_ops", {})
    monkeypatch.setattr(addon_catalog, "_last_results", {})
    monkeypatch.setattr(addon_catalog, "_has_api_routes", lambda _a: False)


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _events(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]


def test_closing_the_stream_keeps_the_lock_until_the_worker_finishes():
    """SSE の接続が切れても (ストリームが閉じられても)、操作が終わるまで鍵は放さない。"""
    started = threading.Event()
    release = threading.Event()

    def runner(_progress):
        started.set()
        assert release.wait(5)
        return None

    loop = asyncio.new_event_loop()
    try:
        async def open_and_close_stream():
            resp = await addon_catalog._run_with_progress_sse("install", ADDON_ID, runner)
            first = await resp.body_iterator.__anext__()
            # 中継が上流との接続を切ったときと同じく、ストリームを途中で閉じる
            await resp.body_iterator.aclose()
            return first

        first = loop.run_until_complete(open_and_close_stream())
        operation_id = _events(first)[0]["operation_id"]
        assert started.wait(5)

        lock = addon_catalog._get_lock(ADDON_ID)
        assert lock.locked(), "接続が切れただけで鍵が放されている"
        status = addon_catalog.get_operation_status(ADDON_ID)
        assert status.running is True
        assert status.kind == "install"
        assert status.operation_id == operation_id

        release.set()
        assert _wait_until(lambda: not addon_catalog.get_operation_status(ADDON_ID).running)
        assert not lock.locked()
        status = addon_catalog.get_operation_status(ADDON_ID)
        assert status.last_result["ok"] is True
        assert status.last_result["operation_id"] == operation_id
        assert status.last_result["operation"] == "install"
    finally:
        release.set()
        loop.close()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(addon_catalog.router, prefix="/api/addon-catalog")
    app.dependency_overrides[get_manager] = lambda: object()
    return TestClient(app)


@pytest.fixture
def blocking_uninstall(tmp_path, monkeypatch):
    """uninstall の中身を、合図があるまで終わらない偽物に差し替える。"""
    (tmp_path / ADDON_ID).mkdir()
    monkeypatch.setattr(addon_catalog, "EXPANSION_DATA_DIR", tmp_path)
    monkeypatch.setattr(addon_catalog, "_try_unregister_addon", lambda *_a: None)
    state = {"started": threading.Event(), "release": threading.Event(), "error": None}

    def fake_uninstall_addon(**_kwargs):
        state["started"].set()
        assert state["release"].wait(5)
        if state["error"]:
            raise AddonInstallError(state["error"])

    monkeypatch.setattr(addon_catalog, "uninstall_addon", fake_uninstall_addon)
    yield state
    state["release"].set()


def _start_uninstall(client: TestClient) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def run():
        box["res"] = client.post("/api/addon-catalog/uninstall", json={"addon_id": ADDON_ID})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_operation_status_while_running_and_after(blocking_uninstall, monkeypatch):
    monkeypatch.setattr(addon_catalog, "_SSE_KEEPALIVE_SEC", 0.05)
    client = _client()

    res = client.get(f"/api/addon-catalog/operations/{ADDON_ID}")
    assert res.status_code == 200
    assert res.json() == {
        "addon_id": ADDON_ID, "running": False, "kind": None,
        "operation_id": None, "last_result": None,
    }

    t, box = _start_uninstall(client)
    assert blocking_uninstall["started"].wait(5)

    running = client.get("/api/addon-catalog/operations").json()
    assert [(op["addon_id"], op["kind"]) for op in running] == [(ADDON_ID, "uninstall")]
    status = client.get(f"/api/addon-catalog/operations/{ADDON_ID}").json()
    assert status["running"] is True and status["kind"] == "uninstall"
    operation_id = status["operation_id"]

    # 走っている間は、同じアドオンへの別の操作は鍵で断られる
    again = client.post("/api/addon-catalog/uninstall", json={"addon_id": ADDON_ID})
    assert again.status_code == 409
    cancel = client.post("/api/addon-catalog/update/cancel", json={"addon_id": ADDON_ID})
    assert cancel.status_code == 409

    time.sleep(0.2)  # 進捗の行が出ない時間 (keepalive が流れるはず)
    blocking_uninstall["release"].set()
    t.join(5)
    res = box["res"]
    assert res.status_code == 200
    assert ": keepalive" in res.text
    events = _events(res.text)
    assert events[0]["phase"] == "started" and events[0]["operation_id"] == operation_id
    assert events[-1]["phase"] == "finished" and events[-1]["ok"] is True
    assert events[-1]["operation_id"] == operation_id

    status = client.get(f"/api/addon-catalog/operations/{ADDON_ID}").json()
    assert status["running"] is False and status["kind"] is None
    assert status["last_result"]["operation_id"] == operation_id
    assert status["last_result"]["ok"] is True
    assert client.get("/api/addon-catalog/operations").json() == []
    assert not addon_catalog._get_lock(ADDON_ID).locked()


def test_failed_operation_is_recorded_as_failed(blocking_uninstall):
    client = _client()
    blocking_uninstall["error"] = "わざと失敗"
    t, box = _start_uninstall(client)
    assert blocking_uninstall["started"].wait(5)
    blocking_uninstall["release"].set()
    t.join(5)
    assert _events(box["res"].text)[-1]["ok"] is False

    last = client.get(f"/api/addon-catalog/operations/{ADDON_ID}").json()["last_result"]
    assert last["ok"] is False
    assert "わざと失敗" in last["error"]
    assert not addon_catalog._get_lock(ADDON_ID).locked()


@pytest.mark.parametrize("bad_id", ["A", "a.b", "_x"])
def test_operation_status_rejects_malformed_addon_ids(bad_id):
    res = _client().get(f"/api/addon-catalog/operations/{bad_id}")
    assert res.status_code == 400
