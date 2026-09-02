"""アドオンの pip install は本体の requirements.lock を constraints として渡す。

アドオンは本体と同じ venv に入るので、constraints が無いとアドオンの
requirements が本体の固定した部品を別の版へ動かせてしまう (逆向きの事故が
2026-09-02 の voice-tts 無音の原因)。docs/intent/dependency_management.md §2-4
の「アドオンは本体の部品を動かせない」を機械で固定する。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from saiverse import addon_installer
from saiverse.addon_manifest import PipInstallStep
from saiverse.data_paths import PROJECT_ROOT


def _pip_step() -> PipInstallStep:
    return PipInstallStep(type="pip_install", name="deps", requirements="requirements.txt")


def _addon_dir(tmp_path: Path) -> Path:
    addon_dir = tmp_path / "some-addon"
    addon_dir.mkdir()
    (addon_dir / "requirements.txt").write_text("numpy>=1.24\n", encoding="utf-8")
    return addon_dir


def test_pip_install_passes_the_core_lock_as_constraints(tmp_path: Path) -> None:
    addon_dir = _addon_dir(tmp_path)
    recorded: list[list[str]] = []

    def fake_run(args, cwd, progress, label, env=None):  # type: ignore[no-untyped-def]
        recorded.append(list(args))

    with patch.object(addon_installer, "_run_subprocess", side_effect=fake_run):
        addon_installer._exec_pip_install(_pip_step(), addon_dir, lambda _e: None)

    assert len(recorded) == 1
    args = recorded[0]
    assert args[1:4] == ["-m", "pip", "install"]
    assert args[args.index("-r") + 1] == str((addon_dir / "requirements.txt").resolve())
    assert args[args.index("-c") + 1] == str(PROJECT_ROOT / "requirements.lock")


def test_the_constraints_file_is_the_repo_lock_and_it_exists() -> None:
    """The constraint must point at the lock that ships with this checkout."""
    assert addon_installer.CORE_REQUIREMENTS_LOCK == PROJECT_ROOT / "requirements.lock"
    assert addon_installer.CORE_REQUIREMENTS_LOCK.is_file()


def test_pip_install_refuses_to_run_without_the_core_lock(tmp_path: Path) -> None:
    """No lock means no constraints, which would let the addon move core pins."""
    addon_dir = _addon_dir(tmp_path)
    with patch.object(
        addon_installer, "CORE_REQUIREMENTS_LOCK", tmp_path / "absent.lock"
    ), patch.object(addon_installer, "_run_subprocess") as run:
        with pytest.raises(addon_installer.AddonInstallError, match="core lock file not found"):
            addon_installer._exec_pip_install(_pip_step(), addon_dir, lambda _e: None)
    run.assert_not_called()
