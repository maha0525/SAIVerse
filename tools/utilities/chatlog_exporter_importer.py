"""
Importer for Chrome extension exported chat logs (ChatGPT/Gemini/Claude Exporter).

Supports both JSON and Markdown formats exported by:
- ChatGPT Exporter (https://www.chatgptexporter.com)
- Gemini Exporter (https://www.ai-chat-exporter.com)
- Claude Exporter (https://www.claudexporter.com)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

LOGGER = logging.getLogger(__name__)


@dataclass
class ExporterMessage:
    """A single message from an exported conversation."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: Optional[datetime] = None


@dataclass
class ExporterConversation:
    """A conversation exported by Chrome extension."""

    title: str
    source: str  # "chatgpt", "gemini", or "claude"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    link: Optional[str] = None
    messages: List[ExporterMessage] = field(default_factory=list)
    raw_file_path: Optional[str] = None

    @property
    def identifier(self) -> str:
        """Generate a unique identifier for the conversation."""
        if self.link:
            # Extract ID from URL
            parts = self.link.rstrip("/").split("/")
            if parts:
                return parts[-1]
        # Fallback to title-based ID
        safe_title = re.sub(r"[^\w\-]", "_", self.title)[:32]
        ts = self.created_at or self.updated_at
        if ts:
            return f"{safe_title}_{ts.strftime('%Y%m%d%H%M%S')}"
        return safe_title

    def iter_memory_payloads(
        self,
        *,
        include_roles: Optional[Sequence[str]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield payloads suitable for SAIMemory.append_persona_message()."""
        for msg in self.messages:
            if include_roles and msg.role not in include_roles:
                continue

            payload: Dict[str, Any] = {
                "role": msg.role,
                "content": msg.content,
            }

            if msg.timestamp:
                iso = msg.timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                if iso.endswith("+00:00"):
                    iso = iso[:-6] + "Z"
                payload["timestamp"] = iso

            payload["metadata"] = {"tags": ["conversation"]}
            yield payload


def _deduplicate_messages(messages: List[ExporterMessage]) -> List[ExporterMessage]:
    """
    Remove consecutive duplicate messages (same content, ignoring whitespace).

    This handles a known bug where messages are sometimes duplicated in exports,
    which can cause role labels to become swapped.
    """
    if not messages:
        return []

    result: List[ExporterMessage] = []
    for msg in messages:
        normalized = msg.content.strip()
        if not result or result[-1].content.strip() != normalized:
            result.append(msg)
        else:
            LOGGER.debug(
                "Removed duplicate message: %s",
                normalized[:50] + "..." if len(normalized) > 50 else normalized,
            )
    return result


def _parse_datetime_flexible(value: Optional[str]) -> Optional[datetime]:
    """Parse datetime from various formats used by exporters."""
    if not value:
        return None

    # Normalize separators
    value = value.strip()

    # Try various formats
    formats = [
        # YYYY/MM/DD HH:mm:ss (ChatGPT, Claude messages)
        "%Y/%m/%d %H:%M:%S",
        # MM/DD/YYYY HH:mm:ss (Gemini, Claude metadata)
        "%m/%d/%Y %H:%M:%S",
        # YYYY/MM/DD H:mm:ss (single digit hour)
        "%Y/%m/%d %H:%M:%S",
        # MM/DD/YYYY H:mm:ss
        "%m/%d/%Y %H:%M:%S",
        # ISO format
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]

    # Handle single-digit time components by normalizing
    # e.g., "2025/12/14 2:35:19" -> "2025/12/14 02:35:19"
    normalized = re.sub(r" (\d):(\d{2}):(\d{2})", r" 0\1:\2:\3", value)

    for fmt in formats:
        try:
            dt = datetime.strptime(normalized, fmt)
            # Assume local time, convert to UTC-aware
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    LOGGER.warning("Could not parse datetime: %s", value)
    return None


def _interpolate_timestamps(
    messages: List[ExporterMessage],
    start_time: datetime,
    end_time: datetime,
) -> None:
    """
    Set timestamps for messages using linear interpolation between start and end.

    First message gets start_time, last message gets end_time, intermediate
    messages are evenly distributed.
    """
    count = len(messages)
    if count == 0:
        return

    if count == 1:
        messages[0].timestamp = start_time
        return

    total_seconds = (end_time - start_time).total_seconds()
    for i, msg in enumerate(messages):
        if count > 1:
            offset = (i / (count - 1)) * total_seconds
        else:
            offset = 0
        msg.timestamp = start_time + timedelta(seconds=offset)


def _increment_timestamps(
    messages: List[ExporterMessage],
    start_time: datetime,
    interval_seconds: int = 60,
) -> None:
    """Set timestamps for messages by incrementing from start_time."""
    for i, msg in enumerate(messages):
        msg.timestamp = start_time + timedelta(seconds=i * interval_seconds)


def _detect_source(metadata: Dict[str, Any]) -> str:
    """Detect the source (chatgpt, gemini, claude) from metadata."""
    powered_by = metadata.get("powered_by", "").lower()
    if "chatgpt" in powered_by:
        return "chatgpt"
    if "gemini" in powered_by:
        return "gemini"
    if "claude" in powered_by:
        return "claude"
    return "unknown"


def _parse_json_format(
    data: Dict[str, Any],
    file_path: Optional[str] = None,
    gemini_start_time: Optional[datetime] = None,
) -> ExporterConversation:
    """Parse JSON format exported by Chrome extensions."""
    metadata = data.get("metadata", {})
    source = _detect_source(metadata)

    title = metadata.get("title", "Untitled")
    link = metadata.get("link")

    dates = metadata.get("dates", {})
    created_at = _parse_datetime_flexible(dates.get("created"))
    updated_at = _parse_datetime_flexible(dates.get("updated"))

    raw_messages = data.get("messages", [])
    messages: List[ExporterMessage] = []

    for raw_msg in raw_messages:
        raw_role = raw_msg.get("role", "")
        content = raw_msg.get("say", "")

        # Normalize role names
        if raw_role.lower() in ("prompt", "user"):
            role = "user"
        elif raw_role.lower() in ("response", "assistant"):
            role = "assistant"
        else:
            role = raw_role.lower()

        # Check for per-message timestamp (Claude format)
        msg_time = _parse_datetime_flexible(raw_msg.get("time"))

        messages.append(ExporterMessage(role=role, content=content, timestamp=msg_time))

    # Deduplicate messages
    messages = _deduplicate_messages(messages)

    # Apply timestamp logic based on source
    if source == "claude":
        # Claude has per-message timestamps, already parsed
        pass
    elif source == "chatgpt":
        # Interpolate between created and updated
        if created_at and updated_at and messages:
            _interpolate_timestamps(messages, created_at, updated_at)
        elif created_at and messages:
            _increment_timestamps(messages, created_at)
    elif source == "gemini":
        # Use provided start time or fallback
        start = gemini_start_time or created_at or datetime.now(tz=timezone.utc)
        if messages:
            _increment_timestamps(messages, start)

    return ExporterConversation(
        title=title,
        source=source,
        created_at=created_at,
        updated_at=updated_at,
        link=link,
        messages=messages,
        raw_file_path=file_path,
    )


def _parse_markdown_format(
    content: str,
    file_path: Optional[str] = None,
    gemini_start_time: Optional[datetime] = None,
) -> ExporterConversation:
    """Parse Markdown format exported by Chrome extensions."""
    lines = content.splitlines()

    # Extract title from first line (# Title)
    title = "Untitled"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()

    # Extract metadata from header
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    link: Optional[str] = None
    source = "unknown"

    # Check header (first 20 lines) for metadata
    for line in lines[:20]:
        if line.startswith("**Created:**") or line.startswith("Created:"):
            match = re.search(r"(?:Created:|\*\*Created:\*\*)\s*(.+?)(?:\s*$|\s+\*\*)", line)
            if match:
                created_at = _parse_datetime_flexible(match.group(1).strip())
        elif line.startswith("**Updated:**") or line.startswith("Updated:"):
            match = re.search(r"(?:Updated:|\*\*Updated:\*\*)\s*(.+?)(?:\s*$|\s+\*\*)", line)
            if match:
                updated_at = _parse_datetime_flexible(match.group(1).strip())
        elif line.startswith("**Exported:**") or line.startswith("Exported:"):
            pass  # We don't use exported time
        elif line.startswith("**Link:**") or line.startswith("Link:"):
            match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", line)
            if match:
                link = match.group(2)

    # Detect source from full content (Powered by line is usually at the end)
    content_lower = content.lower()
    if "chatgpt exporter" in content_lower or "chatgptexporter.com" in content_lower:
        source = "chatgpt"
    elif "gemini exporter" in content_lower or "ai-chat-exporter.com" in content_lower:
        source = "gemini"
    elif "claude exporter" in content_lower or "claudexporter.com" in content_lower:
        source = "claude"

    # Parse messages
    messages: List[ExporterMessage] = []
    current_role: Optional[str] = None
    current_content: List[str] = []
    current_timestamp: Optional[datetime] = None

    for line in lines:
        if line.strip() == "## Prompt:":
            # Save previous message if exists
            if current_role and current_content:
                messages.append(
                    ExporterMessage(
                        role=current_role,
                        content="\n".join(current_content).strip(),
                        timestamp=current_timestamp,
                    )
                )
            current_role = "user"
            current_content = []
            current_timestamp = None
        elif line.strip() == "## Response:":
            if current_role and current_content:
                messages.append(
                    ExporterMessage(
                        role=current_role,
                        content="\n".join(current_content).strip(),
                        timestamp=current_timestamp,
                    )
                )
            current_role = "assistant"
            current_content = []
            current_timestamp = None
        elif current_role:
            # Check for Claude-style timestamp line at start of message
            if not current_content:
                ts_match = re.match(r"^(\d{4}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2})\s*$", line.strip())
                if ts_match:
                    current_timestamp = _parse_datetime_flexible(ts_match.group(1))
                    continue
            current_content.append(line)

    # Save last message
    if current_role and current_content:
        messages.append(
            ExporterMessage(
                role=current_role,
                content="\n".join(current_content).strip(),
                timestamp=current_timestamp,
            )
        )

    # Deduplicate messages
    messages = _deduplicate_messages(messages)

    # Apply timestamp logic
    if source == "claude":
        # Claude MD format has per-message timestamps
        pass
    elif source == "chatgpt":
        if created_at and updated_at and messages:
            _interpolate_timestamps(messages, created_at, updated_at)
        elif created_at and messages:
            _increment_timestamps(messages, created_at)
    elif source == "gemini":
        start = gemini_start_time or created_at or datetime.now(tz=timezone.utc)
        if messages:
            _increment_timestamps(messages, start)

    return ExporterConversation(
        title=title,
        source=source,
        created_at=created_at,
        updated_at=updated_at,
        link=link,
        messages=messages,
        raw_file_path=file_path,
    )


def parse_exporter_file(
    file_path: Union[str, Path],
    *,
    gemini_start_time: Optional[datetime] = None,
) -> ExporterConversation:
    """
    Parse an exported chat log file (JSON or Markdown).

    Args:
        file_path: Path to the exported file
        gemini_start_time: Start time for Gemini exports (which lack timestamps)

    Returns:
        ExporterConversation with parsed messages
    """
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")
    path_str = str(path)

    # Remove BOM if present
    if content.startswith("\ufeff"):
        content = content[1:]

    # Try JSON first
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            return _parse_json_format(data, path_str, gemini_start_time)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to parse %s as JSON, trying Markdown", path)

    # Parse as Markdown
    return _parse_markdown_format(content, path_str, gemini_start_time)


def detect_exporter_source(file_path: Union[str, Path]) -> str:
    """
    Detect the source of an exporter file without fully parsing it.

    Returns: "chatgpt", "gemini", "claude", or "unknown"
    """
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")

    # Remove BOM
    if content.startswith("\ufeff"):
        content = content[1:]

    content_lower = content.lower()

    if "chatgpt exporter" in content_lower or "chatgptexporter.com" in content_lower:
        return "chatgpt"
    if "gemini exporter" in content_lower or "ai-chat-exporter.com" in content_lower:
        return "gemini"
    if "claude exporter" in content_lower or "claudexporter.com" in content_lower:
        return "claude"

    # Try JSON parsing for powered_by field
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            metadata = data.get("metadata", {})
            return _detect_source(metadata)
        except json.JSONDecodeError:
            pass

    return "unknown"
