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
    # The record of a set-aside venv (.setup_in_progress/) goes to tmp_path,
    # not to this checkout.
    monkeypatch.setattr(installer, "WORKSPACE_ROOT", tmp_path)
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
    # Made with this Python, so it is reinstalled in place rather than set aside.
    (config.venv_directory() / "pyvenv.cfg").write_text(
        f"version = {sys.version_info[0]}.{sys.version_info[1]}.0\n", encoding="utf-8"
    )
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


def _installed_venv(series: str) -> None:
    """A SearXNG install whose markers match, in a venv made with Python ``series``."""
    source = config.source_directory()
    source.mkdir()
    (source / installer.SOURCE_MARKER).write_text(json.dumps(config.pinned_release()), encoding="utf-8")
    config.venv_python().parent.mkdir(parents=True)
    config.venv_python().touch()
    (config.venv_directory() / "pyvenv.cfg").write_text(f"version = {series}.0\n", encoding="utf-8")
    (config.venv_directory() / installer.INSTALL_MARKER).write_text(
        json.dumps(installer.install_identity()), encoding="utf-8",
    )


_THIS_SERIES = f"{sys.version_info[0]}.{sys.version_info[1]}"


def test_marker_alone_does_not_hide_failed_runtime_import(installation, monkeypatch):
    _installed_venv(_THIS_SERIES)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, stdout="", stderr="ModuleNotFoundError: No module named 'tzdata'",
    ))
    assert installer.ready() is False


def test_a_matching_install_of_this_python_series_is_ready(installation, monkeypatch):
    _installed_venv(_THIS_SERIES)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    assert installer.ready() is True


def test_an_install_of_another_python_series_is_not_ready(installation, monkeypatch, capsys):
    """Run with SAIVerse's python (setup, the update, the launchers), the check
    fails for a SearXNG venv left on the series SAIVerse ran before, so setup
    makes it again (an update alone otherwise kept it until the next setup)."""
    _installed_venv("3.12" if _THIS_SERIES != "3.12" else "3.11")
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    assert installer.ready() is False
    assert f"SAIVerse runs Python {_THIS_SERIES}" in capsys.readouterr().out


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
    (directory / "pyvenv.cfg").write_text(f"home = C:\\Python\nversion = {version}\n", encoding="utf-8")


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


def _setup_failing_at(monkeypatch, tmp_path: Path, failing_step: str) -> None:
    """``setup`` with the source steps skipped and every command answered:
    the command whose description contains ``failing_step`` fails."""
    monkeypatch.setattr(installer, "prepare_source", lambda: None)
    monkeypatch.setattr(installer, "prepare_compatibility", lambda: None)
    monkeypatch.setattr(installer, "prepare_requirements", lambda: None)
    monkeypatch.setattr(installer, "ready", lambda: False)
    # The limiter copy is the only file the rest of setup writes outside the venv.
    monkeypatch.setattr(installer, "configured_path", lambda name, default: tmp_path / "limiter.toml")
    monkeypatch.setattr(installer.shutil, "copyfile", lambda src, dst: None)

    def run(args, **kwargs):  # noqa: ANN001
        words = [str(a) for a in args]
        if (failing_step == "probe" and "-c" in words) or (
            failing_step != "probe" and failing_step in " ".join(words)
        ):
            raise subprocess.CalledProcessError(23, args)
        if args[1:3] == ["-m", "venv"]:
            venv = Path(args[3])
            python = venv / ("Scripts/python.exe" if installer.os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            (venv / "pyvenv.cfg").write_text(
                f"version = {sys.version_info[0]}.{sys.version_info[1]}.0\n", encoding="utf-8"
            )

    monkeypatch.setattr(installer, "run", run)


@pytest.mark.parametrize("failing_step", ["-m venv", "pip install -r", "probe"])
def test_a_failed_install_after_the_set_aside_puts_the_previous_venv_back(
    installation, tmp_path, monkeypatch, failing_step
):
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    (venv / "kept.txt").write_text("the venv that worked", encoding="utf-8")
    _setup_failing_at(monkeypatch, tmp_path, failing_step)

    with pytest.raises(subprocess.CalledProcessError):
        installer.setup()

    assert (venv / "kept.txt").read_text(encoding="utf-8") == "the venv that worked"
    assert installer.venv_series(venv) == "3.12"
    assert not list(tmp_path.glob("venv.old-*"))


def test_a_successful_install_removes_the_set_aside_venv(installation, tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    _setup_failing_at(monkeypatch, tmp_path, "nothing fails")

    installer.setup()

    assert installer.venv_series(venv) == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert not list(tmp_path.glob("venv.old-*"))


# --- the record of the set-aside venv: a setup stopped part-way is continued ----------


def _set_aside_entry(root: Path) -> dict | None:
    return installer.setup_in_progress.read_entry(root, installer.SET_ASIDE_ENTRY)


def test_the_set_aside_is_recorded_before_the_rename(installation, tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    renames = []
    real = installer.os.replace

    def replace(src, dst):  # noqa: ANN001
        if Path(src) == venv:  # (the record itself is also written with os.replace)
            renames.append(_set_aside_entry(tmp_path))
        real(src, dst)

    monkeypatch.setattr(installer.os, "replace", replace)
    aside = installer.set_aside_venv_of_other_series()
    assert renames[0] == {"venv": str(venv), "aside": str(aside)}
    assert _set_aside_entry(tmp_path) == {"venv": str(venv), "aside": str(aside)}


def test_a_refused_set_aside_removes_its_record(installation, tmp_path, monkeypatch):
    _venv_with_series(tmp_path / "venv", "3.12.10")
    monkeypatch.setattr(installer.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "in use")))
    with pytest.raises(RuntimeError):
        installer.set_aside_venv_of_other_series()
    assert _set_aside_entry(tmp_path) is None


def _stopped_after_the_set_aside(tmp_path: Path) -> Path:
    """A setup that set the 3.12 venv aside, made a venv with this Python and
    was killed (the window closed) before pip finished: no handler ran."""
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    (venv / "kept.txt").write_text("the venv that worked", encoding="utf-8")
    aside = installer.set_aside_venv_of_other_series()
    _venv_with_series(venv, f"{sys.version_info[0]}.{sys.version_info[1]}.0")
    return aside


def test_a_record_makes_the_check_fail_so_the_launcher_runs_setup(installation, tmp_path):
    _stopped_after_the_set_aside(tmp_path)
    assert installer.ready() is False


@pytest.mark.parametrize("failing_step", ["pip install -r", "probe"])
def test_a_setup_after_a_stopped_one_puts_the_set_aside_venv_back_when_it_fails(
    installation, tmp_path, monkeypatch, failing_step
):
    """The venv in place is of this Python's series, so set_aside_venv_of_other_series
    alone would not see it; the record still names the venv that worked."""
    aside = _stopped_after_the_set_aside(tmp_path)
    _setup_failing_at(monkeypatch, tmp_path, failing_step)
    with pytest.raises(subprocess.CalledProcessError):
        installer.setup()
    venv = tmp_path / "venv"
    assert (venv / "kept.txt").read_text(encoding="utf-8") == "the venv that worked"
    assert not aside.exists()
    assert _set_aside_entry(tmp_path) is None


def test_a_setup_after_a_stopped_one_installs_again_and_removes_the_set_aside_venv(
    installation, tmp_path, monkeypatch
):
    _stopped_after_the_set_aside(tmp_path)
    _setup_failing_at(monkeypatch, tmp_path, "nothing fails")
    installer.setup()
    assert installer.venv_series(tmp_path / "venv") == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert not list(tmp_path.glob("venv.old-*")) and not list(tmp_path.glob("venv.deleting-*"))
    assert _set_aside_entry(tmp_path) is None


def test_a_half_made_venv_of_another_series_is_made_again(installation, tmp_path, monkeypatch):
    """The stopped setup ran with another Python than this one: its half-made
    venv is removed and made again with this Python."""
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.11.9")
    aside = installer.set_aside_venv_of_other_series()
    _venv_with_series(venv, "3.12.10")
    (venv / "half.txt").write_text("", encoding="utf-8")
    _setup_failing_at(monkeypatch, tmp_path, "nothing fails")
    installer.setup()
    assert not (venv / "half.txt").exists()
    assert installer.venv_series(venv) == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert not aside.exists()


def test_a_record_whose_set_aside_folder_is_gone_is_dropped(installation, tmp_path):
    """Stopped between writing the record and the rename: the venv in place
    is still the one of the other series, and is set aside as usual."""
    venv = tmp_path / "venv"
    _venv_with_series(venv, "3.12.10")
    installer.setup_in_progress.write_entry(
        tmp_path, installer.SET_ASIDE_ENTRY, {"venv": str(venv), "aside": str(tmp_path / "venv.old-gone")})
    assert installer.set_aside_venv_of_stopped_setup() is None
    assert _set_aside_entry(tmp_path) is None
    assert installer.set_aside_venv_of_other_series() is not None


def test_a_set_aside_venv_stopped_while_being_deleted_is_not_put_back(installation, tmp_path, monkeypatch):
    """It is renamed before it is deleted and the record goes with the rename,
    so a half-deleted venv is never named as the one to put back."""
    aside = _stopped_after_the_set_aside(tmp_path)
    _setup_failing_at(monkeypatch, tmp_path, "nothing fails")

    class Stop(BaseException):
        pass

    real_rmtree = installer.shutil.rmtree

    def stop_on_deleting(path, *args, **kwargs):  # noqa: ANN001
        if ".deleting-" in Path(path).name:
            raise Stop
        return real_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(installer.shutil, "rmtree", stop_on_deleting)
        with pytest.raises(Stop):
            installer.setup()
    assert not aside.exists()
    assert _set_aside_entry(tmp_path) is None
    assert len(list(tmp_path.glob("venv.deleting-*"))) == 1
    installer.setup()  # the next one deletes what is left
    assert not list(tmp_path.glob("venv.deleting-*"))


def _run_main(monkeypatch, action: str) -> int:
    monkeypatch.setattr(sys, "argv", ["searxng_install.py", action])
    return installer.main()


def test_main_sets_up_the_https_trust_before_any_action(monkeypatch):
    """This process downloads the SearXNG source with urllib and starts pip,
    outside main.py's call of ensure_default_https_trust (saiverse/tls_trust.py)."""
    calls: list[str] = []
    monkeypatch.setattr(installer, "ensure_default_https_trust", lambda: calls.append("trust"))
    monkeypatch.setattr(installer, "ready", lambda: calls.append("ready") or True)
    assert _run_main(monkeypatch, "check") == 0
    assert calls == ["trust", "ready"]


def test_a_failed_setup_is_recorded_for_the_banner_and_cleared_by_the_next_success(tmp_path, monkeypatch):
    from saiverse.environment_alerts import SEARXNG_SETUP_ALERT_ID, searxng_setup_alerts

    monkeypatch.delenv(installer.SKIP_FAILURE_RECORD_ENV, raising=False)
    monkeypatch.setattr(installer, "WORKSPACE_ROOT", tmp_path)

    def fail():
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(installer, "setup", fail)
    assert _run_main(monkeypatch, "setup") == 1
    record = tmp_path / installer.SETUP_FAILURE_RECORD
    assert "github unreachable" in json.loads(record.read_text(encoding="utf-8"))["error"]
    alerts = searxng_setup_alerts(tmp_path)
    assert [a["id"] for a in alerts] == [SEARXNG_SETUP_ALERT_ID]
    assert "github unreachable" in alerts[0]["details"]["error"]
    assert "setup.bat" in alerts[0]["message"]
    # The banner does not remove it: only a finished setup does.
    assert record.is_file()

    monkeypatch.setattr(installer, "setup", lambda: None)
    assert _run_main(monkeypatch, "setup") == 0
    assert not record.exists()
    assert searxng_setup_alerts(tmp_path) == []


def test_the_update_engine_keeps_the_failure_in_its_own_record(tmp_path, monkeypatch):
    """refresh_searxng records the failure itself; a second banner would repeat it."""
    from scripts import update_engine

    monkeypatch.setenv(installer.SKIP_FAILURE_RECORD_ENV, "1")
    monkeypatch.setattr(installer, "WORKSPACE_ROOT", tmp_path)

    def fail():
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(installer, "setup", fail)
    assert _run_main(monkeypatch, "setup") == 1
    assert not (tmp_path / installer.SETUP_FAILURE_RECORD).exists()
    assert update_engine.SEARXNG_SKIP_FAILURE_RECORD_ENV == installer.SKIP_FAILURE_RECORD_ENV


def test_the_setup_record_name_matches_the_banner_and_is_ignored_by_git():
    from saiverse import environment_alerts

    assert environment_alerts.SEARXNG_SETUP_FAILURE_RECORD == installer.SETUP_FAILURE_RECORD
    ignored = (config.WORKSPACE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".searxng_*_failed.json*" in ignored
    assert installer.SETUP_FAILURE_RECORD.startswith(".searxng_")
    assert installer.SETUP_FAILURE_RECORD.endswith("_failed.json")


def test_venv_series_reads_version_and_version_info(tmp_path):
    a = tmp_path / "a"
    _venv_with_series(a, "3.13.17")
    assert installer.venv_series(a) == "3.13"
    b = tmp_path / "b"
    b.mkdir()
    (b / "pyvenv.cfg").write_text("version_info = 3.12.4.final.0\n", encoding="utf-8")
    assert installer.venv_series(b) == "3.12"
    assert installer.venv_series(tmp_path / "missing") is None
