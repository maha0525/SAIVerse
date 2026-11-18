"""Next.js must not interrupt user startup with its coding-agent upgrade menu.

Next.js 16.4 enables ``experimental.agentUpgrade = 'security'`` by default, which
displays an interactive ``Upgrade now / Skip`` menu during ``next build`` and
``next dev`` whenever an advisory is published after release. Choosing ``Upgrade
now`` invokes ``next upgrade --agent=security`` (intended for coding agents) and,
if applied in place, modifies ``frontend/package.json`` / ``package-lock.json``
so SAIVerse's updater refuses to run on a dirty working tree.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_next_config_disables_agent_upgrade_prompt() -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed; skipping agentUpgrade config check")

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "frontend" / "scripts" / "test-next-agent-upgrade-disabled.cjs"

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
        f"test-next-agent-upgrade-disabled.cjs failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
