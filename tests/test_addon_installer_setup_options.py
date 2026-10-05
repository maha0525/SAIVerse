"""導入時の質問・専用の Python 環境・二段構えの導入と更新 (2026-10-05)。

設計: docs/intent/addon_catalog_management.md「導入時の質問と、アドオン専用の
Python 環境」。

git の操作は tmp_path に作るローカルのリポジトリで本物を回す (取得・展開・切り替えが
実際に起きることを見るため)。pip と venv の作成は ``_run_subprocess`` を差し替えて
コマンド列だけを見る。python_script の step は本体の Python で本当に実行する
(marker ファイルに追記するだけのスクリプト)。

``SAIVERSE_HOME`` と ``addon_data`` の置き場所は tmp_path に向け、本番の
``~/.saiverse/`` には触れない。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from saiverse import addon_installer, addon_paths
from saiverse.addon_installer import (
    AddonAnswersError,
    AddonStateError,
    AddonVersionError,
    apply_installed_options,
    cancel_install,
    check_min_saiverse_version,
    confirm_install,
    confirm_update,
    current_os,
    get_installed_options,
    get_pending_operation,
    load_saved_answers,
    prepare_install,
    prepare_update,
    prune_answers,
    select_steps,
    uninstall_addon,
)
from saiverse.addon_manifest import GitCloneStep, PipInstallStep, load_manifest

ADDON_ID = "sample-addon"
OTHER_OS = "macos" if current_os() != "macos" else "linux"

MARK_SCRIPT = (
    "import sys, pathlib\n"
    "with pathlib.Path('markers.txt').open('a', encoding='utf-8') as f:\n"
    "    f.write(sys.argv[1] + '\\n')\n"
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setattr(addon_paths, "USER_DATA_DIR", home / "user_data")
    return home


@pytest.fixture
def expansion(tmp_path: Path) -> Path:
    path = tmp_path / "expansion_data"
    path.mkdir()
    return path


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, manifest: dict, extra_files: dict | None = None) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
    (repo / "addon.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (repo / "mark.py").write_text(MARK_SCRIPT, encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def _mark_step(name: str, arg: str, **extra) -> dict:
    return {"name": name, "type": "python_script", "script": "mark.py", "args": [arg], **extra}


def _manifest_v1() -> dict:
    return {
        "name": ADDON_ID,
        "display_name": {"ja": "見本", "en": "Sample"},
        "version": "1.0.0",
        "manifest_version": 2,
        "setup_version": 1,
        "setup": {
            "options": [
                {
                    "id": "engines",
                    "question": {"ja": "エンジン", "en": "Engines"},
                    "multiple": True,
                    "choices": [
                        {"id": "cloud", "label": "クラウド", "default": True},
                        {"id": "gpt", "label": {"ja": "GPU", "en": "GPU"}},
                        {"id": "old", "label": "古い"},
                    ],
                },
            ],
            "steps": [
                _mark_step("always", "always"),
                _mark_step("cloud", "cloud", when={"engines": "cloud"}),
                _mark_step("gpt", "gpt", when={"engines": "gpt"}),
                _mark_step("other-os", "other-os", os=[OTHER_OS]),
            ],
        },
    }


def _markers(addon_dir: Path) -> list[str]:
    path = addon_dir / "markers.txt"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").split()


def _install(repo: Path, commit: str, expansion: Path, answers: dict) -> None:
    prepare_install(str(repo), commit, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, answers, expansion_dir=expansion)


# ---------------------------------------------------------------------------
# manifest の新しい欄の検証
# ---------------------------------------------------------------------------

def _manifest_with(steps: list, options: list | None = None) -> dict:
    setup: dict = {"steps": steps}
    if options is not None:
        setup["options"] = options
    return {"name": "x", "display_name": "X", "version": "1", "manifest_version": 2, "setup": setup}


_ENGINES = [{"id": "engines", "question": "q", "multiple": True,
             "choices": [{"id": "cloud", "label": "c"}, {"id": "gpt", "label": "g"}]}]


def test_manifest_accepts_options_when_env_os() -> None:
    m = load_manifest(_manifest_with(
        [{"name": "p", "type": "pip_install", "requirements": "envs/gpt.txt",
          "env": "gpt_sovits", "when": {"engines": "gpt"}, "os": ["windows", "linux"]}],
        _ENGINES,
    ))
    step = m.setup.steps[0]
    assert step.env == "gpt_sovits"
    assert step.when == {"engines": "gpt"}
    assert step.os == ["windows", "linux"]
    assert m.setup.options[0].choice_ids() == ["cloud", "gpt"]


@pytest.mark.parametrize(
    "steps, options",
    [
        # when が知らない質問を指す
        ([_mark_step("s", "a", when={"nope": "cloud"})], _ENGINES),
        # when が知らない選択肢を指す
        ([_mark_step("s", "a", when={"engines": "nope"})], _ENGINES),
        # os の語彙違反
        ([_mark_step("s", "a", os=["darwin"])], None),
        # env の名前が不正 (パス区切り・'..')
        ([_mark_step("s", "a", env="../escape")], None),
        ([_mark_step("s", "a", env="a/b")], None),
        # env を持てない step 種別
        ([{"name": "g", "type": "git_clone", "url": "https://example.com/r.git",
           "commit": "a" * 40, "dest": "x", "env": "e"}], None),
        # 質問 id の重複
        ([], _ENGINES + _ENGINES),
        # 選択肢 id の重複
        ([], [{"id": "q", "question": "q", "choices": [{"id": "a", "label": "a"}, {"id": "a", "label": "b"}]}]),
        # 一つだけ選ぶ質問に既定が二つ
        ([], [{"id": "q", "question": "q", "choices": [
            {"id": "a", "label": "a", "default": True}, {"id": "b", "label": "b", "default": True}]}]),
        # 知らない欄 (extra=forbid は維持)
        ([], [{"id": "q", "question": "q", "free_text": True, "choices": [{"id": "a", "label": "a"}]}]),
    ],
)
def test_manifest_rejects_invalid_new_fields(steps, options) -> None:
    with pytest.raises(ValidationError):
        load_manifest(_manifest_with(steps, options))


def test_uninstall_when_must_refer_to_setup_options() -> None:
    data = _manifest_with([], _ENGINES)
    data["uninstall"] = {"steps": [{"name": "r", "type": "remove_dir", "path": "x",
                                    "when": {"engines": "missing"}}]}
    with pytest.raises(ValidationError):
        load_manifest(data)


# ---------------------------------------------------------------------------
# when / os による選別、答えの剪定
# ---------------------------------------------------------------------------

def test_select_steps_by_when_and_os() -> None:
    m = load_manifest(_manifest_v1())
    names = [s.name for s in select_steps(m.setup.steps, {"engines": ["gpt"]})]
    assert names == ["always", "gpt"]
    names = [s.name for s in select_steps(m.setup.steps, {"engines": ["gpt"]}, os_name=OTHER_OS)]
    assert names == ["always", "gpt", "other-os"]
    names = [s.name for s in select_steps(m.setup.steps, {"engines": ["cloud", "gpt"]})]
    assert names == ["always", "cloud", "gpt"]


def test_prune_answers_drops_vanished_questions_and_choices() -> None:
    m = load_manifest(_manifest_v1())
    pruned = prune_answers(m, {"engines": ["gpt", "gone"], "removed_question": ["x"]})
    assert pruned == {"engines": ["gpt"]}


# ---------------------------------------------------------------------------
# 専用の Python 環境
# ---------------------------------------------------------------------------

def _fake_run_creating_venv(recorded: list):
    def fake_run(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        recorded.append({"args": list(args), "env": env})
        if args[1:3] == ["-m", "venv"]:
            python = addon_paths.addon_env_python_path(Path(args[3]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
    return fake_run


def test_env_is_created_and_rebuilt_on_python_version_change(home, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run_creating_venv(recorded))
    progress = lambda _e: None  # noqa: E731

    env_dir = addon_installer._ensure_addon_env(ADDON_ID, "gpt", progress)
    assert env_dir == home / "addon_install" / ADDON_ID / "envs" / "gpt"
    record = addon_paths.read_addon_env_record(env_dir)
    assert record["python_version"] == addon_paths.current_python_version()
    assert sum(1 for r in recorded if r["args"][1:3] == ["-m", "venv"]) == 1

    # 同じ Python なら作り直さない
    addon_installer._ensure_addon_env(ADDON_ID, "gpt", progress)
    assert sum(1 for r in recorded if r["args"][1:3] == ["-m", "venv"]) == 1
    assert addon_paths.get_addon_env_python(ADDON_ID, "gpt") == addon_paths.addon_env_python_path(env_dir)

    # 本体の Python が変わったら、関数はエラーで知らせ、次の setup で作り直す
    stale_marker = env_dir / "stale.txt"
    stale_marker.write_text("old", encoding="utf-8")
    monkeypatch.setattr(addon_paths, "current_python_version", lambda: "9.9.9")
    with pytest.raises(addon_paths.AddonEnvError, match="9.9.9"):
        addon_paths.get_addon_env_python(ADDON_ID, "gpt")
    monkeypatch.setattr(addon_installer, "current_python_version", lambda: "9.9.9")
    addon_installer._ensure_addon_env(ADDON_ID, "gpt", progress)
    assert sum(1 for r in recorded if r["args"][1:3] == ["-m", "venv"]) == 2
    assert not stale_marker.exists()
    assert addon_paths.read_addon_env_record(env_dir)["python_version"] == "9.9.9"


def test_get_addon_env_python_refuses_missing_env(home) -> None:
    with pytest.raises(addon_paths.AddonEnvError):
        addon_paths.get_addon_env_python(ADDON_ID, "nothing")


def test_env_pip_install_uses_env_python_without_constraints(home, tmp_path, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run_creating_venv(recorded))
    addon_dir = tmp_path / "addon"
    addon_dir.mkdir()
    (addon_dir / "req.txt").write_text("torch\n", encoding="utf-8")
    step = PipInstallStep(type="pip_install", name="deps", requirements="req.txt", env="gpt")

    addon_installer._exec_pip_install(step, addon_dir, lambda _e: None, ADDON_ID)

    pip_call = recorded[-1]
    env_dir = home / "addon_install" / ADDON_ID / "envs" / "gpt"
    assert pip_call["args"][0] == str(addon_paths.addon_env_python_path(env_dir))
    assert pip_call["args"][1:4] == ["-m", "pip", "install"]
    assert "-c" not in pip_call["args"]
    assert pip_call["env"]["VIRTUAL_ENV"] == str(env_dir)
    assert pip_call["env"]["PATH"].startswith(str(addon_paths.addon_env_python_path(env_dir).parent))


def test_plain_pip_install_still_passes_constraints(tmp_path, monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr(addon_installer, "_run_subprocess", _fake_run_creating_venv(recorded))
    addon_dir = tmp_path / "addon"
    addon_dir.mkdir()
    (addon_dir / "req.txt").write_text("numpy\n", encoding="utf-8")
    step = PipInstallStep(type="pip_install", name="deps", requirements="req.txt")

    addon_installer._exec_pip_install(step, addon_dir, lambda _e: None, ADDON_ID)

    assert "-c" in recorded[-1]["args"]
    assert recorded[-1]["env"] is None


# ---------------------------------------------------------------------------
# 二段構えの導入
# ---------------------------------------------------------------------------

def test_prepare_does_not_check_out_and_confirm_runs_selected_steps(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())

    prepared = prepare_install(str(repo), sha, ADDON_ID, expansion_dir=expansion)

    addon_dir = expansion / ADDON_ID
    assert not (addon_dir / "addon.json").exists()  # working tree へは展開しない
    assert get_pending_operation(ADDON_ID)["commit"] == sha
    payload = prepared.to_dict()
    assert payload["version"] == "1.0.0"
    assert payload["questions"][0]["id"] == "engines"
    assert payload["questions"][0]["question"] == {"ja": "エンジン", "en": "Engines"}
    assert payload["questions"][0]["choices"][0]["label"] == {"ja": "クラウド"}
    assert payload["questions"][0]["choices"][0]["default"] is True
    # 実行中の OS に当てはまらない step は返さない
    assert [s["name"] for s in payload["steps"]] == ["always", "cloud", "gpt"]
    assert payload["steps"][2]["when"] == {"engines": "gpt"}
    assert payload["steps"][0]["when"] == {}

    manifest = confirm_install(ADDON_ID, {"engines": ["gpt"]}, expansion_dir=expansion)

    assert manifest.version == "1.0.0"
    assert (addon_dir / "addon.json").exists()
    assert _markers(addon_dir) == ["always", "gpt"]
    assert load_saved_answers(ADDON_ID) == {"engines": ["gpt"]}
    assert get_pending_operation(ADDON_ID) is None


def test_confirm_without_answers_uses_defaults(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    _install(repo, sha, expansion, {})
    assert _markers(expansion / ADDON_ID) == ["always", "cloud"]
    assert load_saved_answers(ADDON_ID) == {"engines": ["cloud"]}


def test_invalid_answers_keep_the_prepared_state(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    prepare_install(str(repo), sha, ADDON_ID, expansion_dir=expansion)

    with pytest.raises(AddonAnswersError):
        confirm_install(ADDON_ID, {"engines": ["nope"]}, expansion_dir=expansion)

    assert (expansion / ADDON_ID).exists()
    assert get_pending_operation(ADDON_ID) is not None


def test_cancel_install_removes_the_prepared_folder(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    prepare_install(str(repo), sha, ADDON_ID, expansion_dir=expansion)

    cancel_install(ADDON_ID, expansion_dir=expansion)

    assert not (expansion / ADDON_ID).exists()
    assert get_pending_operation(ADDON_ID) is None
    with pytest.raises(AddonStateError):
        confirm_install(ADDON_ID, {}, expansion_dir=expansion)


def test_failed_step_rolls_back_the_install(home, expansion, tmp_path) -> None:
    manifest = _manifest_v1()
    manifest["setup"]["steps"].append(
        {"name": "missing", "type": "python_script", "script": "does_not_exist.py"}
    )
    repo = tmp_path / "repo"
    sha = _commit(repo, manifest)
    prepare_install(str(repo), sha, ADDON_ID, expansion_dir=expansion)

    with pytest.raises(addon_installer.AddonInstallError):
        confirm_install(ADDON_ID, {}, expansion_dir=expansion)

    assert not (expansion / ADDON_ID).exists()
    assert not (home / "addon_install" / ADDON_ID).exists()


def test_min_saiverse_version_refuses_before_any_git(home, expansion, monkeypatch) -> None:
    def no_git(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("git must not run")

    monkeypatch.setattr(addon_installer, "_git", no_git)
    with pytest.raises(AddonVersionError, match="99.0.0"):
        prepare_install("https://example.com/r.git", "a" * 40, ADDON_ID,
                        min_saiverse_version="99.0.0", expansion_dir=expansion)
    assert not (expansion / ADDON_ID).exists()


def test_min_saiverse_version_comparison() -> None:
    check_min_saiverse_version(None, current="0.3.21")
    check_min_saiverse_version("0.3.21", current="0.3.21")
    check_min_saiverse_version("0.3.0", current="0.3.21")
    with pytest.raises(AddonVersionError):
        check_min_saiverse_version("0.4.0", current="0.4.0rc1")
    with pytest.raises(AddonVersionError):
        check_min_saiverse_version("not a version", current="0.3.21")


# ---------------------------------------------------------------------------
# 更新
# ---------------------------------------------------------------------------

def _manifest_v2() -> dict:
    m = _manifest_v1()
    m["version"] = "2.0.0"
    m["setup_version"] = 2
    engines = m["setup"]["options"][0]
    engines["choices"] = [c for c in engines["choices"] if c["id"] != "old"]
    engines["choices"].append({"id": "new", "label": "新しい"})
    m["setup"]["options"].append({
        "id": "device", "question": "装置",
        "choices": [{"id": "cpu", "label": "CPU"}, {"id": "cuda", "label": "CUDA", "default": True}],
    })
    m["setup"]["steps"].append(_mark_step("cuda", "cuda", when={"device": "cuda"}))
    return m


def test_update_fetches_from_the_catalog_repo_url_not_origin(home, expansion, tmp_path) -> None:
    upstream = tmp_path / "upstream"
    sha1 = _commit(upstream, _manifest_v1())
    _install(upstream, sha1, expansion, {"engines": ["gpt", "old"]})

    # カタログが指す先 (フォーク) にだけある commit
    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(upstream), str(fork))
    sha2 = _commit(fork, _manifest_v2())
    addon_dir = expansion / ADDON_ID
    (addon_dir / "markers.txt").unlink()

    prepared = prepare_update(ADDON_ID, str(fork), sha2, expansion_dir=expansion)

    assert prepared.needs_setup is True
    assert json.loads((addon_dir / "addon.json").read_text(encoding="utf-8"))["version"] == "1.0.0"
    # 選択肢が増えた質問と、新しい質問だけを、前の答えが選ばれた状態で出し直す
    questions = {q["id"]: q for q in prepared.questions}
    assert set(questions) == {"engines", "device"}
    selected = [c["id"] for c in questions["engines"]["choices"] if c["selected"]]
    assert selected == ["gpt"]

    manifest = confirm_update(ADDON_ID, {}, expansion_dir=expansion)

    assert manifest.version == "2.0.0"
    assert _git(addon_dir, "rev-parse", "HEAD") == sha2
    # 無くなった選択肢 old は捨てられ、新しい質問は既定値
    assert load_saved_answers(ADDON_ID) == {"engines": ["gpt"], "device": ["cuda"]}
    assert _markers(addon_dir) == ["always", "gpt", "cuda"]
    assert get_pending_operation(ADDON_ID) is None


def test_update_without_setup_version_bump_runs_no_steps(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha1 = _commit(repo, _manifest_v1())
    _install(repo, sha1, expansion, {})
    (expansion / ADDON_ID / "markers.txt").unlink()
    m = _manifest_v1()
    m["version"] = "1.0.1"
    sha2 = _commit(repo, m)

    prepared = prepare_update(ADDON_ID, str(repo), sha2, expansion_dir=expansion)
    assert prepared.needs_setup is False
    assert prepared.questions == [] and prepared.steps == []

    confirm_update(ADDON_ID, {}, expansion_dir=expansion)
    assert _markers(expansion / ADDON_ID) == []
    assert load_saved_answers(ADDON_ID) == {"engines": ["cloud"]}


def test_update_of_manual_clone_without_answers_asks_everything(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha1 = _commit(repo, _manifest_v1())
    _git(tmp_path, "clone", "-q", str(repo), str(expansion / ADDON_ID))  # 手で入れた形
    sha2 = _commit(repo, _manifest_v2())

    prepared = prepare_update(ADDON_ID, str(repo), sha2, expansion_dir=expansion)

    assert {q["id"] for q in prepared.questions} == {"engines", "device"}
    assert all(not c["selected"] for q in prepared.questions for c in q["choices"])
    # 完全な履歴を持つ手元のリポジトリを浅くしない
    confirm_update(ADDON_ID, {"engines": ["new"]}, expansion_dir=expansion)
    assert _git(expansion / ADDON_ID, "rev-parse", "--is-shallow-repository") == "false"
    assert load_saved_answers(ADDON_ID) == {"engines": ["new"], "device": ["cuda"]}
    assert sha1 != sha2


# ---------------------------------------------------------------------------
# 導入済みアドオンの質問の出し直し
# ---------------------------------------------------------------------------

def test_options_runs_only_newly_satisfied_steps(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    _install(repo, sha, expansion, {"engines": ["cloud"]})
    addon_dir = expansion / ADDON_ID
    (addon_dir / "markers.txt").unlink()

    options = get_installed_options(ADDON_ID, expansion_dir=expansion)
    engines = options.questions[0]
    assert [c["id"] for c in engines["choices"] if c["selected"]] == ["cloud"]
    # when があり、保存済みの答えではまだ満たしていない step だけが候補
    assert [s["name"] for s in options.steps] == ["gpt"]

    apply_installed_options(ADDON_ID, {"engines": ["cloud", "gpt"]}, expansion_dir=expansion)

    assert _markers(addon_dir) == ["gpt"]  # always も cloud もやり直さない
    assert load_saved_answers(ADDON_ID) == {"engines": ["cloud", "gpt"]}


def test_options_cannot_remove_a_previous_choice(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    _install(repo, sha, expansion, {"engines": ["cloud"]})

    with pytest.raises(AddonAnswersError):
        apply_installed_options(ADDON_ID, {"engines": ["gpt"]}, expansion_dir=expansion)


def test_options_cannot_change_a_single_choice_answer(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v2())
    _install(repo, sha, expansion, {"device": ["cpu"]})

    with pytest.raises(AddonAnswersError):
        apply_installed_options(ADDON_ID, {"device": ["cuda"]}, expansion_dir=expansion)


# ---------------------------------------------------------------------------
# git_clone の step: 既にあるフォルダは指定 commit に切り替える
# ---------------------------------------------------------------------------

def test_git_clone_switches_an_existing_checkout_to_the_commit(tmp_path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    (sub / "f.txt").write_text("one", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "1")
    sha1 = _git(sub, "rev-parse", "HEAD")
    (sub / "f.txt").write_text("two", encoding="utf-8")
    _git(sub, "commit", "-q", "-am", "2")
    sha2 = _git(sub, "rev-parse", "HEAD")

    addon_dir = tmp_path / "addon"
    addon_dir.mkdir()
    dest = addon_dir / "external" / "sub"
    _git(tmp_path, "clone", "-q", str(sub), str(dest))
    _git(dest, "checkout", "-q", sha1)

    # url は manifest では https 必須なので、ローカルの取得元を直接組み立てる
    step = GitCloneStep.model_construct(
        type="git_clone", name="sub", url=str(sub), commit=sha2, dest="external/sub",
        skip_if_exists=None, when={}, os=None,
    )
    addon_installer._exec_git_clone(step, addon_dir, lambda _e: None)

    assert _git(dest, "rev-parse", "HEAD") == sha2
    assert (dest / "f.txt").read_text(encoding="utf-8") == "two"


def test_git_clone_refuses_an_existing_non_repo_folder(tmp_path) -> None:
    addon_dir = tmp_path / "addon"
    (addon_dir / "external" / "sub").mkdir(parents=True)
    step = GitCloneStep.model_construct(
        type="git_clone", name="sub", url="https://example.com/r.git", commit="a" * 40,
        dest="external/sub", skip_if_exists=None, when={}, os=None,
    )
    with pytest.raises(addon_installer.AddonInstallError, match="git のリポジトリではありません"):
        addon_installer._exec_git_clone(step, addon_dir, lambda _e: None)


# ---------------------------------------------------------------------------
# アンインストール
# ---------------------------------------------------------------------------

def test_uninstall_removes_addon_install_but_keeps_addon_data(home, expansion, tmp_path) -> None:
    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    _install(repo, sha, expansion, {})
    env_dir = home / "addon_install" / ADDON_ID / "envs" / "gpt"
    env_dir.mkdir(parents=True)
    data_file = home / "user_data" / "addon_data" / ADDON_ID / "voice.wav"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text("x", encoding="utf-8")

    uninstall_addon(ADDON_ID, delete_data=False, expansion_dir=expansion)

    assert not (expansion / ADDON_ID).exists()
    assert not (home / "addon_install" / ADDON_ID).exists()
    assert data_file.exists()


# ---------------------------------------------------------------------------
# API (二段構えの契約)
# ---------------------------------------------------------------------------

def test_api_install_prepare_confirm_and_options(home, expansion, tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.deps import get_manager
    from api.routes import addon_catalog

    repo = tmp_path / "repo"
    sha = _commit(repo, _manifest_v1())
    version = SimpleNamespace(commit=sha, min_saiverse_version=None)
    entry = SimpleNamespace(
        repo_url=str(repo), latest="1.0.0",
        get_version=lambda v: version if v == "1.0.0" else None,
    )
    registry = SimpleNamespace(get_addon=lambda a: entry if a == ADDON_ID else None)
    monkeypatch.setattr(addon_catalog, "fetch_registry", lambda: registry)
    monkeypatch.setattr(addon_catalog, "EXPANSION_DATA_DIR", expansion)
    monkeypatch.setattr(addon_catalog, "_try_register_addon", lambda *_a: None)

    app = FastAPI()
    app.include_router(addon_catalog.router, prefix="/api/addon-catalog")
    app.dependency_overrides[get_manager] = lambda: SimpleNamespace()
    client = TestClient(app)

    res = client.post("/api/addon-catalog/install/prepare", json={"addon_id": ADDON_ID})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"addon_id", "version", "setup_version", "questions", "steps"}
    assert body["questions"][0]["id"] == "engines"

    res = client.post(
        "/api/addon-catalog/install/confirm",
        json={"addon_id": ADDON_ID, "answers": {"engines": ["nope"]}},
    )
    assert res.status_code == 400

    res = client.post(
        "/api/addon-catalog/install/confirm",
        json={"addon_id": ADDON_ID, "answers": {"engines": ["gpt"]}},
    )
    assert res.status_code == 200
    events = [json.loads(line[len("data: "):]) for line in res.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["phase"] == "finished" and events[-1]["ok"] is True, events[-1]
    assert _markers(expansion / ADDON_ID) == ["always", "gpt"]

    res = client.get(f"/api/addon-catalog/installed/{ADDON_ID}/options")
    assert res.status_code == 200
    choices = res.json()["questions"][0]["choices"]
    assert [c["id"] for c in choices if c["selected"]] == ["gpt"]

    res = client.post("/api/addon-catalog/install/cancel", json={"addon_id": ADDON_ID})
    assert res.status_code == 409  # 導入済み (prepare 中ではない)


# ---------------------------------------------------------------------------
# 検収 (2026-10-05 ローカルレビュー) で入れた直しの検査
# ---------------------------------------------------------------------------

def _installed_with_env_manifest(expansion: Path) -> Path:
    """env を共有する 2 step (when 違い) を持つアドオンを、導入済みの形で作る。"""
    manifest = {
        "name": ADDON_ID,
        "display_name": "X",
        "version": "1.0.0",
        "manifest_version": 2,
        "setup_version": 1,
        "setup": {
            "options": [{
                "id": "engines", "question": "q", "multiple": True,
                "choices": [{"id": "cloud", "label": "c", "default": True},
                            {"id": "gpt", "label": "g"}],
            }],
            "steps": [
                {"name": "cloud-pkgs", "type": "pip_install", "requirements": "r.txt",
                 "env": "eng", "when": {"engines": "cloud"}},
                {"name": "gpt-pkgs", "type": "pip_install", "requirements": "r.txt",
                 "env": "eng", "when": {"engines": "gpt"}},
            ],
        },
    }
    addon_dir = expansion / ADDON_ID
    _commit(addon_dir, manifest, extra_files={"r.txt": ""})
    return addon_dir


def test_options_reruns_env_mates_when_the_env_will_be_rebuilt(home, expansion) -> None:
    """空から作られる env を使う step は、前から条件を満たしていた分も走る。

    導入後に本体の Python の版が変わると、選択肢の追加で env が空の venv から
    作り直される。新しく条件を満たした step だけでは、前の step が入れていた
    パッケージが戻らない (2026-10-05 ローカルレビューの指摘 1)。
    """
    _installed_with_env_manifest(expansion)
    addon_installer.save_answers(ADDON_ID, {"engines": ["cloud"]})

    # env はあるが、作ったときの Python の版が本体と違う → 作り直し対象
    env_dir = addon_paths.get_addon_env_dir(ADDON_ID, "eng")
    env_dir.mkdir(parents=True)
    (env_dir / addon_paths.ADDON_ENV_RECORD_NAME).write_text(
        json.dumps({"python_version": "0.0.0"}), encoding="utf-8")

    plan = addon_installer.plan_options_apply(
        ADDON_ID, {"engines": ["cloud", "gpt"]}, expansion_dir=expansion)
    assert [s.name for s in plan.steps] == ["cloud-pkgs", "gpt-pkgs"]

    # env が本体の Python のままなら、新しく条件を満たした step だけ
    (env_dir / addon_paths.ADDON_ENV_RECORD_NAME).write_text(
        json.dumps({"python_version": addon_paths.current_python_version()}),
        encoding="utf-8")
    python = addon_paths.addon_env_python_path(env_dir)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    plan = addon_installer.plan_options_apply(
        ADDON_ID, {"engines": ["cloud", "gpt"]}, expansion_dir=expansion)
    assert [s.name for s in plan.steps] == ["gpt-pkgs"]


def test_options_reapply_with_unchanged_answers_repairs_a_missing_env(home, expansion) -> None:
    """答えを変えない「確定のやり直し」が、消えた専用環境の作り直しの入口になる。

    利用者が専用環境を手で消した (または壊れた) とき、選択肢の画面を開いて
    そのまま確定すれば作り直される。環境が健全なら従来どおり何も走らない
    (2026-10-05 の隔離の通し確認で、入口が無いことを踏んで直した)。
    """
    _installed_with_env_manifest(expansion)
    addon_installer.save_answers(ADDON_ID, {"engines": ["cloud", "gpt"]})

    # env が無い (消えた / 壊れて片づけた) → 答えが同じでも、その env を使う
    # step が全部走る
    plan = addon_installer.plan_options_apply(
        ADDON_ID, {"engines": ["cloud", "gpt"]}, expansion_dir=expansion)
    assert [s.name for s in plan.steps] == ["cloud-pkgs", "gpt-pkgs"]

    # env が健全なら何も走らない
    env_dir = addon_paths.get_addon_env_dir(ADDON_ID, "eng")
    env_dir.mkdir(parents=True)
    (env_dir / addon_paths.ADDON_ENV_RECORD_NAME).write_text(
        json.dumps({"python_version": addon_paths.current_python_version()}),
        encoding="utf-8")
    python = addon_paths.addon_env_python_path(env_dir)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    plan = addon_installer.plan_options_apply(
        ADDON_ID, {"engines": ["cloud", "gpt"]}, expansion_dir=expansion)
    assert plan.steps == []


def test_broken_pending_record_is_treated_as_absent(home, expansion) -> None:
    """commit の欠けた途中経過の記録は「無い」扱い (KeyError で 500 にしない)。"""
    install_dir = addon_paths.get_addon_install_dir(ADDON_ID)
    install_dir.mkdir(parents=True)
    (install_dir / "pending.json").write_text(
        json.dumps({"kind": "install"}), encoding="utf-8")
    assert get_pending_operation(ADDON_ID) is None
    with pytest.raises(AddonStateError):
        addon_installer.plan_install_confirm(ADDON_ID, None, expansion_dir=expansion)


@pytest.mark.parametrize("bad_id", ["..", "../x", "a/b", "a\b", "A", ""])
def test_api_rejects_malformed_addon_ids(bad_id) -> None:
    """API の入口で addon_id の形式を検査する (パスの組み立てに使われるため)。"""
    from fastapi import HTTPException

    from api.routes.addon_catalog import _check_addon_id

    with pytest.raises(HTTPException) as exc:
        _check_addon_id(bad_id)
    assert exc.value.status_code == 400
