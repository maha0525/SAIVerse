"""極小 run の隣人吸収 (sai_memory/arasuji/absorption.py) のテスト。

docs/issues/arasuji_tiny_run_absorption.md (2026-08-31 まはー裁定 8 点) を固定する:

- 検出: 材料 0.5U 未満のチャンク = 極小 run (閾値は U から導出)。
- 計画: 吸収先は後ろ (新しい側) の隣人 Lv1。隣人が居ない末尾 run は見送り。
  隣人自体が極小なら合計 0.5U に届くまで連鎖する。
- 実行: 生成が先・削除が後 (generate-then-swap)。帳簿 (Fragment / 付記印 /
  埋め込み) は差し替えに追随する。
- 上位の連鎖再生成: 「被覆範囲から抜けた時点」で 1 回ずつ、ジョブ末尾で全 flush。
- 失敗の可視化: 途中失敗で repair_incomplete の印が残り、再実行で続きから直る。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from sai_memory.arasuji.absorption import (
    AbsorptionError,
    is_repair_incomplete,
    plan_absorption,
    run_absorption,
    split_plan_for_absorption,
    tiny_run_threshold,
)
from sai_memory.arasuji.alignment import (
    CHUNK_LLM_BATCH,
    AlignmentPlan,
    PlannedChunk,
    plan_alignment,
)
from sai_memory.arasuji.storage import (
    create_entry,
    get_entries_covering_messages,
    get_entry,
    init_arasuji_tables,
    mark_consolidated,
)

PERSONA_ID = "absorb-tester"
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: テスト用の U。閾値 (0.5U) = 500 字。
TARGET = 1000


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
        init_arasuji_tables(a.conn)
        yield a
        try:
            a.close()
        except Exception:
            pass


class _Client:
    """吸収 (Lv1 合体) と上位再生成 (統合) を見分けて応答する mock LLM。"""

    MERGE = "合体あらすじ。"
    UPPER = "語り直された上位あらすじ。"

    def __init__(self, fail_calls=()):
        self.prompts = []
        self.fail_calls = set(fail_calls)

    def generate(self, messages, tools):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if len(self.prompts) in self.fail_calls:
            raise RuntimeError("llm down")
        if "統合対象の材料" in prompt:
            return self.UPPER
        return self.MERGE

    def kinds(self):
        return [
            "upper" if "統合対象の材料" in p else "merge" for p in self.prompts
        ]


def _add_message(adapter, minute, chars, prefix="会話"):
    mid = adapter.append_persona_message({
        "role": "user",
        "content": f"{prefix} " + "あ" * chars,
        "timestamp": (BASE_TIME + timedelta(minutes=minute)).isoformat(),
    })
    assert mid is not None
    return mid


def _epoch(minute):
    return int((BASE_TIME + timedelta(minutes=minute)).timestamp())


def _entry(conn, source_ids, *, start_min, end_min, content="既存あらすじ", level=1):
    return create_entry(
        conn, level=level, content=content,
        source_ids=list(source_ids),
        start_time=_epoch(start_min), end_time=_epoch(end_min),
        source_count=len(source_ids), message_count=len(source_ids),
        extra_metadata={"coverage_chars": 1},
    )


def _messages(adapter):
    from sai_memory.memory.storage import get_messages_for_chronicle
    return get_messages_for_chronicle(adapter.conn)


def _processed_ids(conn):
    cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) WHERE level = 1"
    )
    return {row[0] for row in cur.fetchall()}


def _plan(adapter):
    messages = _messages(adapter)
    processed = _processed_ids(adapter.conn)
    plan = plan_alignment(messages, processed, target_chars=TARGET)
    normal, tiny = split_plan_for_absorption(plan, target_chars=TARGET)
    return messages, processed, normal, tiny


# ---------------------------------------------------------------------------
# 検出 (純関数)
# ---------------------------------------------------------------------------


class TestSplit:
    def test_threshold_is_derived_from_target(self):
        assert tiny_run_threshold(10_000) == 5_000
        assert tiny_run_threshold(TARGET) == 500

    def test_chunks_below_half_u_are_tiny(self):
        def chunk(coverage):
            return PlannedChunk(
                kind=CHUNK_LLM_BATCH, messages=[], coverage_chars=coverage,
            )

        plan = AlignmentPlan(
            chunks=[chunk(499), chunk(500), chunk(1200)], total_unprocessed=9,
        )
        normal, tiny = split_plan_for_absorption(plan, target_chars=TARGET)
        assert [c.coverage_chars for c in tiny] == [499]
        assert [c.coverage_chars for c in normal.chunks] == [500, 1200]
        # 件数表示の意味 (total_unprocessed) は変えない
        assert normal.total_unprocessed == 9


# ---------------------------------------------------------------------------
# 計画 (決定論)
# ---------------------------------------------------------------------------


class TestPlanAbsorption:
    def test_absorbs_the_rear_neighbor(self, adapter):
        gap = _add_message(adapter, 0, 100)              # 極小 run
        n1 = _add_message(adapter, 10, 300)
        n2 = _add_message(adapter, 11, 300)
        entry = _entry(adapter.conn, [n1, n2], start_min=10, end_min=11)

        messages, processed, _normal, tiny = _plan(adapter)
        assert len(tiny) == 1
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert plan.unresolved_runs == 0
        assert plan.rewind_run_ids == []
        assert len(plan.items) == 1
        assert plan.items[0].run_message_ids == [gap]
        assert plan.items[0].absorbed_entry_ids == [entry.id]
        assert plan.items[0].material_chars >= tiny_run_threshold(TARGET)

    def test_tail_run_without_neighbor_becomes_rewind_target(self, adapter):
        """後ろに編纂済みが何も無い末尾の極小 run は「見送り」ではなく anchor
        引き戻しの対象になる (裁定 5 改訂 — deferred の概念は廃止)。"""
        n1 = _add_message(adapter, 0, 300)
        _entry(adapter.conn, [n1], start_min=0, end_min=0)
        tail = _add_message(adapter, 10, 100)             # 末尾の端数 (後ろが無い)

        messages, processed, _normal, tiny = _plan(adapter)
        assert len(tiny) == 1
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert plan.items == []
        assert plan.unresolved_runs == 0
        assert plan.rewind_run_ids == [tail]
        assert plan.rewind_first_message_id == tail
        # 引き戻しは LLM ゼロ — 呼び出し回数の見積もりに入らない
        assert plan.llm_calls == 0

    def test_chains_through_tiny_neighbors_until_half_u(self, adapter):
        gap1 = _add_message(adapter, 0, 100)              # 極小 run 1
        b1 = _add_message(adapter, 10, 100)               # 豆粒隣人 E1 の source
        e1 = _entry(adapter.conn, [b1], start_min=10, end_min=10)
        gap2 = _add_message(adapter, 20, 100)             # 極小 run 2 (連鎖で吸収)
        b2 = _add_message(adapter, 30, 300)
        b3 = _add_message(adapter, 31, 300)
        e2 = _entry(adapter.conn, [b2, b3], start_min=30, end_min=31)

        messages, processed, _normal, tiny = _plan(adapter)
        assert len(tiny) == 2
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
        )
        # 2 つの極小 run と 2 つの隣人がひとつの連続範囲に束ねられる
        assert len(plan.items) == 1
        assert plan.unresolved_runs == 0
        assert plan.rewind_run_ids == []
        item = plan.items[0]
        assert set(item.run_message_ids) == {gap1, gap2}
        assert item.absorbed_entry_ids == [e1.id, e2.id]

    def test_holes_inside_the_opened_neighbor_are_scooped_together(self, adapter):
        """開いた隣人 E の被覆範囲の内側にある穴は、閾値と無関係に全部同じ
        item へすくい取られる (2026-08-31 検収指摘 — aifi の実データの形)。

        すくい漏れると 2 つ目以降の穴の walk が「E は吸収予定」で止まって
        見送りになり、1 実行につき穴 1 個しか治らない。"""
        n1 = _add_message(adapter, 0, 600)
        gap1 = _add_message(adapter, 5, 100)   # E の span 内の穴 (最古)
        n2 = _add_message(adapter, 10, 600)
        gap2 = _add_message(adapter, 15, 100)  # E の span 内の穴 (2 つ目)
        n3 = _add_message(adapter, 20, 600)
        entry = _entry(adapter.conn, [n1, n2, n3], start_min=0, end_min=20)

        messages, processed, _normal, tiny = _plan(adapter)
        assert len(tiny) == 2
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
        )
        # 穴は 1 個も取り残されず、1 個の item に両方入る
        assert plan.unresolved_runs == 0
        assert len(plan.items) == 1
        item = plan.items[0]
        assert item.run_message_ids == [gap1, gap2]  # 正典順
        assert item.absorbed_entry_ids == [entry.id]

    def test_presented_digest_leaves_the_run_unresolved(self, adapter):
        """提示中の digest に塞がれた run は、吸収も引き戻しもできない残余
        (unresolved) として数える — 後ろに編纂済みが在るので帯 (rewind 対象)
        ではない。次の畳みで digest が動けば自然に解消する。"""
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 300)
        entry = _entry(adapter.conn, [n1], start_min=10, end_min=10)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
            excluded_entry_ids=frozenset({entry.id}),
        )
        assert plan.items == []
        assert plan.rewind_run_ids == []
        assert plan.unresolved_runs == 1

    def test_dirty_ancestors_are_counted(self, adapter):
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        child = _entry(adapter.conn, [n1], start_min=10, end_min=10)
        parent = _entry(
            adapter.conn, [child.id], start_min=10, end_min=10, level=2,
        )
        mark_consolidated(adapter.conn, [child.id], parent.id)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            adapter.conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert len(plan.items) == 1
        assert plan.stale_upper_ids == [parent.id]
        assert plan.llm_calls == 2  # 合体 1 + 上位再生成 1


# ---------------------------------------------------------------------------
# 実行 (generate-then-swap と帳簿の追随)
# ---------------------------------------------------------------------------


def _insert_fragment(conn, fragment_id, entry_id):
    conn.execute(
        "INSERT INTO memopedia_fragments "
        "(id, content, entity_id, chronicle_entry_id, vividness, source_date, created_at) "
        "VALUES (?, '知識', 'root_chronicle', ?, 1.0, NULL, 1)",
        (fragment_id, entry_id),
    )
    conn.commit()


def _annex_batch(conn, entry_id, *, at, text="知覚テキスト"):
    from sai_memory.perception_buffer import (
        create_consumption_batch,
        init_perception_buffer_table,
        mark_batches_annexed,
        push_perception,
    )
    init_perception_buffer_table(conn)
    item_id = push_perception(conn, "world_state", text)
    batch_id = create_consumption_batch(
        conn, [item_id], consumed_at=at, rendered_text=text,
    )
    mark_batches_annexed(conn, [batch_id], entry_id)
    conn.commit()
    return batch_id


class TestRunAbsorption:
    def test_merge_swaps_and_bookkeeping_follows(self, adapter):
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 300)
        n2 = _add_message(adapter, 11, 300)
        entry = _entry(conn, [n1, n2], start_min=10, end_min=11)
        _insert_fragment(conn, "frag-1", entry.id)
        conn.execute(
            "INSERT INTO arasuji_embeddings (entry_id, vector) VALUES (?, '[]')",
            (entry.id,),
        )
        conn.commit()
        batch_id = _annex_batch(conn, entry.id, at=_epoch(10))

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        client = _Client()
        result = run_absorption(conn, client, plan)

        assert len(result.merged_entries) == 1
        merged = result.merged_entries[0]
        assert result.reopened_entry_ids == [entry.id]
        # 旧隣人は消え、合体エントリが連続範囲 (gap + 隣人の source) を覆う
        assert get_entry(conn, entry.id) is None
        covering = get_entries_covering_messages(conn, [gap, n1, n2])
        assert [e.id for e in covering] == [merged.id]
        assert set(covering[0].source_ids) == {gap, n1, n2}
        assert covering[0].content == _Client.MERGE
        # Fragment は消えず新エントリへ付け替え
        row = conn.execute(
            "SELECT chronicle_entry_id FROM memopedia_fragments WHERE id = 'frag-1'"
        ).fetchone()
        assert row[0] == merged.id
        # 旧エントリの埋め込みは道連れ削除
        row = conn.execute(
            "SELECT 1 FROM arasuji_embeddings WHERE entry_id = ?", (entry.id,)
        ).fetchone()
        assert row is None
        # 付記印は新エントリへ付け替え (提示へ戻らない)
        row = conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        assert row[0] == merged.id
        # 完了 — 未完了の印は立っていない
        assert not is_repair_incomplete(conn)

    def test_multiple_holes_in_one_neighbor_merge_in_one_pass(self, adapter):
        """span 内に穴が複数ある隣人は、1 回の run_absorption で 1 個の合体
        エントリになる (再実行の繰り返しと同範囲の LLM 作り直しを出さない)。"""
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 600)
        gap1 = _add_message(adapter, 5, 100)
        n2 = _add_message(adapter, 10, 600)
        gap2 = _add_message(adapter, 15, 100)
        n3 = _add_message(adapter, 20, 600)
        entry = _entry(conn, [n1, n2, n3], start_min=0, end_min=20)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        client = _Client()
        result = run_absorption(conn, client, plan)

        assert result.unresolved_runs == 0
        assert result.skipped_items == 0
        assert len(result.merged_entries) == 1
        assert client.kinds() == ["merge"]  # LLM は合体 1 回だけ
        # 旧 E は消え、新エントリの source に全穴 + E の全 source が入る
        assert get_entry(conn, entry.id) is None
        covering = get_entries_covering_messages(conn, [gap1])
        assert len(covering) == 1
        assert set(covering[0].source_ids) == {n1, gap1, n2, gap2, n3}
        assert not is_repair_incomplete(conn)

    def test_generation_failure_keeps_old_entries(self, adapter):
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        client = _Client(fail_calls={1})
        with pytest.raises(AbsorptionError):
            run_absorption(conn, client, plan)
        # 生成が先・削除が後: 旧隣人は無傷で残る
        assert get_entry(conn, entry.id) is not None
        # 未完了の印が立ち、帯が再実行を促す
        assert is_repair_incomplete(conn)

    def test_upper_regeneration_follows_the_timeline(self, adapter):
        conn = adapter.conn
        # P1 の範囲: A + gap1 + B / P2 の範囲: C (gap2 が手前)
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        gap1 = _add_message(adapter, 5, 100)
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p1 = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P1",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p1.id)

        # B と gap2 の間に別の編纂済みエントリ D を挟む — 挟まないと gap2 は
        # 「開いた B の隣接の穴」としてすくい取られて item が 1 つになり、
        # このテストの関心 (2 item・2 親の flush 順) が観測できない。
        d1 = _add_message(adapter, 12, 600)
        _entry(conn, [d1], start_min=12, end_min=12, content="D")

        gap2 = _add_message(adapter, 20, 100)
        c1 = _add_message(adapter, 30, 600)
        entry_c = _entry(conn, [c1], start_min=30, end_min=30, content="C")
        p2 = _entry(
            conn, [entry_c.id], start_min=30, end_min=30, level=2, content="P2",
        )
        mark_consolidated(conn, [entry_c.id], p2.id)

        messages, processed, _normal, tiny = _plan(adapter)
        assert len(tiny) == 2
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert len(plan.items) == 2
        client = _Client()
        result = run_absorption(conn, client, plan)

        # 呼び出し順: 合体1 → (P1 の範囲を抜けた) P1 再生成 → 合体2 →
        # (末尾 flush) P2 再生成。同じ上位は 1 回ずつ。
        assert client.kinds() == ["merge", "upper", "merge", "upper"]
        assert result.regenerated_upper_ids == [p1.id, p2.id]

        p1_after = get_entry(conn, p1.id)
        assert p1_after.content == _Client.UPPER
        merged1 = next(
            e for e in result.merged_entries if gap1 in e.source_ids
        )
        # 帳簿: 子の id が新 id へ差し替わっている (A は残り、B は合体側)
        assert p1_after.source_ids == [entry_a.id, merged1.id]
        # content_stale は flush で外れている
        import json
        row = conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (p1.id,)
        ).fetchone()
        assert "content_stale" not in json.loads(row[0])
        # 合体エントリは親に統合済みの子として繋がる
        assert get_entry(conn, merged1.id).parent_id == p1.id
        assert get_entry(conn, merged1.id).is_consolidated
        # gap2 の合体も P2 側に入り、全 gap が被覆されている
        assert get_entries_covering_messages(conn, [gap2])
        assert not is_repair_incomplete(conn)

    def test_midway_failure_leaves_marker_and_rerun_resumes(self, adapter):
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        _add_message(adapter, 5, 100)  # gap → A では後ろに居ないので B を使う
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p1 = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P1 (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p1.id)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        # 呼び出し 1 = 合体 (成功)、2 = P1 再生成 (失敗)
        client = _Client(fail_calls={2})
        with pytest.raises(AbsorptionError):
            run_absorption(conn, client, plan)

        # 親子リンクは整合 (source_ids は新 id)、本文だけ古い
        import json
        p1_mid = get_entry(conn, p1.id)
        assert p1_mid.content == "P1 (古い本文)"
        assert entry_b.id not in p1_mid.source_ids
        row = conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (p1.id,)
        ).fetchone()
        assert json.loads(row[0]).get("content_stale") == 1
        assert is_repair_incomplete(conn)

        # 再実行 (吸収済みの run は processed_ids で自然に飛ぶ → flush のみ)
        messages, processed, _normal, tiny = _plan(adapter)
        assert tiny == []  # 前回の合体で run は被覆済み
        client2 = _Client()
        result = run_absorption(conn, client2, None)
        assert result.regenerated_upper_ids == [p1.id]
        assert get_entry(conn, p1.id).content == _Client.UPPER
        assert not is_repair_incomplete(conn)


# ---------------------------------------------------------------------------
# generate_chronicle への配線 (全量計画 = 補修経路のみ吸収が効く)
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


class TestGenerateChronicleWiring:
    def test_full_plan_diverts_tiny_runs_to_absorption(
        self, adapter, session_factory, monkeypatch,
    ):
        """全量計画 (compile_groups=None) では極小 run が単独編纂されず、
        隣人吸収へ回る。executor へ渡る計画に極小チャンクは残らない。"""
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 300)
        n2 = _add_message(adapter, 11, 300)
        entry = _entry(conn, [n1, n2], start_min=10, end_min=11)

        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        client = _Client()
        captured = {}

        def _capture_plan(plan, *a, **k):
            from sai_memory.arasuji.executor import ExecutionResult
            captured["chunks"] = [list(c.message_ids) for c in plan.chunks]
            return ExecutionResult()

        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=client,
        ), patch(
            "sai_memory.arasuji.executor.execute_plan", _capture_plan,
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0,
        ), patch(
            "sai_memory.memory.entity_extractor.make_batch_callback",
            side_effect=RuntimeError("skip entity extraction"),
        ):
            status = lifecycle.generate_chronicle(persona, force=True)

        assert status == "ok"
        # 極小 run は executor の計画に残らない (吸収へ回った)
        assert captured["chunks"] == []
        # 吸収の結果: 旧隣人は開き直され、合体エントリが連続範囲を覆う
        assert get_entry(conn, entry.id) is None
        covering = get_entries_covering_messages(conn, [gap])
        assert len(covering) == 1
        assert set(covering[0].source_ids) == {gap, n1, n2}
        assert not is_repair_incomplete(conn)

    def test_plan_exception_fails_the_full_plan(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex J3] 吸収計画の例外は「見送り = ok」に潰さず failed で返す —
        ジョブ UI に失敗が出て再実行を促す。"""
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=_Client(),
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.absorption.plan_absorption",
            side_effect=RuntimeError("plan down"),
        ):
            status = lifecycle.generate_chronicle(persona, force=True)
        assert status == "failed"
        assert get_entry(conn, entry.id) is not None  # 何も動かしていない

    def test_unknown_folds_still_defer_not_fail(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex J3] fold 不明 (None) は設計上の見送りであって失敗ではない —
        従来どおり ok で閉じる (対比の固定)。"""
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=_Client(),
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sea.session_lifecycle.collect_folded_chronicle_entry_ids",
            return_value=None,
        ):
            status = lifecycle.generate_chronicle(persona, force=True)
        assert status == "ok"
        # 見送り: 極小 run は編纂されず、隣人も無傷
        assert get_entry(conn, entry.id) is not None
        assert get_entries_covering_messages(conn, [gap]) == []

    def test_maintenance_check_exception_fails_the_full_plan(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex J4] sweep / stale 照会の例外も 0 / False へ潰さず failed。"""
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        _entry(conn, [a1], start_min=0, end_min=0)
        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=_Client(),
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.absorption._sweep_broken_parents",
            side_effect=RuntimeError("sweep down"),
        ):
            status = lifecycle.generate_chronicle(persona, force=True)
        assert status == "failed"

    def test_sweep_runs_in_full_plan_without_other_work(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex H1] tiny も stale も無い全量計画でも sweep が回り、死んだ
        子 id を指す親が治る (仕事ゼロで保険が眠らない)。"""
        import json as _json
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)
        # 残骸の再現: 親の source_ids に死んだ id を直接ねじ込む (未被覆の
        # メッセージは作らない — tiny も stale も無い形を保つ)
        row = conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (p.id,)
        ).fetchone()
        meta = _json.loads(row[0])
        meta["source_ids"] = list(meta["source_ids"]) + ["dead-xyz"]
        conn.execute(
            "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
            (_json.dumps(meta, ensure_ascii=False), p.id),
        )
        conn.commit()

        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        client = _Client()

        def _noop_executor(plan, *a, **k):
            from sai_memory.arasuji.executor import ExecutionResult
            return ExecutionResult()

        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=client,
        ), patch(
            "sai_memory.arasuji.executor.execute_plan", _noop_executor,
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0,
        ), patch(
            "sai_memory.memory.entity_extractor.make_batch_callback",
            side_effect=RuntimeError("skip entity extraction"),
        ):
            status = lifecycle.generate_chronicle(persona, force=True)

        assert status == "ok"
        p_after = get_entry(conn, p.id)
        assert p_after.source_ids == [entry_a.id, entry_b.id]  # 死んだ id 除去
        assert p_after.content == _Client.UPPER                # flush で語り直し
        assert not _is_stale(conn, p.id)
        assert not is_repair_incomplete(conn)

    # -- 前段 (吸収) の進捗表示 (2026-09-01 まはー実機報告) --------------------
    #
    # 症状: 補修ジョブが「Chronicleを生成しています (0/410)...」で凍って見える。
    # 実際は前段の吸収が走っていて (run 一件ごとに LLM)、エリスの実機では前段
    # だけで未被覆 410→258 まで進んでいた。進捗は本編 (execute_plan) にしか
    # 配線されておらず、開始時に出した (0/410) が残り続けていた。

    def _run_with_events(self, adapter, session_factory, monkeypatch):
        """generate_chronicle を回して event_callback の content 列を返す。"""
        from types import SimpleNamespace

        from sea.session_lifecycle import SessionLifecycle

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        manager = SimpleNamespace(SessionLocal=session_factory, personas={})
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
            sai_memory=adapter,
        )
        events = []

        def _noop_executor(plan, *a, **k):
            from sai_memory.arasuji.executor import ExecutionResult
            return ExecutionResult()

        with patch(
            "saiverse.model_configs.find_model_config",
            return_value=(
                "mock-model", {"provider": "mock", "context_length": 1000},
            ),
        ), patch(
            "llm_clients.factory.get_llm_client", return_value=_Client(),
        ), patch(
            "sai_memory.arasuji.executor.execute_plan", _noop_executor,
        ), patch(
            "sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0,
        ), patch(
            "sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0,
        ), patch(
            "sai_memory.memory.entity_extractor.make_batch_callback",
            side_effect=RuntimeError("skip entity extraction"),
        ):
            status = lifecycle.generate_chronicle(
                persona, lambda ev: events.append(ev), force=True,
            )
        return status, [e.get("content", "") for e in events]

    def test_absorption_phase_is_reported_before_the_compile_start(
        self, adapter, session_factory, monkeypatch,
    ):
        """前段のフェーズと進行が流れ、本編の開始はその**後**に出る。

        開始メッセージが先に出ると、吸収の数分〜数十分がまるごと「(0/N) のまま
        凍った」画面になる。
        """
        conn = adapter.conn
        _add_message(adapter, 0, 100)  # 極小 run → 吸収へ
        n1 = _add_message(adapter, 10, 300)
        n2 = _add_message(adapter, 11, 300)
        _entry(conn, [n1, n2], start_min=10, end_min=11)

        status, contents = self._run_with_events(
            adapter, session_factory, monkeypatch,
        )
        assert status == "ok"

        prep = [i for i, c in enumerate(contents) if "下ごしらえ" in c]
        absorb = [i for i, c in enumerate(contents) if "断片を吸収しています" in c]
        start = [i for i, c in enumerate(contents) if c.startswith("Chronicleを生成しています (0/")]
        assert prep, f"前段のフェーズ表示が無い: {contents}"
        assert absorb, f"吸収の進行表示が無い: {contents}"
        assert start, f"本編の開始表示が無い: {contents}"
        # 順序: 下ごしらえ → 吸収の進行 → 本編の開始
        assert prep[0] < absorb[0] < start[0]
        # 進行は「何件目 / 全件」の形
        assert "(1/1)" in contents[absorb[0]]

    def test_no_absorption_work_keeps_the_plain_start_message(
        self, adapter, session_factory, monkeypatch,
    ):
        """吸収の仕事が無い回は前段の表示を出さない (常態を異常の顔で出さない)。"""
        _add_message(adapter, 0, 600)
        _add_message(adapter, 1, 600)

        status, contents = self._run_with_events(
            adapter, session_factory, monkeypatch,
        )
        assert status == "ok"
        assert not [c for c in contents if "下ごしらえ" in c], contents
        assert not [c for c in contents if "断片を吸収しています" in c], contents
        assert [c for c in contents if c.startswith("Chronicleを生成しています (0/")]


# ---------------------------------------------------------------------------
# レビュー消し込み (Codex 敵対レビュー + ローカルレビュー 2026-08-31)
# ---------------------------------------------------------------------------


def _set_stale(conn, entry_id):
    import json
    row = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
    ).fetchone()
    meta = json.loads(row[0]) if row and row[0] else {}
    meta["content_stale"] = 1
    conn.execute(
        "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), entry_id),
    )
    conn.commit()


def _is_stale(conn, entry_id):
    import json
    row = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
    ).fetchone()
    if not row or not row[0]:
        return False
    return json.loads(row[0]).get("content_stale") == 1


class _RecordingLock:
    """__enter__/__exit__ の深さを数えるだけの錠 (db_lock 境界の検証用)。"""

    def __init__(self):
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        return False


class _LockAssertingConn:
    """錠の外で行われた execute / commit / rollback を記録する conn 代理。"""

    def __init__(self, real, lock):
        self._real = real
        self._lock = lock
        self.violations = []

    def _check(self, op):
        if self._lock.depth <= 0:
            self.violations.append(op)

    def execute(self, sql, *a, **k):
        self._check(f"execute: {str(sql)[:60]}")
        return self._real.execute(sql, *a, **k)

    def commit(self):
        self._check("commit")
        return self._real.commit()

    def rollback(self):
        self._check("rollback")
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestReviewFixes:
    def test_sweep_repairs_dead_child_references(self, adapter):
        """[Codex 1] 死んだ子 id を指す親は、ジョブ冒頭の sweep が現況の子から
        引き直して flush の対象に載せる (crash 残骸 / 素の delete_entry の受け皿)。"""
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)
        # 残骸を人工的に作る: B の行だけ消え、親は死んだ id を持ったまま
        conn.execute("DELETE FROM memopedia_pages WHERE id = ?", (entry_b.id,))
        conn.commit()

        client = _Client()
        result = run_absorption(conn, client, None)
        p_after = get_entry(conn, p.id)
        assert p_after.source_ids == [entry_a.id]
        assert p_after.content == _Client.UPPER
        assert result.regenerated_upper_ids == [p.id]
        assert not is_repair_incomplete(conn)

    def test_estimate_counts_sweep_detected_broken_parents(
        self, adapter, monkeypatch,
    ):
        """[Codex 十二巡 持ち越し] sweep が実行時に新しく見つける壊れ親の再生成を
        見積もりも数える (「表示 < 実走」を作らない)。

        壊れ親だけでなく**その先祖**も処置側が stale にして flush するので、
        見積もりも先祖まで数える。検知クエリは実走と一点共有
        (list_broken_parent_ids)。
        """
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)
        g = _entry(conn, [p.id], start_min=0, end_min=10, level=3, content="G")
        mark_consolidated(conn, [p.id], g.id)

        before = estimate_chronicle_generation_cost(conn, model_name="mock-model")
        assert before.upper_regen_calls == 0

        # 残骸を作る: B の行だけ消え、親 P は死んだ子 id を持ったまま
        conn.execute("DELETE FROM memopedia_pages WHERE id = ?", (entry_b.id,))
        conn.commit()

        after = estimate_chronicle_generation_cost(conn, model_name="mock-model")
        # 壊れ親 P + その先祖 G の 2 件
        assert after.upper_regen_calls == 2
        assert after.estimated_llm_calls == (
            after.level1_calls + after.consolidation_calls + 2
        )

        # 実走 (run_absorption) の再生成回数と一致する
        client = _Client()
        result = run_absorption(conn, client, None)
        assert sorted(result.regenerated_upper_ids) == sorted([p.id, g.id])

    def test_estimate_does_not_double_count_already_stale_broken_parents(
        self, adapter, monkeypatch,
    ):
        """既に content_stale の壊れ親を二重に数えない (sweep 分は差分だけ)。"""
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)
        conn.execute("DELETE FROM memopedia_pages WHERE id = ?", (entry_b.id,))
        conn.commit()
        _set_stale(conn, p.id)

        est = estimate_chronicle_generation_cost(conn, model_name="mock-model")
        assert est.upper_regen_calls == 1

    def test_fully_covered_item_is_skipped_without_llm(self, adapter):
        """[Codex 2] run の全 id が被覆済みなら skip (LLM ゼロ・隣人は無傷)。"""
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        # 計画の後で run が被覆された (前回完了後の再実行と同型)
        _entry(conn, [gap], start_min=0, end_min=0, content="既に被覆")
        client = _Client()
        result = run_absorption(conn, client, plan)
        assert result.skipped_items == 1
        assert result.merged_entries == []
        assert client.prompts == []
        assert get_entry(conn, entry.id) is not None

    def test_partially_covered_item_is_discarded(self, adapter):
        """[Codex 2] 一部だけ被覆済みの item は合体を強行せず破棄する。"""
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 600)
        gap1 = _add_message(adapter, 5, 100)
        n2 = _add_message(adapter, 10, 600)
        gap2 = _add_message(adapter, 15, 100)
        n3 = _add_message(adapter, 20, 600)
        entry = _entry(conn, [n1, n2, n3], start_min=0, end_min=20)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert plan.items
        assert set(plan.items[0].run_message_ids) == {gap1, gap2}
        _entry(conn, [gap1], start_min=5, end_min=5, content="片方だけ被覆")
        client = _Client()
        result = run_absorption(conn, client, plan)
        assert result.skipped_items == 1
        assert result.merged_entries == []
        assert client.prompts == []
        assert get_entry(conn, entry.id) is not None
        assert get_entries_covering_messages(conn, [gap2]) == []

    def test_chain_stops_at_parent_boundary(self, adapter):
        """[Codex 3] 隣人連鎖は親境界 (parent_id の変化) を跨がない。"""
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        b1 = _add_message(adapter, 10, 100)
        e1 = _entry(conn, [b1], start_min=10, end_min=10, content="E1")
        p1 = _entry(conn, [e1.id], start_min=10, end_min=10, level=2, content="P1")
        mark_consolidated(conn, [e1.id], p1.id)
        c1 = _add_message(adapter, 20, 600)
        e2 = _entry(conn, [c1], start_min=20, end_min=20, content="E2")
        p2 = _entry(conn, [e2.id], start_min=20, end_min=20, level=2, content="P2")
        mark_consolidated(conn, [e2.id], p2.id)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert len(plan.items) == 1
        # 材料が 0.5U に届かなくても、P1 の子と P2 の子を一つに合体しない
        assert plan.items[0].absorbed_entry_ids == [e1.id]

    def test_upper_regens_not_mixed_into_consolidation_calls(
        self, adapter, monkeypatch,
    ):
        """[Codex 4] 上位再生成は consolidation_calls (CLI の max_folds に直結)
        へ混ぜず、独立のフィールドで数える。"""
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        e = _entry(conn, [n1], start_min=10, end_min=10)
        p = _entry(conn, [e.id], start_min=10, end_min=10, level=2)
        mark_consolidated(conn, [e.id], p.id)
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        est = estimate_chronicle_generation_cost(conn, model_name="mock-model")
        assert est.upper_regen_calls == 1
        assert est.consolidation_calls == 0
        assert est.estimated_llm_calls == est.level1_calls + 1

    def test_upper_regen_conflict_keeps_stale(self, adapter):
        """[Codex 5] LLM の間に子集合が変わったら本文を上書きせず、stale の印を
        残して次の flush へ回す (raise しない)。"""
        import json as _json

        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        p = _entry(
            conn, [entry_a.id], start_min=0, end_min=0,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id], p.id)
        _set_stale(conn, p.id)

        class _MutatingClient(_Client):
            def generate(self, messages, tools):
                row = conn.execute(
                    "SELECT metadata FROM memopedia_pages WHERE id = ?",
                    (p.id,),
                ).fetchone()
                meta = _json.loads(row[0])
                meta["source_ids"] = list(meta.get("source_ids") or []) + [
                    f"ghost-{len(self.prompts)}"
                ]
                conn.execute(
                    "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                    (_json.dumps(meta, ensure_ascii=False), p.id),
                )
                conn.commit()
                return super().generate(messages, tools)

        from sai_memory.arasuji.absorption import regenerate_upper_entry
        outcome = regenerate_upper_entry(conn, _MutatingClient(), p.id)
        assert outcome is None
        assert get_entry(conn, p.id).content == "P (古い本文)"
        assert _is_stale(conn, p.id)
        # flush 経由でも例外にせず、印と未完了マーカーが残って帯が再実行を促す
        result = run_absorption(conn, _MutatingClient(), None)
        assert result.regenerated_upper_ids == []
        assert is_repair_incomplete(conn)

    def test_confirm_recheck_withdraws_when_run_covered_during_llm(self, adapter):
        """[Codex R1] LLM の間に別プロセス (CLI 等) が run を被覆したら、確定
        直前の再検査が拾って合体を取り下げる (二重被覆を作らない)。"""
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )

        class _RacingClient(_Client):
            def generate(self, messages, tools):
                if not self.prompts:  # 最初の合体呼び出しの最中にだけ割り込む
                    _entry(conn, [gap], start_min=0, end_min=0,
                           content="CLI が被覆")
                return super().generate(messages, tools)

        result = run_absorption(conn, _RacingClient(), plan)
        assert result.skipped_items == 1
        assert result.merged_entries == []
        # 旧隣人は無傷。gap を覆うのは割り込んだ側の 1 枚だけ (合体は取り下げ)
        assert get_entry(conn, entry.id) is not None
        covering = get_entries_covering_messages(conn, [gap])
        assert len(covering) == 1
        assert covering[0].content == "CLI が被覆"

    def test_upper_conflict_retry_capped_per_job(self, adapter):
        """[Codex R2] 指紋 CAS 棄却の再試行は 1 ジョブ 2 回まで — 競合が続いても
        LLM が見積もりを超えて伸びない。stale は残って次回へ延期される。"""
        import json as _json

        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        p = _entry(
            conn, [entry_a.id], start_min=0, end_min=0,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id], p.id)
        _set_stale(conn, p.id)
        # 吸収 item を 2 つ (それぞれの手前で P の flush が試行される) +
        # 末尾 flush で計 3 回の機会 — 上限 2 で止まることを見る。item の間に
        # 仕切りのエントリ D を挟む (無いと 2 つ目の穴が隣接スクープで item1 に
        # 併合され、item が 1 つになる)。
        _add_message(adapter, 100, 100)
        n1 = _add_message(adapter, 110, 600)
        _entry(conn, [n1], start_min=110, end_min=110)
        d1 = _add_message(adapter, 150, 600)
        _entry(conn, [d1], start_min=150, end_min=150, content="D")
        _add_message(adapter, 200, 100)
        n2 = _add_message(adapter, 210, 600)
        _entry(conn, [n2], start_min=210, end_min=210)

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert len(plan.items) == 2

        class _MutatingClient(_Client):
            def generate(self, messages, tools):
                row = conn.execute(
                    "SELECT metadata FROM memopedia_pages WHERE id = ?",
                    (p.id,),
                ).fetchone()
                meta = _json.loads(row[0])
                meta["source_ids"] = list(meta.get("source_ids") or []) + [
                    f"ghost-{len(self.prompts)}"
                ]
                conn.execute(
                    "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                    (_json.dumps(meta, ensure_ascii=False), p.id),
                )
                conn.commit()
                return super().generate(messages, tools)

        client = _MutatingClient()
        result = run_absorption(conn, client, plan)
        assert client.kinds().count("upper") == 2  # 3 回目は撃たれない
        assert len(result.merged_entries) == 2     # 合体は普通に進む
        assert _is_stale(conn, p.id)
        assert is_repair_incomplete(conn)

    def test_locked_db_error_fails_closed(self, adapter):
        """[Codex R3/F2] テーブル不在以外の OperationalError (ロック等) は
        下位 API (perception_buffer) でも握らず fail-closed — 吸収は失敗として
        上がり、未完了の印が残る。注入は conn.execute レベル (関数 patch では
        下位の握りをすり抜けて検証にならない — Codex 三巡 F2)。"""
        import sqlite3 as _sqlite3

        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )

        class _LockOnPerception:
            """perception_batches に触る SQL だけロック例外を返す conn 代理。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                if "perception_batches" in sql:
                    raise _sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with pytest.raises(_sqlite3.OperationalError):
            run_absorption(_LockOnPerception(conn), _Client(), plan)
        assert is_repair_incomplete(conn)
        assert get_entry(conn, entry.id) is not None  # 旧隣人は無傷

    def test_cli_limit_semantics_gate_absorption(self, adapter, monkeypatch):
        """[Codex F1] --limit の三形: 未指定 (None) と 0 = 全量・吸収あり、
        N>0 = 切り詰め + 吸収見送り。CLI と同じ判定関数と、見積もり側の
        skip_absorption/truncate_limit を固定する。"""
        from scripts.arasuji.build_arasuji_core import absorption_allowed_for_limit
        assert absorption_allowed_for_limit(None) is True    # 未指定
        assert absorption_allowed_for_limit(0) is True       # --limit 0
        assert absorption_allowed_for_limit(100) is False    # --limit 100

        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        conn = adapter.conn
        _add_message(adapter, 0, 100)                 # 極小 run
        n1 = _add_message(adapter, 10, 600)
        _entry(conn, [n1], start_min=10, end_min=10)  # 後ろの隣人
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        est_full = estimate_chronicle_generation_cost(
            conn, model_name="mock-model",
        )
        est_limited = estimate_chronicle_generation_cost(
            conn, model_name="mock-model",
            truncate_limit=100, skip_absorption=True,
        )
        assert est_full.level1_calls == 1      # 吸収 item 1 件が数えられる
        assert est_limited.level1_calls == 0   # 吸収見送り (通常チャンクも無し)

    def test_estimate_absorption_plan_failure_propagates(
        self, adapter, monkeypatch,
    ):
        """[Codex G1] 見積もり側の吸収計画の例外は 0 計上へ潰さず伝播する
        (表示 ≥ 実走 — cost-estimate エンドポイントは 500 で止まる)。"""
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        _entry(conn, [n1], start_min=10, end_min=10)
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        with patch(
            "sai_memory.arasuji.absorption.plan_absorption",
            side_effect=RuntimeError("plan down"),
        ):
            with pytest.raises(RuntimeError):
                estimate_chronicle_generation_cost(conn, model_name="mock-model")

    def test_estimate_tail_fold_estimator_failure_propagates(
        self, adapter, monkeypatch,
    ):
        """[Codex G2] 引き戻しの畳み見込みの例外も伝播 (0 でごまかさない)。"""
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        _entry(conn, [n1], start_min=0, end_min=0)
        _add_message(adapter, 10, 100)  # 帯 (末尾の未被覆 run)
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))

        def _raising_estimator(first_id, run_ids):
            raise RuntimeError("fold estimation down")

        with pytest.raises(RuntimeError):
            estimate_chronicle_generation_cost(
                conn, model_name="mock-model",
                tail_fold_estimator=_raising_estimator,
            )

    def test_missing_tables_are_tolerated(self):
        """[Codex R3] テーブル不在 (Chronicle 実績ゼロの新規 DB) は従来どおり
        縮退 — 例外にしない。"""
        import sqlite3 as _sqlite3

        from sai_memory.arasuji.absorption import list_stale_upper_ids
        bare = _sqlite3.connect(":memory:")
        try:
            assert list_stale_upper_ids(bare) == []
            assert is_repair_incomplete(bare) is False
        finally:
            bare.close()

    def test_delete_entry_updates_parent_bookkeeping(self, adapter):
        """[Codex H1] UI の個別削除 (素の delete_entry) が親の帳簿を即時に
        引き直す — 死んだ子 id の供給源を断つ。content_stale は付けない
        (手動削除を上位本文へ自動で伝えない — R6 と同族)。"""
        from sai_memory.arasuji.storage import delete_entry
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)

        assert delete_entry(conn, entry_b.id)
        p_after = get_entry(conn, p.id)
        assert p_after.source_ids == [entry_a.id]
        assert p_after.source_count == 1
        assert p_after.message_count == entry_a.message_count
        assert not _is_stale(conn, p.id)
        # 最後の子を消すと空の親は解体される
        assert delete_entry(conn, entry_a.id)
        assert get_entry(conn, p.id) is None

    def test_upper_regen_drops_dead_source_ids(self, adapter):
        """[Codex H2] stale の後に子が消えても、再生成は帳簿を生存子で
        引き直してから書く — source_ids に死んだ id を残して stale だけ
        消さない。"""
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)
        _set_stale(conn, p.id)
        # 残骸の再現: B の行だけ直接消す (帳簿更新を通さない crash 相当)
        conn.execute("DELETE FROM memopedia_pages WHERE id = ?", (entry_b.id,))
        conn.commit()

        from sai_memory.arasuji.absorption import regenerate_upper_entry
        outcome = regenerate_upper_entry(conn, _Client(), p.id)
        assert outcome is True
        p_after = get_entry(conn, p.id)
        assert p_after.source_ids == [entry_a.id]
        assert p_after.content == _Client.UPPER
        assert not _is_stale(conn, p.id)

    def test_delete_routes_hold_the_beat_lock(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex J2] 個別削除 / 全削除の API ルートは Beat ロックで補修ジョブ・
        Metabolism と直列化される (削除は錠の内側で走る)。"""
        from contextlib import contextmanager
        from pathlib import Path
        from types import SimpleNamespace

        import api.routes.people.arasuji as route_module
        from sai_memory.arasuji import storage as arasuji_storage

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 100)
        entry = _entry(conn, [n1], start_min=0, end_min=0)
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        monkeypatch.setattr(
            route_module, "get_persona_memory_db", lambda pid: Path(db_file),
        )
        manager = SimpleNamespace(SessionLocal=session_factory)
        events = []

        @contextmanager
        def _recording_hold(manager_, persona_id_, purpose="", check_gate=True):
            events.append(("enter", purpose, check_gate))
            try:
                yield
            finally:
                events.append(("exit", purpose))

        real_delete = arasuji_storage.delete_entry

        def _observing_delete(conn_, eid):
            events.append(("delete",))
            return real_delete(conn_, eid)

        with patch("sea.beat_gate.hold_beat", _recording_hold), patch(
            "sai_memory.arasuji.storage.delete_entry", _observing_delete,
        ):
            result = route_module.delete_arasuji_entry(
                PERSONA_ID, entry.id, manager=manager,
            )
            assert result["success"] is True
            idx_enter = events.index(("enter", "arasuji_delete", False))
            idx_delete = events.index(("delete",))
            idx_exit = events.index(("exit", "arasuji_delete"))
            assert idx_enter < idx_delete < idx_exit

            events.clear()
            result2 = route_module.delete_all_arasuji_entries(
                PERSONA_ID, manager=manager,
            )
            assert result2["success"] is True
            assert ("enter", "arasuji_delete", False) in events

    def test_patch_and_regenerate_routes_hold_the_beat_lock(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex K3] PATCH (本文編集) と POST regenerate も Beat ロックの
        内側で書く — J2 (削除) の掃き漏れの同族掃討。"""
        import asyncio
        from contextlib import contextmanager
        from pathlib import Path
        from types import SimpleNamespace

        import api.routes.people.arasuji as route_module
        from api.routes.people.models import UpdateArasujiEntryRequest
        from sai_memory.arasuji import storage as arasuji_storage

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 100)
        entry = _entry(conn, [n1], start_min=0, end_min=0)
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        monkeypatch.setattr(
            route_module, "get_persona_memory_db", lambda pid: Path(db_file),
        )
        manager = SimpleNamespace(SessionLocal=session_factory)
        events = []

        @contextmanager
        def _recording_hold(manager_, persona_id_, purpose="", check_gate=True):
            events.append(("enter", purpose, check_gate))
            try:
                yield
            finally:
                events.append(("exit", purpose))

        real_update = arasuji_storage.update_entry_content

        def _observing_update(conn_, eid, content):
            events.append(("update",))
            return real_update(conn_, eid, content)

        def _fake_regenerate(conn_, eid, persona_id=None):
            events.append(("regenerate",))
            return SimpleNamespace(id="new-id", content="再生成本文")

        with patch("sea.beat_gate.hold_beat", _recording_hold), patch(
            "sai_memory.arasuji.storage.update_entry_content",
            _observing_update,
        ), patch(
            "sai_memory.arasuji.storage.regenerate_entry", _fake_regenerate,
        ):
            result = route_module.update_arasuji_entry(
                PERSONA_ID, entry.id,
                UpdateArasujiEntryRequest(content="編集後"), manager=manager,
            )
            assert result["success"] is True
            idx_enter = events.index(("enter", "arasuji_edit", False))
            idx_update = events.index(("update",))
            idx_exit = events.index(("exit", "arasuji_edit"))
            assert idx_enter < idx_update < idx_exit

            events.clear()
            result2 = asyncio.run(route_module.regenerate_arasuji_entry(
                PERSONA_ID, entry.id, manager=manager,
            ))
            assert result2["success"] is True
            idx_enter = events.index(("enter", "arasuji_regenerate", False))
            idx_regen = events.index(("regenerate",))
            idx_exit = events.index(("exit", "arasuji_regenerate"))
            assert idx_enter < idx_regen < idx_exit

    def test_delete_and_update_parent_refreshes_bookkeeping(self, adapter):
        """[Codex J5] 取り下げ・差し替えが通る delete_entry_and_update_parent
        も、親の帳簿 (span / counts) を現況から引き直す (delete_entry と同じ着地)。"""
        from sai_memory.arasuji.storage import delete_entry_and_update_parent
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        b1 = _add_message(adapter, 10, 600)
        entry_b = _entry(conn, [b1], start_min=10, end_min=10, content="B")
        p = _entry(
            conn, [entry_a.id, entry_b.id], start_min=0, end_min=10,
            level=2, content="P",
        )
        mark_consolidated(conn, [entry_a.id, entry_b.id], p.id)

        ok, parent_id = delete_entry_and_update_parent(conn, entry_b.id)
        assert ok and parent_id == p.id
        p_after = get_entry(conn, p.id)
        assert p_after.source_ids == [entry_a.id]
        assert p_after.source_count == 1
        assert p_after.message_count == entry_a.message_count
        assert not _is_stale(conn, p.id)

    def test_all_db_access_runs_under_the_db_lock(self, adapter):
        """[Codex 八巡] run_absorption の DB 接触 (sweep・マーカー上げ下げ・
        材料読み・生成保存・差し替え・上位再生成・末尾判定) は LLM 以外すべて
        db_lock の内側 — 錠の外の execute/commit/rollback を 1 件でも検出
        したら失敗する機械検査。"""
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        e = _entry(conn, [n1], start_min=10, end_min=10)
        p = _entry(conn, [e.id], start_min=10, end_min=10, level=2)
        mark_consolidated(conn, [e.id], p.id)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        lock = _RecordingLock()
        proxy = _LockAssertingConn(conn, lock)
        result = run_absorption(proxy, _Client(), plan, db_lock=lock)
        assert proxy.violations == []
        assert len(result.merged_entries) == 1
        assert result.regenerated_upper_ids == [p.id]
        assert not is_repair_incomplete(conn)

    def test_withdraw_path_runs_under_the_db_lock(self, adapter):
        """[Codex 八巡] CAS/再検査での撤去 (_withdraw) も錠の内側で走る。"""
        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )

        class _RacingClient(_Client):
            def generate(self, messages, tools):
                if not self.prompts:
                    # 別の書き手 (生 conn 経由 — 検査対象外) が run を被覆する
                    _entry(conn, [gap], start_min=0, end_min=0,
                           content="別の書き手が被覆")
                return super().generate(messages, tools)

        lock = _RecordingLock()
        proxy = _LockAssertingConn(conn, lock)
        result = run_absorption(proxy, _RacingClient(), plan, db_lock=lock)
        assert proxy.violations == []
        assert result.skipped_items == 1
        assert result.merged_entries == []
        assert get_entry(conn, entry.id) is not None

    def test_fragment_repoint_commit_failure_rolls_back(self, adapter):
        """[Codex 十二巡 Q2 同族] 帳簿付け替えの ``commit()`` 失敗も、未確定の
        UPDATE を明示的に巻き戻してから撤去する。

        巻き戻さないと、未確定の付け替えが**巻き戻し処理や撤去の commit に
        相乗りして確定**し、Fragment が撤去済みの合体エントリを指す。
        """
        import sqlite3 as _sqlite3

        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        _insert_fragment(conn, "frag-q2", entry.id)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )

        class _FailFragmentCommit:
            """Fragment の付け替え UPDATE は通し、その commit だけ 1 回失敗させる。"""

            def __init__(self, real):
                self._real = real
                self._armed = False
                self.fired = False

            def execute(self, sql, *a, **k):
                if (
                    "UPDATE memopedia_fragments" in sql
                    and "WHERE chronicle_entry_id = ?" in sql
                    and not self.fired
                ):
                    self._armed = True
                return self._real.execute(sql, *a, **k)

            def commit(self):
                if self._armed:
                    self._armed = False
                    self.fired = True
                    raise _sqlite3.OperationalError("database is locked")
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = _FailFragmentCommit(conn)
        with pytest.raises(AbsorptionError):
            run_absorption(proxy, _Client(), plan)

        assert proxy.fired
        # 旧隣人は無傷・合体エントリは撤去済み・Fragment は旧 entry のまま
        assert get_entry(conn, entry.id) is not None
        assert get_entries_covering_messages(conn, [gap]) == []
        row = conn.execute(
            "SELECT chronicle_entry_id FROM memopedia_fragments "
            "WHERE id = 'frag-q2'"
        ).fetchone()
        assert row[0] == entry.id

    def test_adopted_merge_entry_is_not_withdrawn(self, adapter):
        """[Codex M1] 取り下げ対象 (合体エントリ) が生成〜取り下げの間に別の
        書き手に採用されていたら (親付け / 統合 / 編集)、消さずに残す —
        次回補修が再計画する。"""
        import sai_memory.arasuji.absorption as absorption_module

        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        p = _entry(conn, [], start_min=0, end_min=20, level=2, content="採用先")
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        real_covered = absorption_module._covered_run_count
        adopted = {}

        def _adopting_covered(conn_, run_ids, *, exclude_entry_id=None):
            if exclude_entry_id is None:
                return real_covered(conn_, run_ids)
            # 確定直前の再検査の瞬間に、別の書き手が合体エントリを採用した
            from sai_memory.arasuji.storage import add_to_parent_source_ids
            add_to_parent_source_ids(conn, exclude_entry_id, p.id)
            adopted["id"] = exclude_entry_id
            return 1  # 競合ありとして取り下げ経路へ

        with patch(
            "sai_memory.arasuji.absorption._covered_run_count",
            _adopting_covered,
        ):
            result = run_absorption(conn, _Client(), plan)
        assert result.skipped_items == 1
        merged = get_entry(conn, adopted["id"])
        assert merged is not None          # 消されていない
        assert merged.parent_id == p.id    # 採用の形跡は無傷
        assert get_entry(conn, entry.id) is not None  # 旧隣人も無傷

    def test_adopted_merge_entry_survives_cas_conflict(self, adapter):
        """[Codex M1] CAS 競合 (旧隣人が生成中に動いた) での取り下げも条件つき。
        合体エントリが別の書き手に採用されていたら消さずに残す。"""
        import sai_memory.arasuji.absorption as absorption_module
        from sai_memory.arasuji.storage import (
            add_to_parent_source_ids,
            update_entry_content,
        )

        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        p = _entry(conn, [], start_min=0, end_min=20, level=2, content="採用先")
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )

        known = {entry.id, p.id}
        reads = {"neighbor": 0}
        adopted = {}
        real_get = absorption_module.get_entry

        def _racing_get(conn_, entry_id):
            if entry_id == entry.id:
                reads["neighbor"] += 1
                if reads["neighbor"] == 2:
                    # 1 回目 = スナップショット、2 回目 = CAS 照合。その直前に
                    # 別の書き手が旧隣人を書き換える → CAS 競合。
                    update_entry_content(conn, entry.id, "別の書き手が編集")
            elif entry_id not in known and "id" not in adopted:
                # 取り下げ直前の読み — その瞬間に合体エントリが親へ採用される
                adopted["id"] = entry_id
                add_to_parent_source_ids(conn, entry_id, p.id)
            return real_get(conn_, entry_id)

        with patch(
            "sai_memory.arasuji.absorption.get_entry", _racing_get,
        ):
            result = run_absorption(conn, _Client(), plan)

        assert result.skipped_items == 1
        assert result.merged_entries == []
        merged = get_entry(conn, adopted["id"])
        assert merged is not None          # 消されていない
        assert merged.parent_id == p.id    # 採用の形跡は無傷
        assert get_entry(conn, entry.id) is not None  # 旧隣人も無傷

    def test_untouched_merge_entry_is_withdrawn_on_cas_conflict(self, adapter):
        """[Codex M1] 逆側の固定 — 素のまま (親なし・未統合・content 一致) なら
        従来どおり削除する。条件つき化が取り下げそのものを殺していないこと。"""
        conn = adapter.conn
        _add_message(adapter, 0, 100)
        n1 = _add_message(adapter, 10, 600)
        entry = _entry(conn, [n1], start_min=10, end_min=10)
        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        before = {
            row[0] for row in conn.execute(
                "SELECT id FROM memopedia_pages"
            ).fetchall()
        }

        class _RacingClient(_Client):
            def generate(self, messages, tools):
                out = super().generate(messages, tools)
                from sai_memory.arasuji.storage import update_entry_content
                update_entry_content(conn, entry.id, "別の書き手が編集")
                return out

        result = run_absorption(conn, _RacingClient(), plan)
        assert result.skipped_items == 1
        assert result.merged_entries == []
        after = {
            row[0] for row in conn.execute(
                "SELECT id FROM memopedia_pages"
            ).fetchall()
        }
        assert after == before  # 合体エントリは撤去済み — 残骸なし
        assert get_entry(conn, entry.id) is not None

    def test_orphan_marker_clears_when_no_work_remains(self, adapter):
        """[L1] 仕事ゼロ (items なし・stale なし) の再実行は、残った未完了の
        印を外す — 帯の「前回の処理が完了していません」を永久表示にしない。"""
        from sai_memory.arasuji.absorption import set_repair_incomplete
        conn = adapter.conn
        set_repair_incomplete(conn)
        assert is_repair_incomplete(conn)
        result = run_absorption(conn, _Client(), None)
        assert result.merged_entries == []
        assert not is_repair_incomplete(conn)

    def test_stray_stale_mark_on_lv1_is_cleared(self, adapter):
        """[L4] Lv1 に迷い込んだ content_stale は raise せず印だけ外す。"""
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 100)
        e = _entry(conn, [n1], start_min=0, end_min=0)
        _set_stale(conn, e.id)
        client = _Client()
        run_absorption(conn, client, None)  # 例外にならないこと
        assert not _is_stale(conn, e.id)
        assert not is_repair_incomplete(conn)
        assert client.prompts == []  # 再生成はしない (印を外すだけ)

    def test_real_plan_tail_fractions_are_absorbed_not_tiny(self):
        """[L7] 「チャンク < 0.5U ⟺ run < 0.5U」の根拠 — 本物の plan_alignment
        で、U 以上の run の末尾の端数が直前チャンクへ吸収され、単独の
        sub-0.5U チャンクにならないことを固定する。"""
        from sai_memory.memory.storage import Message

        def _m(i, size):
            return Message(
                id=f"m{i}", thread_id="main", role="user",
                content="あ" * size, resource_id=None, created_at=i,
            )

        plan = plan_alignment(
            [_m(0, 600), _m(1, 600), _m(2, 200)], set(), target_chars=TARGET,
        )
        normal, tiny = split_plan_for_absorption(plan, target_chars=TARGET)
        assert tiny == []
        assert [c.coverage_chars for c in normal.chunks] == [1400]

        plan2 = plan_alignment(
            [_m(0, 600), _m(1, 600), _m(2, 600), _m(3, 600), _m(4, 100)],
            set(), target_chars=TARGET,
        )
        normal2, tiny2 = split_plan_for_absorption(plan2, target_chars=TARGET)
        assert tiny2 == []
        assert [c.coverage_chars for c in normal2.chunks] == [1200, 1300]


# ---------------------------------------------------------------------------
# 末尾の未被覆 run の anchor 引き戻し (裁定 5 改訂)
# ---------------------------------------------------------------------------


def _make_lifecycle(session_factory):
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle
    manager = SimpleNamespace(SessionLocal=session_factory, personas={})
    return SessionLifecycle(SimpleNamespace(), manager)


def _persona(adapter):
    from types import SimpleNamespace
    return SimpleNamespace(
        persona_id=PERSONA_ID, persona_name="テスター", model="claude-x",
        sai_memory=adapter,
    )


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


def _row_anchor(lc, model):
    entry = lc.load_anchor_entry(PERSONA_ID, model) or {}
    return entry.get("anchor_id")


class TestTailRewind:
    """run_tail_rewind / plan_tail_rewind (sea/coverage_repair.py) を固定する。

    - 帯 (後ろに編纂済みの無い末尾の極小 run 群) は anchor 引き戻しで窓に入る
      (LLM ゼロの帳簿操作)。
    - 戻す行 = 境界の行 (最古の温かい anchor)。温かい行が無ければ最古の冷えた行。
    - anchor 行なし / 帯が既に窓の中 → 何もしない。
    - 引き戻し後の窓が上限 (high) を超えたらジョブ内で即座に畳む。
    - 冪等: 引き戻し後の再実行は帯を数えない。
    """

    def _tail_fixture(self, adapter, *, anchor_chars=50):
        """entry に覆われた n1,n2 + 未被覆の極小 gap + 境界メッセージ m_a。"""
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        n2 = _add_message(adapter, 1, 300)
        _entry(conn, [n1, n2], start_min=0, end_min=1)
        gap = _add_message(adapter, 10, 100)
        m_a = _add_message(adapter, 20, anchor_chars)
        return gap, m_a

    def test_warm_boundary_anchor_is_rewound(self, adapter, session_factory, monkeypatch):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)

        status = run_tail_rewind(lc, _persona(adapter))
        # 引き戻しのみ (このモデルに水位設定が無い = 畳み判定なし)。LLM は
        # 一切呼ばれない (client がそもそも存在しない)。
        assert status == "rewound"
        assert _row_anchor(lc, "model-a") == gap

        # 冪等: 帯は窓の中に入ったので、再実行は何も見つけない
        assert run_tail_rewind(lc, _persona(adapter)) == "none"

    def test_oldest_cold_row_is_rewound_when_no_warm(
        self, adapter, session_factory, monkeypatch,
    ):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        m_b = _add_message(adapter, 30, 50)
        lc = _make_lifecycle(session_factory)
        _cold_row(lc, "model-a", m_a)
        _cold_row(lc, "model-b", m_b)

        status = run_tail_rewind(lc, _persona(adapter))
        assert status == "rewound"
        # 最古の anchor の行 (model-a) だけが引き戻される
        assert _row_anchor(lc, "model-a") == gap
        assert _row_anchor(lc, "model-b") == m_b

    def test_zone_already_inside_a_window_is_skipped(
        self, adapter, session_factory, monkeypatch,
    ):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        _entry(conn, [n1], start_min=0, end_min=0)
        _add_message(adapter, 10, 100)  # 帯
        lc = _make_lifecycle(session_factory)
        # anchor が帯より古い冷えた行 — 帯は既にこの窓の中 (§16-1 成立済み)
        _cold_row(lc, "model-a", n1)

        status = run_tail_rewind(lc, _persona(adapter))
        assert status == "skipped"
        assert _row_anchor(lc, "model-a") == n1  # 動かさない (前進もしない)

    def test_no_anchor_rows_does_nothing(
        self, adapter, session_factory, monkeypatch,
    ):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        # 行ゼロ → 初会話の bootstrap (§16-3) に任せて何もしない
        assert run_tail_rewind(lc, _persona(adapter)) == "skipped"

    def _patched_window(self, chars):
        from types import SimpleNamespace
        return SimpleNamespace(
            presented=[{"id": "w1", "content": "あ" * chars, "metadata": None}],
            raw=[], folds=[], anchor_id=None,
        )

    def test_fold_runs_in_job_when_over_high(
        self, adapter, session_factory, monkeypatch,
    ):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from types import SimpleNamespace
        from unittest.mock import Mock

        from sea.coverage_repair import run_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        lc.get_presented_window = lambda p, mk, aid=None: self._patched_window(300)
        lc.get_metabolism_watermarks = (
            lambda p, mk=None: SimpleNamespace(high=100, target=50)
        )
        lc.run_manual_compaction = Mock(return_value="ok")

        status = run_tail_rewind(lc, _persona(adapter))
        assert status == "rewound_folded"
        assert _row_anchor(lc, "model-a") == gap
        lc.run_manual_compaction.assert_called_once()
        assert lc.run_manual_compaction.call_args.kwargs["model_key"] == "model-a"

    def test_fold_not_run_when_under_high(
        self, adapter, session_factory, monkeypatch,
    ):
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from types import SimpleNamespace
        from unittest.mock import Mock

        from sea.coverage_repair import run_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        lc.get_presented_window = lambda p, mk, aid=None: self._patched_window(80)
        lc.get_metabolism_watermarks = (
            lambda p, mk=None: SimpleNamespace(high=100, target=50)
        )
        lc.run_manual_compaction = Mock(return_value="ok")

        status = run_tail_rewind(lc, _persona(adapter))
        assert status == "rewound"
        assert _row_anchor(lc, "model-a") == gap
        lc.run_manual_compaction.assert_not_called()

    def test_strict_anchor_read_failure_propagates_from_plan(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex M2] plan_tail_rewind は strict 読み — 読めないを「行なし」に
        潰さず伝播する (見積もり側は 500 に着地)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import plan_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        lc.load_anchor_entries_strict = lambda pid: (
            (_ for _ in ()).throw(RuntimeError("strict down"))
        )
        with pytest.raises(RuntimeError):
            plan_tail_rewind(lc, _persona(adapter), adapter.conn, gap)

    def test_plan_failure_returns_failed_from_run(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex M2] 実行側は計画の失敗を "failed" で返す (行は動かさない)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        _gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        with patch(
            "sea.coverage_repair.plan_tail_rewind",
            side_effect=RuntimeError("plan down"),
        ):
            assert run_tail_rewind(lc, _persona(adapter)) == "failed"
        assert _row_anchor(lc, "model-a") == m_a  # anchor は据え置き

    def test_rewind_failure_maps_repair_status_to_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex M2] run_coverage_repair は引き戻しの失敗を status="failed"
        に写像する (編纂は確定済み — 再実行が引き戻しだけやり直す)。"""
        lc = _make_lifecycle(session_factory)
        lc.generate_chronicle = lambda *a, **k: "ok"
        lc.is_chronicle_enabled_for_persona = lambda p: True
        with patch(
            "sea.coverage_repair.run_tail_rewind", return_value="failed",
        ), patch(
            "sea.coverage_repair.mark_covered_cold_windows",
            return_value=(0, 0),
        ):
            status, _mark_failures = lc.run_coverage_repair(_persona(adapter))
        assert status == "failed"

    def test_zone_resolution_failure_returns_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex N1] 帯の解決失敗は "skipped" へ潰さず "failed" を返す —
        「帯が本当に無い (none)」と「解決に失敗した」は別の事実。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        _gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        with patch(
            "sea.coverage_repair._resolve_uncovered_tail",
            side_effect=RuntimeError("zone down"),
        ):
            assert run_tail_rewind(lc, _persona(adapter)) == "failed"
        assert _row_anchor(lc, "model-a") == m_a  # anchor は据え置き

    def _break_row_updates(self, lc):
        """行の UPDATE だけが DB 例外になる SessionLocal 代理を仕込む。

        読み (計画・anchor の strict 読み・位置照会) はそのまま通り、書き込み
        だけが落ちる — CAS 不一致 (行は読めて anchor が違うだけ) と区別できる
        形で「書き込みの失敗」を再現するため。
        """
        real_factory = lc.manager.SessionLocal

        class _Query:
            def __init__(self, real):
                self._real = real

            def filter_by(self, **kwargs):
                return _Query(self._real.filter_by(**kwargs))

            def update(self, *args, **kwargs):
                raise RuntimeError("db write down")

            def __getattr__(self, name):
                return getattr(self._real, name)

        class _Session:
            def __init__(self, real):
                self._real = real

            def query(self, *args, **kwargs):
                return _Query(self._real.query(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self._real, name)

        lc.manager.SessionLocal = lambda: _Session(real_factory())

    def test_write_failure_returns_failed_not_cas_rejected(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex 十一巡 P1] 引き戻しの書き込みが DB で失敗したら "failed" —
        CAS 棄却 (意図した見送り) へ潰さない。潰すと補修が成功の顔で終わり、
        引き戻されていない末尾が残ったことが誰にも分からない (裁定 6)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        _gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        self._break_row_updates(lc)

        assert run_tail_rewind(lc, _persona(adapter)) == "failed"
        assert _row_anchor(lc, "model-a") == m_a  # 行は動いていない

    def test_pure_cas_mismatch_stays_cas_rejected(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex 十一巡 P1] 対: 書き込みは生きていて anchor が動いていただけ
        なら従来どおり "cas_rejected" (次回が再計画する意図した見送り)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from dataclasses import replace

        from sea.coverage_repair import plan_tail_rewind, run_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        plan = plan_tail_rewind(lc, _persona(adapter), adapter.conn, gap)
        assert plan is not None
        # 計画〜書き込みの間に anchor が動いた形 (CAS の前提が現況と違う)。
        moved = replace(plan, expected_anchor_id="anchor-that-moved")
        with patch(
            "sea.coverage_repair.plan_tail_rewind", return_value=moved,
        ):
            assert run_tail_rewind(lc, _persona(adapter)) == "cas_rejected"
        assert _row_anchor(lc, "model-a") == m_a

    def test_write_failure_maps_repair_status_to_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex 十一巡 P1] 書き込み失敗は補修の status="failed" まで届く
        (実 run_tail_rewind / 実 _write_refill 経由で経路ごと固定する)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        _gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        lc.generate_chronicle = lambda *a, **k: "ok"
        lc.is_chronicle_enabled_for_persona = lambda p: True
        _warm_row(lc, "model-a", m_a)
        self._break_row_updates(lc)
        with patch(
            "sea.coverage_repair.mark_covered_cold_windows",
            return_value=(0, 0),
        ):
            status, _mark_failures = lc.run_coverage_repair(_persona(adapter))
        assert status == "failed"
        assert _row_anchor(lc, "model-a") == m_a

    def _repair_status(self, adapter, session_factory, monkeypatch, **patches):
        """run_coverage_repair を編纂 "ok" 固定で回し、返る status を得る。"""
        lc = _make_lifecycle(session_factory)
        lc.generate_chronicle = lambda *a, **k: "ok"
        lc.is_chronicle_enabled_for_persona = lambda p: True
        with patch(
            "sea.coverage_repair.mark_covered_cold_windows",
            return_value=(0, 0),
        ), patch("sea.coverage_repair.run_tail_rewind", **patches):
            status, _mark_failures = lc.run_coverage_repair(_persona(adapter))
        return status

    def test_zone_resolution_failure_maps_repair_status_to_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex N1] 帯の解決失敗は補修の status="failed" まで届く
        (実 run_tail_rewind 経由 — 写像だけでなく経路ごと固定する)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        _gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        lc.generate_chronicle = lambda *a, **k: "ok"
        lc.is_chronicle_enabled_for_persona = lambda p: True
        _warm_row(lc, "model-a", m_a)
        with patch(
            "sea.coverage_repair._resolve_uncovered_tail",
            side_effect=RuntimeError("zone down"),
        ), patch(
            "sea.coverage_repair.mark_covered_cold_windows",
            return_value=(0, 0),
        ):
            status, _mark_failures = lc.run_coverage_repair(_persona(adapter))
        assert status == "failed"
        assert _row_anchor(lc, "model-a") == m_a

    def test_fold_failure_maps_repair_status_to_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex N2] 引き戻しは成功したが窓の畳みが未完 — 成功扱いにしない。

        再実行は畳みを再試行しない (帯はもう窓の中) ので、失敗表示の意味は
        「全部は終わらなかった」の申告。窓は次の会話の非常畳み (§14-3) が
        回復する。
        """
        assert self._repair_status(
            adapter, session_factory, monkeypatch,
            return_value="rewound_fold_failed",
        ) == "failed"

    def test_unknown_rewind_status_maps_repair_status_to_failed(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex N2] 白名簿に無い未知の状態語も失敗側 (黙って通さない)。"""
        assert self._repair_status(
            adapter, session_factory, monkeypatch,
            return_value="some_future_status",
        ) == "failed"

    @pytest.mark.parametrize("rewind_status", [
        "none", "rewound", "rewound_folded", "skipped", "cas_rejected",
    ])
    def test_intended_rewind_statuses_keep_repair_ok(
        self, adapter, session_factory, monkeypatch, rewind_status,
    ):
        """[Codex N2] 完了と意図した見送りは "ok" のまま
        (skipped = 行なし/既に窓の中、cas_rejected = 次回再計画)。"""
        assert self._repair_status(
            adapter, session_factory, monkeypatch,
            return_value=rewind_status,
        ) == "ok"

    def test_rewind_preserves_fold_records_without_rereading(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex K2] 引き戻しは計画時に持ち越した圧縮区間記録を書く — 行の
        再読みに一切依存せず、既存の記録が空で上書きされない (データ喪失級の
        再読み失敗経路の根絶)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import run_tail_rewind
        from sea.session_window import FoldedRange, deserialize_folds

        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        fold = FoldedRange(
            message_ids=[m_a], start_at=None, end_at=None,
            chronicle_entry_ids=["e-1"], chronicle_short_ids=[],
            presented_raw=True,
        )
        assert lc.write_folds_if_anchor_unchanged(
            PERSONA_ID, "model-a", m_a, [fold],
        )
        # 書き込み直前の行再読みが死んでいても引き戻しは成立する
        # (= 再読みへの依存が無いことの証明)
        lc.load_anchor_entry = lambda *a, **k: (
            (_ for _ in ()).throw(RuntimeError("reread down"))
        )
        status = run_tail_rewind(lc, _persona(adapter))
        assert status == "rewound"
        entries = lc.load_anchor_entries(PERSONA_ID)
        assert entries["model-a"]["anchor_id"] == gap
        folds_after = deserialize_folds(entries["model-a"]["folded_ranges"])
        assert [f.chronicle_entry_ids for f in folds_after] == [["e-1"]]

    def test_estimate_counts_the_post_rewind_fold(
        self, adapter, session_factory, monkeypatch,
    ):
        """cost-estimate は引き戻し (0 コール) に伴う即時畳みの見込みを
        tail_fold_estimator 経由で加算する (表示と実走の一致)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        self._tail_fixture(adapter)

        base = estimate_chronicle_generation_cost(
            adapter.conn, model_name="mock-model",
        )
        with_fold = estimate_chronicle_generation_cost(
            adapter.conn, model_name="mock-model",
            tail_fold_estimator=lambda first_id, run_ids: (3, 3000),
        )
        assert with_fold.estimated_llm_calls == base.estimated_llm_calls + 3

    def test_position_lookup_failure_propagates_from_plan(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex M2] _anchor_positions の位置照会例外 (接続レベルの失敗) は
        「この行は位置が引けない」へ潰さず伝播する — 欠けた集合で境界行を
        選ばない。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        import sai_memory.memory.storage as mem_storage

        from sea.coverage_repair import plan_tail_rewind
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        real_pos = mem_storage.get_message_position

        def _boom(conn_, message_id):
            if message_id == m_a:
                raise RuntimeError("position lookup down")
            return real_pos(conn_, message_id)

        with patch(
            "sai_memory.memory.storage.get_message_position", _boom,
        ), pytest.raises(RuntimeError):
            plan_tail_rewind(lc, _persona(adapter), adapter.conn, gap)

    def test_estimate_propagates_strict_anchor_read_failure(
        self, adapter, session_factory, monkeypatch,
    ):
        """[Codex M2] 見積もり側 (estimate_tail_rewind_fold) も strict 読みの
        失敗を伝播する — 0 でごまかすと引き戻し後の畳みが表示なしで課金される
        (エンドポイントは 500 に着地)。"""
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        from sea.coverage_repair import estimate_tail_rewind_fold
        gap, m_a = self._tail_fixture(adapter)
        lc = _make_lifecycle(session_factory)
        _warm_row(lc, "model-a", m_a)
        lc.load_anchor_entries_strict = lambda pid: (
            (_ for _ in ()).throw(RuntimeError("strict down"))
        )
        with pytest.raises(RuntimeError):
            estimate_tail_rewind_fold(lc, _persona(adapter), adapter.conn, gap)
        # 見積もり本体 (estimate_chronicle_generation_cost) が callback の例外を
        # 握り潰さないことは TestReviewFixes の G2 テストで固定済み — 二枚
        # 揃って「strict 読みの失敗 → エンドポイント 500」になる。


# ---------------------------------------------------------------------------
# 削除経路の埋め込み道連れ (帳簿調査表の裁定)
# ---------------------------------------------------------------------------


class TestCliMaintenanceAlwaysRuns:
    """[Codex 十一巡 P2] 全量再編纂スクリプトの保守経路。

    通常チャンクも新規吸収も無い回でも、run_absorption が担う保守 —
    壊れた親の sweep / 前回の未完了 (content_stale) の flush /
    repair_incomplete マーカーの解除 — は必ず走らなければならない。飛ばすと
    Chronicle タブの帯の「前回の処理が完了していません。再実行してください」
    (裁定 6) が、再実行しても消えない状態になる。
    """

    def _run_cli(self, tmp_path, monkeypatch, client, *extra_argv):
        import scripts.arasuji.build_arasuji_core as cli

        monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        monkeypatch.setattr(
            cli, "find_model_config",
            lambda name: (
                "mock-model",
                {
                    "provider": "mock", "model": "mock-model",
                    "context_length": 1000,
                },
            ),
        )
        monkeypatch.setattr(
            cli.sys, "argv",
            ["build_arasuji.py", PERSONA_ID, "--yes", *extra_argv],
        )
        with patch(
            "llm_clients.factory.get_llm_client", return_value=client,
        ), pytest.raises(SystemExit) as exc:
            cli.run_cli()
        return exc.value.code

    def test_marker_only_run_clears_the_marker(
        self, adapter, tmp_path, monkeypatch,
    ):
        """編纂対象ゼロ + マーカーだけ残った回でもマーカーは解ける。"""
        from sai_memory.arasuji.absorption import set_repair_incomplete
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 600)
        _entry(conn, [n1], start_min=0, end_min=0)   # 全部被覆済み
        set_repair_incomplete(conn)
        assert is_repair_incomplete(conn)

        client = _Client()
        assert self._run_cli(tmp_path, monkeypatch, client) == 0
        assert not is_repair_incomplete(conn)
        assert client.prompts == []   # 保守だけ — LLM は呼ばない

    def test_limit_run_flushes_the_previous_incomplete(
        self, adapter, tmp_path, monkeypatch,
    ):
        """--limit N>0 で新規吸収を見送る回でも、前回の未完了の flush は走る。

        見積もり (skip_absorption=True) が upper_regen_calls として表示する
        仕事そのものなので、実行が飛ばすと「表示したのに走らない」になる。
        """
        from sai_memory.arasuji.absorption import set_repair_incomplete
        from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 600)
        child = _entry(conn, [n1], start_min=0, end_min=0, content="A")
        parent = _entry(
            conn, [child.id], start_min=0, end_min=0,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [child.id], parent.id)
        _set_stale(conn, parent.id)
        set_repair_incomplete(conn)

        monkeypatch.setenv("SAIVERSE_CHRONICLE_BAND_BUDGET", str(TARGET))
        est = estimate_chronicle_generation_cost(
            conn, model_name="mock-model",
            truncate_limit=100, skip_absorption=True,
        )
        assert est.upper_regen_calls == 1   # 表示する仕事は 1 件

        client = _Client()
        assert self._run_cli(
            tmp_path, monkeypatch, client, "--limit", "100",
        ) == 0
        assert client.kinds() == ["upper"]  # 実走も 1 件 (表示と一致)
        assert get_entry(conn, parent.id).content == _Client.UPPER
        assert not _is_stale(conn, parent.id)
        assert not is_repair_incomplete(conn)

    def test_broken_parent_is_swept_when_nothing_to_compile(
        self, adapter, tmp_path, monkeypatch,
    ):
        """編纂対象ゼロの回でも、死んだ子 id を指す親の sweep は走る。

        crash 残骸の形: 親の source_ids に、もう存在しない子 id が残っている。
        メッセージ側は全部被覆済みなので通常チャンクはゼロ — 早期終了が
        sweep を飛ばすと、壊れた親は誰にも直されない。
        """
        conn = adapter.conn
        a1 = _add_message(adapter, 0, 600)
        entry_a = _entry(conn, [a1], start_min=0, end_min=0, content="A")
        parent = _entry(
            conn, [entry_a.id, "dead-child-id"], start_min=0, end_min=0,
            level=2, content="P (古い本文)",
        )
        mark_consolidated(conn, [entry_a.id], parent.id)

        client = _Client()
        assert self._run_cli(tmp_path, monkeypatch, client) == 0
        parent_after = get_entry(conn, parent.id)
        assert parent_after.source_ids == [entry_a.id]
        assert parent_after.content == _Client.UPPER
        assert not _is_stale(conn, parent.id)
        assert not is_repair_incomplete(conn)


class TestEmbeddingCascade:
    def test_delete_entry_drops_the_embedding_row(self, adapter):
        from sai_memory.arasuji.storage import delete_entry
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 100)
        entry = _entry(conn, [n1], start_min=0, end_min=0)
        conn.execute(
            "INSERT INTO arasuji_embeddings (entry_id, vector) VALUES (?, '[]')",
            (entry.id,),
        )
        conn.commit()
        assert delete_entry(conn, entry.id)
        row = conn.execute(
            "SELECT 1 FROM arasuji_embeddings WHERE entry_id = ?", (entry.id,)
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# Lv1 → 消えたメッセージの掃除 (2026-09-01 まはー裁定)
# ---------------------------------------------------------------------------


def _meta_json(conn, entry_id):
    row = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
    ).fetchone()
    return row[0]


class TestSweepDeadMessageSources:
    """UI のメッセージ削除が残した孤児 source_ids を掃く (_sweep_broken_parents
    の Lv1 → メッセージ版)。掃かないと隣人の再開検査が落ち、その隣の未被覆
    断片が永久に取り残される。"""

    def test_dead_ids_are_dropped_and_counts_re_derived(self, adapter):
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        n2 = _add_message(adapter, 1, 300)
        entry = _entry(
            conn, [n1, "dead-1", n2, "dead-2"], start_min=0, end_min=1,
        )
        assert entry.source_count == 4

        removed = _sweep_dead_message_sources(conn)

        assert removed == {entry.id: ["dead-1", "dead-2"]}
        after = get_entry(conn, entry.id)
        assert after.source_ids == [n1, n2]
        assert after.source_count == 2
        assert after.message_count == 2
        # 本文と span は帳簿補修の対象外 (保存則 — 編纂は本文を作らない)
        assert after.content == entry.content
        assert (after.start_time, after.end_time) == (
            entry.start_time, entry.end_time,
        )

    def test_healthy_entries_are_untouched(self, adapter):
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        n2 = _add_message(adapter, 1, 300)
        entry = _entry(conn, [n1, n2], start_min=0, end_min=1)
        before = _meta_json(conn, entry.id)

        assert _sweep_dead_message_sources(conn) == {}
        assert _meta_json(conn, entry.id) == before

    def test_is_idempotent(self, adapter):
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        entry = _entry(conn, [n1, "dead-1"], start_min=0, end_min=0)

        assert _sweep_dead_message_sources(conn) == {entry.id: ["dead-1"]}
        first = _meta_json(conn, entry.id)
        # 二度目は検出ゼロで何も書かない
        assert _sweep_dead_message_sources(conn) == {}
        assert _meta_json(conn, entry.id) == first

    def test_entry_without_surviving_sources_is_left_alone(self, adapter):
        """生ログが丸ごと消えた Lv1 は触らない — source ゼロにすると何も被覆
        しない幽霊になり、本文を消せば持ち主の記憶を機構が捨てることになる。"""
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        entry = _entry(conn, ["dead-1", "dead-2"], start_min=0, end_min=1)
        before = _meta_json(conn, entry.id)

        assert _sweep_dead_message_sources(conn) == {}
        assert _meta_json(conn, entry.id) == before

    def test_parent_bookkeeping_follows_without_marking_stale(self, adapter):
        """子の counts が変わった親は引き直す (mark_stale=False — 本文の
        語り直しは起こさないので見積もりの LLM 回数は動かない)。"""
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        n2 = _add_message(adapter, 1, 300)
        child = _entry(conn, [n1, "dead-1", n2], start_min=0, end_min=1)
        parent = _entry(
            conn, [child.id], start_min=0, end_min=1, level=2, content="P",
        )
        conn.execute(
            "UPDATE memopedia_pages SET metadata = json_set("
            "metadata, '$.message_count', 3) WHERE id = ?", (parent.id,),
        )
        conn.commit()
        mark_consolidated(conn, [child.id], parent.id)

        _sweep_dead_message_sources(conn)

        parent_after = get_entry(conn, parent.id)
        assert parent_after.message_count == 2
        assert parent_after.content == "P"
        assert not _is_stale(conn, parent.id)

    def test_neighbor_with_orphans_becomes_absorbable_after_the_sweep(
        self, adapter,
    ):
        """統合: 孤児参照を持つ隣人は開き直しを拒まれ、隣の未被覆断片が取り
        残される。sweep 後は同じ計画で吸収され、断片が被覆に入る。"""
        from sai_memory.arasuji.absorption import _sweep_dead_message_sources

        conn = adapter.conn
        gap = _add_message(adapter, 0, 100)               # 取り残される断片
        n1 = _add_message(adapter, 10, 300)
        n2 = _add_message(adapter, 11, 300)
        entry = _entry(conn, [n1, "dead-1", n2], start_min=10, end_min=11)

        messages, processed, _normal, tiny = _plan(adapter)
        blocked = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert blocked.items == []
        assert blocked.unresolved_runs == 1

        assert _sweep_dead_message_sources(conn) == {entry.id: ["dead-1"]}

        messages, processed, _normal, tiny = _plan(adapter)
        plan = plan_absorption(
            conn, tiny, messages, processed, target_chars=TARGET,
        )
        assert plan.unresolved_runs == 0
        assert len(plan.items) == 1
        assert plan.items[0].run_message_ids == [gap]
        assert plan.items[0].absorbed_entry_ids == [entry.id]

        result = run_absorption(conn, _Client(), plan)
        assert len(result.merged_entries) == 1
        covering = get_entries_covering_messages(conn, [gap, n1, n2])
        assert [e.id for e in covering] == [result.merged_entries[0].id]
        assert set(covering[0].source_ids) == {gap, n1, n2}

    def test_run_absorption_sweeps_before_planning_work(self, adapter):
        """run_absorption 冒頭の保守ブロックが (仕事ゼロの回でも) 掃く。"""
        conn = adapter.conn
        n1 = _add_message(adapter, 0, 300)
        entry = _entry(conn, [n1, "dead-1"], start_min=0, end_min=0)

        run_absorption(conn, _Client(), None)

        after = get_entry(conn, entry.id)
        assert after.source_ids == [n1]
        assert after.source_count == 1
