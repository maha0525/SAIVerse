"""scripts/check_lock_platforms.py の判定を、ネットワーク無しで固定する。

固定する不変条件:
- wheel の適合判定は pip と同じ packaging.tags で行う (py312 を 3.11 に受け付けない、
  素の linux_x86_64 wheel を拒まない、macOS の床 (x86_64=13.0 / arm64=14.0) を守る)
- PyPI の照会に失敗した pin は FAIL であって WARN ではない (fail-closed)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_lock_platforms.py"
_spec = importlib.util.spec_from_file_location("check_lock_platforms", _SCRIPT)
assert _spec is not None and _spec.loader is not None
clp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clp)

ALL_TARGETS = [(platform, py) for platform in clp.PLATFORMS for py in clp.PYTHONS]


def _fits_on(filename: str) -> set[tuple[str, str]]:
    return {(platform, py) for platform, py in ALL_TARGETS if clp._wheel_fits(filename, platform, py)}


def test_pure_python_wheel_fits_every_target() -> None:
    assert _fits_on("foo-1.0-py3-none-any.whl") == set(ALL_TARGETS)


def test_py312_tag_is_not_accepted_for_python_311() -> None:
    fits = _fits_on("foo-1.0-py312-none-any.whl")
    assert not any(py == "3.11" for _, py in fits)
    assert {py for _, py in fits} == {"3.12", "3.13"}
    assert {platform for platform, _ in fits} == set(clp.PLATFORMS)


def test_plain_linux_x86_64_wheel_fits_only_that_interpreter() -> None:
    assert _fits_on("foo-1.0-cp311-cp311-linux_x86_64.whl") == {("linux_x86_64", "3.11")}


def test_abi3_windows_wheel_fits_that_python_and_newer() -> None:
    assert _fits_on("foo-1.0-cp311-abi3-win_amd64.whl") == {("win_amd64", py) for py in ("3.11", "3.12", "3.13")}


def test_macos_x86_64_floor_is_13() -> None:
    assert _fits_on("foo-1.0-cp313-cp313-macosx_14_0_x86_64.whl") == set()
    assert _fits_on("foo-1.0-cp313-cp313-macosx_13_0_x86_64.whl") == {("macos_x86_64", "3.13")}


def test_macos_arm64_accepts_older_deployment_targets() -> None:
    assert _fits_on("foo-1.0-cp313-cp313-macosx_11_0_arm64.whl") == {("macos_arm64", "3.13")}


def test_universal2_wheel_fits_both_macos_targets() -> None:
    assert _fits_on("foo-1.0-cp313-cp313-macosx_10_9_universal2.whl") == {
        ("macos_x86_64", "3.13"),
        ("macos_arm64", "3.13"),
    }


def test_non_wheel_and_garbage_filenames_never_fit() -> None:
    assert _fits_on("foo-1.0.tar.gz") == set()
    assert _fits_on("not-a-wheel.whl") == set()


def test_requires_python_exclusion() -> None:
    assert clp._python_excluded(">=3.12", "3.11") is True
    assert clp._python_excluded(">=3.12", "3.12") is False
    assert clp._python_excluded(None, "3.11") is False
    assert clp._python_excluded("garbage", "3.11") is False  # unreadable: not treated as excluded


def _write_lock(tmp_path: Path, text: str) -> Path:
    lock = tmp_path / "requirements.lock"
    lock.write_text(text, encoding="utf-8")
    return lock


def test_pypi_lookup_failure_is_a_fail_not_a_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "foo==1.0\n    # via -r requirements.txt\n")
    monkeypatch.setattr(clp, "_release_on_pypi", lambda name, version: None)

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "FAIL foo==1.0: PyPI lookup failed" in out
    assert "WARN" not in out
    assert "installable on every target platform" not in out


def test_all_targets_covered_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "foo==1.0\n")
    monkeypatch.setattr(
        clp, "_release_on_pypi", lambda name, version: clp.PyPIRelease(["foo-1.0-py3-none-any.whl"], ">=3.9")
    )

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 0
    assert "every pin was fetched" in capsys.readouterr().out


def test_sdist_only_is_a_warn_but_requires_python_exclusion_is_a_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "foo==1.0\nbar==2.0\n")
    releases = {
        "foo": clp.PyPIRelease(["foo-1.0.tar.gz"], None),
        "bar": clp.PyPIRelease(["bar-2.0-py3-none-any.whl", "bar-2.0.tar.gz"], ">=3.12"),
    }
    monkeypatch.setattr(clp, "_release_on_pypi", lambda name, version: releases[name])

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "WARN foo==1.0: no wheel for win_amd64 / Python 3.11 (sdist only: needs a build)" in out
    assert "FAIL bar==2.0: Requires-Python >=3.12 excludes Python 3.11" in out
    assert "FAIL bar==2.0: Requires-Python >=3.12 excludes Python 3.12" not in out


def test_marker_excluded_platform_is_not_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "pywin32==311 ; sys_platform == 'win32'\n")
    monkeypatch.setattr(
        clp,
        "_release_on_pypi",
        lambda name, version: clp.PyPIRelease(
            [f"pywin32-311-cp{py}-cp{py}-win_amd64.whl" for py in ("311", "312", "313")], None
        ),
    )

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 0
    assert "WARN" not in capsys.readouterr().out
