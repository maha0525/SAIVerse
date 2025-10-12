from __future__ import annotations

import json
import textwrap
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence


UTC = timezone.utc


def _load_conversations_payload(source: Path) -> list[dict[str, Any]]:
    if not source.exists():
        raise FileNotFoundError(f"ChatGPT export not found: {source}")

    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            candidates = [name for name in zf.namelist() if name.endswith("conversations.json")]
            if not candidates:
                raise FileNotFoundError("conversations.json not found inside export ZIP")
            # Prefer top-level file if available, otherwise pick the shortest path.
            candidates.sort(key=lambda name: (name.count("/"), len(name)))
            target = candidates[0]
            with zf.open(target) as raw:
                with TextIOWrapper(raw, encoding="utf-8") as fh:
                    data = json.load(fh)
    else:
        text = source.read_text(encoding="utf-8")
        data = json.loads(text)

    if not isinstance(data, list):
        raise ValueError("Expected conversations.json to contain a list")
    return [entry for entry in data if isinstance(entry, dict)]


def _dt_from_epoch(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        # Some exports store timestamps in milliseconds or microseconds.
        for _ in range(4):
            if ts < 0:
                break
            if ts < 32503680000:  # 3000-01-01T00:00:00Z
                break
            ts /= 1000.0
        if ts < 0:
            return None
        if ts >= 32503680000:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    iso = dt.astimezone(UTC).replace(microsecond=0).isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


def _extract_text_from_parts(parts: Any) -> str:
    if not parts:
        return ""
    if isinstance(parts, list):
        collected: list[str] = []
        for item in parts:
            if isinstance(item, str):
                collected.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    collected.append(text)
        return "\n".join(collected).strip()
    if isinstance(parts, str):
        return parts.strip()
    return ""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    ctype = content.get("content_type")
    if ctype in {"text", "tether_browsing_display"}:
        return _extract_text_from_parts(content.get("parts"))
    if ctype == "multimodal_text":
        return _extract_text_from_parts(content.get("parts"))
    if ctype == "code":
        return _extract_text_from_parts(content.get("text"))
    if ctype == "tool_result":
        # Tool results may include JSON or plain text in 'tool_outputs'
        outputs = content.get("tool_outputs")
        if isinstance(outputs, list):
            texts: list[str] = []
            for entry in outputs:
                if isinstance(entry, dict):
                    content_str = entry.get("content")
                    if isinstance(content_str, str):
                        texts.append(content_str)
            if texts:
                return "\n".join(texts).strip()
        return ""
    # Fallback: try parts if present.
    return _extract_text_from_parts(content.get("parts"))


def _is_hidden(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("is_visually_hidden_from_conversation"))


def _conversation_path(mapping: dict[str, Any], current_node: Optional[str]) -> List[str]:
    if not mapping:
        return []
    if current_node and current_node in mapping:
        ordered: List[str] = []
        seen: set[str] = set()
        node_id: Optional[str] = current_node
        while node_id and node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
            node = mapping.get(node_id) or {}
            node_id = node.get("parent")
            if node_id is None:
                break
            if node_id not in mapping:
                break
        ordered.reverse()
        return ordered

    # Fallback: return nodes with messages sorted by timestamp.
    entries = []
    for node_id, node in mapping.items():
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        ts = _dt_from_epoch(message.get("create_time")) or datetime.min.replace(tzinfo=UTC)
        entries.append((ts, node_id))
    entries.sort()
    return [node_id for _, node_id in entries]


@dataclass
class ConversationMessage:
    node_id: str
    role: str
    content: str
    create_time: Optional[datetime]
    metadata: dict[str, Any]

    def to_memory_payload(self) -> dict[str, Any]:
        timestamp = _format_datetime(self.create_time)
        payload: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if timestamp:
            payload["timestamp"] = timestamp
        return payload


@dataclass
class ConversationRecord:
    identifier: str
    title: str
    create_time: Optional[datetime]
    update_time: Optional[datetime]
    messages: list[ConversationMessage]
    conversation_id: Optional[str]
    default_model_slug: Optional[str]

    def message_count(self, *, include_hidden: bool = False) -> int:
        if include_hidden:
            return len(self.messages)
        return sum(1 for msg in self.messages if msg.content)

    def first_user_preview(self, limit: int = 120) -> str:
        candidate = next((m.content for m in self.messages if m.role == "user" and m.content), "")
        if not candidate:
            candidate = next((m.content for m in self.messages if m.content), "")
        if not candidate:
            return ""
        return textwrap.shorten(candidate.replace("\n", " ").strip(), width=max(10, limit), placeholder="…")

    def to_summary_dict(self, *, preview_chars: int = 120) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "create_time": _format_datetime(self.create_time),
            "update_time": _format_datetime(self.update_time),
            "message_count": self.message_count(),
            "first_user_preview": self.first_user_preview(limit=preview_chars),
            "default_model": self.default_model_slug,
        }

    def iter_memory_payloads(self, *, include_roles: Optional[Sequence[str]] = None) -> Iterable[dict[str, Any]]:
        allowed = set(include_roles) if include_roles else None
        for message in self.messages:
            if allowed is not None and message.role not in allowed:
                continue
            if not message.content and message.role not in {"system"}:
                continue
            yield message.to_memory_payload()


class ChatGPTExport:
    def __init__(self, source: Path) -> None:
        self.source = source
        raw = _load_conversations_payload(source)
        self._records = [_build_record(entry) for entry in raw]

    @property
    def conversations(self) -> list[ConversationRecord]:
        return self._records

    def get_by_identifier(self, identifier: str) -> Optional[ConversationRecord]:
        for record in self._records:
            if identifier in {record.identifier, record.conversation_id}:
                return record
        return None

    def summaries(self, *, preview_chars: int = 120) -> list[dict[str, Any]]:
        return [record.to_summary_dict(preview_chars=preview_chars) for record in self._records]


def _build_record(entry: dict[str, Any]) -> ConversationRecord:
    title = entry.get("title") or "(untitled)"
    identifier = entry.get("id") or entry.get("conversation_id") or title
    create_time = _dt_from_epoch(entry.get("create_time"))
    update_time = _dt_from_epoch(entry.get("update_time"))
    default_model = entry.get("default_model_slug")
    mapping = entry.get("mapping") or {}
    if not isinstance(mapping, dict):
        mapping = {}
    current_node = entry.get("current_node")

    path_ids = _conversation_path(mapping, current_node)
    messages: list[ConversationMessage] = []
    for node_id in path_ids:
        node = mapping.get(node_id) or {}
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        if _is_hidden(message):
            continue
        role = message.get("author", {}).get("role") or "system"
        content = _message_text(message)
        create_dt = _dt_from_epoch(message.get("create_time"))
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        msg = ConversationMessage(
            node_id=node_id,
            role=role,
            content=content.strip(),
            create_time=create_dt,
            metadata=metadata,
        )
        messages.append(msg)

    return ConversationRecord(
        identifier=str(identifier),
        title=str(title),
        create_time=create_time,
        update_time=update_time,
        messages=messages,
        conversation_id=entry.get("conversation_id"),
        default_model_slug=default_model,
    )
