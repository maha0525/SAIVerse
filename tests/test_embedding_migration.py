"""埋め込みモデル移行の機構 (docs/intent/embedding_gemma2_migration.md §2) のテスト。

押さえること:

- 起動時の検出が 4 系統 (メッセージ / Chronicle / Memopedia ページ / Fragment)
  のどれに古い長さのベクトルがあっても旗を立てる。ベクトルが一つも無い新品の
  memory.db では旗を立てず、いまのモデルを記録する。
- 再計算 API のジョブが 3 系統の古いベクトルを消してその場で作り直し、
  メッセージも作り直し、``embed_model`` を記録し、そのペルソナの警告を下ろす。
- CLI も同じ範囲を直し、``embed_model`` を記録する。小さい次元への再計算は拒む。
- 会話ログのインポートは、取り込み中は埋め込まず、取り込みの完了を報告して
  から裏で取り込んだ分だけを埋める。始まるときに前回の埋め込みを待って前回の
  数字を消し、失敗は状態に残す。
- 再計算の最終確認は全行を見る。埋め込みの計算中に本文が変わった行は保存しない。
- 再計算の受付は印をその場で付けて二重受付を断る。読み込みに失敗していた
  埋め込み器は立て直してから警告を下ろし、立て直せなければ警告を残す。
- 記録されたモデルの名札が現行と違えば、次元が同じでも全行を作り直す
  (API も CLI も)。名札が同じなら従来どおり古い長さの行だけを直す。
- 再計算は消す前に「移行中」の印を置き、完走で消す。途中で死んで印が残った
  memory.db は、ベクトルが一つも無くても起動時に旗を立てる。

Embedder は本物を使わず、決まった長さのベクトルを返す偽物に差し替える。
DB は一時ディレクトリの SQLite。LLM は呼ばない。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import create_entry
from sai_memory.config import EMBED_DIM, EMBED_MODEL_ID, load_settings
from sai_memory.embedding_migration import MIGRATION_IN_PROGRESS_KEY
from sai_memory.memopedia import init_memopedia_tables
from sai_memory.memopedia.storage import create_fragment, create_page
from sai_memory.memory.storage import (
    add_message,
    get_embed_metadata,
    get_or_create_thread,
    init_db,
    replace_message_embeddings,
    set_embed_metadata,
)
from sai_memory.unified_recall import (
    store_chronicle_embedding,
    store_fragment_embedding,
    store_memopedia_embedding,
)

OLD_DIM = 384
TABLES = (
    "message_embeddings",
    "arasuji_embeddings",
    "memopedia_embeddings",
    "memopedia_fragment_embeddings",
)


class FakeEmbedder:
    """EMBED_DIM の長さのベクトルを返す偽の Embedder。"""

    model_name = EMBED_MODEL_ID
    embedding_size = EMBED_DIM

    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    def embed(self, texts, *, is_query=False):
        self.calls += 1
        return [[float((len(t) % 7) + 1)] + [0.0] * (EMBED_DIM - 1) for t in texts]


def _vec(dim: int) -> list:
    return [0.1] * dim


def _open(db_path: Path) -> sqlite3.Connection:
    conn = init_db(str(db_path), check_same_thread=False)
    init_memopedia_tables(conn)
    init_arasuji_tables(conn)
    return conn


def _seed(conn: sqlite3.Connection, dim: int) -> dict:
    """4 系統に 1 件ずつ (メッセージは 2 件) 原文と、長さ dim のベクトルを入れる。"""
    get_or_create_thread(conn, "p:__persona__", "p")
    m1 = add_message(conn, thread_id="p:__persona__", role="user",
                     content="猫ロボットの話をしよう", resource_id="p", created_at=1000)
    m2 = add_message(conn, thread_id="p:__persona__", role="assistant",
                     content="いいね、耳の形から決めよう", resource_id="p", created_at=1001)
    replace_message_embeddings(conn, m1, [_vec(dim)])
    replace_message_embeddings(conn, m2, [_vec(dim), _vec(dim)])
    create_entry(conn, level=1, content="猫ロボットの設計を始めた日", source_ids=[],
                 start_time=1000, end_time=2000, source_count=1, message_count=2,
                 entry_id="chr1")
    store_chronicle_embedding(conn, "chr1", _vec(dim))
    page = create_page(conn, parent_id="root_terms", title="猫ロボット",
                       summary="まはーと設計しているロボット", content="", category="terms")
    store_memopedia_embedding(conn, page.id, _vec(dim))
    frag = create_fragment(conn, entity_id=page.id, content="耳は三角形にした",
                           source_date="2026-10-12")
    store_fragment_embedding(conn, frag.id, _vec(dim))
    return {"messages": [m1, m2], "chronicle": "chr1", "page": page.id, "fragment": frag.id}


def _dims(conn: sqlite3.Connection, table: str) -> set:
    return {len(json.loads(v)) for (v,) in conn.execute(f"SELECT vector FROM {table}")}


def _texts(conn: sqlite3.Connection) -> tuple:
    return (
        conn.execute("SELECT id, content FROM messages ORDER BY id").fetchall(),
        conn.execute("SELECT id, content FROM arasuji_entries ORDER BY id").fetchall(),
        conn.execute("SELECT id, title, summary FROM memopedia_pages ORDER BY id").fetchall(),
        conn.execute("SELECT id, content FROM memopedia_fragments ORDER BY id").fetchall(),
    )


def _check(conn: sqlite3.Connection) -> bool:
    """adapter の起動時判定だけを、与えた接続に対して走らせて旗を返す。"""
    from saiverse_memory.adapter import SAIMemoryAdapter

    adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
    adapter.conn = conn
    adapter.persona_id = "p"
    adapter.settings = load_settings()
    adapter.embed_model_changed = False
    adapter._check_embed_model_change()
    return adapter.embed_model_changed


# ---------------------------------------------------------------------------
# 起動時の検出
# ---------------------------------------------------------------------------

def test_fresh_db_records_model_without_flag(tmp_path):
    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
        assert _check(conn) is False
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
    finally:
        conn.close()


def test_current_model_and_dims_do_not_flag(tmp_path):
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        assert _check(conn) is False
    finally:
        conn.close()


@pytest.mark.parametrize("recorded", [None, "intfloat/multilingual-e5-small"])
def test_missing_or_other_model_name_flags(tmp_path, recorded):
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        if recorded is not None:
            set_embed_metadata(conn, "embed_model", recorded)
        assert _check(conn) is True
    finally:
        conn.close()


@pytest.mark.parametrize("table", TABLES)
def test_old_dimension_in_any_table_flags_even_with_current_name(tmp_path, table):
    """名前が現行でも、4 系統のどれか 1 つに古い長さがあれば旗が立つ。"""
    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        conn.execute(f"UPDATE {table} SET vector = ?", (json.dumps(_vec(OLD_DIM)),))
        conn.commit()
        assert _check(conn) is True
    finally:
        conn.close()


def test_sample_finds_stale_rows_at_either_end(tmp_path):
    from sai_memory.embedding_migration import sample_stale_vector_tables

    conn = _open(tmp_path / "memory.db")
    try:
        for i in range(40):
            store_chronicle_embedding(conn, f"e{i:02d}", _vec(EMBED_DIM))
        assert sample_stale_vector_tables(conn, EMBED_DIM) == []
        store_chronicle_embedding(conn, "e_new", _vec(OLD_DIM))  # 最新端
        assert sample_stale_vector_tables(conn, EMBED_DIM) == ["arasuji_embeddings"]
    finally:
        conn.close()


def test_real_adapter_runs_the_check_on_open(tmp_path):
    """adapter の初期化で判定が走り、Chronicle だけ古い memory.db で旗が立つ。"""
    from saiverse_memory.adapter import SAIMemoryAdapter

    conn = _open(tmp_path / "memory.db")
    try:
        create_entry(conn, level=1, content="あらすじ", source_ids=[], start_time=1,
                     end_time=2, source_count=1, message_count=1, entry_id="c1")
        store_chronicle_embedding(conn, "c1", _vec(OLD_DIM))
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
    finally:
        conn.close()

    settings = replace(load_settings(), memory_enabled=True)
    with patch("saiverse_memory.adapter.Embedder", FakeEmbedder):
        adapter = SAIMemoryAdapter("p", persona_dir=tmp_path, settings=settings)
    try:
        assert adapter.embed_model_changed is True
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# 再計算 API のジョブ (検出 → 再計算 → 4 系統が揃う、の通し)
# ---------------------------------------------------------------------------

def _manager_with_warning(persona_ids, extra_warnings=()):
    from manager.persona import PersonaMixin, embed_model_mismatch_warning

    class _Manager(PersonaMixin):
        """clear_embed_model_mismatch を動かす最小の manager。"""

    mgr = _Manager.__new__(_Manager)
    mgr.personas = {
        pid: SimpleNamespace(sai_memory=SimpleNamespace(
            embed_model_changed=True, ensure_embedder=lambda: True,
        ))
        for pid in persona_ids
    }
    mgr.startup_warnings = [*extra_warnings, embed_model_mismatch_warning(list(persona_ids))]
    return mgr


def _run_api_job(db_path: Path, *, force: bool, manager):
    from api.routes.people import reembed

    with patch.object(reembed, "get_persona_memory_db", lambda _pid: db_path), \
            patch("sai_memory.memory.recall.Embedder", FakeEmbedder):
        reembed._run_reembed_task("p", force, manager)
    return reembed._reembed_status["p"]


@pytest.mark.parametrize("force", [True, False])
def test_api_job_rebuilds_all_four_systems_and_lifts_warning(tmp_path, force):
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
        set_embed_metadata(conn, "sluice_pan_marker", "keep-me")
        texts_before = _texts(conn)
        assert _check(conn) is True
    finally:
        conn.close()

    other = {"source": "persona_load", "message": "x"}
    mgr = _manager_with_warning(["p", "q"], extra_warnings=[other])
    warnings_list = mgr.startup_warnings

    status = _run_api_job(db_path, force=force, manager=mgr)

    assert status["running"] is False, status
    assert status["sources"] == {
        "chronicle": {"total": 1, "done": 1},
        "memopedia": {"total": 1, "done": 1},
        "fragment": {"total": 1, "done": 1},
    }
    assert status["progress"] == 2 and status["total"] == 2

    conn = _open(db_path)
    try:
        for table in TABLES:
            assert _dims(conn, table) == {EMBED_DIM}, table
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert get_embed_metadata(conn, "sluice_pan_marker") == "keep-me"
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert _texts(conn) == texts_before  # 原文は一字も変わらない
        assert _check(conn) is False
    finally:
        conn.close()

    # 警告: p だけ外れ、q は残る。他の警告と、リストの同一性 (AdminService と共有) は保つ
    assert mgr.personas["p"].sai_memory.embed_model_changed is False
    assert mgr.personas["q"].sai_memory.embed_model_changed is True
    assert mgr.startup_warnings is warnings_list
    mismatch = [w for w in mgr.startup_warnings if w["source"] == "embed_model_mismatch"]
    assert [w["persona_ids"] for w in mismatch] == [["q"]]
    assert "q" in mismatch[0]["message"] and "p," not in mismatch[0]["message"]
    assert other in mgr.startup_warnings


def test_api_job_removes_warning_when_last_persona_done(tmp_path):
    from api.routes.config import check_reembed_needed

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])
    assert check_reembed_needed(manager=mgr)["needed"] is True

    _run_api_job(db_path, force=False, manager=mgr)

    assert mgr.startup_warnings == []
    assert check_reembed_needed(manager=mgr) == {"needed": False, "persona_ids": [], "message": ""}


def test_api_job_keeps_warning_when_stale_vectors_remain(tmp_path):
    from api.routes.people import reembed

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])

    with patch("sai_memory.embedding_migration.scan_stale_vector_tables",
               return_value=["message_embeddings"]):
        status = _run_api_job(db_path, force=False, manager=mgr)

    assert "Stale vectors remain" in status["message"]
    assert mgr.personas["p"].sai_memory.embed_model_changed is True
    assert [w["persona_ids"] for w in mgr.startup_warnings] == [["p"]]
    reembed._reembed_status.pop("p", None)


def test_api_job_without_force_keeps_current_vectors(tmp_path):
    """force なしで名札も現行なら、すでに現行の長さのベクトルは消さない (作り直さない)。"""
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        marker = json.dumps([0.5] * EMBED_DIM)
        conn.execute("UPDATE memopedia_embeddings SET vector = ? WHERE page_id = ?",
                     (marker, ids["page"]))
        conn.execute("UPDATE arasuji_embeddings SET vector = ?", (json.dumps(_vec(OLD_DIM)),))
        conn.commit()
    finally:
        conn.close()

    status = _run_api_job(db_path, force=False, manager=None)

    assert status["sources"]["chronicle"] == {"total": 1, "done": 1}
    assert status["sources"]["memopedia"] == {"total": 0, "done": 0}
    conn = _open(db_path)
    try:
        assert conn.execute("SELECT vector FROM memopedia_embeddings").fetchone()[0] == marker
        assert _dims(conn, "arasuji_embeddings") == {EMBED_DIM}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "personas" / "p" / "memory.db"
    db_path.parent.mkdir(parents=True)
    return db_path


def _run_cli(tmp_path, monkeypatch, *, force=False):
    from scripts import reembed_memory

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    reembed_memory._reembed_persona(
        "p", embedder=FakeEmbedder(), expected_dim=EMBED_DIM,
        chunk_min=120, chunk_max=480, model_id=EMBED_MODEL_ID, force=force,
    )


def test_cli_rebuilds_four_systems_and_records_model(tmp_path, monkeypatch):
    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
    finally:
        conn.close()

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        for table in TABLES:
            assert _dims(conn, table) == {EMBED_DIM}, table
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert _check(conn) is False
    finally:
        conn.close()


def test_cli_refuses_smaller_dimension_without_force(tmp_path, monkeypatch):
    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed(conn, 1024)
        set_embed_metadata(conn, "embed_model", "bigger-model")
    finally:
        conn.close()

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        for table in TABLES:
            assert _dims(conn, table) == {1024}, table
        assert get_embed_metadata(conn, "embed_model") == "bigger-model"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 会話ログのインポート
# ---------------------------------------------------------------------------

def test_backfill_limited_to_given_message_ids(tmp_path):
    from saiverse_memory.adapter import SAIMemoryAdapter

    settings = replace(load_settings(), memory_enabled=True)
    with patch("saiverse_memory.adapter.Embedder", FakeEmbedder):
        adapter = SAIMemoryAdapter("p", persona_dir=tmp_path, settings=settings)
    try:
        skipped = adapter.append_persona_message(
            {"role": "user", "content": "前回スキップした取り込み", "embedding_chunks": 0})
        mine = adapter.append_persona_message(
            {"role": "user", "content": "今回の取り込み", "embedding_chunks": 0})
        result = adapter.backfill_missing_message_embeddings(message_ids=[mine])
        assert result["embedded"] == 1
        embedded = {r[0] for r in adapter.conn.execute(
            "SELECT DISTINCT message_id FROM message_embeddings")}
        assert embedded == {mine}
        assert skipped not in embedded
    finally:
        adapter.close()


class _FakeImportAdapter:
    """取り込みの記録だけをする偽 adapter。"""

    instances: list = []

    def __init__(self, persona_id, **kwargs):
        self.payloads = []
        self.backfill_calls = []
        self.closed = 0
        self.status_at_backfill = None
        self.embedder = object()
        _FakeImportAdapter.instances.append(self)

    def append_persona_message(self, payload, thread_suffix=None):
        self.payloads.append(dict(payload))
        return f"m{len(self.payloads)}"

    def set_thread_title(self, *args):
        pass

    def backfill_missing_message_embeddings(self, *, progress_callback=None, message_ids=None):
        from api.routes.people import import_chatlog

        self.status_at_backfill = dict(import_chatlog._extension_import_status.get("p", {}))
        self.backfill_calls.append(list(message_ids))
        if progress_callback:
            progress_callback(len(message_ids), len(message_ids))
        return {"total_missing": len(message_ids), "embedded": len(message_ids), "failed": 0}

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("skip_embedding", [False, True])
def test_extension_import_embeds_after_import_in_background(tmp_path, skip_embedding):
    from api.routes.people import import_chatlog

    conversation = SimpleNamespace(
        identifier="conv1",
        title="猫ロボットの相談",
        iter_memory_payloads=lambda: iter([
            {"role": "user", "content": "耳はどうする？"},
            {"role": "assistant", "content": "三角にしよう"},
        ]),
    )
    src = tmp_path / "export.json"
    src.write_text("{}", encoding="utf-8")
    _FakeImportAdapter.instances = []
    import_chatlog._import_embedding_status.pop("p", None)
    import_chatlog._import_embedding_threads.pop("p", None)

    with patch("tools.utilities.chatlog_exporter_importer.parse_exporter_file",
               lambda _p: conversation), \
            patch("saiverse_memory.SAIMemoryAdapter", _FakeImportAdapter):
        import_chatlog._run_extension_import_task("p", str(src), skip_embedding)
        thread = import_chatlog._import_embedding_threads.get("p")
        if thread is not None:
            thread.join(timeout=10)

    (adapter,) = _FakeImportAdapter.instances
    # 取り込み中はどちらの選択でも埋め込まない
    assert [p["embedding_chunks"] for p in adapter.payloads] == [0, 0]
    assert import_chatlog._extension_import_status["p"]["success"] is True
    assert adapter.closed == 1
    status = import_chatlog.get_extension_import_status("p", manager=None)
    if skip_embedding:
        assert thread is None
        assert adapter.backfill_calls == []
    else:
        assert adapter.backfill_calls == [["m1", "m2"]]
        # 埋め込みは「取り込み完了」を報告した後に始まる
        assert adapter.status_at_backfill.get("success") is True
        assert adapter.status_at_backfill.get("running") is False
        assert status.embedding_running is False
        assert status.embedding_progress == 2 and status.embedding_total == 2
    import_chatlog._extension_import_status.pop("p", None)
    import_chatlog._import_embedding_status.pop("p", None)


def test_official_import_embeds_after_import_in_background():
    from api.routes.people import import_chatlog

    record = SimpleNamespace(
        conversation_id="c1", identifier="c1", title="(untitled)",
        iter_memory_payloads=lambda include_roles=None: iter([
            {"role": "user", "content": "こんにちは", "metadata": {}},
        ]),
    )
    import_chatlog._chatgpt_export_cache["k"] = {
        "export": SimpleNamespace(conversations=[record]), "tmp_path": None, "persona_id": "p",
    }
    _FakeImportAdapter.instances = []
    import_chatlog._import_embedding_threads.pop("p", None)

    with patch("saiverse_memory.SAIMemoryAdapter", _FakeImportAdapter):
        import_chatlog._run_official_import_task("p", "k", ["0"], False)
        import_chatlog._import_embedding_threads["p"].join(timeout=10)

    (adapter,) = _FakeImportAdapter.instances
    assert [p["embedding_chunks"] for p in adapter.payloads] == [0]
    assert import_chatlog._official_import_status["p"]["success"] is True
    assert adapter.backfill_calls == [["m1"]]
    assert adapter.closed == 1
    assert "k" not in import_chatlog._chatgpt_export_cache
    import_chatlog._official_import_status.pop("p", None)
    import_chatlog._import_embedding_status.pop("p", None)


# ---------------------------------------------------------------------------
# 最終確認は全行を見る (起動時の標本では中央の古い行を見落とす)
# ---------------------------------------------------------------------------

def test_finish_reembed_finds_stale_row_in_the_middle(tmp_path):
    from sai_memory.embedding_migration import finish_reembed, sample_stale_vector_tables

    conn = _open(tmp_path / "memory.db")
    try:
        for i in range(40):
            dim = OLD_DIM if i == 20 else EMBED_DIM
            store_chronicle_embedding(conn, f"e{i:02d}", _vec(dim))
        # 標本 (両端 8 件ずつ) には入らない位置
        assert sample_stale_vector_tables(conn, EMBED_DIM) == []
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID) == ["arasuji_embeddings"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 埋め込みの計算中に本文が変わった行は保存しない
# ---------------------------------------------------------------------------

class _EditingEmbedder(FakeEmbedder):
    """1 回目の embed の最中に ``edit(conn)`` を走らせる (計算中の編集の再現)。"""

    def __init__(self, conn, edit):
        super().__init__()
        self._conn = conn
        self._edit = edit

    def embed(self, texts, *, is_query=False):
        if self._edit is not None:
            edit, self._edit = self._edit, None
            edit(self._conn)
        return super().embed(texts, is_query=is_query)


def test_reembed_messages_skips_message_edited_while_embedding(tmp_path):
    from sai_memory.embedding_migration import reembed_messages

    conn = _open(tmp_path / "memory.db")
    try:
        ids = _seed(conn, OLD_DIM)
        m1 = ids["messages"][0]

        def _edit(c):
            c.execute("UPDATE messages SET content = ? WHERE id = ?", ("書き換えた本文", m1))
            c.commit()

        result = reembed_messages(
            conn, _EditingEmbedder(conn, _edit), [m1], chunk_min=120, chunk_max=480,
        )
        assert result["changed"] == 1 and result["embedded"] == 0
        dims = {len(json.loads(v)) for (v,) in conn.execute(
            "SELECT vector FROM message_embeddings WHERE message_id = ?", (m1,))}
        assert dims == {OLD_DIM}  # 古い本文のベクトルを新しい長さで書かない

        # 編集が無ければ普通に作り直す
        result = reembed_messages(conn, FakeEmbedder(), [m1], chunk_min=120, chunk_max=480)
        assert result["embedded"] == 1 and result["changed"] == 0
    finally:
        conn.close()


def _edit_chronicle(c):
    from sai_memory.arasuji.storage import update_entry_content

    update_entry_content(c, "chr1", "書き換えたあらすじ")


def _edit_page(ids):
    def _edit(c):
        c.execute("UPDATE memopedia_pages SET summary = ? WHERE id = ?", ("書き換えた要約", ids["page"]))
        c.commit()
    return _edit


def _edit_fragment(ids):
    def _edit(c):
        c.execute("UPDATE memopedia_fragments SET content = ? WHERE id = ?",
                  ("書き換えた断片", ids["fragment"]))
        c.commit()
    return _edit


def _delete_fragment(ids):
    def _edit(c):
        c.execute("DELETE FROM memopedia_fragments WHERE id = ?", (ids["fragment"],))
        c.commit()
    return _edit


@pytest.mark.parametrize("source", ["chronicle", "memopedia", "fragment", "fragment_deleted"])
def test_generators_skip_rows_changed_while_embedding(tmp_path, source):
    from sai_memory.unified_recall import (
        embed_chronicle_entries,
        embed_memopedia_fragments,
        embed_memopedia_pages,
    )

    conn = _open(tmp_path / "memory.db")
    try:
        ids = _seed(conn, EMBED_DIM)
        for table in TABLES[1:]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

        table, id_col, row_id, gen, edit = {
            "chronicle": ("arasuji_embeddings", "entry_id", ids["chronicle"],
                          lambda e: embed_chronicle_entries(conn, e, level=1), _edit_chronicle),
            "memopedia": ("memopedia_embeddings", "page_id", ids["page"],
                          lambda e: embed_memopedia_pages(conn, e), _edit_page(ids)),
            "fragment": ("memopedia_fragment_embeddings", "fragment_id", ids["fragment"],
                         lambda e: embed_memopedia_fragments(conn, e), _edit_fragment(ids)),
            "fragment_deleted": ("memopedia_fragment_embeddings", "fragment_id", ids["fragment"],
                                 lambda e: embed_memopedia_fragments(conn, e), _delete_fragment(ids)),
        }[source]

        assert gen(_EditingEmbedder(conn, edit)) == 0
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ?", (row_id,)
        ).fetchone()[0] == 0

        # 次の生成パスが新しい本文で埋める (消えた行は対象から外れる)
        expected = 0 if source == "fragment_deleted" else 1
        assert gen(FakeEmbedder()) == expected
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ?", (row_id,)
        ).fetchone()[0] == expected
    finally:
        conn.close()


def test_rebuild_reports_skipped_rows(tmp_path):
    from sai_memory.embedding_migration import rebuild_recall_source_embeddings

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, OLD_DIM)
        result = rebuild_recall_source_embeddings(conn, _EditingEmbedder(conn, _edit_chronicle))
        assert result["chronicle"] == {"deleted": 1, "total": 1, "done": 0, "skipped": 1}
        assert result["memopedia"]["skipped"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# インポートと再計算のジョブ衛生
# ---------------------------------------------------------------------------

def _run_extension_import(tmp_path, skip_embedding):
    from api.routes.people import import_chatlog

    conversation = SimpleNamespace(
        identifier="conv1", title="猫ロボットの相談",
        iter_memory_payloads=lambda: iter([{"role": "user", "content": "耳はどうする？"}]),
    )
    src = tmp_path / "export.json"
    src.write_text("{}", encoding="utf-8")
    with patch("tools.utilities.chatlog_exporter_importer.parse_exporter_file",
               lambda _p: conversation), \
            patch("saiverse_memory.SAIMemoryAdapter", _FakeImportAdapter):
        import_chatlog._run_extension_import_task("p", str(src), skip_embedding)


def test_import_start_clears_previous_embedding_numbers(tmp_path):
    from api.routes.people import import_chatlog

    _FakeImportAdapter.instances = []
    import_chatlog._import_embedding_threads.pop("p", None)
    import_chatlog._import_embedding_status["p"] = {
        "embedding_running": False, "embedding_progress": 99, "embedding_total": 99,
        "embedding_failed": 3, "embedding_error": "old",
    }
    try:
        _run_extension_import(tmp_path, skip_embedding=True)
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.success is True
        assert status.embedding_progress is None and status.embedding_error is None
    finally:
        import_chatlog._extension_import_status.pop("p", None)
        import_chatlog._import_embedding_status.pop("p", None)


def test_import_waits_for_previous_embedding_thread(tmp_path):
    import threading
    import time

    from api.routes.people import import_chatlog

    _FakeImportAdapter.instances = []
    release = threading.Event()
    previous = threading.Thread(target=release.wait, args=(10,), daemon=True)
    previous.start()
    import_chatlog._import_embedding_threads["p"] = previous
    try:
        runner = threading.Thread(
            target=_run_extension_import, args=(tmp_path, True), daemon=True,
        )
        runner.start()
        time.sleep(0.3)
        assert _FakeImportAdapter.instances == []  # 前回の埋め込みが終わるまで本文に入らない
        release.set()
        runner.join(timeout=10)
        assert not runner.is_alive()
        assert len(_FakeImportAdapter.instances) == 1
    finally:
        release.set()
        import_chatlog._import_embedding_threads.pop("p", None)
        import_chatlog._extension_import_status.pop("p", None)
        import_chatlog._import_embedding_status.pop("p", None)


class _BackfillAdapter:
    def __init__(self, *, result=None, error=None, embedder=object()):
        self._result = result
        self._error = error
        self.embedder = embedder
        self.closed = 0

    def backfill_missing_message_embeddings(self, *, progress_callback=None, message_ids=None):
        if self._error is not None:
            raise self._error
        return self._result

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("adapter, expected_failed, error_part", [
    (_BackfillAdapter(error=RuntimeError("disk full\nsecond line")), 0, "RuntimeError: disk full"),
    (_BackfillAdapter(result={"total_missing": 2, "embedded": 1, "failed": 1}), 1, "1 message(s)"),
    (_BackfillAdapter(result={"total_missing": 2, "embedded": 0, "failed": 0}, embedder=None),
     0, "not available"),
    (_BackfillAdapter(result={"total_missing": 2, "embedded": 2, "failed": 0}), 0, None),
])
def test_import_embedding_keeps_failures_in_status(adapter, expected_failed, error_part):
    from api.routes.people import import_chatlog

    try:
        import_chatlog._embed_imported_messages("p", adapter, ["m1", "m2"])
        status = import_chatlog._import_embedding_status["p"]
        assert status["embedding_running"] is False
        assert status["embedding_total"] == 2
        assert status["embedding_failed"] == expected_failed
        if error_part is None:
            assert "embedding_error" not in status
        else:
            assert error_part in status["embedding_error"]
            assert "\n" not in status["embedding_error"]
        assert adapter.closed == 1
    finally:
        import_chatlog._import_embedding_status.pop("p", None)


def test_reembed_request_reserves_running_before_task_starts(tmp_path):
    from api.routes.people import reembed
    from api.routes.people.models import ReembedRequest

    db_path = tmp_path / "memory.db"
    db_path.write_bytes(b"")

    class _Tasks:
        def __init__(self):
            self.added = []

        def add_task(self, *args):
            self.added.append(args)

    tasks = _Tasks()
    reembed._reembed_status.pop("p", None)
    try:
        with patch.object(reembed, "get_persona_memory_db", lambda _pid: db_path):
            first = reembed.reembed_persona_memory("p", ReembedRequest(), tasks, manager=None)
            # タスクはまだ走っていないが、印はもう付いている
            assert reembed._reembed_status["p"]["running"] is True
            second = reembed.reembed_persona_memory("p", ReembedRequest(), tasks, manager=None)
        assert first["success"] is True
        assert second["success"] is False
        assert len(tasks.added) == 1
    finally:
        reembed._reembed_status.pop("p", None)


# ---------------------------------------------------------------------------
# 読み込みに失敗していた埋め込み器の立て直し
# ---------------------------------------------------------------------------

def _bare_adapter(embedder=None):
    from saiverse_memory.adapter import SAIMemoryAdapter

    adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
    adapter.persona_id = "p"
    adapter.settings = load_settings()
    adapter.embedder = embedder
    return adapter


def test_ensure_embedder_loads_when_missing():
    adapter = _bare_adapter()
    with patch("saiverse_memory.adapter.Embedder", FakeEmbedder):
        assert adapter.ensure_embedder() is True
    assert isinstance(adapter.embedder, FakeEmbedder)


def test_ensure_embedder_keeps_existing_without_loading():
    existing = FakeEmbedder()
    adapter = _bare_adapter(existing)
    with patch("saiverse_memory.adapter.Embedder",
               side_effect=AssertionError("must not reload")):
        assert adapter.ensure_embedder() is True
    assert adapter.embedder is existing


def test_ensure_embedder_failure_returns_false_with_warning(caplog):
    import logging

    adapter = _bare_adapter()
    with patch("saiverse_memory.adapter.Embedder", side_effect=OSError("model files missing")), \
            caplog.at_level(logging.WARNING, logger="saiverse_memory.adapter"):
        assert adapter.ensure_embedder() is False
    assert adapter.embedder is None
    assert any(r.levelno == logging.WARNING and "retrying" in r.getMessage()
               for r in caplog.records)


def test_api_job_keeps_warning_when_live_embedder_cannot_load(tmp_path, caplog):
    import logging

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])
    mgr.personas["p"].sai_memory.ensure_embedder = lambda: False

    with caplog.at_level(logging.WARNING, logger="api.routes.people.reembed"):
        status = _run_api_job(db_path, force=False, manager=mgr)

    assert "could not be loaded" in status["message"]
    assert mgr.personas["p"].sai_memory.embed_model_changed is True
    assert [w["persona_ids"] for w in mgr.startup_warnings] == [["p"]]
    assert any("could not be loaded" in r.getMessage() for r in caplog.records)


def test_api_job_lifts_warning_for_persona_not_loaded(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])
    mgr.personas = {}  # 未ロード: 次のロードで正しく読むので警告は下ろしてよい

    _run_api_job(db_path, force=False, manager=mgr)

    assert mgr.startup_warnings == []


# ---------------------------------------------------------------------------
# 名札の変更は次元に関係なく全再計算 (同じ次元の旧モデルからの移行)
# ---------------------------------------------------------------------------

SAME_DIM_OLD_MODEL = "embeddinggemma-2-q8@old"
_OLD_SPACE_VEC = json.dumps([0.5] * EMBED_DIM)


def _seed_same_dim_old_space(conn: sqlite3.Connection, label: str) -> None:
    """長さは現行と同じだが、別のモデルが作った (という体の) ベクトルを 4 系統に置く。"""
    _seed(conn, EMBED_DIM)
    for table in TABLES:
        conn.execute(f"UPDATE {table} SET vector = ?", (_OLD_SPACE_VEC,))
    conn.commit()
    set_embed_metadata(conn, "embed_model", label)


def _old_space_left(conn: sqlite3.Connection) -> dict:
    return {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE vector = ?", (_OLD_SPACE_VEC,)
        ).fetchone()[0]
        for table in TABLES
    }


def test_api_job_rebuilds_everything_when_label_changed_with_same_dim(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed_same_dim_old_space(conn, SAME_DIM_OLD_MODEL)
        assert _check(conn) is True
    finally:
        conn.close()

    status = _run_api_job(db_path, force=False, manager=None)

    assert status["running"] is False, status
    assert status["sources"] == {
        "chronicle": {"total": 1, "done": 1},
        "memopedia": {"total": 1, "done": 1},
        "fragment": {"total": 1, "done": 1},
    }
    assert status["progress"] == 2 and status["total"] == 2
    conn = _open(db_path)
    try:
        assert _old_space_left(conn) == {t: 0 for t in TABLES}
        for table in TABLES:
            assert _dims(conn, table) == {EMBED_DIM}, table
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert _check(conn) is False
    finally:
        conn.close()


def test_api_job_with_current_label_and_same_dim_rebuilds_nothing(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed_same_dim_old_space(conn, EMBED_MODEL_ID)
        before = _old_space_left(conn)
    finally:
        conn.close()

    status = _run_api_job(db_path, force=False, manager=None)

    assert status["sources"] == {
        s: {"total": 0, "done": 0} for s in ("chronicle", "memopedia", "fragment")
    }
    assert status["progress"] == 0 and status["total"] == 0
    conn = _open(db_path)
    try:
        assert before == {
            "message_embeddings": 3, "arasuji_embeddings": 1,
            "memopedia_embeddings": 1, "memopedia_fragment_embeddings": 1,
        }
        assert _old_space_left(conn) == before
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()


@pytest.mark.parametrize("label, rebuilds", [
    (SAME_DIM_OLD_MODEL, True),
    (EMBED_MODEL_ID, False),
])
def test_cli_label_change_rebuilds_everything_with_same_dim(tmp_path, monkeypatch,
                                                            label, rebuilds):
    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed_same_dim_old_space(conn, label)
        before = _old_space_left(conn)
    finally:
        conn.close()

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        if rebuilds:
            assert _old_space_left(conn) == {t: 0 for t in TABLES}
        else:
            assert _old_space_left(conn) == before  # 名札が同じなら従来どおり何もしない
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()


def test_label_change_without_vectors_is_not_promoted(tmp_path):
    from sai_memory.embedding_migration import model_label_changed

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", SAME_DIM_OLD_MODEL)
        assert model_label_changed(conn, EMBED_MODEL_ID) is False
        _seed(conn, EMBED_DIM)
        assert model_label_changed(conn, EMBED_MODEL_ID) is True
        conn.execute("DELETE FROM embed_metadata WHERE key = 'embed_model'")
        conn.commit()
        assert model_label_changed(conn, EMBED_MODEL_ID) is True  # 名札が無いのも「違う」
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        assert model_label_changed(conn, EMBED_MODEL_ID) is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 「移行中」の印: 途中で死んだ再計算は、ベクトルが全部消えていても起動時に警告する
# ---------------------------------------------------------------------------

def test_unfinished_mark_flags_even_when_every_vector_is_gone(tmp_path):
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
        set_embed_metadata(conn, "sluice_pan_marker", "keep-me")
        begin_reembed(conn, EMBED_MODEL_ID)
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY).startswith(EMBED_MODEL_ID)

        assert _check(conn) is True
        # 新品と誤認して名札を書き換えない
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"

        # ジョブが最後まで走れば印は消え、旗も立たない
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID) == []
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert get_embed_metadata(conn, "sluice_pan_marker") == "keep-me"
        assert _check(conn) is False
    finally:
        conn.close()


def test_unfinished_mark_flags_with_current_label_and_dims(tmp_path):
    from sai_memory.embedding_migration import begin_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        assert _check(conn) is False
        begin_reembed(conn, EMBED_MODEL_ID)
        assert _check(conn) is True
    finally:
        conn.close()


def test_finish_reembed_clears_mark_even_when_stale_rows_remain(tmp_path):
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        store_chronicle_embedding(conn, "e1", _vec(OLD_DIM))
        begin_reembed(conn, EMBED_MODEL_ID)
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID) == ["arasuji_embeddings"]
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()


def test_api_job_that_dies_half_way_leaves_the_mark(tmp_path):
    from api.routes.people import reembed
    from sai_memory.embedding_migration import has_any_vectors

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
    finally:
        conn.close()

    with patch("sai_memory.embedding_migration.reembed_messages",
               side_effect=RuntimeError("process died")):
        status = _run_api_job(db_path, force=False, manager=None)

    assert status["running"] is False
    assert "process died" in status["message"]
    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"
        # 削除を確定した直後に死んだ状態 (全ベクトル消失) を再現する
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        assert has_any_vectors(conn) is False
        assert _check(conn) is True
    finally:
        conn.close()
    reembed._reembed_status.pop("p", None)

    # 再実行が完走すれば印は消える
    _run_api_job(db_path, force=False, manager=None)
    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        for table in TABLES:
            assert _dims(conn, table) == {EMBED_DIM}, table
        assert _check(conn) is False
    finally:
        conn.close()
    reembed._reembed_status.pop("p", None)


def test_cli_that_dies_half_way_leaves_the_mark(tmp_path, monkeypatch):
    from scripts import reembed_memory

    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
    finally:
        conn.close()

    with patch.object(reembed_memory, "reembed_messages",
                      side_effect=RuntimeError("process died")), \
            pytest.raises(RuntimeError):
        _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert _check(conn) is True
    finally:
        conn.close()


def test_cli_refusing_smaller_dimension_does_not_mark(tmp_path, monkeypatch):
    db_path = _cli_db(tmp_path)
    conn = _open(db_path)
    try:
        _seed(conn, 1024)
        set_embed_metadata(conn, "embed_model", "bigger-model")
    finally:
        conn.close()

    _run_cli(tmp_path, monkeypatch)

    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()
