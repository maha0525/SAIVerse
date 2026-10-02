"""LlamaServerManager の busy 判定・idle 停止・リクエスト前保証の安全契約。

2026-08-03 の実害: /health はスロットが推論中でも 200 を返すため、
「status != 200 なら busy」という旧判定は常に「暇」と答え、idle checker が
プロンプト処理 39% のサーバーを停止して 25 分級の応答を二度殺した。

契約:
1. 状態は三値 — "idle" (全スロット暇と証明) / "busy" (処理中と証明) /
   "unknown" (証明不能)。判定材料は /slots の ``is_processing`` (厳密な bool)
2. "unknown" は**決して停止根拠にならない** (busy_deadline 強制停止にも使わない)。
   警告は世代ごとに 30 分間隔で繰り返す
3. プロセスが死んでいるときだけ接続不能でも "idle" (掃除経路を塞がない)
4. 停止は「観測時と同じサーバーが、今なお停止条件を満たす」ときだけ。
   idle 停止は kill 直前にロック内で /slots を再確認する
5. ensure_running は毎リクエスト前に呼ばれる。管理下プロセスが生存して
   いれば HTTP を打たず activity 申告だけ (高速経路) — /health の一時不調で
   生存中のサーバーを二重起動しない
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from llm_clients.llama_server import LlamaServerManager, ManagedServer

PORT = 8088
BASE_URL = f"http://127.0.0.1:{PORT}/v1"
ENDPOINT = ("127.0.0.1", PORT)
OTHER_ENDPOINT = ("nebula", PORT)
OTHER_URL = f"http://nebula:{PORT}/v1"


@pytest.fixture(autouse=True)
def _no_live_backend(monkeypatch):
    """失敗したテストも実 HTTP・プロセス起動・停止・監視スレッドへ漏らさない。"""
    def forbidden(*args, **kwargs):
        pytest.fail("Live backend interaction is forbidden in isolated manager tests")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr("llm_clients.llama_server.subprocess.Popen", forbidden)
    monkeypatch.setattr(LlamaServerManager, "_kill_process_tree", forbidden)
    monkeypatch.setattr(LlamaServerManager, "_ensure_idle_checker", forbidden)
    # SDK の構築も置換する。API 呼び出しは各テストが明示的に偽物を差す。
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=forbidden)))
    monkeypatch.setattr("llm_clients.openai.OpenAI", lambda **kwargs: sdk)


def _proc(alive: bool = True):
    return SimpleNamespace(
        pid=1234,
        poll=lambda: None if alive else 1,
        returncode=None if alive else 1,
    )


def _managed(*, alive=True, host="127.0.0.1", identity="test",
             idle_for=0.0, idle_timeout=600.0, busy_deadline=3600.0):
    return ManagedServer(
        process=_proc(alive),
        identity=identity,
        port=PORT,
        config={},
        host=host,
        idle_timeout=idle_timeout,
        busy_deadline=busy_deadline,
        last_activity=time.monotonic() - idle_for,
    )


def _register(mgr, **kwargs):
    managed = _managed(**kwargs)
    mgr._servers[(managed.host, PORT)] = managed
    return managed


def _resp(status_code: int, payload=None, bad_json: bool = False):
    def _json():
        if bad_json:
            raise ValueError("not json")
        return payload

    return SimpleNamespace(status_code=status_code, json=_json)


# ---------------------------------------------------------------------------
# 契約1: /slots の is_processing による三値判定 (b10229 実フォーマット準拠)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slots, expected",
    [
        ([{"id": 0, "id_task": 135, "n_ctx": 102400, "is_processing": True}], "busy"),
        (
            [
                {"id": 0, "id_task": -1, "is_processing": False},
                {"id": 1, "id_task": 7, "is_processing": True},
            ],
            "busy",
        ),
        ([{"id": 0, "id_task": -1, "is_processing": False}], "idle"),
    ],
)
def test_probe_follows_is_processing(slots, expected):
    mgr = LlamaServerManager()
    with patch(
        "llm_clients.llama_server.httpx.get", return_value=_resp(200, slots)
    ) as get:
        assert mgr._probe_slots(PORT, _managed()) == expected
    # /health ではなく /slots を、指定 host に対して叩いていること
    assert get.call_args.args[0] == f"http://127.0.0.1:{PORT}/slots"


def test_probe_uses_configured_host():
    """非 loopback で立てたサーバーは同じ host に問い合わせる。"""
    mgr = LlamaServerManager()
    with patch(
        "llm_clients.llama_server.httpx.get",
        return_value=_resp(200, [{"is_processing": False}]),
    ) as get:
        assert mgr._probe_slots(PORT, _managed(host="192.168.1.20")) == "idle"
    assert get.call_args.args[0] == f"http://192.168.1.20:{PORT}/slots"


# ---------------------------------------------------------------------------
# 契約2: 証明不能は "unknown" + warn (世代ごと・間隔付き)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "resp",
    [
        _resp(501),                                        # --no-slots で無効化
        _resp(200, bad_json=True),                         # JSON でない
        _resp(200, {"slots": []}),                         # 配列でない
        _resp(200, []),                                    # 空 = スロットが見えない
        _resp(200, [{"id": 0}]),                           # is_processing 欠損
        _resp(200, [{"id": 0, "is_processing": None}]),    # null
        _resp(200, [{"id": 0, "is_processing": "true"}]),  # 型違い
        _resp(200, ["oops"]),                              # 要素が dict でない
    ],
)
def test_unprovable_state_is_unknown_and_warns_once(resp, caplog):
    mgr = LlamaServerManager()
    managed = _managed()
    with patch("llm_clients.llama_server.httpx.get", return_value=resp):
        with caplog.at_level("WARNING"):
            assert mgr._probe_slots(PORT, managed) == "unknown"
            assert mgr._probe_slots(PORT, managed) == "unknown"
    warnings = [r for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 1


def test_warning_repeats_after_interval(caplog):
    """unknown のまま放置された管理下サーバーが無言化しない (定期再警告)。"""
    mgr = LlamaServerManager()
    managed = _managed()
    with patch("llm_clients.llama_server.httpx.get", return_value=_resp(501)):
        with caplog.at_level("WARNING"):
            mgr._probe_slots(PORT, managed)
            mgr._slots_warned[(ENDPOINT, managed.generation)] -= 1801.0
            mgr._probe_slots(PORT, managed)
    warnings = [r for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 2


def test_warning_not_suppressed_across_generations(caplog):
    """旧世代の warn 記録が新世代 (再起動後) の警告を抑止しない。"""
    mgr = LlamaServerManager()
    # 両方を変数に束ねて同時に生存させる。一時オブジェクトのまま続けて渡すと
    # 二つが同じアドレスに載ることがあり、テストの成否が割り当ての運で決まる
    old_gen, new_gen = _managed(), _managed()
    with patch("llm_clients.llama_server.httpx.get", return_value=_resp(501)):
        with caplog.at_level("WARNING"):
            mgr._probe_slots(PORT, old_gen)
            mgr._probe_slots(PORT, new_gen)
    warnings = [r for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 2


def test_generation_numbers_never_repeat():
    """世代番号は使い回されない。

    抑止表はサーバーオブジェクトより長生きするので、鍵は「回収されても
    次の個体に渡らない値」でなければならない。
    """
    assert len({_managed().generation for _ in range(50)}) == 50


def test_warning_not_suppressed_when_previous_generation_reused_address(caplog):
    """回収された旧世代と同じアドレスに載っても、新世代は警告する。

    鍵に id() を使っていた頃の実害: 一時オブジェクトが解放されると次の
    ManagedServer が同じアドレスに載り、旧世代の「警告済み」記録がそのまま
    新世代の鍵になって警告が消えた。pytest 並列実行で先行テストが変わると
    割り当て履歴が変わるため、稀に落ちる形で表面化した (2026-08-16)。
    """
    mgr = LlamaServerManager()
    managed = _managed()
    # 旧世代がこのアドレスに載ったまま警告済みの記録を残して回収された状況
    mgr._slots_warned[(ENDPOINT, id(managed))] = time.monotonic()
    with patch("llm_clients.llama_server.httpx.get", return_value=_resp(501)):
        with caplog.at_level("WARNING"):
            assert mgr._probe_slots(PORT, managed) == "unknown"
    warnings = [r for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 1


def test_connection_error_with_live_process_is_unknown_and_warns(caplog):
    mgr = LlamaServerManager()
    with patch(
        "llm_clients.llama_server.httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        with caplog.at_level("WARNING"):
            assert mgr._probe_slots(PORT, _managed(alive=True)) == "unknown"
    warnings = [r for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 契約3: プロセス死亡時だけ接続不能でも "idle" (警告なし)
# ---------------------------------------------------------------------------

def test_connection_error_with_dead_process_is_idle_without_warning(caplog):
    mgr = LlamaServerManager()
    with patch(
        "llm_clients.llama_server.httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        with caplog.at_level("WARNING"):
            assert mgr._probe_slots(PORT, _managed(alive=False)) == "idle"
    assert not [r for r in caplog.records if "busy 判定不能" in r.getMessage()]


# ---------------------------------------------------------------------------
# 契約4: 停止条件の再検証 (_finalize_stop)
# ---------------------------------------------------------------------------

def test_finalize_stop_stops_idle_server():
    mgr = LlamaServerManager()
    observed = _register(mgr, alive=False, idle_for=700.0)
    with patch.object(mgr, "_probe_slots", return_value="idle"):
        mgr._finalize_stop(ENDPOINT, observed, "idle")
    assert ENDPOINT not in mgr._servers


def test_finalize_stop_aborts_when_reprobe_flips_to_busy():
    """idle 観測 → kill の間に推論が始まったら、直前の再確認で止める。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    with patch.object(mgr, "_probe_slots", return_value="busy"):
        mgr._finalize_stop(ENDPOINT, observed, "idle")
    assert ENDPOINT in mgr._servers


def test_finalize_stop_aborts_when_activity_resumed():
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    observed.last_activity = time.monotonic()  # 新しい利用が始まった
    mgr._finalize_stop(ENDPOINT, observed, "idle")
    assert ENDPOINT in mgr._servers


def test_finalize_stop_aborts_when_server_generation_changed():
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    replacement = _register(mgr, idle_for=700.0)  # 同ポートを新世代で上書き
    assert observed is not replacement
    mgr._finalize_stop(ENDPOINT, observed, "idle")
    assert mgr._servers[ENDPOINT] is replacement


def test_finalize_stop_never_stops_unknown_even_past_deadline():
    """証明不能は busy_deadline を超えても止めない (証明の無い kill をしない)。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=4000.0)  # busy_deadline=3600 を超過
    mgr._finalize_stop(ENDPOINT, observed, "unknown")
    assert ENDPOINT in mgr._servers


def test_finalize_stop_ignores_unexpected_state():
    """三値以外の値 (タイポ等) が停止根拠に化けない。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=4000.0)
    mgr._finalize_stop(ENDPOINT, observed, "idlle")
    assert ENDPOINT in mgr._servers


def test_finalize_stop_busy_observation_refreshes_activity_clock():
    """busy の目撃は「利用」— 時計を巻き直し、長時間推論の完了直後に
    開始時刻基準の idle 停止が発火しない。busy_deadline の起算は別時計。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    before = observed.last_activity
    mgr._finalize_stop(ENDPOINT, observed, "busy")
    assert ENDPOINT in mgr._servers
    assert observed.last_activity > before
    assert observed.busy_since is not None


def test_finalize_stop_forces_busy_server_past_deadline():
    mgr = LlamaServerManager()
    observed = _register(mgr, alive=False, idle_for=700.0)
    observed.busy_since = time.monotonic() - 4000.0  # busy_deadline=3600 超過
    with patch.object(mgr, "_probe_slots", return_value="busy"):
        mgr._finalize_stop(ENDPOINT, observed, "busy")
    assert ENDPOINT not in mgr._servers


def test_busy_deadline_fires_even_with_lease_held():
    """ハング回収の非常弁 (busy_deadline) は貸出札で塞がれない。

    リクエストがハングしたまま札を握り続けても、処理中と証明された上で
    期限を超えたサーバーは強制停止できる。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, alive=False, idle_for=700.0)
    observed.busy_since = time.monotonic() - 4000.0
    mgr._inflight[ENDPOINT] = 1  # ハングしたリクエストが札を保持
    with patch.object(mgr, "_probe_slots", return_value="busy"):
        mgr._finalize_stop(ENDPOINT, observed, "busy")
    assert ENDPOINT not in mgr._servers


def test_busy_deadline_aborts_when_reprobe_shows_completion():
    """期限超過でも kill 直前の再確認で busy を証明できなければ撃たない
    (完了・KV 保存直後のサーバーを古い観測で殺さない)。idle と分かったら
    古い busy 時計も消す — 次の推論が引き継いで即時強制停止されないため。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    observed.busy_since = time.monotonic() - 4000.0
    with patch.object(mgr, "_probe_slots", return_value="idle"):
        mgr._finalize_stop(ENDPOINT, observed, "busy")
    assert ENDPOINT in mgr._servers
    assert observed.busy_since is None


def test_busy_deadline_keeps_clock_when_reprobe_unknown():
    """再確認が unknown なら撃たないが、busy 時計は維持する
    (証明不能で時計を消すとハング回収が無効化されるため)。"""
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    before = time.monotonic() - 4000.0
    observed.busy_since = before
    with patch.object(mgr, "_probe_slots", return_value="unknown"):
        mgr._finalize_stop(ENDPOINT, observed, "busy")
    assert ENDPOINT in mgr._servers
    assert observed.busy_since == before


def test_finalize_stop_idle_observation_clears_busy_since():
    mgr = LlamaServerManager()
    observed = _register(mgr, idle_for=700.0)
    observed.busy_since = time.monotonic() - 100.0
    # 再確認で busy に転じたケースでも busy_since はクリア済みであること
    with patch.object(mgr, "_probe_slots", return_value="busy"):
        mgr._finalize_stop(ENDPOINT, observed, "idle")
    assert observed.busy_since is None
    assert ENDPOINT in mgr._servers


def test_request_lease_blocks_stop_and_release_refreshes_clock(tmp_path):
    """貸出札が出ている間は idle 停止できず、返却が完了時刻の申告を兼ねる。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    managed = _register(mgr, alive=True, idle_for=700.0)
    managed.busy_since = time.monotonic() - 100.0
    before = managed.last_activity
    with mgr.request_lease(BASE_URL, _config(str(bat))):
        assert mgr._inflight[ENDPOINT] == 1
        # /slots が「暇」でも札がある間は撃てない (応答直後・KV save 中)
        with patch.object(mgr, "_probe_slots", return_value="idle"):
            mgr._finalize_stop(ENDPOINT, managed, "idle")
        assert ENDPOINT in mgr._servers
    assert mgr._inflight.get(ENDPOINT, 0) == 0
    assert managed.last_activity > before  # 返却 = 完了時刻からの idle 起算
    assert managed.busy_since is None


def test_request_lease_released_on_exception(tmp_path):
    """例外・ストリーム中断でも札は漏れない (finally 返却)。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    _register(mgr)
    with pytest.raises(RuntimeError):
        with mgr.request_lease(BASE_URL, _config(str(bat))):
            raise RuntimeError("stream interrupted")
    assert mgr._inflight.get(ENDPOINT, 0) == 0


def test_request_lease_protects_across_generation_swap(tmp_path):
    """札は接続先に付く — 貸出中に世代交代しても、新世代を守り続ける。

    ストリーム中にプロセスが死んで再起動された場合、旧世代に付けた札が
    置き去りになって新世代が無防備になる、という穴 (十二巡目) の封じ。
    """
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    _register(mgr, idle_for=700.0)
    with mgr.request_lease(BASE_URL, _config(str(bat))):
        replacement = _register(mgr, idle_for=700.0)  # 貸出中に世代交代
        with patch.object(mgr, "_probe_slots", return_value="idle"):
            mgr._finalize_stop(ENDPOINT, replacement, "idle")
        assert mgr._servers[ENDPOINT] is replacement  # 新世代も撃たれない


# ---------------------------------------------------------------------------
# 契約5: ensure_running の高速経路 / 低速経路
# ---------------------------------------------------------------------------

def _config(bat_path: str):
    return {
        "base_url": BASE_URL,
        "llama_server": {"command": bat_path},
    }


def test_ensure_running_fast_path_no_http_no_relaunch(tmp_path):
    """管理下プロセスが生存していれば HTTP を打たず activity 申告のみ。

    /health の一時不調 (高負荷時のタイムアウト等) で生存中の 82GB 級サーバーを
    二重起動しないための核心。
    """
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    config = _config(str(bat))
    identity = mgr._desired_identity(config["llama_server"])
    managed = _register(mgr, identity=identity, idle_for=500.0)
    before = managed.last_activity
    with (
        patch("llm_clients.llama_server.httpx.get") as get,
        patch.object(mgr, "_launch") as launch,
    ):
        mgr.ensure_running(BASE_URL, config)
    get.assert_not_called()
    launch.assert_not_called()
    assert managed.last_activity > before


def test_ensure_running_relaunches_dead_server(tmp_path):
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    config = _config(str(bat))
    identity = mgr._desired_identity(config["llama_server"])
    _register(mgr, identity=identity, alive=False)
    with (
        patch.object(mgr, "_health_check", return_value=False),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(BASE_URL, config)
    launch.assert_called_once()


def test_ensure_running_does_not_launch_over_external_port_reuse(tmp_path, caplog):
    """管理下プロセスは死亡・ポートは応答 = 外部再利用。起動して衝突しない。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    config = _config(str(bat))
    identity = mgr._desired_identity(config["llama_server"])
    _register(mgr, identity=identity, alive=False)
    with (
        patch.object(mgr, "_health_check", return_value=True),
        patch.object(mgr, "_launch") as launch,
    ):
        with caplog.at_level("WARNING"):
            mgr.ensure_running(BASE_URL, config)
    launch.assert_not_called()
    assert ENDPOINT not in mgr._servers
    assert [r for r in caplog.records if "外部プロセスが再利用中" in r.getMessage()]


def test_ensure_running_skips_recheck_of_known_external(tmp_path):
    """外部管理サーバーは間隔内なら /health を再確認しない (毎リクエスト直列化の防止)。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    mgr._external_ok[("127.0.0.1", PORT)] = time.monotonic()
    with patch("llm_clients.llama_server.httpx.get") as get:
        mgr.ensure_running(BASE_URL, _config(str(bat)))
    get.assert_not_called()


def test_ensure_running_stale_negative_probe_does_not_launch_over_external(tmp_path):
    """自分の unhealthy 観測より後の外部確認を信じる (順序判定、TTL 非依存)。

    並行する ensure_running の一方が /health=200 で外部確認を記録した後、
    他方の古い一時失敗の観測が外部サーバーの上に起動してはならない。
    ロック待ちが長引いて確認が TTL (30秒) より古くなっても、観測の
    新旧関係は変わらないので結論も変わらない。
    """
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")

    def health_false_then_record(base):
        # このスレッドの観測は False。ロック取得前に別スレッドが外部確認を
        # 記録し、さらにロック待ちで確認が TTL より古くなった、という並び
        mgr._external_ok[("127.0.0.1", PORT)] = time.monotonic() + 0.001
        return False

    with (
        patch.object(mgr, "_health_check", side_effect=health_false_then_record),
        patch.object(mgr, "_launch") as launch,
    ):
        mgr.ensure_running(BASE_URL, _config(str(bat)))
    launch.assert_not_called()


def test_ensure_running_newer_negative_probe_launches(tmp_path):
    """外部確認より自分の unhealthy 観測が新しければ、起動してよい
    (外部サーバーが消えた後の正当な引き継ぎを塞がない)。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    # 古い外部確認 (TTL も切れている)
    mgr._external_ok[("127.0.0.1", PORT)] = time.monotonic() - 100.0
    with (
        patch.object(mgr, "_health_check", return_value=False),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(BASE_URL, _config(str(bat)))
    launch.assert_called_once()


def test_ensure_running_host_mismatch_is_not_fast_path(tmp_path):
    """同じポートでも別 host は高速経路で成功扱いにせず、既存台帳も置換しない。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    config = _config(str(bat))
    identity = mgr._desired_identity(config["llama_server"])
    _register(mgr, identity=identity, host="192.168.1.20")  # 別 host で管理中
    with (
        patch.object(mgr, "_health_check", return_value=False),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
        patch.object(mgr, "_stop_server") as stop,
    ):
        mgr.ensure_running(BASE_URL, config)  # 127.0.0.1 向けの要求
    stop.assert_not_called()  # 別 host の管理サーバーには触れない
    launch.assert_called_once()


def test_ensure_running_host_mismatch_with_healthy_endpoint_does_not_destroy(tmp_path, caplog):
    """応答する外部 endpoint は、別 host の管理台帳や activity に触れない。"""
    mgr = LlamaServerManager()
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    config = _config(str(bat))
    identity = mgr._desired_identity(config["llama_server"])
    managed = _register(mgr, identity=identity, host="192.168.1.20", idle_for=700.0)
    before = managed.last_activity
    with (
        patch.object(mgr, "_health_check", return_value=True),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_stop_server") as stop,
    ):
        with caplog.at_level("WARNING"):
            mgr.ensure_running(BASE_URL, config)
    stop.assert_not_called()
    launch.assert_not_called()
    assert mgr._servers[(managed.host, PORT)] is managed
    assert managed.last_activity == before
    assert ENDPOINT in mgr._external_ok
    assert not [r for r in caplog.records if "多 host 構成はサポート外" in r.getMessage()]


# ---------------------------------------------------------------------------
# _launch の設定検証と warn 記録の掃除
# ---------------------------------------------------------------------------

def _launch_with(mgr, tmp_path, llama_cfg_extra=None, *, host="127.0.0.1", healthy=True):
    bat = tmp_path / "start_model.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    llama_cfg = {"command": str(bat), **(llama_cfg_extra or {})}
    with (
        patch("llm_clients.llama_server.subprocess.Popen", return_value=_proc()),
        patch.object(LlamaServerManager, "_wait_for_health", return_value=healthy),
        patch.object(LlamaServerManager, "_open_log_file", return_value=None),
    ):
        mgr._launch(host, PORT, {}, llama_cfg, "identity")


def test_launch_clears_warned_records_for_endpoint(tmp_path):
    mgr = LlamaServerManager()
    mgr._slots_warned[(ENDPOINT, 111)] = time.monotonic()
    mgr._slots_warned[(("127.0.0.1", 9999), 222)] = time.monotonic()
    _launch_with(mgr, tmp_path)
    assert not [k for k in mgr._slots_warned if k[0] == ENDPOINT]
    assert (("127.0.0.1", 9999), 222) in mgr._slots_warned
    assert mgr._servers[ENDPOINT].host == "127.0.0.1"


def test_launch_clamps_busy_deadline_below_idle_timeout(tmp_path, caplog):
    """busy_deadline < idle_timeout は実現不能なので丸めて知らせる。"""
    mgr = LlamaServerManager()
    with caplog.at_level("WARNING"):
        _launch_with(mgr, tmp_path, {"idle_timeout": 3600, "busy_deadline": 60})
    assert mgr._servers[ENDPOINT].busy_deadline == 3600
    assert [r for r in caplog.records if "丸める" in r.getMessage()]


# ---------------------------------------------------------------------------
# キャッシュ済みクライアントの自動復帰 (リクエスト前 ensure_backend)
# ---------------------------------------------------------------------------

def test_openai_client_ensures_server_before_each_request():
    """bind されたクライアントは送信前に毎回 ensure_running を通す。"""
    from llm_clients.openai import OpenAIClient

    client = OpenAIClient("test-model", api_key="dummy", base_url=BASE_URL)
    config = {"llama_server": {"command": "x.bat"}}
    client.bind_llama_server(BASE_URL, config)

    calls = []
    fake_mgr = SimpleNamespace(
        ensure_running=lambda base, cfg: calls.append((base, cfg))
    )
    from contextlib import contextmanager

    @contextmanager
    def fake_lease(base, cfg):
        calls.append(("lease_enter", base))
        yield
        calls.append(("lease_exit", base))

    fake_mgr.request_lease = fake_lease
    with (
        patch("llm_clients.llama_server.get_server_manager", return_value=fake_mgr),
        patch.object(client, "client") as sdk,
    ):
        client._create_completion(model="test-model", messages=[])
        client._create_completion(model="test-model", messages=[])
    # 毎リクエスト: 送信前の ensure + 送信を覆う貸出札
    ensures = [c for c in calls if c[0] not in ("lease_enter", "lease_exit")]
    leases = [c for c in calls if c[0] == "lease_enter"]
    assert len(ensures) == 2 and len(leases) == 2
    assert ensures[0] == (BASE_URL, config)
    assert sdk.chat.completions.create.call_count == 2


def test_openai_client_stream_holds_lease_until_completion():
    """ストリームは ensure → 札取得の順で始まり、完走まで札を保持する。

    順序が逆だと、idle 停止済みサーバーの再起動前に札が空発行され、
    実ストリームが無防備になる。"""
    from contextlib import contextmanager
    from llm_clients.openai import OpenAIClient

    client = OpenAIClient("test-model", api_key="dummy", base_url=BASE_URL)
    events = []

    @contextmanager
    def fake_lease():
        events.append("enter")
        yield
        events.append("exit")

    def impl(*a, **k):
        yield "a"
        events.append("mid")
        yield "b"

    with (
        patch.object(client, "ensure_backend", lambda: events.append("ensure")),
        patch.object(client, "backend_lease", fake_lease),
        patch.object(client, "_generate_stream_impl", impl),
    ):
        chunks = list(client.generate_stream([]))
    assert chunks == ["a", "b"]
    # ensure が札より先、札は生成の間ずっと保持
    assert events == ["ensure", "enter", "mid", "exit"]


def test_openai_client_stream_releases_lease_on_abandon():
    """途中で捨てられたストリームでも札は返却される (漏れない)。"""
    from contextlib import contextmanager
    from llm_clients.openai import OpenAIClient

    client = OpenAIClient("test-model", api_key="dummy", base_url=BASE_URL)
    events = []

    @contextmanager
    def fake_lease():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    with (
        patch.object(client, "backend_lease", fake_lease),
        patch.object(client, "_generate_stream_impl", return_value=iter(["a", "b", "c"])),
    ):
        stream = client.generate_stream([])
        next(stream)
        stream.close()  # 消費を中断
    assert events == ["enter", "exit"]


def test_openai_client_without_binding_skips_ensure():
    from llm_clients.openai import OpenAIClient

    client = OpenAIClient("test-model", api_key="dummy", base_url=BASE_URL)
    with (
        patch("llm_clients.llama_server.get_server_manager") as get_mgr,
        patch.object(client, "client"),
    ):
        client._create_completion(model="test-model", messages=[])
    get_mgr.assert_not_called()


def test_llama_cached_client_lease_covers_restore_through_save():
    """ensure は restore より先、貸出札は restore〜save 全体を覆う。

    /slots は save 中も「暇」を返すため、札が save 完了前に返却されると
    保存中のサーバーを idle 停止が撃てる。"""
    from contextlib import contextmanager
    from llm_clients.llama_cache import LlamaCachedClient

    order = []

    class FakeInner:
        supports_images = False
        config_key = ""

        def ensure_backend(self):
            order.append("ensure")

        @contextmanager
        def backend_lease(self):
            order.append("lease_enter")
            try:
                yield
            finally:
                order.append("lease_exit")

        def generate(self, *a, **k):
            order.append("generate")
            return "ok"

        def configure_parameters(self, p):
            pass

    cache = SimpleNamespace(
        acquire_slot=lambda: order.append("acquire") or 0,
        restore=lambda slot, pid: order.append("restore"),
        save=lambda slot, pid: order.append("save"),
        release_slot=lambda slot: order.append("release"),
    )
    client = LlamaCachedClient(FakeInner(), cache)
    assert client.generate([]) == "ok"
    assert order.index("ensure") < order.index("restore")
    assert order.index("lease_enter") < order.index("restore")
    assert order.index("save") < order.index("lease_exit")

# 同じポート番号でも、別 host のリクエストはローカルの寿命に触れない。
def test_remote_lease_does_not_refresh_local_activity():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    local.busy_since = time.monotonic() - 100.0
    before = (local.last_activity, local.busy_since)
    with mgr.request_lease(OTHER_URL, _config("unused.sh")):
        pass
    assert (local.last_activity, local.busy_since) == before


def test_healthy_remote_does_not_refresh_local_activity():
    mgr = LlamaServerManager()
    config = _config("unused.sh")
    identity = mgr._desired_identity(config["llama_server"])
    local = _register(mgr, identity=identity, idle_for=700.0)
    before = local.last_activity
    with (
        patch.object(mgr, "_health_check", return_value=True),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_stop_server") as stop,
    ):
        mgr.ensure_running(OTHER_URL, config)
    launch.assert_not_called()
    stop.assert_not_called()
    assert local.last_activity == before


def test_fast_path_only_refreshes_matching_host():
    mgr = LlamaServerManager()
    config = _config("unused.sh")
    identity = mgr._desired_identity(config["llama_server"])
    local = _register(mgr, identity=identity, idle_for=700.0)
    other = _register(mgr, identity=identity, host="nebula", idle_for=700.0)
    before_local, before_other = local.last_activity, other.last_activity
    with patch.object(mgr, "_health_check") as health:
        mgr.ensure_running(OTHER_URL, config)
    health.assert_not_called()
    assert mgr._servers == {ENDPOINT: local, OTHER_ENDPOINT: other}
    assert local.last_activity == before_local
    assert other.last_activity > before_other


def test_external_recheck_cache_is_independent_of_managed_same_port():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    before = local.last_activity
    with patch.object(mgr, "_health_check", return_value=True) as health:
        mgr.ensure_running(OTHER_URL, _config("unused.sh"))
        mgr.ensure_running(OTHER_URL, _config("unused.sh"))
    health.assert_called_once_with(f"http://nebula:{PORT}")
    assert mgr._servers == {ENDPOINT: local}
    assert set(mgr._external_ok) == {OTHER_ENDPOINT}
    assert local.last_activity == before


def test_other_host_external_cache_does_not_suppress_local_launch():
    mgr = LlamaServerManager()
    config = _config("unused.sh")
    mgr._external_ok[OTHER_ENDPOINT] = time.monotonic()
    with (
        patch.object(mgr, "_health_check", return_value=False),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(BASE_URL, config)
    launch.assert_called_once_with(*ENDPOINT, config, config["llama_server"], mgr._desired_identity(config["llama_server"]))
    assert OTHER_ENDPOINT in mgr._external_ok


@pytest.mark.parametrize("healthy", [False, True])
def test_config_change_only_replaces_matching_endpoint(healthy):
    mgr = LlamaServerManager()
    config = _config("new_model.sh")
    local = _register(mgr, identity="old-model")
    other = _register(mgr, host="nebula", identity="other-model", idle_for=700.0)
    before = other.last_activity
    with (
        patch.object(mgr, "_health_check", return_value=healthy),
        patch.object(mgr, "_kill_process_tree") as kill,
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(BASE_URL, config)
    kill.assert_called_once_with(local.process)
    launch.assert_called_once_with(*ENDPOINT, config, config["llama_server"], mgr._desired_identity(config["llama_server"]))
    assert mgr._servers == {OTHER_ENDPOINT: other}
    assert other.last_activity == before


@pytest.mark.parametrize("same_identity", [False, True])
def test_dead_endpoint_external_reuse_preserves_other_host(same_identity):
    mgr = LlamaServerManager()
    config = _config("unused.sh")
    identity = mgr._desired_identity(config["llama_server"])
    _register(mgr, alive=False, identity=identity if same_identity else "old-model")
    other = _register(mgr, host="nebula", idle_for=700.0)
    before = other.last_activity
    with (
        patch.object(mgr, "_health_check", return_value=True),
        patch.object(mgr, "_launch") as launch,
    ):
        mgr.ensure_running(BASE_URL, config)
    launch.assert_not_called()
    assert mgr._servers == {OTHER_ENDPOINT: other}
    assert set(mgr._external_ok) == {ENDPOINT}
    assert other.last_activity == before


def test_nested_leases_and_exception_are_counted_per_endpoint():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    other = _register(mgr, host="nebula", idle_for=700.0)
    before_other = other.last_activity
    config = _config("unused.sh")
    with mgr.request_lease(BASE_URL, config):
        with mgr.request_lease(OTHER_URL, config):
            with pytest.raises(RuntimeError, match="interrupted"):
                with mgr.request_lease(BASE_URL, config):
                    assert mgr._inflight == {ENDPOINT: 2, OTHER_ENDPOINT: 1}
                    raise RuntimeError("interrupted")
            assert mgr._inflight == {ENDPOINT: 1, OTHER_ENDPOINT: 1}
            assert other.last_activity == before_other
        assert mgr._inflight == {ENDPOINT: 1}
        assert other.last_activity > before_other
        before_other = other.last_activity
    assert mgr._inflight == {}
    assert other.last_activity == before_other
    assert local.busy_since is None


def test_other_host_lease_does_not_block_local_idle_stop():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    other = _register(mgr, host="nebula", idle_for=700.0)
    with mgr.request_lease(OTHER_URL, _config("unused.sh")):
        with (
            patch.object(mgr, "_probe_slots", return_value="idle"),
            patch.object(mgr, "_kill_process_tree") as kill,
        ):
            mgr._finalize_stop(OTHER_ENDPOINT, other, "idle")
            kill.assert_not_called()
            mgr._finalize_stop(ENDPOINT, local, "idle")
            kill.assert_called_once_with(local.process)
        assert mgr._servers == {OTHER_ENDPOINT: other}
    assert mgr._inflight == {}


def test_idle_checker_routes_same_port_candidates_by_endpoint():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    other = _register(mgr, host="nebula", idle_for=700.0)
    disabled = _register(mgr, host="disabled", idle_timeout=0, idle_for=700.0)
    with (
        patch.object(mgr._idle_checker_stop, "wait", side_effect=[False, True]),
        patch.object(mgr, "_probe_slots", return_value="idle"),
        patch.object(mgr, "_kill_process_tree") as kill,
    ):
        mgr._idle_check_loop()
    assert [call.args[0] for call in kill.call_args_list] == [local.process, other.process]
    assert mgr._servers == {("disabled", PORT): disabled}


def test_shutdown_stops_each_managed_endpoint_but_not_external():
    mgr = LlamaServerManager()
    local = _register(mgr)
    other = _register(mgr, host="nebula")
    mgr._external_ok[("external", PORT)] = time.monotonic()
    with patch.object(mgr, "_kill_process_tree") as kill:
        mgr.shutdown_all()
        mgr.shutdown_all()  # repeated shutdown is harmless
    assert [call.args[0] for call in kill.call_args_list] == [local.process, other.process]
    assert mgr._servers == {}
    assert mgr._idle_checker_stop.is_set()


def test_launch_preserves_same_port_other_host_state(tmp_path):
    mgr = LlamaServerManager()
    other = _register(mgr, host="nebula")
    now = time.monotonic()
    mgr._slots_warned[(ENDPOINT, 1)] = now
    mgr._slots_warned[(OTHER_ENDPOINT, other.generation)] = now
    mgr._external_ok = {ENDPOINT: now, OTHER_ENDPOINT: now}
    _launch_with(mgr, tmp_path)
    assert mgr._servers[ENDPOINT].host == ENDPOINT[0]
    assert mgr._servers[OTHER_ENDPOINT] is other
    assert mgr._slots_warned == {(OTHER_ENDPOINT, other.generation): now}
    assert mgr._external_ok == {OTHER_ENDPOINT: now}


def test_launch_failure_only_removes_failed_endpoint(tmp_path):
    mgr = LlamaServerManager()
    other = _register(mgr, host="nebula")
    with patch.object(mgr, "_kill_process_tree") as kill:
        with pytest.raises(RuntimeError, match="failed to become healthy"):
            _launch_with(mgr, tmp_path, healthy=False)
    kill.assert_called_once()
    assert kill.call_args.args[0] is not other.process
    assert mgr._servers == {OTHER_ENDPOINT: other}


def test_warning_records_include_host_and_generation(caplog):
    mgr = LlamaServerManager()
    local = _register(mgr)
    other = _register(mgr, host="nebula")
    with caplog.at_level("WARNING"):
        mgr._warn_slots_unavailable(PORT, local, "test")
        mgr._warn_slots_unavailable(PORT, other, "test")
        mgr._warn_slots_unavailable(PORT, local, "test")
    assert set(mgr._slots_warned) == {
        (ENDPOINT, local.generation), (OTHER_ENDPOINT, other.generation),
    }
    warnings = [r.getMessage() for r in caplog.records if "busy 判定不能" in r.getMessage()]
    assert len(warnings) == 2
    assert f"127.0.0.1:{PORT}" in warnings[0]
    assert f"nebula:{PORT}" in warnings[1]


@pytest.mark.parametrize("other_host", ["localhost", "::1", "0.0.0.0", "nebula"])
def test_distinct_host_names_are_not_merged(other_host):
    mgr = LlamaServerManager()
    host_in_url = f"[{other_host}]" if ":" in other_host else other_host
    other_url = f"http://{host_in_url}:{PORT}/v1"
    config = _config("unused.sh")
    with mgr.request_lease(BASE_URL, config), mgr.request_lease(other_url, config):
        assert mgr._inflight == {ENDPOINT: 1, (other_host, PORT): 1}
    assert mgr._inflight == {}


def test_no_server_config_skips_all_lifecycle_state():
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    before = local.last_activity
    with mgr.request_lease(BASE_URL, {}):
        mgr.ensure_running(BASE_URL, {})
        assert mgr._inflight == {}
    assert local.last_activity == before


def test_openai_client_remote_request_leases_only_its_endpoint():
    from llm_clients.openai import OpenAIClient

    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    local.busy_since = time.monotonic() - 100.0
    before = (local.last_activity, local.busy_since)
    client = OpenAIClient("test-model", api_key="dummy", base_url=OTHER_URL)
    client.bind_llama_server(OTHER_URL, _config("unused.sh"))

    def send(**kwargs):
        assert mgr._inflight == {OTHER_ENDPOINT: 1}
        return "synthetic response"

    with (
        patch("llm_clients.llama_server.get_server_manager", return_value=mgr),
        patch.object(mgr, "_health_check", return_value=True),
        patch.object(client, "client") as sdk,
    ):
        sdk.chat.completions.create.side_effect = send
        assert client._create_completion(model="test-model", messages=[]) == "synthetic response"
    assert (local.last_activity, local.busy_since) == before
    assert mgr._servers == {ENDPOINT: local}
    assert mgr._inflight == {}


def test_openai_stream_close_returns_only_matching_host_lease():
    from llm_clients.openai import OpenAIClient

    mgr = LlamaServerManager()
    config = _config("unused.sh")
    identity = mgr._desired_identity(config["llama_server"])
    local = _register(mgr, identity=identity, idle_for=700.0)
    other = _register(mgr, host="nebula", identity=identity, idle_for=700.0)
    before = local.last_activity
    client = OpenAIClient("test-model", api_key="dummy", base_url=OTHER_URL)
    client.bind_llama_server(OTHER_URL, config)
    with (
        patch("llm_clients.llama_server.get_server_manager", return_value=mgr),
        patch.object(client, "_generate_stream_impl", return_value=iter(["a", "b"])),
    ):
        stream = client.generate_stream([])
        assert next(stream) == "a"
        assert mgr._inflight == {OTHER_ENDPOINT: 1}
        before_release = other.last_activity
        stream.close()
    assert mgr._inflight == {}
    assert local.last_activity == before
    assert other.last_activity >= before_release


def test_failed_other_host_probe_keeps_existing_local_process():
    """既存の設定起動経路は変えないが、同ポートの別 host は停止しない。"""
    mgr = LlamaServerManager()
    local = _register(mgr, idle_for=700.0)
    before = local.last_activity
    config = _config("unused.sh")
    with (
        patch.object(mgr, "_health_check", return_value=False),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(OTHER_URL, config)
    launch.assert_called_once_with(*OTHER_ENDPOINT, config, config["llama_server"], mgr._desired_identity(config["llama_server"]))
    assert mgr._servers == {ENDPOINT: local}
    assert local.last_activity == before


def test_url_variants_share_only_the_same_host_and_port():
    mgr = LlamaServerManager()
    config = _config("unused.sh")
    with mgr.request_lease(f"http://NEBULA:{PORT}/v1", config):
        with mgr.request_lease(f"http://nebula:{PORT}/another-path", config):
            with mgr.request_lease(f"http://nebula:{PORT + 1}/v1", config):
                assert mgr._inflight == {OTHER_ENDPOINT: 2, ("nebula", PORT + 1): 1}
    assert mgr._inflight == {}


def test_stale_probe_cannot_use_other_host_external_success():
    mgr = LlamaServerManager()
    config = _config("unused.sh")

    def observe_another_host(base):
        mgr._external_ok[OTHER_ENDPOINT] = time.monotonic() + 0.001
        return False

    with (
        patch.object(mgr, "_health_check", side_effect=observe_another_host),
        patch.object(mgr, "_launch") as launch,
        patch.object(mgr, "_ensure_idle_checker"),
    ):
        mgr.ensure_running(BASE_URL, config)
    launch.assert_called_once()
    assert launch.call_args.args[:2] == ENDPOINT
