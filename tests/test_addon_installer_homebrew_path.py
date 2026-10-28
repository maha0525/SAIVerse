"""macOS で、アドオンの導入のサブプロセスの PATH に Homebrew の bin を足すこと。

Homebrew の bin (``/opt/homebrew/bin`` 等) は macOS の既定の PATH に無く、
``brew shellenv`` を通した shell から起動したときだけ PATH に入る。
``_run_subprocess`` は親プロセスの PATH を子へ継ぐので、それ以外の起動元では
pip のソースビルド (python_mecab_ko の ``mecab-config`` 等) が Homebrew の
コマンドを見つけられなかった。``os.path.isdir`` と ``subprocess.Popen`` は
差し替え、実在のディレクトリや本物のプロセスには依存しない。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from saiverse import addon_installer

_REAL_ISDIR = os.path.isdir


def _isdir_only(*existing: str):  # type: ignore[no-untyped-def]
    def isdir(path):  # type: ignore[no-untyped-def]
        if path in addon_installer._HOMEBREW_BIN_DIRS:
            return path in existing
        return _REAL_ISDIR(path)

    return isdir


def _join(*entries: str) -> str:
    return os.pathsep.join(entries)


def test_an_existing_homebrew_bin_is_added_at_the_end() -> None:
    with patch("os.path.isdir", side_effect=_isdir_only("/opt/homebrew/bin")):
        result = addon_installer._path_with_homebrew_bins(_join("/env/bin", "/usr/bin"))
    assert result == _join("/env/bin", "/usr/bin", "/opt/homebrew/bin")


def test_a_bin_already_on_path_is_not_added_again() -> None:
    existing = addon_installer._HOMEBREW_BIN_DIRS
    with patch("os.path.isdir", side_effect=_isdir_only(*existing)):
        result = addon_installer._path_with_homebrew_bins(_join("/usr/local/bin", "/usr/bin"))
    assert result == _join("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin")
    assert result.split(os.pathsep).count("/usr/local/bin") == 1


def test_a_missing_directory_is_not_added() -> None:
    with patch("os.path.isdir", side_effect=_isdir_only()):
        assert addon_installer._path_with_homebrew_bins(_join("/env/bin", "/usr/bin")) == _join(
            "/env/bin", "/usr/bin")


def test_the_original_entries_keep_their_order() -> None:
    original = _join("/z/bin", "/a/bin", "/m/bin")
    with patch("os.path.isdir", side_effect=_isdir_only(*addon_installer._HOMEBREW_BIN_DIRS)):
        result = addon_installer._path_with_homebrew_bins(original)
    assert result.split(os.pathsep)[:3] == ["/z/bin", "/a/bin", "/m/bin"]
    assert result.split(os.pathsep)[3:] == list(addon_installer._HOMEBREW_BIN_DIRS)


def test_an_empty_path_becomes_the_existing_bins() -> None:
    with patch("os.path.isdir", side_effect=_isdir_only("/opt/homebrew/bin")):
        assert addon_installer._path_with_homebrew_bins("") == "/opt/homebrew/bin"


def _path_given_to_popen(monkeypatch, platform: str, tmp_path: Path) -> str:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(addon_installer.sys, "platform", platform)
    monkeypatch.setenv("PATH", _join("/env/bin", "/usr/bin"))
    proc = MagicMock()
    proc.stdout = iter([])
    proc.wait.return_value = 0
    with patch("os.path.isdir", side_effect=_isdir_only("/opt/homebrew/bin")), patch.object(
        addon_installer.subprocess, "Popen", return_value=proc
    ) as popen:
        addon_installer._run_subprocess(
            ["pip", "install", "x"], cwd=tmp_path, progress=lambda _e: None, label="pip")
    return popen.call_args.kwargs["env"]["PATH"]


def test_run_subprocess_adds_homebrew_bins_on_macos(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = _path_given_to_popen(monkeypatch, "darwin", tmp_path)
    assert path.split(os.pathsep) == ["/env/bin", "/usr/bin", "/opt/homebrew/bin"]


def test_run_subprocess_leaves_path_alone_elsewhere(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert _path_given_to_popen(monkeypatch, "linux", tmp_path) == _join("/env/bin", "/usr/bin")
