"""Claude (claude.ai) 公式エクスポートの読み取り。

Claude の「設定 → プライバシー → データをエクスポート」は manifest JSON を配り、
その中の URL からカテゴリ別の ZIP (conversations / claude_code_cloud / projects /
memories / frames / light_metadata) を個別にダウンロードさせる。本モジュールは
そのうち取り込み対象の 4 カテゴリを解析し、ChatGPT 取り込みと同じ
``ConversationRecord`` 互換のレコード (または Memopedia 向けの記憶アイテム) に
変換する。設計: docs/intent/claude_import.md

- conversations: 会話ごとに 1 レコード。text ブロックのみ採用し、thinking /
  tool_use / tool_result / injected_prompt_block は捨てる。
- claude_code_cloud: セッション transcript とチャンネルメッセージからスレッドを
  再構成する (text を持つ発言のみ)。
- projects: プロジェクト指示 (prompt_template) を冒頭の system メッセージに、
  資料 (docs) を続く system メッセージにしたスレッド。
- memories: Memopedia へ転記する記憶アイテムの一覧。
- frames: 生成物 (HTML 等) なので取り込み対象外。
- manifest: ZIP 本体ではないので、ダウンロード手順の案内対象。
"""
from __future__ import annotations

import json
import logging
import re
import textwrap
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tools.utilities.chatgpt_importer import ConversationMessage, ConversationRecord

LOGGER = logging.getLogger(__name__)

UTC = timezone.utc

CATEGORY_CONVERSATIONS = "conversations"
CATEGORY_CLAUDE_CODE = "claude_code"
CATEGORY_PROJECTS = "projects"
CATEGORY_MEMORIES = "memories"
# 取り込み対象外 (検出だけして案内を返す)
CATEGORY_FRAMES = "frames"
CATEGORY_MANIFEST = "manifest"
CATEGORY_LIGHT_METADATA = "light_metadata"

IMPORTABLE_CATEGORIES = (
    CATEGORY_CONVERSATIONS,
    CATEGORY_CLAUDE_CODE,
    CATEGORY_PROJECTS,
    CATEGORY_MEMORIES,
)

_CONVERSATIONS_JSON_RE = re.compile(r"(^|/)conversations(?:-\d+)?\.json$")


class ClaudeImportError(ValueError):
    """解析できない / 取り込み対象外のファイル。``code`` で理由を機械判別できる。"""

    def __init__(self, code: str, message: str, *, detected_category: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.detected_category = detected_category


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO 8601 文字列 (Z 接尾辞を含む) を UTC aware datetime に。"""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _shorten(text: str, width: int = 60) -> str:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return ""
    return textwrap.shorten(cleaned, width=max(10, width), placeholder="…")


@dataclass
class ClaudeMemoryItem:
    """memories カテゴリの 1 転記候補 (Memopedia の 1 ページになる)。"""

    item_id: str
    kind: str  # conversations_memory / project_memory / memory_file
    title: str
    content: str
    updated_at: Optional[datetime] = None

    def to_summary_dict(self, *, preview_chars: int = 120) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "kind": self.kind,
            "title": self.title,
            "preview": _shorten(self.content, preview_chars),
            "char_count": len(self.content),
        }


@dataclass
class ClaudeExportFile:
    """アップロードされた 1 ファイルの解析結果。"""

    category: str
    conversations: list[ConversationRecord] = field(default_factory=list)
    memory_items: list[ClaudeMemoryItem] = field(default_factory=list)
    # projects のみ: record.identifier → prompt_template (空は含めない)
    project_instructions: dict[str, str] = field(default_factory=dict)

    def get_record(self, selector: str) -> Optional[ConversationRecord]:
        try:
            idx = int(selector)
            if 0 <= idx < len(self.conversations):
                return self.conversations[idx]
        except ValueError:
            pass
        for record in self.conversations:
            if selector in {record.identifier, record.conversation_id}:
                return record
        return None

    def get_memory_item(self, selector: str) -> Optional[ClaudeMemoryItem]:
        try:
            idx = int(selector)
            if 0 <= idx < len(self.memory_items):
                return self.memory_items[idx]
        except ValueError:
            pass
        for item in self.memory_items:
            if item.item_id == selector:
                return item
        return None


# ---------------------------------------------------------------------------
# カテゴリ判定
# ---------------------------------------------------------------------------

def detect_category(path: Path) -> str:
    """ファイルがエクスポートのどのカテゴリかを中身から判定する。

    ZIP はエントリ名、JSON は構造で見る。判定できなければ
    ``ClaudeImportError(code="not_claude_export")``。
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        return _category_from_names(names)

    # 単体 JSON (ZIP を展開して中身だけ投げ込んだ場合も受ける)
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClaudeImportError(
            "not_claude_export", f"Not a Claude export (unreadable file): {exc}"
        ) from exc
    if isinstance(data, dict):
        if isinstance(data.get("data_files"), list):
            return CATEGORY_MANIFEST
        if "claude_code_cloud" in data:
            return CATEGORY_CLAUDE_CODE
        if "conversations_memory" in data or "memory_files" in data:
            return CATEGORY_MEMORIES
        if "prompt_template" in data and "docs" in data:
            return CATEGORY_PROJECTS
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "chat_messages" in data[0]:
            return CATEGORY_CONVERSATIONS
    raise ClaudeImportError(
        "not_claude_export", "Could not recognize the file as a Claude export"
    )


def _category_from_names(names: list[str]) -> str:
    for name in names:
        if _CONVERSATIONS_JSON_RE.search(name):
            return CATEGORY_CONVERSATIONS
        if "claude_code_cloud/" in name or name.startswith("claude_code_cloud"):
            return CATEGORY_CLAUDE_CODE
        if name.startswith("projects/") or "/projects/" in name:
            return CATEGORY_PROJECTS
        if name.startswith("memories/") or "/memories/" in name:
            return CATEGORY_MEMORIES
        if name.startswith("frames/") or "/frames/" in name or name.startswith("frames"):
            return CATEGORY_FRAMES
        if name in ("users.json", "login_history.json"):
            return CATEGORY_LIGHT_METADATA
    raise ClaudeImportError(
        "not_claude_export", "ZIP does not contain any known Claude export entries"
    )


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------

def load_claude_export(path: Path) -> ClaudeExportFile:
    """アップロードされたファイルを解析する。

    取り込み対象外のカテゴリ (frames / manifest / light_metadata) は
    ``ClaudeImportError`` (code = ``"<category>_not_supported"``) を上げ、
    呼び出し側が案内を出せるよう ``detected_category`` を添える。
    """
    category = detect_category(path)
    if category not in IMPORTABLE_CATEGORIES:
        raise ClaudeImportError(
            f"{category}_not_supported",
            f"Claude export category '{category}' is not importable",
            detected_category=category,
        )
    if category == CATEGORY_CONVERSATIONS:
        return _load_conversations(path)
    if category == CATEGORY_CLAUDE_CODE:
        return _load_claude_code(path)
    if category == CATEGORY_PROJECTS:
        return _load_projects(path)
    return _load_memories(path)


def _read_json_entries(path: Path, pattern: re.Pattern[str]) -> list[tuple[str, Any]]:
    """ZIP (または単体 JSON) から pattern に合うエントリを読み出す。"""
    results: list[tuple[str, Any]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if not pattern.search(name):
                    continue
                try:
                    with zf.open(name) as raw:
                        results.append((name, json.load(raw)))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    LOGGER.warning("Skipping unreadable entry in Claude export: %s", name)
    else:
        with path.open(encoding="utf-8") as fh:
            results.append((path.name, json.load(fh)))
    return results


# --- conversations ---------------------------------------------------------

def _load_conversations(path: Path) -> ClaudeExportFile:
    entries = _read_json_entries(path, _CONVERSATIONS_JSON_RE)
    if not entries and not zipfile.is_zipfile(path):
        entries = [(path.name, json.loads(path.read_text(encoding="utf-8")))]
    records: list[ConversationRecord] = []
    for _, data in entries:
        if not isinstance(data, list):
            continue
        for conv in data:
            if isinstance(conv, dict):
                records.append(_conversation_record(conv))
    return ClaudeExportFile(category=CATEGORY_CONVERSATIONS, conversations=records)


def _block_texts(content: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"].strip()
                if text:
                    texts.append(text)
    return texts


def _attachment_note(message: dict[str, Any]) -> str:
    """添付・ファイルの名前だけを印として残す (中身は取り込まない)。"""
    names: list[str] = []
    for key in ("attachments", "files"):
        for item in message.get(key) or []:
            if isinstance(item, dict):
                name = item.get("file_name") or item.get("name")
                if name:
                    names.append(str(name))
    if not names:
        return ""
    return "\n".join(f"【添付: {name}】" for name in names)


def _conversation_record(conv: dict[str, Any]) -> ConversationRecord:
    identifier = str(conv.get("uuid") or conv.get("name") or "(unknown)")
    title = str(conv.get("name") or "").strip() or "(untitled)"
    messages: list[ConversationMessage] = []
    for msg in conv.get("chat_messages") or []:
        if not isinstance(msg, dict):
            continue
        role = "user" if msg.get("sender") == "human" else "assistant"
        texts = _block_texts(msg.get("content"))
        if not texts and isinstance(msg.get("text"), str) and msg["text"].strip():
            texts = [msg["text"].strip()]
        note = _attachment_note(msg)
        if note:
            texts.append(note)
        content = "\n\n".join(texts).strip()
        if not content:
            continue
        messages.append(
            ConversationMessage(
                node_id=str(msg.get("uuid") or len(messages)),
                role=role,
                content=content,
                create_time=_parse_iso(msg.get("created_at")),
                metadata={},
            )
        )
    return ConversationRecord(
        identifier=identifier,
        title=title,
        create_time=_parse_iso(conv.get("created_at")),
        update_time=_parse_iso(conv.get("updated_at")),
        messages=messages,
        conversation_id=str(conv.get("uuid")) if conv.get("uuid") else None,
        default_model_slug=None,
    )


# --- claude_code_cloud -----------------------------------------------------

_TRANSCRIPTS_RE = re.compile(r"(^|/)channel_session_transcripts\.json$")
_CHANNEL_MESSAGES_RE = re.compile(r"(^|/)channels/messages\.json$")
_CHANNELS_RE = re.compile(r"(^|/)channels/channels\.json$")


def _unwrap_cloud(data: Any) -> list[Any]:
    """claude_code_cloud の各 JSON は {"claude_code_cloud": [...]} で包まれる。"""
    if isinstance(data, dict):
        inner = data.get("claude_code_cloud")
        if isinstance(inner, list):
            return inner
    if isinstance(data, list):
        return data
    return []


_CLOUD_TRANSCRIPTS = "transcripts"
_CLOUD_CHANNEL_MESSAGES = "channel_messages"
_CLOUD_CHANNELS = "channels"


def _cloud_kind_from_name(name: str) -> Optional[str]:
    if _TRANSCRIPTS_RE.search(name):
        return _CLOUD_TRANSCRIPTS
    if _CHANNEL_MESSAGES_RE.search(name):
        return _CLOUD_CHANNEL_MESSAGES
    if _CHANNELS_RE.search(name):
        return _CLOUD_CHANNELS
    return None


def _cloud_kind_from_items(items: list[dict[str, Any]]) -> Optional[str]:
    """単体 JSON 用: 要素のキーから transcript / チャンネル発言 / チャンネル一覧を見分ける。

    アップロードは一時ファイル名で保存されるので、単体 JSON ではファイル名に頼れない。
    """
    for item in items:
        if "session_id" in item and "role" in item:
            return _CLOUD_TRANSCRIPTS
        if "message_id" in item and "channel_id" in item:
            return _CLOUD_CHANNEL_MESSAGES
    for item in items:
        if "channel_id" in item and ("name" in item or "title" in item):
            return _CLOUD_CHANNELS
    return None


def _load_claude_code(path: Path) -> ClaudeExportFile:
    transcripts: list[dict[str, Any]] = []
    channel_messages: list[dict[str, Any]] = []
    channel_names: dict[str, str] = {}
    is_zip = zipfile.is_zipfile(path)
    for name, data in _read_json_entries(path, re.compile(r"\.json$")):
        items = [i for i in _unwrap_cloud(data) if isinstance(i, dict)]
        kind = _cloud_kind_from_name(name) if is_zip else _cloud_kind_from_items(items)
        if kind == _CLOUD_TRANSCRIPTS:
            transcripts = items
        elif kind == _CLOUD_CHANNEL_MESSAGES:
            channel_messages = items
        elif kind == _CLOUD_CHANNELS:
            for ch in items:
                if ch.get("channel_id"):
                    label = ch.get("name") or ch.get("title")
                    if label:
                        channel_names[str(ch["channel_id"])] = str(label)

    records: list[ConversationRecord] = []
    records.extend(_session_records(transcripts))
    records.extend(_channel_records(channel_messages, channel_names))
    return ClaudeExportFile(category=CATEGORY_CLAUDE_CODE, conversations=records)


def _session_records(items: list[dict[str, Any]]) -> list[ConversationRecord]:
    sessions: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        sid = item.get("session_id")
        if sid:
            sessions.setdefault(str(sid), []).append(item)

    records: list[ConversationRecord] = []
    for sid, session_items in sessions.items():
        session_items.sort(key=lambda i: str(i.get("created_at") or ""))
        messages: list[ConversationMessage] = []
        for item in session_items:
            texts = _block_texts(item.get("content"))
            if not texts:
                continue
            role = "user" if item.get("role") == "user" else "assistant"
            content = "\n\n".join(texts).strip()
            if (
                item.get("continues_previous_item")
                and messages
                and messages[-1].role == role
            ):
                messages[-1].content = f"{messages[-1].content}\n\n{content}"
                continue
            messages.append(
                ConversationMessage(
                    node_id=f"{sid}:{len(messages)}",
                    role=role,
                    content=content,
                    create_time=_parse_iso(item.get("created_at")),
                    metadata={},
                )
            )
        if not messages:
            continue
        first_user = next((m.content for m in messages if m.role == "user"), "")
        title = _shorten(first_user, 50) or f"Claude Code {sid}"
        records.append(
            ConversationRecord(
                identifier=sid,
                title=title,
                create_time=messages[0].create_time,
                update_time=messages[-1].create_time,
                messages=messages,
                conversation_id=sid,
                default_model_slug=None,
            )
        )
    records.sort(key=lambda r: (r.create_time or datetime.min.replace(tzinfo=UTC)))
    return records


def _channel_records(
    items: list[dict[str, Any]], channel_names: dict[str, str]
) -> list[ConversationRecord]:
    channels: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            continue  # notice 等の本文なしは捨てる
        cid = item.get("channel_id")
        if cid:
            channels.setdefault(str(cid), []).append(item)

    records: list[ConversationRecord] = []
    for cid, channel_items in channels.items():
        channel_items.sort(key=lambda i: str(i.get("created_at") or ""))
        messages = [
            ConversationMessage(
                node_id=str(item.get("message_id") or idx),
                role="user" if item.get("author_account_uuid") else "assistant",
                content=item["text"].strip(),
                create_time=_parse_iso(item.get("created_at")),
                metadata={},
            )
            for idx, item in enumerate(channel_items)
        ]
        label = channel_names.get(cid) or f"Claude Code channel {cid}"
        records.append(
            ConversationRecord(
                identifier=f"channel:{cid}",
                title=str(label),
                create_time=messages[0].create_time,
                update_time=messages[-1].create_time,
                messages=messages,
                conversation_id=f"channel:{cid}",
                default_model_slug=None,
            )
        )
    return records


# --- projects --------------------------------------------------------------

_PROJECT_JSON_RE = re.compile(r"(^|/)projects/[^/]+\.json$")


def _load_projects(path: Path) -> ClaudeExportFile:
    entries = _read_json_entries(path, _PROJECT_JSON_RE)
    records: list[ConversationRecord] = []
    instructions: dict[str, str] = {}
    for _, data in entries:
        if not isinstance(data, dict):
            continue
        record = _project_record(data)
        if record is None:
            continue
        records.append(record)
        prompt = str(data.get("prompt_template") or "").strip()
        if prompt:
            instructions[record.identifier] = prompt
    return ClaudeExportFile(
        category=CATEGORY_PROJECTS,
        conversations=records,
        project_instructions=instructions,
    )


def _project_record(data: dict[str, Any]) -> Optional[ConversationRecord]:
    identifier = str(data.get("uuid") or data.get("name") or "").strip()
    if not identifier:
        return None
    title = str(data.get("name") or "").strip() or "(untitled)"
    created = _parse_iso(data.get("created_at"))
    messages: list[ConversationMessage] = []
    prompt = str(data.get("prompt_template") or "").strip()
    if prompt:
        # プロジェクト指示はスレッド冒頭の system メッセージとして残す
        messages.append(
            ConversationMessage(
                node_id=f"{identifier}:instructions",
                role="system",
                content=f"【プロジェクト指示: {title}】\n{prompt}",
                create_time=created,
                metadata={},
            )
        )
    for doc in data.get("docs") or []:
        if not isinstance(doc, dict):
            continue
        content = str(doc.get("content") or "").strip()
        if not content:
            continue
        filename = str(doc.get("filename") or "document")
        messages.append(
            ConversationMessage(
                node_id=str(doc.get("uuid") or f"{identifier}:{len(messages)}"),
                role="system",
                content=f"【プロジェクト資料: {filename}】\n{content}",
                create_time=_parse_iso(doc.get("created_at")) or created,
                metadata={},
            )
        )
    if not messages:
        return None
    return ConversationRecord(
        identifier=identifier,
        title=title,
        create_time=created,
        update_time=_parse_iso(data.get("updated_at")),
        messages=messages,
        conversation_id=identifier,
        default_model_slug=None,
    )


# --- memories --------------------------------------------------------------

_MEMORIES_JSON_RE = re.compile(r"(^|/)memories/[^/]+\.json$")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)


def _load_memories(path: Path) -> ClaudeExportFile:
    entries = _read_json_entries(path, _MEMORIES_JSON_RE)
    if not entries and not zipfile.is_zipfile(path):
        entries = [(path.name, json.loads(path.read_text(encoding="utf-8")))]
    items: list[ClaudeMemoryItem] = []
    for _, data in entries:
        if not isinstance(data, dict):
            continue
        items.extend(_memory_items(data))
    return ClaudeExportFile(category=CATEGORY_MEMORIES, memory_items=items)


def _memory_items(data: dict[str, Any]) -> list[ClaudeMemoryItem]:
    items: list[ClaudeMemoryItem] = []
    conv_memory = str(data.get("conversations_memory") or "").strip()
    if conv_memory:
        items.append(
            ClaudeMemoryItem(
                item_id="conversations_memory",
                kind="conversations_memory",
                title="Claudeの記憶 (会話全体)",
                content=conv_memory,
            )
        )
    project_memories = data.get("project_memories")
    if isinstance(project_memories, dict):
        for project_id, content in sorted(project_memories.items()):
            text = str(content or "").strip()
            if not text:
                continue
            items.append(
                ClaudeMemoryItem(
                    item_id=f"project_memory:{project_id}",
                    kind="project_memory",
                    title=f"Claudeプロジェクト記憶 ({str(project_id)[:8]})",
                    content=text,
                )
            )
    for entry in data.get("memory_files") or []:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        mem_path = str(entry.get("path") or "")
        match = _FRONTMATTER_NAME_RE.search(content)
        if match:
            title = match.group(1).strip()
        else:
            title = Path(mem_path).stem or mem_path or "memory"
        items.append(
            ClaudeMemoryItem(
                item_id=f"memory_file:{mem_path or len(items)}",
                kind="memory_file",
                title=title,
                content=content,
                updated_at=_parse_iso(entry.get("updated_at")),
            )
        )
    return items
