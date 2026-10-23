import subprocess
import sys

import pytest


def test_guard_refuses_pip_install_with_the_test_interpreter():
    with pytest.raises(AssertionError, match="changes the real .venv"):
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])


def test_guard_allows_pip_check():
    assert subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0


def test_guard_also_refuses_a_popen_of_pip_install():
    """The update engine's _run starts its commands with Popen, not run."""
    with pytest.raises(AssertionError, match="changes the real .venv"):
        subprocess.Popen([sys.executable, "-m", "pip", "uninstall", "-y", "pip"])
