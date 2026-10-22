"""アドオン installer: git 取得 + manifest 検証 + 導入時の質問 + setup steps 実行。

設計は ``docs/intent/addon_catalog_management.md`` 参照 (導入時の質問と専用の
Python 環境は「導入時の質問と、アドオン専用の Python 環境」の節)。

導入と更新は二段構え。質問は addon.json にあるので、取得前のアドオンの質問を
確認ダイアログに出すには、working tree へ展開する前に manifest を読む必要がある。

    1. prepare: 取得 (``git fetch``) までを行い、``git show <commit>:addon.json`` で
       manifest を読んで検証し、質問と step の一覧を返す。指す commit は
       ``~/.saiverse/addon_install/<id>/pending.json`` に残す (再起動を跨いでも
       confirm / cancel ができるように)。
    2. confirm: 答えを受け取って ``git checkout <commit>`` し、答えと OS で選んだ
       step を実行し、答えを保存する。
    3. cancel: 導入の prepare で作ったフォルダを消す。更新の cancel は記録を
       消すだけ (fetch しただけなので working tree は元のまま)。

提供する主要関数:
    - ``prepare_install`` / ``confirm_install`` / ``cancel_install``
    - ``prepare_update`` / ``confirm_update`` / ``cancel_update``
    - ``get_installed_options`` / ``apply_installed_options``: 導入済みアドオンの
      質問の出し直し (複数選べる質問の選択肢を足すだけ)
    - ``uninstall_addon``
    confirm 系は ``plan_*`` (副作用なしの検証と計画) と ``execute_*_plan`` (実行) に
    分かれている。API は plan の失敗を SSE を始める前に HTTP エラーとして返す。

すべて ``progress_callback`` を受け取り、ステップ毎に状態を流せる。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from saiverse.addon_manifest import (
    AddonManifest,
    DownloadFileStep,
    GitCloneStep,
    PipInstallStep,
    PlatformScriptStep,
    PythonScriptStep,
    RemoveDirStep,
    SetupStep,
    load_manifest,
)
from saiverse.addon_paths import (
    ADDON_ENV_RECORD_NAME,
    addon_env_matches_current_python,
    addon_env_python_path,
    current_python_version,
    get_addon_data_dir,
    get_addon_env_dir,
    get_addon_install_dir,
    read_addon_env_record,
)
from saiverse.data_paths import EXPANSION_DATA_DIR, PROJECT_ROOT
from saiverse.i18n_utils import normalize_i18n_dict

LOGGER = logging.getLogger(__name__)

# 導入時の答え: {質問 id: [選択肢 id, ...]} (一つだけ選ぶ質問も要素 1 の一覧)
Answers = Dict[str, List[str]]

SETUP_ANSWERS_NAME = "setup_answers.json"
PENDING_NAME = "pending.json"


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """Installer の進捗イベント。UI へストリーミングする時の payload にもなる。

    phase: "clone" / "manifest" / "step" / "rollback" / "done" / "error"
    step_index / step_total: phase=="step" の時のみ意味あり
    message: 人間可読の説明
    """
    phase: str
    message: str
    step_index: Optional[int] = None
    step_total: Optional[int] = None
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {"phase": self.phase, "message": self.message}
        if self.step_index is not None:
            d["step_index"] = self.step_index
        if self.step_total is not None:
            d["step_total"] = self.step_total
        if self.extra is not None:
            d["extra"] = self.extra
        return d


ProgressCallback = Callable[[ProgressEvent], None]


def _noop_progress(_event: ProgressEvent) -> None:  # pragma: no cover - default
    pass


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AddonInstallError(RuntimeError):
    """Installer の運用エラー (ユーザー入力起因の異常)。"""


class AddonManifestError(AddonInstallError):
    """addon.json の不在 / 不正。"""


class AddonAnswersError(AddonInstallError):
    """導入時の質問への答えが不正 (知らない質問・選択肢、外せない選択肢を外した等)。"""


class AddonStateError(AddonInstallError):
    """操作の前提となる状態に無い (導入済み / 未導入 / prepare されていない)。"""


class AddonVersionError(AddonInstallError):
    """SAIVerse 本体のバージョンが、アドオンの要求 (min_saiverse_version) に足りない。"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


def current_os() -> str:
    """実行中の OS を step.os の語彙 (windows / linux / macos) で返す。"""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def _run_subprocess(
    args: list[str],
    cwd: Path,
    progress: ProgressCallback,
    label: str,
    env: Optional[dict] = None,
) -> None:
    """subprocess を回し、stdout/stderr を逐次 progress 経由でログに流す。

    失敗時は returncode を含めて AddonInstallError を raise。
    """
    LOGGER.info("installer: running %s (cwd=%s)", args, cwd)
    progress(ProgressEvent(phase="step", message=f"{label}: 実行中 ({args[0]})"))

    effective_env = os.environ.copy()
    if env:
        effective_env.update(env)

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=effective_env,
        )
    except FileNotFoundError as e:
        raise AddonInstallError(
            f"{label}: 実行ファイルが見つかりません ({args[0]!r}): {e}"
        ) from e

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        LOGGER.debug("installer[%s]: %s", label, line)
        progress(ProgressEvent(phase="step", message=line, extra={"label": label}))
    rc = proc.wait()
    if rc != 0:
        raise AddonInstallError(
            f"{label}: 終了コード {rc} で失敗 ({' '.join(args)})"
        )


def _git(args: list[str], cwd: Path, progress: ProgressCallback, label: str) -> None:
    _run_subprocess(["git", *args], cwd=cwd, progress=progress, label=label)


def _git_output(args: list[str], cwd: Path) -> str:
    """git を実行して stdout を返す (進捗には流さない、読み取り用)。"""
    LOGGER.debug("installer: git %s (cwd=%s)", args, cwd)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise AddonInstallError(f"git が見つかりません: {e}") from e
    if proc.returncode != 0:
        raise AddonInstallError(
            f"git {' '.join(args)} が終了コード {proc.returncode} で失敗: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _fetch_args(repo_dir: Path, url: str, commit: str) -> list[str]:
    """既存のリポジトリへ ``url`` から ``commit`` を取得する git fetch の引数。

    origin ではなく URL を直接渡す (取得先を manifest / カタログの URL に揃える)。
    手で git clone した完全な履歴のリポジトリを浅くしないように、既に浅い
    リポジトリのときだけ ``--depth 1`` を付ける。
    """
    try:
        shallow = _git_output(["rev-parse", "--is-shallow-repository"], cwd=repo_dir).strip()
    except AddonInstallError:
        shallow = "false"
    depth = ["--depth", "1"] if shallow == "true" else []
    return ["fetch", *depth, url, commit]


def _check_addon_dir_path(addon_dir: Path, rel: str, field: str) -> Path:
    """addon ディレクトリ相対の安全なパスを絶対パスに解決。

    Pydantic 側で文字列レベルの検証は済んでいるが、最終的な実体解決でも
    addon_dir の外に出ていないかを再確認 (defense in depth)。
    """
    full = (addon_dir / rel).resolve()
    addon_root = addon_dir.resolve()
    try:
        full.relative_to(addon_root)
    except ValueError as e:
        raise AddonInstallError(
            f"{field}: {rel!r} resolved outside addon dir ({full})"
        ) from e
    return full


def _check_data_dir_path(data_dir: Path, rel: str, field: str) -> Path:
    full = (data_dir / rel).resolve()
    data_root = data_dir.resolve()
    try:
        full.relative_to(data_root)
    except ValueError as e:
        raise AddonInstallError(
            f"{field}: {rel!r} resolved outside data dir ({full})"
        ) from e
    return full


def _on_rm_error(func, path, exc_info):  # noqa: ANN001 - shutil.rmtree onerror signature
    """Windows で read-only ファイル (git の pack index 等) を rmtree するための onerror。

    read-only ビットを落としてから再試行。それでも駄目なら raise。
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc_info[1]


def _safe_rmtree(path: Path) -> None:
    """Windows の read-only git ファイルに耐える rmtree。"""
    if not path.exists():
        return
    shutil.rmtree(path, onerror=_on_rm_error)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# SAIVerse 本体のバージョン検査 (registry の min_saiverse_version)
# ---------------------------------------------------------------------------

def check_min_saiverse_version(
    required: Optional[str], current: Optional[str] = None
) -> None:
    """本体のバージョンが ``required`` 以上か確かめ、足りなければ断る。

    本体のバージョンの正本はリポジトリルートの VERSION (``saiverse.__version__``)。
    比較は PEP 440 (``api/routes/system.py`` / ``saiverse/upgrade.py`` と同じ順序)。
    取得 (git) の前に呼ぶこと — 更新は取得後に止めても元の commit に戻らない。
    """
    if not required:
        return
    if current is None:
        from saiverse import __version__ as current
    try:
        required_v = Version(required.strip().lstrip("v"))
    except InvalidVersion as e:
        raise AddonVersionError(
            f"カタログに書かれた必要な SAIVerse のバージョン ({required!r}) が読めません"
        ) from e
    try:
        current_v = Version(current.strip().lstrip("v"))
    except InvalidVersion as e:
        raise AddonVersionError(
            f"この SAIVerse のバージョン ({current!r}) が読めないため、"
            f"このアドオンが必要とするバージョン ({required}) を満たすか確かめられません"
        ) from e
    if current_v < required_v:
        raise AddonVersionError(
            f"このアドオンには SAIVerse {required} 以降が必要です "
            f"(いまの SAIVerse は {current})。先に SAIVerse を更新してください。"
        )


# ---------------------------------------------------------------------------
# 導入時の答え: 検証・既定値・選別・保存
# ---------------------------------------------------------------------------

def default_answers(manifest: AddonManifest) -> Answers:
    """全質問を既定値で答えたもの。"""
    return {o.id: o.default_answer() for o in manifest.setup_options}


def prune_answers(manifest: AddonManifest, answers: Answers) -> Answers:
    """manifest に無くなった質問・選択肢の答えを捨てる。

    残すと、もう画面に出ない答えが ``when`` の判定に効き続ける。一つだけ選ぶ
    質問で選択肢が消えて答えが空になったら、その質問ごと捨てる (未回答に戻す)。
    """
    result: Answers = {}
    for qid, chosen in answers.items():
        opt = manifest.setup.get_option(qid) if manifest.setup else None
        if opt is None:
            continue
        valid = opt.choice_ids()
        kept = [c for c in chosen if c in valid]
        if not opt.multiple and len(kept) != 1:
            continue
        if opt.multiple or kept:
            result[qid] = kept
    return result


def normalize_answers(manifest: AddonManifest, raw: Optional[dict]) -> Answers:
    """利用者 (API / CLI) から来た答えを検証して正規化する。

    形は ``{質問 id: [選択肢 id, ...]}``。知らない質問・選択肢、一つだけ選ぶ質問に
    1 個以外の答え、は ``AddonAnswersError``。渡されなかった質問は含めない
    (既定値で埋めるのは ``fill_defaults``)。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AddonAnswersError("answers must be an object of {question_id: [choice_id, ...]}")
    result: Answers = {}
    for qid, chosen in raw.items():
        opt = manifest.setup.get_option(qid) if manifest.setup else None
        if opt is None:
            raise AddonAnswersError(f"unknown question {qid!r}")
        if not isinstance(chosen, list) or not all(isinstance(c, str) for c in chosen):
            raise AddonAnswersError(f"question {qid!r}: the answer must be a list of choice ids")
        valid = opt.choice_ids()
        deduped: List[str] = []
        for c in chosen:
            if c not in valid:
                raise AddonAnswersError(f"question {qid!r}: unknown choice {c!r}")
            if c not in deduped:
                deduped.append(c)
        if not opt.multiple and len(deduped) != 1:
            raise AddonAnswersError(
                f"question {qid!r} takes exactly one choice, got {len(deduped)}"
            )
        result[qid] = deduped
    return result


def fill_defaults(manifest: AddonManifest, answers: Answers) -> Answers:
    """答えの無い質問を既定値で埋める。"""
    result = dict(answers)
    for opt in manifest.setup_options:
        if opt.id not in result:
            result[opt.id] = opt.default_answer()
    return result


def step_when_matches(step: SetupStep, answers: Answers) -> bool:
    """``when`` の全部の質問の答えに、その選択肢が含まれるか (空の when は常に真)。"""
    return all(cid in answers.get(qid, []) for qid, cid in step.when.items())


def step_os_matches(step: SetupStep, os_name: Optional[str] = None) -> bool:
    if step.os is None:
        return True
    return (os_name or current_os()) in step.os


def select_steps_for_os(
    steps: Iterable[SetupStep], os_name: Optional[str] = None
) -> List[SetupStep]:
    return [s for s in steps if step_os_matches(s, os_name)]


def select_steps(
    steps: Iterable[SetupStep], answers: Answers, os_name: Optional[str] = None
) -> List[SetupStep]:
    """答えと OS で、実行する step を選ぶ。"""
    return [
        s for s in steps
        if step_os_matches(s, os_name) and step_when_matches(s, answers)
    ]


def load_saved_answers(addon_id: str) -> Optional[Answers]:
    """保存された答え。ファイルが無ければ None (= 答えたことが無い)。"""
    path = get_addon_install_dir(addon_id) / SETUP_ANSWERS_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("installer: unreadable setup answers %s", path, exc_info=True)
        return None
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        LOGGER.warning("installer: setup answers %s has no 'answers' object", path)
        return None
    return {
        str(q): [str(c) for c in cs]
        for q, cs in answers.items()
        if isinstance(cs, list)
    }


def save_answers(addon_id: str, answers: Answers) -> None:
    path = get_addon_install_dir(addon_id) / SETUP_ANSWERS_NAME
    _write_json_atomic(path, {"answers": answers})


# ---------------------------------------------------------------------------
# prepare と confirm の間の記録 (pending.json)
# ---------------------------------------------------------------------------

def get_pending_operation(addon_id: str) -> Optional[dict]:
    """prepare 済みで confirm / cancel を待っている操作。無ければ None。

    返値: ``{"kind": "install" | "update", "commit": ..., "repo_url": ...}``
    壊れた記録 (読めない / 欄が欠けている) は、無いものとして扱う — 欠けた
    commit を後段が KeyError で踏むより、「準備されていません」で止まる方がよい。
    """
    path = get_addon_install_dir(addon_id) / PENDING_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("installer: unreadable pending record %s", path, exc_info=True)
        return None
    if not isinstance(data, dict) or data.get("kind") not in ("install", "update"):
        return None
    commit = data.get("commit")
    repo_url = data.get("repo_url")
    if not (isinstance(commit, str) and commit and isinstance(repo_url, str)):
        LOGGER.warning("installer: incomplete pending record %s: %s", path, data)
        return None
    return data


def _write_pending(addon_id: str, kind: str, commit: str, repo_url: str) -> None:
    _write_json_atomic(
        get_addon_install_dir(addon_id) / PENDING_NAME,
        {"kind": kind, "commit": commit, "repo_url": repo_url},
    )


def _clear_pending(addon_id: str) -> None:
    install_dir = get_addon_install_dir(addon_id)
    path = install_dir / PENDING_NAME
    if path.exists():
        path.unlink()
    # pending のためだけに作ったフォルダなら消す (空のときだけ)
    try:
        install_dir.rmdir()
    except OSError:
        pass


def _require_pending(addon_id: str, kind: str) -> dict:
    pending = get_pending_operation(addon_id)
    if pending is None or pending.get("kind") != kind:
        verb = "導入" if kind == "install" else "更新"
        raise AddonStateError(
            f"{addon_id} の{verb}は準備されていません (prepare からやり直してください)"
        )
    return pending


# ---------------------------------------------------------------------------
# アドオン専用の Python 環境
# ---------------------------------------------------------------------------

def _step_env(step: SetupStep) -> Optional[str]:
    if isinstance(step, (PipInstallStep, PythonScriptStep, PlatformScriptStep)):
        return step.env
    return None


# このプロセスで pip を上げ終えた専用の環境のフォルダ (``str(env_dir)``)。
# 専用の環境の pip は、venv を作ったときの Python に同梱された版のまま残る
# (実測: 2026-10-22 の時点で voice-tts の env は pip 24.3.1、同梱の certifi
# 2024.08.30)。setup の step の pip install はこの pip でダウンロードするので、
# ここが古いと step のダウンロードの TLS 検証が古い certifi (pip._vendor.certifi)
# で行われる (2026-10-21、certifi 2026.10.19 が鍵の漏れた emSign を信頼する
# 証明書発行元の一覧から外した)。失敗した環境も入れて、同じプロセスの中で再試行を繰り返さない。
_PIP_UPGRADED_ENVS: set[str] = set()


def _upgrade_env_pip(env_dir: Path, env_name: str, progress: ProgressCallback) -> None:
    """専用の環境の pip を上げる。失敗しても setup は続ける (pip はいまの版のまま動く)。"""
    _PIP_UPGRADED_ENVS.add(str(env_dir))
    try:
        _run_subprocess(
            [str(addon_env_python_path(env_dir)), "-m", "pip", "install", "--upgrade", "pip"],
            cwd=env_dir,
            progress=progress,
            label=f"upgrade pip ({env_name})",
            env=_env_process_vars(env_dir),
        )
    except AddonInstallError as exc:
        LOGGER.warning("installer: pip upgrade in env %s failed; continuing: %s", env_dir, exc)
        progress(ProgressEvent(
            phase="step",
            message=(
                f"専用の Python 環境 {env_name} の pip を更新できませんでした。"
                f"いまの pip のまま続けます ({exc})"
            ),
        ))


def _ensure_addon_env(addon_id: str, env_name: str, progress: ProgressCallback) -> Path:
    """専用の環境を用意して、その場所を返す。

    無ければ本体の Python (``sys.executable``) の ``-m venv`` で作り、使った Python の
    バージョンを環境の中に記録する。記録が無い・記録の Python の major.minor が本体と
    違う環境は作り直す (パッチ番号だけの違いは作り直さない、
    ``addon_env_matches_current_python``)。記録は venv の作成に成功してから書くので、
    途中で失敗した環境は次の実行で作り直される。

    作った直後と、既存の環境をこのプロセスで初めて使うときに、その環境の pip を
    上げる (``_upgrade_env_pip``)。
    """
    env_dir = get_addon_env_dir(addon_id, env_name)
    current = current_python_version()
    record = read_addon_env_record(env_dir)
    python = addon_env_python_path(env_dir)
    if addon_env_matches_current_python(record) and python.exists():
        if str(env_dir) not in _PIP_UPGRADED_ENVS:
            _upgrade_env_pip(env_dir, env_name, progress)
        return env_dir
    if env_dir.exists():
        recorded = record.get("python_version") if record else None
        progress(ProgressEvent(
            phase="step",
            message=(
                f"専用の Python 環境 {env_name} を作り直します "
                f"(作成時の Python: {recorded or '不明'}、いまの Python: {current})"
            ),
        ))
        _safe_rmtree(env_dir)
    else:
        progress(ProgressEvent(
            phase="step", message=f"専用の Python 環境 {env_name} を作成します",
        ))
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_subprocess(
        [sys.executable, "-m", "venv", str(env_dir)],
        cwd=env_dir.parent,
        progress=progress,
        label=f"venv {env_name}",
    )
    _write_json_atomic(
        env_dir / ADDON_ENV_RECORD_NAME,
        {
            "python_version": current,
            "base_python": sys.executable,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        },
    )
    _upgrade_env_pip(env_dir, env_name, progress)
    return env_dir


def _env_process_vars(env_dir: Path) -> dict:
    """専用の環境で起動する子プロセスの環境変数 (VIRTUAL_ENV と PATH をその環境へ)。"""
    bin_dir = addon_env_python_path(env_dir).parent
    env_vars = {
        "VIRTUAL_ENV": str(env_dir),
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
    }
    if os.name == "nt":
        # wheel の無いパッケージを pip がソースビルドするとき、CP932 ロケールだと
        # MSVC が非 ASCII を含むソースで字句解析に失敗することがある。/utf-8 を
        # 渡して回避する (voice-tts の旧 setup.bat と同じ手当て。MSVC 以外は読まない)。
        # 利用者が自分で CL を立てていたら消さずに後ろへ足す。
        existing_cl = os.environ.get("CL", "").strip()
        env_vars["CL"] = f"{existing_cl} /utf-8".strip()
    return env_vars


def _resolve_step_env(
    step: SetupStep, addon_id: Optional[str], progress: ProgressCallback
) -> Optional[Path]:
    env_name = _step_env(step)
    if env_name is None:
        return None
    if addon_id is None:
        raise AddonInstallError(f"{step.name}: env step needs the addon id")
    return _ensure_addon_env(addon_id, env_name, progress)


def _env_needs_full_run(addon_id: str, env_name: str) -> bool:
    """この env が、次の setup で空から作られる (= 作り直しか新規作成) か。

    空から作られる env は、新しく条件を満たした step だけ走らせても中身が
    戻らない — その env を使う step を全部走らせる必要がある
    (``plan_options_apply`` が使う)。
    """
    env_dir = get_addon_env_dir(addon_id, env_name)
    record = read_addon_env_record(env_dir)
    return (
        not addon_env_matches_current_python(record)
        or not addon_env_python_path(env_dir).exists()
    )


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

def _step_should_skip(step: SetupStep, addon_dir: Path, progress: ProgressCallback) -> bool:
    if step.skip_if_exists:
        skip_path = _check_addon_dir_path(addon_dir, step.skip_if_exists, "skip_if_exists")
        if skip_path.exists():
            progress(ProgressEvent(
                phase="step",
                message=f"{step.name}: skip_if_exists={step.skip_if_exists} 既存のためスキップ"
            ))
            return True
    return False


# 本体が固定した部品の一覧 (docs/intent/dependency_management.md)。アドオンの
# pip install にはこれを constraints (-c) として渡す。アドオンは本体の venv を
# 共有するので、constraints が無いとアドオンの requirements が本体の部品を別の版へ
# 動かせてしまう (逆向きの事故が 2026-09-02 の voice-tts 無音の原因)。
# 例外は env の付いた step — 専用の環境に入れるので本体の venv は動かない。
CORE_REQUIREMENTS_LOCK = PROJECT_ROOT / "requirements.lock"


def _exec_pip_install(
    step: PipInstallStep,
    addon_dir: Path,
    progress: ProgressCallback,
    addon_id: Optional[str] = None,
) -> None:
    req_path = _check_addon_dir_path(addon_dir, step.requirements, "requirements")
    if not req_path.exists():
        raise AddonInstallError(
            f"pip_install: requirements file not found: {req_path}"
        )
    env_dir = _resolve_step_env(step, addon_id, progress)
    if env_dir is not None:
        # 専用の環境の pip。本体の venv に何も入れないので constraints は渡さない
        # (渡すと、本体と両立しないパッケージを専用の環境に入れる目的が果たせない)。
        _run_subprocess(
            [str(addon_env_python_path(env_dir)), "-m", "pip", "install", "-r", str(req_path)],
            cwd=addon_dir,
            progress=progress,
            label=step.name,
            env=_env_process_vars(env_dir),
        )
        return
    if not CORE_REQUIREMENTS_LOCK.exists():
        # 本体の checkout に lock が無い = 配布物が壊れている。pip の
        # "Could not open requirements file" より先に、何が無いのかを言う。
        raise AddonInstallError(
            f"pip_install: core lock file not found: {CORE_REQUIREMENTS_LOCK} "
            "(the SAIVerse checkout is incomplete; update it first)"
        )
    _run_subprocess(
        [
            sys.executable, "-m", "pip", "install",
            "-r", str(req_path),
            # 本体が固定した版を動かさせない。動かす必要があるアドオンはここで
            # 失敗して理由が出る (入ってから黙って壊れる代わりに)。
            "-c", str(CORE_REQUIREMENTS_LOCK),
        ],
        cwd=addon_dir,
        progress=progress,
        label=step.name,
    )


def _exec_platform_script(
    step: PlatformScriptStep,
    addon_dir: Path,
    progress: ProgressCallback,
    addon_id: Optional[str] = None,
) -> None:
    script_rel = step.windows if _is_windows() else step.unix
    if not script_rel:
        progress(ProgressEvent(
            phase="step",
            message=f"{step.name}: 現在のプラットフォームではスクリプト未定義のためスキップ"
        ))
        return
    script_abs = _check_addon_dir_path(addon_dir, script_rel, "platform_script")
    if not script_abs.exists():
        raise AddonInstallError(f"platform_script: script not found: {script_abs}")

    if _is_windows():
        args = ["cmd.exe", "/C", str(script_abs)]
    else:
        # 実行権限がない場合に備えて bash 経由で実行 (sh ファイルでも .bash でも対応)
        args = ["bash", str(script_abs)]
    env_dir = _resolve_step_env(step, addon_id, progress)
    _run_subprocess(
        args,
        cwd=addon_dir,
        progress=progress,
        label=step.name,
        env=_env_process_vars(env_dir) if env_dir is not None else None,
    )


def _exec_python_script(
    step: PythonScriptStep,
    addon_dir: Path,
    progress: ProgressCallback,
    addon_id: Optional[str] = None,
) -> None:
    script_abs = _check_addon_dir_path(addon_dir, step.script, "python_script")
    if not script_abs.exists():
        raise AddonInstallError(f"python_script: script not found: {script_abs}")
    env_dir = _resolve_step_env(step, addon_id, progress)
    if env_dir is not None:
        python = str(addon_env_python_path(env_dir))
        env_vars: Optional[dict] = _env_process_vars(env_dir)
    else:
        python = sys.executable
        env_vars = None
    _run_subprocess(
        [python, str(script_abs), *step.args],
        cwd=addon_dir,
        progress=progress,
        label=step.name,
        env=env_vars,
    )


def _exec_remove_dir(
    step: RemoveDirStep,
    addon_dir: Path,
    data_dir: Path,
    progress: ProgressCallback,
) -> None:
    if step.target == "addon":
        target = _check_addon_dir_path(addon_dir, step.path, "remove_dir.path")
    else:  # "data"
        target = _check_data_dir_path(data_dir, step.path, "remove_dir.path")

    if not target.exists():
        progress(ProgressEvent(
            phase="step",
            message=f"{step.name}: {target} は存在しないのでスキップ"
        ))
        return
    progress(ProgressEvent(phase="step", message=f"{step.name}: {target} を削除"))
    if target.is_dir():
        _safe_rmtree(target)
    else:
        target.unlink()


def _exec_git_clone(
    step: GitCloneStep, addon_dir: Path, progress: ProgressCallback
) -> None:
    dest_abs = _check_addon_dir_path(addon_dir, step.dest, "git_clone.dest")
    if dest_abs.exists():
        # 既にあるときは飛ばさず、指定の commit に切り替える。飛ばすと、更新で
        # setup_version と commit を上げても古い commit のまま残る (不変条件 3・4)。
        if not (dest_abs / ".git").exists():
            # .git が無いフォルダで git を叩くと、親 (アドオン自身) のリポジトリを
            # 操作してしまう。
            raise AddonInstallError(
                f"{step.name}: {dest_abs} は既にありますが git のリポジトリではありません。"
                "このフォルダを消してから setup をやり直してください。"
            )
        try:
            head = _git_output(["rev-parse", "HEAD"], cwd=dest_abs).strip()
        except AddonInstallError:
            head = ""
        if head == step.commit:
            progress(ProgressEvent(
                phase="step",
                message=f"{step.name}: {dest_abs} は既に {step.commit[:7]} です",
            ))
            return
        progress(ProgressEvent(
            phase="step",
            message=f"{step.name}: {dest_abs} を {step.commit[:7]} に切り替えます",
        ))
        _git(
            _fetch_args(dest_abs, step.url, step.commit),
            cwd=dest_abs,
            progress=progress,
            label=step.name,
        )
        _git(["checkout", step.commit], cwd=dest_abs, progress=progress, label=step.name)
        return
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    # shallow clone してから commit checkout (sub-repo を pin)
    _git(
        ["clone", "--no-checkout", step.url, str(dest_abs)],
        cwd=addon_dir,
        progress=progress,
        label=step.name,
    )
    _git(
        ["fetch", "--depth", "1", "origin", step.commit],
        cwd=dest_abs,
        progress=progress,
        label=step.name,
    )
    _git(
        ["checkout", step.commit],
        cwd=dest_abs,
        progress=progress,
        label=step.name,
    )


def _exec_download_file(
    step: DownloadFileStep,
    data_dir: Path,
    progress: ProgressCallback,
) -> None:
    dest_abs = _check_data_dir_path(data_dir, step.dest_data, "download_file.dest_data")
    dest_abs.parent.mkdir(parents=True, exist_ok=True)

    progress(ProgressEvent(
        phase="step", message=f"{step.name}: {step.url} を DL 中..."
    ))
    # 一時ファイルに DL してから SHA256 検証 → atomic rename
    with tempfile.NamedTemporaryFile(
        delete=False, dir=str(dest_abs.parent), prefix=".dl-"
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        urllib.request.urlretrieve(step.url, tmp_path)  # noqa: S310 - URL は manifest で http(s) 検証済
        actual = _sha256_file(tmp_path)
        if actual.lower() != step.sha256.lower():
            raise AddonInstallError(
                f"download_file: SHA256 mismatch for {step.url}\n"
                f"  expected: {step.sha256}\n"
                f"  actual:   {actual}"
            )
        if dest_abs.exists():
            dest_abs.unlink()
        tmp_path.replace(dest_abs)
        progress(ProgressEvent(
            phase="step", message=f"{step.name}: SHA256 検証 OK → {dest_abs} に配置"
        ))
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _execute_step(
    step: SetupStep,
    addon_id: str,
    addon_dir: Path,
    data_dir: Path,
    progress: ProgressCallback,
) -> None:
    if _step_should_skip(step, addon_dir, progress):
        return
    if isinstance(step, PipInstallStep):
        _exec_pip_install(step, addon_dir, progress, addon_id)
    elif isinstance(step, PlatformScriptStep):
        _exec_platform_script(step, addon_dir, progress, addon_id)
    elif isinstance(step, PythonScriptStep):
        _exec_python_script(step, addon_dir, progress, addon_id)
    elif isinstance(step, RemoveDirStep):
        _exec_remove_dir(step, addon_dir, data_dir, progress)
    elif isinstance(step, GitCloneStep):
        _exec_git_clone(step, addon_dir, progress)
    elif isinstance(step, DownloadFileStep):
        _exec_download_file(step, data_dir, progress)
    else:
        raise AddonInstallError(f"unknown step type: {type(step).__name__}")


def _execute_steps(
    steps: list[SetupStep],
    addon_id: str,
    addon_dir: Path,
    data_dir: Path,
    progress: ProgressCallback,
) -> None:
    total = len(steps)
    for i, step in enumerate(steps, start=1):
        progress(ProgressEvent(
            phase="step",
            step_index=i,
            step_total=total,
            message=f"[{i}/{total}] {step.name}",
        ))
        _execute_step(step, addon_id, addon_dir, data_dir, progress)


# ---------------------------------------------------------------------------
# Manifest IO
# ---------------------------------------------------------------------------

def _parse_manifest_text(text: str, source: str) -> AddonManifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AddonManifestError(f"addon.json ({source}) is not valid JSON: {e}") from e
    try:
        return load_manifest(data)
    except ValidationError as e:
        raise AddonManifestError(f"addon.json ({source}) validation failed:\n{e}") from e


def _read_manifest(addon_dir: Path) -> AddonManifest:
    manifest_path = addon_dir / "addon.json"
    if not manifest_path.exists():
        raise AddonManifestError(f"addon.json not found in {addon_dir}")
    return _parse_manifest_text(manifest_path.read_text(encoding="utf-8"), str(manifest_path))


def _read_manifest_at_commit(addon_dir: Path, commit: str) -> AddonManifest:
    """working tree に展開せずに、指定 commit の addon.json を読む。"""
    try:
        text = _git_output(["show", f"{commit}:addon.json"], cwd=addon_dir)
    except AddonInstallError as e:
        raise AddonManifestError(f"addon.json not found at commit {commit}: {e}") from e
    return _parse_manifest_text(text, f"commit {commit[:7]}")


def _check_manifest_name(manifest: AddonManifest, addon_id: str) -> None:
    if manifest.name != addon_id:
        raise AddonManifestError(
            f"manifest name mismatch: addon.json name={manifest.name!r} "
            f"but addon_id={addon_id!r}"
        )


# ---------------------------------------------------------------------------
# 確認ダイアログに出す形 (API / CLI 共通)
# ---------------------------------------------------------------------------

def describe_questions(
    manifest: AddonManifest,
    selected: Optional[Answers] = None,
    only: Optional[Iterable[str]] = None,
) -> List[dict]:
    """質問の一覧。``selected`` にある選択肢には ``selected: true`` を付ける。

    質問文・選択肢の表示文は、言語ごとの辞書に正規化して返す (アドオン一覧の
    ``display_name_i18n`` と同じ流儀。文字列だけのものは ``{"ja": ...}`` になる)。
    """
    selected = selected or {}
    only_set = set(only) if only is not None else None
    result = []
    for opt in manifest.setup_options:
        if only_set is not None and opt.id not in only_set:
            continue
        chosen = selected.get(opt.id, [])
        result.append({
            "id": opt.id,
            "question": normalize_i18n_dict(opt.question),
            "multiple": opt.multiple,
            "choices": [
                {
                    "id": c.id,
                    "label": normalize_i18n_dict(c.label),
                    "default": c.default,
                    "selected": c.id in chosen,
                }
                for c in opt.choices
            ],
        })
    return result


def describe_steps(steps: Iterable[SetupStep]) -> List[dict]:
    """step の一覧 (``when`` は {質問 id: 選択肢 id} のまま、無ければ {})。"""
    return [
        {
            "name": s.name,
            "type": s.type,
            "when": dict(s.when),
            "env": _step_env(s),
        }
        for s in steps
    ]


@dataclass
class PreparedSetup:
    """prepare / 質問の出し直しの結果。確認ダイアログに出すもの。"""
    addon_id: str
    manifest: AddonManifest
    needs_setup: bool
    questions: List[dict] = field(default_factory=list)
    steps: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "addon_id": self.addon_id,
            "version": self.manifest.version,
            "setup_version": self.manifest.setup_version,
            "needs_setup": self.needs_setup,
            "questions": self.questions,
            "steps": self.steps,
        }


@dataclass
class SetupPlan:
    """confirm の計画。``plan_*`` が副作用なしで作り、``execute_*_plan`` が実行する。"""
    kind: str  # "install" / "update" / "options"
    addon_id: str
    addon_dir: Path
    manifest: AddonManifest
    steps: List[SetupStep]
    answers: Optional[Answers]  # 実行後に保存する答え (None は保存しない)
    commit: Optional[str] = None
    old_setup_version: Optional[int] = None


# ---------------------------------------------------------------------------
# 導入 (install)
# ---------------------------------------------------------------------------

def _infer_addon_id(repo_url: str) -> str:
    repo_name = repo_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    return repo_name


def prepare_install(
    repo_url: str,
    commit: str,
    addon_id: Optional[str] = None,
    *,
    min_saiverse_version: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> PreparedSetup:
    """導入の一段目: 取得して manifest を読み、質問と step の一覧を返す。

    ``git clone --no-checkout`` + ``git fetch --depth 1 origin <commit>`` までで、
    working tree には展開しない (confirm までアドオンとして認識されない)。
    前回の prepare の残り (confirm も cancel もされなかったもの) があれば作り直す。
    失敗したら作ったフォルダを消す。
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    check_min_saiverse_version(min_saiverse_version)

    addon_id = addon_id or _infer_addon_id(repo_url)
    addon_dir = expansion / addon_id
    if addon_dir.exists():
        pending = get_pending_operation(addon_id)
        if pending is not None and pending.get("kind") == "install":
            LOGGER.info("installer: discarding a previous install prepare of %s", addon_id)
            _safe_rmtree(addon_dir)
            _clear_pending(addon_id)
        else:
            raise AddonStateError(
                f"addon directory already exists: {addon_dir} "
                "(update it, or uninstall it first)"
            )
    expansion.mkdir(parents=True, exist_ok=True)

    progress(ProgressEvent(phase="clone", message=f"{repo_url} を {addon_dir} に取得中..."))
    try:
        _git(
            ["clone", "--no-checkout", repo_url, str(addon_dir)],
            cwd=expansion, progress=progress, label="clone",
        )
        _git(
            ["fetch", "--depth", "1", "origin", commit],
            cwd=addon_dir, progress=progress, label="fetch",
        )
        progress(ProgressEvent(phase="manifest", message="addon.json を検証中..."))
        manifest = _read_manifest_at_commit(addon_dir, commit)
        _check_manifest_name(manifest, addon_id)
        _write_pending(addon_id, "install", commit, repo_url)
    except BaseException:
        if addon_dir.exists():
            try:
                _safe_rmtree(addon_dir)
            except OSError as cleanup_err:
                LOGGER.warning(
                    "installer: prepare cleanup failed for %s: %s", addon_dir, cleanup_err
                )
        raise

    return PreparedSetup(
        addon_id=addon_id,
        manifest=manifest,
        needs_setup=True,
        questions=describe_questions(manifest),
        steps=describe_steps(select_steps_for_os(manifest.setup_steps)),
    )


def plan_install_confirm(
    addon_id: str,
    answers: Optional[dict],
    expansion_dir: Optional[Path] = None,
) -> SetupPlan:
    """導入の二段目の計画 (副作用なし)。答えが不正なら ``AddonAnswersError``。"""
    expansion = expansion_dir or EXPANSION_DATA_DIR
    pending = _require_pending(addon_id, "install")
    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonStateError(f"prepared addon directory is gone: {addon_dir}")
    commit = pending["commit"]
    manifest = _read_manifest_at_commit(addon_dir, commit)
    _check_manifest_name(manifest, addon_id)
    final = fill_defaults(manifest, normalize_answers(manifest, answers))
    return SetupPlan(
        kind="install",
        addon_id=addon_id,
        addon_dir=addon_dir,
        manifest=manifest,
        steps=select_steps(manifest.setup_steps, final),
        answers=final,
        commit=commit,
    )


def execute_install_plan(
    plan: SetupPlan, progress_callback: Optional[ProgressCallback] = None
) -> AddonManifest:
    """導入の二段目: checkout して、選ばれた step を実行し、答えを保存する。

    失敗したらアドオンのフォルダと ``addon_install/<id>/`` を消す (rollback)。
    永続データ (``addon_data/``) は触らない。
    """
    progress = progress_callback or _noop_progress
    addon_id = plan.addon_id
    addon_dir = plan.addon_dir
    manifest = plan.manifest
    try:
        _git(["checkout", plan.commit], cwd=addon_dir, progress=progress, label="checkout")
        data_dir = get_addon_data_dir(addon_id)
        _run_selected_steps(plan, data_dir, progress)
        if plan.answers is not None:
            save_answers(addon_id, plan.answers)
        _clear_pending(addon_id)
        progress(ProgressEvent(
            phase="done",
            message=f"{addon_id} v{manifest.version} のインストール完了",
        ))
        return manifest
    except BaseException as e:
        progress(ProgressEvent(
            phase="rollback",
            message=f"エラー発生、{addon_dir} を削除します: {e}",
        ))
        for path in (addon_dir, get_addon_install_dir(addon_id)):
            try:
                _safe_rmtree(path)
            except OSError as cleanup_err:
                LOGGER.warning(
                    "installer: rollback cleanup failed for %s: %s", path, cleanup_err
                )
        raise


def confirm_install(
    addon_id: str,
    answers: Optional[dict] = None,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> AddonManifest:
    return execute_install_plan(
        plan_install_confirm(addon_id, answers, expansion_dir), progress_callback
    )


def cancel_install(addon_id: str, expansion_dir: Optional[Path] = None) -> None:
    """導入の prepare で作ったフォルダを消す。"""
    expansion = expansion_dir or EXPANSION_DATA_DIR
    _require_pending(addon_id, "install")
    _safe_rmtree(expansion / addon_id)
    _clear_pending(addon_id)


def _run_selected_steps(plan: SetupPlan, data_dir: Path, progress: ProgressCallback) -> None:
    all_steps = plan.manifest.setup_steps
    if not plan.steps:
        progress(ProgressEvent(phase="step", message="実行する setup.steps なし、スキップ"))
        return
    progress(ProgressEvent(
        phase="step",
        message=f"setup.steps を実行 ({len(plan.steps)} 件 / 全 {len(all_steps)} 件)",
        step_total=len(plan.steps),
    ))
    _execute_steps(plan.steps, plan.addon_id, plan.addon_dir, data_dir, progress)


# ---------------------------------------------------------------------------
# 更新 (update)
# ---------------------------------------------------------------------------

def _questions_to_ask_on_update(
    old: AddonManifest, new: AddonManifest, saved: Optional[Answers]
) -> Optional[List[str]]:
    """更新で出し直す質問の id。None は全部。

    保存された答えが無ければ全部。あれば、新しく増えた質問と、選択肢が増えた
    質問だけ (前の答えが選ばれた状態で出す)。
    """
    if saved is None:
        return None
    ask = []
    for opt in new.setup_options:
        old_opt = old.setup.get_option(opt.id) if old.setup else None
        if opt.id not in saved or old_opt is None:
            ask.append(opt.id)
        elif set(opt.choice_ids()) - set(old_opt.choice_ids()):
            ask.append(opt.id)
    return ask


def prepare_update(
    addon_id: str,
    repo_url: str,
    new_commit: str,
    *,
    min_saiverse_version: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> PreparedSetup:
    """更新の一段目: ``repo_url`` から commit を取得して、新しい manifest を読む。

    取得先はフォルダの origin ではなく、カタログの ``repo_url`` (カタログが指す先を
    切り替えたとき、手で入れたアドオンの origin が別のリポジトリを指すとき)。
    working tree は元の commit のまま。
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    check_min_saiverse_version(min_saiverse_version)

    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonStateError(f"addon directory not found: {addon_dir} (install it first)")
    pending = get_pending_operation(addon_id)
    if pending is not None and pending.get("kind") == "install":
        raise AddonStateError(f"{addon_id} は導入の途中です (導入を確定するか取り消してください)")

    old_manifest = _read_manifest(addon_dir)
    progress(ProgressEvent(phase="clone", message=f"{repo_url} から {new_commit} を取得中..."))
    _git(_fetch_args(addon_dir, repo_url, new_commit), cwd=addon_dir, progress=progress, label="fetch")
    progress(ProgressEvent(phase="manifest", message="新 addon.json を検証中..."))
    new_manifest = _read_manifest_at_commit(addon_dir, new_commit)
    _check_manifest_name(new_manifest, addon_id)
    _write_pending(addon_id, "update", new_commit, repo_url)

    needs_setup = new_manifest.setup_version > old_manifest.setup_version
    if not needs_setup:
        return PreparedSetup(addon_id=addon_id, manifest=new_manifest, needs_setup=False)
    saved_raw = load_saved_answers(addon_id)
    saved = prune_answers(new_manifest, saved_raw) if saved_raw is not None else None
    return PreparedSetup(
        addon_id=addon_id,
        manifest=new_manifest,
        needs_setup=True,
        questions=describe_questions(
            new_manifest,
            selected=saved,
            only=_questions_to_ask_on_update(old_manifest, new_manifest, saved),
        ),
        steps=describe_steps(select_steps_for_os(new_manifest.setup_steps)),
    )


def plan_update_confirm(
    addon_id: str,
    answers: Optional[dict],
    expansion_dir: Optional[Path] = None,
) -> SetupPlan:
    """更新の二段目の計画 (副作用なし)。

    setup をやり直すときの答えは、保存された答え (新しい manifest に無い質問・
    選択肢は捨てる) に、今回の答えを重ね、残りを既定値で埋めたもの。
    """
    expansion = expansion_dir or EXPANSION_DATA_DIR
    pending = _require_pending(addon_id, "update")
    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonStateError(f"addon directory not found: {addon_dir}")
    commit = pending["commit"]
    old_manifest = _read_manifest(addon_dir)
    new_manifest = _read_manifest_at_commit(addon_dir, commit)
    _check_manifest_name(new_manifest, addon_id)

    saved_raw = load_saved_answers(addon_id)
    saved = prune_answers(new_manifest, saved_raw) if saved_raw is not None else None
    needs_setup = new_manifest.setup_version > old_manifest.setup_version
    if needs_setup:
        merged = dict(saved or {})
        merged.update(normalize_answers(new_manifest, answers))
        final: Optional[Answers] = fill_defaults(new_manifest, merged)
        steps = select_steps(new_manifest.setup_steps, final)
    else:
        # setup は走らない。保存された答えがあれば、無くなった質問の分だけ捨てる
        final = saved
        steps = []
    return SetupPlan(
        kind="update",
        addon_id=addon_id,
        addon_dir=addon_dir,
        manifest=new_manifest,
        steps=steps,
        answers=final,
        commit=commit,
        old_setup_version=old_manifest.setup_version,
    )


def execute_update_plan(
    plan: SetupPlan, progress_callback: Optional[ProgressCallback] = None
) -> AddonManifest:
    progress = progress_callback or _noop_progress
    addon_id = plan.addon_id
    try:
        progress(ProgressEvent(
            phase="clone", message=f"{addon_id} を commit {plan.commit} に更新中...",
        ))
        _git(["checkout", plan.commit], cwd=plan.addon_dir, progress=progress, label="checkout")
        new_manifest = _read_manifest(plan.addon_dir)
        _check_manifest_name(new_manifest, addon_id)
        data_dir = get_addon_data_dir(addon_id)
        old_v = plan.old_setup_version
        if new_manifest.setup_version > (old_v or 0):
            progress(ProgressEvent(
                phase="step",
                message=f"setup_version が {old_v} → {new_manifest.setup_version} に上がったため setup をやり直します",
            ))
            _run_selected_steps(plan, data_dir, progress)
        else:
            progress(ProgressEvent(
                phase="step",
                message=(
                    f"setup_version 変更なし ({old_v}={new_manifest.setup_version})、"
                    f"setup.steps の再実行は不要"
                ),
            ))
        if plan.answers is not None:
            save_answers(addon_id, plan.answers)
    finally:
        _clear_pending(addon_id)

    progress(ProgressEvent(
        phase="done",
        message=(
            f"{addon_id} を v{new_manifest.version} "
            f"(commit {(plan.commit or '')[:7]}) に更新完了"
        ),
    ))
    return new_manifest


def confirm_update(
    addon_id: str,
    answers: Optional[dict] = None,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> AddonManifest:
    return execute_update_plan(
        plan_update_confirm(addon_id, answers, expansion_dir), progress_callback
    )


def cancel_update(addon_id: str) -> None:
    """更新の prepare を取り消す。fetch しただけなので、記録を消すだけ。"""
    _require_pending(addon_id, "update")
    _clear_pending(addon_id)


# ---------------------------------------------------------------------------
# 導入済みアドオンの質問の出し直し (options)
# ---------------------------------------------------------------------------

def _installed_manifest(addon_id: str, expansion_dir: Optional[Path]) -> tuple[Path, AddonManifest]:
    addon_dir = (expansion_dir or EXPANSION_DATA_DIR) / addon_id
    if not addon_dir.exists():
        raise AddonStateError(f"addon directory not found: {addon_dir}")
    pending = get_pending_operation(addon_id)
    if pending is not None and pending.get("kind") == "install":
        raise AddonStateError(f"{addon_id} は導入の途中です")
    manifest = _read_manifest(addon_dir)
    _check_manifest_name(manifest, addon_id)
    return addon_dir, manifest


def _saved_or_empty(manifest: AddonManifest, addon_id: str) -> Answers:
    saved_raw = load_saved_answers(addon_id)
    return prune_answers(manifest, saved_raw) if saved_raw is not None else {}


def get_installed_options(
    addon_id: str, expansion_dir: Optional[Path] = None
) -> PreparedSetup:
    """導入済みアドオンの質問 (保存済みの答えに ``selected: true``) と、答えを足したときに
    実行の候補になる step (``when`` があり、保存済みの答えではまだ条件を満たして
    いないもの、OS で絞り込み済み) を返す。"""
    _addon_dir, manifest = _installed_manifest(addon_id, expansion_dir)
    saved = _saved_or_empty(manifest, addon_id)
    candidates = [
        s for s in select_steps_for_os(manifest.setup_steps)
        if s.when and not step_when_matches(s, saved)
    ]
    return PreparedSetup(
        addon_id=addon_id,
        manifest=manifest,
        needs_setup=False,
        questions=describe_questions(manifest, selected=saved),
        steps=describe_steps(candidates),
    )


def plan_options_apply(
    addon_id: str,
    answers: Optional[dict],
    expansion_dir: Optional[Path] = None,
) -> SetupPlan:
    """質問の出し直しの計画 (副作用なし)。

    足せるのは複数選べる質問の選択肢だけ。前に選んだ選択肢は外せず、一つだけ
    選ぶ質問の答えは (保存済みなら) 変えられない。実行するのは、答えの変化で
    新しく実行の条件を満たした step だけ (``when`` の無い step はやり直さない)。
    """
    addon_dir, manifest = _installed_manifest(addon_id, expansion_dir)
    saved = _saved_or_empty(manifest, addon_id)
    given = normalize_answers(manifest, answers)
    for qid, chosen in given.items():
        opt = manifest.setup.get_option(qid)  # normalize_answers が実在を保証
        prev = saved.get(qid, [])
        if not set(prev) <= set(chosen):
            raise AddonAnswersError(
                f"question {qid!r}: 前に選んだ選択肢 {sorted(set(prev) - set(chosen))} は外せません"
            )
        if opt is not None and not opt.multiple and prev and chosen != prev:
            raise AddonAnswersError(
                f"question {qid!r}: 一つだけ選ぶ質問の答えは導入後には変えられません"
            )
    merged = dict(saved)
    merged.update(given)
    final = fill_defaults(manifest, merged)
    satisfied = select_steps(manifest.setup_steps, final)
    new_steps = [s for s in satisfied if s.when and not step_when_matches(s, saved)]
    # 空から作られる env (新規作成・消えた/壊れた環境・Python の版違い) を使う
    # step は、前から条件を満たしていたものも走らせる。新しく条件を満たした step
    # だけだと、作り直された env に、前の step が入れていたパッケージが戻らない。
    # 判定を satisfied 全体に広げてあるのは、答えを変えずに確定し直したときも
    # 消えた環境が作り直されるようにするため — 「選択肢の確定のやり直し」が、
    # 専用環境の修復の入口を兼ねる (答えも環境も変わっていなければ従来どおり何もしない)。
    rebuilt_envs = {
        env for s in satisfied
        if (env := _step_env(s)) is not None and _env_needs_full_run(addon_id, env)
    }
    steps = [
        s for s in satisfied
        if s in new_steps or (_step_env(s) is not None and _step_env(s) in rebuilt_envs)
    ]
    return SetupPlan(
        kind="options",
        addon_id=addon_id,
        addon_dir=addon_dir,
        manifest=manifest,
        steps=steps,
        answers=final,
    )


def execute_options_plan(
    plan: SetupPlan, progress_callback: Optional[ProgressCallback] = None
) -> AddonManifest:
    progress = progress_callback or _noop_progress
    data_dir = get_addon_data_dir(plan.addon_id)
    _run_selected_steps(plan, data_dir, progress)
    if plan.answers is not None:
        save_answers(plan.addon_id, plan.answers)
    progress(ProgressEvent(
        phase="done", message=f"{plan.addon_id} の選択を反映しました",
    ))
    return plan.manifest


def apply_installed_options(
    addon_id: str,
    answers: Optional[dict],
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> AddonManifest:
    return execute_options_plan(
        plan_options_apply(addon_id, answers, expansion_dir), progress_callback
    )


# ---------------------------------------------------------------------------
# 本体の Python の系列が変わったあとの、専用の環境の作り直し (setup から呼ぶ)
# ---------------------------------------------------------------------------

# 作り直しに失敗したアドオンの記録。setup が SAIVerse のフォルダに書く
# (``scripts/rebuild_addon_envs.py`` → ``record_env_rebuild_failures``)。
# 最後の作り直しで失敗が無ければ消す。形は ``record_env_rebuild_failures`` を参照。
ENV_REBUILD_FAILURES_PATH = PROJECT_ROOT / ".addon_env_rebuild_failures.json"


@dataclass
class EnvRebuildResult:
    """一つのアドオンの、setup でのパッケージの入れ直しの結果。``error`` が None なら成功。

    ``core_venv`` が False なら専用の環境の作り直し (``rebuild_unusable_addon_envs``、
    ``envs`` は作り直した環境の名前)。True なら本体の ``.venv`` に入れる step の
    入れ直し (``reinstall_core_venv_steps``、``envs`` は空)。
    """
    addon_id: str
    envs: List[str]
    steps: List[str] = field(default_factory=list)
    error: Optional[str] = None
    core_venv: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def env_failures_in_record(path: Optional[Path] = None) -> Dict[str, set[str]]:
    """前の setup の記録で、専用の環境の作り直しが済んでいない ``{addon_id: {環境の名前}}``。

    作り直しは古い環境を消してから作るので、作る前に失敗するとフォルダごと無くなり、
    フォルダを走査するだけでは次の setup がその環境を見つけられない。記録が無い・
    読めないときは空 (読めない記録の扱いは ``core_venv_failures_in_record`` が持つ)。
    """
    target = path or ENV_REBUILD_FAILURES_PATH
    if not target.is_file():
        return {}
    try:
        failures = json.loads(target.read_text(encoding="utf-8"))["failures"]
        if not isinstance(failures, list):
            return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    found: Dict[str, set[str]] = {}
    for item in failures:
        if not isinstance(item, dict) or item.get("core_venv") is True or not item.get("addon_id"):
            continue
        envs = item.get("envs")
        if isinstance(envs, list):
            names = {str(name) for name in envs if isinstance(name, str) and name}
            if names:
                found.setdefault(str(item["addon_id"]), set()).update(names)
    return found


def _unusable_env_dirs(addon_id: str, *, include_recorded: bool = True) -> List[str]:
    """作り直しが要る専用の環境の名前。

    ``addon_install/<id>/envs/`` にあって、記録が無い・読めない・記録の Python の
    major.minor が本体と違う・python の実行ファイルが無いもの (``_env_needs_full_run``
    と同じ判定) と、前の setup で作り直しに失敗したと記録されているもの
    (``env_failures_in_record`` — フォルダが消えていても含める)。``include_recorded``
    False は記録を見ない — 作り直しを走らせた直後の「まだ使えないか」の確かめは、
    いまの環境だけで判定する (記録はこのあと書き直される)。
    """
    envs_dir = get_addon_install_dir(addon_id) / "envs"
    names = []
    recorded = env_failures_in_record().get(addon_id, set()) if include_recorded else set()
    for name in sorted(recorded):
        try:
            get_addon_env_dir(addon_id, name)  # 不正な名前は捨てる
        except ValueError:
            continue
        names.append(name)
    if not envs_dir.is_dir():
        return names
    for path in sorted(envs_dir.iterdir()):
        if not path.is_dir():
            continue
        try:
            needs = _env_needs_full_run(addon_id, path.name)
        except ValueError:  # 専用の環境の名前として不正 (manifest が作れない名前)
            continue
        if needs and path.name not in names:
            names.append(path.name)
    return names


def find_unusable_addon_envs(expansion_dir: Optional[Path] = None) -> Dict[str, List[str]]:
    """``{addon_id: [使えない専用の環境の名前, ...]}`` (副作用なし、manifest は読まない)。

    対象はアドオンのフォルダ (``expansion_data/<id>/``) があるものだけ。使えない環境が
    無いアドオンは含めない。
    """
    expansion = expansion_dir or EXPANSION_DATA_DIR
    found: Dict[str, List[str]] = {}
    if not expansion.is_dir():
        return found
    for addon_dir in sorted(p for p in expansion.iterdir() if p.is_dir()):
        try:
            envs = _unusable_env_dirs(addon_dir.name)
        except ValueError:  # アドオンの id として不正なフォルダ名
            continue
        if envs:
            found[addon_dir.name] = envs
    return found


def plan_env_rebuild(addon_id: str, expansion_dir: Optional[Path] = None) -> SetupPlan:
    """保存された答えのまま、使えない専用の環境を作り直す計画 (副作用なし)。

    走らせるのは、保存された答え (無い質問は既定値) と OS で選ばれる step のうち、
    フォルダが既にあって、いまの Python では使えない専用の環境 (``_unusable_env_dirs``)
    を使うもの全部。環境は空の venv から作り直されるので、新しく条件を満たした
    step だけでなく、その環境を使う step を全部走らせる (``plan_options_apply`` と同じ
    理由)。フォルダの無い環境 (一度も作られていない) は作らない。答えは保存しない
    (``answers=None``) — 既定値で埋めた答えを書くと、保存された答えが変わるため。
    """
    addon_dir, manifest = _installed_manifest(addon_id, expansion_dir)
    answers = fill_defaults(manifest, _saved_or_empty(manifest, addon_id))
    satisfied = select_steps(manifest.setup_steps, answers)
    unusable = set(_unusable_env_dirs(addon_id))
    steps = [s for s in satisfied if _step_env(s) in unusable]
    return SetupPlan(
        kind="env_rebuild",
        addon_id=addon_id,
        addon_dir=addon_dir,
        manifest=manifest,
        steps=steps,
        answers=None,
    )


def _forget_env_records(addon_id: str, env_names: Iterable[str]) -> None:
    """作り直しに失敗した環境の記録 (``saiverse_env.json``) を消す。

    ``_ensure_addon_env`` は venv を作った直後に記録を書くので、その後の step が
    失敗すると「いまの Python で作った」記録の付いた、パッケージの欠けた環境が残る。
    記録を消しておけば、``get_addon_env_python`` は「setup をやり直して」と断り、
    次の setup とアドオン管理の選択肢の確定のやり直しが、その環境を作り直す。
    """
    for name in env_names:
        record = get_addon_env_dir(addon_id, name) / ADDON_ENV_RECORD_NAME
        try:
            record.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("installer: could not remove env record %s", record, exc_info=True)


def rebuild_unusable_addon_envs(
    expansion_dir: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[EnvRebuildResult]:
    """導入済みのアドオンの専用の環境のうち、いまの Python で使えないものを作り直す。

    setup が新しい ``.venv`` を作ったあと (本体の Python の系列が変わったあと) に、
    ``.venv`` の Python で呼ぶ (``scripts/rebuild_addon_envs.py``)。アドオンごとに
    ``plan_env_rebuild`` の step を走らせる。アドオンのバージョン (checkout)、
    保存された答え、アドオンのデータ (``addon_data/<id>/``) は変えない。

    一つのアドオンの失敗では止めずに次のアドオンへ進み、失敗は結果に入れる。失敗した
    アドオンの、作り直しの対象だった環境の記録は消す (``_forget_env_records``)。
    使えない環境が無いアドオンは manifest も読まず、結果に入れない。

    同じアドオンへの同時の操作を防ぐ API の鍵 (``api/routes/addon_catalog.py`` の
    ``_get_lock``) は SAIVerse のプロセスの中だけの ``threading.Lock`` で、別の
    プロセスからは取れない。呼び出し側 (setup) は、SAIVerse が動いていないことを
    確かめてから呼ぶ。
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    results: List[EnvRebuildResult] = []
    for addon_id, unusable in find_unusable_addon_envs(expansion).items():
        try:
            plan = plan_env_rebuild(addon_id, expansion)
        except AddonInstallError as exc:
            results.append(EnvRebuildResult(addon_id=addon_id, envs=unusable, error=str(exc)))
            continue
        envs = sorted({env for s in plan.steps if (env := _step_env(s)) is not None})
        if not plan.steps:
            # 使えない環境はあるが、いまの答えと OS ではどの step も使わない (答えの
            # 選択肢が manifest から消えたなど)。アドオンはその環境を使わない。
            LOGGER.info(
                "installer: %s has unusable envs %s that no selected step uses; leaving them",
                addon_id, unusable,
            )
            continue
        progress(ProgressEvent(
            phase="step",
            message=(
                f"{addon_id}: 専用の Python 環境 {', '.join(envs)} を、いまの Python "
                f"({current_python_version()}) で作り直します"
            ),
        ))
        step_names = [s.name for s in plan.steps]
        try:
            _run_selected_steps(plan, get_addon_data_dir(addon_id), progress)
            still = sorted(set(envs) & set(_unusable_env_dirs(addon_id, include_recorded=False)))
            if still:
                # step が skip_if_exists で飛ばされた、などで環境が作られなかった
                raise AddonInstallError(
                    f"the Python environment {', '.join(still)} was not rebuilt "
                    "(its setup steps were skipped)"
                )
        except Exception as exc:  # noqa: BLE001 - 一つのアドオンの失敗で他を止めない
            LOGGER.warning("installer: rebuilding envs of %s failed", addon_id, exc_info=True)
            _forget_env_records(addon_id, envs)
            results.append(EnvRebuildResult(
                addon_id=addon_id, envs=envs, steps=step_names, error=str(exc),
            ))
            continue
        results.append(EnvRebuildResult(addon_id=addon_id, envs=envs, steps=step_names))
    return results


def record_env_rebuild_failures(
    results: Iterable[EnvRebuildResult], path: Optional[Path] = None
) -> Optional[Path]:
    """失敗した入れ直しをファイルに書く。失敗が無ければファイルを消して None を返す。

    ``results`` には、本体の ``.venv`` に入れる step の入れ直しと専用の環境の作り直しの
    両方の結果を一度に渡す (片方だけで呼ぶと、もう片方の失敗の記録を消す)。

    形 (UTF-8 の JSON)::

        {
          "created_at": "2026-10-23T12:34:56+00:00",
          "python_version": "3.13.17",
          "failures": [
            {"addon_id": "saiverse-voice-tts", "envs": ["gpt_sovits"],
             "steps": ["gpt-sovits-packages"], "error": "...", "core_venv": false},
            {"addon_id": "saiverse-voice-tts", "envs": [],
             "steps": ["Voice TTS のパッケージ"], "error": "...", "core_venv": true}
          ]
        }

    ``core_venv`` が true の項目は、本体の ``.venv`` への入れ直しの失敗。次の setup は、
    ``.venv`` を作り直さない回でも、その入れ直しをやり直す
    (``core_venv_failures_in_record``)。利用者がすること (setup をもう一度・アドオン管理で
    そのアドオンを入れ直す) は、読む側が書く。
    """
    target = path or ENV_REBUILD_FAILURES_PATH
    failures = [r for r in results if not r.ok]
    if not failures:
        target.unlink(missing_ok=True)
        return None
    _write_json_atomic(target, {
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "python_version": current_python_version(),
        "failures": [
            {
                "addon_id": r.addon_id,
                "envs": r.envs,
                "steps": r.steps,
                "error": r.error,
                "core_venv": r.core_venv,
            }
            for r in failures
        ],
    })
    return target


# ``scripts/rebuild_addon_envs.py`` 自体が想定外の例外で止まったときの記録の ``addon_id``。
SETUP_CRASH_ADDON_ID = "(setup)"


def core_venv_failures_in_record(path: Optional[Path] = None) -> set[str]:
    """前の setup の記録で、本体の ``.venv`` への入れ直しが済んでいないアドオンの id。

    記録が無ければ空。記録はあるが読めないときは、どのアドオンが済んでいないか
    分からないので ``{SETUP_CRASH_ADDON_ID}`` を返す (呼び出し側は全部をやり直す)。
    """
    target = path or ENV_REBUILD_FAILURES_PATH
    if not target.is_file():
        return set()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        failures = document["failures"]
        if not isinstance(failures, list):
            raise TypeError("failures is not a list")
    except (OSError, ValueError, KeyError, TypeError):
        LOGGER.warning("installer: unreadable env rebuild record %s", target, exc_info=True)
        return {SETUP_CRASH_ADDON_ID}
    return {
        str(item["addon_id"])
        for item in failures
        if isinstance(item, dict) and item.get("core_venv") is True and item.get("addon_id")
    }


# ---------------------------------------------------------------------------
# 本体の .venv を作り直したあとの、本体の .venv に入れる step の入れ直し (setup から呼ぶ)
# ---------------------------------------------------------------------------

def _installs_into_core_venv(step: SetupStep) -> bool:
    """本体の ``.venv`` にパッケージを入れる step か: ``env`` の無い ``pip_install`` だけ。

    ``_exec_pip_install`` は ``env`` が無ければ本体の Python (``sys.executable``) の pip に
    requirements.lock を constraints として渡して入れる。``env`` の無い ``python_script``
    (本体の Python で起動) と ``platform_script`` (本体の環境変数のまま起動) も、中で pip を
    呼べば本体の ``.venv`` に入れられるが、対象にしない。そういうアドオンはカタログに
    載せない (addon_catalog_management.md の不変条件 7)。スクリプトの中の pip には
    constraints が渡らない (issues/addon_setup_scripts_bypass_lock_constraints.md) ので、
    setup から走らせ直すと本体のパッケージを動かしうる。いまある ``env`` の無い
    スクリプト (voice-tts の設定ファイルのひな形の作成、Irodori-TTS の依存の手直し) が
    残すものは ``.venv`` の外にあり、``.venv`` を作り直しても消えない。
    """
    return isinstance(step, PipInstallStep) and step.env is None


def plan_core_venv_reinstall(addon_id: str, expansion_dir: Optional[Path] = None) -> SetupPlan:
    """保存された答えのまま、本体の ``.venv`` に入れる step を走らせ直す計画 (副作用なし)。

    走らせるのは、保存された答え (無い質問は既定値) と OS で選ばれる step のうち、
    ``_installs_into_core_venv`` のもの全部 (``plan_env_rebuild`` と同じ選び方)。
    答えは保存しない (``answers=None``)。
    """
    addon_dir, manifest = _installed_manifest(addon_id, expansion_dir)
    answers = fill_defaults(manifest, _saved_or_empty(manifest, addon_id))
    steps = [s for s in select_steps(manifest.setup_steps, answers) if _installs_into_core_venv(s)]
    return SetupPlan(
        kind="core_venv_reinstall",
        addon_id=addon_id,
        addon_dir=addon_dir,
        manifest=manifest,
        steps=steps,
        answers=None,
    )


def plan_core_venv_reinstalls(
    expansion_dir: Optional[Path] = None, addon_ids: Optional[Iterable[str]] = None,
) -> tuple[List[SetupPlan], List[EnvRebuildResult]]:
    """導入済みのアドオンごとの ``plan_core_venv_reinstall`` (副作用なし)。

    返値は (走らせる step があるアドオンの計画, 計画を作れなかったアドオンの失敗)。
    ``addon_ids`` が None なら導入済みの全部、与えられればその中だけ。対象は
    ``expansion_data/<id>/addon.json`` があるアドオンで、導入の途中 (prepare だけで
    confirm されていない) のものは入れない。``addon.json`` が読めないアドオンは、
    本体の ``.venv`` に入れる step があるか分からないので失敗に入れる。
    """
    expansion = expansion_dir or EXPANSION_DATA_DIR
    wanted = set(addon_ids) if addon_ids is not None else None
    plans: List[SetupPlan] = []
    failures: List[EnvRebuildResult] = []
    if not expansion.is_dir():
        return plans, failures
    for addon_dir in sorted(p for p in expansion.iterdir() if p.is_dir()):
        addon_id = addon_dir.name
        if wanted is not None and addon_id not in wanted:
            continue
        if not (addon_dir / "addon.json").is_file():
            continue  # 導入の仕組みが入れたアドオンではない (setup の step を持たない)
        try:
            pending = get_pending_operation(addon_id)
        except ValueError:  # アドオンの id として不正なフォルダ名
            continue
        if pending is not None and pending.get("kind") == "install":
            continue
        try:
            plan = plan_core_venv_reinstall(addon_id, expansion)
        except (AddonInstallError, OSError, ValueError) as exc:  # 読めない・文字コードが違う addon.json も
            failures.append(EnvRebuildResult(
                addon_id=addon_id, envs=[], core_venv=True,
                error=f"could not read the setup steps that install into the SAIVerse .venv: {exc}",
            ))
            continue
        if plan.steps:
            plans.append(plan)
    return plans, failures


def reinstall_core_venv_steps(
    expansion_dir: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
    addon_ids: Optional[Iterable[str]] = None,
) -> List[EnvRebuildResult]:
    """導入済みのアドオンの、本体の ``.venv`` に入れる step を走らせ直す。

    setup が新しい ``.venv`` を作ったあとに、``.venv`` の Python で呼ぶ
    (``scripts/rebuild_addon_envs.py --core-venv-recreated``)。新しい ``.venv`` には
    requirements.lock の部品しか入っていないので、``env`` の無い ``pip_install`` の
    step が入れていたパッケージを、保存された答えのまま入れ直す
    (``plan_core_venv_reinstalls``)。pip には既存の導入と同じく requirements.lock を
    constraints として渡す (``_exec_pip_install``)。アドオンのバージョン (checkout)・
    保存された答え・アドオンのデータは変えない。

    一つのアドオンの失敗では止めずに次のアドオンへ進み、失敗は結果に入れる
    (``core_venv=True``)。呼び出し側 (setup) は、SAIVerse が動いていないことを
    確かめてから呼ぶ (``rebuild_unusable_addon_envs`` と同じ理由)。
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    plans, results = plan_core_venv_reinstalls(expansion, addon_ids)
    for plan in plans:
        step_names = [s.name for s in plan.steps]
        progress(ProgressEvent(
            phase="step",
            message=(
                f"{plan.addon_id}: SAIVerse 本体の .venv に入れるパッケージを入れ直します "
                f"({', '.join(step_names)})"
            ),
        ))
        try:
            _run_selected_steps(plan, get_addon_data_dir(plan.addon_id), progress)
        except Exception as exc:  # noqa: BLE001 - 一つのアドオンの失敗で他を止めない
            LOGGER.warning(
                "installer: reinstalling core venv packages of %s failed", plan.addon_id,
                exc_info=True,
            )
            results.append(EnvRebuildResult(
                addon_id=plan.addon_id, envs=[], steps=step_names, core_venv=True,
                error=f"installing the packages into the SAIVerse .venv failed: {exc}",
            ))
            continue
        results.append(EnvRebuildResult(
            addon_id=plan.addon_id, envs=[], steps=step_names, core_venv=True,
        ))
    return results


# ---------------------------------------------------------------------------
# アンインストール
# ---------------------------------------------------------------------------

def uninstall_addon(
    addon_id: str,
    delete_data: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> None:
    """アドオンをアンインストール。

    手順:
        1. manifest の uninstall.steps のうち、OS と (保存された答え、無ければ
           既定値での) when に当てはまるものを実行
        2. expansion_data/<addon_id>/ を削除
        3. ~/.saiverse/addon_install/<addon_id>/ (専用の環境・答え) を必ず削除
           (作り直せる導入物なので、残すかどうかを選ばせない)
        4. delete_data=True なら ~/.saiverse/user_data/addon_data/<addon_id>/ も削除
           (デフォルト False = ユーザーデータは保護)
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonStateError(f"addon directory not found: {addon_dir}")

    try:
        manifest = _read_manifest(addon_dir)
    except AddonManifestError:
        manifest = None
        progress(ProgressEvent(
            phase="manifest",
            message="addon.json 読込に失敗、uninstall.steps はスキップしてディレクトリ削除のみ実施",
        ))

    data_dir = get_addon_data_dir(addon_id)

    if manifest and manifest.uninstall and manifest.uninstall.steps:
        saved_raw = load_saved_answers(addon_id)
        answers = fill_defaults(
            manifest,
            prune_answers(manifest, saved_raw) if saved_raw is not None else {},
        )
        steps = select_steps(manifest.uninstall.steps, answers)
        progress(ProgressEvent(
            phase="step",
            message=f"uninstall.steps を実行 ({len(steps)} 件)",
        ))
        _execute_steps(steps, addon_id, addon_dir, data_dir, progress)

    progress(ProgressEvent(phase="step", message=f"{addon_dir} を削除"))
    _safe_rmtree(addon_dir)

    install_dir = get_addon_install_dir(addon_id)
    if install_dir.exists():
        progress(ProgressEvent(
            phase="step", message=f"導入物 (専用の環境・答え) {install_dir} を削除",
        ))
        _safe_rmtree(install_dir)

    if delete_data and data_dir.exists():
        progress(ProgressEvent(
            phase="step",
            message=f"永続データ {data_dir} を削除 (delete_data=True)",
        ))
        _safe_rmtree(data_dir)

    progress(ProgressEvent(phase="done", message=f"{addon_id} をアンインストール完了"))


__all__ = [
    "Answers",
    "PreparedSetup",
    "SetupPlan",
    "check_min_saiverse_version",
    "prepare_install",
    "plan_install_confirm",
    "execute_install_plan",
    "confirm_install",
    "cancel_install",
    "prepare_update",
    "plan_update_confirm",
    "execute_update_plan",
    "confirm_update",
    "cancel_update",
    "get_installed_options",
    "plan_options_apply",
    "execute_options_plan",
    "apply_installed_options",
    "ENV_REBUILD_FAILURES_PATH",
    "EnvRebuildResult",
    "find_unusable_addon_envs",
    "plan_env_rebuild",
    "rebuild_unusable_addon_envs",
    "record_env_rebuild_failures",
    "uninstall_addon",
    "get_pending_operation",
    "load_saved_answers",
    "save_answers",
    "default_answers",
    "prune_answers",
    "normalize_answers",
    "fill_defaults",
    "select_steps",
    "select_steps_for_os",
    "describe_questions",
    "describe_steps",
    "current_os",
    "ProgressEvent",
    "ProgressCallback",
    "AddonInstallError",
    "AddonManifestError",
    "AddonAnswersError",
    "AddonStateError",
    "AddonVersionError",
]
