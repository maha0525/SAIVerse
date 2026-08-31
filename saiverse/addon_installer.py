"""アドオン installer: git clone + manifest 検証 + setup steps 実行。

設計は ``docs/intent/addon_catalog_management.md`` 参照。

提供する主要関数:
    - ``install_addon(repo_url, commit, ...)``: 新規インストール
    - ``update_addon(addon_id, new_commit, ...)``: 既存アドオンを別 commit に更新
    - ``uninstall_addon(addon_id, ...)``: アンインストール

すべて ``progress_callback`` を受け取り、ステップ毎に状態を流せる形にしてある
(Phase 3 で UI に SSE / WS で流す経路に繋ぐ前提)。

Phase 1 ではこのモジュールを ``scripts/addon_install.py`` 経由で CLI から
動作確認する。UI からの呼び出し経路は Phase 2-3 で追加。
"""
from __future__ import annotations

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
from saiverse.addon_paths import get_addon_data_dir
from saiverse.data_paths import EXPANSION_DATA_DIR

LOGGER = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


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


def _exec_pip_install(step: PipInstallStep, addon_dir: Path, progress: ProgressCallback) -> None:
    req_path = _check_addon_dir_path(addon_dir, step.requirements, "requirements")
    if not req_path.exists():
        raise AddonInstallError(
            f"pip_install: requirements file not found: {req_path}"
        )
    _run_subprocess(
        [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
        cwd=addon_dir,
        progress=progress,
        label=step.name,
    )


def _exec_platform_script(
    step: PlatformScriptStep, addon_dir: Path, progress: ProgressCallback
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
    _run_subprocess(args, cwd=addon_dir, progress=progress, label=step.name)


def _exec_python_script(
    step: PythonScriptStep, addon_dir: Path, progress: ProgressCallback
) -> None:
    script_abs = _check_addon_dir_path(addon_dir, step.script, "python_script")
    if not script_abs.exists():
        raise AddonInstallError(f"python_script: script not found: {script_abs}")
    _run_subprocess(
        [sys.executable, str(script_abs), *step.args],
        cwd=addon_dir,
        progress=progress,
        label=step.name,
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
        # skip_if_exists を使うべきケースだが、念のため再 clone は避ける
        progress(ProgressEvent(
            phase="step",
            message=f"{step.name}: {dest_abs} は既存のため再 clone をスキップ"
        ))
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
    addon_dir: Path,
    data_dir: Path,
    progress: ProgressCallback,
) -> None:
    if _step_should_skip(step, addon_dir, progress):
        return
    if isinstance(step, PipInstallStep):
        _exec_pip_install(step, addon_dir, progress)
    elif isinstance(step, PlatformScriptStep):
        _exec_platform_script(step, addon_dir, progress)
    elif isinstance(step, PythonScriptStep):
        _exec_python_script(step, addon_dir, progress)
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
        _execute_step(step, addon_dir, data_dir, progress)


# ---------------------------------------------------------------------------
# Manifest IO
# ---------------------------------------------------------------------------

def _read_manifest(addon_dir: Path) -> AddonManifest:
    manifest_path = addon_dir / "addon.json"
    if not manifest_path.exists():
        raise AddonManifestError(f"addon.json not found in {addon_dir}")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise AddonManifestError(f"addon.json is not valid JSON: {e}") from e
    try:
        return load_manifest(data)
    except ValidationError as e:
        raise AddonManifestError(f"addon.json validation failed:\n{e}") from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_addon(
    repo_url: str,
    commit: str,
    addon_id: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> AddonManifest:
    """新規アドオンをインストール。

    手順:
        1. expansion_data/<addon_id>/ に shallow clone
        2. commit SHA に checkout
        3. addon.json を validate
        4. setup.steps を順次実行
        5. 完了後、manifest を返す

    addon_id を省略すると repo_url のリポジトリ名から推定する
    (例: ``.../saiverse-voice-tts.git`` → ``saiverse-voice-tts``)。
    manifest の name と addon_id が不一致の場合は AddonInstallError。

    エラー時は clone した addon ディレクトリを削除 (rollback)。
    永続データディレクトリは触らない (= ユーザーデータは保護)。
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR

    if addon_id is None:
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        addon_id = repo_name

    addon_dir = expansion / addon_id
    if addon_dir.exists():
        raise AddonInstallError(
            f"addon directory already exists: {addon_dir} "
            f"(use update_addon to update, or uninstall_addon first)"
        )
    expansion.mkdir(parents=True, exist_ok=True)

    progress(ProgressEvent(
        phase="clone",
        message=f"{repo_url} を {addon_dir} に clone 中...",
    ))

    cloned = False
    try:
        _git(
            ["clone", "--no-checkout", repo_url, str(addon_dir)],
            cwd=expansion,
            progress=progress,
            label="clone",
        )
        cloned = True
        _git(
            ["fetch", "--depth", "1", "origin", commit],
            cwd=addon_dir,
            progress=progress,
            label="fetch",
        )
        _git(
            ["checkout", commit],
            cwd=addon_dir,
            progress=progress,
            label="checkout",
        )

        progress(ProgressEvent(phase="manifest", message="addon.json を検証中..."))
        manifest = _read_manifest(addon_dir)
        if manifest.name != addon_id:
            raise AddonInstallError(
                f"manifest name mismatch: addon.json name={manifest.name!r} "
                f"but addon_id={addon_id!r}"
            )

        data_dir = get_addon_data_dir(addon_id)

        if manifest.setup and manifest.setup.steps:
            progress(ProgressEvent(
                phase="step",
                message=f"setup.steps を実行 ({len(manifest.setup.steps)} 件)",
                step_total=len(manifest.setup.steps),
            ))
            _execute_steps(manifest.setup.steps, addon_dir, data_dir, progress)
        else:
            progress(ProgressEvent(phase="step", message="setup.steps なし、スキップ"))

        progress(ProgressEvent(
            phase="done",
            message=f"{addon_id} v{manifest.version} のインストール完了",
        ))
        return manifest
    except BaseException as e:
        if cloned and addon_dir.exists():
            progress(ProgressEvent(
                phase="rollback",
                message=f"エラー発生、clone した {addon_dir} を削除します: {e}",
            ))
            try:
                _safe_rmtree(addon_dir)
            except OSError as cleanup_err:
                LOGGER.warning(
                    "installer: rollback cleanup failed for %s: %s",
                    addon_dir, cleanup_err,
                )
        raise


def update_addon(
    addon_id: str,
    new_commit: str,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> AddonManifest:
    """既存アドオンを指定 commit に更新。

    手順:
        1. expansion_data/<addon_id>/ で fetch + checkout
        2. 旧 manifest の setup_version を控えておく
        3. 新 manifest を validate
        4. 新 setup_version > 旧 setup_version なら setup.steps を再実行
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonInstallError(
            f"addon directory not found: {addon_dir} (install_addon first)"
        )

    old_manifest = _read_manifest(addon_dir)
    old_setup_version = old_manifest.setup_version

    progress(ProgressEvent(
        phase="clone",
        message=f"{addon_id} を commit {new_commit} に更新中...",
    ))
    _git(
        ["fetch", "--depth", "1", "origin", new_commit],
        cwd=addon_dir,
        progress=progress,
        label="fetch",
    )
    _git(
        ["checkout", new_commit],
        cwd=addon_dir,
        progress=progress,
        label="checkout",
    )

    progress(ProgressEvent(phase="manifest", message="新 addon.json を検証中..."))
    new_manifest = _read_manifest(addon_dir)
    if new_manifest.name != addon_id:
        raise AddonInstallError(
            f"manifest name mismatch after update: {new_manifest.name!r} vs {addon_id!r}"
        )

    data_dir = get_addon_data_dir(addon_id)

    if (
        new_manifest.setup
        and new_manifest.setup.steps
        and new_manifest.setup_version > old_setup_version
    ):
        progress(ProgressEvent(
            phase="step",
            message=(
                f"setup_version が {old_setup_version} → {new_manifest.setup_version} に "
                f"上がったため setup.steps を再実行 ({len(new_manifest.setup.steps)} 件)"
            ),
        ))
        _execute_steps(new_manifest.setup.steps, addon_dir, data_dir, progress)
    else:
        progress(ProgressEvent(
            phase="step",
            message=(
                f"setup_version 変更なし ({old_setup_version}={new_manifest.setup_version})、"
                f"setup.steps の再実行は不要"
            ),
        ))

    progress(ProgressEvent(
        phase="done",
        message=f"{addon_id} を v{new_manifest.version} (commit {new_commit[:7]}) に更新完了",
    ))
    return new_manifest


def uninstall_addon(
    addon_id: str,
    delete_data: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    expansion_dir: Optional[Path] = None,
) -> None:
    """アドオンをアンインストール。

    手順:
        1. manifest の uninstall.steps を実行 (主に remove_dir)
        2. expansion_data/<addon_id>/ を削除
        3. delete_data=True なら ~/.saiverse/user_data/addon_data/<addon_id>/ も削除
           (デフォルト False = ユーザーデータは保護)
    """
    progress = progress_callback or _noop_progress
    expansion = expansion_dir or EXPANSION_DATA_DIR
    addon_dir = expansion / addon_id
    if not addon_dir.exists():
        raise AddonInstallError(f"addon directory not found: {addon_dir}")

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
        progress(ProgressEvent(
            phase="step",
            message=f"uninstall.steps を実行 ({len(manifest.uninstall.steps)} 件)",
        ))
        _execute_steps(manifest.uninstall.steps, addon_dir, data_dir, progress)

    progress(ProgressEvent(phase="step", message=f"{addon_dir} を削除"))
    _safe_rmtree(addon_dir)

    if delete_data and data_dir.exists():
        progress(ProgressEvent(
            phase="step",
            message=f"永続データ {data_dir} を削除 (delete_data=True)",
        ))
        _safe_rmtree(data_dir)

    progress(ProgressEvent(phase="done", message=f"{addon_id} をアンインストール完了"))


__all__ = [
    "install_addon",
    "update_addon",
    "uninstall_addon",
    "ProgressEvent",
    "ProgressCallback",
    "AddonInstallError",
    "AddonManifestError",
]
