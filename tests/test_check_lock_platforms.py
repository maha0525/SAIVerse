"""scripts/check_lock_platforms.py の判定を、ネットワーク無しで固定する。

固定する不変条件:
- wheel の適合判定は pip と同じ packaging.tags で行う (py312 を 3.11 に受け付けない、
  素の linux_x86_64 wheel を拒まない、macOS の床 (x86_64=13.0 / arm64=14.0) を守る)
- Linux の床は glibc 2.31 (manylinux_2_35 しか無い wheel は入らない、2_28 は入る)
- Requires-Python はファイル単位で見る、yanked されたファイルは候補に数えない
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


def test_linux_glibc_floor_rejects_wheels_newer_than_the_floor() -> None:
    assert clp.LINUX_GLIBC_FLOOR == (2, 31)
    # glibc 2.31 cannot load a wheel built against 2.35: that wheel counts as absent.
    assert _fits_on("foo-1.0-cp311-cp311-manylinux_2_35_x86_64.whl") == set()
    assert _fits_on("foo-1.0-cp311-cp311-manylinux_2_28_x86_64.whl") == {("linux_x86_64", "3.11")}
    assert _fits_on("foo-1.0-cp311-cp311-manylinux_2_31_x86_64.whl") == {("linux_x86_64", "3.11")}
    # The legacy aliases all name a glibc below the floor.
    assert _fits_on("foo-1.0-cp311-cp311-manylinux2014_x86_64.whl") == {("linux_x86_64", "3.11")}
    assert _fits_on("foo-1.0-cp311-cp311-manylinux1_x86_64.whl") == {("linux_x86_64", "3.11")}
    assert _fits_on("foo-1.0-cp311-cp311-musllinux_1_2_x86_64.whl") == set()


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


def _release(*filenames: str, requires_python: str | None = None, **per_file) -> "clp.PyPIRelease":  # type: ignore[no-untyped-def]
    """A PyPI release whose files carry no Requires-Python of their own unless
    ``per_file`` maps a filename to ``(requires_python, yanked)``."""
    files = []
    for filename in filenames:
        file_requires, yanked = per_file.get(filename, (None, False))
        files.append(clp.PyPIFile(filename, file_requires, yanked))
    return clp.PyPIRelease(files, requires_python)


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
        clp, "_release_on_pypi", lambda name, version: _release("foo-1.0-py3-none-any.whl", requires_python=">=3.9")
    )

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 0
    assert "every pin was fetched" in capsys.readouterr().out


def test_sdist_only_is_a_warn_but_requires_python_exclusion_is_a_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "foo==1.0\nbar==2.0\n")
    releases = {
        "foo": _release("foo-1.0.tar.gz"),
        # No per-file value: the release-level Requires-Python is the fallback.
        "bar": _release("bar-2.0-py3-none-any.whl", "bar-2.0.tar.gz", requires_python=">=3.12"),
    }
    monkeypatch.setattr(clp, "_release_on_pypi", lambda name, version: releases[name])

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "WARN foo==1.0: no wheel for win_amd64 / Python 3.11 (sdist only: needs a build)" in out
    assert "FAIL bar==2.0: Requires-Python >=3.12 excludes Python 3.11" in out
    assert "FAIL bar==2.0: Requires-Python >=3.12 excludes Python 3.12" not in out


def test_requires_python_is_judged_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The 3.11 wheel allows 3.11 even though the release says >=3.12; the
    3.12 wheel does not rescue 3.11; the sdist's own >=3.12 means 3.11 has no
    sdist to fall back on either."""
    lock = _write_lock(tmp_path, "foo==1.0\nbar==2.0\n")
    releases = {
        "foo": _release(
            "foo-1.0-py311-none-any.whl",
            "foo-1.0-py312-none-any.whl",
            requires_python=">=3.12",
            **{"foo-1.0-py311-none-any.whl": (">=3.11", False)},
        ),
        # A wheel that fits nowhere on 3.11 plus an sdist whose own Requires-Python excludes 3.11.
        "bar": _release(
            "bar-2.0-py312-none-any.whl",
            "bar-2.0.tar.gz",
            requires_python=">=3.11",
            **{"bar-2.0.tar.gz": (">=3.12", False)},
        ),
    }
    monkeypatch.setattr(clp, "_release_on_pypi", lambda name, version: releases[name])

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "foo==1.0" not in out  # every target has a wheel whose own Requires-Python allows it
    assert (
        "FAIL bar==2.0: no wheel for win_amd64 / Python 3.11 "
        "(the sdist's Requires-Python excludes this Python: install fails)"
    ) in out
    assert "WARN bar==2.0" not in out
    assert "bar==2.0: no wheel for win_amd64 / Python 3.12" not in out


def test_yanked_files_are_not_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "foo==1.0\nbar==2.0\n")
    releases = {
        # The only wheel is yanked; the sdist remains -> WARN, not a pass.
        "foo": _release(
            "foo-1.0-py3-none-any.whl",
            "foo-1.0.tar.gz",
            **{"foo-1.0-py3-none-any.whl": (None, True)},
        ),
        # Everything yanked -> FAIL once, not per target.
        "bar": _release("bar-2.0-py3-none-any.whl", **{"bar-2.0-py3-none-any.whl": (None, True)}),
    }
    monkeypatch.setattr(clp, "_release_on_pypi", lambda name, version: releases[name])

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "WARN foo==1.0: no wheel for win_amd64 / Python 3.11 (sdist only: needs a build)" in out
    assert out.count("FAIL bar==2.0: all files yanked") == 1
    assert "bar==2.0: no wheel" not in out


def test_marker_excluded_platform_is_not_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock = _write_lock(tmp_path, "pywin32==311 ; sys_platform == 'win32'\n")
    monkeypatch.setattr(
        clp,
        "_release_on_pypi",
        lambda name, version: _release(*[f"pywin32-311-cp{py}-cp{py}-win_amd64.whl" for py in ("311", "312", "313")]),
    )

    assert clp.main(["check_lock_platforms.py", str(lock)]) == 0
    assert "WARN" not in capsys.readouterr().out
