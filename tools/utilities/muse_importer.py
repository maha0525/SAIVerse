"""Import Muse (Meta personal AI) "Download your agent data" exports.

The manifest is the source of truth: the human-readable transcript that Muse
also writes duplicates it.  This module turns ``manifest.json`` into the same
``ConversationMessage`` / ``ConversationRecord`` shapes the ChatGPT importer
uses, so the CLI can reuse the existing memory-payload contract.

The export can be passed as a ZIP file, an extracted directory, or a
manifest.json file.  Workspace files are kept as metadata only; the files
themselves are not restored into SAIVerse.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Optional, Sequence

from tools.utilities.chatgpt_importer import ConversationMessage, ConversationRecord

LOGGER = logging.getLogger(__name__)

UTC = timezone.utc

_MANIFEST_RE = re.compile(r"(?:^|/)manifest\.json$")

#: Keys the Muse manifest defines on a turn.  Anything else is preserved in
#: ``metadata["muse_extra"]`` instead of being dropped.
_TURN_KEYS = frozenset(
    {"turn_id", "role", "content", "source", "created_at", "channels", "transcript_file", "resources"}
)

#: Keys the Muse manifest defines on a workspace file entry.
_WORKSPACE_FILE_KEYS = frozenset(
    {"source", "workspace_path", "bundled_file_path", "size_bytes", "unresolved_reason"}
)


def _load_manifest_payload(source: Path | str) -> dict[str, Any]:
    """Read the manifest from a ZIP, an extracted directory, or a JSON file."""
    source = Path(source).expanduser()
    if source.is_dir():
        direct = source / "manifest.json"
        if direct.is_file():
            target = direct
        else:
            candidates = sorted(
                (path for path in source.rglob("manifest.json") if path.is_file()),
                key=lambda path: (len(path.relative_to(source).parts), str(path)),
            )
            if not candidates:
                raise FileNotFoundError(f"manifest.json not found in directory: {source}")
            target = candidates[0]
        payload = _read_manifest_json(target)
        return _require_manifest_dict(payload, target)

    if not source.exists():
        raise FileNotFoundError(f"Muse export not found: {source}")

    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            candidates = [name for name in zf.namelist() if _MANIFEST_RE.search(name)]
            if not candidates:
                raise FileNotFoundError("manifest.json not found inside export ZIP")
            # Prefer a top-level manifest, then shorter paths/names.
            candidates.sort(key=lambda name: (name.count("/"), len(name), name))
            target = candidates[0]
            if len(candidates) > 1:
                LOGGER.warning(
                    "Found %d manifest.json files in %s; using %s",
                    len(candidates), source, target,
                )
            with zf.open(target) as raw:
                with TextIOWrapper(raw, encoding="utf-8-sig") as fh:
                    payload = json.load(fh)
        return _require_manifest_dict(payload, Path(f"{source}:{target}"))

    payload = _read_manifest_json(source)
    return _require_manifest_dict(payload, source)


def _read_manifest_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def _require_manifest_dict(payload: Any, source: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"Muse manifest must be a JSON object: {source}")
    return payload


def _dt_from_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp and normalise it to aware UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    iso = dt.astimezone(UTC).replace(microsecond=0).isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


def _as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MuseWorkspaceFile:
    """One entry of ``manifest.json``'s ``workspace_files`` (metadata only)."""

    source: Optional[str]
    workspace_path: Optional[str]
    bundled_file_path: Optional[str]
    size_bytes: Optional[int]
    unresolved_reason: Optional[str]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "workspace_path": self.workspace_path,
            "bundled_file_path": self.bundled_file_path,
            "size_bytes": self.size_bytes,
            "unresolved_reason": self.unresolved_reason,
        }
        for key, value in self.extra.items():
            data.setdefault(key, value)
        return data


def _build_workspace_file(entry: Any) -> MuseWorkspaceFile:
    if not isinstance(entry, dict):
        # A malformed entry is still a file the manifest counts; keep it
        # without failing the whole export.
        return MuseWorkspaceFile(
            source=None,
            workspace_path=None,
            bundled_file_path=None,
            size_bytes=None,
            unresolved_reason=None,
            extra={"raw": entry},
        )
    extra = {key: value for key, value in entry.items() if key not in _WORKSPACE_FILE_KEYS}
    return MuseWorkspaceFile(
        source=_as_optional_str(entry.get("source")),
        workspace_path=_as_optional_str(entry.get("workspace_path")),
        bundled_file_path=_as_optional_str(entry.get("bundled_file_path")),
        size_bytes=_as_optional_int(entry.get("size_bytes")),
        unresolved_reason=_as_optional_str(entry.get("unresolved_reason")),
        extra=extra,
    )


def _turn_metadata(turn: dict[str, Any]) -> dict[str, Any]:
    """Keep every known provenance field and park unknown ones under ``muse_extra``."""
    metadata: dict[str, Any] = {}
    source = turn.get("source")
    if source is not None:
        metadata["source"] = source
    channels = turn.get("channels")
    if channels is not None:
        metadata["channels"] = channels
    transcript_file = turn.get("transcript_file")
    if transcript_file is not None:
        metadata["transcript_file"] = transcript_file
    resources = turn.get("resources")
    if resources is not None:
        # The real schema is unconfirmed; do not interpret the elements.
        metadata["resources"] = resources
    extra = {key: value for key, value in turn.items() if key not in _TURN_KEYS}
    if extra:
        metadata["muse_extra"] = extra
    return metadata


def _build_message(turn: dict[str, Any], index: int) -> ConversationMessage:
    turn_id = _as_optional_str(turn.get("turn_id")) or f"muse-turn-{index}"
    role_value = turn.get("role")
    role = _as_optional_str(role_value) or "system"
    content_value = turn.get("content")
    if isinstance(content_value, str):
        content = content_value.strip()
    elif content_value is None:
        content = ""
    else:
        content = str(content_value).strip()
    create_time = _dt_from_iso(turn.get("created_at"))
    return ConversationMessage(
        node_id=turn_id,
        role=role,
        content=content,
        create_time=create_time,
        metadata=_turn_metadata(turn),
    )


def _first_transcript_file(turns: Sequence[dict[str, Any]]) -> Optional[str]:
    for turn in turns:
        value = turn.get("transcript_file")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _record_identifier(manifest: dict[str, Any], turns: Sequence[dict[str, Any]], source: Path) -> str:
    file_name = manifest.get("file_name")
    if isinstance(file_name, str) and file_name.strip():
        return Path(file_name.strip()).stem
    transcript = _first_transcript_file(turns)
    if transcript:
        return Path(transcript).stem
    return source.stem or "muse-export"


def _record_title(manifest: dict[str, Any], turns: Sequence[dict[str, Any]], identifier: str) -> str:
    transcript = _first_transcript_file(turns)
    if transcript:
        return Path(transcript).stem
    file_name = manifest.get("file_name")
    if isinstance(file_name, str) and file_name.strip():
        return Path(file_name.strip()).stem
    return identifier


def _build_record(
    manifest: dict[str, Any],
    source: Path,
) -> ConversationRecord:
    raw_turns = manifest.get("turns")
    turns = [turn for turn in raw_turns if isinstance(turn, dict)] if isinstance(raw_turns, list) else []
    messages = [_build_message(turn, index) for index, turn in enumerate(turns)]
    identifier = _record_identifier(manifest, turns, source)
    title = _record_title(manifest, turns, identifier)
    exported_at = _dt_from_iso(manifest.get("exported_at"))
    create_time = (messages[0].create_time or exported_at) if messages else exported_at
    update_time = (messages[-1].create_time or exported_at) if messages else exported_at
    return ConversationRecord(
        identifier=identifier,
        title=title,
        create_time=create_time,
        update_time=update_time,
        messages=messages,
        conversation_id=identifier,
        default_model_slug=None,
    )


class MuseExport:
    """A Muse agent-data export, parsed from its manifest.json."""

    def __init__(self, source: Path | str) -> None:
        self.source = Path(source).expanduser()
        manifest = _load_manifest_payload(self.source)
        self.manifest = manifest
        self.exported_at = _dt_from_iso(manifest.get("exported_at"))
        file_name = manifest.get("file_name")
        self.file_name = _as_optional_str(file_name)
        raw_workspace_files = manifest.get("workspace_files")
        if isinstance(raw_workspace_files, list):
            self.workspace_files = [_build_workspace_file(entry) for entry in raw_workspace_files]
        else:
            self.workspace_files = []
        voice_audio = manifest.get("voice_audio")
        self.voice_audio = voice_audio if isinstance(voice_audio, list) else []
        self.voice_audio_inventory_unavailable = bool(
            manifest.get("voice_audio_inventory_unavailable")
        )
        self.mail = manifest.get("mail")
        self.blobs = manifest.get("blobs")
        self._records = [_build_record(manifest, source)]

    @property
    def conversations(self) -> list[ConversationRecord]:
        return self._records

    @property
    def turns(self) -> list[ConversationMessage]:
        """The messages of the export's single conversation (empty when there is none)."""
        return self._records[0].messages if self._records else []

    @property
    def workspace_file_count(self) -> int:
        return len(self.workspace_files)

    def get_by_identifier(self, identifier: str) -> Optional[ConversationRecord]:
        for record in self._records:
            if identifier in {record.identifier, record.conversation_id}:
                return record
        return None

    def summaries(self, *, preview_chars: int = 120) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for record in self._records:
            summary = record.to_summary_dict(preview_chars=preview_chars)
            summary["exported_at"] = _format_datetime(self.exported_at)
            summary["workspace_file_count"] = self.workspace_file_count
            summary["workspace_files"] = [item.to_dict() for item in self.workspace_files]
            summaries.append(summary)
        return summaries
