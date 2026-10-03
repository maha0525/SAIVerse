"""隔離 SQLite で、付け替えの commit 結果が不明な回の帰属維持を固定する。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from sai_memory.arasuji.absorption import (
    AbsorptionError,
    AbsorptionItem,
    AbsorptionPlan,
    _repoint_batches,
    _repoint_fragments_back,
    run_absorption,
)
from sai_memory.arasuji.storage import create_entry, get_entry, init_arasuji_tables, regenerate_entry
from sai_memory.memory.storage import add_message, get_or_create_thread, init_db
from sai_memory.perception_buffer import (
    create_consumption_batch,
    init_perception_buffer_table,
    mark_batches_annexed,
    push_perception,
)


def _entry(conn, message_ids, content):
    return create_entry(
        conn, level=1, content=content, source_ids=message_ids,
        start_time=1, end_time=3, source_count=len(message_ids),
        message_count=len(message_ids),
    )


@pytest.fixture
def memory(tmp_path):
    path = tmp_path / "memory.db"
    conn = init_db(str(path))
    init_arasuji_tables(conn)
    init_perception_buffer_table(conn)
    get_or_create_thread(conn, "synthetic")
    messages = [
        add_message(conn, "synthetic", "user", f"合成メッセージ {i}", created_at=i + 1)
        for i in range(4)
    ]
    old = [_entry(conn, [mid], f"旧あらすじ {i}") for i, mid in enumerate(messages[1:3])]
    other = _entry(conn, [messages[3]], "並行操作の帰属先")
    fragments = []
    batches = []
    for i, entry in enumerate(old):
        ids = [f"fragment-{i}-{j}" for j in range(2)]
        fragments.append(ids)
        conn.executemany(
            "INSERT INTO memopedia_fragments "
            "(id, content, entity_id, chronicle_entry_id, created_at) "
            "VALUES (?, '合成知識', 'root_chronicle', ?, 1)",
            [(fid, entry.id) for fid in ids],
        )
        batch_ids = []
        for j in range(2):
            item_id = push_perception(conn, "world_state", f"合成知覚 {i}-{j}")
            batch_id = create_consumption_batch(
                conn, [item_id], consumed_at=i + 1, rendered_text=f"合成知覚 {i}-{j}",
            )
            mark_batches_annexed(conn, [batch_id], entry.id)
            batch_ids.append(batch_id)
        batches.append(batch_ids)
    conn.commit()
    try:
        yield SimpleNamespace(
            conn=conn, path=path, messages=messages, old=old, other=other,
            fragments=fragments, batches=batches, created=[],
        )
    finally:
        conn.close()


class _FailRepointCommit:
    """本物の UPDATE を通し、指定した付け替えの commit 前後で一度だけ失敗。"""

    def __init__(self, memory, table, *, after_commit, occurrence=1, after_rollback=None):
        self._real = memory.conn
        self.memory = memory
        self.table = table
        self.after_commit = after_commit
        self.occurrence = occurrence
        self.after_rollback = after_rollback
        self._armed = False
        self.fired = False
        self.rollbacks = 0
        self.repoint_count = 0

    def execute(self, sql, parameters=(), **kwargs):
        if (
            not self.fired and self.memory.created
            and f"UPDATE {self.table} SET" in sql
            and parameters[0] == self.memory.created[-1]
        ):
            self.repoint_count += 1
            self._armed = self.repoint_count == self.occurrence
        return self._real.execute(sql, parameters, **kwargs)

    def commit(self):
        if self._armed:
            self._armed = False
            self.fired = True
            if self.after_commit:
                self._real.commit()
                assert not self._real.in_transaction
            raise sqlite3.OperationalError("injected repoint commit failure")
        return self._real.commit()

    def rollback(self):
        self.rollbacks += 1
        self._real.rollback()
        if self.after_rollback is not None:
            callback, self.after_rollback = self.after_rollback, None
            callback()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(memory, proxy, operation, monkeypatch, *, after_generate=None):
    from sai_memory.arasuji import storage

    delete_entry = storage.delete_entry_and_update_parent
    withdrawals = []

    def withdraw(conn, entry_id, **kwargs):
        if entry_id in memory.created:
            # 削除の unmark に救われたように見えるテストにしない。
            # 撤去に入る前に、試行対象の帰属が新 id から離れていること。
            for table, column, groups in (
                ("memopedia_fragments", "chronicle_entry_id", memory.fragments),
                ("perception_batches", "annexed_entry_id", memory.batches),
            ):
                for ids in groups:
                    ph = ",".join("?" for _ in ids)
                    assert conn.execute(
                        f"SELECT id FROM {table} WHERE id IN ({ph}) AND {column} = ?",
                        (*ids, entry_id),
                    ).fetchall() == []
            withdrawals.append(entry_id)
        return delete_entry(conn, entry_id, **kwargs)

    monkeypatch.setattr(storage, "delete_entry_and_update_parent", withdraw)
    monkeypatch.setattr("sai_memory.arasuji.absorption.delete_entry_and_update_parent", withdraw)

    def generate(conn, messages, *args, **kwargs):
        entry = _entry(conn, [message.id for message in messages], "新あらすじ")
        memory.created.append(entry.id)
        if after_generate is not None:
            after_generate()
        return entry

    if operation == "absorption":
        monkeypatch.setattr(
            "sai_memory.arasuji.generator.generate_level1_arasuji",
            lambda client, conn, messages, **kwargs: generate(conn, messages),
        )
        plan = AbsorptionPlan(items=[AbsorptionItem(
            run_message_ids=[memory.messages[0]],
            absorbed_entry_ids=[entry.id for entry in memory.old],
            material_chars=100, start_at=1,
        )])
        with pytest.raises(AbsorptionError, match="bookkeeping repoint failed"):
            run_absorption(proxy, object(), plan)
    else:
        monkeypatch.setattr(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages", generate,
        )
        assert regenerate_entry(proxy, memory.old[0].id) is None
    assert withdrawals == memory.created


def _assert_recovered(memory, *, reassigned=False):
    assert len(memory.created) == 1
    assert get_entry(memory.conn, memory.created[0]) is None
    for index, entry in enumerate(memory.old):
        assert get_entry(memory.conn, entry.id) is not None
        for table, column, ids in (
            ("memopedia_fragments", "chronicle_entry_id", memory.fragments[index]),
            ("perception_batches", "annexed_entry_id", memory.batches[index]),
        ):
            for position, row_id in enumerate(ids):
                expected = memory.other.id if reassigned and index == position == 0 else entry.id
                row = memory.conn.execute(
                    f"SELECT {column} FROM {table} WHERE id = ?", (row_id,),
                ).fetchone()
                assert row[0] == expected
            assert memory.conn.execute(
                f"SELECT id FROM {table} WHERE {column} = ?", (memory.created[0],),
            ).fetchall() == []


@pytest.mark.parametrize("operation,occurrence", [("absorption", 1), ("absorption", 2), ("regenerate", 1)])
@pytest.mark.parametrize("table", ["memopedia_fragments", "perception_batches"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["not-committed", "committed-then-raised"])
def test_repoint_failure_restores_all_attempted_owners(memory, monkeypatch, operation, occurrence, table, after_commit):
    proxy = _FailRepointCommit(memory, table, after_commit=after_commit, occurrence=occurrence)
    _run(memory, proxy, operation, monkeypatch)
    assert proxy.fired
    assert proxy.rollbacks >= 1
    _assert_recovered(memory)


@pytest.mark.parametrize("operation", ["absorption", "regenerate"])
@pytest.mark.parametrize("table", ["memopedia_fragments", "perception_batches"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["not-committed", "committed-then-raised"])
def test_recovery_preserves_concurrent_reassignment(memory, monkeypatch, operation, table, after_commit):
    def reassign_from_another_connection():
        # 先行 rollback でロックが解けた隙に別 writer が帰属を変える。
        with closing(sqlite3.connect(memory.path)) as other, other:
            other.execute(
                "UPDATE memopedia_fragments SET chronicle_entry_id = ? WHERE id = ?",
                (memory.other.id, memory.fragments[0][0]),
            )
            other.execute(
                "UPDATE perception_batches SET annexed_entry_id = ? WHERE id = ?",
                (memory.other.id, memory.batches[0][0]),
            )

    proxy = _FailRepointCommit(
        memory, table, after_commit=after_commit, after_rollback=reassign_from_another_connection,
    )
    _run(memory, proxy, operation, monkeypatch)
    assert proxy.fired
    _assert_recovered(memory, reassigned=True)


def test_conditional_restore_is_idempotent_and_id_scoped(memory):
    replacement = _entry(memory.conn, [], "撤去予定")
    for entry, fragments, batches in zip(memory.old, memory.fragments, memory.batches):
        memory.conn.execute(
            "UPDATE memopedia_fragments SET chronicle_entry_id = ? WHERE chronicle_entry_id = ?",
            (replacement.id, entry.id),
        )
        _repoint_batches(memory.conn, batches, entry.id, replacement.id)

    for _ in range(2):
        _repoint_fragments_back(memory.conn, memory.fragments[0], memory.old[0].id, replacement.id)
        _repoint_batches(memory.conn, memory.batches[0], replacement.id, memory.old[0].id)

    for table, column, groups in (
        ("memopedia_fragments", "chronicle_entry_id", memory.fragments),
        ("perception_batches", "annexed_entry_id", memory.batches),
    ):
        for index, ids in enumerate(groups):
            for row_id in ids:
                row = memory.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()
                assert row[0] == (memory.old[0].id if index == 0 else replacement.id)


def test_regeneration_restore_does_not_claim_an_unattempted_batch(memory, monkeypatch):
    unrelated = memory.batches[1][0]

    def assign_unrelated_batch_to_replacement():
        with closing(sqlite3.connect(memory.path)) as other, other:
            other.execute(
                "UPDATE perception_batches SET annexed_entry_id = ? WHERE id = ?",
                (memory.created[0], unrelated),
            )

    # 対象外バッチは撤去時の通常 unmark で NULL に戻り、旧 entry には移さない。
    memory.batches[1] = memory.batches[1][1:]
    proxy = _FailRepointCommit(
        memory, "perception_batches", after_commit=True,
        after_rollback=assign_unrelated_batch_to_replacement,
    )
    _run(memory, proxy, "regenerate", monkeypatch)
    _assert_recovered(memory)
    assert memory.conn.execute(
        "SELECT annexed_entry_id FROM perception_batches WHERE id = ?", (unrelated,),
    ).fetchone()[0] is None


@pytest.mark.parametrize("empty", [False, True])
def test_batch_reassign_can_be_limited_to_attempted_ids(memory, empty):
    from sai_memory.perception_buffer import reassign_batches_annexed

    replacement = _entry(memory.conn, [], "新あらすじ")
    ids = [] if empty else [memory.batches[0][0]]
    moved = reassign_batches_annexed(
        memory.conn, memory.old[0].id, replacement.id, batch_ids=ids,
    )
    assert moved == len(ids)
    memory.conn.commit()
    for batch_id in memory.batches[0]:
        row = memory.conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?", (batch_id,),
        ).fetchone()
        assert row[0] == (replacement.id if batch_id in ids else memory.old[0].id)


def test_regeneration_still_aborts_when_material_batches_change(memory, monkeypatch):
    def add_material_during_generation():
        item_id = push_perception(memory.conn, "world_state", "生成中に増えた合成知覚")
        batch_id = create_consumption_batch(
            memory.conn, [item_id], consumed_at=10, rendered_text="生成中に増えた合成知覚",
        )
        mark_batches_annexed(memory.conn, [batch_id], memory.old[0].id)
        memory.conn.commit()
        memory.batches[0].append(batch_id)

    _run(memory, memory.conn, "regenerate", monkeypatch, after_generate=add_material_during_generation)
    _assert_recovered(memory)


class _InsertFragmentAfterSelection:
    """A separate writer commits after the real SELECT has released its cursor."""

    def __init__(self, memory, occurrence):
        self.memory = memory
        self._real = memory.conn
        self.occurrence = occurrence
        self.selections = 0
        self.inserted_owner = None

    def execute(self, sql, parameters=(), **kwargs):
        cursor = self._real.execute(sql, parameters, **kwargs)
        if self.memory.created and sql == "SELECT id FROM memopedia_fragments WHERE chronicle_entry_id = ?":
            rows = cursor.fetchall()
            self.selections += 1
            if self.selections == self.occurrence:
                with closing(sqlite3.connect(self.memory.path)) as other, other:
                    other.execute(
                        "INSERT INTO memopedia_fragments (id, content, entity_id, chronicle_entry_id, created_at) "
                        "VALUES ('concurrent-fragment', '合成知識', 'root_chronicle', ?, 1)",
                        parameters,
                    )
                self.inserted_owner = parameters[0]
            return SimpleNamespace(fetchall=lambda: rows)
        return cursor

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("operation,occurrence", [("absorption", 1), ("absorption", 2), ("regenerate", 1)])
def test_fragment_added_after_selection_aborts_swap(memory, monkeypatch, operation, occurrence):
    proxy = _InsertFragmentAfterSelection(memory, occurrence)
    _run(memory, proxy, operation, monkeypatch)
    assert proxy.inserted_owner is not None
    _assert_recovered(memory)
    owner = memory.conn.execute(
        "SELECT chronicle_entry_id FROM memopedia_fragments WHERE id = 'concurrent-fragment'",
    ).fetchone()[0]
    assert owner == proxy.inserted_owner
    assert get_entry(memory.conn, owner) is not None
