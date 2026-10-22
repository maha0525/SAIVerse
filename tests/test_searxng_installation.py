"""Contracts for pinned source delivery and incomplete-install rejection."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import download_searxng_source as download  # noqa: E402
import searxng_config as config  # noqa: E402
import searxng_install as installer  # noqa: E402


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARXNG_REF", raising=False)
    monkeypatch.setenv("SEARXNG_SRC_DIR", str(tmp_path / "source with spaces"))
    monkeypatch.setenv("SEARXNG_VENV_DIR", str(tmp_path / "venv"))
    return tmp_path


@pytest.mark.parametrize("ref", ["master", "v1.0", "ff8f5dd", "../master"])
def test_floating_or_incomplete_refs_are_rejected(monkeypatch, ref):
    monkeypatch.setenv("SEARXNG_REF", ref)
    with pytest.raises(ValueError, match="commit SHA"):
        config.pinned_release()


def test_source_default_contains_the_exact_selected_commit(monkeypatch):
    monkeypatch.delenv("SEARXNG_SRC_DIR", raising=False)
    monkeypatch.delenv("SEARXNG_REF", raising=False)
    commit = config.pinned_release()["commit"]
    assert config.source_directory() == config.SCRIPT_DIR / ".searxng-src" / commit
    assert download._build_zip_url(commit).endswith(f"/archive/{commit}.zip")


def test_selective_archive_contains_all_build_inputs(installation, monkeypatch):
    commit = config.pinned_release()["commit"]
    archive = installation / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in installer.REQUIRED_FILES:
            zf.writestr(f"searxng-{commit}/{name}", "package input")
        zf.writestr(f"searxng-{commit}/utils/windows:invalid", "excluded")
    monkeypatch.setattr(download, "_download_zip", lambda ref: str(archive))

    installer.prepare_source()

    source = config.source_directory()
    assert all((source / name).is_file() for name in installer.REQUIRED_FILES)
    assert installer.read_json(source / installer.SOURCE_MARKER) == {"commit": commit}
    assert not (source / "utils").exists()


def test_wrong_commit_archive_is_rejected_without_publishing_source(installation, monkeypatch):
    archive = installation / "wrong.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("searxng-master/requirements.txt", "")
    monkeypatch.setattr(download, "_download_zip", lambda ref: str(archive))
    with pytest.raises(ValueError, match="does not match commit"):
        installer.prepare_source()
    assert not config.source_directory().exists()


def test_incomplete_archive_is_rejected_without_publishing_source(installation, monkeypatch):
    archive = installation / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"searxng-{config.pinned_release()['commit']}/requirements.txt", "")
    monkeypatch.setattr(download, "_download_zip", lambda ref: str(archive))
    with pytest.raises(RuntimeError, match="archive is missing"):
        installer.prepare_source()
    assert not config.source_directory().exists()


def test_unmanaged_source_is_preserved(installation):
    source = config.source_directory()
    source.mkdir()
    original = source / "requirements.txt"
    original.write_text("user modifications", encoding="utf-8")
    with pytest.raises(RuntimeError, match="preserving it"):
        installer.prepare_source()
    assert original.read_text(encoding="utf-8") == "user modifications"


@pytest.mark.parametrize("relative", ["../outside", "searx/../../outside", "searx\\..\\outside"])
def test_archive_paths_cannot_escape_destination(relative):
    with pytest.raises(ValueError, match="Unsafe path"):
        download._should_extract("root/" + relative, "root/", selective=True)


def test_failed_pip_does_not_leave_completion_marker(installation, monkeypatch):
    config.venv_python().parent.mkdir(parents=True)
    config.venv_python().touch()
    marker = config.venv_directory() / installer.INSTALL_MARKER
    marker.write_text("old completion record", encoding="utf-8")
    monkeypatch.setattr(installer, "prepare_source", lambda: None)
    monkeypatch.setattr(installer, "prepare_compatibility", lambda: None)
    monkeypatch.setattr(installer, "prepare_requirements", lambda: None)
    monkeypatch.setattr(installer, "ready", lambda: False)

    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(23, args)

    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.setup()
    assert not marker.exists()


def test_marker_alone_does_not_hide_failed_runtime_import(installation, monkeypatch):
    source = config.source_directory()
    source.mkdir()
    (source / installer.SOURCE_MARKER).write_text(json.dumps(config.pinned_release()), encoding="utf-8")
    config.venv_python().parent.mkdir(parents=True)
    config.venv_python().touch()
    (config.venv_directory() / installer.INSTALL_MARKER).write_text(
        json.dumps(installer.install_identity()), encoding="utf-8",
    )
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, stdout="", stderr="ModuleNotFoundError: No module named 'tzdata'",
    ))
    assert installer.ready() is False


UPSTREAM_REQUIREMENTS = """certifi==2025.11.12
babel==2.17.0
httpx[http2]==0.28.1
tomli==2.3.0; python_version < '3.11'
"""


def test_requirement_floors_replace_certifi_and_append_anyio():
    updated = installer.apply_requirement_floors(UPSTREAM_REQUIREMENTS)
    lines = updated.splitlines()
    assert lines[0] == f"certifi>={installer.REQUIREMENT_FLOORS['certifi']}"
    assert "certifi==2025.11.12" not in lines
    assert lines[-1] == f"anyio>={installer.REQUIREMENT_FLOORS['anyio']}"
    assert lines[1:4] == UPSTREAM_REQUIREMENTS.splitlines()[1:4]


def test_requirement_floors_pin_the_fixed_releases():
    """GHSA-6q48-6c7w-9gw2 (certifi) / CVE-2026-63374 (anyio) の修正版が下限。"""
    assert installer.REQUIREMENT_FLOORS == {"certifi": "2026.10.19", "anyio": "4.14.2"}


def test_requirement_floors_are_idempotent():
    once = installer.apply_requirement_floors(UPSTREAM_REQUIREMENTS)
    assert installer.apply_requirement_floors(once) == once


def test_requirement_floors_replace_existing_anyio_pin_once():
    updated = installer.apply_requirement_floors("anyio==4.0.0\nCertifi==2025.1.1\ncertifi==2024.1.1\n")
    assert updated == "anyio>=4.14.2\ncertifi>=2026.10.19\n"


def test_prepare_requirements_rewrites_source_file(installation):
    source = config.source_directory()
    source.mkdir()
    target = source / "requirements.txt"
    target.write_text(UPSTREAM_REQUIREMENTS, encoding="utf-8")

    installer.prepare_requirements()
    first = target.read_text(encoding="utf-8")
    installer.prepare_requirements()

    assert first == installer.apply_requirement_floors(UPSTREAM_REQUIREMENTS)
    assert target.read_text(encoding="utf-8") == first
    assert sorted(p.name for p in source.iterdir()) == ["requirements.txt"]


def _venv_with_series(directory: Path, version: str) -> None:
    directory.mkdir(parents=True)
    (directory / "pyvenv.cfg").write_text(f"home = C:\Python\nversion = {version}\n", encoding="utf-8")


def test_a_venv_of_another_python_series_is_set_aside(installation, tmp_path):
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    aside = installer.set_aside_venv_of_other_series()
    assert aside is not None and aside.is_dir()
    assert not venv.exists()
    assert aside.name.startswith("venv.old-")


def test_a_venv_of_the_same_series_is_kept(installation, tmp_path):
    venv = tmp_path / "venv"
    _venv_with_series(venv, f"{sys.version_info[0]}.{sys.version_info[1]}.99")
    assert installer.set_aside_venv_of_other_series() is None
    assert venv.is_dir()


def test_no_venv_is_nothing_to_set_aside(installation):
    assert installer.set_aside_venv_of_other_series() is None


def test_a_venv_that_cannot_be_renamed_stops_setup_and_keeps_it(installation, tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")

    def refuse(src, dst):  # noqa: ANN001
        raise PermissionError(5, "in use")

    monkeypatch.setattr(installer.os, "replace", refuse)
    with pytest.raises(RuntimeError, match="SearXNG"):
        installer.set_aside_venv_of_other_series()
    assert venv.is_dir()


def test_venv_series_reads_version_and_version_info(tmp_path):
    a = tmp_path / "a"
    _venv_with_series(a, "3.13.17")
    assert installer.venv_series(a) == "3.13"
    b = tmp_path / "b"
    b.mkdir()
    (b / "pyvenv.cfg").write_text("version_info = 3.12.4.final.0\n", encoding="utf-8")
    assert installer.venv_series(b) == "3.12"
    assert installer.venv_series(tmp_path / "missing") is None
