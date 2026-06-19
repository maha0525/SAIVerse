"""Auto-launch and lifecycle management for llama.cpp server processes."""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import threading
import time
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


@dataclass
class ManagedServer:
    process: subprocess.Popen
    identity: str
    port: int
    config: Dict[str, Any] = field(repr=False)


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

    def ensure_running(self, base_url: str, config: Dict[str, Any]) -> None:
        llama_cfg = config.get("llama_server")
        if not isinstance(llama_cfg, dict):
            return

        host, port = self._parse_host_port(base_url)
        health_base = f"http://{host}:{port}"

        desired_identity = self._desired_identity(llama_cfg)

        with self._lock:
            if self._health_check(health_base):
                managed = self._servers.get(port)
                if managed is None:
                    logger.info(
                        "[llama_server] Port %d already responding (externally managed), using as-is",
                        port,
                    )
                    return
                if managed.identity == desired_identity:
                    logger.debug("[llama_server] Port %d already serving correct config", port)
                    return
                logger.info(
                    "[llama_server] Port %d config changed (have=%s, want=%s), restarting",
                    port, managed.identity, desired_identity,
                )
                self._stop_server(port)

            self._launch(host, port, config, llama_cfg, desired_identity)

    def shutdown_all(self) -> None:
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
        logger.info("[llama_server] Launching: %s (cwd=%s)", " ".join(cmd), cwd or "<inherit>")

        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

        self._servers[port] = ManagedServer(
            process=process,
            identity=identity,
            port=port,
            config=llama_cfg,
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
            proc.terminate()
            try:
                proc.wait(timeout=_SHUTDOWN_WAIT)
            except subprocess.TimeoutExpired:
                logger.warning("[llama_server] Port %d did not exit in time; killing", port)
                proc.kill()
                proc.wait(timeout=5)
        except Exception as exc:
            logger.error("[llama_server] Failed to stop port %d cleanly: %s", port, exc)

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
