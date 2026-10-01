"""The Next.js dev server must accept the hostnames users reach it by.

A hostname missing from ``allowedDevOrigins`` (frontend/next.config.ts) is not
a visible error: the HMR websocket is refused, the dev client retries for about
40 seconds and then reloads the page, forever. The check itself lives in
frontend/scripts/test-dev-origins.cjs so that it can use Next.js's own matcher.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_dev_server_allows_tailscale_hostnames():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed; skipping dev-origin check")

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "frontend" / "scripts" / "test-dev-origins.cjs"

    result = subprocess.run(
        [node_bin, str(script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0 and result.stdout.startswith("SKIP:"):
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, (
        f"test-dev-origins.cjs failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
