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


# --- the record: a setup stopped part-way is continued ---------------------------------
#
# prepare writes .setup_in_progress/venv.json before it renames or creates .venv;
# finish or a rollback removes it. While it is there, the next prepare
# continues the stopped setup whatever .venv looks like.


def _entry(root: Path) -> dict | None:
    return setup_venv.setup_in_progress.read_entry(root, setup_venv.VENV_ENTRY)


def _write_entry(root: Path, old: str | None) -> None:
    setup_venv.setup_in_progress.write_entry(root, setup_venv.VENV_ENTRY, {"old": old})


def _old_venv(root: Path, name: str, *, complete: bool = True) -> Path:
    """A .venv the previous setup renamed aside."""
    venv = _make_venv(root, _cfg("version = 3.12.10"), complete=complete)
    return venv.rename(root / name)


class _Stop(BaseException):
    """Stands in for the setup window being closed (not an Exception, as Ctrl+C is not)."""


def test_the_record_is_written_before_the_rename_and_create(tmp_path) -> None:
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "create"
    assert _entry(tmp_path)["old"] is None
    (tmp_path / ".setup_in_progress" / "venv.json").unlink()
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-123456"
    assert _entry(tmp_path)["old"] == ".venv.old-20261023-123456"


def test_keep_writes_no_record(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.13.2"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "keep"
    assert _entry(tmp_path) is None


def test_a_refused_rename_removes_the_record(tmp_path, monkeypatch) -> None:
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    monkeypatch.setattr(setup_venv.os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError(32, "in use")))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "locked"
    assert _entry(tmp_path) is None


def test_an_unwritable_record_changes_nothing(tmp_path, monkeypatch) -> None:
    _make_venv(tmp_path, _cfg("version = 3.12.10"))

    def unwritable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "denied")

    monkeypatch.setattr(setup_venv.setup_in_progress, "write_entry", unwritable)
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "error"
    assert (tmp_path / ".venv" / "marker.txt").is_file()
    assert setup_venv.prepare(tmp_path / "nothing-here", "3.13", NOW) == "error"


def test_stopped_between_the_record_and_the_rename(tmp_path, monkeypatch) -> None:
    """The record names a folder that was never made: .venv is still the old
    one and is renamed as in a first try."""
    _make_venv(tmp_path, _cfg("version = 3.12.10"))

    def stop(_src, _dst):  # type: ignore[no-untyped-def]
        raise _Stop

    with monkeypatch.context() as m:
        m.setattr(setup_venv.os, "rename", stop)
        with pytest.raises(_Stop):
            setup_venv.prepare(tmp_path, "3.13", NOW)
    assert _entry(tmp_path)["old"] == ".venv.old-20261023-123456"
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-123456"
    assert (tmp_path / ".venv.old-20261023-123456" / "marker.txt").read_text(encoding="utf-8") == "old"


def test_stopped_after_the_rename_creates_with_the_old_one_to_put_back(tmp_path) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _write_entry(tmp_path, ".venv.old-20261023-120000")
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-120000"
    assert setup_venv.rollback(tmp_path, ".venv.old-20261023-120000") is True
    assert (tmp_path / ".venv" / "marker.txt").is_file()
    assert _entry(tmp_path) is None  # put back: nothing is in progress any more


def test_stopped_after_the_new_venv_was_made_resumes(tmp_path) -> None:
    """keep would skip the addon reinstall into .venv, could not roll back a
    failing pip, and finish would delete the old one."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    _write_entry(tmp_path, ".venv.old-20261023-120000")
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "resume .venv.old-20261023-120000"
    assert setup_venv.prepare(tmp_path, None, NOW) == "resume .venv.old-20261023-120000"
    assert (tmp_path / ".venv.old-20261023-120000").is_dir()


def test_stopped_while_the_new_venv_was_made_deletes_it_and_keeps_the_old_one(tmp_path) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, None, complete=False)
    _write_entry(tmp_path, ".venv.old-20261023-120000")
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "renamed .venv.old-20261023-120000"
    assert not (tmp_path / ".venv").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [".setup_in_progress", ".venv.old-20261023-120000"]


def test_a_stopped_setup_that_only_created_resumes_without_an_old_one(tmp_path) -> None:
    """A setup that started with no .venv (create) stopped before finish: the
    .venv it made gets its packages and addon packages again, nothing to put back."""
    _write_entry(tmp_path, None)
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "create"
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "resume"
    assert _entry(tmp_path) is not None


def test_an_unreadable_record_still_continues(tmp_path) -> None:
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    path = setup_venv.setup_in_progress.entry_path(tmp_path, setup_venv.VENV_ENTRY)
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "resume"


def test_finish_removes_the_record_and_renames_before_deleting(tmp_path, monkeypatch) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    _write_entry(tmp_path, ".venv.old-20261023-120000")
    deleted: list[str] = []
    real = setup_venv._rmtree
    monkeypatch.setattr(setup_venv, "_rmtree", lambda path: (deleted.append(path.name), real(path)))
    assert setup_venv.finish(tmp_path, NOW) == []
    assert _entry(tmp_path) is None
    assert deleted == [".venv.deleting-20261023-123456"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [".venv"]


def test_a_finish_stopped_while_deleting_leaves_nothing_to_put_back(tmp_path, monkeypatch) -> None:
    """A half-deleted old .venv still has activate and python: under its old
    name the next setup would put it back."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    _write_entry(tmp_path, ".venv.old-20261023-120000")

    def stop(_path):  # type: ignore[no-untyped-def]
        raise _Stop

    with monkeypatch.context() as m:
        m.setattr(setup_venv, "_rmtree", stop)
        with pytest.raises(_Stop):
            setup_venv.finish(tmp_path, NOW)
    assert (tmp_path / ".venv.deleting-20261023-123456").is_dir()
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "keep"
    assert setup_venv.rollback(tmp_path, ".venv.deleting-20261023-123456") is False
    assert setup_venv.finish(tmp_path, NOW) == []  # the next finish deletes it
    assert sorted(p.name for p in tmp_path.iterdir()) == [".venv"]


def test_a_record_naming_a_deleting_folder_is_not_a_place_to_go_back_to(tmp_path) -> None:
    deleting = _old_venv(tmp_path, ".venv.deleting-20261023-120000")
    _write_entry(tmp_path, deleting.name)
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == "create"
    assert _entry(tmp_path)["old"] is None


def test_finish_keeps_an_old_venv_whose_rename_is_refused(tmp_path, monkeypatch) -> None:
    """Renaming fails while a file inside is in use (Windows); deleting would
    then stop part-way, so it is left as it is for the next finish."""
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    monkeypatch.setattr(setup_venv.os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError(32, "in use")))
    assert setup_venv.finish(tmp_path, NOW) == [".venv.old-20261023-120000"]
    assert (tmp_path / ".venv.old-20261023-120000" / "marker.txt").is_file()


def test_a_failed_rollback_keeps_the_record(tmp_path, monkeypatch) -> None:
    _old_venv(tmp_path, ".venv.old-20261023-120000")
    _make_venv(tmp_path, None, complete=False)
    _write_entry(tmp_path, ".venv.old-20261023-120000")
    monkeypatch.setattr(setup_venv.os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError(32, "in use")))
    assert setup_venv.rollback(tmp_path, ".venv.old-20261023-120000") is False
    assert _entry(tmp_path) is not None


def test_a_whole_setup_stopped_at_each_point_ends_with_the_old_venv_or_a_finished_one(tmp_path) -> None:
    """Every point a 3.12 -> 3.13 setup can stop at, followed by a setup that
    fails (rollback) or one that finishes."""
    _make_venv(tmp_path, _cfg("version = 3.12.10"))
    first = setup_venv.prepare(tmp_path, "3.13", NOW)
    assert first == "renamed .venv.old-20261023-123456"
    old = first.split(" ", 1)[1]
    _make_venv(tmp_path, None, complete=False)  # stopped while making the new .venv
    again = setup_venv.prepare(tmp_path, "3.13", NOW)
    assert again == first  # the old one stays the one to put back
    _make_venv(tmp_path, _cfg("version = 3.13.17"))  # made, then stopped before pip finished
    assert setup_venv.prepare(tmp_path, "3.13", NOW) == f"resume {old}"
    assert setup_venv.rollback(tmp_path, old) is True  # pip failed this time
    assert (tmp_path / ".venv" / "marker.txt").read_text(encoding="utf-8") == "old"
    assert setup_venv.read_venv_series(tmp_path / ".venv") == "3.12"
    assert _entry(tmp_path) is None
    # The next setup starts over and finishes.
    assert setup_venv.prepare(tmp_path, "3.13", NOW).startswith("renamed ")
    _make_venv(tmp_path, _cfg("version = 3.13.17"))
    assert setup_venv.finish(tmp_path, NOW) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [".venv"]


# --- check-stopped: nothing may change while SAIVerse runs ------------------------------


def test_the_launchers_check_before_the_first_change_and_finish_after_the_addons() -> None:
    root = _SCRIPT.parent.parent
    bat = (root / "setup.bat").read_text(encoding="ascii")
    check = bat.index("scripts\\setup_venv.py check-stopped")
    for first_change in ("install_node_portable.ps1", "setup_venv.py prepare", "npm install", "setup_searxng.ps1"):
        assert check < bat.index(first_change), first_change
    assert bat.index("rebuild_addon_envs.py") < bat.index("setup_venv.py finish")
    # finish only when the addon script ran to its end
    rebuild = bat[bat.index("python scripts\\rebuild_addon_envs.py"):]
    assert rebuild.index("if errorlevel 1 goto :addon_rebuild_unfinished") < rebuild.index("setup_venv.py finish")
    assert 'if "%VENV_ACTION%"=="resume" goto :venv_resume' in bat
    resume = bat[bat.index(":venv_resume"):]
    assert resume.index("--core-venv-recreated") < resume.index(":venv_ready")
    sh = (root / "setup.sh").read_text(encoding="utf-8")
    check = sh.index("scripts/setup_venv.py check-stopped")
    for first_change in ("setup_venv.py prepare", "npm install", "setup_searxng.sh"):
        assert check < sh.index(first_change), first_change
    assert sh.index("if python scripts/rebuild_addon_envs.py") < sh.index("setup_venv.py finish")
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


class _Terminal:
    """A stdin: ``tty`` is whether it is a terminal, ``lines`` what the user types
    (an empty string is end of input; an exception is raised as Ctrl+C would be)."""

    def __init__(self, *lines: object, tty: bool = True) -> None:
        self.tty = tty
        self.lines = list(lines)
        self.reads = 0

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        self.reads += 1
        line = self.lines.pop(0) if self.lines else ""
        if isinstance(line, BaseException):
            raise line
        return str(line)


def test_a_first_install_is_not_stopped_or_asked(tmp_path, isolated_home, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SEARXNG_VENV_DIR", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    terminal = _Terminal(tty=False)
    assert setup_venv.check_stopped(root, terminal) is True
    assert terminal.reads == 0
    assert capsys.readouterr().err == ""  # nothing installed: nothing that could run here


@pytest.mark.parametrize("installed", [".venv", "frontend/node_modules", "scripts/.searxng-venv"])
def test_without_the_server_check_setup_asks_and_goes_on_after_enter(
    tmp_path, isolated_home, monkeypatch, capsys, installed
) -> None:
    monkeypatch.delenv("SEARXNG_VENV_DIR", raising=False)
    monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root: None)
    (tmp_path / installed).mkdir(parents=True)
    terminal = _Terminal("\n")
    assert setup_venv.check_stopped(tmp_path, terminal) is True
    assert terminal.reads == 1
    err = capsys.readouterr().err
    assert "SAIVerse の窓 (Backend / Frontend / SearXNG) をすべて閉じたら Enter を押してください。" in err
    assert "Ctrl+C" in err


@pytest.mark.parametrize(
    "terminal",
    [_Terminal("\n", tty=False), _Terminal(""), _Terminal(KeyboardInterrupt()), _Terminal(EOFError())],
    ids=["not-a-terminal", "end-of-input", "ctrl-c", "eof-error"],
)
def test_without_the_server_check_setup_stops_unless_the_user_presses_enter(
    tmp_path, isolated_home, monkeypatch, capsys, terminal
) -> None:
    monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root: setup_venv._ENGINE_PROCESSES_UNCHECKED)
    assert setup_venv.check_stopped(tmp_path, terminal) is False
    assert "setup" in capsys.readouterr().err
    if not terminal.tty:
        assert terminal.reads == 0  # nobody to ask: it does not wait


def test_the_exact_check_of_the_venv_decides(tmp_path, isolated_home, monkeypatch, capsys) -> None:
    calls: list = []
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: calls.append(home) or ("stopped", ""))
    for code, allowed in ((0, True), (20, False)):
        monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root, code=code: code)
        assert setup_venv.check_stopped(tmp_path, _Terminal(tty=False)) is allowed
    # 21: SAIVerse is stopped but the servers could not be listed -> ask
    monkeypatch.setattr(setup_venv, "_check_with_venv", lambda root: 21)
    assert setup_venv.check_stopped(tmp_path, _Terminal("\n")) is True
    assert setup_venv.check_stopped(tmp_path, _Terminal(tty=False)) is False
    assert calls == []  # the stdlib check is only the fallback
    err = capsys.readouterr().err
    assert "setup stopped before changing anything" in err


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
    assert setup_venv.check_stopped(tmp_path, _Terminal("\n")) is False
    assert seen == [isolated_home]
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: ("unknown", "unreadable"))
    assert setup_venv.check_stopped(tmp_path, _Terminal("\n")) is False
    monkeypatch.setattr(setup_venv, "runtime_marker_status", lambda home: ("stopped", "absent"))
    (tmp_path / ".venv").mkdir()
    assert setup_venv.check_stopped(tmp_path, _Terminal("\n")) is True
    assert setup_venv.check_stopped(tmp_path, _Terminal(tty=False)) is False


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
    # A SAIVerse folder without .env uses the one of a parent folder, as load_dotenv() does.
    (tmp_path / "SAIVerse").mkdir()
    assert setup_venv.saiverse_home(tmp_path / "SAIVerse", env) == Path(loaded)


def _dotenv_trees(tmp_path: Path):  # type: ignore[no-untyped-def]
    """(name, SAIVerse folder) pairs, each made in its own tree under tmp_path."""
    def tree(name: str) -> tuple[Path, Path]:
        top = tmp_path / name
        project = top / "parent" / "SAIVerse"
        project.mkdir(parents=True)
        return top, project

    top, project = tree("none")
    yield "no .env below tmp_path", project
    top, project = tree("parent")
    (project.parent / ".env").write_text("A=1\n", encoding="utf-8")
    yield ".env in the parent", project
    top, project = tree("both")
    (project.parent / ".env").write_text("A=1\n", encoding="utf-8")
    (project / ".env").write_text("A=2\n", encoding="utf-8")
    yield ".env in the folder and the parent", project
    top, project = tree("dir")
    (project / ".env").mkdir()
    (top / ".env").write_text("A=1\n", encoding="utf-8")
    yield ".env folder in the folder, file two up", project


def test_the_dotenv_is_found_upward_as_python_dotenv_finds_it(tmp_path, monkeypatch) -> None:
    """main.py's load_dotenv() runs find_dotenv from main.py's folder;
    find_dotenv(usecwd=True) from that folder is the same walk."""
    import dotenv

    update_engine = _engine()
    for name, project in _dotenv_trees(tmp_path):
        monkeypatch.chdir(project)
        found = dotenv.find_dotenv(usecwd=True)
        expected = Path(found) if found else None
        assert setup_venv.find_dotenv_path(project) == expected, name
        assert update_engine.find_dotenv_path(project) == expected, name
    parent = tmp_path / "parent" / "parent" / ".env"
    assert setup_venv.find_dotenv_path(tmp_path / "parent" / "parent" / "SAIVerse") == parent


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
