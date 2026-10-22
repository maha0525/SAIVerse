"""setup.bat / setup.sh の .venv の確かめと作り直し (scripts/setup_venv.py)。

設計: docs/intent/dependency_management.md §2-7。.venv は tmp_path の中に偽物を
作る (本物の venv は作らない)。改名の失敗は os.rename を差し替えて起こす。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup_venv.py"
_spec = importlib.util.spec_from_file_location("setup_venv_under_test", _SCRIPT)
setup_venv = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(setup_venv)

NOW = dt.datetime(2026, 10, 23, 12, 34, 56)


def _make_venv(root: Path, cfg: str | None, *, complete: bool = True) -> Path:
    venv = root / ".venv"
    venv.mkdir()
    if complete:
        if os.name == "nt":
            (venv / "Scripts").mkdir()
            (venv / "Scripts" / "activate.bat").write_text("", encoding="utf-8")
            (venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
        else:
            (venv / "bin").mkdir()
            (venv / "bin" / "activate").write_text("", encoding="utf-8")
            (venv / "bin" / "python").write_text("", encoding="utf-8")
    if cfg is not None:
        (venv / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    (venv / "marker.txt").write_text("old", encoding="utf-8")
    return venv


def _cfg(version_line: str) -> str:
    return f"home = C:\\Python312\ninclude-system-site-packages = false\n{version_line}\n"


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        (_cfg("version = 3.12.4"), "3.12"),
        (_cfg("version = 3.13.17"), "3.13"),
        (_cfg("version_info = 3.12.4.final.0"), "3.12"),  # virtualenv / uv の形
        ("home = x\nversion = 3.13.2\nversion_info = 3.12.0\n", "3.13"),  # version が優先
        ("home = x\n", None),
        (_cfg("version = unknown"), None),
        (None, None),
    ],
)
def test_read_venv_series(tmp_path, cfg, expected) -> None:
    venv = _make_venv(tmp_path, cfg)
    assert setup_venv.read_venv_series(venv) == expected


def test_no_venv_means_create(tmp_path) -> None:
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "create"
    assert not (tmp_path / ".venv").exists()


def test_venv_of_the_expected_series_is_kept(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.13.2"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "keep"
    assert (tmp_path / ".venv" / "marker.txt").is_file()


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg("version = 3.12.10"),  # 別の系列
        _cfg("version = 3.14.0"),
        "home = x\n",  # version の行が無い (系列が分からない)
        None,  # pyvenv.cfg が無い
    ],
)
def test_other_or_unknown_series_is_renamed_not_deleted(tmp_path, cfg) -> None:
    _make_venv(tmp_path, cfg)
    result = setup_venv.prepare(tmp_path, "3.13", NOW)
    assert result == "renamed .venv.old-20261023-123456"
    assert not (tmp_path / ".venv").exists()
    assert (tmp_path / ".venv.old-20261023-123456" / "marker.txt").read_text(encoding="utf-8") == "old"


def test_without_expected_series_the_series_is_not_checked(tmp_path) -> None:
    """SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 の経路: 系列を理由に作り直さない。"""
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    assert setup_venv.prepare(tmp_path, None, NOW) == "keep"


def test_incomplete_venv_is_renamed_even_without_expected_series(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.13.2"), complete=False)
    assert setup_venv.prepare(tmp_path, None, NOW).startswith("renamed .venv.old-")
    assert not (tmp_path / ".venv").exists()


def test_rename_gets_a_new_name_when_the_old_one_is_taken(tmp_path) -> None:
    (tmp_path / ".venv.old-20261023-123456").mkdir()
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-123456-2"


def test_refused_rename_changes_nothing(tmp_path, monkeypatch) -> None:
    _make_venv(tmp_path, _cfg("version = 3.12.10"))

    def refuse(_src, _dst):  # type: ignore[no-untyped-def]
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(setup_venv.os, "rename", refuse)
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "locked"
    assert (tmp_path / ".venv" / "marker.txt").is_file()
    assert [p.name for p in tmp_path.iterdir()] == [".venv"]


def test_finish_deletes_only_the_folders_prepare_creates(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.13.2"))
    for name in (".venv.old-20261023-123456", ".venv.old-20261020-010203-2"):
        (tmp_path / name / "Lib").mkdir(parents=True)
        (tmp_path / name / "Lib" / "x.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv.old").mkdir()  # 利用者が手で作ったもの
    (tmp_path / ".venv.old-backup").mkdir()
    assert setup_venv.finish(tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [".venv", ".venv.old", ".venv.old-backup"]


def test_rollback_puts_the_previous_venv_back(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    old = setup_venv.prepare(tmp_path, "3.13", NOW).split(" ", 1)[1]
    (tmp_path / ".venv").mkdir()  # 作りかけの新しい .venv
    (tmp_path / ".venv" / "half.txt").write_text("", encoding="utf-8")
    assert setup_venv.rollback(tmp_path, old) is True
    assert (tmp_path / ".venv" / "marker.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / ".venv" / "half.txt").exists()
    assert not (tmp_path / old).exists()


def test_rollback_refuses_a_name_it_did_not_create(tmp_path) -> None:
    (tmp_path / "elsewhere").mkdir()
    assert setup_venv.rollback(tmp_path, "elsewhere") is False
    assert (tmp_path / "elsewhere").is_dir()


def test_main_prints_one_result_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _make_venv(tmp_path, _cfg("version = 3.13.2"))
    assert setup_venv.main(["prepare", "--expect-series", "3.13"]) == 0
    assert capsys.readouterr().out == "keep\n"


# --- a setup stopped part-way is continued (resume) ------------------------------------


def _old_venv(root: Path, name: str, *, complete: bool = True) -> Path:
    """A .venv the previous setup renamed aside."""
    venv = _make_venv(root, _cfg("version = 3.12.10"), complete=complete)
    return venv.rename(root / name)


def test_a_new_venv_beside_an_old_one_resumes_the_previous_setup(tmp_path) -> None:
    """The previous setup renamed the old .venv, made the new one and stopped
    before finish: keep would skip the addon reinstall into .venv, could not
    roll back a failing pip, and finish would delete the old one."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "resume .venv.old-20261023-120000"
    assert (tmp_path / ".venv.old-20261023-120000").is_dir()
    assert setup_venv.prepare(tmp_path, None, NOW) == "resume .venv.old-20261023-120000"


def test_resume_names_the_newest_complete_old_venv(tmp_path) -> None:
    _old_venv(tmp_path, ".venv.old-20261020-010203")
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _old_venv(tmp_path, ".venv.old-20261023-120000-2")
    _old_venv(tmp_path, ".venv.old-20261024-000000", complete=False)  # what a failed finish left
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "resume .venv.old-20261023-120000-2"


def test_only_incomplete_old_venvs_do_not_resume(tmp_path) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000", complete=False)
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "keep"


def test_no_venv_beside_an_old_one_creates_it_with_the_old_one_to_roll_back_to(tmp_path) -> None:
    """Stopped between the rename and the new .venv."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-120000"
    assert setup_venv.rollback(tmp_path, ".venv.old-20261023-120000") is True
    assert (tmp_path / ".venv" / "marker.txt").is_file()


def test_a_half_made_venv_rolls_back_to_the_venv_before_the_previous_setup(tmp_path) -> None:
    """Stopped while the new .venv was being made: it is incomplete, and the one
    to put back is the old one, not the half-made one."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, None, complete=False)
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-120000"
    assert (tmp_path / ".venv.old-20261023-123456").is_dir()  # the half-made one, deleted by finish
    assert not (tmp_path / ".venv").exists()


def test_the_resumed_old_venv_can_be_rolled_back_and_is_finished(tmp_path) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    old = setup_venv.prepare(tmp_path, "3.13", NOW).split(" ", 1)[1]
    assert setup_venv.finish(tmp_path) == []
    assert not (tmp_path / old).exists()
    assert (tmp_path / ".venv").is_dir()


# --- check-stopped: nothing may change while SAIVerse runs ------------------------------


def test_the_launchers_check_before_the_first_change_and_finish_after_the_addons() -> None:
    root = _SCRIPT.parent.parent
    bat = (root / "setup.bat").read_text(encoding="ascii")
    check = bat.index("scripts\\setup_venv.py check-stopped")
    for first_change in ("install_node_portable.ps1", "setup_venv.py prepare", "npm install", "setup_searxng.ps1"):
        assert check < bat.index(first_change), first_change
    assert bat.index("rebuild_addon_envs.py") < bat.index("setup_venv.py finish")
    assert 'if "%VENV_ACTION%"=="resume" goto :venv_resume' in bat
    resume = bat[bat.index(":venv_resume"):]
    assert resume.index("--core-venv-recreated") < resume.index(":venv_ready")
    sh = (root / "setup.sh").read_text(encoding="utf-8")
    check = sh.index("scripts/setup_venv.py check-stopped")
    for first_change in ("setup_venv.py prepare", "npm install", "setup_searxng.sh"):
        assert check < sh.index(first_change), first_change
    assert sh.index("rebuild_addon_envs.py") < sh.index("setup_venv.py finish")
    assert '[ "$VENV_ACTION" = "resume" ]' in sh


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Path:
    """SAIVERSE_HOME in tmp_path: never this machine's runtime markers."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    return home


def _marker(home: Path, *, pid: int, created: object, name: str = "a.json") -> Path:
    directory = home / ".runtime"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps({"pid": pid, "token": "t", "city_name": "city_a", "process_created_at": created}),
        encoding="utf-8",
    )
    return path


def test_a_first_install_is_not_stopped(tmp_path, isolated_home, monkeypatch, capsys) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    assert setup_venv.check_stopped(root) is True
    assert capsys.readouterr().err == ""  # no .venv: nothing that could run here


def test_the_exact_check_of_the_venv_decides(tmp_path, isolated_home, monkeypatch, capsys) -> None:
    calls: list = []
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: calls.append(home) or ("stopped", ""))
    for code, allowed in ((0, True), (20, False), (21, True)):
        monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root, code=code: code)
        assert setup_venv.check_stopped(tmp_path) is allowed
    assert calls == []  # the stdlib check is only the fallback
    err = capsys.readouterr().err
    assert "setup stopped before changing anything" in err
    assert "Could not check whether this folder's SAIVerse Frontend or SearXNG window is open" in err


@pytest.mark.parametrize("code", [None, 22, 1, 2, 103])
def test_without_a_usable_venv_the_markers_are_read_with_the_standard_library(
    tmp_path, isolated_home, monkeypatch, code
) -> None:
    monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root: code)
    seen: list = []

    def status(home):  # type: ignore[no-untyped-def]
        seen.append(home)
        return "running", "SAIVerse City 'city_a' pid 1"

    monkeypatch.setattr(setup_venv, "runtime_marker_status", status)
    assert setup_venv.check_stopped(tmp_path) is False
    assert seen == [isolated_home]
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: ("unknown", "unreadable"))
    assert setup_venv.check_stopped(tmp_path) is False
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: ("stopped", "absent"))
    assert setup_venv.check_stopped(tmp_path) is True


def test_an_unreadable_dotenv_stops_setup(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root: None)
    (tmp_path / ".env").write_bytes(b"SAIVERSE_HOME=\xff\xfe\n")
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: pytest.fail("must not guess a home"))
    assert setup_venv.check_stopped(tmp_path) is False


def test_runtime_markers_read_with_the_standard_library(isolated_home) -> None:
    psutil = pytest.importorskip("psutil")
    assert setup_venv.runtime_marker_status(isolated_home) == ("stopped", "runtime markers absent")

    me = psutil.Process(os.getpid())
    running = _marker(isolated_home, pid=me.pid, created=me.create_time())
    state, reason = setup_venv.runtime_marker_status(isolated_home)
    assert state == "running" and str(me.pid) in reason

    # The same pid with another start time: a reused pid, i.e. a stale marker.
    _marker(isolated_home, pid=me.pid, created=me.create_time() - 3600, name="a.json")
    assert setup_venv.runtime_marker_status(isolated_home)[0] == "stopped"
    # A live pid without a start time to compare cannot be told apart from SAIVerse.
    _marker(isolated_home, pid=me.pid, created=None, name="a.json")
    assert setup_venv.runtime_marker_status(isolated_home)[0] == "unknown"
    running.write_text("{not json", encoding="utf-8")
    assert setup_venv.runtime_marker_status(isolated_home)[0] == "unknown"


def test_a_marker_of_an_exited_process_is_stale(isolated_home) -> None:
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    _marker(isolated_home, pid=child.pid, created=1.0)
    assert setup_venv.runtime_marker_status(isolated_home)[0] == "stopped"


def test_the_start_time_agrees_with_psutil() -> None:
    """runtime_marker records psutil's create_time; the standard-library reading
    must land on the same value (within the tolerance) on this platform."""
    psutil = pytest.importorskip("psutil")
    actual = setup_venv._process_created_at(os.getpid())
    if actual is None:
        pytest.skip("this platform has no standard-library start time (macOS)")
    assert abs(actual - psutil.Process(os.getpid()).create_time()) < 0.1


# --- SAIVERSE_HOME exactly as main.py's load_dotenv() resolves it -----------------------

_DOTENV_SAMPLES = [
    "SAIVERSE_HOME=/plain\n",
    "export SAIVERSE_HOME = '/single quoted' # comment\n",
    'SAIVERSE_HOME="C:\\\\Users\\\\x\\\\home"\n',
    "SAIVERSE_HOME=/a/b # trailing comment\nOTHER=1\n",
    "BASE=/from/file\nSAIVERSE_HOME=${BASE}/saiverse\n",
    "SAIVERSE_HOME=${MISSING:-/fallback}/x\n",
    "# comment\n\n  SAIVERSE_HOME=/indented\r\n",
    "\ufeffSAIVERSE_HOME=/bom\n",
    "broken line without equals\nSAIVERSE_HOME=/after/broken\n",
    "'SAIVERSE_HOME'=/quoted/key\n",
    'SAIVERSE_HOME="multi\nline"\n',
    "SAIVERSE_HOME\n",
    "SAIVERSE_HOME=\n",
    "SAIVERSE_HOME='${BASE}/single'\n",
]


@pytest.mark.parametrize("text", _DOTENV_SAMPLES)
def test_the_dotenv_reading_agrees_with_python_dotenv(text, monkeypatch) -> None:
    from dotenv.main import DotEnv
    import io

    monkeypatch.setenv("BASE", "/from/environment")
    monkeypatch.delenv("MISSING", raising=False)
    expected = DotEnv(None, stream=io.StringIO(text), interpolate=True, override=False).dict()
    assert setup_venv.resolve_dotenv(setup_venv.parse_dotenv(text), dict(os.environ)) == dict(expected)


def test_saiverse_home_follows_load_dotenv(tmp_path, monkeypatch) -> None:
    """Run main.py's own load_dotenv on the same .env in a child process and
    compare: an environment variable wins inside ${...} and over the file."""
    import subprocess
    import sys

    (tmp_path / ".env").write_text("BASE=/from/file\nSAIVERSE_HOME=${BASE}/saiverse\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("SAIVERSE_HOME", "PYTHON_DOTENV_DISABLED")}
    env["BASE"] = "/from/environment"
    probe = (
        "import os, sys; from dotenv import load_dotenv; load_dotenv(sys.argv[1]); "
        "print(os.environ.get('SAIVERSE_HOME'))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path / ".env")], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "/from/environment/saiverse"
    assert setup_venv.saiverse_home(tmp_path, env) == Path(loaded)
    assert setup_venv.saiverse_home(tmp_path, {**env, "SAIVERSE_HOME": "/set"}) == Path("/set")
    assert setup_venv.saiverse_home(tmp_path, {**env, "PYTHON_DOTENV_DISABLED": "1"}) == Path.home() / ".saiverse"
    assert setup_venv.saiverse_home(tmp_path / "no-dotenv", env) == Path.home() / ".saiverse"


# --- update_engine.py --check-stopped (the exact check run with the existing .venv) -----


def _engine():  # type: ignore[no-untyped-def]
    from scripts import update_engine

    return update_engine


def test_the_engine_check_reports_running_saiverse_and_servers(tmp_path, monkeypatch) -> None:
    from saiverse import runtime_marker

    update_engine = _engine()
    monkeypatch.setattr(update_engine, "_apply_dotenv_home", lambda project: None)
    none = update_engine.FrontendServers([], None)
    monkeypatch.setattr(update_engine, "find_frontend_servers", lambda project: none)
    monkeypatch.setattr(update_engine, "find_searxng_servers", lambda project: [])

    monkeypatch.setattr(runtime_marker, "marker_status", lambda: ("stopped", "absent"))
    assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_NOTHING_RUNNING
    for state in ("running", "unknown"):
        monkeypatch.setattr(runtime_marker, "marker_status", lambda state=state: (state, "pid 1"))
        assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_MAY_BE_RUNNING

    monkeypatch.setattr(runtime_marker, "marker_status", lambda: ("stopped", "absent"))
    from unittest.mock import MagicMock

    monkeypatch.setattr(update_engine, "find_searxng_servers", lambda project: [MagicMock(pid=7)])
    assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_MAY_BE_RUNNING

    def unavailable(project):  # type: ignore[no-untyped-def]
        raise update_engine.FrontendCheckUnavailable("no psutil")

    monkeypatch.setattr(update_engine, "find_frontend_servers", unavailable)
    assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_PROCESSES_UNCHECKED


def test_the_engine_check_without_dotenv_falls_back_and_an_unreadable_dotenv_stops(tmp_path, monkeypatch) -> None:
    update_engine = _engine()

    def no_dotenv(project):  # type: ignore[no-untyped-def]
        raise update_engine.DotenvUnavailable(".env exists, but python-dotenv is not installed")

    monkeypatch.setattr(update_engine, "_apply_dotenv_home", no_dotenv)
    assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_CHECK_UNAVAILABLE

    def unreadable(project):  # type: ignore[no-untyped-def]
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(update_engine, "_apply_dotenv_home", unreadable)
    assert update_engine.check_stopped_for_setup(tmp_path) == update_engine.STOPPED_MAY_BE_RUNNING


def test_the_exit_codes_agree() -> None:
    update_engine = _engine()
    assert setup_venv._ENGINE_NOTHING_RUNNING == update_engine.STOPPED_NOTHING_RUNNING
    assert setup_venv._ENGINE_MAY_BE_RUNNING == update_engine.STOPPED_MAY_BE_RUNNING
    assert setup_venv._ENGINE_PROCESSES_UNCHECKED == update_engine.STOPPED_PROCESSES_UNCHECKED
