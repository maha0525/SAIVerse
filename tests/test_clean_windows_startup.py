"""A freshly installed Windows: no Visual C++ runtime, Git only inside the folder.

Both were found by installing the v0.3.19 ZIP in Windows Sandbox (2026-10-02):
docs/issues/clean_windows_missing_vc_runtime_blocks_startup.md
docs/issues/searxng_needs_git_on_path.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ensure_searxng_version  # noqa: E402
from saiverse import windows_runtime_check  # noqa: E402


# --- Visual C++ runtime -------------------------------------------------------


def test_missing_dlls_are_named(monkeypatch):
    monkeypatch.setattr(windows_runtime_check.sys, "platform", "win32")
    present = {"vcruntime140.dll", "vcruntime140_1.dll"}  # what python.org's Python ships

    missing = windows_runtime_check.missing_vc_runtime_dlls(can_load=lambda name: name in present)

    assert missing == ["msvcp140.dll", "msvcp140_1.dll"]


def test_other_platforms_never_report_missing_dlls(monkeypatch):
    monkeypatch.setattr(windows_runtime_check.sys, "platform", "darwin")

    assert windows_runtime_check.missing_vc_runtime_dlls(can_load=lambda name: False) == []


def test_startup_stops_with_a_readable_message_when_onnxruntime_cannot_load(capsys):
    with pytest.raises(SystemExit) as stopped:
        windows_runtime_check.exit_if_vc_runtime_missing(
            missing_dlls=lambda: ["msvcp140.dll"],
            onnxruntime_loads=lambda: False,
        )

    assert stopped.value.code == 1
    message = capsys.readouterr().err
    assert "Visual C++" in message
    assert "msvcp140.dll" in message
    assert "setup.bat" in message
    assert windows_runtime_check.VC_REDIST_URL in message


def test_startup_continues_when_nothing_is_missing(capsys):
    windows_runtime_check.exit_if_vc_runtime_missing(
        missing_dlls=lambda: [],
        onnxruntime_loads=lambda: pytest.fail("onnxruntime must not be probed when nothing is missing"),
    )

    assert capsys.readouterr().err == ""


def test_startup_is_not_blocked_when_the_guess_is_wrong_but_onnxruntime_loads(capsys):
    # The DLL check is a guess about another component's needs. A machine where
    # onnxruntime does load must never be kept from starting by it.
    windows_runtime_check.exit_if_vc_runtime_missing(
        missing_dlls=lambda: ["msvcp140_1.dll"],
        onnxruntime_loads=lambda: True,
    )

    assert capsys.readouterr().err == ""


def test_message_survives_a_console_that_cannot_show_japanese(monkeypatch):
    class _AsciiConsole:
        encoding = "ascii"

        def __init__(self):
            self.written = ""

        def write(self, text):
            text.encode("ascii")  # an ASCII console raises on anything else
            self.written += text

        def flush(self):
            pass

    console = _AsciiConsole()
    monkeypatch.setattr(windows_runtime_check.sys, "stderr", console)

    with pytest.raises(SystemExit):
        windows_runtime_check.exit_if_vc_runtime_missing(
            missing_dlls=lambda: ["msvcp140.dll"],
            onnxruntime_loads=lambda: False,
        )

    assert "SAIVerse cannot start" in console.written


def test_backend_checks_the_runtime_before_importing_what_needs_it():
    lines = (REPO_ROOT / "main.py").read_text(encoding="utf-8").splitlines()
    check = next(i for i, line in enumerate(lines) if line.strip() == "exit_if_vc_runtime_missing()")
    manager_import = next(i for i, line in enumerate(lines) if line.startswith("from saiverse.saiverse_manager import"))

    assert check < manager_import


def _command_lines(path: Path, comment: str) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith(comment)]


def test_setup_installs_the_runtime():
    commands = _command_lines(REPO_ROOT / "setup.bat", "REM ")

    assert any(line.endswith("scripts\\install_vc_redist.ps1") for line in commands)


@pytest.mark.parametrize("script", ["scripts/install_vc_redist.ps1", "setup.bat"])
def test_windows_scripts_stay_ascii(script):
    # A .bat / .ps1 with non-ASCII bytes is misread under the console code page.
    (REPO_ROOT / script).read_bytes().decode("ascii")


# --- SearXNG without git ------------------------------------------------------


@pytest.fixture
def searxng_src(tmp_path: Path) -> Path:
    (tmp_path / "searx").mkdir()
    return tmp_path


def test_version_file_is_written_when_git_cannot_be_found(searxng_src, monkeypatch):
    monkeypatch.setattr(ensure_searxng_version.shutil, "which", lambda name: None)

    assert ensure_searxng_version.ensure_version_frozen(searxng_src) is True

    # SearXNG reads exactly these five names (searx/version.py); the values are
    # the ones SearXNG itself falls back to when git fails.
    namespace: dict = {}
    exec((searxng_src / "searx" / "version_frozen.py").read_text(encoding="utf-8"), namespace)
    assert {name: namespace[name] for name in ("VERSION_STRING", "VERSION_TAG", "DOCKER_TAG", "GIT_URL", "GIT_BRANCH")} == {
        "VERSION_STRING": "1.0.0",
        "VERSION_TAG": "1.0.0",
        "DOCKER_TAG": "1.0.0",
        "GIT_URL": "unknown",
        "GIT_BRANCH": "unknown",
    }


def test_version_file_is_not_written_when_git_is_available(searxng_src, monkeypatch):
    # With git, SearXNG reports its real version; a frozen file would hide it.
    monkeypatch.setattr(ensure_searxng_version.shutil, "which", lambda name: "C:/Git/cmd/git.exe")

    assert ensure_searxng_version.ensure_version_frozen(searxng_src) is False
    assert not (searxng_src / "searx" / "version_frozen.py").exists()


def test_existing_version_file_is_left_alone(searxng_src, monkeypatch):
    monkeypatch.setattr(ensure_searxng_version.shutil, "which", lambda name: None)
    target = searxng_src / "searx" / "version_frozen.py"
    target.write_text('VERSION_STRING = "2026.9.1"\n', encoding="utf-8")

    assert ensure_searxng_version.ensure_version_frozen(searxng_src) is False
    assert target.read_text(encoding="utf-8") == 'VERSION_STRING = "2026.9.1"\n'


def test_missing_source_directory_does_not_stop_the_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(ensure_searxng_version.shutil, "which", lambda name: None)

    assert ensure_searxng_version.main(["ensure_searxng_version.py", str(tmp_path / "not-there")]) == 0


@pytest.mark.parametrize(
    ("script", "comment"),
    [("scripts/run_searxng_server.ps1", "#"), ("scripts/run_searxng_server.sh", "#")],
)
def test_both_launchers_prepare_the_version_file_before_starting(script, comment):
    commands = _command_lines(REPO_ROOT / script, comment)
    prepare = next(i for i, line in enumerate(commands) if "ensure_searxng_version.py" in line)
    start = next(i for i, line in enumerate(commands) if "searx.webapp" in line)

    assert prepare < start
