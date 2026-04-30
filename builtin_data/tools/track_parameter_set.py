"""track_parameter_set: Track パラメータの連続値を更新するスペル。

intent B v0.7 §"Track パラメータ機構の実装方針" の "ペルソナ自身による明示更新"。
``action_tracks.metadata.parameters[name] = value`` を更新する。

例: 「この掃除 Track は十分やったから dirtiness を 0 に戻す」
    → ``track_parameter_set(track_id="t_clean", parameter_name="dirtiness", value=0.0)``

メタレイヤー判断時にプロンプトに含められる + 内部 alert ポーラの閾値判定で
使われる連続値。0.0〜1.0 推奨。
"""
from __future__ import annotations

import json
from typing import Tuple

from database.session import SessionLocal
from saiverse.track_manager import (
    InvalidTrackStateError,
    TrackManager,
    TrackNotFoundError,
)
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def track_parameter_set(
    track_id: str, parameter_name: str, value: float
) -> Tuple[str, ToolResult, None]:
    """Set a continuous parameter value on a Track.

    Args:
        track_id: Target Track id.
        parameter_name: Parameter key (e.g. 'dirtiness', 'hunger').
        value: Numeric value, 0.0〜1.0 推奨.

    Returns:
        (message, ToolResult, None).
    """
    if not get_active_persona_id():
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    try:
        track = _track_manager.set_parameter(track_id, parameter_name, value)
    except TrackNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except (InvalidTrackStateError, ValueError) as exc:
        raise RuntimeError(f"track_parameter_set failed: {exc}") from exc

    parameters = {}
    if track.track_metadata:
        try:
            metadata = json.loads(track.track_metadata)
            if isinstance(metadata, dict):
                params = metadata.get("parameters")
                if isinstance(params, dict):
                    parameters = params
        except (TypeError, ValueError):
            parameters = {}

    snippet = ToolResult(
        history_snippet=json.dumps(
            {
                "track_id": track.track_id,
                "parameter_name": parameter_name,
                "value": parameters.get(parameter_name),
                "all_parameters": parameters,
            },
            ensure_ascii=False,
        )
    )
    label = track.title or track.track_type
    return (
        f"Set {parameter_name}={parameters.get(parameter_name)} on track '{label}' "
        f"({track.track_id[:8]}…).",
        snippet,
        None,
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_parameter_set",
        description=(
            "Set a continuous-value parameter on a Track "
            "(e.g. dirtiness, hunger, hours_since_check). "
            "The value is stored in action_tracks.metadata.parameters and is "
            "visible to the meta layer for judgment + threshold-based internal "
            "alert polling. Recommended range: 0.0–1.0."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "string",
                    "description": "Target Track id.",
                },
                "parameter_name": {
                    "type": "string",
                    "description": "Parameter key (e.g. 'dirtiness', 'hunger').",
                },
                "value": {
                    "type": "number",
                    "description": "Numeric value (0.0–1.0 recommended).",
                },
            },
            "required": ["track_id", "parameter_name", "value"],
        },
        result_type="string",
        spell=True,
        spell_display_name="トラックパラメータ更新",
    )
