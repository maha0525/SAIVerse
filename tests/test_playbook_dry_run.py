import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
mod = importlib.import_module("scripts.playbook_dry_run")


def test_dry_runner_records_suppressed_effects():
    r = mod.DryRunner()
    r.record_tool_call(playbook_name="pb", step=1, node_id="n1", tool_name="http.fetch", params={"url": "x"})
    assert r.plan[0]["tool"] == "http.fetch"
    assert r.plan[0]["suppressed_effects"] == ["network_call"]


def test_build_execution_plan_includes_ordered_steps():
    reports = [
        mod.NodeReport(step=2, node_id="n2", node_type="tool", machine={"tool": "db.write", "params": {"x": 1}}),
        mod.NodeReport(step=1, node_id="n1", node_type="set"),
    ]
    plan = mod.build_execution_plan("pb", reports)
    assert plan["playbook"] == "pb"
    assert plan["steps"][0]["step"] == 2
