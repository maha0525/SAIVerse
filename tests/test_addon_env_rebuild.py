"""本体の Python の系列が変わったあとの、アドオン専用の環境の作り直し (setup から呼ぶ)。

設計: docs/intent/dependency_management.md §2-7。venv の作成と pip は
``_run_subprocess`` を差し替えて、コマンド列だけを見る (本物の venv・pip・ネットワークは
使わない)。``SAIVERSE_HOME``・``addon_data``・失敗の記録ファイルは tmp_path に向ける。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from unittest.mock import patch

import pytest

from saiverse import addon_installer, addon_paths

STALE = "3.12.10"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setattr(addon_paths, "USER_DATA_DIR", home / "user_data")
    monkeypatch.setattr(addon_installer, "_PIP_UPGRADED_ENVS", set())
    monkeypatch.setattr(
        addon_installer, "ENV_REBUILD_FAILURES_PATH", tmp_path / ".addon_env_rebuild_failures.json")
    return home


@pytest.fixture
def expansion(tmp_path: Path) -> Path:
    path = tmp_path / "expansion_data"
    path.mkdir()
    return path


def _manifest(addon_id: str) -> dict:
    return {
        "name": addon_id,
        "display_name": "X",
        "version": "1.2.3",
        "manifest_version": 2,
        "setup_version": 4,
        "setup": {
            "options": [{
                "id": "engines", "question": "q", "multiple": True,
                "choices": [{"id": "cloud", "label": "c", "default": True},
                            {"id": "gpt", "label": "g"}],
            }],
            "steps": [
                {"name": "core-pkgs", "type": "pip_install", "requirements": "r.txt"},
                {"name": "cloud-pkgs", "type": "pip_install", "requirements": "r.txt",
                 "env": "eng", "when": {"engines": "cloud"}},
                {"name": "gpt-pkgs", "type": "pip_install", "requirements": "r.txt",
                 "env": "eng", "when": {"engines": "gpt"}},
                {"name": "gpt-core-pkgs", "type": "pip_install", "requirements": "r.txt",
                 "when": {"engines": "gpt"}},
                # env の無いスクリプト: 本体の .venv の入れ直しの対象にしない
                {"name": "make-config", "type": "python_script", "script": "s.py"},
            ],
        },
    }


def _install(expansion: Path, addon_id: str, answers: dict, env_python: str | None) -> Path:
    """導入済みの形のアドオンを作る (env_python が None なら専用の環境は無い)。"""
    addon_dir = expansion / addon_id
    addon_dir.mkdir()
    (addon_dir / "addon.json").write_text(json.dumps(_manifest(addon_id)), encoding="utf-8")
    (addon_dir / "r.txt").write_text("", encoding="utf-8")
    install_dir = addon_paths.get_addon_install_dir(addon_id)
    install_dir.mkdir(parents=True)
    (install_dir / addon_installer.SETUP_ANSWERS_NAME).write_text(
        json.dumps({"answers": answers}), encoding="utf-8")
    if env_python is not None:
        env_dir = addon_paths.get_addon_env_dir(addon_id, "eng")
        python = addon_paths.addon_env_python_path(env_dir)
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        (env_dir / "old-package.txt").write_text("", encoding="utf-8")
        (env_dir / addon_paths.ADDON_ENV_RECORD_NAME).write_text(
            json.dumps({"python_version": env_python}), encoding="utf-8")
    data_dir = addon_paths.get_addon_data_dir(addon_id)
    (data_dir / "state.db").write_text("persona data", encoding="utf-8")
    return addon_dir


def _fake_run(recorded: list):
    def fake_run(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        recorded.append({"args": list(args), "label": label, "cwd": str(cwd)})
        if args[1:3] == ["-m", "venv"]:
            python = addon_paths.addon_env_python_path(Path(args[3]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
    return fake_run


def _labels(recorded: list) -> list[str]:
    return [r["label"] for r in recorded]


def _snapshot(addon_id: str, addon_dir: Path) -> tuple[str, str, str]:
    install_dir = addon_paths.get_addon_install_dir(addon_id)
    return (
        (install_dir / addon_installer.SETUP_ANSWERS_NAME).read_text(encoding="utf-8"),
        (addon_dir / "addon.json").read_text(encoding="utf-8"),
        (addon_paths.get_addon_data_dir(addon_id) / "state.db").read_text(encoding="utf-8"),
    )


def test_env_of_another_series_is_rebuilt_with_the_saved_answers(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    addon_dir = _install(expansion, "a-addon", {"engines": ["cloud", "gpt"]}, STALE)
    before = _snapshot("a-addon", addon_dir)

    results = addon_installer.rebuild_unusable_addon_envs(expansion)

    assert [(r.addon_id, r.envs, r.steps, r.ok) for r in results] == [
        ("a-addon", ["eng"], ["cloud-pkgs", "gpt-pkgs"], True)]
    # 専用の環境を使う step だけを、保存された答えのとおり全部走らせる
    # (本体の venv に入れる core-pkgs は走らせない)
    assert [lbl for lbl in _labels(recorded) if not lbl.startswith(("venv ", "upgrade pip"))] == [
        "cloud-pkgs", "gpt-pkgs"]
    assert _labels(recorded).count("venv eng") == 1
    env_dir = addon_paths.get_addon_env_dir("a-addon", "eng")
    assert not (env_dir / "old-package.txt").exists()  # 空の venv から作り直した
    record = addon_paths.read_addon_env_record(env_dir)
    assert record["python_version"] == addon_paths.current_python_version()
    # 答え・アドオンのバージョン・アドオンのデータは変わらない
    assert _snapshot("a-addon", addon_dir) == before
    assert addon_paths.get_addon_env_python("a-addon", "eng") == addon_paths.addon_env_python_path(env_dir)


def test_env_of_the_current_series_is_not_touched(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, addon_paths.current_python_version())

    assert addon_installer.find_unusable_addon_envs(expansion) == {}
    assert addon_installer.rebuild_unusable_addon_envs(expansion) == []
    assert recorded == []
    assert (addon_paths.get_addon_env_dir("a-addon", "eng") / "old-package.txt").exists()


def test_a_patch_difference_is_not_a_rebuild(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    major, minor = addon_paths.python_minor_version(addon_paths.current_python_version())
    _install(expansion, "a-addon", {"engines": ["cloud"]}, f"{major}.{minor}.0")
    assert addon_installer.rebuild_unusable_addon_envs(expansion) == []
    assert recorded == []


def test_addon_without_an_env_folder_gets_none_created(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    assert addon_installer.rebuild_unusable_addon_envs(expansion) == []
    assert recorded == []
    assert not addon_paths.get_addon_env_dir("a-addon", "eng").exists()


def test_a_failure_does_not_stop_the_other_addons_and_is_recorded(home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    # a-addon の gpt-pkgs だけ失敗させる (step の名前は両方のアドオンで同じなので cwd で見分ける)
    def fake(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        _fake_run(recorded)(args, cwd, progress, label, env)
        if label == "gpt-pkgs" and Path(cwd).name == "a-addon":
            raise addon_installer.AddonInstallError("gpt-pkgs: 終了コード 1 で失敗")
    monkeypatch.setattr(addon_installer, "_run_subprocess", fake)
    a_dir = _install(expansion, "a-addon", {"engines": ["cloud", "gpt"]}, STALE)
    _install(expansion, "b-addon", {"engines": ["cloud"]}, STALE)
    before = _snapshot("a-addon", a_dir)

    results = addon_installer.rebuild_unusable_addon_envs(expansion)

    assert [(r.addon_id, r.ok) for r in results] == [("a-addon", False), ("b-addon", True)]
    assert "gpt-pkgs" in results[0].error
    # 失敗した環境には記録が無く、やりかけの記録が残る: 使えば「setup をやり直して」と
    # 断られ、次の setup で作り直される
    a_env = addon_paths.get_addon_env_dir("a-addon", "eng")
    assert addon_paths.read_addon_env_record(a_env) is None
    assert _entry_files("a-addon") == ["env-eng"]
    assert _entry_files("b-addon") == []
    with pytest.raises(addon_paths.AddonEnvError):
        addon_paths.get_addon_env_python("a-addon", "eng")
    assert addon_installer.find_unusable_addon_envs(expansion) == {"a-addon": ["eng"]}
    assert _snapshot("a-addon", a_dir) == before
    # 成功したアドオンの環境は使える
    addon_paths.get_addon_env_python("b-addon", "eng")

    path = addon_installer.record_env_rebuild_failures(results)
    assert path == tmp_path / ".addon_env_rebuild_failures.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["python_version"] == addon_paths.current_python_version()
    assert [(f["addon_id"], f["envs"], f["steps"]) for f in data["failures"]] == [
        ("a-addon", ["eng"], ["cloud-pkgs", "gpt-pkgs"])]
    assert "gpt-pkgs" in data["failures"][0]["error"]

    # 次の作り直しで失敗が無くなれば、記録のファイルは消える
    assert addon_installer.record_env_rebuild_failures([]) is None
    assert not path.exists()


def test_unreadable_manifest_is_reported_as_a_failure(home, expansion, monkeypatch) -> None:
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run([]))
    addon_dir = _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    (addon_dir / "addon.json").write_text("{broken", encoding="utf-8")
    results = addon_installer.rebuild_unusable_addon_envs(expansion)
    assert [(r.addon_id, r.ok) for r in results] == [("a-addon", False)]


_DOTENV_LOADS: list = []


def _load_entry():
    """Load the script with python-dotenv's load_dotenv replaced: the checkout's
    real .env (API keys) must not leak into the test process."""
    import dotenv

    script = Path(__file__).resolve().parent.parent / "scripts" / "rebuild_addon_envs.py"
    spec = importlib.util.spec_from_file_location("rebuild_addon_envs_under_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.object(dotenv, "load_dotenv", side_effect=lambda *a, **k: _DOTENV_LOADS.append((a, k))):
        spec.loader.exec_module(module)
    return module


def test_the_entry_loads_the_same_dotenv_as_main_before_resolving_paths() -> None:
    """The .env main.py's load_dotenv() finds: from the SAIVerse folder upward
    (only its location is looked at here; load_dotenv is replaced)."""
    from scripts.update_engine import find_dotenv_path

    _DOTENV_LOADS.clear()
    _load_entry()
    found = find_dotenv_path(Path(__file__).resolve().parent.parent)
    assert _DOTENV_LOADS == ([((found,), {})] if found is not None else [])


def test_entry_does_not_rebuild_while_saiverse_may_be_running(home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    entry = _load_entry()
    monkeypatch.setattr(entry, "_saiverse_running", lambda: (True, "verified SAIVerse City process pid 1"))

    assert entry.main(expansion) == 0
    assert recorded == []
    data = json.loads((tmp_path / ".addon_env_rebuild_failures.json").read_text(encoding="utf-8"))
    assert [f["addon_id"] for f in data["failures"]] == ["a-addon"]
    assert "SAIVerse may be running" in data["failures"][0]["error"]


def test_entry_rebuilds_and_clears_the_record(home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    record = tmp_path / ".addon_env_rebuild_failures.json"
    record.write_text("{}", encoding="utf-8")  # 前回の失敗の記録
    entry = _load_entry()
    monkeypatch.setattr(entry, "_saiverse_running", lambda: (False, "runtime markers absent"))

    assert entry.main(expansion) == 0
    assert "cloud-pkgs" in _labels(recorded)
    assert not record.exists()


def test_a_crash_of_the_entry_still_leaves_a_record_for_the_banner(home, monkeypatch, tmp_path) -> None:
    entry = _load_entry()

    def crash(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected")

    monkeypatch.setattr(entry, "find_unusable_addon_envs", crash)
    assert entry._main_recording_crashes() == 1
    data = json.loads((tmp_path / ".addon_env_rebuild_failures.json").read_text(encoding="utf-8"))
    assert data["failures"][0]["addon_id"] == "(setup)"
    assert "unexpected" in data["failures"][0]["error"]
    assert data["failures"][0]["core_venv"] is False


# ---------------------------------------------------------------------------
# 本体の .venv を作り直した回の、本体の .venv に入れる step の入れ直し
# ---------------------------------------------------------------------------

LOCK = str(addon_installer.CORE_REQUIREMENTS_LOCK)
RECORD_NAME = ".addon_env_rebuild_failures.json"


def _entry_not_running(monkeypatch):  # type: ignore[no-untyped-def]
    entry = _load_entry()
    monkeypatch.setattr(entry, "_saiverse_running", lambda: (False, "runtime markers absent"))
    return entry


def _core_runs(recorded: list) -> list[tuple[str, str]]:
    """本体の Python の pip で走った step の (アドオンのフォルダ名, step の名前)。"""
    return [
        (Path(r["cwd"]).name, r["label"]) for r in recorded
        if r["args"][:4] == [addon_installer.sys.executable, "-m", "pip", "install"]
    ]


def test_core_venv_steps_run_with_the_saved_answers_only_with_the_flag(
        home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    current = addon_paths.current_python_version()
    a_dir = _install(expansion, "a-addon", {"engines": ["cloud"]}, current)
    b_dir = _install(expansion, "b-addon", {"engines": ["cloud", "gpt"]}, current)
    (expansion / "hand-made").mkdir()  # addon.json の無いフォルダは対象にしない
    before = (_snapshot("a-addon", a_dir), _snapshot("b-addon", b_dir))
    entry = _entry_not_running(monkeypatch)

    # 付かない回 (setup_venv.py prepare が keep) は、何も走らせない
    assert entry.main(expansion) == 0
    assert recorded == []

    assert entry.main(expansion, core_venv_recreated=True) == 0
    # 保存された答えのとおり、env の無い pip_install だけ (スクリプトと専用の環境の step は走らせない)
    assert _core_runs(recorded) == [
        ("a-addon", "core-pkgs"), ("b-addon", "core-pkgs"), ("b-addon", "gpt-core-pkgs")]
    assert _labels(recorded) == ["core-pkgs", "core-pkgs", "gpt-core-pkgs"]
    # 既存の導入と同じく requirements.lock を constraints として渡す
    for r in recorded:
        assert r["args"][-2:] == ["-c", LOCK]
    # 答え・アドオンのバージョン・アドオンのデータは変わらない
    assert (_snapshot("a-addon", a_dir), _snapshot("b-addon", b_dir)) == before
    assert not (tmp_path / RECORD_NAME).exists()


def test_an_addon_without_saved_answers_uses_the_defaults(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {}, None)
    (addon_paths.get_addon_install_dir("a-addon") / addon_installer.SETUP_ANSWERS_NAME).unlink()

    results = addon_installer.reinstall_core_venv_steps(expansion)

    assert [(r.addon_id, r.steps, r.ok, r.core_venv) for r in results] == [
        ("a-addon", ["core-pkgs"], True, True)]
    assert addon_installer.load_saved_answers("a-addon") is None  # 答えは書かない


def test_a_prepared_but_unconfirmed_install_is_skipped(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    addon_installer._write_pending("a-addon", "install", "0" * 40, "https://example.invalid/a.git")
    assert addon_installer.reinstall_core_venv_steps(expansion) == []
    assert recorded == []


def test_core_steps_run_before_the_env_rebuild(home, expansion, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    entry = _entry_not_running(monkeypatch)

    assert entry.main(expansion, core_venv_recreated=True) == 0
    labels = [lbl for lbl in _labels(recorded) if not lbl.startswith("upgrade pip")]
    assert labels == ["core-pkgs", "venv eng", "cloud-pkgs"]


def test_failures_of_both_kinds_are_kept_in_one_record(home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []

    def fake(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        _fake_run(recorded)(args, cwd, progress, label, env)
        if label in ("core-pkgs", "gpt-pkgs"):
            raise addon_installer.AddonInstallError(f"{label}: 終了コード 1 で失敗")

    monkeypatch.setattr(addon_installer, "_run_subprocess", fake)
    a_dir = _install(expansion, "a-addon", {"engines": ["cloud", "gpt"]}, STALE)
    before = _snapshot("a-addon", a_dir)
    entry = _entry_not_running(monkeypatch)

    assert entry.main(expansion, core_venv_recreated=True) == 0

    # 本体側の失敗のあとも、専用の環境の作り直しは続けて走る
    assert "venv eng" in _labels(recorded) and "cloud-pkgs" in _labels(recorded)
    assert _snapshot("a-addon", a_dir) == before
    data = json.loads((tmp_path / RECORD_NAME).read_text(encoding="utf-8"))
    assert [(f["addon_id"], f["core_venv"], f["envs"], f["steps"]) for f in data["failures"]] == [
        ("a-addon", True, [], ["core-pkgs", "gpt-core-pkgs"]),
        ("a-addon", False, ["eng"], ["cloud-pkgs", "gpt-pkgs"]),
    ]
    assert "SAIVerse .venv" in data["failures"][0]["error"]
    assert "core-pkgs" in data["failures"][0]["error"]
    assert "gpt-pkgs" in data["failures"][1]["error"]

    # 起動時のバナーは、アドオンの名前を一度だけ出し、どちらの失敗かを詳細に書く
    from saiverse import environment_alerts

    (alert,) = environment_alerts.addon_env_rebuild_alerts(tmp_path / RECORD_NAME)
    assert alert["details"]["reason"] == ["a-addon"]
    assert "(SAIVerse .venv)" in alert["details"]["error"]
    assert "(addon environment)" in alert["details"]["error"]


def test_a_previous_core_failure_is_retried_in_a_run_that_keeps_the_venv(
        home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    current = addon_paths.current_python_version()
    _install(expansion, "a-addon", {"engines": ["cloud"]}, current)
    _install(expansion, "b-addon", {"engines": ["cloud"]}, current)
    addon_installer.record_env_rebuild_failures([
        addon_installer.EnvRebuildResult(
            addon_id="a-addon", envs=[], steps=["core-pkgs"], error="x", core_venv=True)])
    entry = _entry_not_running(monkeypatch)

    # 前の回に本体の .venv への入れ直しが失敗したアドオンだけ、付かない回でもやり直す
    assert entry.main(expansion) == 0
    assert _core_runs(recorded) == [("a-addon", "core-pkgs")]
    assert not (tmp_path / RECORD_NAME).exists()

    # 専用の環境の失敗だけの記録では、本体の .venv の入れ直しはしない
    # (専用の環境の方は、記録された名前としてやり直される)
    recorded.clear()
    addon_installer.record_env_rebuild_failures([
        addon_installer.EnvRebuildResult(addon_id="a-addon", envs=["eng"], error="x")])
    assert entry.main(expansion) == 0
    assert _core_runs(recorded) == []


def test_core_steps_are_not_run_while_saiverse_may_be_running(home, expansion, monkeypatch, tmp_path) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    _install(expansion, "a-addon", {"engines": ["cloud"]}, addon_paths.current_python_version())
    entry = _load_entry()
    monkeypatch.setattr(entry, "_saiverse_running", lambda: (True, "verified SAIVerse City process pid 1"))

    assert entry.main(expansion, core_venv_recreated=True) == 0
    assert recorded == []
    data = json.loads((tmp_path / RECORD_NAME).read_text(encoding="utf-8"))
    assert [(f["addon_id"], f["core_venv"], f["steps"]) for f in data["failures"]] == [
        ("a-addon", True, ["core-pkgs"])]
    assert "SAIVerse may be running" in data["failures"][0]["error"]
    # 次の回 (.venv はもう作り直さない) で、やり直しの対象になる
    assert addon_installer.core_venv_failures_in_record() == {"a-addon"}


def test_an_unreadable_manifest_is_a_core_failure_when_the_venv_was_recreated(
        home, expansion, monkeypatch) -> None:
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run([]))
    addon_dir = _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    (addon_dir / "addon.json").write_text("{broken", encoding="utf-8")
    results = addon_installer.reinstall_core_venv_steps(expansion)
    assert [(r.addon_id, r.ok, r.core_venv) for r in results] == [("a-addon", False, True)]


def test_a_crash_with_the_flag_records_that_the_core_venv_is_not_done(home, monkeypatch, tmp_path) -> None:
    entry = _load_entry()

    def crash(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected")

    monkeypatch.setattr(entry, "plan_core_venv_reinstalls", crash)
    assert entry._main_recording_crashes(["--core-venv-recreated"]) == 1
    data = json.loads((tmp_path / RECORD_NAME).read_text(encoding="utf-8"))
    assert [(f["addon_id"], f["core_venv"]) for f in data["failures"]] == [("(setup)", True)]
    # 次の回は、全部のアドオンの本体側の step をやり直す
    assert addon_installer.core_venv_failures_in_record() == {addon_installer.SETUP_CRASH_ADDON_ID}


# ---------------------------------------------------------------------------
# The rebuild record: written before an env is removed, removed after its steps
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Stands in for the setup window being closed (not an Exception, as Ctrl+C is not)."""


def _entry_files(addon_id: str) -> list[str]:
    from scripts import setup_in_progress

    return setup_in_progress.entry_names(addon_paths.get_addon_install_dir(addon_id))


def _stopping_at(recorded: list, stop_when):  # type: ignore[no-untyped-def]
    """``_fake_run`` that raises ``_Stop`` before running the command ``stop_when`` picks."""
    fake = _fake_run(recorded)

    def run(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        if stop_when(args, label):
            raise _Stop
        fake(args, cwd, progress, label, env)

    return run


def test_an_env_lost_by_a_stopped_rebuild_is_rebuilt_next_time(home, expansion, monkeypatch) -> None:
    """The rebuild removes the old env before creating it: stopped right after
    the removal, no folder is left, and only the record brings the env back."""
    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    env_dir = addon_paths.get_addon_env_dir("a-addon", "eng")
    recorded: list = []
    monkeypatch.setattr(
        addon_installer, "_run_subprocess", _stopping_at(recorded, lambda args, label: args[1:3] == ["-m", "venv"]))
    with pytest.raises(_Stop):
        addon_installer.rebuild_unusable_addon_envs(expansion)
    assert not env_dir.exists()
    assert _entry_files("a-addon") == ["env-eng"]
    # Nothing on disk tells the env is missing; the record does.
    assert addon_installer.find_unusable_addon_envs(expansion) == {"a-addon": ["eng"]}

    recorded.clear()
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    results = addon_installer.rebuild_unusable_addon_envs(expansion)
    assert [(r.addon_id, r.ok) for r in results] == [("a-addon", True)]
    assert any(c["args"][1:3] == ["-m", "venv"] for c in recorded)
    assert addon_paths.get_addon_env_python("a-addon", "eng") == addon_paths.addon_env_python_path(env_dir)
    assert _entry_files("a-addon") == []
    assert addon_installer.find_unusable_addon_envs(expansion) == {}


def test_an_env_stopped_while_it_was_deleted_does_not_look_usable(home, expansion, monkeypatch) -> None:
    """Deleting stopped part-way leaves python behind; the env record goes first,
    so the half-deleted env is refused and rebuilt."""
    _install(expansion, "a-addon", {"engines": ["cloud"]}, addon_paths.current_python_version())
    env_dir = addon_paths.get_addon_env_dir("a-addon", "eng")
    assert addon_installer.find_unusable_addon_envs(expansion) == {}

    def stop(_path):  # type: ignore[no-untyped-def]
        raise _Stop

    with monkeypatch.context() as m:
        # The record of the env is stale for this run, so it is rebuilt.
        m.setattr(addon_paths, "current_python_version", lambda: "9.9.9")
        m.setattr(addon_installer, "current_python_version", lambda: "9.9.9")
        m.setattr(addon_installer, "_safe_rmtree", stop)
        with pytest.raises(_Stop):
            addon_installer._ensure_addon_env("a-addon", "eng", lambda _e: None, set())
    assert addon_paths.addon_env_python_path(env_dir).exists()
    with pytest.raises(addon_paths.AddonEnvError):
        addon_paths.get_addon_env_python("a-addon", "eng")
    assert addon_installer.find_unusable_addon_envs(expansion) == {"a-addon": ["eng"]}


def test_a_recorded_env_is_rebuilt_even_when_its_record_says_it_is_current(home, expansion, monkeypatch) -> None:
    """Stopped after the env record was written but before the rebuild record
    was removed: the env is complete, and is still rebuilt rather than trusted."""
    from scripts import setup_in_progress

    _install(expansion, "a-addon", {"engines": ["cloud"]}, addon_paths.current_python_version())
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir("a-addon"), "env-eng", {})
    env_dir = addon_paths.get_addon_env_dir("a-addon", "eng")
    assert addon_installer._env_needs_full_run("a-addon", "eng") is True
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    results = addon_installer.rebuild_unusable_addon_envs(expansion)
    assert [(r.addon_id, r.ok) for r in results] == [("a-addon", True)]
    assert not (env_dir / "old-package.txt").exists()  # made again from an empty venv
    assert _entry_files("a-addon") == []


def test_a_failed_rebuild_keeps_the_record_for_every_entrance(home, expansion, monkeypatch) -> None:
    """The next setup and the addon manager (install / update / options) all go
    through _ensure_addon_env and plan_options_apply's _env_needs_full_run."""
    _install(expansion, "a-addon", {"engines": ["cloud", "gpt"]}, STALE)
    recorded: list = []

    def fail(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        _fake_run(recorded)(args, cwd, progress, label, env)
        if label == "gpt-pkgs":
            raise addon_installer.AddonInstallError("gpt-pkgs failed")

    monkeypatch.setattr(addon_installer, "_run_subprocess", fail)
    results = addon_installer.rebuild_unusable_addon_envs(expansion)
    assert [(r.addon_id, r.ok) for r in results] == [("a-addon", False)]
    assert _entry_files("a-addon") == ["env-eng"]
    # Choosing the options again (without changing them) runs every step of the env.
    plan = addon_installer.plan_options_apply("a-addon", None, expansion)
    assert [s.name for s in plan.steps] == ["cloud-pkgs", "gpt-pkgs"]

    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run(recorded))
    addon_installer.execute_options_plan(plan)
    assert _entry_files("a-addon") == []
    addon_paths.get_addon_env_python("a-addon", "eng")


def test_uninstalling_removes_the_record_with_the_install_folder(home, expansion, monkeypatch) -> None:
    from scripts import setup_in_progress

    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir("a-addon"), "env-eng", {})
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run([]))
    addon_installer.uninstall_addon("a-addon", expansion_dir=expansion)
    assert _entry_files("a-addon") == []


def test_a_record_with_an_invalid_env_name_is_ignored(home, expansion) -> None:
    from scripts import setup_in_progress

    _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir("a-addon"), "env-bad name", {})
    assert addon_installer.find_unusable_addon_envs(expansion) == {}


def test_the_failure_record_alone_no_longer_names_envs_to_rebuild(home, expansion, tmp_path) -> None:
    """The rebuild record is the one place that says an env is unfinished; the
    failure record is for the banner (and the core .venv retry) only."""
    _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    (tmp_path / ".addon_env_rebuild_failures.json").write_text(json.dumps({
        "failures": [{"addon_id": "a-addon", "envs": ["eng"], "core_venv": False}],
    }), encoding="utf-8")
    assert addon_installer.find_unusable_addon_envs(expansion) == {}


# --- the banner for an env left part-way (saiverse/environment_alerts.py) ------------------


@pytest.fixture
def banner_paths(expansion, monkeypatch):  # type: ignore[no-untyped-def]
    """The banner reads the display name from expansion_data; use the test's."""
    from saiverse import data_paths

    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", expansion)
    monkeypatch.setattr(addon_installer, "EXPANSION_DATA_DIR", expansion)


@pytest.mark.usefixtures("banner_paths")
def test_an_env_removed_by_a_stopped_rebuild_is_on_the_banner_once(home, expansion, monkeypatch, tmp_path) -> None:
    """Stopped right after the old env was removed: setup never reached the
    failure record, and only the in-progress record names the addon."""
    from saiverse import environment_alerts

    # The test interpreter may be of another series than the recommended one.
    monkeypatch.setenv(environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV, "1")
    _install(expansion, "a-addon", {"engines": ["cloud"]}, STALE)
    monkeypatch.setattr(
        addon_installer, "_run_subprocess", _stopping_at([], lambda args, label: args[1:3] == ["-m", "venv"]))
    with pytest.raises(_Stop):
        addon_installer.rebuild_unusable_addon_envs(expansion)
    assert not (tmp_path / ".addon_env_rebuild_failures.json").exists()
    assert addon_installer.find_addon_envs_left_part_way(expansion) == {"a-addon": ["eng"]}

    alerts = environment_alerts.environment_alerts(tmp_path)
    assert [alert["id"] for alert in alerts] == [environment_alerts.ADDON_ENV_STOPPED_ALERT_ID]
    assert "X (a-addon)" in alerts[0]["message"]
    assert "setup.bat" in alerts[0]["message"] and "アドオン管理" in alerts[0]["message"]
    assert alerts[0]["details"] == {"reason": ["a-addon"], "envs": ["a-addon: eng"]}

    # The same addon in the failure record (a rebuild that failed and wrote it):
    # only the failure alert names it.
    addon_installer.record_env_rebuild_failures([addon_installer.EnvRebuildResult(
        addon_id="a-addon", envs=["eng"], steps=["cloud-pkgs"], error="pip failed")])
    alerts = environment_alerts.environment_alerts(tmp_path)
    assert [alert["id"] for alert in alerts] == [environment_alerts.ADDON_ENV_REBUILD_ALERT_ID]

    # The next rebuild finishes: no record, no failure, no banner.
    addon_installer.record_env_rebuild_failures([])
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run([]))
    addon_installer.rebuild_unusable_addon_envs(expansion)
    assert environment_alerts.environment_alerts(tmp_path) == []


def test_a_record_of_an_env_no_selected_step_uses_is_not_shown(home, expansion) -> None:
    """Setup leaves such an env alone, so its record is harmless and the user
    could not clear it: not on the banner."""
    from scripts import setup_in_progress

    _install(expansion, "a-addon", {"engines": []}, None)
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir("a-addon"), "env-eng", {})
    assert addon_installer.find_addon_envs_left_part_way(expansion) == {}


def test_an_addon_with_an_unreadable_manifest_shows_every_recorded_env(home, expansion) -> None:
    from scripts import setup_in_progress

    addon_dir = _install(expansion, "a-addon", {"engines": ["cloud"]}, None)
    (addon_dir / "addon.json").write_text("{not json", encoding="utf-8")
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir("a-addon"), "env-eng", {})
    assert addon_installer.find_addon_envs_left_part_way(expansion) == {"a-addon": ["eng"]}
