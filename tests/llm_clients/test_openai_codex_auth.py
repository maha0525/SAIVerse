"""Codex サブスク認証のテスト。

対象: llm_clients/openai_codex_auth.py (ストア解決・デバイスコードフロー) と
llm_clients/openai_codex.py の refresh 書き戻し先の所有権。
ネットワークには一切出ない (HTTP は全て偽物に差し替える)。

Intent doc: docs/intent/codex_subscription_auth.md §7
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_clients import openai_codex_auth as oca
from llm_clients.openai_codex import OpenAICodexClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _jwt(payload: dict) -> str:
    """Unsigned JWT — decode_jwt_payload only reads the payload segment."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _access_token(account_id: str = "acct-123", expires_in_seconds: int = 3600) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    return _jwt(
        {
            "exp": int(exp.timestamp()),
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }
    )


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """SAIVerse ストアと Codex CLI ストアを両方 tmp に隔離する。"""
    saiverse_dir = tmp_path / "user_data"
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(saiverse_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return {
        "saiverse": saiverse_dir / oca.SAIVERSE_STORE_FILENAME,
        "cli": codex_home / "auth.json",
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeSession:
    """_http_session() の差し替え先。応答キューを順に返し、呼び出しを記録する。"""

    def __init__(self, queue: list):
        self._queue = queue
        self.calls: list = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self._queue:
            raise AssertionError(f"unexpected extra HTTP call to {url}")
        return self._queue.pop(0)

    def close(self):
        pass


@pytest.fixture
def fake_http(monkeypatch):
    """openai_codex_auth の HTTP を応答キュー式の偽セッションへ差し替える。"""
    queue: list = []
    session = FakeSession(queue)
    monkeypatch.setattr(oca, "_http_session", lambda: session)
    return {"queue": queue, "session": session}


# ---------------------------------------------------------------------------
# store resolution
# ---------------------------------------------------------------------------

def test_resolve_none_when_no_store_exists(isolated_stores):
    assert oca.resolve_active_store() is None


def test_resolve_falls_back_to_cli_store(isolated_stores):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "a"}})
    store = oca.resolve_active_store()
    assert store is not None
    assert store.kind == oca.STORE_KIND_CODEX_CLI
    assert store.path == isolated_stores["cli"]


def test_resolve_prefers_saiverse_store(isolated_stores):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "sv"}})
    store = oca.resolve_active_store()
    assert store.kind == oca.STORE_KIND_SAIVERSE
    assert store.path == isolated_stores["saiverse"]


def test_saiverse_store_wins_even_if_broken(isolated_stores):
    """自前ストアが壊れていても CLI へ黙って乗り換えない (intent §3-2)。"""
    isolated_stores["saiverse"].parent.mkdir(parents=True, exist_ok=True)
    isolated_stores["saiverse"].write_text("not json", encoding="utf-8")
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    store = oca.resolve_active_store()
    assert store.kind == oca.STORE_KIND_SAIVERSE
    with pytest.raises(Exception):
        oca.read_auth_store(store.path)


def test_logout_deletes_only_saiverse_store(isolated_stores):
    _write_json(isolated_stores["saiverse"], {"tokens": {}})
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    assert oca.delete_saiverse_store() is True
    assert not isolated_stores["saiverse"].exists()
    assert isolated_stores["cli"].exists()
    # 2 回目は消すものがない
    assert oca.delete_saiverse_store() is False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def test_extract_account_id_from_access_token():
    token = _access_token(account_id="acct-xyz")
    assert oca.extract_account_id(token) == "acct-xyz"


def test_extract_account_id_falls_back_to_id_token():
    id_token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-id"}})
    assert oca.extract_account_id("garbage", id_token) == "acct-id"


def test_access_token_expiry_reads_exp():
    token = _access_token(expires_in_seconds=1000)
    expiry = oca.access_token_expiry(token)
    assert expiry is not None
    remaining = (expiry - datetime.now(tz=timezone.utc)).total_seconds()
    assert 900 < remaining < 1100


# ---------------------------------------------------------------------------
# device-code flow steps
# ---------------------------------------------------------------------------

def test_request_usercode_success(fake_http):
    fake_http["queue"].append(
        FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "dev-1", "interval": "5"})
    )
    manager = oca.CodexDeviceLoginManager()
    device = manager._request_usercode()
    assert device["user_code"] == "ABCD-1234"
    assert device["device_auth_id"] == "dev-1"
    assert device["interval"] == 5
    url, kwargs = fake_http["session"].calls[0]
    assert url == oca.DEVICE_USERCODE_URL
    assert kwargs["json"] == {"client_id": oca.CODEX_OAUTH_CLIENT_ID}


def test_request_usercode_rate_limited(fake_http):
    fake_http["queue"].append(FakeResponse(429))
    manager = oca.CodexDeviceLoginManager()
    with pytest.raises(oca.CodexDeviceLoginError, match="429"):
        manager._request_usercode()


def _open_attempt(
    manager: oca.CodexDeviceLoginManager,
) -> tuple[int, threading.Event, str]:
    """テスト用: HTTP を通さず「現行の試行」を 1 つ開いた状態を作る。"""
    manager._generation += 1
    event = threading.Event()
    manager._current_cancel = event
    lease = f"lease-{manager._generation}"
    manager._leases = {lease}
    return manager._generation, event, lease


def test_poll_pending_then_success(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake_http["queue"].extend(
        [
            FakeResponse(403),
            FakeResponse(404),
            FakeResponse(200, {"authorization_code": "code-1", "code_verifier": "ver-1"}),
        ]
    )
    manager = oca.CodexDeviceLoginManager()
    gen, event, _lease = _open_attempt(manager)
    result = manager._poll_for_authorization(gen, event, "dev-1", "ABCD-1234", interval=0)
    assert result == {"authorization_code": "code-1", "code_verifier": "ver-1"}


def test_poll_unexpected_status_raises(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake_http["queue"].append(FakeResponse(500))
    manager = oca.CodexDeviceLoginManager()
    gen, event, _lease = _open_attempt(manager)
    with pytest.raises(oca.CodexDeviceLoginError, match="500"):
        manager._poll_for_authorization(gen, event, "dev-1", "ABCD-1234", interval=0)


def test_poll_stops_when_cancelled(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    manager = oca.CodexDeviceLoginManager()
    gen, event, _lease = _open_attempt(manager)
    event.set()  # cancel 済み — HTTP を一切呼ばずに None で帰るはず
    result = manager._poll_for_authorization(gen, event, "dev-1", "ABCD-1234", interval=0)
    assert result is None
    assert fake_http["session"].calls == []


def test_poll_stops_when_superseded(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    manager = oca.CodexDeviceLoginManager()
    gen, event, _lease = _open_attempt(manager)
    _open_attempt(manager)  # 新しい試行に追い越された
    result = manager._poll_for_authorization(gen, event, "dev-1", "ABCD-1234", interval=0)
    assert result is None
    assert fake_http["session"].calls == []


def test_exchange_code_posts_authorization_grant(fake_http):
    fake_http["queue"].append(
        FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    )
    manager = oca.CodexDeviceLoginManager()
    tokens = manager._exchange_code({"authorization_code": "code-1", "code_verifier": "ver-1"})
    assert tokens["access_token"] == "at"
    url, kwargs = fake_http["session"].calls[0]
    assert url == oca.CODEX_REFRESH_TOKEN_URL
    body = kwargs["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code-1"
    assert body["code_verifier"] == "ver-1"
    assert body["redirect_uri"] == oca.DEVICE_REDIRECT_URI


def test_persist_login_writes_saiverse_store(isolated_stores):
    access = _access_token(account_id="acct-777")
    oca.persist_saiverse_login({"access_token": access, "refresh_token": "rt", "id_token": "it"})
    data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == access
    assert data["tokens"]["refresh_token"] == "rt"
    assert data["tokens"]["account_id"] == "acct-777"
    assert data["source"] == "saiverse_device_login"
    # 一意名 tmp が残っていない
    assert not list(isolated_stores["saiverse"].parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
# F1: 世代競合 — cancel 済み・追い越された試行は保存も状態更新もできない
# ---------------------------------------------------------------------------

def test_commit_rejected_after_cancel(isolated_stores):
    """認可コード取得後〜保存前に cancel された試行はトークンを保存しない。"""
    manager = oca.CodexDeviceLoginManager()
    gen, event, lease = _open_attempt(manager)
    manager._state = {"state": oca.LOGIN_STATE_WAITING, "attempt_id": gen}
    assert manager.cancel(gen, lease) is True  # 交換処理中に cancel が届いた想定
    manager._commit_login(
        gen, event, oca.read_logout_marker(),
        {"access_token": _access_token(), "refresh_token": "rt"},
    )
    assert not isolated_stores["saiverse"].exists()
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE


def test_commit_rejected_for_stale_generation(isolated_stores):
    """cancel 後に再 start しても、旧世代の結果は新試行を上書きできない。"""
    manager = oca.CodexDeviceLoginManager()
    old_gen, old_event, _old_lease = _open_attempt(manager)
    new_gen, _new_event, _nl = _open_attempt(manager)  # 再 start に相当
    manager._state = {"state": oca.LOGIN_STATE_WAITING, "attempt_id": new_gen}
    manager._commit_login(
        old_gen, old_event, oca.read_logout_marker(),
        {"access_token": _access_token("acct-old"), "refresh_token": "rt"},
    )
    assert not isolated_stores["saiverse"].exists()
    assert manager.status() == {"state": oca.LOGIN_STATE_WAITING, "attempt_id": new_gen}
    # 現行世代なら保存できる
    manager._commit_login(
        new_gen, threading.Event(), oca.read_logout_marker(),
        {"access_token": _access_token("acct-new"), "refresh_token": "rt"},
    )
    data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert data["tokens"]["account_id"] == "acct-new"


def test_fail_from_stale_generation_does_not_touch_state(isolated_stores):
    manager = oca.CodexDeviceLoginManager()
    old_gen, old_event, _old_lease = _open_attempt(manager)
    new_gen, _e, _l = _open_attempt(manager)
    manager._state = {"state": oca.LOGIN_STATE_WAITING, "attempt_id": new_gen}
    manager._fail(old_gen, old_event, "old failure")
    assert manager.status() == {"state": oca.LOGIN_STATE_WAITING, "attempt_id": new_gen}


def test_cancel_with_stale_attempt_or_lease_is_ignored(isolated_stores, fake_http, monkeypatch):
    """遅延した旧 cancel (閉じたモーダル由来) が新試行を殺さない。"""
    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_run", lambda self, *a: None)
    fake_http["queue"].append(
        FakeResponse(200, {"user_code": "AAAA-1111", "device_auth_id": "dev-1", "interval": 3})
    )
    manager = oca.CodexDeviceLoginManager()
    started = manager.start()
    current_id = started["attempt_id"]
    lease = started["lease_id"]
    assert manager.cancel(current_id - 1, lease) is False  # 旧世代の cancel は無視
    assert manager.status()["state"] == oca.LOGIN_STATE_WAITING
    assert manager.cancel(current_id, "unknown-lease") is False  # 未知の lease も無視
    assert manager.status()["state"] == oca.LOGIN_STATE_WAITING
    assert manager.cancel(current_id, lease) is True
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE
    # 確定後の cancel も no-op
    manager._state = {"state": oca.LOGIN_STATE_SUCCESS, "attempt_id": current_id}
    manager._leases = {lease}
    assert manager.cancel(current_id, lease) is False
    assert manager.status()["state"] == oca.LOGIN_STATE_SUCCESS


def test_join_shares_attempt_and_delayed_cancel_cannot_kill_it(
    isolated_stores, fake_http, monkeypatch
):
    """R2-①の再現: 相乗りした 2 クライアントの片方の遅延 cancel が試行を止めない。

    進行中の試行への 2 回目の start は同じ attempt_id / user_code に相乗りし
    (usercode 申請は 1 回だけ)、別の lease を受け取る。閉じたモーダル側の
    cancel は自分の lease を返すだけで、残る lease がある限り試行は生きる。
    """
    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_run", lambda self, *a: None)
    fake_http["queue"].append(
        FakeResponse(200, {"user_code": "CCCC-3333", "device_auth_id": "dev-3", "interval": 3})
    )
    manager = oca.CodexDeviceLoginManager()
    first = manager.start()   # モーダル A
    second = manager.start()  # 開き直したモーダル B (相乗り)
    assert second["attempt_id"] == first["attempt_id"]
    assert second["user_code"] == first["user_code"]
    assert second["lease_id"] != first["lease_id"]
    assert len(fake_http["session"].calls) == 1  # usercode 申請は 1 回だけ

    # A の遅延 cancel — B が lease を持つ限り試行は止まらない
    assert manager.cancel(first["attempt_id"], first["lease_id"]) is False
    assert manager.status()["state"] == oca.LOGIN_STATE_WAITING
    # B も返却して初めて止まる
    assert manager.cancel(second["attempt_id"], second["lease_id"]) is True
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE


def test_concurrent_starts_single_usercode_request(isolated_stores, monkeypatch):
    """R2-④の再現: STARTING 中の並行 start は待たされ、同じ試行に相乗りする。"""
    manager = oca.CodexDeviceLoginManager()
    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_run", lambda self, *a: None)

    gate = threading.Event()
    usercode_calls = []

    def _slow_usercode(self):
        usercode_calls.append(1)
        assert gate.wait(timeout=10), "gate never opened"
        return {"user_code": "DDDD-4444", "device_auth_id": "dev-4", "interval": 3}

    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_request_usercode", _slow_usercode)

    results: list = [None, None]

    def _starter(idx):
        results[idx] = manager.start()

    t1 = threading.Thread(target=_starter, args=(0,))
    t1.start()
    # t1 が usercode 往復に入るのを待つ
    deadline = time.monotonic() + 5
    while not usercode_calls and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert usercode_calls, "first start never reached the usercode request"
    t2 = threading.Thread(target=_starter, args=(1,))
    t2.start()
    gate.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    assert len(usercode_calls) == 1  # 申請は 1 回だけ (二重申請の 429 誘発なし)
    a, b = results
    assert a["state"] == oca.LOGIN_STATE_WAITING
    assert b["state"] == oca.LOGIN_STATE_WAITING
    assert a["attempt_id"] == b["attempt_id"]
    assert a["user_code"] == b["user_code"] == "DDDD-4444"
    assert a["lease_id"] != b["lease_id"]


def test_cancel_during_usercode_request_prevents_thread_spawn(isolated_stores, monkeypatch):
    """start 応答前 (デバイスコード申請中) の cancel でポーリングが始まらない。"""
    manager = oca.CodexDeviceLoginManager()

    def _usercode_with_midflight_cancel(self):
        # 申請の往復中に cancel が届いた状況を再現
        lease = next(iter(manager._leases))
        assert manager.cancel(manager.status()["attempt_id"], lease) is True
        return {"user_code": "BBBB-2222", "device_auth_id": "dev-2", "interval": 3}

    monkeypatch.setattr(
        oca.CodexDeviceLoginManager, "_request_usercode", _usercode_with_midflight_cancel
    )
    spawned = []
    monkeypatch.setattr(
        threading, "Thread",
        lambda *a, **k: spawned.append(k) or pytest.fail("poll thread must not spawn"),
    )
    result = manager.start()
    assert result["state"] == oca.LOGIN_STATE_IDLE
    assert spawned == []


def test_start_queued_before_logout_cannot_revive_after_logout(isolated_stores, monkeypatch):
    """R3-①の再現: logout 前に受理された start は、logout 後に新試行を開けない。

    順序を固定する: A が usercode 往復中に B が start へ入り marker を snapshot、
    その後 logout が完了し、それから A/B を進める。A は世代切れで idle、
    B は marker 不一致で中断エラーにならなければならない。
    """
    manager = oca.CodexDeviceLoginManager()
    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_run", lambda self, *a: None)

    gate = threading.Event()
    a_entered_usercode = threading.Event()
    snapshot_events: dict = {}

    def _slow_usercode(self):
        a_entered_usercode.set()
        assert gate.wait(timeout=10), "gate never opened"
        return {"user_code": "EEEE-5555", "device_auth_id": "dev-5", "interval": 3}

    def _hooked_snapshot(self):
        value = oca.read_logout_marker()
        snapshot_events.setdefault(threading.current_thread().name, threading.Event()).set()
        return value

    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_request_usercode", _slow_usercode)
    monkeypatch.setattr(oca.CodexDeviceLoginManager, "_snapshot_logout_marker", _hooked_snapshot)

    outcomes: dict = {}

    def _start(name):
        try:
            outcomes[name] = manager.start()
        except oca.CodexDeviceLoginError as exc:
            outcomes[name] = exc

    ta = threading.Thread(target=_start, args=("start-a",), name="start-a")
    ta.start()
    assert a_entered_usercode.wait(timeout=5)

    tb = threading.Thread(target=_start, args=("start-b",), name="start-b")
    tb.start()
    # B が marker を snapshot して _start_lock 待ちに入ったことを確認してから logout
    deadline = time.monotonic() + 5
    while "start-b" not in snapshot_events and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert snapshot_events["start-b"].wait(timeout=5)

    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "old"}})
    assert manager.logout_and_delete_store() is True

    gate.set()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert not ta.is_alive() and not tb.is_alive()

    # A: 世代切れで idle (試行は開かない)
    assert isinstance(outcomes["start-a"], dict)
    assert outcomes["start-a"]["state"] == oca.LOGIN_STATE_IDLE
    # B: logout に追い越されたので中断エラー
    assert isinstance(outcomes["start-b"], oca.CodexDeviceLoginError)
    # ストアは消えたまま・状態は idle のまま
    assert not isolated_stores["saiverse"].exists()
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE


def test_logout_invalidates_inflight_login(isolated_stores):
    """R2-②の再現: logout 後に生き残ったワーカーがストアを再生成できない。"""
    manager = oca.CodexDeviceLoginManager()
    gen, event, _lease = _open_attempt(manager)
    marker_at_start = oca.read_logout_marker()
    manager._state = {"state": oca.LOGIN_STATE_WAITING, "attempt_id": gen}
    # ログイン済みの状態から logout
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "old"}})
    assert manager.logout_and_delete_store() is True
    assert not isolated_stores["saiverse"].exists()
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE
    # ブラウザ側の認証が logout とすれ違いで完了した想定 — 保存は拒否される
    manager._commit_login(
        gen, event, marker_at_start,
        {"access_token": _access_token(), "refresh_token": "rt"},
    )
    assert not isolated_stores["saiverse"].exists()
    assert manager.status()["state"] == oca.LOGIN_STATE_IDLE


def test_logout_in_another_process_blocks_this_process_commit(isolated_stores):
    """R5-①の再現: 同じ ~/.saiverse を共有する別プロセスの logout が効く。

    別プロセスは別の manager インスタンスとして再現する (プロセス内メモリを
    共有しない点が本質で、共有されるのは logout marker ファイルとストアだけ)。
    """
    manager_a = oca.CodexDeviceLoginManager()  # ログイン進行中のプロセス
    manager_b = oca.CodexDeviceLoginManager()  # logout を受けたプロセス
    gen, event, _lease = _open_attempt(manager_a)
    marker_at_start = oca.read_logout_marker()
    manager_a._state = {"state": oca.LOGIN_STATE_WAITING, "attempt_id": gen}

    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "old"}})
    assert manager_b.logout_and_delete_store() is True
    assert not isolated_stores["saiverse"].exists()

    # A のワーカーは自プロセスの世代チェックを素通りするが、
    # 永続 logout marker の再照合 (ストアロック下) で拒否される
    manager_a._commit_login(
        gen, event, marker_at_start,
        {"access_token": _access_token(), "refresh_token": "rt"},
    )
    assert not isolated_stores["saiverse"].exists()
    assert manager_a.status()["state"] == oca.LOGIN_STATE_IDLE


def test_persist_refuses_on_logout_marker_mismatch(isolated_stores):
    marker = oca.read_logout_marker()
    with oca.saiverse_store_lock():
        oca._renew_logout_marker_locked()
    with pytest.raises(oca.LoginInvalidatedByLogout):
        oca.persist_saiverse_login(
            {"access_token": _access_token(), "refresh_token": "rt"},
            expected_logout_marker=marker,
        )
    assert not isolated_stores["saiverse"].exists()


def test_marker_corruption_cannot_recreate_old_value(isolated_stores):
    """R6-①の再現 (ABA): marker ファイルの破損・削除は必ず「保存拒否」側に倒れる。

    counter 方式だと 破損→0 扱い→logout で 1 に戻り、昔の snapshot 1 と一致して
    logout 後の保存が通った。nonce 方式では logout のたびに新しい乱数になるので、
    どんな破損・削除を挟んでも古い snapshot と一致することはない。
    """
    # 一度 logout して marker ファイルを実在させ、その値を snapshot に持つ試行を作る
    manager = oca.CodexDeviceLoginManager()
    manager.logout_and_delete_store()
    marker_at_start = oca.read_logout_marker()
    assert marker_at_start != ""

    # marker ファイルが破損 → 消失、その後さらに logout が起きる
    oca._logout_marker_path().write_text("garbage!!", encoding="utf-8")
    oca._logout_marker_path().unlink()
    manager.logout_and_delete_store()

    # 昔の snapshot での保存は拒否されなければならない
    with pytest.raises(oca.LoginInvalidatedByLogout):
        oca.persist_saiverse_login(
            {"access_token": _access_token(), "refresh_token": "rt"},
            expected_logout_marker=marker_at_start,
        )
    assert not isolated_stores["saiverse"].exists()


def test_pristine_snapshot_then_logout_then_marker_deletion_still_refused(isolated_stores):
    """R7-①の再現 (欠落 ABA): 「logout の後に marker が消える」順序でも保存拒否。

    snapshot が "" (欠落の読み値) を正当な値として持てると、logout 後に marker
    ファイルが消えたとき読みが再び "" になって一致してしまう。snapshot は初回に
    marker を初期化してから取るので、"" は snapshot に存在せず、削除後の比較は
    必ず不一致 = 拒否になる。
    """
    manager = oca.CodexDeviceLoginManager()
    # pristine 状態での snapshot — 初期化が走り、空にはならない
    marker_at_start = manager._snapshot_logout_marker()
    assert marker_at_start != ""
    assert oca._logout_marker_path().exists()

    manager.logout_and_delete_store()
    # logout の後に marker ファイルが破壊される (削除)
    oca._logout_marker_path().unlink()
    assert oca.read_logout_marker() == ""

    with pytest.raises(oca.LoginInvalidatedByLogout):
        oca.persist_saiverse_login(
            {"access_token": _access_token(), "refresh_token": "rt"},
            expected_logout_marker=marker_at_start,
        )
    assert not isolated_stores["saiverse"].exists()


def test_unreadable_marker_fails_closed(isolated_stores, monkeypatch):
    """読み取り失敗 (OSError) は毎回異なる値になり、いかなる snapshot とも一致しない。"""
    def _boom(*a, **k):
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(Path, "read_text", _boom)
    first = oca.read_logout_marker()
    second = oca.read_logout_marker()
    assert first.startswith("unreadable:")
    assert first != second


# ---------------------------------------------------------------------------
# F2: ストア操作の直列化
# ---------------------------------------------------------------------------

def _assert_blocks_under_held_lock(isolated_stores, monkeypatch, operation):
    """ストアロックを別スレッド視点で保持したまま operation を走らせ、Timeout を確認。"""
    from filelock import Timeout as FileLockTimeout

    monkeypatch.setattr(oca, "STORE_LOCK_TIMEOUT_SECONDS", 0.2)
    holder = oca.saiverse_store_lock(timeout=5)
    holder.acquire()
    try:
        result: dict = {}

        def _worker():
            try:
                operation()
                result["outcome"] = "completed"
            except FileLockTimeout:
                result["outcome"] = "timeout"
            except Exception as exc:  # noqa: BLE001
                result["outcome"] = f"error: {exc}"

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "operation neither completed nor timed out"
        assert result["outcome"] == "timeout"
    finally:
        holder.release()


def test_logout_serializes_on_store_lock(isolated_stores, monkeypatch):
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "a"}})
    _assert_blocks_under_held_lock(isolated_stores, monkeypatch, oca.delete_saiverse_store)
    # ロック解放後は普通に消せる
    assert oca.delete_saiverse_store() is True


def test_login_persist_serializes_on_store_lock(isolated_stores, monkeypatch):
    _assert_blocks_under_held_lock(
        isolated_stores, monkeypatch,
        lambda: oca.persist_saiverse_login(
            {"access_token": _access_token(), "refresh_token": "rt"}
        ),
    )


def test_logout_does_not_unlink_held_lock_file(isolated_stores, monkeypatch):
    """保持中ロックファイルの unlink は POSIX で相互排他を破る — logout は触らない。

    (解放後のロックファイル削除は filelock ライブラリ自身の管轄で、Windows では
    release 時に消える。ここで守るのは「他者が保持している間に消さない」こと。)
    """
    from filelock import Timeout as FileLockTimeout

    monkeypatch.setattr(oca, "STORE_LOCK_TIMEOUT_SECONDS", 0.2)
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "a"}})
    lock_path = isolated_stores["saiverse"].with_suffix(".json.lock")
    holder = oca.saiverse_store_lock(timeout=5)
    holder.acquire()
    try:
        result: dict = {}

        def _worker():
            try:
                oca.delete_saiverse_store()
                result["outcome"] = "completed"
            except FileLockTimeout:
                result["outcome"] = "timeout"

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=10)
        assert result["outcome"] == "timeout"
        assert lock_path.exists()  # 保持中のロックファイルは無傷
        assert isolated_stores["saiverse"].exists()  # ストアも消えていない
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# F3: トークンファイルの権限 (POSIX のみ実測、Windows では chmod が no-op)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode semantics only")
def test_store_file_mode_is_0600(isolated_stores):
    oca.write_auth_store(isolated_stores["saiverse"], {"tokens": {"access_token": "a"}})
    mode = isolated_stores["saiverse"].stat().st_mode & 0o777
    assert mode == 0o600


def test_full_login_flow_end_to_end(isolated_stores, fake_http, monkeypatch):
    """start() → 背景スレッド → 成功状態 + ストア書き込み、の一気通貫。"""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    access = _access_token(account_id="acct-e2e")
    fake_http["queue"].extend(
        [
            FakeResponse(200, {"user_code": "WXYZ-9876", "device_auth_id": "dev-9", "interval": 0}),
            FakeResponse(403),
            FakeResponse(200, {"authorization_code": "code-9", "code_verifier": "ver-9"}),
            FakeResponse(200, {"access_token": access, "refresh_token": "rt-9"}),
        ]
    )
    manager = oca.CodexDeviceLoginManager()
    started = manager.start()
    assert started["state"] == oca.LOGIN_STATE_WAITING
    assert started["user_code"] == "WXYZ-9876"
    assert started["verification_url"] == oca.DEVICE_VERIFICATION_URL

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if manager.status()["state"] in (oca.LOGIN_STATE_SUCCESS, oca.LOGIN_STATE_ERROR):
            break
        threading.Event().wait(0.02)
    status = manager.status()
    assert status["state"] == oca.LOGIN_STATE_SUCCESS
    assert status["account_id"] == "acct-e2e"
    assert isolated_stores["saiverse"].exists()
    # トークンは status に出ない
    assert "access_token" not in json.dumps(status)


def test_status_payload_never_contains_tokens(isolated_stores):
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": _access_token(), "refresh_token": "secret-rt"}},
    )
    status = oca.auth_status()
    dumped = json.dumps(status)
    assert "secret-rt" not in dumped
    assert status["logged_in"] is True
    assert status["store"] == oca.STORE_KIND_SAIVERSE


# ---------------------------------------------------------------------------
# refresh write-back ownership (openai_codex.py 側)
# ---------------------------------------------------------------------------

def _client() -> OpenAICodexClient:
    return OpenAICodexClient(model="codex-test-model")


def test_refresh_writes_back_to_saiverse_store(isolated_stores, monkeypatch):
    old_access = _access_token(expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}},
    )
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": "cli-access", "refresh_token": "cli-rt"}},
    )
    client = _client()
    new_access = _access_token(expires_in_seconds=3600)
    monkeypatch.setattr(
        client,
        "_request_refresh",
        lambda rt: {"access_token": new_access, "refresh_token": "rt-new"},
    )
    client._refresh_or_pickup_latest(prior_access_token=old_access)

    saiverse_data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert saiverse_data["tokens"]["access_token"] == new_access
    assert saiverse_data["tokens"]["refresh_token"] == "rt-new"
    # CLI ストアは無傷 (書き戻し先の所有権、intent §3-1)
    cli_data = json.loads(isolated_stores["cli"].read_text(encoding="utf-8"))
    assert cli_data["tokens"]["access_token"] == "cli-access"
    assert cli_data["tokens"]["refresh_token"] == "cli-rt"


def test_refresh_writes_back_to_cli_store_when_piggybacking(isolated_stores, monkeypatch):
    old_access = _access_token(expires_in_seconds=-100)
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}},
    )
    client = _client()
    new_access = _access_token(expires_in_seconds=3600)
    monkeypatch.setattr(
        client,
        "_request_refresh",
        lambda rt: {"access_token": new_access, "refresh_token": "rt-new"},
    )
    client._refresh_or_pickup_latest(prior_access_token=old_access)
    cli_data = json.loads(isolated_stores["cli"].read_text(encoding="utf-8"))
    assert cli_data["tokens"]["access_token"] == new_access
    assert not isolated_stores["saiverse"].exists()


def test_refresh_picks_up_fresh_token_without_spending_refresh_token(
    isolated_stores, monkeypatch
):
    """他プロセスが更新済みなら refresh_token を使わない (既存挙動の維持)。"""
    fresh_access = _access_token(expires_in_seconds=3600)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": fresh_access, "refresh_token": "rt-keep"}},
    )
    client = _client()

    def _fail(_rt):
        raise AssertionError("refresh_token must not be spent when a fresh token exists")

    monkeypatch.setattr(client, "_request_refresh", _fail)
    client._refresh_or_pickup_latest(prior_access_token="stale-different-token")
    data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert data["tokens"]["refresh_token"] == "rt-keep"


def test_load_auth_error_mentions_both_login_paths(isolated_stores):
    client = _client()
    with pytest.raises(RuntimeError) as exc_info:
        client._load_auth()
    message = str(exc_info.value)
    assert "ログイン" in message
    assert "codex login" in message


# ---------------------------------------------------------------------------
# API routes (最小アプリに載せて叩く)
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import codex_auth as codex_auth_routes

    app = FastAPI()
    app.include_router(codex_auth_routes.router, prefix="/codex-auth")
    return TestClient(app), codex_auth_routes


def test_route_status_and_logout(api_client, isolated_stores):
    client, _routes = api_client
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": _access_token(), "refresh_token": "rt"}},
    )
    res = client.get("/codex-auth/status")
    assert res.status_code == 200
    assert res.json()["store"] == "saiverse"

    res = client.post("/codex-auth/logout")
    assert res.status_code == 200
    body = res.json()
    assert body["removed"] is True
    assert body["logged_in"] is False
    assert not isolated_stores["saiverse"].exists()


def test_route_login_start_maps_flow_error_to_502(api_client, monkeypatch):
    client, routes = api_client

    def _boom():
        raise oca.CodexDeviceLoginError("throttled")

    monkeypatch.setattr(routes.LOGIN_MANAGER, "start", _boom)
    res = client.post("/codex-auth/login/start")
    assert res.status_code == 502
    assert "throttled" in res.json()["detail"]


def test_route_login_status_reflects_manager(api_client, monkeypatch):
    client, routes = api_client
    monkeypatch.setattr(
        routes.LOGIN_MANAGER,
        "status",
        lambda: {"state": "waiting", "user_code": "AAAA-0000"},
    )
    res = client.get("/codex-auth/login/status")
    assert res.status_code == 200
    assert res.json()["user_code"] == "AAAA-0000"


def test_route_cancel_requires_attempt_id_and_lease_id(api_client, monkeypatch):
    """R2-③の再現: 引数省略の cancel は 422 — 世代ガードの迂回路を作らない。"""
    client, routes = api_client
    assert client.post("/codex-auth/login/cancel").status_code == 422  # body なし
    assert client.post("/codex-auth/login/cancel", json={}).status_code == 422
    assert (
        client.post("/codex-auth/login/cancel", json={"attempt_id": 1}).status_code == 422
    )  # lease_id 欠落

    calls: list = []
    monkeypatch.setattr(
        routes.LOGIN_MANAGER,
        "cancel",
        lambda attempt_id, lease_id: calls.append((attempt_id, lease_id)) or False,
    )
    res = client.post(
        "/codex-auth/login/cancel", json={"attempt_id": 3, "lease_id": "x"}
    )
    assert res.status_code == 200
    assert res.json()["cancelled"] is False
    assert calls == [(3, "x")]
