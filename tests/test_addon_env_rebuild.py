"""本体の Python の系列が変わったあとの、アドオン専用の環境の作り直し (setup から呼ぶ)。

設計: docs/intent/dependency_management.md §2-7。venv の作成と pip は
``_run_subprocess`` を差し替えて、コマンド列だけを見る (本物の venv・pip・ネットワークは
使わない)。``SAIVERSE_HOME``・``addon_data``・失敗の記録ファイルは tmp_path に向ける。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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
    # 失敗した環境の記録は消す: 使えば「setup をやり直して」と断られ、次の setup で作り直される
    a_env = addon_paths.get_addon_env_dir("a-addon", "eng")
    assert addon_paths.read_addon_env_record(a_env) is None
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


def _load_entry():
    script = Path(__file__).resolve().parent.parent / "scripts" / "rebuild_addon_envs.py"
    spec = importlib.util.spec_from_file_location("rebuild_addon_envs_under_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
