"""Tool imports leave log destinations and configuration to the application."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tool_loader import load_builtin_tool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_MODULES = ("calculator", "read_url_content", "send_email_to_user")

# A fresh interpreter matters: tools may already be cached by pytest collection,
# and a leaked FileHandler must not survive into another test or the test runner.
_IMPORT_PROBE = r'''
import json
import logging
import os
import sys
from pathlib import Path


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise AssertionError("Network access is forbidden in the import probe")


sys.addaudithook(deny_network)
legacy_log = Path(os.environ.get("SAIVERSE_LOG_PATH", "saiverse_log.txt"))

def snapshot():
    if not legacy_log.exists():
        return None
    return legacy_log.read_bytes(), legacy_log.stat().st_mtime_ns

before_import = snapshot()
from saiverse.logging_config import configure_logging

log_dir = configure_logging("DEBUG")
import tools

names = ("calculator", "read_url_content", "send_email_to_user")
if sys.argv[1] == "autodiscovery":
    # This is the real package-import discovery path, not a mocked loader.
    assert {"calculate_expression", "read_url_content", "send_email_to_user"} <= tools.TOOL_REGISTRY.keys()
    modules = [sys.modules[f"tools._loaded.{name}"] for name in names]
else:
    name = sys.argv[1]
    path = Path(sys.argv[2]) / "builtin_data" / "tools" / f"{name}.py"
    modules = [tools._load_module_from_path(f"tools._logging_test.{name}", path)]

assert snapshot() == before_import, "import changed the legacy log"
for module in modules:
    module.logger.debug("isolation-debug:%s", module.__name__)
    module.logger.info("isolation-info:%s", module.__name__)
logging.shutdown()
print(json.dumps({
    "backend_log": str(log_dir / "backend.log"),
    "loggers": [{
        "name": module.__name__,
        "handlers": len(module.logger.handlers),
        "propagate": module.logger.propagate,
        "level": module.logger.level,
    } for module in modules],
}))
'''


def _snapshot(path):
    if not path.exists():
        return None
    return path.read_bytes(), path.stat().st_mtime_ns


@pytest.mark.parametrize("target", (*TOOL_MODULES, "autodiscovery"))
@pytest.mark.parametrize("inherited_path", [True, False], ids=["inherited", "default"])
@pytest.mark.parametrize("existing", [True, False], ids=["existing", "missing"])
def test_imports_leave_legacy_logs_untouched(tmp_path, target, inherited_path, existing):
    work_dir = tmp_path / "cwd"
    work_dir.mkdir()
    legacy_log = tmp_path / "outside-isolated-home" / "log.txt"
    default_log = work_dir / "saiverse_log.txt"
    protected_log = legacy_log if inherited_path else default_log
    if existing:
        protected_log.parent.mkdir(parents=True, exist_ok=True)
        protected_log.write_bytes(b"existing log: do not touch\n")
        os.utime(protected_log, ns=(1_000_000_000, 1_000_000_000))
    before = {path: _snapshot(path) for path in (legacy_log, default_log)}

    # Do not inherit user plugins, credentials, DB paths or an existing log path.
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL",
    }}
    env.update({
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "SAIVERSE_HOME": str(tmp_path / "isolated-home"),
        "SAIVERSE_USER_DATA_DIR": str(tmp_path / "isolated-home" / "user_data"),
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if inherited_path:
        env["SAIVERSE_LOG_PATH"] = str(legacy_log)
    if target != "autodiscovery":
        env["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, target, str(PROJECT_ROOT)],
        cwd=work_dir, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: _snapshot(path) for path in before} == before
    if not existing:
        assert not legacy_log.parent.exists()
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    backend_log = Path(probe["backend_log"]).read_text(encoding="utf-8")
    assert "logger initialized" not in backend_log
    for logger in probe["loggers"]:
        assert logger["handlers"] == 0
        assert logger["propagate"] is True
        assert logger["level"] == 0  # NOTSET: inherit the application's level.
        for level in ("debug", "info"):
            assert backend_log.count(f"isolation-{level}:{logger['name']}") == 1


@pytest.fixture
def url_reader():
    return load_builtin_tool("read_url_content")


def test_url_reader_preserves_content_and_logs(url_reader, monkeypatch, caplog):
    response = MagicMock()
    response.headers = {"Content-Type": "text/html; charset=utf-8"}
    response.apparent_encoding = "utf-8"
    response.text = "<html><nav>Ignore me</nav><main><h1>Example</h1><p>Body text.</p></main></html>"
    get = MagicMock(return_value=response)
    monkeypatch.setattr(url_reader.requests, "get", get)
    with caplog.at_level("INFO"):
        message, result = url_reader.read_url_content("example.invalid/article")
    get.assert_called_once_with(
        "https://example.invalid/article", headers={"User-Agent": url_reader.USER_AGENT},
        timeout=url_reader.DEFAULT_TIMEOUT, allow_redirects=True,
    )
    response.raise_for_status.assert_called_once_with()
    assert response.encoding == "utf-8"
    assert "# Example" in message and "Body text." in message
    assert "Ignore me" not in message
    assert result.history_snippet.startswith("URL読み込み: https://example.invalid/article")
    assert "read_url_content called" in caplog.text
    assert "read_url_content completed" in caplog.text


def test_url_reader_preserves_request_errors(url_reader, monkeypatch):
    get = MagicMock(side_effect=url_reader.requests.exceptions.Timeout())
    monkeypatch.setattr(url_reader.requests, "get", get)
    message, result = url_reader.read_url_content("https://example.invalid")
    assert message.startswith("タイムアウト:")
    assert result.history_snippet is None


@pytest.fixture
def email_tool(monkeypatch):
    module = load_builtin_tool("send_email_to_user")
    # The real email function builds its EmailMessage, but DB and SMTP stay fake.
    monkeypatch.setattr(module, "default_db_path", lambda: Path("unused-test.db"))
    monkeypatch.setattr(module, "create_engine", MagicMock())
    monkeypatch.setattr(module.Base.metadata, "create_all", MagicMock())
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = [
        SimpleNamespace(MAILADDRESS="recipient@example.invalid"),
        SimpleNamespace(AINAME="Test Persona"),
    ]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(module, "sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(module, "get_active_persona_id", lambda: "synthetic-persona")
    for key, value in {
        "SMTP_HOST": "smtp.example.invalid", "SMTP_PORT": "587",
        "SMTP_USERNAME": "sender@example.invalid", "SMTP_PASSWORD": "synthetic-password",
        "SMTP_FROM": "sender@example.invalid", "SMTP_USE_TLS": "true", "SMTP_DEBUG": "0",
    }.items():
        monkeypatch.setenv(key, value)
    smtp = MagicMock()
    monkeypatch.setattr(module.smtplib, "SMTP", smtp)
    return module, smtp


def test_email_preserves_message_and_logs(email_tool, caplog):
    module, smtp = email_tool
    with caplog.at_level("INFO"):
        result = module.send_email_to_user(7, "Synthetic subject", "Synthetic body")
    assert result == "Email sent."
    smtp.assert_called_once_with("smtp.example.invalid", 587, timeout=30)
    server = smtp.return_value.__enter__.return_value
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("sender@example.invalid", "synthetic-password")
    server.send_message.assert_called_once()
    message = server.send_message.call_args.args[0]
    assert message["From"] == "Test Persona from SAIVerse <sender@example.invalid>"
    assert message["To"] == "recipient@example.invalid"
    assert message["Subject"] == "Synthetic subject"
    assert message.get_content().strip() == "Synthetic body"
    assert "send_email_to_user called" in caplog.text
    assert "send_email_to_user result: Email sent." in caplog.text


def test_email_preserves_smtp_failure(email_tool, caplog):
    module, smtp = email_tool
    smtp.side_effect = RuntimeError("synthetic SMTP failure")
    with caplog.at_level("ERROR"):
        result = module.send_email_to_user(7, "Synthetic subject", "Synthetic body")
    assert result == "Failed to send email: synthetic SMTP failure"
    assert "email send failed for user_id=7" in caplog.text
