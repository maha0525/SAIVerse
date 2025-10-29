from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelContext:
    """Discord チャンネルと SAIVerse の City / Building の対応情報。"""

    channel_id: str
    city_id: str
    building_id: str
    host_user_id: str
    allowed_roles: tuple[str, ...] = ()
    invite_required: bool = False


class ChannelMapping:
    """チャネルと City/Building のマッピングを管理する。"""

    def __init__(self, entries: Iterable[ChannelContext]):
        self._table: dict[str, ChannelContext] = {}
        for context in entries:
            self._table[context.channel_id] = context

    def get(self, channel_id: str | int) -> ChannelContext | None:
        key = str(channel_id)
        return self._table.get(key)

    def __contains__(self, channel_id: str | int) -> bool:  # pragma: no cover - trivial
        return str(channel_id) in self._table

    @classmethod
    def from_iterable(cls, raw_entries: Iterable[Mapping[str, str]]) -> ChannelMapping:
        contexts = []
        for entry in raw_entries:
            try:
                contexts.append(
                    ChannelContext(
                        channel_id=str(entry["channel_id"]),
                        city_id=str(entry["city_id"]),
                        building_id=str(entry["building_id"]),
                        host_user_id=str(entry["host_user_id"]),
                        allowed_roles=tuple(str(role) for role in entry.get("allowed_roles", [])),
                        invite_required=bool(entry.get("invite_required", False)),
                    )
                )
            except KeyError as exc:  # pragma: no cover - guarded input
                missing = exc.args[0]
                raise ValueError(f"mapping entry missing key '{missing}'") from exc
        return cls(contexts)

    @classmethod
    def from_json(cls, json_text: str) -> ChannelMapping:
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:  # pragma: no cover - guarded input
            raise ValueError("Invalid JSON for channel mapping") from exc

        if isinstance(payload, dict):
            # allow {"channel_id": {...}} format
            entries = []
            for channel_id, data in payload.items():
                data = dict(data)
                data.setdefault("channel_id", channel_id)
                entries.append(data)
        elif isinstance(payload, list):
            entries = payload
        else:  # pragma: no cover - guarded input
            raise ValueError("Channel mapping must be list or dict")
        return cls.from_iterable(entries)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        var_name: str = "SAIVERSE_GATEWAY_CHANNEL_MAP",
    ) -> ChannelMapping:
        env = environ or os.environ
        json_text = env.get(var_name, "")
        if not json_text:
            return cls([])
        return cls.from_json(json_text)

    def find_by_location(self, city_id: str, building_id: str) -> ChannelContext | None:
        for context in self._table.values():
            if context.city_id == city_id and context.building_id == building_id:
                return context
        return None
