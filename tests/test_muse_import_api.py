"""Muse 公式エクスポート取り込み API (import_muse) のテスト。

隔離した偽 manager と、test_muse_importer が生成する架空のフィクスチャ ZIP だけを
使う。実データや本番ペルソナには触れない。設計: docs/intent/muse_export_import.md
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.deps import get_manager
from api.routes.people import import_muse
from sai_memory.memory.storage import SYSTEM_NOTICE_KEY
from saiverse_memory.adapter import EMBEDDING_DEFERRED
from tests.test_muse_importer import FIXTURE_ZIP
from tools.utilities.muse_importer import MuseExport

PERSONA_ID = "test_persona_muse"


@pytest.fixture(autouse=True)
def _clean_muse_state():
    """preview が残した一時ファイルとモジュール状態をテストごとに片付ける。"""
    import_muse._muse_export_cache.clear()
    import_muse._muse_import_status.clear()
    import_muse._muse_file_cache.clear()
    yield
    for cached in list(import_muse._muse_export_cache.values()):
        tmp_path = cached.get("tmp_path")
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()
    import_muse._muse_export_cache.clear()
    import_muse._muse_import_status.clear()
    import_muse._muse_file_cache.clear()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(import_muse.router, prefix="/api/people")
    manager = SimpleNamespace(personas={PERSONA_ID: object()})
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as test_client:
        yield test_client


def _post_preview(client: TestClient):
    return client.post(
        f"/api/people/{PERSONA_ID}/import/muse/preview",
        files={"file": ("EYI_Package_test.zip", FIXTURE_ZIP.read_bytes(), "application/zip")},
    )


def test_preview_returns_conversation_and_workspace_metadata(client):
    res = _post_preview(client)
    assert res.status_code == 200

    body = res.json()
    assert body["total_count"] == 1
    assert body["workspace_file_count"] == 3
    assert body["exported_at"] == "2027-01-15T00:00:00Z"
    assert len(body["conversations"]) == 1

    conversation = body["conversations"][0]
    assert conversation["idx"] == 0
    assert conversation["message_count"] == 14
    assert conversation["title"] == "Conversation with Muse AI_01-15-2027_0000000001"
    assert conversation["preview"].startswith("ルナ、はじめまして。山田花子です。")
    assert body["cache_key"] in import_muse._muse_export_cache


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("notes.txt", b"This is not a Muse export."),
        ("broken.zip", b"not a zip archive"),
    ],
)
def test_preview_rejects_non_muse_files(client, filename, payload):
    res = client.post(
        f"/api/people/{PERSONA_ID}/import/muse/preview",
        files={"file": (filename, payload, "application/octet-stream")},
    )
    assert res.status_code == 400


def test_resolve_records_accepts_identifier_and_numeric_idx():
    export = MuseExport(FIXTURE_ZIP)
    record = export.conversations[0]
    assert import_muse._resolve_records(export, ["0"]) == [record]
    assert import_muse._resolve_records(export, [record.identifier]) == [record]
    assert import_muse._resolve_records(export, ["no-such", "0"]) == [record]


def test_run_muse_import_task_saves_payloads_and_hands_off_embedding(client, monkeypatch):
    preview = _post_preview(client).json()
    cache_key = preview["cache_key"]

    class FakeAdapter:
        def __init__(self, persona_id: str):
            self.persona_id = persona_id
            self.payloads = []
            self.titles = []
            self.closed = False

        def append_persona_message(self, payload, thread_suffix=None):
            self.payloads.append((payload, thread_suffix))
            return f"msg-{len(self.payloads)}"

        def set_thread_title(self, thread_suffix, title):
            self.titles.append((thread_suffix, title))

        def close(self):
            self.closed = True

    created = []

    def fake_adapter(persona_id: str):
        adapter = FakeAdapter(persona_id)
        created.append(adapter)
        return adapter

    monkeypatch.setattr("saiverse_memory.SAIMemoryAdapter", fake_adapter)
    handed_off = []
    monkeypatch.setattr(
        import_muse,
        "_launch_import_embedding",
        lambda persona_id, adapter, count: handed_off.append((persona_id, adapter, count)) or True,
    )

    import_muse._run_muse_import_task(PERSONA_ID, cache_key, ["0"], False)

    status = import_muse._muse_import_status[PERSONA_ID]
    assert status["success"] is True
    assert status["conversations"] == 1
    assert status["messages"] == 15
    assert status["embedding"] is True

    adapter = created[0]
    assert len(adapter.payloads) == 15
    assert all(payload["embedding_chunks"] == EMBEDDING_DEFERRED for payload, _ in adapter.payloads)
    # 先頭は CLI と同じ出典見出し (システム通知、系統名 muse_import)。
    header, _header_suffix = adapter.payloads[0]
    assert header["role"] == "system"
    assert header["metadata"][SYSTEM_NOTICE_KEY] == "muse_import"
    assert header["content"].startswith("[Imported Muse conversation ")
    assert adapter.titles == [(
        "EYI Package_01-15-2027_0000000001",
        "Conversation with Muse AI_01-15-2027_0000000001",
    )]
    assert handed_off == [(PERSONA_ID, adapter, 15)]
    # handed_off のときは背景スレッドが adapter を引き取るため、ここでは閉じない。
    assert adapter.closed is False
    assert cache_key not in import_muse._muse_export_cache


def test_import_reserves_running_state_before_task_starts(client, monkeypatch):
    """受理した時点で実行中を予約し、タスク開始前の二重要求を 409 で弾く。"""
    preview = _post_preview(client).json()
    cache_key = preview["cache_key"]

    # 背景タスクを無力化し、「タスクがまだ状態を立てていない」状況を作る。
    monkeypatch.setattr(import_muse, "_run_muse_import_task", lambda *a, **k: None)

    body = {"cache_key": cache_key, "conversation_ids": ["0"], "skip_embedding": True}
    res = client.post(f"/api/people/{PERSONA_ID}/import/muse", json=body)
    assert res.status_code == 200
    # タスクは何もしていないのに、予約によって実行中になっている。
    assert import_muse._muse_import_status[PERSONA_ID]["running"] is True

    res = client.post(f"/api/people/{PERSONA_ID}/import/muse", json=body)
    assert res.status_code == 409

# ---------------------------------------------------------------------------
# 後段: SOUL / IDENTITY / MEMORY / AGENTS の個別ファイル取り込み
# ---------------------------------------------------------------------------

# 架空の Muse ペルソナ「ルナ」のファイル。実在の人物・ペルソナとは無関係。
SOUL_TEXT = """ルナは、星見市の小さな図書館に住む架空のアシスタント。
わからないことは「わからない」と言い、相手の話を最後まで聞いてから答える。
難しい話も、星空を見上げるような落ち着いた言葉で伝える。"""

IDENTITY_TEXT = """名前: ルナ
絵文字: 🌙
一人称: わたし
出身: 星見市の架空の図書館"""

MEMORY_TEXT = """ルナは、星見市の図書館で働く利用者を支えるために生まれた架空の存在。

# 好きなもの
- 星空と、静かな夜の読書
- 季節の変わり目に図書館の窓を開けること

# 苦手なもの
- 急かされること
- 約束を黙って破ること

# 大切にしていること
- 相手の言葉をそのまま受け取る
- わからないことをわからないと言う"""

AGENTS_TEXT = """# ルナの作業ルール（架空）

- 作業を始める前に、今日の予定を短く確認する。
- 判断に迷ったら、利用者に一度だけ質問する。
- 終わった作業は、図書館の記録ノートに残す。"""

AGENTS_TEXT_UPDATED = """# ルナの作業ルール（架空・改訂）

- 作業を始める前に、今日の予定と締め切りを確認する。
- 判断に迷ったら、利用者に選択肢を二つ示して尋ねる。
- 終わった作業と未完了の作業を、図書館の記録ノートに分けて残す。"""


class DummyEmbedder:
    """テストで実モデルのダウンロードを起こさないための差し替え。"""

    def __init__(self, *args, **kwargs):
        self.model_name = "dummy"

    def embed(self, texts, *, is_query=False):
        return [[0.0] * 3 for _ in texts]


class FakeManager:
    """Muse ファイル取り込みが触る manager の最小の偽物。"""

    def __init__(self):
        self.personas = {PERSONA_ID: object()}
        self.updated = {}
        self._system_prompt = "既存のプロンプト"

    def get_ai_details(self, ai_id: str):
        if ai_id != PERSONA_ID:
            return None
        return {
            "SYSTEMPROMPT": self._system_prompt,
            "AINAME": "ルナ",
            "DESCRIPTION": "架空の図書館アシスタント",
            "HOME_CITYID": 1,
            "AUTONOMY_ENABLED": True,
            "AVATAR_IMAGE": None,
            "APPEARANCE_IMAGE_PATH": None,
        }

    def update_ai(self, **kwargs):
        self.updated = kwargs
        self._system_prompt = kwargs.get("system_prompt", self._system_prompt)
        return "ok"


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """実 SAIMemoryAdapter を組み立てるテストの置き場と埋め込みを隔離する。"""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    monkeypatch.setenv("SAIMEMORY_BACKUP_ON_START", "0")
    monkeypatch.setattr("saiverse_memory.adapter.Embedder", DummyEmbedder)


@pytest.fixture()
def file_manager():
    return FakeManager()


@pytest.fixture()
def file_client(isolated_home, file_manager):
    app = FastAPI()
    app.include_router(import_muse.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: file_manager
    with TestClient(app) as test_client:
        yield test_client


def _post_files_preview(
    client: TestClient,
    *,
    memory_text: str = MEMORY_TEXT,
    agents_text: str = AGENTS_TEXT,
):
    files = [
        ("files", ("SOUL.md", SOUL_TEXT.encode("utf-8"), "text/markdown")),
        ("files", ("MEMORY.md", memory_text.encode("utf-8"), "text/markdown")),
        ("files", ("AGENTS.md", agents_text.encode("utf-8"), "text/markdown")),
        ("files", ("IDENTITY.md", IDENTITY_TEXT.encode("utf-8"), "text/markdown")),
    ]
    return client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files/preview",
        files=files,
    )


def test_split_memory_items_headings_and_preamble():
    items = import_muse._split_memory_items(MEMORY_TEXT)

    assert len(items) == 4
    assert items[0]["title"] is None
    assert "星見市の図書館で働く" in items[0]["content"]
    assert [item["title"] for item in items[1:]] == ["好きなもの", "苦手なもの", "大切にしていること"]
    assert items[1]["content"].startswith("# 好きなもの")
    assert "- 星空と、静かな夜の読書" in items[1]["content"]


def test_split_memory_items_fallbacks():
    paragraphs = import_muse._split_memory_items("第一段落。\n\n第二段落。\n\n第三段落。")
    assert len(paragraphs) == 3
    assert all(item["title"] is None for item in paragraphs)

    lines = import_muse._split_memory_items("一行目\n二行目\n三行目")
    assert len(lines) == 3
    assert all(item["title"] is None for item in lines)

    assert import_muse._split_memory_items("") == []
    assert import_muse._split_memory_items("   \n  \n") == []

    one_heading = import_muse._split_memory_items("# 唯一の見出し\n\n本文だけ")
    assert len(one_heading) == 2
    assert all(item["title"] is None for item in one_heading)


def test_files_preview_detects_kinds_and_counts(client):
    files = [
        ("files", ("SOUL.md", SOUL_TEXT.encode("utf-8"), "text/markdown")),
        ("files", ("MEMORY.md", MEMORY_TEXT.encode("utf-8"), "text/markdown")),
        ("files", ("AGENTS.md", AGENTS_TEXT.encode("utf-8"), "text/markdown")),
        ("files", ("IDENTITY.md", IDENTITY_TEXT.encode("utf-8"), "text/markdown")),
        ("files", ("notes.txt", b"not a Muse file", "text/plain")),
    ]
    res = client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files/preview",
        files=files,
    )
    assert res.status_code == 200

    body = res.json()
    by_name = {entry["filename"]: entry for entry in body["files"]}
    assert body["total_count"] == 4
    assert by_name["SOUL.md"]["kind"] == "soul"
    assert by_name["SOUL.md"]["destination"] == "system_prompt"
    assert by_name["IDENTITY.md"]["kind"] == "identity"
    assert by_name["IDENTITY.md"]["destination"] == "system_prompt"
    assert by_name["MEMORY.md"]["kind"] == "memory"
    assert by_name["MEMORY.md"]["destination"] == "core_memory"
    assert by_name["MEMORY.md"]["item_count"] == 4
    assert by_name["AGENTS.md"]["kind"] == "agents"
    assert by_name["AGENTS.md"]["destination"] == "memopedia"
    assert by_name["notes.txt"]["kind"] is None
    assert by_name["notes.txt"]["destination"] is None
    assert by_name["notes.txt"]["item_count"] == 0
    assert body["cache_key"] in import_muse._muse_file_cache


def test_files_preview_no_recognized_files(client):
    res = client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files/preview",
        files=[("files", ("notes.txt", b"not a Muse file", "text/plain"))],
    )
    assert res.status_code == 200

    body = res.json()
    assert body["cache_key"] == ""
    assert body["total_count"] == 0
    assert body["files"][0]["kind"] is None


def test_files_import_end_to_end(file_client, file_manager):
    preview = _post_files_preview(file_client)
    assert preview.status_code == 200
    cache_key = preview.json()["cache_key"]
    assert cache_key in import_muse._muse_file_cache

    res = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": []},
    )
    assert res.status_code == 200

    body = res.json()
    assert body["success"] is True
    assert body["core_created"] == 4
    assert body["system_prompt_appended"] == ["soul", "identity"]
    assert body["memopedia_action"] == "created"
    assert body["trunk_title"] == import_muse._MEMOPEDIA_TRUNK_TITLE

    expected_prompt = (
        "既存のプロンプト\n\n" + SOUL_TEXT.strip() + "\n\n" + IDENTITY_TEXT.strip()
    )
    assert file_manager.updated["system_prompt"] == expected_prompt

    from sai_memory.core_memory import list_core_memories
    from sai_memory.memopedia import Memopedia
    from saiverse_memory import SAIMemoryAdapter

    adapter = SAIMemoryAdapter(PERSONA_ID)
    try:
        assert len(list_core_memories(adapter.conn)) == 4
        page = Memopedia(adapter.conn).find_by_title(import_muse._AGENTS_PAGE_TITLE)
        assert page is not None
        assert page.content == AGENTS_TEXT.strip()
    finally:
        adapter.close()

    res_again = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": []},
    )
    assert res_again.status_code == 400


def test_files_import_second_run_updates_agents_and_skips_core(file_client):
    first_preview = _post_files_preview(file_client)
    assert first_preview.status_code == 200
    first_res = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": first_preview.json()["cache_key"], "kinds": []},
    )
    assert first_res.status_code == 200

    second_preview = _post_files_preview(file_client, agents_text=AGENTS_TEXT_UPDATED)
    assert second_preview.status_code == 200
    second_res = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": second_preview.json()["cache_key"], "kinds": []},
    )
    assert second_res.status_code == 200

    body = second_res.json()
    assert body["memopedia_action"] == "updated"
    assert body["core_created"] == 0
    assert body["core_skipped"] == 4

    from sai_memory.memopedia import Memopedia
    from saiverse_memory import SAIMemoryAdapter

    adapter = SAIMemoryAdapter(PERSONA_ID)
    try:
        page = Memopedia(adapter.conn).find_by_title(import_muse._AGENTS_PAGE_TITLE)
        assert page is not None
        assert page.content == AGENTS_TEXT_UPDATED.strip()
    finally:
        adapter.close()


def test_files_import_requires_valid_cache(file_client):
    res = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": "no-such-cache", "kinds": []},
    )
    assert res.status_code == 400


def test_files_import_retry_after_memory_failure_does_not_duplicate_prompt(
    file_client, file_manager, monkeypatch
):
    """記憶側の失敗後に同じキャッシュで再試行してもプロンプトは二重追記されない。"""
    preview = _post_files_preview(file_client)
    assert preview.status_code == 200
    cache_key = preview.json()["cache_key"]

    original = import_muse._add_core_memory_items
    calls = {"n": 0}

    def flaky_add(conn, contents):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated memory failure")
        return original(conn, contents)

    monkeypatch.setattr(import_muse, "_add_core_memory_items", flaky_add)

    first = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": []},
    )
    assert first.status_code == 500
    # プロンプト追記は永続化済みで、再試行のためにキャッシュは残っている。
    assert file_manager._system_prompt.count(SOUL_TEXT.strip()) == 1
    assert cache_key in import_muse._muse_file_cache

    second = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": []},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["success"] is True
    assert body["system_prompt_appended"] == ["soul", "identity"]
    assert body["core_created"] == 4
    assert file_manager._system_prompt.count(SOUL_TEXT.strip()) == 1
    assert file_manager._system_prompt.count(IDENTITY_TEXT.strip()) == 1


def test_files_import_retry_with_changed_selection_appends_new_kind(
    file_client, file_manager, monkeypatch
):
    """失敗後の再試行で選択を変更しても、未適用の種別は取りこぼさない。"""
    preview = _post_files_preview(file_client)
    assert preview.status_code == 200
    cache_key = preview.json()["cache_key"]

    original = import_muse._add_core_memory_items
    calls = {"n": 0}

    def flaky_add(conn, contents):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated memory failure")
        return original(conn, contents)

    monkeypatch.setattr(import_muse, "_add_core_memory_items", flaky_add)

    # 初回は SOUL + MEMORY だけを選び、記憶側で失敗させる。
    first = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": ["soul", "memory"]},
    )
    assert first.status_code == 500
    assert file_manager._system_prompt.count(SOUL_TEXT.strip()) == 1
    assert IDENTITY_TEXT.strip() not in file_manager._system_prompt

    # 再試行では SOUL を外して IDENTITY を追加する。
    second = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": cache_key, "kinds": ["identity", "memory"]},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["success"] is True
    assert body["system_prompt_appended"] == ["identity"]
    assert body["core_created"] == 4
    assert file_manager._system_prompt.count(SOUL_TEXT.strip()) == 1
    assert file_manager._system_prompt.count(IDENTITY_TEXT.strip()) == 1


SOUL_TEXT_WITH_NOTE = """> **About this file.** This is a fictional persona note.
> It explains how the file works. This note is not part of the file.

# SOUL.md

ルナは星見市の図書館に住む架空のアシスタント。"""

IDENTITY_TEXT_WITH_NOTE = """> **About this file.** Fictional identity note.
> This note is not part of the file.

名前: ルナ
出身: 星見市の架空の図書館"""

MEMORY_TEXT_WITH_NOTE = """> **About this file.** This is a fictional long-term memory note.
> This note is not part of the file.

# MEMORY.md
<!-- Fictional curated memory: how this file is organized. -->

## 好きなもの
- 星空と、静かな夜の読書

## 苦手なもの
- 急かされること"""

AGENTS_TEXT_WITH_COMMENT = """# ルナの作業ルール（架空）

<!-- Fictional comment about the file format. -->
- 作業を始める前に、今日の予定を短く確認する。"""


def test_files_import_strips_muse_format_notes(file_client, file_manager):
    """Muse のファイル形式の説明文は取り込まず、本文だけを取り込む。"""
    files = [
        ("files", ("SOUL.md", SOUL_TEXT_WITH_NOTE.encode("utf-8"), "text/markdown")),
        ("files", ("IDENTITY.md", IDENTITY_TEXT_WITH_NOTE.encode("utf-8"), "text/markdown")),
        ("files", ("MEMORY.md", MEMORY_TEXT_WITH_NOTE.encode("utf-8"), "text/markdown")),
        ("files", ("AGENTS.md", AGENTS_TEXT_WITH_COMMENT.encode("utf-8"), "text/markdown")),
    ]
    preview = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files/preview",
        files=files,
    )
    assert preview.status_code == 200
    preview_body = preview.json()
    memory_entry = next(f for f in preview_body["files"] if f["kind"] == "memory")
    # 注記の前文と、見出しだけの H1 節は項目にならない。
    assert memory_entry["item_count"] == 2

    res = file_client.post(
        f"/api/people/{PERSONA_ID}/import/muse/files",
        json={"cache_key": preview_body["cache_key"], "kinds": []},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["core_created"] == 2

    prompt = file_manager._system_prompt
    assert "ルナは星見市の図書館に住む架空のアシスタント" in prompt
    assert "名前: ルナ" in prompt
    assert "About this file" not in prompt
    assert "This note is not part of the file" not in prompt

    from sai_memory.core_memory import list_core_memories
    from sai_memory.memopedia import Memopedia
    from saiverse_memory import SAIMemoryAdapter

    adapter = SAIMemoryAdapter(PERSONA_ID)
    try:
        items = list_core_memories(adapter.conn)
        assert len(items) == 2
        for item in items:
            content = item["content"] if isinstance(item, dict) else str(item)
            assert "About this file" not in content
            assert "Fictional curated memory" not in content
        page = Memopedia(adapter.conn).find_by_title(import_muse._AGENTS_PAGE_TITLE)
        assert page is not None
        assert "Fictional comment" not in page.content
        assert "今日の予定を短く確認する" in page.content
    finally:
        adapter.close()
