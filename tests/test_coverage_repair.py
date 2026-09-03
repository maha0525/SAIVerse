"""被覆補修 (arasuji_levels.md §16) のテスト — 止め線・冷えた窓への印・窓の誕生の護り。

固定する不変条件:

- 止め線 = 温かい (TTL 内の) anchor のうち正典順で最古の位置。温かい行が無ければ
  上端なし (全域が補修対象 — ログインポートで作った anchor 行ゼロのペルソナが
  この形)。
- 見積もり (estimate_chronicle_generation_cost) と生成 (generate_chronicle の
  全量計画) は同じ止め線関数を通り、同じ数を言う。
- 止め線の切り位置はスペルの群を割らない (群ごと手前へ下げる)。
- 補修の完了後、冷えた anchor 行の窓を覆うエントリは §15 の印 (presented_raw)
  として行へ追記される。冪等 (同じ entry_id は重複追加しない)。温かい行と、
  anchor を跨ぐエントリは触らない。
- 新しい anchor 行が被覆済み領域の上に生まれるときは、初期圧縮区間として
  同じ印を持って生まれる (§16-3)。
- あらすじエントリの削除で、その範囲は次の見積もりから「未被覆」として
  数え直される (§16-2: 消して再編纂の運用)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.coverage_repair import (
    CeilingResolutionError,
    coverage_marks_for_window,
    mark_covered_cold_windows,
    resolve_compile_ceiling,
)
from sea.session_lifecycle import SessionLifecycle
from sea.session_window import deserialize_folds

PERSONA_ID = "tester"
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _small_band_budget(monkeypatch):
    """U をテストデータの規模に合わせる。

    2026-08-31 の極小 run 吸収 (arasuji_tiny_run_absorption) で、全量計画の
    材料 0.5U 未満の run は単独編纂されなくなった。本ファイルの関心は止め線と
    印であって run の大きさではないので、U を小さくして従来どおり全 run が
    通常編纂に乗る前提を保つ (メッセージ 1 通 ≈ 205 字 ≥ U=200)。
    """
    monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", "200")


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        persona_path = tmp_path / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True)
        a = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID,
        )
        yield a
        try:
            a.close()
        except Exception:
            pass


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _make_lifecycle(session_factory, personas=None):
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas if personas is not None else {},
    )
    return SessionLifecycle(SimpleNamespace(), manager)


def _add_messages(adapter, count, *, start_minute=0, chars=200, prefix="会話"):
    """count 件の会話メッセージを正典順 (時刻昇順) で積む。id のリストを返す。"""
    ids = []
    for i in range(count):
        mid = adapter.append_persona_message({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{prefix} {i} " + "あ" * chars,
            "timestamp": (
                BASE_TIME + timedelta(minutes=start_minute + i)
            ).isoformat(),
        })
        assert mid is not None
        ids.append(mid)
    return ids


def _warm_row(lc, model, anchor_id):
    lc.upsert_anchor_entry(PERSONA_ID, model, {
        "anchor_id": anchor_id,
        "updated_at": datetime.now().isoformat(),
        "ttl_seconds": 3600,
    })


def _cold_row(lc, model, anchor_id):
    lc.upsert_anchor_entry(PERSONA_ID, model, {
        "anchor_id": anchor_id,
        "updated_at": (datetime.now() - timedelta(days=3)).isoformat(),
        "ttl_seconds": 300,
    })


def _create_entry(conn, source_ids, content="あらすじ"):
    from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
    init_arasuji_tables(conn)
    rows = conn.execute(
        "SELECT COALESCE(created_at, 0) FROM messages WHERE id IN ({})".format(
            ",".join("?" for _ in source_ids)
        ),
        [str(s) for s in source_ids],
    ).fetchall()
    times = sorted(int(r[0]) for r in rows) or [0]
    return create_entry(
        conn, level=1, content=content,
        source_ids=[str(s) for s in source_ids],
        start_time=times[0], end_time=times[-1],
        source_count=len(source_ids), message_count=len(source_ids),
        extra_metadata={"digest_origin": "batch", "coverage_chars": 100},
    )


# ---------------------------------------------------------------------------
# 止め線 (compile ceiling)
# ---------------------------------------------------------------------------


class TestResolveCompileCeiling:
    def test_no_anchor_rows_means_no_ceiling(self, adapter, session_factory):
        """anchor 行ゼロ (ログインポート直後の新規ペルソナ) → 上端なし = 全域編纂可。"""
        _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        assert resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn) is None

    def test_ceiling_is_the_oldest_warm_anchor(self, adapter, session_factory):
        """複数 model: 温かい行のうち正典順で最古の anchor が上端。冷えた行は無視。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", ids[2])
        _warm_row(lc, "model-b", ids[3])
        _cold_row(lc, "model-c", ids[0])  # 冷えた行は上端に影響しない
        ceiling = resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn)
        assert ceiling is not None
        assert ceiling.message_id == ids[2]
        assert ceiling.model_key == "model-a"

    def test_all_cold_means_no_ceiling(self, adapter, session_factory):
        ids = _add_messages(adapter, 3)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[1])
        assert resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn) is None

    def test_warm_anchor_at_missing_message_fails_resolution(
        self, adapter, session_factory,
    ):
        """温かい行の anchor が messages に無い → 解決失敗 (fail-closed)。

        「制約なし」へ潰すと、位置の分からない温かい窓の下を全量計画が掘る
        (Codex レビュー 2026-08-31 採用 1)。
        """
        ids = _add_messages(adapter, 3)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", "ghost-message")
        _warm_row(lc, "model-b", ids[2])
        with pytest.raises(CeilingResolutionError):
            resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn)
        # 全量計画 (generate_chronicle) も failed で止まる
        executor = _CapturingExecutor()
        assert _generate(lc, _persona(adapter), executor) == "failed"
        assert executor.chunks == []  # 1 件も編纂していない

    def test_anchor_row_read_failure_fails_resolution(
        self, adapter, session_factory,
    ):
        """行の読み取り失敗は「上端なし」へ潰さない — 解決失敗 (fail-closed)。"""
        _add_messages(adapter, 3)
        lc = _make_lifecycle(session_factory)
        with patch.object(
            lc, "load_anchor_entries_strict",
            side_effect=RuntimeError("db down"),
        ):
            with pytest.raises(CeilingResolutionError):
                resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn)
            executor = _CapturingExecutor()
            assert _generate(lc, _persona(adapter), executor) == "failed"
            assert executor.chunks == []

    def test_null_created_at_orders_before_all_real_timestamps(
        self, adapter, session_factory,
    ):
        """created_at NULL の行は正典順で全ての実時刻 (0 含む) より前 —
        rowid が後でも上端の比較は共有述語と同じ順序になる (採用 2)。"""
        ids = _add_messages(adapter, 3)
        # ids[0]=ts0 (rowid 小), ids[1]=NULL (rowid 大), ids[2]=ts100
        adapter.conn.execute(
            "UPDATE messages SET created_at = 0 WHERE id = ?", (ids[0],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = NULL WHERE id = ?", (ids[1],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = 100 WHERE id = ?", (ids[2],))
        adapter.conn.commit()
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", ids[0])  # ts0
        _warm_row(lc, "model-b", ids[1])  # NULL — 正典順ではこちらが最古
        ceiling = resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn)
        assert ceiling is not None
        assert ceiling.message_id == ids[1]


class TestClipMessagesBeforePosition:
    def test_keeps_strictly_older_messages(self, adapter):
        from sai_memory.memory.storage import (
            clip_messages_before_position,
            get_message_position,
            get_messages_for_chronicle,
        )
        ids = _add_messages(adapter, 4)
        msgs = get_messages_for_chronicle(adapter.conn)
        pos = get_message_position(adapter.conn, ids[2])
        clipped = clip_messages_before_position(adapter.conn, msgs, *pos)
        assert [m.id for m in clipped] == ids[:2]

    def test_boundary_does_not_split_a_spell_group(self, adapter):
        """切り位置がスペルの群の内側に落ちたら、群の先頭まで下げる (§4-3 の同族)。"""
        from sai_memory.memory.storage import (
            clip_messages_before_position,
            get_message_position,
            get_messages_for_chronicle,
        )
        ids = _add_messages(adapter, 5)
        # ids[1] を群の起点、ids[2]・ids[3] をその群のメンバーにする
        for member in (ids[2], ids[3]):
            adapter.conn.execute(
                "UPDATE messages SET spell_origin_id = ? WHERE id = ?",
                (ids[1], member),
            )
        adapter.conn.commit()
        msgs = get_messages_for_chronicle(adapter.conn)
        # 上端を ids[3] に置く → 素の境界は index 3 だが、群 (index 1..3) の
        # 内側なので index 1 (群の起点) まで下がる
        pos = get_message_position(adapter.conn, ids[3])
        clipped = clip_messages_before_position(adapter.conn, msgs, *pos)
        assert [m.id for m in clipped] == ids[:1]

    def test_null_and_zero_created_at_follow_canonical_order(self, adapter):
        """NULL と 0 の created_at が混在しても、切断は共有述語の正典順に一致する。

        NULL 行の rowid が 0 行より後でも、NULL は「全ての実時刻より前」。
        COALESCE(created_at,0) だと rowid の後勝ちで 0 行の後ろに化ける
        (Codex レビュー 2026-08-31 採用 2)。
        """
        from sai_memory.memory.storage import (
            clip_messages_before_position,
            get_message_position,
            get_messages_for_chronicle,
        )
        ids = _add_messages(adapter, 3)
        adapter.conn.execute(
            "UPDATE messages SET created_at = 0 WHERE id = ?", (ids[0],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = NULL WHERE id = ?", (ids[1],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = 100 WHERE id = ?", (ids[2],))
        adapter.conn.commit()
        msgs = get_messages_for_chronicle(adapter.conn)
        # 読み出し順も正典順: NULL → 0 → 100
        assert [m.id for m in msgs] == [ids[1], ids[0], ids[2]]
        # 止め線 = 0 の行 → 厳密に前は NULL 行だけ
        pos = get_message_position(adapter.conn, ids[0])
        assert pos == (0, 1)
        clipped = clip_messages_before_position(adapter.conn, msgs, *pos)
        assert [m.id for m in clipped] == [ids[1]]

    def test_window_inclusion_follows_canonical_order_with_nulls(self, adapter):
        """窓 (anchor 以降) の包含判定も同じ正典順 — NULL 行は 0 anchor の窓に
        入らない (rowid が後でも)。"""
        ids = _add_messages(adapter, 3)
        adapter.conn.execute(
            "UPDATE messages SET created_at = 0 WHERE id = ?", (ids[0],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = NULL WHERE id = ?", (ids[1],))
        adapter.conn.execute(
            "UPDATE messages SET created_at = 100 WHERE id = ?", (ids[2],))
        adapter.conn.commit()
        # anchor = 0 の行。窓 = {ids[0], ids[2]} — NULL 行は窓の外 (手前)。
        inside = _create_entry(adapter.conn, [ids[0], ids[2]])
        _create_entry(adapter.conn, [ids[1]])  # 窓の外のみ → 印の対象外
        marks = coverage_marks_for_window(adapter.conn, ids[0], [])
        assert [f.chronicle_entry_ids for f in marks] == [[inside.id]]
        assert marks[0].message_ids == [ids[0], ids[2]]


# ---------------------------------------------------------------------------
# 見積もりと生成の範囲一致 (同じ止め線関数を通ること)
# ---------------------------------------------------------------------------


class _CapturingExecutor:
    """execute_plan の代替 — 計画のチャンク内容を捕まえ、entry を実際に書く。"""

    def __init__(self, write_entries=False):
        self.chunks = []
        self.write_entries = write_entries

    def __call__(self, plan, client, conn, **kwargs):
        from sai_memory.arasuji.executor import ExecutionResult
        self.chunks.append([list(c.message_ids) for c in plan.chunks])
        created = []
        if self.write_entries:
            for chunk in plan.chunks:
                created.append(
                    _create_entry(conn, chunk.message_ids, content="生成あらすじ"),
                )
        return ExecutionResult(created=created)


def _generate(lifecycle, persona, executor):
    with patch(
        "saiverse.model_configs.find_model_config",
        return_value=("mock-model", {"provider": "mock", "context_length": 1000}),
    ), patch(
        "llm_clients.factory.get_llm_client", return_value=SimpleNamespace(),
    ), patch(
        "sai_memory.arasuji.executor.execute_plan", executor,
    ), patch(
        "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
    ), patch(
        "sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0,
    ), patch(
        "sai_memory.memory.entity_extractor.make_batch_callback",
        side_effect=RuntimeError("skip entity extraction"),
    ):
        return lifecycle.generate_chronicle(persona, force=True)


def _estimate(lc, adapter):
    """API ルート (cost-estimate) と同じ組み立て: 同じ止め線関数で絞って見積もる。"""
    from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
    from sai_memory.arasuji.storage import init_arasuji_tables
    init_arasuji_tables(adapter.conn)
    ceiling = resolve_compile_ceiling(lc, PERSONA_ID, adapter.conn)
    return estimate_chronicle_generation_cost(
        adapter.conn,
        model_name="mock-model",
        compile_before=(
            (ceiling.created_at, ceiling.rowid) if ceiling is not None else None
        ),
    )


def _persona(adapter):
    return SimpleNamespace(
        persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
        sai_memory=adapter,
    )


class TestEstimateGenerationParity:
    def test_warm_anchor_limits_both_estimate_and_generation(
        self, adapter, session_factory,
    ):
        """温かい anchor の手前だけが対象になり、見積もりと実走が同じ数を言う。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", ids[2])

        est = _estimate(lc, adapter)
        assert est.unprocessed_messages == 2

        executor = _CapturingExecutor()
        persona = _persona(adapter)
        assert _generate(lc, persona, executor) == "ok"
        compiled = [mid for chunk in executor.chunks[-1] for mid in chunk]
        assert compiled == ids[:2]
        assert len(compiled) == est.unprocessed_messages

    def test_no_anchor_rows_repairs_the_whole_history(
        self, adapter, session_factory,
    ):
        """anchor 行ゼロ (ログインポートの新規ペルソナ) → 全履歴が対象。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)

        est = _estimate(lc, adapter)
        assert est.unprocessed_messages == 4

        executor = _CapturingExecutor()
        assert _generate(lc, _persona(adapter), executor) == "ok"
        compiled = [mid for chunk in executor.chunks[-1] for mid in chunk]
        assert compiled == ids
        assert len(compiled) == est.unprocessed_messages

    def test_imported_old_logs_fall_below_the_ceiling(
        self, adapter, session_factory,
    ):
        """会話済み (温かい anchor あり) のペルソナに古いログを後からインポート
        した形 — インポート分は正典順で anchor より古いので補修対象に入る。"""
        recent = _add_messages(adapter, 2, start_minute=1000, prefix="現行会話")
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", recent[0])
        # 後からのインポート (rowid は新しいが created_at は古い)
        imported = _add_messages(adapter, 3, start_minute=0, prefix="インポート")

        est = _estimate(lc, adapter)
        assert est.unprocessed_messages == 3

        executor = _CapturingExecutor()
        assert _generate(lc, _persona(adapter), executor) == "ok"
        compiled = [mid for chunk in executor.chunks[-1] for mid in chunk]
        assert compiled == imported
        assert len(compiled) == est.unprocessed_messages


# ---------------------------------------------------------------------------
# 冷えた anchor 行への印 (§15 の presented_raw の追記)
# ---------------------------------------------------------------------------


class TestColdWindowMarks:
    def test_marks_cold_window_and_is_idempotent(self, adapter, session_factory):
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[2])
        _warm_row(lc, "model-b", ids[2])
        entry = _create_entry(adapter.conn, ids[2:4])

        assert mark_covered_cold_windows(lc, _persona(adapter)) == (1, 0)
        saved = deserialize_folds(
            lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"),
        )
        assert len(saved) == 1
        assert saved[0].presented_raw is True
        assert saved[0].chronicle_entry_ids == [entry.id]
        assert saved[0].message_ids == ids[2:4]

        # 温かい行は触らない
        warm = lc.load_anchor_entry(PERSONA_ID, "model-b")
        assert not warm.get("folded_ranges")

        # 冪等: もう一度走らせても重複追加しない
        assert mark_covered_cold_windows(lc, _persona(adapter)) == (0, 0)
        saved = deserialize_folds(
            lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"),
        )
        assert len(saved) == 1

    def test_straddling_entry_is_not_marked(self, adapter, session_factory):
        """anchor を跨ぐエントリに印を書くと窓の提示が digest に倒れる — 見送る。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[2])
        _create_entry(adapter.conn, ids[1:4])  # ids[1] は窓の外

        # 跨ぎの見送りは「失敗」ではない (次の畳みで正規の圧縮区間になる)
        assert mark_covered_cold_windows(lc, _persona(adapter)) == (0, 0)
        entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
        assert not entry.get("folded_ranges")

    def test_fold_write_is_cas_guarded_and_keeps_temperature(
        self, adapter, session_factory,
    ):
        """印の書き込みは anchor 一致の CAS で、温度 (UPDATED_AT) を触らない。"""
        ids = _add_messages(adapter, 3)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[1])
        before = lc.load_anchor_entry(PERSONA_ID, "model-a")

        marks = coverage_marks_for_window(adapter.conn, ids[1], [])
        assert marks == []  # エントリなし → 印なし (器の確認だけ)

        from sea.session_window import FoldedRange
        fold = FoldedRange(message_ids=[ids[1]], presented_raw=True,
                           chronicle_entry_ids=["e1"])
        # anchor がズレていたら棄却
        assert lc.write_folds_if_anchor_unchanged(
            PERSONA_ID, "model-a", "wrong-anchor", [fold],
        ) is False
        assert not lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges")
        # 一致すれば書けて、温度は据え置き (冷えたまま)
        assert lc.write_folds_if_anchor_unchanged(
            PERSONA_ID, "model-a", ids[1], [fold],
        ) is True
        after = lc.load_anchor_entry(PERSONA_ID, "model-a")
        assert deserialize_folds(after.get("folded_ranges"))[0].presented_raw
        assert after["updated_at"] == before["updated_at"]
        assert not lc._anchor_entry_is_hot(after, "model-a", PERSONA_ID)


# ---------------------------------------------------------------------------
# 窓の誕生時の護り (§16-3)
# ---------------------------------------------------------------------------


class TestBootstrapGuard:
    def test_new_row_is_born_with_coverage_marks(self, adapter, session_factory):
        ids = _add_messages(adapter, 4)
        persona = _persona(adapter)
        lc = _make_lifecycle(session_factory, personas={PERSONA_ID: persona})
        entry = _create_entry(adapter.conn, ids[2:4])

        _warm_row(lc, "model-a", ids[2])  # 新規行の作成 = 窓の誕生
        saved = deserialize_folds(
            lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"),
        )
        assert len(saved) == 1
        assert saved[0].presented_raw is True
        assert saved[0].chronicle_entry_ids == [entry.id]
        assert saved[0].message_ids == ids[2:4]

    def test_new_row_without_reachable_adapter_is_born_plain(
        self, adapter, session_factory,
    ):
        """manager から memory.db が引けない環境 (従来のテスト形) は印なしで生まれる。"""
        ids = _add_messages(adapter, 2)
        _create_entry(adapter.conn, ids)
        lc = _make_lifecycle(session_factory)  # personas 空
        _warm_row(lc, "model-a", ids[0])
        assert not lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges")


# ---------------------------------------------------------------------------
# 削除 → 未被覆へ戻る (§16-2 消して再編纂)
# ---------------------------------------------------------------------------


class TestDeleteRestoresUncovered:
    def test_deleted_entry_counts_as_uncovered_again(
        self, adapter, session_factory,
    ):
        from sai_memory.arasuji.storage import delete_entry
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        entry = _create_entry(adapter.conn, ids)

        assert _estimate(lc, adapter).unprocessed_messages == 0
        assert delete_entry(adapter.conn, entry.id) is True
        assert _estimate(lc, adapter).unprocessed_messages == 4


# ---------------------------------------------------------------------------
# run_coverage_repair (入口の契約: disabled / 編纂 + 印の一巡)
# ---------------------------------------------------------------------------


class TestRunCoverageRepair:
    def test_disabled_when_the_persona_toggle_is_off(self, adapter, session_factory):
        """門はペルソナ設定だけ (env ENABLE_MEMORY_WEAVE_CONTEXT は 2026-09-01 撤去)。"""
        lc = _make_lifecycle(session_factory)
        with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=False):
            assert lc.run_coverage_repair(_persona(adapter)) == ("disabled", 0)

    def test_runs_without_any_env_gate(self, adapter, session_factory):
        """環境変数が一切無くても "disabled" に落ちない (撤去の回帰止め)。

        .env に行が無いアップグレード組で記憶の整理が全停止した実害の再発防止。
        ここでは編纂本体まで行かせず、門を通ったことだけを見る。
        """
        import os
        lc = _make_lifecycle(session_factory)
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(lc, "generate_chronicle", return_value="deferred"):
            status, _marks = lc.run_coverage_repair(_persona(adapter))
        assert status == "deferred"  # = 門は通った (disabled ではない)

    def test_rebuilds_head_even_when_disabled(
        self, adapter, session_factory,
    ):
        """補修が何もしなくても head 再構築を発火する (手動入口の契約)。

        ボタンを押した以上、設定トグルの変更はコンテキストへ反映される
        (2026-09-01。run_manual_compaction と同じ規律)。補修の本体は
        on_metabolism を発火しないので、出口の 1 回だけになる。
        """
        lc = _make_lifecycle(session_factory)
        calls = []
        with patch.object(lc, "is_chronicle_enabled_for_persona", return_value=False), \
                patch(
                    "saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                    lambda persona, manager, model_key=None: calls.append(model_key),
                ):
            assert lc.run_coverage_repair(_persona(adapter)) == ("disabled", 0)
        assert len(calls) == 1

    def test_compiles_uncovered_past_and_marks_cold_window(
        self, adapter, session_factory,
    ):
        """一巡: 止め線なし → 未被覆の過去を編纂し、冷えた窓に印が付く。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[2])
        # 窓の中 (ids[2:4]) は被覆済み、ids[0:2] が未被覆の過去
        entry = _create_entry(adapter.conn, ids[2:4])

        executor = _CapturingExecutor(write_entries=True)
        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=("mock-model", {"provider": "mock", "context_length": 1000}),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=SimpleNamespace(),
        ), patch(
            "sai_memory.arasuji.executor.execute_plan", executor,
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0,
        ), patch(
            "sai_memory.memory.entity_extractor.make_batch_callback",
            side_effect=RuntimeError("skip entity extraction"),
        ):
            assert lc.run_coverage_repair(_persona(adapter)) == ("ok", 0)

        # 未被覆だった ids[0:2] だけが編纂された (processed は自動で飛ぶ)
        compiled = [mid for chunk in executor.chunks[-1] for mid in chunk]
        assert compiled == ids[:2]
        # 冷えた窓 (anchor=ids[2]) を覆う既存エントリに印が付いた
        saved = deserialize_folds(
            lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"),
        )
        assert [f.chronicle_entry_ids for f in saved] == [[entry.id]]
        assert all(f.presented_raw for f in saved)


# ---------------------------------------------------------------------------
# 印の失敗の可視化 (Codex レビュー 2026-08-31 採用 4)
# ---------------------------------------------------------------------------


class TestMarkFailureVisibility:
    def test_write_failure_is_counted(self, adapter, session_factory):
        """書き込み失敗 (CAS 棄却 / DB 失敗) は失敗数として返る。"""
        ids = _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", ids[2])
        _create_entry(adapter.conn, ids[2:4])
        with patch.object(
            lc, "write_folds_if_anchor_unchanged", return_value=False,
        ):
            assert mark_covered_cold_windows(lc, _persona(adapter)) == (0, 1)

    def test_worker_reports_mark_failures_but_stays_completed(
        self, adapter, session_factory,
    ):
        """編纂成功 + 印の失敗 → job は completed のまま、警告文言を出す。"""
        from api.routes.people import arasuji as arasuji_api
        from sea.cancellation import CancellationToken
        lc = _make_lifecycle(session_factory)
        job_id = arasuji_api._create_job(PERSONA_ID)
        with patch.object(
            lc, "run_coverage_repair_checked", return_value=("ok", 2, True),
        ), patch.object(lc, "ensure_recall_embeddings"):
            arasuji_api._run_coverage_repair_job(
                job_id, _persona(adapter), lc, CancellationToken(),
            )
        job = arasuji_api._get_job(job_id)
        assert job["status"] == "completed"
        assert "印は書けませんでした" in job["message"]
        assert "次回の補修時に自動で再適用されます" in job["message"]
        assert job["warning"] is None

    def test_worker_warns_when_head_was_not_rebuilt(self, adapter, session_factory):
        """head を組み直せなかったら completed のまま warning を添える。

        畳み・補修は成功しているので失敗扱いにはしない。救済 (再試行) もしない —
        「知らせることは必要」だけが裁定 (2026-09-01)。
        """
        from api.routes.people import arasuji as arasuji_api
        from sea.cancellation import CancellationToken
        lc = _make_lifecycle(session_factory)
        job_id = arasuji_api._create_job(PERSONA_ID)
        with patch.object(
            lc, "run_coverage_repair_checked", return_value=("ok", 0, False),
        ), patch.object(lc, "ensure_recall_embeddings"):
            arasuji_api._run_coverage_repair_job(
                job_id, _persona(adapter), lc, CancellationToken(),
            )
        job = arasuji_api._get_job(job_id)
        assert job["status"] == "completed"
        assert job["warning"] == arasuji_api._HEAD_REBUILD_WARNING


# ---------------------------------------------------------------------------
# 見積もりの時点ずれの歯止め (Codex レビュー 2026-08-31 採用 3)
# ---------------------------------------------------------------------------


class TestEstimateStaleGuard:
    def test_increase_blocks_execution(self, adapter, session_factory):
        """承認した件数より対象が増えていたら実行せず estimate_stale。"""
        from api.routes.people import arasuji as arasuji_api
        from sea.cancellation import CancellationToken
        _add_messages(adapter, 4)  # 現在の対象 = 4 件
        lc = _make_lifecycle(session_factory)
        job_id = arasuji_api._create_job(PERSONA_ID)
        with patch.object(lc, "run_coverage_repair_checked") as run:
            arasuji_api._run_coverage_repair_job(
                job_id, _persona(adapter), lc, CancellationToken(),
                confirmed_unprocessed=2,  # 見積もり時は 2 件だった
            )
            run.assert_not_called()
        job = arasuji_api._get_job(job_id)
        assert job["status"] == "failed"
        assert job["error_code"] == "estimate_stale"

    def test_equal_or_decrease_runs(self, adapter, session_factory):
        """同数・減少 (表示より安くなる方向) は実行してよい。"""
        from api.routes.people import arasuji as arasuji_api
        from sea.cancellation import CancellationToken
        _add_messages(adapter, 4)
        lc = _make_lifecycle(session_factory)
        with patch.object(
            lc, "run_coverage_repair_checked", return_value=("ok", 0, True),
        ) as run, patch.object(lc, "ensure_recall_embeddings"):
            for confirmed in (4, 10):  # 同数 / 減少 (承認 10 → 現在 4)
                job_id = arasuji_api._create_job(PERSONA_ID)
                arasuji_api._run_coverage_repair_job(
                    job_id, _persona(adapter), lc, CancellationToken(),
                    confirmed_unprocessed=confirmed,
                )
                job = arasuji_api._get_job(job_id)
                assert job["status"] == "completed"
            assert run.call_count == 2


# ---------------------------------------------------------------------------
# 末尾の引き戻し (run_tail_rewind) — 引き戻し後の畳みの主語
# ---------------------------------------------------------------------------


class TestTailRewindPostFold:
    """引き戻し後に「畳む」と言えるのは、会話の行が残す量を超えているときだけ。

    上限 (fold_needed) の主語は合計だが、残す量の主語は会話の行 (2026-09-03
    まはー裁定)。行が残す量以下なら退場計画は保護範囲で埋まって空になるので、
    畳みを呼んでも門で "noop" — 「古い側を畳んでいます」の通知も
    "rewound_folded" (畳み完了) の返り値も嘘になる。
    """

    def _run(self, session_factory, plan, events):
        from unittest.mock import Mock

        from sea.coverage_repair import run_tail_rewind
        lc = _make_lifecycle(session_factory)
        lc.run_manual_compaction = Mock(return_value="ok")
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            sai_memory=SimpleNamespace(is_ready=lambda: True, conn=object()),
        )
        with patch("sea.coverage_repair._resolve_uncovered_tail", return_value="gap"), \
                patch("sea.coverage_repair.plan_tail_rewind", return_value=plan), \
                patch.object(lc, "_write_refill", return_value=True):
            status = run_tail_rewind(lc, persona, event_callback=events.append)
        return status, lc.run_manual_compaction

    def _plan(self, **overrides):
        from sea.coverage_repair import TailRewindPlan
        kwargs = dict(
            model_key="model-a", expected_anchor_id="a1", new_anchor_id="gap",
            window_chars_after=300, fold_needed=True,
            window_rows_chars_after=200, target_chars=50,
        )
        kwargs.update(overrides)
        return TailRewindPlan(**kwargs)

    def test_perception_only_over_budget_does_not_fold(self, session_factory):
        """合計 300 > 上限だが会話の行 40 <= 残す量 50 → 通知なし・"rewound"。"""
        events = []
        status, fold = self._run(
            session_factory,
            self._plan(window_chars_after=300, window_rows_chars_after=40),
            events,
        )
        assert status == "rewound"
        fold.assert_not_called()
        assert [e["content"] for e in events] == [
            "あらすじにできない少量の末尾を、提示窓へ戻しました。",
        ]

    def test_rows_over_target_still_notifies_and_folds(self, session_factory):
        """会話の行 200 > 残す量 50 → 従来どおり通知して畳み、"rewound_folded"。"""
        events = []
        status, fold = self._run(session_factory, self._plan(), events)
        assert status == "rewound_folded"
        fold.assert_called_once()
        assert fold.call_args.kwargs["model_key"] == "model-a"
        assert any("古い側を畳んでいます" in e["content"] for e in events)

    def test_compaction_noop_is_reported_as_rewound_not_folded(
        self, session_factory,
    ):
        """畳みを呼んだが門で "noop" → 畳んでいないので "rewound" (成功系のまま)。"""
        from unittest.mock import Mock

        from sea.coverage_repair import run_tail_rewind
        lc = _make_lifecycle(session_factory)
        lc.run_manual_compaction = Mock(return_value="noop")
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            sai_memory=SimpleNamespace(is_ready=lambda: True, conn=object()),
        )
        with patch("sea.coverage_repair._resolve_uncovered_tail", return_value="gap"), \
                patch("sea.coverage_repair.plan_tail_rewind", return_value=self._plan()), \
                patch.object(lc, "_write_refill", return_value=True):
            assert run_tail_rewind(lc, persona) == "rewound"
        lc.run_manual_compaction.assert_called_once()

    def test_fold_evictable_property(self):
        """fold_evictable = 上限超え かつ 行 > 残す量 (残す量不明なら上限超えだけ)。"""
        assert self._plan().fold_evictable is True
        assert self._plan(window_rows_chars_after=50).fold_evictable is False
        assert self._plan(fold_needed=False).fold_evictable is False
        assert self._plan(target_chars=None, window_rows_chars_after=0).fold_evictable is True
