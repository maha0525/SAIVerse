"""Auto-launch and lifecycle management for llama.cpp server processes."""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_HEALTH_CHECK_TIMEOUT = 2.0
_HEALTH_CHECK_WAIT_MAX = 120.0
_HEALTH_CHECK_INTERVAL = 2.0
_SHUTDOWN_WAIT = 10
_DEFAULT_IDLE_TIMEOUT = 600
_DEFAULT_BUSY_DEADLINE = 3600
_IDLE_CHECK_INTERVAL = 30
#: unknown (証明不能) は自動停止しないため、リソースを握ったままの管理下
#: サーバーが無言にならないよう、警告をこの間隔で繰り返す
_WARN_REPEAT_INTERVAL = 1800.0
#: 外部管理サーバー (管理外プロセス) の /health 再確認間隔。毎リクエスト
#: 確認するとグローバルロック越しに全リクエストが直列化するため間引く
_EXTERNAL_RECHECK_INTERVAL = 30.0


@dataclass
class ManagedServer:
    process: subprocess.Popen
    identity: str
    port: int
    config: Dict[str, Any] = field(repr=False)
    host: str = "127.0.0.1"
    idle_timeout: float = _DEFAULT_IDLE_TIMEOUT
    busy_deadline: float = _DEFAULT_BUSY_DEADLINE
    last_activity: float = field(default_factory=time.monotonic)
    #: idle checker が最初に busy を観測した時刻。busy_deadline の起算点。
    #: idle 観測・リクエスト完了 (lease 返却) でクリアされる
    busy_since: Optional[float] = None


class LlamaServerManager:
    """Manages llama.cpp server processes (one per port).

    Two launch modes:
      - Command mode: ``llama_server.command`` points to a .bat/.sh script.
        The script is executed as-is; ``llama_server_binary``, ``model_path``,
        ``extra_args`` etc. are all ignored.
      - Build-up mode: individual fields (``model_path``, ``n_gpu_layers``,
        ``extra_args``, …) are assembled into a command line.
    """

    def __init__(self) -> None:
        self._servers: Dict[int, ManagedServer] = {}
        self._lock = threading.Lock()
        self._idle_checker: Optional[threading.Thread] = None
        self._idle_checker_stop = threading.Event()
        # _slots_warned ((port, サーバー世代id) → 最終警告時刻) は専用ロックで
        # 守る: /slots の問い合わせは self._lock 保持中にも走る (停止直前の
        # 再確認) ため、warn 経路が self._lock を取り直すと非再入ロックで
        # 自己デッドロックする。世代 id をキーに含めるのは、旧世代の probe が
        # 再起動後に新世代の警告を抑止しないため
        self._slots_warned: Dict[tuple[int, int], float] = {}
        self._warn_lock = threading.Lock()
        # 外部管理サーバーの直近 /health 成功時刻 ((host, port) → monotonic)
        self._external_ok: Dict[tuple[str, int], float] = {}
        # 貸出中のリクエスト数 (port → count)。ポート単位で持つ — リクエストは
        # ポートへ飛ぶので、途中で世代交代してもそのポートの現職を守り続ける
        self._inflight: Dict[int, int] = {}

    def ensure_running(self, base_url: str, config: Dict[str, Any]) -> None:
        """サーバーの存在を保証し、使用中であることを申告する。

        毎リクエスト前に呼ばれる (OpenAIClient._create_completion) ため、
        高速経路と低速経路を分ける:

        - **高速経路** (管理下プロセスが生存 + identity 一致): last_activity を
          更新するだけ。HTTP を打たず、ロック保持はマイクロ秒。/health の
          一時的な不調で生存中のサーバーを二重起動しないため、生存判定は
          プロセスの存在だけで行う (応答性の検証はリクエスト自身がする)
        - **低速経路** (管理下に無い / プロセス死亡 / identity 不一致): ロック外で
          /health を確認してから、ロック内で状態を再確認して起動・置換する。
          起動 (_launch) はロック保持のまま行う = ポート単位でなくグローバルな
          single-flight だが、同時に複数の巨大モデルを起動しないのはむしろ望む
          挙動 (RAM/VRAM 保護)
        """
        llama_cfg = config.get("llama_server")
        if not isinstance(llama_cfg, dict):
            return

        host, port = self._parse_host_port(base_url)
        health_base = f"http://{host}:{port}"
        desired_identity = self._desired_identity(llama_cfg)

        endpoint = (host, port)
        with self._lock:
            managed = self._servers.get(port)
            if (
                managed is not None
                and managed.identity == desired_identity
                and managed.host == host
                and managed.process.poll() is None
            ):
                managed.last_activity = time.monotonic()
                return
            if managed is None:
                last_ok = self._external_ok.get(endpoint)
                if (
                    last_ok is not None
                    and time.monotonic() - last_ok < _EXTERNAL_RECHECK_INTERVAL
                ):
                    return  # 外部管理サーバー、直近に確認済み

        # ---- 低速経路: ロック外で健康確認 (HTTP 待ちで他スレッドを塞がない) ----
        probe_started = time.monotonic()
        healthy = self._health_check(health_base)
        # 観測時刻は probe 完了直後に確定する。ロック取得やログ出力の後の
        # 時刻を使うと、観測の実順序と記録の順序がずれる
        probe_finished = time.monotonic()

        with self._lock:
            managed = self._servers.get(port)
            # 待っている間に他スレッドが処理した可能性があるので再確認
            if (
                managed is not None
                and managed.identity == desired_identity
                and managed.host == host
                and managed.process.poll() is None
            ):
                managed.last_activity = time.monotonic()
                return

            if managed is None:
                if healthy:
                    logger.info(
                        "[llama_server] Port %d already responding (externally managed), using as-is",
                        port,
                    )
                    self._external_ok[endpoint] = probe_finished
                    return
                # 自分の unhealthy 観測より**後に**別スレッドが外部サーバーを
                # 確認していたら、そちらの新しい情報を信じて起動しない。
                # 判定は時刻の窓 (TTL) でなく観測の順序 — ロック待ちで
                # 自分の観測が古びても、新旧関係は逆転しない
                last_ok = self._external_ok.get(endpoint)
                if last_ok is not None and last_ok >= probe_started:
                    return
                self._external_ok.pop(endpoint, None)
            elif managed.identity != desired_identity or managed.host != host:
                if managed.process.poll() is not None and healthy:
                    # 旧プロセスは死んでいてポートは別物が応答 = 外部再利用。
                    # ここで起動するとポート衝突する
                    self._servers.pop(port, None)
                    logger.warning(
                        "[llama_server] Port %d: 管理下プロセスは死んでいるがポートは応答 — "
                        "外部プロセスが再利用中とみなし、起動しない",
                        port,
                    )
                    self._external_ok[endpoint] = probe_finished
                    return
                if managed.host != host and healthy:
                    # 同一ポートを別 host で使い分ける構成はサポート外。
                    # ターゲット endpoint は応答しており、それが管理下プロセス
                    # (別 host に bind) か外部かは証明できない — 壊さず・
                    # 上書き起動もせず、応答している現状を使う。応答者が
                    # 管理下プロセス (wildcard bind や host 別名) の可能性が
                    # あるため、活動時刻も更新して idle 停止の誤射を防ぐ
                    managed.last_activity = time.monotonic()
                    logger.warning(
                        "[llama_server] Port %d: 管理下は %s、要求は %s で endpoint は応答中 — "
                        "同一ポートの多 host 構成はサポート外のため、上書きせず現状を使う",
                        port, managed.host, host,
                    )
                    self._external_ok[endpoint] = probe_finished
                    return
                logger.info(
                    "[llama_server] Port %d config changed (have=%s@%s, want=%s@%s), restarting",
                    port, managed.identity, managed.host, desired_identity, host,
                )
                self._stop_server(port)
            else:
                # identity・host 一致だがプロセス死亡。ポートが応答するなら
                # 外部が再利用している = 起動すると衝突するので手を出さない
                self._servers.pop(port, None)
                if healthy:
                    logger.warning(
                        "[llama_server] Port %d: 管理下プロセスは死んでいるがポートは応答 — "
                        "外部プロセスが再利用中とみなし、起動しない",
                        port,
                    )
                    self._external_ok[endpoint] = probe_finished
                    return

            self._launch(host, port, config, llama_cfg, desired_identity)
            self._ensure_idle_checker()

    @contextmanager
    def request_lease(self, base_url: str, config: Dict[str, Any]):
        """リクエストの貸出札。札が出ている間、idle checker はこのサーバーを
        停止できない。

        /slots の「暇」観測だけでは、応答受信直後 (完了申告前) や KV slot
        save (数 GB のディスク書き込みで数秒かかる) の最中と区別できない。
        リクエスト送信〜後処理完了までを札で覆い、停止判定と原子的に排他する。
        返却は try/finally なので、例外・ストリーム中断でも漏れない。
        返却時に last_activity を更新する = 完了時刻からの idle 起算
        (idle_timeout 超の長時間推論の完了直後に停止が発火しない)。
        """
        llama_cfg = config.get("llama_server")
        if not isinstance(llama_cfg, dict):
            yield
            return
        _, port = self._parse_host_port(base_url)
        with self._lock:
            self._inflight[port] = self._inflight.get(port, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._inflight.get(port, 0) - 1
                if remaining <= 0:
                    self._inflight.pop(port, None)
                else:
                    self._inflight[port] = remaining
                current = self._servers.get(port)
                if current is not None and current.process.poll() is None:
                    current.last_activity = time.monotonic()
                    current.busy_since = None

    def shutdown_all(self) -> None:
        self._idle_checker_stop.set()
        if self._idle_checker is not None:
            self._idle_checker.join(timeout=5)
            self._idle_checker = None
        with self._lock:
            ports = list(self._servers.keys())
            for port in ports:
                self._stop_server(port)

    def _launch(
        self,
        host: str,
        port: int,
        config: Dict[str, Any],
        llama_cfg: Dict[str, Any],
        identity: str,
    ) -> None:
        command = llama_cfg.get("command")
        cwd: str | None = None
        if isinstance(command, str) and command.strip():
            cmd = self._build_command_mode(command.strip())
            cwd = str(Path(cmd[0]).parent)
        else:
            cmd = self._build_buildup_mode(host, port, config, llama_cfg)

        log_file = self._open_log_file(port)
        with self._warn_lock:
            # 再起動で /slots 可否は変わりうる。旧世代の警告記録ごと掃除する
            self._slots_warned = {
                k: v for k, v in self._slots_warned.items() if k[0] != port
            }
        self._external_ok.pop((host, port), None)
        logger.info("[llama_server] Launching: %s (cwd=%s)", " ".join(cmd), cwd or "<inherit>")

        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=flags,
        )

        idle_timeout = llama_cfg.get("idle_timeout")
        if not isinstance(idle_timeout, (int, float)) or idle_timeout < 0:
            idle_timeout = _DEFAULT_IDLE_TIMEOUT

        busy_deadline = llama_cfg.get("busy_deadline")
        if not isinstance(busy_deadline, (int, float)) or busy_deadline <= 0:
            busy_deadline = _DEFAULT_BUSY_DEADLINE
        if 0 < idle_timeout and busy_deadline < idle_timeout:
            # 停止候補になるのは idle_timeout 経過後なので、それより短い
            # busy_deadline は効かない。黙って遅延させず、丸めて知らせる
            logger.warning(
                "[llama_server] Port %d: busy_deadline (%.0fs) < idle_timeout (%.0fs) は"
                "実現できないため idle_timeout に丸める",
                port, float(busy_deadline), float(idle_timeout),
            )
            busy_deadline = idle_timeout

        self._servers[port] = ManagedServer(
            process=process,
            identity=identity,
            port=port,
            config=llama_cfg,
            host=host,
            idle_timeout=float(idle_timeout),
            busy_deadline=float(busy_deadline),
        )

        health_base = f"http://{host}:{port}"
        if not self._wait_for_health(health_base, process):
            self._stop_server(port)
            raise RuntimeError(
                f"llama-server failed to become healthy within {_HEALTH_CHECK_WAIT_MAX:.0f}s "
                f"(port={port})"
            )

        logger.info("[llama_server] Server ready on port %d (PID %d)", port, process.pid)

    @staticmethod
    def _build_command_mode(command: str) -> list[str]:
        command_path = LlamaServerManager._expand_path(command)
        if not Path(command_path).is_file():
            raise FileNotFoundError(f"Launch script not found: {command_path}")
        return [command_path]

    def _build_buildup_mode(
        self,
        host: str,
        port: int,
        config: Dict[str, Any],
        llama_cfg: Dict[str, Any],
    ) -> list[str]:
        binary = self._resolve_binary(config, llama_cfg)
        model_path = self._expand_model_path(llama_cfg.get("model_path", ""))
        if not model_path:
            raise ValueError("llama_server.model_path is required (or use llama_server.command)")
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"GGUF model not found: {model_path}")

        cmd = [binary, "--model", model_path, "--port", str(port)]

        if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
            cmd.extend(["--host", host])

        ctx = config.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            cmd.extend(["--ctx-size", str(ctx)])

        n_gpu = llama_cfg.get("n_gpu_layers")
        if isinstance(n_gpu, int):
            cmd.extend(["--n-gpu-layers", str(n_gpu)])

        parallel = llama_cfg.get("parallel")
        if isinstance(parallel, int) and parallel > 1:
            cmd.extend(["--parallel", str(parallel)])

        extra_args = llama_cfg.get("extra_args")
        if isinstance(extra_args, list):
            cmd.extend([str(a) for a in extra_args])

        return cmd

    def _ensure_idle_checker(self) -> None:
        if self._idle_checker is not None and self._idle_checker.is_alive():
            return
        self._idle_checker_stop.clear()
        t = threading.Thread(target=self._idle_check_loop, daemon=True, name="llama-idle-checker")
        self._idle_checker = t
        t.start()

    def _idle_check_loop(self) -> None:
        while not self._idle_checker_stop.wait(timeout=_IDLE_CHECK_INTERVAL):
            with self._lock:
                if not self._servers:
                    break
                now = time.monotonic()
                candidates: list[tuple[int, ManagedServer]] = [
                    (port, managed)
                    for port, managed in self._servers.items()
                    if managed.idle_timeout != 0
                    and now - managed.last_activity >= managed.idle_timeout
                ]

            for port, observed in candidates:
                # /slots への問い合わせはロック外 (HTTP 待ちで他スレッドを塞がない)
                state = self._probe_slots(port, observed)
                self._finalize_stop(port, observed, state)

    def _finalize_stop(self, port: int, observed: ManagedServer, state: str) -> None:
        """観測時と同じサーバーが、今なお停止条件を満たすときだけ止める。

        候補選定〜busy 判定の間はロックを解放しているので、その間に
        ensure_running が last_activity を更新したり、サーバーが世代交代して
        いる可能性がある。古い観測に基づく停止は「処理中の射殺」を再現する
        ため、最終ロック内で世代 (同一オブジェクト) と idle 条件を再確認する。

        state の三値: "idle" だけが停止根拠になる。"busy" (処理中と証明) は
        busy_deadline 超過でのみ強制停止。"unknown" (証明不能) は**決して
        止めない** — busy_deadline の強制停止にも使わない (証明の無い kill は
        今回防ごうとした事故そのもの)。
        """
        if state not in ("idle", "busy"):
            # "unknown" と未知の値はどちらも停止根拠にしない (fail-safe)。
            # unknown の warn は _probe_slots 側が出している
            if state != "unknown":
                logger.error(
                    "[llama_server] Port %d: 未知の状態 %r — 停止しない (fail-safe)",
                    port, state,
                )
            return
        with self._lock:
            managed = self._servers.get(port)
            if managed is not observed:
                return  # 再起動などで世代が変わった。この観測は無効
            idle_secs = time.monotonic() - managed.last_activity
            if idle_secs < managed.idle_timeout:
                return  # 判定の合間に新しい利用が始まった
            if state == "busy":
                # busy の目撃も「利用」— 時計を巻き直し、長時間推論の完了直後に
                # 開始時刻基準で idle 停止が発火するのを防ぐ。busy_deadline の
                # 起算は別時計 (最初に busy を目撃した時刻) で持つ
                now = time.monotonic()
                if managed.busy_since is None:
                    managed.busy_since = now
                managed.last_activity = now
                busy_secs = now - managed.busy_since
                if busy_secs >= managed.busy_deadline:
                    recheck = self._probe_slots(port, managed)
                    if recheck != "busy":
                        if recheck == "idle":
                            # 処理は終わっていた。古い busy 時計を残すと次の
                            # 推論が引き継いで即時強制停止されるため消す
                            managed.busy_since = None
                        return  # 観測が古い — 完了・保存直後のサーバーを撃たない
                    logger.warning(
                        "[llama_server] Port %d busy for %.0fs, exceeded deadline (%.0fs), forcing stop",
                        port, busy_secs, managed.busy_deadline,
                    )
                    self._stop_server(port)
                else:
                    logger.debug(
                        "[llama_server] Port %d still processing (%.0fs/%.0fs deadline)",
                        port, busy_secs, managed.busy_deadline,
                    )
                return
            managed.busy_since = None  # idle 観測 = 処理は終わっている
            if self._inflight.get(port, 0) > 0:
                # 貸出中のリクエストがある。/slots が暇に見えても応答直後や
                # KV save 中でありうる — idle 停止はしない。busy_deadline の
                # 強制停止 (上の busy 分岐) には札は効かない: ハング回収の
                # 非常弁を札で塞がないため
                return
            # state == "idle": kill 直前にロック内でもう一度 /slots を確認し、
            # 「idle 観測 → 停止」の間に始まった推論を撃つ窓を最小化する。
            # クライアントはマネージャを経由せず直接 HTTP を打つため、この窓を
            # ゼロにはできない (既知の限界、intent doc 参照)
            if self._probe_slots(port, managed) != "idle":
                return
            idle_secs = time.monotonic() - managed.last_activity
            if idle_secs < managed.idle_timeout:
                return
            logger.info(
                "[llama_server] Port %d idle for %.0fs (timeout=%.0fs), stopping",
                port, idle_secs, managed.idle_timeout,
            )
            self._stop_server(port)

    def _stop_server(self, port: int) -> None:
        managed = self._servers.pop(port, None)
        if managed is None:
            return
        proc = managed.process
        if proc.poll() is not None:
            logger.debug("[llama_server] Port %d process already exited (rc=%s)", port, proc.returncode)
            return
        logger.info("[llama_server] Stopping server on port %d (PID %d)", port, proc.pid)
        try:
            self._kill_process_tree(proc)
        except Exception as exc:
            logger.error("[llama_server] Failed to stop port %d cleanly: %s", port, exc)

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        pid = proc.pid
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
            try:
                proc.wait(timeout=_SHUTDOWN_WAIT)
            except subprocess.TimeoutExpired:
                logger.warning("[llama_server] PID %d still alive after taskkill /T", pid)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=_SHUTDOWN_WAIT)
            except subprocess.TimeoutExpired:
                logger.warning("[llama_server] PID %d did not exit in time; killing", pid)
                proc.kill()
                proc.wait(timeout=5)

    def _probe_slots(self, port: int, managed: ManagedServer) -> str:
        """サーバーの状態を三値で返す: "busy" / "idle" / "unknown"。

        /health は生存確認専用で、スロットが推論中でも 200 を返す（b10229 実測。
        2026-08-03、プロンプト処理 39% のサーバーを idle 判定で停止し、25 分級の
        応答を二度殺した）。忙しさは /slots（既定で有効、--no-slots で無効化可）の
        ``is_processing``（厳密な bool）で判定する。

        - "idle": 全スロットが is_processing=False と**証明**できた
        - "busy": 処理中スロットがあると証明できた
        - "unknown": 証明不能 (エンドポイント無効・応答が想定形でない・生存中
          なのに接続不能)。呼び出し側は unknown を停止根拠にしてはならない
        - 例外: プロセスが死んでいる場合だけは接続不能でも "idle" (掃除経路)

        self._lock 保持中にも呼ばれるため、この関数は self._lock を取らない
        (warn は専用の _warn_lock)。
        """
        try:
            resp = httpx.get(
                f"http://{managed.host}:{port}/slots",
                timeout=_HEALTH_CHECK_TIMEOUT,
            )
        except Exception as exc:
            if managed.process.poll() is None:
                # 生きているのに読めない = 証明不能。無言で残り続けるのを
                # 避けるため、fail-safe に倒れている事実を warn しておく
                self._warn_slots_unavailable(
                    port, managed, f"接続失敗: {type(exc).__name__}"
                )
                return "unknown"
            return "idle"  # プロセスが死んでいる。掃除経路を塞がない

        if resp.status_code != 200:
            self._warn_slots_unavailable(port, managed, f"HTTP {resp.status_code}")
            return "unknown"

        # 想定形: トップレベル配列、各要素に厳密な bool の is_processing。
        # 欠損・型違い・空配列は「暇の証明」にならない
        try:
            slots = resp.json()
            flags = (
                [slot.get("is_processing") for slot in slots]
                if isinstance(slots, list)
                else None
            )
        except Exception:
            flags = None
        if not flags or not all(isinstance(f, bool) for f in flags):
            self._warn_slots_unavailable(port, managed, "/slots の応答が想定形でない")
            return "unknown"
        return "busy" if any(flags) else "idle"

    def _warn_slots_unavailable(
        self, port: int, managed: ManagedServer, reason: str
    ) -> None:
        """busy 判定不能 = idle 自動停止が事実上無効になることを知らせる。

        毎チェック (30秒間隔) で吠えるとログが埋まるが、一度きりだと
        リソースを握ったままの管理下サーバーが無言になる。間を取って
        _WARN_REPEAT_INTERVAL ごとに繰り返す。キーにサーバー世代 (managed の
        オブジェクト id) を含め、旧世代の probe が新世代の警告を抑止しない。
        """
        now = time.monotonic()
        key = (port, id(managed))
        with self._warn_lock:
            last = self._slots_warned.get(key)
            if last is not None and now - last < _WARN_REPEAT_INTERVAL:
                return
            self._slots_warned[key] = now
        logger.warning(
            "[llama_server] Port %d: /slots が使えない (%s) ため busy 判定不能。"
            "このサーバーの idle 自動停止は働かない。--no-slots を外すと有効になる",
            port, reason,
        )

    def _health_check(self, base_url: str) -> bool:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=_HEALTH_CHECK_TIMEOUT)
            return resp.status_code == 200
        except Exception:
            return False

    def _wait_for_health(self, base_url: str, process: subprocess.Popen) -> bool:
        deadline = time.monotonic() + _HEALTH_CHECK_WAIT_MAX
        while time.monotonic() < deadline:
            if process.poll() is not None:
                logger.error(
                    "[llama_server] Process exited prematurely (rc=%s)", process.returncode
                )
                return False
            if self._health_check(base_url):
                return True
            time.sleep(_HEALTH_CHECK_INTERVAL)
        return False

    def _resolve_binary(
        self, config: Dict[str, Any], llama_cfg: Dict[str, Any]
    ) -> str:
        binary = llama_cfg.get("binary")
        if isinstance(binary, str) and binary.strip():
            return self._expand_path(binary.strip())

        binary = config.get("llama_server_binary")
        if isinstance(binary, str) and binary.strip():
            return self._expand_path(binary.strip())

        binary = os.environ.get("LLAMA_SERVER_BINARY")
        if binary:
            return self._expand_path(binary)

        found = shutil.which("llama-server")
        if found:
            return found

        raise FileNotFoundError(
            "llama-server binary not found. Set llama_server.binary in model config, "
            "llama_server_binary in provider config, LLAMA_SERVER_BINARY env var, "
            "or ensure llama-server is on PATH."
        )

    @staticmethod
    def _desired_identity(llama_cfg: Dict[str, Any]) -> str:
        command = llama_cfg.get("command")
        if isinstance(command, str) and command.strip():
            return f"command:{LlamaServerManager._expand_path(command.strip())}"
        model_path = llama_cfg.get("model_path", "")
        return f"model:{LlamaServerManager._expand_model_path(model_path)}"

    @staticmethod
    def _expand_path(p: str) -> str:
        return os.path.expandvars(os.path.expanduser(p))

    @staticmethod
    def _expand_model_path(p: str) -> str:
        if not p:
            return ""
        return str(Path(os.path.expandvars(os.path.expanduser(p))).resolve())

    @staticmethod
    def _parse_host_port(base_url: str) -> tuple[str, int]:
        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8080
        return host, port

    @staticmethod
    def _open_log_file(port: int):
        try:
            from saiverse.logging_config import get_session_log_dir
            log_dir = get_session_log_dir()
        except Exception:
            log_dir = Path.cwd()
        log_path = log_dir / f"llama_server_{port}.log"
        logger.info("[llama_server] Server log: %s", log_path)
        return open(log_path, "a", encoding="utf-8")


_manager: Optional[LlamaServerManager] = None
_manager_lock = threading.Lock()


def get_server_manager() -> LlamaServerManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = LlamaServerManager()
                atexit.register(_manager.shutdown_all)
    return _manager


__all__ = ["LlamaServerManager", "get_server_manager"]
