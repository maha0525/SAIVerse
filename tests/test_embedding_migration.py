"""埋め込みモデル移行の機構 (docs/intent/embedding_gemma2_migration.md §2) のテスト。

押さえること:

- 起動時の検出が 4 系統 (メッセージ / Chronicle / Memopedia ページ / Fragment)
  のどれに古い長さのベクトルがあっても旗を立てる。ベクトルが一つも無い新品の
  memory.db では旗を立てず、いまのモデルを記録する。
- 再計算 API のジョブが 3 系統の古いベクトルを消してその場で作り直し、
  メッセージも作り直し、``embed_model`` を記録し、そのペルソナの警告を下ろす。
- CLI も同じ範囲を直し、``embed_model`` を記録する。小さい次元への再計算は拒む。
- 会話ログのインポートは、取り込み中は埋め込まず、取り込みの完了を報告して
  から裏で取り込んだ分だけを埋める。取り込み自体は前回の埋め込みを待たず、
  待つのは裏の埋め込みのスレッドだけ。状態は取り込みごとのジョブ番号で分け、
  失敗は状態に残す。裏へ回す直前に memory.db へ消えない印を付け、失敗ゼロで
  完走したら消す (残った印は起動時の旗になり、再計算が消す)。
- 再計算の最終確認は全行を見る。完了 (古い行なし・ベクトル待ちの行なし・
  編集で飛ばした行なし) のときだけ名札と印を同じトランザクションで確定し、
  未完了なら両方そのままで、次の実行が全再計算に昇格して収束する。
- force は 3 系統の表を全行消す (再生成されない幹ページ等の旧ベクトルも残さない)。
- 埋め込みの計算中に本文が変わった行は保存しない。本文の読み直しと保存は
  BEGIN IMMEDIATE の中で一体に行い、別の接続は割り込めない。
- 埋め込みモデルの読み込みに失敗した adapter も旗を立てる。
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


def _reset_import_embedding_state():
    from api.routes.people import import_chatlog

    with import_chatlog._import_embedding_lock:
        import_chatlog._import_embedding_jobs.clear()
        import_chatlog._import_embedding_threads.clear()
        import_chatlog._import_embedding_latest.clear()
        import_chatlog._import_embedding_dirty.clear()
    import_chatlog._extension_import_status.pop("p", None)
    import_chatlog._official_import_status.pop("p", None)


class _FakeImportAdapter:
    """取り込みの記録だけをする偽 adapter。"""

    instances: list = []

    def __init__(self, persona_id, **kwargs):
        self.payloads = []
        self.backfill_calls = []
        self.closed = 0
        self.status_at_backfill = None
        self.embedder = object()
        # 印の書き込み (begin の件数) と削除の回数、begin 時点の取り込み状態
        self.marks_begun = []
        self.marks_finished = 0
        self.status_at_begin = None
        _FakeImportAdapter.instances.append(self)

    def begin_embedding_backfill(self, count):
        from api.routes.people import import_chatlog

        self.status_at_begin = dict(import_chatlog._extension_import_status.get("p", {}))
        self.marks_begun.append(count)
        return True

    def finish_embedding_backfill(self):
        self.marks_finished += 1

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
        return {"total_missing": len(message_ids), "embedded": len(message_ids),
                "failed": 0, "changed": 0}

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
    _reset_import_embedding_state()

    try:
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
            # skip の取り込みは消えない印を書かない
            assert adapter.marks_begun == [] and adapter.marks_finished == 0
            assert status.embedding_running is None
        else:
            assert adapter.backfill_calls == [["m1", "m2"]]
            # 印は「取り込み完了」の報告より前に書き、埋め込みはその報告の後に始まる
            assert adapter.marks_begun == [2]
            assert adapter.status_at_begin.get("running") is True
            assert adapter.status_at_backfill.get("success") is True
            assert adapter.status_at_backfill.get("running") is False
            # 失敗ゼロで完走したので印を消す
            assert adapter.marks_finished == 1
            assert status.embedding_running is False
            assert status.embedding_progress == 2 and status.embedding_total == 2
    finally:
        _reset_import_embedding_state()


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
    _reset_import_embedding_state()

    try:
        with patch("saiverse_memory.SAIMemoryAdapter", _FakeImportAdapter):
            import_chatlog._run_official_import_task("p", "k", ["0"], False)
            import_chatlog._import_embedding_threads["p"].join(timeout=10)

        (adapter,) = _FakeImportAdapter.instances
        assert [p["embedding_chunks"] for p in adapter.payloads] == [0]
        assert import_chatlog._official_import_status["p"]["success"] is True
        assert adapter.backfill_calls == [["m1"]]
        assert adapter.marks_begun == [1] and adapter.marks_finished == 1
        assert adapter.closed == 1
        assert "k" not in import_chatlog._chatgpt_export_cache
        status = import_chatlog.get_official_import_status("p", manager=None)
        assert status.embedding_running is False and status.embedding_progress == 1
    finally:
        _reset_import_embedding_state()


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
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID)
        assert outcome.stale_tables == ["arasuji_embeddings"]
        assert outcome.complete is False
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


def test_skip_import_does_not_show_or_reset_another_jobs_numbers(tmp_path):
    """skip の取り込みは埋め込みの列に関わらず、別の取り込みの数字も見せない・消さない。"""
    from api.routes.people import import_chatlog

    _FakeImportAdapter.instances = []
    _reset_import_embedding_state()
    earlier = {
        "embedding_running": False, "embedding_progress": 99, "embedding_total": 99,
        "embedding_failed": 3, "embedding_error": "old",
    }
    import_chatlog._import_embedding_jobs[7] = dict(earlier)
    import_chatlog._official_import_status["p"] = {
        "running": False, "success": True, "message": "x", "embedding_job": 7,
    }
    try:
        _run_extension_import(tmp_path, skip_embedding=True)
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.success is True
        assert status.embedding_progress is None and status.embedding_error is None
        # 別の取り込み (公式) の状態は、その取り込みのジョブの結果のまま
        official = import_chatlog.get_official_import_status("p", manager=None)
        assert official.embedding_error == "old" and official.embedding_progress == 99
        assert import_chatlog._import_embedding_jobs[7] == earlier
    finally:
        _reset_import_embedding_state()


def test_import_does_not_wait_for_previous_embedding(tmp_path):
    """取り込みは前回の埋め込みを待たない。待つのは裏の埋め込みスレッドだけ。"""
    import threading

    from api.routes.people import import_chatlog

    _FakeImportAdapter.instances = []
    _reset_import_embedding_state()
    release = threading.Event()
    previous = threading.Thread(target=release.wait, args=(10,), daemon=True)
    previous.start()
    import_chatlog._import_embedding_threads["p"] = previous
    try:
        # 同じスレッドで呼んで、すぐ返ってくる (前回が終わるのを待たない)
        _run_extension_import(tmp_path, skip_embedding=False)
        (adapter,) = _FakeImportAdapter.instances
        assert import_chatlog._extension_import_status["p"]["success"] is True
        mine = import_chatlog._import_embedding_threads["p"]
        assert mine is not previous and mine.is_alive()
        # 裏の埋め込みは前回の終了を待っていて、まだ始まっていない
        assert adapter.backfill_calls == []
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.embedding_running is True
        assert status.embedding_progress == 0 and status.embedding_total == 1

        release.set()
        mine.join(timeout=10)
        assert adapter.backfill_calls == [["m1"]]
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.embedding_running is False and status.embedding_progress == 1
    finally:
        release.set()
        _reset_import_embedding_state()


def test_import_embedding_jobs_queue_per_persona_and_keep_their_own_status(tmp_path):
    """二つの取り込みの埋め込みは一本の列で順に走り、状態はジョブごとに分かれる。

    印を消すのは列の最後のジョブだけ (前のジョブの完走で、後ろに並んだ分の印を消さない)。
    """
    import threading

    from api.routes.people import import_chatlog

    _reset_import_embedding_state()
    order = []
    gate = threading.Event()

    class _SlowAdapter(_FakeImportAdapter):
        def backfill_missing_message_embeddings(self, *, progress_callback=None, message_ids=None):
            if message_ids == ["a1"]:
                gate.wait(10)
            order.append(list(message_ids))
            return {"total_missing": len(message_ids), "embedded": len(message_ids),
                    "failed": 0, "changed": 0}

    first, second = _SlowAdapter("p"), _SlowAdapter("p")
    try:
        job1 = import_chatlog._reserve_import_embedding("p", first, 1)
        assert import_chatlog._launch_import_embedding("p", job1, first, ["a1"]) is True
        job2 = import_chatlog._reserve_import_embedding("p", second, 2)
        assert import_chatlog._launch_import_embedding("p", job2, second, ["b1", "b2"]) is True
        assert job2 != job1
        t2 = import_chatlog._import_embedding_threads["p"]
        assert import_chatlog._import_embedding_jobs[job1]["embedding_running"] is True
        assert import_chatlog._import_embedding_jobs[job2]["embedding_total"] == 2

        gate.set()
        t2.join(timeout=10)
        assert order == [["a1"], ["b1", "b2"]]  # 同じペルソナでは二本同時に走らない
        assert import_chatlog._import_embedding_jobs[job1]["embedding_progress"] == 1
        assert import_chatlog._import_embedding_jobs[job2]["embedding_progress"] == 2
        # 前のジョブは後ろが並んでいたので印を消さず、最後のジョブが消す
        assert first.marks_finished == 0 and second.marks_finished == 1
    finally:
        gate.set()
        _reset_import_embedding_state()


class _BackfillAdapter:
    def __init__(self, *, result=None, error=None, embedder=object()):
        self._result = result
        self._error = error
        self.embedder = embedder
        self.closed = 0
        self.marks_finished = 0

    def backfill_missing_message_embeddings(self, *, progress_callback=None, message_ids=None):
        if self._error is not None:
            raise self._error
        return self._result

    def finish_embedding_backfill(self):
        self.marks_finished += 1

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("make_adapter, expected_failed, error_part", [
    (lambda: _BackfillAdapter(error=RuntimeError("disk full\nsecond line")), 0,
     "RuntimeError: disk full"),
    (lambda: _BackfillAdapter(result={"total_missing": 2, "embedded": 1, "failed": 1}), 1,
     "1 message(s)"),
    (lambda: _BackfillAdapter(result={"total_missing": 2, "embedded": 0, "failed": 0},
                              embedder=None), 0, "not available"),
    (lambda: _BackfillAdapter(result={"total_missing": 2, "embedded": 2, "failed": 0}), 0, None),
])
def test_import_embedding_keeps_failures_in_status(make_adapter, expected_failed, error_part):
    from api.routes.people import import_chatlog

    _reset_import_embedding_state()
    adapter = make_adapter()
    try:
        import_chatlog._import_embedding_latest["p"] = 1
        import_chatlog._embed_imported_messages("p", 1, adapter, ["m1", "m2"])
        status = import_chatlog._import_embedding_jobs[1]
        assert status["embedding_running"] is False
        assert status["embedding_total"] == 2
        assert status["embedding_failed"] == expected_failed
        if error_part is None:
            assert "embedding_error" not in status
            assert adapter.marks_finished == 1  # 失敗ゼロで完走 → 印を消す
        else:
            assert error_part in status["embedding_error"]
            assert "\n" not in status["embedding_error"]
            assert adapter.marks_finished == 0  # 失敗が残れば印も残す
            assert import_chatlog._import_embedding_dirty["p"] is True
        assert adapter.closed == 1
    finally:
        _reset_import_embedding_state()


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
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID).complete is True
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


def test_finish_reembed_keeps_label_and_mark_when_stale_rows_remain(tmp_path):
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
        store_chronicle_embedding(conn, "e1", _vec(OLD_DIM))
        begin_reembed(conn, EMBED_MODEL_ID)
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID)
        assert outcome.complete is False
        assert outcome.stale_tables == ["arasuji_embeddings"]
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"
    finally:
        conn.close()


@pytest.mark.parametrize("left", ["pending", "skipped"])
def test_finish_reembed_incomplete_when_rows_wait_or_were_skipped(tmp_path, left):
    """ベクトル待ちの行 / 編集で飛ばした行が残れば、名札も印も動かさない。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        begin_reembed(conn, EMBED_MODEL_ID)
        skipped = 0
        if left == "pending":
            conn.execute("DELETE FROM arasuji_embeddings")  # Chronicle Lv1 がベクトル待ち
            conn.commit()
        else:
            skipped = 1
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, skipped=skipped)
        assert outcome.complete is False
        if left == "pending":
            assert outcome.pending == {"chronicle": 1}
            assert "chronicle 1" in outcome.describe()
        else:
            assert outcome.skipped == 1 and "Skipped 1" in outcome.describe()
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert _check(conn) is True
    finally:
        conn.close()


def test_finish_reembed_commits_label_and_mark_in_one_transaction(tmp_path):
    """印の削除が失敗したら、名札の更新も巻き戻る (同じトランザクションで確定する)。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", "old-label")
        begin_reembed(conn, EMBED_MODEL_ID)
        conn.execute(
            f"""
            CREATE TRIGGER refuse_mark_delete BEFORE DELETE ON embed_metadata
            WHEN OLD.key = '{MIGRATION_IN_PROGRESS_KEY}'
            BEGIN SELECT RAISE(ABORT, 'refused'); END
            """
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID)
        assert conn.in_transaction is False
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None

        conn.execute("DROP TRIGGER refuse_mark_delete")
        conn.commit()
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID).complete is True
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()


class _EditOnceEmbedder(FakeEmbedder):
    """API のジョブが作る Embedder の代役。最初の embed の最中に別の接続で編集する。"""

    edit = None  # (db_path, sql, params) を入れると 1 回だけ実行する

    def embed(self, texts, *, is_query=False):
        if _EditOnceEmbedder.edit is not None:
            (db_path, sql, params), _EditOnceEmbedder.edit = _EditOnceEmbedder.edit, None
            other = sqlite3.connect(str(db_path))
            try:
                other.execute(sql, params)
                other.commit()
            finally:
                other.close()
        return super().embed(texts, is_query=is_query)


def test_api_job_with_skipped_row_stays_incomplete_then_converges(tmp_path):
    """編集で飛ばした行があると名札・印は動かず警告も残る。再実行は全再計算に昇格して収束する。"""
    from api.routes.people import reembed

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, OLD_DIM)
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])

    _EditOnceEmbedder.edit = (
        db_path, "UPDATE memopedia_pages SET content = ? WHERE id = ?", ("書き換えた", "chr1"),
    )
    try:
        with patch.object(reembed, "get_persona_memory_db", lambda _pid: db_path), \
                patch("sai_memory.memory.recall.Embedder", _EditOnceEmbedder):
            reembed._run_reembed_task("p", False, mgr)
        status = reembed._reembed_status["p"]
    finally:
        _EditOnceEmbedder.edit = None
    assert "Skipped 1" in status["message"] and "Run the re-embed again" in status["message"]
    assert mgr.personas["p"].sai_memory.embed_model_changed is True
    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"
        assert _check(conn) is True
        # 名札を現行に書き換えても、残った「移行中」の印だけで次の実行は全再計算に昇格する
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
    finally:
        conn.close()

    status = _run_api_job(db_path, force=False, manager=mgr)
    assert "Run the re-embed again" not in status["message"]
    conn = _open(db_path)
    try:
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        for table in TABLES:
            assert _dims(conn, table) == {EMBED_DIM}, table
        assert _check(conn) is False
    finally:
        conn.close()
    assert mgr.startup_warnings == []
    reembed._reembed_status.pop("p", None)


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


# ---------------------------------------------------------------------------
# force は 3 系統の表を全行消す (再生成されない行の旧ベクトルも検索に残さない)
# ---------------------------------------------------------------------------

def test_force_rebuild_drops_vectors_the_generators_never_recreate(tmp_path):
    from sai_memory.embedding_migration import rebuild_recall_source_embeddings
    from sai_memory.unified_recall import (
        get_chronicle_embeddings,
        get_fragment_embeddings,
        get_memopedia_embeddings,
    )

    conn = _open(tmp_path / "memory.db")
    try:
        ids = _seed(conn, EMBED_DIM)
        # 長さは現行と同じだが別のモデルが作った (という体の) ベクトルを、
        # 生成器が作らない行に置く: Chronicle Lv2、幹ページ、消えた断片の残骸
        create_entry(conn, level=2, content="上位のあらすじ", source_ids=[], start_time=1,
                     end_time=2, source_count=1, message_count=1, entry_id="chr2")
        store_chronicle_embedding(conn, "chr2", json.loads(_OLD_SPACE_VEC))
        trunk = create_page(conn, parent_id="root_terms", title="幹", summary="幹の要約",
                            content="", category="terms", is_trunk=True)
        store_memopedia_embedding(conn, trunk.id, json.loads(_OLD_SPACE_VEC))
        store_fragment_embedding(conn, "gone-fragment", json.loads(_OLD_SPACE_VEC))
        # 修正前は検索の getter がこれらを除外せず返していた
        assert "chr2" in {r[0] for r in get_chronicle_embeddings(conn)}
        assert trunk.id in {r[0] for r in get_memopedia_embeddings(conn)}

        result = rebuild_recall_source_embeddings(conn, FakeEmbedder(), force=True)

        assert result["chronicle"]["deleted"] == 2 and result["chronicle"]["done"] == 1
        assert result["memopedia"]["deleted"] == 2 and result["memopedia"]["done"] == 1
        assert result["fragment"]["deleted"] == 2 and result["fragment"]["done"] == 1
        assert _old_space_left(conn)["arasuji_embeddings"] == 0
        assert {r[0] for r in get_chronicle_embeddings(conn)} == {ids["chronicle"]}
        assert {r[0] for r in get_memopedia_embeddings(conn)} == {ids["page"]}
        assert {r[0] for r in get_fragment_embeddings(conn)} == {ids["fragment"]}
        assert conn.execute(
            "SELECT COUNT(*) FROM memopedia_fragment_embeddings WHERE fragment_id = 'gone-fragment'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 本文の読み直しと保存は BEGIN IMMEDIATE の中で一体 (別の接続は割り込めない)
# ---------------------------------------------------------------------------

def _try_write_from_other_connection(db_path: Path) -> str:
    """別の接続から待ちなしで書いてみて、結果 ("written" か例外の文言) を返す。"""
    other = sqlite3.connect(str(db_path), timeout=0)
    try:
        other.execute("CREATE TABLE IF NOT EXISTS _probe (x)")
        other.execute("INSERT INTO _probe VALUES (1)")
        other.commit()
        return "written"
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        other.close()


def test_immediate_transaction_blocks_other_connections_and_rolls_back(tmp_path):
    from sai_memory.db_locks import immediate_transaction

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, EMBED_DIM)
        with immediate_transaction(conn):
            assert "locked" in _try_write_from_other_connection(db_path)
        assert conn.in_transaction is False
        assert _try_write_from_other_connection(db_path) == "written"

        with pytest.raises(RuntimeError):
            with immediate_transaction(conn):
                conn.execute("DELETE FROM arasuji_embeddings")
                raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) FROM arasuji_embeddings").fetchone()[0] == 1

        # 呼び手のトランザクションの中では BEGIN も commit もしない
        conn.execute("BEGIN IMMEDIATE")
        with immediate_transaction(conn):
            conn.execute("DELETE FROM arasuji_embeddings")
        assert conn.in_transaction is True
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM arasuji_embeddings").fetchone()[0] == 1
    finally:
        conn.close()


def test_generator_recheck_runs_inside_the_write_lock(tmp_path):
    """3 系統の生成器: 本文を読み直している最中、別の接続は書けない。"""
    from sai_memory import unified_recall

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        _seed(conn, EMBED_DIM)
        conn.execute("DELETE FROM arasuji_embeddings")
        conn.commit()
        seen = []
        real = unified_recall._current_chronicle_text

        def _probe(c, entry_id):
            seen.append(_try_write_from_other_connection(db_path))
            return real(c, entry_id)

        with patch.object(unified_recall, "_current_chronicle_text", _probe):
            assert unified_recall.embed_chronicle_entries(conn, FakeEmbedder(), level=1) == 1
        assert len(seen) == 1 and "locked" in seen[0]
        assert conn.execute("SELECT COUNT(*) FROM arasuji_embeddings").fetchone()[0] == 1
    finally:
        conn.close()


def _probe_get_message(db_path: Path, seen: list):
    from sai_memory.memory import storage

    real = storage.get_message
    calls = {"n": 0}

    def _probe(c, mid):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 回目は計算前の読み取り、2 回目がロック内の読み直し
            seen.append(_try_write_from_other_connection(db_path))
        return real(c, mid)

    return patch.object(storage, "get_message", _probe)


def test_reembed_messages_recheck_runs_inside_the_write_lock(tmp_path):
    from sai_memory.embedding_migration import reembed_messages

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, OLD_DIM)
        seen: list = []
        with _probe_get_message(db_path, seen):
            result = reembed_messages(
                conn, FakeEmbedder(), [ids["messages"][0]], chunk_min=120, chunk_max=480,
            )
        assert result["embedded"] == 1
        assert len(seen) == 1 and "locked" in seen[0]
    finally:
        conn.close()


def _real_adapter(tmp_path, embedder_cls=FakeEmbedder):
    from saiverse_memory.adapter import SAIMemoryAdapter

    settings = replace(load_settings(), memory_enabled=True)
    with patch("saiverse_memory.adapter.Embedder", embedder_cls):
        return SAIMemoryAdapter("p", persona_dir=tmp_path, settings=settings)


def test_backfill_recheck_runs_inside_the_write_lock_and_skips_edits(tmp_path):
    adapter = _real_adapter(tmp_path)
    db_path = tmp_path / "memory.db"
    try:
        create_entry(adapter.conn, level=1, content="あらすじ", source_ids=[], start_time=1,
                     end_time=2, source_count=1, message_count=1, entry_id="chr1")
        mid = adapter.append_persona_message(
            {"role": "user", "content": "取り込んだ本文", "embedding_chunks": 0})
        from sai_memory.memory import storage

        # backfill は一覧の後、各メッセージでロック内の読み直しを 1 回だけ行う
        seen: list = []
        real = storage.get_message

        def _probe(c, message_id):
            seen.append(_try_write_from_other_connection(db_path))
            return real(c, message_id)

        with patch.object(storage, "get_message", _probe):
            result = adapter.backfill_missing_message_embeddings(message_ids=[mid])
        assert result["embedded"] == 1 and result["changed"] == 0
        assert len(seen) == 1 and "locked" in seen[0]

        # 計算中に本文が書き換わったメッセージは、古い本文のベクトルを書かない
        mid2 = adapter.append_persona_message(
            {"role": "user", "content": "もう一つの本文", "embedding_chunks": 0})

        class _Editing(FakeEmbedder):
            def embed(self, texts, *, is_query=False):
                adapter.conn.execute(
                    "UPDATE messages SET content = ? WHERE id = ?", ("書き換えた", mid2))
                adapter.conn.commit()
                return super().embed(texts, is_query=is_query)

        adapter.embedder = _Editing()
        result = adapter.backfill_missing_message_embeddings(message_ids=[mid2])
        assert result["changed"] == 1 and result["embedded"] == 0
        assert adapter.conn.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE message_id = ?", (mid2,)
        ).fetchone()[0] == 0
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# インポートの背景埋め込みの消えない印
# ---------------------------------------------------------------------------

def test_backfill_mark_begin_extends_and_finish_removes(tmp_path):
    from sai_memory.embedding_migration import (
        BACKFILL_PENDING_KEY,
        backfill_pending,
        begin_backfill,
        finish_backfill,
    )

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "sluice_pan_marker", "keep-me")
        assert backfill_pending(conn) is None
        assert begin_backfill(conn, 3) is True
        first = backfill_pending(conn)
        since, count = first.rsplit(" ", 1)
        assert count == "3"
        # 既にある印には件数を足し、時刻は古いまま (最も古い未完の仕事を指す)
        assert begin_backfill(conn, 2) is False
        assert backfill_pending(conn) == f"{since} 5"
        assert conn.in_transaction is False

        assert _check(conn) is True  # 残っている印は起動時の旗になる
        finish_backfill(conn)
        assert get_embed_metadata(conn, BACKFILL_PENDING_KEY) is None
        assert get_embed_metadata(conn, "sluice_pan_marker") == "keep-me"
        assert _check(conn) is False
    finally:
        conn.close()


class _FailingEmbedder(FakeEmbedder):
    def embed(self, texts, *, is_query=False):
        raise RuntimeError("embedding failed")


def _run_real_extension_import(tmp_path, *, skip_embedding, embedder_cls=FakeEmbedder):
    from api.routes.people import import_chatlog
    from saiverse_memory.adapter import SAIMemoryAdapter

    conversation = SimpleNamespace(
        identifier="conv1", title="猫ロボットの相談",
        iter_memory_payloads=lambda: iter([
            {"role": "user", "content": "耳はどうする？"},
            {"role": "assistant", "content": "三角にしよう"},
        ]),
    )
    src = tmp_path / "export.json"
    src.write_text("{}", encoding="utf-8")
    settings = replace(load_settings(), memory_enabled=True)

    def _adapter(pid, **kwargs):
        return SAIMemoryAdapter(pid, persona_dir=tmp_path, settings=settings)

    with patch("tools.utilities.chatlog_exporter_importer.parse_exporter_file",
               lambda _p: conversation), \
            patch("saiverse_memory.SAIMemoryAdapter", _adapter), \
            patch("saiverse_memory.adapter.Embedder", embedder_cls):
        import_chatlog._run_extension_import_task("p", str(src), skip_embedding)
        thread = import_chatlog._import_embedding_threads.get("p")
        if thread is not None:
            thread.join(timeout=10)
    return import_chatlog.get_extension_import_status("p", manager=None)


@pytest.mark.parametrize("case", ["success", "failure", "skip"])
def test_import_backfill_mark_lifecycle_with_real_adapter(tmp_path, case):
    from sai_memory.embedding_migration import backfill_pending

    _reset_import_embedding_state()
    try:
        status = _run_real_extension_import(
            tmp_path, skip_embedding=(case == "skip"),
            embedder_cls=_FailingEmbedder if case == "failure" else FakeEmbedder,
        )
        assert status.success is True
        conn = _open(tmp_path / "memory.db")
        try:
            embedded = conn.execute(
                "SELECT COUNT(DISTINCT message_id) FROM message_embeddings").fetchone()[0]
            if case == "success":
                assert embedded == 2
                assert backfill_pending(conn) is None
                assert status.embedding_error is None
            elif case == "failure":
                assert embedded == 0
                assert backfill_pending(conn) is not None  # 失敗が残れば印も残る
                assert "2 message(s)" in status.embedding_error
            else:
                assert embedded == 0
                assert backfill_pending(conn) is None  # skip は印を書かない
        finally:
            conn.close()

        if case == "failure":
            # 次の起動で adapter の検査が旗を立てる
            adapter = _real_adapter(tmp_path)
            try:
                assert adapter.embed_model_changed is True
            finally:
                adapter.close()
    finally:
        _reset_import_embedding_state()


def test_api_reembed_fills_imported_messages_and_removes_backfill_mark(tmp_path):
    """残った印からの回復: バナーの再計算がベクトルの無いメッセージを埋め、印を消す。"""
    from api.routes.people import reembed
    from sai_memory.embedding_migration import backfill_pending, begin_backfill

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (ids["messages"][0],))
        conn.commit()
        begin_backfill(conn, 1)
        assert _check(conn) is True
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])

    status = _run_api_job(db_path, force=False, manager=mgr)

    assert status["progress"] == 1 and status["total"] == 1
    conn = _open(db_path)
    try:
        assert backfill_pending(conn) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE message_id = ?", (ids["messages"][0],)
        ).fetchone()[0] == 1
        assert _check(conn) is False
    finally:
        conn.close()
    assert mgr.startup_warnings == []
    reembed._reembed_status.pop("p", None)


def test_finish_reembed_keeps_backfill_mark_that_changed_during_the_job(tmp_path):
    from sai_memory.embedding_migration import backfill_pending, begin_backfill, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        begin_backfill(conn, 1)
        seen_at_start = backfill_pending(conn)
        begin_backfill(conn, 4)  # ジョブの途中で別の取り込みが仕事を足した
        assert finish_reembed(
            conn, FakeEmbedder(), EMBED_MODEL_ID, backfill_mark=seen_at_start,
        ).complete is True
        assert backfill_pending(conn) is not None
        # 値が変わっていなければ消す
        assert finish_reembed(
            conn, FakeEmbedder(), EMBED_MODEL_ID, backfill_mark=backfill_pending(conn),
        ).complete is True
        assert backfill_pending(conn) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 埋め込みモデルの読み込みに失敗しても検査は走り、旗が立つ
# ---------------------------------------------------------------------------

class _BrokenEmbedder:
    def __init__(self, *args, **kwargs):
        raise OSError("model files missing")


def test_adapter_flags_when_the_embedding_model_fails_to_load(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="saiverse_memory.adapter"):
        adapter = _real_adapter(tmp_path, embedder_cls=_BrokenEmbedder)
    try:
        assert adapter.embedder is None
        assert adapter.embed_model_changed is True
        assert any("not loaded" in r.getMessage() for r in caplog.records)
        # 新品の DB でも検査そのものは走り、名札を記録している
        assert get_embed_metadata(adapter.conn, "embed_model") == EMBED_MODEL_ID
    finally:
        adapter.close()
