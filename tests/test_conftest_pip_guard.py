import subprocess
import sys

import pytest


def test_guard_refuses_pip_install_with_the_test_interpreter():
    with pytest.raises(AssertionError, match="changes the real .venv"):
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])


def test_guard_allows_pip_check():
    assert subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0
