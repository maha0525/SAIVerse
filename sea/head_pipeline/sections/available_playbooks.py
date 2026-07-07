"""AvailablePlaybooksSection — "## 利用可能なPlaybook" を head に。

`sea/runtime_context.py` 旧 system prompt の 4. available playbooks を移植。
capture で ``list_available_playbooks`` ツールを 1 回呼んで結果を frozen 化する。
平時の render はそこから文字列を組み立てるだけ (= live tool を再呼び出ししない)。

BUILDING_ENTERED を refresh_on_events に **含めない**。Building 移動で Playbook 一覧が
変動した場合は diff 経由で末尾通知し、head は次の Metabolism まで据え置く。

詳細: docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybookEntry:
    name: str
    description: str


@dataclass(frozen=True)
class AvailablePlaybooksSnapshot:
    entries: tuple[PlaybookEntry, ...]


class AvailablePlaybooksSection:
    name = "available_playbooks"
    order = 400
    refresh_on_events = frozenset({EventType.ADDON_LOADED, EventType.ADDON_UNLOADED})

    def capture(self, ctx: LineHeadInput) -> AvailablePlaybooksSnapshot:
        from tools import TOOL_REGISTRY

        list_func = TOOL_REGISTRY.get("list_available_playbooks")
        if list_func is None:
            return AvailablePlaybooksSnapshot(entries=())
        try:
            raw = list_func(
                persona_id=ctx.persona_id,
                building_id=ctx.current_building_id,
            )
        except Exception:
            LOGGER.warning(
                "available_playbooks: list_available_playbooks raised",
                exc_info=True,
            )
            return AvailablePlaybooksSnapshot(entries=())

        payload = raw[0] if isinstance(raw, tuple) else raw
        if not payload:
            return AvailablePlaybooksSnapshot(entries=())
        try:
            parsed = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError:
            LOGGER.warning(
                "available_playbooks: failed to parse list_available_playbooks output",
                exc_info=True,
            )
            return AvailablePlaybooksSnapshot(entries=())

        entries: list[PlaybookEntry] = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                description = (item.get("description") or "").strip()
                entries.append(PlaybookEntry(name=name, description=description))
        entries.sort(key=lambda e: e.name)
        return AvailablePlaybooksSnapshot(entries=tuple(entries))

    def render(self, snapshot: AvailablePlaybooksSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.entries:
            return None
        lines = [
            "## 利用可能なPlaybook",
            "",
            "`run_playbook` スペルの `playbook` 引数に以下の名前を渡すと実行できる:",
            "",
        ]
        for entry in snapshot.entries:
            if entry.description:
                lines.append(f"- **{entry.name}**: {entry.description}")
            else:
                lines.append(f"- **{entry.name}**")
        return RenderedSection(text="\n".join(lines))

    def diff_to_notifications(
        self,
        old: Optional[AvailablePlaybooksSnapshot],
        new: Optional[AvailablePlaybooksSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        old_names = {e.name for e in old.entries}
        new_names = {e.name for e in new.entries}
        labels: list[NotificationLabel] = []
        for name in sorted(new_names - old_names):
            labels.append(NotificationLabel(
                kind="playbook_added",
                label=f"新しい能力「{name}」が使えるようになりました",
            ))
        for name in sorted(old_names - new_names):
            labels.append(NotificationLabel(
                kind="playbook_removed",
                label=f"能力「{name}」が使えなくなりました",
            ))
        return labels

    def serialize_snapshot(self, snapshot: AvailablePlaybooksSnapshot) -> str:
        return json.dumps(
            {"entries": [asdict(e) for e in snapshot.entries]},
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> AvailablePlaybooksSnapshot:
        payload = json.loads(data)
        entries = tuple(PlaybookEntry(**e) for e in payload.get("entries", []))
        return AvailablePlaybooksSnapshot(entries=entries)
