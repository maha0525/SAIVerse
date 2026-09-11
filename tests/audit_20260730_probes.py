"""Read-only audit probes with synthetic data; no persona or LLM execution.

Run from the repository root with .venv/Scripts/python.exe -X utf8
tests/audit_20260730_probes.py. Assertions describe observed defects, not desired
product behavior. Remove or invert them when the corresponding issues are fixed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tool-module imports can initialize log files. Redirect them before importing.
_sandbox = Path(tempfile.mkdtemp(prefix="saiverse-audit-20260730-"))
# Leave diagnostics here: imported database/logging modules can hold open files
# until process exit on Windows. Do not recursively delete a live test store.
os.environ["SAIVERSE_HOME"] = str(_sandbox)
os.environ["SAIVERSE_USER_DATA_DIR"] = str(_sandbox / "user_data")
os.environ["SAIVERSE_LOG_PATH"] = str(_sandbox / "log.txt")

from sea.eviction_plan import Watermarks, stored_message_chars  # noqa: E402
from sea.head_pipeline.pipeline import HeadPipeline  # noqa: E402
from sea.head_pipeline.registry import HeadSectionRegistry  # noqa: E402
from sea.head_pipeline.sections.memory_weave import MemoryWeaveSection  # noqa: E402
from sea.head_pipeline.types import LineHeadInput  # noqa: E402
from sea.session_lifecycle import (  # noqa: E402
    _WEAVE_INSPECT_FAILED,
    SessionLifecycle,
)
from sea.session_window import FoldedRange, SessionWindow  # noqa: E402


def probe_refold_floor() -> dict:
    """Run the real refold planner and real presentation with a fake digest."""
    lc = SessionLifecycle(None, None)
    rows = [
        {"id": f"m{i}", "role": "user", "content": "x" * 1000, "created_at": i}
        for i in range(12)
    ]
    fold = FoldedRange(
        message_ids=[r["id"] for r in rows[:10]],
        chronicle_entry_ids=["synthetic-digest"], presented_raw=True,
    )
    window = SessionWindow("m0", rows, list(rows), [fold])
    with patch.object(lc, "_resolve_fold_digest", return_value="synthetic digest"):
        result = lc._refold_raw_view_plan(None, window, Watermarks(target=5000, high=10000))
    assert result is not None
    after, count = result
    after_chars = stored_message_chars(after)
    assert after_chars < 5000, after_chars
    # Feed the actual result into the real inverse planner, without persistence.
    refolded = SessionWindow("m0", rows, after, [fold])
    with patch.object(lc, "get_presented_window", return_value=refolded), patch.object(
        lc, "_resolve_fold_digest", return_value="synthetic digest",
    ), patch.object(lc, "presented_chars", side_effect=lambda _p, rows, *_a, **_kw: stored_message_chars(rows)):
        reopened = lc._plan_window_refill(
            None, "audit-model", "m0", Watermarks(target=5000, high=10000),
        )
    assert reopened is not None and reopened["final_chars"] == 12000
    assert reopened["opened_in_window"] == 1
    return {"before": 12000, "target": 5000, "after": after_chars,
            "reopened": 12000, "flipped": count}


def probe_head_capture_failure() -> dict:
    """An actual section + pipeline replace a valid snapshot with an empty one."""
    section = MemoryWeaveSection()
    registry = HeadSectionRegistry()
    registry.register(section)
    pipeline = HeadPipeline(registry=registry)  # No persistent store.
    persona = SimpleNamespace(persona_id="audit-synthetic", sai_memory=None)
    ctx = LineHeadInput(
        persona_id=persona.persona_id, model_key="audit-model",
        persona=persona, manager=SimpleNamespace(),
    )
    payload = [{"role": "user", "content": "synthetic past",
                "metadata": {"__memory_weave_type__": "chronicle"}}]
    with patch.object(section, "_resolve_enabled", return_value=True), patch(
        "builtin_data.tools.get_memory_weave_context.get_memory_weave_context",
        side_effect=[payload, RuntimeError("synthetic read failure")],
    ):
        before = pipeline.capture_all(ctx)
        after = pipeline.capture_all(ctx)
    old = before.sections["memory_weave"]
    new = after.sections["memory_weave"]
    assert len(old.entries) == 1 and new.entries == () and old is not new
    assert after.capture_failures == {}
    lc = SessionLifecycle(None, None)
    with patch.object(lc, "_head_weave_snapshot", side_effect=[old, new]), patch(
        "saiverse.dynamic_state.DynamicStateManager.on_metabolism", return_value=True,
    ):
        accepted = lc._recapture_head_after_refill(persona, persona.persona_id, "audit-model", "audit")
    assert accepted is True
    return {"entries_before": 1, "entries_after": 0,
            "capture_failures": after.capture_failures, "accepted_as_success": accepted}


def probe_unknown_head_inspection() -> dict:
    """The refill caller still treats the new failure sentinel as a snapshot."""
    lc = SessionLifecycle(None, None)
    with patch.object(lc, "_head_weave_snapshot", side_effect=[object(), _WEAVE_INSPECT_FAILED]), patch(
        "saiverse.dynamic_state.DynamicStateManager.on_metabolism", return_value=True,
    ):
        accepted = lc._recapture_head_after_refill(None, "audit-synthetic", "audit-model", "audit")
    assert accepted is True
    return {"inspection": "failed", "accepted_as_success": accepted}


def probe_historical_guards() -> dict:
    """Execute the committed, pure July planner with synthetic messages only."""
    git = shutil.which("git") or "C:/Program Files/Git/cmd/git.exe"
    source = subprocess.check_output(
        [git, "show", "7dae379a:sea/window_refill.py"],
        cwd=Path(__file__).resolve().parents[1],
    ).decode("utf-8")
    module = ModuleType("audit_historical_window_refill")
    sys.modules[module.__name__] = module
    exec(compile(source, "7dae379a:sea/window_refill.py", "exec"), module.__dict__)
    raw = [{"id": "m0", "content": "x" * 6000}]
    fold = FoldedRange(message_ids=["m0"])
    presented = [{"id": "folded:m0", "content": "digest",
                  "metadata": {"__folded_range__": True}}]
    chosen, _ = module.plan_reopen([fold], raw, presented, 1000, 5000)
    assert chosen == []
    before = [{"id": f"b{i}", "content": "x" * 1000} for i in range(2)]
    entry = SimpleNamespace(id="e", short_id=1, source_ids=["b0"])
    gap_plan = module.plan_rewind(before, [entry], [], set(), set(), {"b0", "b1"}, 5000)
    assert gap_plan is None
    entry.source_ids = ["outside-read-budget", "b0", "b1"]
    missing_plan = module.plan_rewind(before, [entry], [], set(), set(), {"b0", "b1"}, 5000)
    assert missing_plan is None
    return {"oversized_fold_refused": True, "uncovered_gap_refused": True,
            "source_outside_read_budget_refused": True}


if __name__ == "__main__":
    print(json.dumps({
        "historical_guards": probe_historical_guards(),
        "refold_floor": probe_refold_floor(),
        "head_capture_failure": probe_head_capture_failure(),
        "unknown_head_inspection": probe_unknown_head_inspection(),
    }, ensure_ascii=False, indent=2))
