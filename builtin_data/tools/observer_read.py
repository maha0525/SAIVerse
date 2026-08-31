"""Observer の最新メトリクスを読む spell。

ペルソナは observer_metrics / fixture.STATE_JSON から最新のキャッシュ値を
読み取るだけで、センサー I/O や外部 API を直接叩かない。
"""
from __future__ import annotations

from typing import Optional

from tools.context import get_active_manager
from tools.core import ToolSchema


def observer_read(observer_id: str, metric_name: Optional[str] = None) -> str:
    """Read the latest metrics from an Observer.

    Args:
        observer_id: The Observer's ID
        metric_name: If specified, return only this metric's latest value.
                     If omitted, return all metrics for the observer.
    """
    manager = get_active_manager()
    if not manager:
        return "エラー: マネージャーが利用できません。"

    obs_mgr = manager.observer_manager
    if not obs_mgr:
        return "エラー: Observer マネージャーが利用できません。"

    latest = obs_mgr.get_latest_metrics(observer_id)
    if not latest:
        return "データなし: この Observer にはまだ観測値がありません。"

    if metric_name:
        entry = latest.get(metric_name)
        if not entry:
            available = ", ".join(latest.keys())
            return f"メトリクス '{metric_name}' が見つかりません。利用可能: {available}"
        parts = []
        if entry.get("value_num") is not None:
            parts.append(str(entry["value_num"]))
        if entry.get("value_text"):
            parts.append(entry["value_text"])
        recorded = entry.get("recorded_at", "不明")
        return f"{metric_name}: {' / '.join(parts)} (記録: {recorded})"

    lines = []
    for name, entry in latest.items():
        parts = []
        if entry.get("value_num") is not None:
            parts.append(str(entry["value_num"]))
        if entry.get("value_text"):
            parts.append(entry["value_text"])
        recorded = entry.get("recorded_at", "")
        val_str = " / ".join(parts) if parts else "N/A"
        lines.append(f"- {name}: {val_str} ({recorded})")
    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="observer_read",
        description=(
            "Read the latest observation data from a building fixture's sensor/monitor. "
            "Returns cached values — does not trigger new measurements."
        ),
        parameters={
            "type": "object",
            "properties": {
                "observer_id": {
                    "type": "string",
                    "description": "The Observer ID to read from",
                },
                "metric_name": {
                    "type": "string",
                    "description": (
                        "Specific metric to read (e.g., 'heart_rate', 'co2eq'). "
                        "Omit to get all metrics."
                    ),
                },
            },
            "required": ["observer_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="オブザーバー観測値取得",
    )
