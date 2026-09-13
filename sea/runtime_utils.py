from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


def event_building_id(runtime: Any, persona: Any) -> Optional[str]:
    """画面イベントに載せる「いまペルソナが居る部屋」。分からなければ ``None``。

    フロントは ``building_id`` を名乗るイベントを、閲覧中の部屋と突き合わせて
    「別の部屋のものなら吹き出しに触らない」と判定する
    (docs/issues/pulse_beats_merge_into_single_record.md 契約 5)。名乗らない
    イベントは従来どおり素通しされるので、引けなかったときは名乗らない方へ倒す。

    部屋 id を引数で受け取らないノード (TOOL / MEMORIZE / tool_call / 自動想起)
    のための口。現在地の正は在室表なので ``_effective_building_id`` に聞く —
    在室表を持たない部分構築の runtime (テストのスタブ) では黙って None を返す。
    """
    resolver = getattr(runtime, "_effective_building_id", None)
    if resolver is None:
        return None
    try:
        return resolver(persona, "") or None
    except Exception:
        LOGGER.debug(
            "[sea] could not resolve the current building for a UI event",
            exc_info=True,
        )
        return None


def _is_llm_streaming_enabled() -> bool:
    """Check whether LLM streaming is enabled via env var (default: enabled)."""
    val = os.getenv("SAIVERSE_LLM_STREAMING", "true")
    result = val.lower() not in ("false", "0", "off", "no")
    logging.info("[DEBUG] _is_llm_streaming_enabled: raw_val=%r, result=%s", val, result)
    return result


def _format(template: str, variables: Dict[str, Any]) -> str:
    """Simple {key} placeholder replacement formatter."""
    lookup: Dict[str, str] = {}
    for key, value in variables.items():
        lookup[str(key)] = "" if value is None else str(value)

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in lookup:
            return lookup[key]
        return match.group(0)

    return re.sub(r"\{([\w.]+)\}", replacer, template)


def _resolve_template_arg(template: str, variables: Dict[str, Any]) -> Any:
    """Resolve a template arg, preserving non-string types for pure variable references.

    If the template is exactly ``{key}`` (a single variable reference with no
    surrounding text) and the corresponding value is a dict or list, return
    the original value instead of stringifying it.  This allows structured
    data (e.g. metadata dicts) to flow through playbook args without being
    converted to their ``str()`` representation.

    For any other template pattern (e.g. ``"prefix {key} suffix"``), fall
    back to the regular ``_format`` string interpolation.
    """
    m = re.fullmatch(r"\{([\w.]+)\}", template)
    if m:
        key = m.group(1)
        if key in variables:
            return variables[key]
    return _format(template, variables)
