"""setup.bat / setup.sh の .venv の確かめと作り直し (scripts/setup_venv.py)。

設計: docs/intent/dependency_management.md §2-7。.venv は tmp_path の中に偽物を
作る (本物の venv は作らない)。改名の失敗は os.rename を差し替えて起こす。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
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
