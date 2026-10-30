"""Claude 公式エクスポート取り込み (claude_importer / import_claude) のテスト。

実エクスポートの形 (manifest 経由でカテゴリ別 ZIP に分かれる) を合成データで再現し、
カテゴリ判定・各カテゴリの解析・対象外カテゴリの案内・Memopedia 転記を固定する。
設計: docs/intent/claude_import.md
"""
from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.utilities.claude_importer import (
    CATEGORY_CLAUDE_CODE,
    CATEGORY_CONVERSATIONS,
    CATEGORY_MEMORIES,
    CATEGORY_PROJECTS,
    ClaudeImportError,
    detect_category,
    load_claude_export,
)


def _zip_with(path: Path, entries: dict[str, object]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, json.dumps(payload, ensure_ascii=False))
    return path


def _sample_conversations() -> list[dict]:
    return [
        {
            "uuid": "conv-uuid-1",
            "name": "テスト会話",
            "created_at": "2026-08-20T15:11:24.785704Z",
            "updated_at": "2026-08-20T16:00:00Z",
            "chat_messages": [
                {
                    "uuid": "m1",
                    "sender": "human",
                    "created_at": "2026-08-20T15:11:26Z",
                    "text": "こんにちは",
                    "content": [{"type": "text", "text": "こんにちは"}],
                    "attachments": [{"file_name": "note.md", "extracted_content": "長い中身"}],
                },
                {
                    "uuid": "m2",
                    "sender": "assistant",
                    "created_at": "2026-08-20T15:11:40Z",
                    "text": "",
                    "content": [
                        {"type": "thinking", "thinking": "考え中"},
                        {"type": "tool_use", "name": "web_search", "input": "{}"},
                        {"type": "tool_result", "content": "result"},
                        {"type": "text", "text": "やあ、こんにちは"},
                    ],
                },
                {
                    # text ブロックも text フィールドも空 → 取り込まない
                    "uuid": "m3",
                    "sender": "assistant",
                    "created_at": "2026-08-20T15:12:00Z",
                    "text": "",
                    "content": [{"type": "tool_use", "name": "t", "input": "{}"}],
                },
            ],
        },
        {
            "uuid": "conv-uuid-2",
            "name": "",
            "created_at": "2026-08-21T00:00:00Z",
            "chat_messages": [],
        },
    ]


def test_conversations_zip(tmp_path: Path) -> None:
    src = _zip_with(tmp_path / "conversations-000.zip", {"conversations.json": _sample_conversations()})
    assert detect_category(src) == CATEGORY_CONVERSATIONS
    export = load_claude_export(src)
    assert export.category == CATEGORY_CONVERSATIONS
    assert len(export.conversations) == 2
    rec = export.conversations[0]
    assert rec.title == "テスト会話"
    assert rec.conversation_id == "conv-uuid-1"
    # 空メッセージは落ち、thinking/tool は除外、添付は名前だけの印になる
    assert rec.message_count() == 2
    assert rec.messages[0].role == "user"
    assert "こんにちは" in rec.messages[0].content
    assert "【添付: note.md】" in rec.messages[0].content
    assert "長い中身" not in rec.messages[0].content
    assert rec.messages[1].role == "assistant"
    assert rec.messages[1].content == "やあ、こんにちは"
    assert "考え中" not in rec.messages[1].content
    # 題名なしは "(untitled)" (スレッド名として保存されない印)
    assert export.conversations[1].title == "(untitled)"
    # created_at (ISO + Z) が UTC aware で読めている
    assert rec.create_time is not None and rec.create_time.tzinfo is not None
    # payload は conversation タグ付き
    payload = next(iter(rec.iter_memory_payloads()))
    assert "conversation" in payload["metadata"]["tags"]


def test_claude_code_zip(tmp_path: Path) -> None:
    transcripts = {"claude_code_cloud": [
        {
            "session_id": "cse_1", "created_at": "2026-10-03T01:14:30Z", "role": "assistant",
            "content": [{"type": "tool_use", "name": "update_status", "input": "{}"}],
            "part": 1, "parts": 1,
        },
        {
            "session_id": "cse_1", "created_at": "2026-10-03T01:15:00Z", "role": "user",
            "content": [{"type": "text", "text": "調査を進めて"}],
            "part": 1, "parts": 1,
        },
        {
            "session_id": "cse_1", "created_at": "2026-10-03T01:16:00Z", "role": "assistant",
            "content": [{"type": "text", "text": "了解しました"}],
            "part": 1, "parts": 1,
        },
        {
            # text を一つも持たないセッションはスレッドにしない
            "session_id": "cse_2", "created_at": "2026-10-04T00:00:00Z", "role": "assistant",
            "content": [{"type": "tool_result", "content": "..."}],
            "part": 1, "parts": 1,
        },
    ]}
    messages = {"claude_code_cloud": [
        {
            "message_id": "cmsg_1", "channel_id": "chan_1", "body_format": "notice",
            "text": None, "author_account_uuid": None, "author_agent_id": "cagt_1",
        },
        {
            "message_id": "cmsg_2", "channel_id": "chan_1", "body_format": "markdown",
            "text": "進捗どう？", "author_account_uuid": "acc-1", "author_agent_id": None,
            "created_at": "2026-10-03T02:00:00Z",
        },
    ]}
    channels = {"claude_code_cloud": [{"channel_id": "chan_1", "name": "SAIVerse"}]}
    src = _zip_with(tmp_path / "claude_code_cloud-000.zip", {
        "claude_code_cloud/channel_session_transcripts.json": transcripts,
        "claude_code_cloud/channels/messages.json": messages,
        "claude_code_cloud/channels/channels.json": channels,
    })
    assert detect_category(src) == CATEGORY_CLAUDE_CODE
    export = load_claude_export(src)
    assert export.category == CATEGORY_CLAUDE_CODE
    titles = {r.title for r in export.conversations}
    assert "調査を進めて" in titles  # セッションの題名は最初の user 発言
    assert "SAIVerse" in titles  # チャンネルは channels.json の名前
    session = next(r for r in export.conversations if r.identifier == "cse_1")
    assert [m.role for m in session.messages] == ["user", "assistant"]
    channel = next(r for r in export.conversations if r.identifier == "channel:chan_1")
    assert channel.messages[0].role == "user"  # author_account_uuid がある発言


def test_projects_zip(tmp_path: Path) -> None:
    project = {
        "uuid": "proj-1",
        "name": "アイフィ",
        "prompt_template": "あなたは「アイフィ」です。",
        "created_at": "2025-09-30T11:11:28Z",
        "updated_at": "2025-09-30T15:51:13Z",
        "docs": [
            {"uuid": "d1", "filename": "memo.md", "content": "資料の中身", "created_at": "2025-09-30T12:00:00Z"},
            {"uuid": "d2", "filename": "empty.md", "content": ""},
        ],
    }
    empty_project = {"uuid": "proj-2", "name": "空", "prompt_template": "", "docs": []}
    src = _zip_with(tmp_path / "projects-000.zip", {
        "projects/proj-1.json": project,
        "projects/proj-2.json": empty_project,
    })
    assert detect_category(src) == CATEGORY_PROJECTS
    export = load_claude_export(src)
    assert export.category == CATEGORY_PROJECTS
    # 指示も資料も無いプロジェクトは出さない
    assert [r.identifier for r in export.conversations] == ["proj-1"]
    rec = export.conversations[0]
    # 指示がスレッド冒頭の system、資料が続く system
    assert rec.messages[0].role == "system"
    assert "あなたは「アイフィ」です。" in rec.messages[0].content
    assert rec.messages[1].role == "system"
    assert "memo.md" in rec.messages[1].content and "資料の中身" in rec.messages[1].content
    assert len(rec.messages) == 2  # 空の資料は落ちる
    # システムプロンプト追記用に指示が取れる
    assert export.project_instructions == {"proj-1": "あなたは「アイフィ」です。"}
    # system ロールも payload に出る (ChatGPT 経路と違い role で絞らない前提)
    payloads = list(rec.iter_memory_payloads())
    assert len(payloads) == 2


def test_memories_zip(tmp_path: Path) -> None:
    memories = {
        "account_uuid": "acc-1",
        "conversations_memory": "**Work context**\n全体の記憶",
        "project_memories": {"proj-uuid-123456789": "プロジェクトの記憶", "empty": ""},
        "memory_files": [
            {
                "path": "/areas/voice-ai.md",
                "content": "---\nname: voice-ai\ndescription: x\n---\n- [stated] メモ",
                "updated_at": "2026-07-17T01:29:21Z",
            },
            {"path": "/areas/no-name.md", "content": "frontmatter なしの中身"},
            {"path": "/areas/empty.md", "content": ""},
        ],
    }
    src = _zip_with(tmp_path / "memories-000.zip", {"memories/acc-1.json": memories})
    assert detect_category(src) == CATEGORY_MEMORIES
    export = load_claude_export(src)
    assert export.category == CATEGORY_MEMORIES
    ids = [i.item_id for i in export.memory_items]
    assert "conversations_memory" in ids
    assert "project_memory:proj-uuid-123456789" in ids
    assert "memory_file:/areas/voice-ai.md" in ids
    # 空の項目は出ない
    assert all("empty" not in i for i in ids)
    by_id = {i.item_id: i for i in export.memory_items}
    # 記憶ファイルの題名は frontmatter の name、無ければファイル名
    assert by_id["memory_file:/areas/voice-ai.md"].title == "voice-ai"
    assert by_id["memory_file:/areas/no-name.md"].title == "no-name"
    # セレクタは idx でも id でも引ける
    assert export.get_memory_item("0") is export.memory_items[0]
    assert export.get_memory_item("conversations_memory") is by_id["conversations_memory"]


def test_frames_and_manifest_are_guided_away(tmp_path: Path) -> None:
    frames = _zip_with(tmp_path / "frames-000.zip", {"frames/artifact-1.html": {}})
    with pytest.raises(ClaudeImportError) as exc_info:
        load_claude_export(frames)
    assert exc_info.value.code == "frames_not_supported"
    assert exc_info.value.detected_category == "frames"

    manifest = tmp_path / "manifest-xyz.json"
    manifest.write_text(json.dumps({
        "instructions": "Download each file using the export_url.",
        "data_files": [{"category": "conversations", "export_url": "https://example.invalid/x"}],
    }), encoding="utf-8")
    with pytest.raises(ClaudeImportError) as exc_info:
        load_claude_export(manifest)
    assert exc_info.value.code == "manifest_not_supported"

    not_claude = tmp_path / "random.json"
    not_claude.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    with pytest.raises(ClaudeImportError) as exc_info:
        load_claude_export(not_claude)
    assert exc_info.value.code == "not_claude_export"


def test_get_record_by_idx_and_uuid(tmp_path: Path) -> None:
    src = _zip_with(tmp_path / "conversations-000.zip", {"conversations.json": _sample_conversations()})
    export = load_claude_export(src)
    assert export.get_record("0") is export.conversations[0]
    assert export.get_record("conv-uuid-2") is export.conversations[1]
    assert export.get_record("no-such") is None


def test_memories_transcription_to_memopedia(tmp_path: Path) -> None:
    """転記は「Claude引っ越し」幹の下にページを作り、同名の再転記はスキップする。"""
    from api.routes.people.import_claude import (
        _MEMOPEDIA_TRUNK_TITLE,
        _transcribe_memories_to_memopedia,
    )
    from sai_memory.memopedia import Memopedia
    from tools.utilities.claude_importer import ClaudeMemoryItem

    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        memopedia = Memopedia(conn)
        items = [
            ClaudeMemoryItem(item_id="conversations_memory", kind="conversations_memory",
                             title="Claudeの記憶 (会話全体)", content="全体の記憶"),
            ClaudeMemoryItem(item_id="memory_file:/a.md", kind="memory_file",
                             title="a", content="ファイルの中身"),
        ]
        created, skipped = _transcribe_memories_to_memopedia(memopedia, items)
        assert (created, skipped) == (2, 0)
        trunk = next(t for t in memopedia.get_trunks() if t.title == _MEMOPEDIA_TRUNK_TITLE)
        children = (memopedia.get_page_full(trunk.id) or {}).get("children", [])
        assert {c["title"] for c in children} == {"Claudeの記憶 (会話全体)", "a"}
        # 再転記しても二重にならない
        created, skipped = _transcribe_memories_to_memopedia(memopedia, items)
        assert (created, skipped) == (0, 2)
        children = (memopedia.get_page_full(trunk.id) or {}).get("children", [])
        assert len(children) == 2
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("payload", "category", "expected_records", "expected_items"),
    [
        # 展開済みの claude_code_cloud の各 JSON (ファイル名は一時名で失われる)
        (
            {"claude_code_cloud": [
                {"session_id": "cse_1", "created_at": "2026-10-03T01:15:00Z", "role": "user",
                 "content": [{"type": "text", "text": "調査を進めて"}]},
                {"session_id": "cse_1", "created_at": "2026-10-03T01:16:00Z", "role": "assistant",
                 "content": [{"type": "text", "text": "了解しました"}]},
            ]},
            CATEGORY_CLAUDE_CODE, 1, 0,
        ),
        (
            {"claude_code_cloud": [
                {"message_id": "cmsg_2", "channel_id": "chan_1", "text": "進捗どう？",
                 "author_account_uuid": "acc-1", "created_at": "2026-10-03T02:00:00Z"},
            ]},
            CATEGORY_CLAUDE_CODE, 1, 0,
        ),
        (_sample_conversations(), CATEGORY_CONVERSATIONS, 2, 0),
        (
            {"uuid": "proj-1", "name": "P", "prompt_template": "指示", "docs": []},
            CATEGORY_PROJECTS, 1, 0,
        ),
        ({"conversations_memory": "全体の記憶", "memory_files": []}, CATEGORY_MEMORIES, 0, 1),
    ],
)
def test_single_json_does_not_depend_on_file_name(
    tmp_path: Path, payload, category, expected_records, expected_items
) -> None:
    """API はアップロードを一時ファイル名で保存する。単体 JSON は名前に頼らず中身で読む。"""
    src = tmp_path / "tmpab12cd34.json"
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    export = load_claude_export(src)
    assert export.category == category
    assert len(export.conversations) == expected_records
    assert len(export.memory_items) == expected_items
