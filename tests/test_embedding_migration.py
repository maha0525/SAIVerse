"""埋め込みモデル移行の機構 (docs/intent/embedding_gemma2_migration.md §2) のテスト。

押さえること:

- 起動時の検出が 4 系統 (メッセージ / Chronicle / Memopedia ページ / Fragment)
  のどれに古い長さのベクトルがあっても旗を立てる。ベクトルが一つも無い新品の
  memory.db では旗を立てず、いまのモデルを記録する。
- 再計算 API のジョブが 3 系統の古いベクトルを消してその場で作り直し、
  メッセージも作り直し、``embed_model`` を記録し、そのペルソナの警告を下ろす。
- CLI も同じ範囲を直し、``embed_model`` を記録する。小さい次元への再計算は拒む。
- 会話ログのインポートは、取り込み中は埋め込まず、各メッセージを空のベクトル行
  と同じトランザクションで保存する (skip は何も書かない)。空の行は「古いベクトル」
  なので、裏の埋め込みはペルソナの古いベクトルを作り直すだけで埋まり、裏が死ん
  でも起動時の検査が旗を立て、再計算が埋める。取り込み自体は前回の埋め込みを
  待たず、待つのは裏の埋め込みのスレッドだけ。失敗は状態に残す。保存できなかった
  メッセージがあれば取り込みは成功と言わない。
- 本文があるのにベクトルを作れなかったメッセージ (embedder が無い・計算や保存の
  失敗、追加・編集・手動追加のどれでも) は空の行を残し、起動時の検査と再計算が拾う。
- 再計算の最終確認は全行を見る。完了 (古い行なし・編集で古いベクトルのまま
  残ったメッセージなし・確定の錠の中で数えた空の行なし・印が自分のもの) の
  ときだけ名札と印を同じトランザクション
  で確定し、未完了なら両方そのままで、次の実行が全再計算に昇格して収束する。
  ベクトルの無い Chronicle 等の行は完了を妨げない (次の Metabolism が埋める)。
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

import contextlib
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

def _reset_import_embedding_state():
    from api.routes.people import import_chatlog

    with import_chatlog._import_embedding_lock:
        import_chatlog._import_embedding_status.clear()
        import_chatlog._import_embedding_threads.clear()
    import_chatlog._extension_import_status.pop("p", None)
    import_chatlog._official_import_status.pop("p", None)


class _FakeImportAdapter:
    """取り込みの記録だけをする偽 adapter。"""

    instances: list = []

    def __init__(self, persona_id, **kwargs):
        self.payloads = []
        self.reembed_calls = 0
        self.closed = 0
        _FakeImportAdapter.instances.append(self)

    def append_persona_message(self, payload, thread_suffix=None):
        self.payloads.append(dict(payload))
        return f"m{len(self.payloads)}"

    def set_thread_title(self, *args):
        pass

    def reembed_stale_messages(self, *, progress_callback=None):
        self.reembed_calls += 1
        n = len(self.payloads)
        if progress_callback:
            progress_callback(n, n)
        return {"embedded": n, "dropped": 0, "changed": 0, "total": n}

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("skip_embedding", [False, True])
def test_extension_import_embeds_after_import_in_background(tmp_path, skip_embedding):
    from api.routes.people import import_chatlog
    from saiverse_memory.adapter import EMBEDDING_DEFERRED

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
        assert import_chatlog._extension_import_status["p"]["success"] is True
        assert adapter.closed == 1
        status = import_chatlog.get_extension_import_status("p", manager=None)
        if skip_embedding:
            # skip は空の行も書かせない (取り込み中はどちらの選択でも埋め込まない)
            assert [p["embedding_chunks"] for p in adapter.payloads] == [0, 0]
            assert thread is None and adapter.reembed_calls == 0
            assert status.embedding_running is None
        else:
            assert [p["embedding_chunks"] for p in adapter.payloads] == [EMBEDDING_DEFERRED] * 2
            assert adapter.reembed_calls == 1
            assert status.embedding_running is False
            assert status.embedding_progress == 2 and status.embedding_total == 2
            assert status.embedding_error is None
    finally:
        _reset_import_embedding_state()


def test_official_import_embeds_after_import_in_background():
    from api.routes.people import import_chatlog
    from saiverse_memory.adapter import EMBEDDING_DEFERRED

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
        assert [p["embedding_chunks"] for p in adapter.payloads] == [EMBEDDING_DEFERRED]
        assert import_chatlog._official_import_status["p"]["success"] is True
        assert adapter.reembed_calls == 1 and adapter.closed == 1
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
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=None)
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
# インポートの裏の埋め込み (状態は表示だけ。正しさは DB の空の行が持つ)
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


def test_skip_import_does_not_show_or_reset_the_embedding_status(tmp_path):
    """skip の取り込みは裏の埋め込みを見せず、そのペルソナの状態も消さない。"""
    from api.routes.people import import_chatlog

    _FakeImportAdapter.instances = []
    _reset_import_embedding_state()
    earlier = {
        "embedding_running": False, "embedding_progress": 99, "embedding_total": 99,
        "embedding_failed": 0, "embedding_error": "old",
    }
    import_chatlog._import_embedding_status["p"] = dict(earlier)
    import_chatlog._official_import_status["p"] = {
        "running": False, "success": True, "message": "x", "embedding": True,
    }
    try:
        _run_extension_import(tmp_path, skip_embedding=True)
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.success is True
        assert status.embedding_progress is None and status.embedding_error is None
        # 埋め込みを裏へ回した取り込み (公式) は、そのペルソナの状態を見せる
        official = import_chatlog.get_official_import_status("p", manager=None)
        assert official.embedding_error == "old" and official.embedding_progress == 99
        assert import_chatlog._import_embedding_status["p"] == earlier
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
        assert adapter.reembed_calls == 0
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.embedding_running is True
        assert status.embedding_progress == 0 and status.embedding_total == 1

        release.set()
        mine.join(timeout=10)
        assert adapter.reembed_calls == 1
        status = import_chatlog.get_extension_import_status("p", manager=None)
        assert status.embedding_running is False and status.embedding_progress == 1
    finally:
        release.set()
        _reset_import_embedding_state()


def test_import_embeddings_of_one_persona_run_one_at_a_time(tmp_path):
    """二つの取り込みの埋め込みは一本の列で順に走り、状態を書くのは最後のスレッドだけ。"""
    import threading

    from api.routes.people import import_chatlog

    _reset_import_embedding_state()
    order = []
    gate = threading.Event()

    class _SlowAdapter(_FakeImportAdapter):
        def __init__(self, name):
            super().__init__("p")
            self.name = name

        def reembed_stale_messages(self, *, progress_callback=None):
            if self.name == "first":
                gate.wait(10)
            order.append(self.name)
            if progress_callback:
                progress_callback(7, 7)
            return {"embedded": 7, "dropped": 0, "changed": 0, "total": 7}

    first, second = _SlowAdapter("first"), _SlowAdapter("second")
    try:
        assert import_chatlog._launch_import_embedding("p", first, 1) is True
        assert import_chatlog._launch_import_embedding("p", second, 2) is True
        t2 = import_chatlog._import_embedding_threads["p"]
        assert import_chatlog._import_embedding_status["p"] == {
            "embedding_running": True, "embedding_progress": 0, "embedding_total": 2,
        }

        gate.set()
        t2.join(timeout=10)
        assert order == ["first", "second"]  # 同じペルソナでは二本同時に走らない
        assert first.closed == 1 and second.closed == 1
        status = import_chatlog._import_embedding_status["p"]
        assert status["embedding_running"] is False and status["embedding_progress"] == 7
    finally:
        gate.set()
        _reset_import_embedding_state()


class _ReembedAdapter:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.closed = 0

    def reembed_stale_messages(self, *, progress_callback=None):
        if self._error is not None:
            raise self._error
        return self._result

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("make_adapter, error_part", [
    (lambda: _ReembedAdapter(error=RuntimeError("disk full\nsecond line")),
     "RuntimeError: disk full"),
    (lambda: _ReembedAdapter(error=RuntimeError("Embedding model is not available; 2 ...")),
     "not available"),
    (lambda: _ReembedAdapter(result={"embedded": 1, "dropped": 0, "changed": 1, "total": 2}),
     "1 message(s) were edited"),
    (lambda: _ReembedAdapter(result={"embedded": 2, "dropped": 0, "changed": 0, "total": 2}),
     None),
])
def test_import_embedding_keeps_failures_in_status(make_adapter, error_part):
    import threading

    from api.routes.people import import_chatlog

    _reset_import_embedding_state()
    adapter = make_adapter()
    try:
        import_chatlog._import_embedding_threads["p"] = threading.current_thread()
        import_chatlog._embed_imported_messages("p", adapter)
        status = import_chatlog._import_embedding_status["p"]
        assert status["embedding_running"] is False
        if error_part is None:
            assert "embedding_error" not in status
            assert status["embedding_progress"] == 2 and status["embedding_total"] == 2
        else:
            assert error_part in status["embedding_error"]
            assert "\n" not in status["embedding_error"]
        assert adapter.closed == 1
    finally:
        _reset_import_embedding_state()


def test_import_embedding_shows_error_when_listing_targets_fails(tmp_path):
    """古いベクトルの一覧づくりが失敗したら、綺麗に完了したことにせず状態に出す。"""
    import threading

    from api.routes.people import import_chatlog

    _reset_import_embedding_state()
    adapter = _real_adapter(tmp_path)
    try:
        import_chatlog._import_embedding_threads["p"] = threading.current_thread()
        with patch("sai_memory.embedding_migration.message_reembed_targets",
                   side_effect=sqlite3.OperationalError("database disk image is malformed")):
            import_chatlog._embed_imported_messages("p", adapter)
        status = import_chatlog._import_embedding_status["p"]
        assert status["embedding_running"] is False
        assert "malformed" in status["embedding_error"]
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
        token = begin_reembed(conn, EMBED_MODEL_ID)
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY).startswith(EMBED_MODEL_ID)

        assert _check(conn) is True
        # 新品と誤認して名札を書き換えない
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"

        # ジョブが最後まで走れば印は消え、旗も立たない
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token).complete is True
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
        token = begin_reembed(conn, EMBED_MODEL_ID)
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token)
        assert outcome.complete is False
        assert outcome.stale_tables == ["arasuji_embeddings"]
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "intfloat/multilingual-e5-small"
    finally:
        conn.close()


def test_finish_reembed_completes_with_rows_waiting_for_a_vector(tmp_path):
    """ベクトルの無い Chronicle 等の行は完了を妨げない (次の Metabolism が埋める)。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        token = begin_reembed(conn, EMBED_MODEL_ID)
        conn.execute("DELETE FROM arasuji_embeddings")  # Chronicle Lv1 がベクトル待ち
        conn.commit()
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token)
        assert outcome.complete is True
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
    finally:
        conn.close()


def test_finish_reembed_incomplete_when_a_message_kept_its_old_vector(tmp_path):
    """編集で古いベクトルのまま残ったメッセージがあれば、名札も印も動かさない。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        token = begin_reembed(conn, EMBED_MODEL_ID)
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token, skipped=1)
        assert outcome.complete is False
        assert outcome.skipped == 1 and "Skipped 1" in outcome.describe()
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert _check(conn) is True
    finally:
        conn.close()


def test_finish_reembed_does_not_remove_another_jobs_mark(tmp_path):
    """並走した二つの再計算: 先に終わった方は、後から始まった方の印を消さない。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        first = begin_reembed(conn, EMBED_MODEL_ID)
        second = begin_reembed(conn, EMBED_MODEL_ID)  # 別のプロセス (CLI) が始めた
        assert first != second

        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=first)
        assert outcome.complete is False and outcome.other_job is True
        assert "Another re-embed" in outcome.describe()
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY).endswith(second)
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert conn.in_transaction is False

        # 後の方は自分の印を消して確定する
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=second).complete is True
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        # 印がもう無ければ、遅れて終わった方も閉じたものとして扱う
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=first).complete is True
    finally:
        conn.close()


def test_finish_reembed_commits_label_and_mark_in_one_transaction(tmp_path):
    """印の削除が失敗したら、名札の更新も巻き戻る (同じトランザクションで確定する)。"""
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        set_embed_metadata(conn, "embed_model", "old-label")
        token = begin_reembed(conn, EMBED_MODEL_ID)
        conn.execute(
            f"""
            CREATE TRIGGER refuse_mark_delete BEFORE DELETE ON embed_metadata
            WHEN OLD.key = '{MIGRATION_IN_PROGRESS_KEY}'
            BEGIN SELECT RAISE(ABORT, 'refused'); END
            """
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token)
        assert conn.in_transaction is False
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None

        conn.execute("DROP TRIGGER refuse_mark_delete")
        conn.commit()
        assert finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token).complete is True
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
    finally:
        conn.close()


class _EditOnceEmbedder(FakeEmbedder):
    """API のジョブが作る Embedder の代役。trigger を含む本文の embed の最中に別の接続で編集する。"""

    edit = None  # (db_path, sql, params, trigger) を入れると 1 回だけ実行する

    def embed(self, texts, *, is_query=False):
        pending = _EditOnceEmbedder.edit
        if pending is not None and any(pending[3] in t for t in texts):
            (db_path, sql, params, _), _EditOnceEmbedder.edit = pending, None
            other = sqlite3.connect(str(db_path))
            try:
                other.execute(sql, params)
                other.commit()
            finally:
                other.close()
        return super().embed(texts, is_query=is_query)


def test_api_job_with_skipped_row_stays_incomplete_then_converges(tmp_path):
    """編集で古いベクトルのまま残ったメッセージがあると名札・印は動かず警告も残る。

    再実行は全再計算に昇格して収束する。
    """
    from api.routes.people import reembed

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, OLD_DIM)
        set_embed_metadata(conn, "embed_model", "intfloat/multilingual-e5-small")
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])

    _EditOnceEmbedder.edit = (
        db_path, "UPDATE messages SET content = ? WHERE id = ?",
        ("書き換えた", ids["messages"][0]), "猫ロボットの話をしよう",
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
        adapter.append_persona_message(
            {"role": "user", "content": "取り込んだ本文", "embedding_chunks": 0})
        from sai_memory.memory import storage

        # backfill は一覧の後、各メッセージでロック内の読み直しを 1 回だけ行う
        seen: list = []
        real = storage.get_message

        def _probe(c, message_id):
            seen.append(_try_write_from_other_connection(db_path))
            return real(c, message_id)

        with patch.object(storage, "get_message", _probe):
            result = adapter.backfill_missing_message_embeddings()
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
        result = adapter.backfill_missing_message_embeddings()
        assert result["changed"] == 1 and result["embedded"] == 0
        assert adapter.conn.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE message_id = ?", (mid2,)
        ).fetchone()[0] == 0
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# インポートの後回しの埋め込み: 空のベクトル行
# ---------------------------------------------------------------------------

def _vector_rows(conn, message_id):
    return [v for (v,) in conn.execute(
        "SELECT vector FROM message_embeddings WHERE message_id = ?", (message_id,))]


def test_deferred_append_writes_an_empty_row_and_skip_writes_nothing(tmp_path):
    from sai_memory.memory.storage import DEFERRED_VECTOR
    from saiverse_memory.adapter import EMBEDDING_DEFERRED

    adapter = _real_adapter(tmp_path)
    try:
        deferred = adapter.append_persona_message(
            {"role": "user", "content": "あとで埋める本文", "embedding_chunks": EMBEDDING_DEFERRED})
        skipped = adapter.append_persona_message(
            {"role": "user", "content": "埋めない本文", "embedding_chunks": 0})
        blank = adapter.append_persona_message(
            {"role": "user", "content": "   ", "embedding_chunks": EMBEDDING_DEFERRED})
        normal = adapter.append_persona_message({"role": "user", "content": "その場で埋める本文"})
        assert _vector_rows(adapter.conn, deferred) == [DEFERRED_VECTOR]
        assert _vector_rows(adapter.conn, skipped) == []
        assert _vector_rows(adapter.conn, blank) == []  # 本文の無い行は埋め込みの対象外
        assert [len(json.loads(v)) for v in _vector_rows(adapter.conn, normal)] == [EMBED_DIM]
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()


def test_deferred_append_stores_message_and_empty_row_as_one_write(tmp_path):
    """空の行が書けなければメッセージも入らない (後で誰も埋めない行を作らない)。"""
    from saiverse_memory.adapter import EMBEDDING_DEFERRED

    adapter = _real_adapter(tmp_path)
    try:
        with patch("saiverse_memory.adapter.write_deferred_message_embedding",
                   side_effect=sqlite3.OperationalError("disk I/O error")):
            mid = adapter.append_persona_message(
                {"role": "user", "content": "取り込む本文", "embedding_chunks": EMBEDDING_DEFERRED})
        assert mid is None
        assert adapter.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()


class _FailingEmbedder(FakeEmbedder):
    def embed(self, texts, *, is_query=False):
        raise RuntimeError("embedding failed")


_IMPORT_PAYLOADS = (
    {"role": "user", "content": "耳はどうする？"},
    {"role": "assistant", "content": "三角にしよう"},
    {"role": "user", "content": "しっぽは？"},
)


def _run_real_extension_import(tmp_path, *, skip_embedding, embedder_cls=FakeEmbedder,
                               fail_on_append=None, launch=True):
    from api.routes.people import import_chatlog
    from saiverse_memory.adapter import SAIMemoryAdapter

    conversation = SimpleNamespace(
        identifier="conv1", title="猫ロボットの相談",
        iter_memory_payloads=lambda: iter([dict(p) for p in _IMPORT_PAYLOADS]),
    )
    src = tmp_path / "export.json"
    src.write_text("{}", encoding="utf-8")
    settings = replace(load_settings(), memory_enabled=True)

    def _adapter(pid, **kwargs):
        adapter = SAIMemoryAdapter(pid, persona_dir=tmp_path, settings=settings)
        if fail_on_append is not None:
            real_append = adapter.append_persona_message
            calls = {"n": 0}

            def _append(payload, thread_suffix=None):
                calls["n"] += 1
                if calls["n"] == fail_on_append:
                    raise RuntimeError("import interrupted")
                return real_append(payload, thread_suffix=thread_suffix)

            adapter.append_persona_message = _append
        return adapter

    patches = [
        patch("tools.utilities.chatlog_exporter_importer.parse_exporter_file",
              lambda _p: conversation),
        patch("saiverse_memory.SAIMemoryAdapter", _adapter),
        patch("saiverse_memory.adapter.Embedder", embedder_cls),
    ]
    if not launch:
        # 裏の埋め込みが走らないまま終わる (プロセスが死んだ状態の再現)
        patches.append(patch.object(import_chatlog, "_launch_import_embedding",
                                    lambda *a, **k: False))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        import_chatlog._run_extension_import_task("p", str(src), skip_embedding)
        thread = import_chatlog._import_embedding_threads.get("p")
        if thread is not None:
            thread.join(timeout=10)
    return import_chatlog.get_extension_import_status("p", manager=None)


def _deferred_count(db_path):
    from sai_memory.memory.storage import count_deferred_message_embeddings

    conn = _open(db_path)
    try:
        return count_deferred_message_embeddings(conn)
    finally:
        conn.close()


def _flag_on_open(tmp_path) -> bool:
    adapter = _real_adapter(tmp_path)
    try:
        return adapter.embed_model_changed
    finally:
        adapter.close()


def test_background_embedding_fills_the_empty_rows(tmp_path):
    _reset_import_embedding_state()
    try:
        status = _run_real_extension_import(tmp_path, skip_embedding=False)
        assert status.success is True and status.embedding_error is None
        assert status.embedding_progress == 3
        db_path = tmp_path / "memory.db"
        assert _deferred_count(db_path) == 0
        conn = _open(db_path)
        try:
            assert _dims(conn, "message_embeddings") == {EMBED_DIM}
            assert conn.execute(
                "SELECT COUNT(DISTINCT message_id) FROM message_embeddings").fetchone()[0] == 3
        finally:
            conn.close()
        assert _flag_on_open(tmp_path) is False
    finally:
        _reset_import_embedding_state()


def test_skip_import_leaves_no_rows_and_no_flag(tmp_path):
    _reset_import_embedding_state()
    try:
        status = _run_real_extension_import(tmp_path, skip_embedding=True)
        assert status.success is True and status.embedding_running is None
        conn = _open(tmp_path / "memory.db")
        try:
            assert conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0] == 0
        finally:
            conn.close()
        assert _flag_on_open(tmp_path) is False
    finally:
        _reset_import_embedding_state()


def test_failed_background_embedding_leaves_rows_that_flag_at_startup(tmp_path):
    _reset_import_embedding_state()
    try:
        status = _run_real_extension_import(
            tmp_path, skip_embedding=False, embedder_cls=_FailingEmbedder,
        )
        assert status.success is True
        assert "embedding failed" in status.embedding_error
        assert _deferred_count(tmp_path / "memory.db") == 3
        assert _flag_on_open(tmp_path) is True
    finally:
        _reset_import_embedding_state()


def test_import_interrupted_midway_keeps_an_empty_row_per_saved_message(tmp_path):
    _reset_import_embedding_state()
    try:
        status = _run_real_extension_import(tmp_path, skip_embedding=False, fail_on_append=3)
        assert status.success is False
        conn = _open(tmp_path / "memory.db")
        try:
            saved = [r[0] for r in conn.execute("SELECT id FROM messages")]
            assert len(saved) == 2
            for mid in saved:
                assert _vector_rows(conn, mid) == ["[]"]
        finally:
            conn.close()
        assert _flag_on_open(tmp_path) is True
    finally:
        _reset_import_embedding_state()


def test_empty_rows_in_the_middle_still_flag_at_startup(tmp_path):
    """裏が走らずに終わり、空の行が表の真ん中に埋もれても起動時の検査は旗を立てる。"""
    from sai_memory.embedding_migration import sample_stale_vector_tables

    _reset_import_embedding_state()
    try:
        adapter = _real_adapter(tmp_path)
        try:
            for i in range(10):
                adapter.append_persona_message({"role": "user", "content": f"前の会話 {i}"})
        finally:
            adapter.close()
        status = _run_real_extension_import(tmp_path, skip_embedding=False, launch=False)
        assert status.success is True
        adapter = _real_adapter(tmp_path)
        try:
            for i in range(10):
                adapter.append_persona_message({"role": "user", "content": f"後の会話 {i}"})
        finally:
            adapter.close()

        conn = _open(tmp_path / "memory.db")
        try:
            assert sample_stale_vector_tables(conn, EMBED_DIM) == []  # 標本では見えない位置
        finally:
            conn.close()
        assert _deferred_count(tmp_path / "memory.db") == 3
        assert _flag_on_open(tmp_path) is True
    finally:
        _reset_import_embedding_state()


def test_reembed_without_include_missing_fills_empty_rows(tmp_path):
    from sai_memory.embedding_migration import message_reembed_targets, reembed_messages
    from sai_memory.memory.storage import write_deferred_message_embedding

    conn = _open(tmp_path / "memory.db")
    try:
        ids = _seed(conn, EMBED_DIM)
        m1 = ids["messages"][0]
        write_deferred_message_embedding(conn, m1)
        assert message_reembed_targets(
            conn, EMBED_DIM, force=False, include_missing=False) == {m1}
        result = reembed_messages(conn, FakeEmbedder(), [m1], chunk_min=120, chunk_max=480)
        assert result["embedded"] == 1
        assert [len(json.loads(v)) for v in _vector_rows(conn, m1)] == [EMBED_DIM]
    finally:
        conn.close()


def test_api_reembed_fills_empty_rows_and_lifts_the_warning(tmp_path):
    """裏が終わらなかった取り込みからの回復: バナーの再計算が空の行を埋める。"""
    from api.routes.people import reembed
    from sai_memory.memory.storage import write_deferred_message_embedding

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", EMBED_MODEL_ID)
        write_deferred_message_embedding(conn, ids["messages"][0])
        assert _check(conn) is True
    finally:
        conn.close()
    mgr = _manager_with_warning(["p"])

    status = _run_api_job(db_path, force=False, manager=mgr)

    assert status["progress"] == 1 and status["total"] == 1
    assert _deferred_count(db_path) == 0
    conn = _open(db_path)
    try:
        assert _dims(conn, "message_embeddings") == {EMBED_DIM}
        assert _check(conn) is False
    finally:
        conn.close()
    assert mgr.startup_warnings == []
    reembed._reembed_status.pop("p", None)


def test_semantic_recall_skips_empty_rows_without_a_warning(tmp_path, caplog):
    import logging

    from sai_memory.memory.recall import semantic_recall, semantic_recall_groups
    from sai_memory.memory.storage import write_deferred_message_embedding

    conn = _open(tmp_path / "memory.db")
    try:
        ids = _seed(conn, EMBED_DIM)
        m1, m2 = ids["messages"]
        write_deferred_message_embedding(conn, m1)  # 埋め込み待ち
        replace_message_embeddings(conn, m2, [_vec(OLD_DIM)])  # 古いモデルの行
        kwargs = dict(thread_id="p:__persona__", resource_id=None, topk=5,
                      range_before=0, range_after=0, scope="thread")
        for fn in (semantic_recall, semantic_recall_groups):
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                fn(conn, FakeEmbedder(), "猫", **kwargs)
            warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
            assert len(warned) == 1 and m2 in warned[0]  # 古い行だけ警告する
            assert any("1 message(s) still waiting" in r.getMessage()
                       for r in caplog.records if r.levelno == logging.DEBUG)
    finally:
        conn.close()


def test_backfill_reports_a_failed_listing_instead_of_nothing_missing(tmp_path):
    adapter = _real_adapter(tmp_path)
    try:
        with patch("sai_memory.memory.storage.get_messages_without_embeddings",
                   side_effect=sqlite3.OperationalError("database disk image is malformed")):
            result = adapter.backfill_missing_message_embeddings()
        assert result["aborted"] is True
        assert "malformed" in result["error"]
    finally:
        adapter.close()


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



# ---------------------------------------------------------------------------
# 取り込みで保存できなかったメッセージは失敗として数える
# ---------------------------------------------------------------------------

def _fail_nth_empty_row(n):
    """n 回目の空の行の INSERT だけを失敗させる (それ以外は本物に通す)。"""
    from sai_memory.memory import storage

    real = storage.write_deferred_message_embedding
    calls = {"n": 0}

    def _write(conn, message_id, *, commit=True):
        calls["n"] += 1
        if calls["n"] == n:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, message_id, commit=commit)

    return patch("saiverse_memory.adapter.write_deferred_message_embedding", _write)


def test_extension_import_counts_a_message_that_could_not_be_saved(tmp_path):
    _reset_import_embedding_state()
    try:
        with _fail_nth_empty_row(2):
            status = _run_real_extension_import(tmp_path, skip_embedding=False)
        assert status.success is False
        assert "Saved 2 of 3 messages; 1 failed" in status.message
        conn = _open(tmp_path / "memory.db")
        try:
            # 失敗した一件はメッセージごと入っていない (空の行と一体の書き込み)
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            conn.close()
    finally:
        _reset_import_embedding_state()


def _run_real_official_import(tmp_path):
    from api.routes.people import import_chatlog
    from saiverse_memory.adapter import SAIMemoryAdapter

    record = SimpleNamespace(
        conversation_id="c1", identifier="c1", title="(untitled)",
        iter_memory_payloads=lambda include_roles=None: iter(
            [dict(p, metadata={}) for p in _IMPORT_PAYLOADS]),
    )
    import_chatlog._chatgpt_export_cache["k"] = {
        "export": SimpleNamespace(conversations=[record]), "tmp_path": None, "persona_id": "p",
    }
    settings = replace(load_settings(), memory_enabled=True)

    def _adapter(pid, **kwargs):
        return SAIMemoryAdapter(pid, persona_dir=tmp_path, settings=settings)

    with patch("saiverse_memory.SAIMemoryAdapter", _adapter), \
            patch("saiverse_memory.adapter.Embedder", FakeEmbedder):
        import_chatlog._run_official_import_task("p", "k", ["0"], False)
        thread = import_chatlog._import_embedding_threads.get("p")
        if thread is not None:
            thread.join(timeout=10)
    return import_chatlog.get_official_import_status("p", manager=None)


def test_official_import_counts_a_message_that_could_not_be_saved(tmp_path):
    _reset_import_embedding_state()
    try:
        with _fail_nth_empty_row(3):
            status = _run_real_official_import(tmp_path)
        assert status.success is False
        assert "Saved 2 of 3 messages; 1 failed" in status.message
        assert status.messages == 2
    finally:
        _reset_import_embedding_state()


def test_official_import_with_every_message_saved_succeeds(tmp_path):
    _reset_import_embedding_state()
    try:
        status = _run_real_official_import(tmp_path)
        assert status.success is True and status.messages == 3
        assert _deferred_count(tmp_path / "memory.db") == 0  # 裏の埋め込みが埋めた
    finally:
        _reset_import_embedding_state()


# ---------------------------------------------------------------------------
# 本文があるのにベクトルを作れなかったら、どの経路でも空の行を残す
# ---------------------------------------------------------------------------

def _assert_flagged_then_filled(tmp_path, mid):
    """空の行で起動時の検査が旗を立て、背景の埋め込みが埋めると旗が下りる。"""
    from sai_memory.memory.storage import DEFERRED_VECTOR

    adapter = _real_adapter(tmp_path)
    try:
        assert _vector_rows(adapter.conn, mid) == [DEFERRED_VECTOR]
        assert adapter.embed_model_changed is True
        result = adapter.reembed_stale_messages()
        assert result["embedded"] == 1
        assert [len(json.loads(v)) for v in _vector_rows(adapter.conn, mid)] == [EMBED_DIM]
    finally:
        adapter.close()
    assert _flag_on_open(tmp_path) is False


def test_append_without_embedder_writes_an_empty_row(tmp_path):
    from sai_memory.memory.storage import DEFERRED_VECTOR

    adapter = _real_adapter(tmp_path, embedder_cls=_BrokenEmbedder)
    try:
        assert adapter.embedder is None
        mid = adapter.append_persona_message({"role": "user", "content": "読み込み失敗中の会話"})
        skipped = adapter.append_persona_message(
            {"role": "user", "content": "埋めない本文", "embedding_chunks": 0})
        assert mid is not None
        assert _vector_rows(adapter.conn, mid) == [DEFERRED_VECTOR]
        assert _vector_rows(adapter.conn, skipped) == []  # スキップ指定は何も書かない
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()
    _assert_flagged_then_filled(tmp_path, mid)


def test_append_with_failing_embed_keeps_the_empty_row(tmp_path):
    adapter = _real_adapter(tmp_path, embedder_cls=_FailingEmbedder)
    try:
        mid = adapter.append_persona_message({"role": "user", "content": "計算に失敗する会話"})
        assert mid is not None  # 行は入ったので id は返す
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()
    _assert_flagged_then_filled(tmp_path, mid)


def test_append_whose_vector_store_fails_keeps_the_empty_row(tmp_path):
    """ベクトルの保存が途中で失敗しても巻き戻り、空の行は消えない。"""
    from sai_memory.memory.storage import DEFERRED_VECTOR

    adapter = _real_adapter(tmp_path)
    try:
        with patch("saiverse_memory.adapter.replace_message_embeddings",
                   side_effect=sqlite3.OperationalError("disk I/O error")):
            mid = adapter.append_persona_message({"role": "user", "content": "保存に失敗する会話"})
        assert mid is not None
        assert _vector_rows(adapter.conn, mid) == [DEFERRED_VECTOR]
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()


@pytest.mark.parametrize("broken", ["no_embedder", "embed_raises"])
def test_update_message_without_vectors_writes_an_empty_row(tmp_path, broken):
    adapter = _real_adapter(tmp_path)
    try:
        mid = adapter.append_persona_message({"role": "user", "content": "元の本文"})
        assert [len(json.loads(v)) for v in _vector_rows(adapter.conn, mid)] == [EMBED_DIM]
        adapter.embedder = None if broken == "no_embedder" else _FailingEmbedder()
        assert adapter.update_message(mid, new_content="書き換えた本文") is True
        assert adapter.conn.execute(
            "SELECT content FROM messages WHERE id = ?", (mid,)).fetchone()[0] == "書き換えた本文"
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()
    _assert_flagged_then_filled(tmp_path, mid)


def test_update_message_to_blank_text_leaves_no_row(tmp_path):
    adapter = _real_adapter(tmp_path)
    try:
        mid = adapter.append_persona_message({"role": "user", "content": "元の本文"})
        adapter.embedder = None
        assert adapter.update_message(mid, new_content="   ") is True
        assert _vector_rows(adapter.conn, mid) == []  # 本文の無い行は埋め込みの対象外
    finally:
        adapter.close()


def test_update_message_failure_rolls_the_edit_back(tmp_path):
    adapter = _real_adapter(tmp_path)
    try:
        mid = adapter.append_persona_message({"role": "user", "content": "元の本文"})
        before = _vector_rows(adapter.conn, mid)
        with patch("saiverse_memory.adapter.replace_message_embeddings",
                   side_effect=sqlite3.OperationalError("disk I/O error")):
            assert adapter.update_message(mid, new_content="書き換えた本文") is False
        assert adapter.conn.execute(
            "SELECT content FROM messages WHERE id = ?", (mid,)).fetchone()[0] == "元の本文"
        assert _vector_rows(adapter.conn, mid) == before
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()


@pytest.mark.parametrize("broken", ["no_embedder", "embed_raises"])
def test_manual_message_add_without_vectors_writes_an_empty_row(tmp_path, broken):
    """記憶画面からの手動追加 (adapter を通らない経路) も同じ規則に従う。"""
    from api.routes.people import memory as memory_routes
    from api.routes.people.models import CreateMessageRequest

    adapter = _real_adapter(tmp_path)
    try:
        adapter.embedder = None if broken == "no_embedder" else _FailingEmbedder()
        manager = SimpleNamespace(personas={"p": SimpleNamespace(sai_memory=adapter)})
        item = memory_routes.create_message(
            "p", "p:__persona__",
            CreateMessageRequest(role="user", content="手で足した会話"), manager=manager,
        )
        mid = item.id
        assert adapter.conn.in_transaction is False
    finally:
        adapter.close()
    _assert_flagged_then_filled(tmp_path, mid)


def test_manual_message_add_embeds_on_the_spot(tmp_path):
    from api.routes.people import memory as memory_routes
    from api.routes.people.models import CreateMessageRequest

    adapter = _real_adapter(tmp_path)
    try:
        manager = SimpleNamespace(personas={"p": SimpleNamespace(sai_memory=adapter)})
        item = memory_routes.create_message(
            "p", "p:__persona__",
            CreateMessageRequest(role="user", content="手で足した会話"), manager=manager,
        )
        assert [len(json.loads(v)) for v in _vector_rows(adapter.conn, item.id)] == [EMBED_DIM]
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# 完了確定は錠の中で空の行の件数を見る
# ---------------------------------------------------------------------------

def test_finish_reembed_incomplete_when_an_empty_row_appears_after_the_scan(tmp_path):
    """走査の後・確定の前に別の接続が空の行を足したら、名札も印も触らない。"""
    from sai_memory import embedding_migration
    from sai_memory.embedding_migration import begin_reembed, finish_reembed
    from sai_memory.memory.storage import write_deferred_message_embedding

    db_path = tmp_path / "memory.db"
    conn = _open(db_path)
    try:
        ids = _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        token = begin_reembed(conn, EMBED_MODEL_ID)
        real_scan = embedding_migration.scan_stale_vector_tables

        def _scan_then_other_writer(c, dim):
            found = real_scan(c, dim)
            other = _open(db_path)
            try:
                write_deferred_message_embedding(other, ids["messages"][0])
            finally:
                other.close()
            return found

        with patch.object(embedding_migration, "scan_stale_vector_tables",
                          _scan_then_other_writer):
            outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token)
        assert outcome.stale_tables == []  # 走査の時点では見えていなかった
        assert outcome.complete is False and outcome.deferred == 1
        assert "wait for their embeddings" in outcome.describe()
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is not None
        assert get_embed_metadata(conn, "embed_model") == "old-label"
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_finish_reembed_without_empty_rows_completes(tmp_path):
    from sai_memory.embedding_migration import begin_reembed, finish_reembed

    conn = _open(tmp_path / "memory.db")
    try:
        _seed(conn, EMBED_DIM)
        set_embed_metadata(conn, "embed_model", "old-label")
        token = begin_reembed(conn, EMBED_MODEL_ID)
        outcome = finish_reembed(conn, FakeEmbedder(), EMBED_MODEL_ID, token=token)
        assert outcome.complete is True and outcome.deferred == 0
        assert get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY) is None
        assert get_embed_metadata(conn, "embed_model") == EMBED_MODEL_ID
    finally:
        conn.close()
