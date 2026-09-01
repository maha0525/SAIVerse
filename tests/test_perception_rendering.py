"""知覚レンダリング (W14, perception_buffer.md §10) の回帰テスト。

対象:
- 提示の時刻順マージ (sea/runtime_context._merge_consumed_perceptions):
  未付記バッチの確定文面 (rendered_text) をそのまま出す (再 reduce / 再 format
  しない)・提示から下ろす唯一の手段は付記印・legacy event_message 行との共存・
  Chronicle 無効ペルソナだけの窓例外。
- 知覚バッチの材料化 (sai_memory/arasuji/executor, 2026-08-29 まはー裁定):
  バッチは編纂 LLM の【知覚】材料として時刻順にプロンプトへ入り、digest 本文
  への機械的な付記 (annex ブロック連結) はしない。付記印 (annexed_entry_id =
  材料として消費済み) は digest 確定 tx と同乗し、群内限定の敷き詰めと
  一括回収は従来どおり。
- 機構名義の行の長さ規則 (generator.MECHANISM_TEXT_MAX_CHARS): 閾値以下は
  全文が材料に、超えたら決定論の一行に縮む。チャンクの字数勘定 (alignment)
  も圧縮後サイズで数える。
- 直挿しの移送 (B4/B7): day_plan の移動失敗通知と upgrade_handlers の
  アップデート通知が push_perception 経由になったこと。
"""
from __future__ import annotations

import sqlite3
import threading
import unittest
from types import SimpleNamespace

from sai_memory.perception_buffer import (
    create_consumption_batch,
    init_perception_buffer_table,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
)
from sea.runtime_context import _merge_consumed_perceptions


def _fake_sai_memory():
    conn = sqlite3.connect(":memory:")
    init_perception_buffer_table(conn)
    return SimpleNamespace(
        conn=conn,
        _db_lock=threading.RLock(),
        is_ready=lambda: True,
    )


def _persona(sai_mem):
    return SimpleNamespace(persona_id="p1", sai_memory=sai_mem)


#: Chronicle 有効相当 (lifecycle 無し = 判定不能 → バッチを隠さない側)。
_RUNTIME = SimpleNamespace(session_lifecycle=None)


def _row(content, created_at, role="user", metadata=None, **extra):
    msg = {"role": role, "content": content, "created_at": created_at}
    if metadata is not None:
        msg["metadata"] = metadata
    msg.update(extra)
    return msg


def _batch(conn, text, at, pulse_id=None, media=None, boundary=None):
    """項目 1 件を積んで消費バッチを確定する (rendered_text = text)。

    ``boundary`` = (created_at, rowid) の境界キー (省略時 NULL)。
    """
    item_id = push_perception(conn, "world_state", text)
    b_created, b_rowid = boundary if boundary else (None, None)
    return create_consumption_batch(
        conn, [item_id], consumed_at=at, rendered_text=text,
        pulse_id=pulse_id, media=media,
        boundary_created_at=b_created, boundary_rowid=b_rowid,
    )


class MergeConsumedPerceptionsTest(unittest.TestCase):
    def setUp(self):
        self.sai = _fake_sai_memory()
        self.persona = _persona(self.sai)
        self.conn = self.sai.conn
        self.addCleanup(self.conn.close)

    def _merge(self, recent, runtime=_RUNTIME):
        return _merge_consumed_perceptions(runtime, self.persona, recent)

    def test_block_inserted_at_consumed_position(self):
        _batch(self.conn, "[システム通知]\n誰かが入室した", at=1500, pulse_id="pA")
        recent = [
            _row("古い発言", 1000),
            _row("新しい発言", 2000),
        ]
        merged = self._merge(recent)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["content"], "古い発言")
        # 消費時刻 1500 のブロックは 1000 と 2000 の間に入る。
        self.assertIn("誰かが入室した", merged[1]["content"])
        self.assertTrue(merged[1]["content"].startswith("<system>"))
        self.assertEqual(merged[1]["role"], "user")
        self.assertEqual(merged[2]["content"], "新しい発言")
        # 旧 flush の行と同型のタグ + マージ由来の目印。
        meta = merged[1]["metadata"]
        self.assertIn("event_message", meta["tags"])
        self.assertTrue(meta["__consumed_perception__"])

    def test_same_second_goes_after_raw_row(self):
        # 同時刻なら生ログが先 (消費は書き込みの後に起きた事実)。
        _batch(self.conn, "同時刻の知覚", at=2000)
        recent = [_row("古い発言", 1000), _row("同時刻の発言", 2000)]
        merged = self._merge(recent)
        self.assertEqual(
            [m["content"] for m in merged][:2], ["古い発言", "同時刻の発言"],
        )
        self.assertIn("同時刻の知覚", merged[2]["content"])

    def test_same_second_batches_stay_separate_blocks(self):
        # 同秒・別 flush の 2 バッチは別ブロックのまま提示される — 秒精度の
        # 時刻からグループを再構成しない (2026-08-18 Codex #2)。
        _batch(self.conn, "知覚1", at=1500, pulse_id="pA")
        _batch(self.conn, "知覚2", at=1500, pulse_id="pB")
        merged = self._merge([_row("開始", 1000), _row("終了", 2000)])
        blocks = [
            m for m in merged
            if (m.get("metadata") or {}).get("__consumed_perception__")
        ]
        self.assertEqual(len(blocks), 2)
        self.assertIn("知覚1", blocks[0]["content"])
        self.assertNotIn("知覚2", blocks[0]["content"])
        self.assertIn("知覚2", blocks[1]["content"])

    def test_rendered_text_is_presented_verbatim(self):
        # 提示は消費時の確定文面をそのまま出す — 後の消費と混ぜて再 reduce
        # しない (reduce の中間状態の復活防止, Codex #3)。同じ reduce_key を
        # 別々の Beat で消費した場合、両方のバッチが独立に見える。
        i1 = push_perception(
            self.conn, "core_memory_correction", "旧", reduce_key="core:1",
        )
        create_consumption_batch(
            self.conn, [i1], consumed_at=1200, rendered_text="[更新]\n旧",
        )
        i2 = push_perception(
            self.conn, "core_memory_correction", "新", reduce_key="core:1",
        )
        create_consumption_batch(
            self.conn, [i2], consumed_at=1400, rendered_text="[更新]\n新",
        )
        merged = self._merge([_row("開始", 1000), _row("終了", 2000)])
        blocks = [
            m for m in merged
            if (m.get("metadata") or {}).get("__consumed_perception__")
        ]
        self.assertEqual(len(blocks), 2)
        self.assertIn("旧", blocks[0]["content"])
        self.assertIn("新", blocks[1]["content"])

    def test_batch_older_than_window_is_still_presented(self):
        # 提示から下ろす唯一の手段は付記印 — 窓開始の計算では下ろさない
        # (Codex #1)。最古行より古いバッチは先頭に出る。
        _batch(self.conn, "窓より古い知覚", at=500)
        recent = [_row("窓の最古", 1000), _row("終了", 2000)]
        merged = self._merge(recent)
        self.assertEqual(len(merged), 3)
        self.assertIn("窓より古い知覚", merged[0]["content"])
        self.assertEqual(merged[1]["content"], "窓の最古")

    def test_chronicle_disabled_persona_forgets_old_batches(self):
        # 「編纂なしで忘れる」を選んだペルソナだけは、提示最古行より古い
        # バッチを会話と同じように忘れる (§10.3 の唯一の例外)。
        import os
        from unittest.mock import patch
        _batch(self.conn, "窓より古い知覚", at=500)
        _batch(self.conn, "窓の中の知覚", at=1500)
        disabled_runtime = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda p: False,
            ),
        )
        merged = self._merge(
            [_row("窓の最古", 1000), _row("終了", 2000)],
            runtime=disabled_runtime,
        )
        joined = str(merged)
        self.assertNotIn("窓より古い知覚", joined)
        self.assertIn("窓の中の知覚", joined)

    def test_annexed_batch_is_hidden(self):
        # 付記印が付いたバッチは提示から下りる — digest 側 (fold の置き換え)
        # が同じ内容を持つため (§10.4)。
        b1 = _batch(self.conn, "畳まれた知覚", at=1300)
        _batch(self.conn, "まだの知覚", at=1800)
        mark_batches_annexed(self.conn, [b1], "entry-1")
        self.conn.commit()
        merged = self._merge([_row("開始", 1000), _row("終了", 2000)])
        joined = str(merged)
        self.assertNotIn("畳まれた知覚", joined)
        self.assertIn("まだの知覚", joined)

    def test_consumed_after_last_row_appends_at_tail(self):
        _batch(self.conn, "最後の知覚", at=3000)
        merged = self._merge([_row("開始", 1000), _row("終了", 2000)])
        self.assertIn("最後の知覚", merged[-1]["content"])

    def test_legacy_event_rows_coexist(self):
        # legacy の event_message 行 (直挿し時代) は生ログ側にそのまま居て、
        # 新経路のブロックと並ぶ (混在期間の想定挙動)。
        _batch(self.conn, "新経路の知覚", at=1500)
        legacy = _row(
            "<system>[システム通知] legacy の通知</system>", 1200,
            metadata={"tags": ["internal", "event_message"]},
        )
        merged = self._merge([_row("開始", 1000), legacy, _row("終了", 2000)])
        contents = [m["content"] for m in merged]
        self.assertIn("<system>[システム通知] legacy の通知</system>", contents)
        self.assertTrue(any("新経路の知覚" in c for c in contents))
        # legacy (1200) → 新ブロック (1500) の時刻順。
        self.assertLess(
            contents.index("<system>[システム通知] legacy の通知</system>"),
            next(i for i, c in enumerate(contents) if "新経路の知覚" in c),
        )

    def test_media_carried_from_batch(self):
        media = [{"path": "/img/a.png", "mime_type": "image/png", "role": "image"}]
        _batch(self.conn, "様子", at=1500, media=media)
        merged = self._merge([_row("開始", 1000), _row("終了", 2000)])
        block = merged[1]
        self.assertEqual(block["metadata"]["media"], media)

    def test_empty_history_still_presents_batches(self):
        # 履歴が空でも未付記バッチは提示される (付記されるまで消えない)。
        _batch(self.conn, "履歴なしでも見える知覚", at=1500)
        merged = self._merge([])
        self.assertEqual(len(merged), 1)
        self.assertIn("履歴なしでも見える知覚", merged[0]["content"])

    def test_failure_falls_back_to_raw_history(self):
        # 読み出し失敗はマージなしに倒す (履歴ゼロより知覚欠けの方が軽い)。
        self.sai.conn.close()
        recent = [_row("開始", 1000)]
        merged = self._merge(recent)
        self.assertEqual(merged, recent)

    def test_chronicle_disabled_empty_history_uses_anchor_cutoff(self):
        # Chronicle 無効 + 生ログ窓が空でも、anchor 境界で絞る — 過去バッチの
        # 無制限な再提示をしない (2026-08-19 Codex 第二巡 #3)。
        import os
        from unittest.mock import patch
        # _anchor_epoch は storage.get_message (7 基本列の SELECT) で引く。
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, "
            "role TEXT, content TEXT, resource_id TEXT, created_at INTEGER, "
            "metadata TEXT)"
        )
        anchor_id = "anchor-1"
        self.conn.execute(
            "INSERT INTO messages VALUES (?, 't1', 'user', 'anchor', 'p1', 1000, NULL)",
            (anchor_id,),
        )
        self.conn.commit()
        _batch(self.conn, "anchor より古いバッチ", at=500)
        _batch(self.conn, "anchor より新しいバッチ", at=1500)
        disabled_runtime = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda p: False,
            ),
        )
        merged = _merge_consumed_perceptions(
            disabled_runtime, self.persona, [], anchor_id=anchor_id,
        )
        joined = str(merged)
        self.assertNotIn("anchor より古いバッチ", joined)
        self.assertIn("anchor より新しいバッチ", joined)

    def test_chronicle_disabled_same_second_uses_canonical_order(self):
        # anchor と同秒に確定したバッチは、境界キー (created_at, rowid) で
        # 正典順どおりに判定される — epoch 比較では区別できない直前/直後
        # (2026-08-19 Codex 第三巡 #4)。
        import os
        from unittest.mock import patch
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, "
            "role TEXT, content TEXT, resource_id TEXT, created_at INTEGER, "
            "metadata TEXT)"
        )
        # 同秒の 2 行 (rowid 1, 2)。anchor は 2 行目。
        self.conn.execute(
            "INSERT INTO messages VALUES ('m1', 't1', 'user', 'a', 'p1', 1000, NULL)"
        )
        self.conn.execute(
            "INSERT INTO messages VALUES ('m2', 't1', 'user', 'b', 'p1', 1000, NULL)"
        )
        self.conn.commit()
        # anchor 行が保存される前に確定したバッチ (境界 = rowid 1)。
        _batch(self.conn, "anchor 直前のバッチ", at=1000, boundary=(1000, 1))
        # anchor 行の保存後に確定したバッチ (境界 = rowid 2)。
        _batch(self.conn, "anchor 直後のバッチ", at=1000, boundary=(1000, 2))
        # 境界キーの無い旧バッチ (同秒) は epoch フォールバックで残る。
        _batch(self.conn, "境界なしの旧バッチ", at=1000)
        disabled_runtime = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda p: False,
            ),
        )
        merged = _merge_consumed_perceptions(
            disabled_runtime, self.persona, [], anchor_id="m2",
        )
        joined = str(merged)
        self.assertNotIn("anchor 直前のバッチ", joined)
        self.assertIn("anchor 直後のバッチ", joined)
        self.assertIn("境界なしの旧バッチ", joined)

    def test_chronicle_disabled_full_bootstrap_shows_all(self):
        # anchor も履歴も無い完全ブートストラップだけは全提示 (隠さない側)。
        import os
        from unittest.mock import patch
        _batch(self.conn, "ブートストラップ期のバッチ", at=500)
        disabled_runtime = SimpleNamespace(
            session_lifecycle=SimpleNamespace(
                is_chronicle_enabled_for_persona=lambda p: False,
            ),
        )
        merged = _merge_consumed_perceptions(
            disabled_runtime, self.persona, [], anchor_id=None,
        )
        self.assertIn("ブートストラップ期のバッチ", str(merged))


class PerceptionAnnexTest(unittest.TestCase):
    """知覚バッチの材料化 (executor) と長さ規則 (2026-08-29 まはー裁定)。

    バッチは digest 本文へ転写せず、編纂 LLM の材料としてプロンプトに入る。
    付記印 (annexed_entry_id = 材料として消費済み) は digest 確定 tx と同乗。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)

    def test_collect_includes_batches_in_order(self):
        from sai_memory.arasuji.executor import collect_annex_items
        b1 = _batch(self.conn, "新着記事X", at=1200)
        b2 = _batch(self.conn, "後の記事Y", at=1300)
        # 範囲外 (hi 以降) は入らない。
        _batch(self.conn, "範囲外の記事", at=2000)
        items, batch_ids = collect_annex_items(self.conn, 1000, 1500)
        self.assertEqual([d["at"] for d in items], [1200, 1300])
        self.assertEqual([d["text"] for d in items], ["新着記事X", "後の記事Y"])
        # バッチ id は付記印 (材料として消費済み) 用に返る。
        self.assertEqual(batch_ids, [b1, b2])

    def test_collect_skips_annexed_batches(self):
        from sai_memory.arasuji.executor import collect_annex_items
        b1 = _batch(self.conn, "付記済み", at=1200)
        mark_batches_annexed(self.conn, [b1], "entry-x")
        self.conn.commit()
        items, batch_ids = collect_annex_items(self.conn, 1000, 1500)
        self.assertEqual(items, [])
        self.assertEqual(batch_ids, [])

    def test_collect_recovers_leftovers_before_compiled_end(self):
        # 一括回収: 既存の編纂被覆の末尾以前に consumed_at を持つ未付記バッチは
        # スパン外でも引き取る (チャンク skip・印付け失敗の回収路)。
        from sai_memory.arasuji.executor import collect_annex_items
        b_old = _batch(self.conn, "取り残された知覚", at=800)
        b_in = _batch(self.conn, "スパン内の知覚", at=1200)
        items, batch_ids = collect_annex_items(
            self.conn, 1000, 1500, recover_before=900,
        )
        self.assertEqual([d["at"] for d in items], [800, 1200])
        self.assertEqual(batch_ids, [b_old, b_in])

    # ---- 長さ規則 (generator.MECHANISM_TEXT_MAX_CHARS) ----

    def test_short_mechanism_row_passes_verbatim(self):
        # 回帰 (a): 閾値以下のスペル結果は全文がそのまま材料に入る。
        from sai_memory.arasuji.generator import (
            MECHANISM_TEXT_MAX_CHARS,
            material_text,
        )
        from sai_memory.memory.storage import Message
        body = "[Spell Result: web_search]\n短い検索結果"
        self.assertLessEqual(len(body), MECHANISM_TEXT_MAX_CHARS)
        msg = Message(
            id="s1", thread_id="t1", role="system", content=body,
            resource_id="p1", created_at=1000,
            metadata={"tags": ["conversation", "spell"]},
        )
        self.assertEqual(material_text(msg), body)

    def test_long_mechanism_row_condenses_to_one_line(self):
        # 回帰 (b): 閾値超えのスペル結果は決定論の一行 (名前 + 字数) に縮む。
        # DB の行は触らない — 縮むのは材料を組む時だけ。
        from sai_memory.arasuji.generator import (
            MECHANISM_TEXT_MAX_CHARS,
            material_text,
        )
        from sai_memory.memory.storage import Message
        body = "[Spell Result: web_search]\n" + "x" * (MECHANISM_TEXT_MAX_CHARS * 4)
        msg = Message(
            id="s1", thread_id="t1", role="system", content=body,
            resource_id="p1", created_at=1000,
            metadata={"tags": ["conversation", "spell"]},
        )
        line = material_text(msg)
        self.assertIn("[Spell Result: web_search] を受け取った", line)
        self.assertIn("字)", line)
        self.assertLess(len(line), 120)
        # 名前プレフィックスの無い長文は先頭行の冒頭 + 字数。
        msg2 = Message(
            id="s2", thread_id="t1", role="user",
            content="通知の長い本文 " * 200, resource_id="p1", created_at=1001,
            metadata={"tags": ["internal", "event_message"]},
        )
        line2 = material_text(msg2)
        self.assertIn("通知の長い本文", line2)
        self.assertIn("字)", line2)
        self.assertLess(len(line2), 120)

    def test_non_mechanism_long_row_is_not_condensed(self):
        # 本人の長い発話は縮めない — 長さ規則は機構名義の行 (タグ判定) だけ。
        from sai_memory.arasuji.generator import (
            MECHANISM_TEXT_MAX_CHARS,
            material_text,
        )
        from sai_memory.memory.storage import Message
        body = "長い独白 " * (MECHANISM_TEXT_MAX_CHARS // 2)
        msg = Message(
            id="u1", thread_id="t1", role="assistant", content=body,
            resource_id="p1", created_at=1000,
            metadata={"tags": ["conversation"]},
        )
        self.assertEqual(material_text(msg), body)

    def test_alignment_counts_condensed_size(self):
        # 回帰 (b): チャンクの字数勘定 (U 計算) も圧縮後サイズで数える。
        # 圧縮前サイズで数えると 10,000 字のスペル結果 2 行はそれぞれ単独で
        # チャンクを閉じて 2 チャンクになるが、材料の実体は一行ずつなので
        # 1 チャンクに束ねるのが正しい。
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.memory.storage import Message
        big = "[Spell Result: big]\n" + "x" * 10_000
        msgs = [
            Message(
                id=f"s{i}", thread_id="t1", role="system", content=big,
                resource_id="p1", created_at=1000 + i * 100,
                metadata={"tags": ["conversation", "spell"]},
            )
            for i in range(2)
        ] + [
            Message(
                id="u1", thread_id="t1", role="user", content="続きの発言",
                resource_id="p1", created_at=1300,
            ),
        ]
        plan = plan_alignment(msgs, set(), target_chars=10_000)
        self.assertEqual(len(plan.chunks), 1)
        # 記録される被覆も材料字数 (圧縮後) — 勘定と材料の実体を一致させる。
        self.assertLess(plan.chunks[0].coverage_chars, 300)

    def _chunk(self, times, group_key=None):
        return SimpleNamespace(
            messages=[SimpleNamespace(created_at=t) for t in times],
            group_key=group_key,
        )

    def test_annex_spans_tile_within_group(self):
        from sai_memory.arasuji.executor import _annex_time_spans
        chunks = [
            self._chunk([1000, 1100], group_key=0),
            self._chunk([1200, 1400], group_key=0),
        ]
        spans = _annex_time_spans(chunks)
        # 同一群: チャンク 1 は次のチャンクの開始まで — fold 内の切れ目
        # (pulse 関節で消費された知覚が落ちる場所) を必ず引き受ける。
        self.assertEqual(spans[0], (1000, 1200))
        # 群の最後のチャンクは自分の末尾まで。
        self.assertEqual(spans[1], (1200, 1401))

    def test_annex_spans_do_not_tile_across_groups(self):
        # 群と群の間には生きた提示中の範囲が挟まりうる — 跨いで敷き詰めると
        # 提示中の期間のバッチを先取りで畳んでしまう (Codex #5)。
        from sai_memory.arasuji.executor import _annex_time_spans
        chunks = [
            self._chunk([1000, 1100], group_key=0),
            self._chunk([5000, 5200], group_key=1),
        ]
        spans = _annex_time_spans(chunks)
        # 群 0 の末尾は自分の末尾まで (5000 まで伸びない)。
        self.assertEqual(spans[0], (1000, 1101))
        self.assertEqual(spans[1], (5000, 5201))

    def _make_plan_messages(self, times, prefix="m", extra=None):
        from sai_memory.memory.storage import Message
        msgs = [
            Message(
                id=f"{prefix}{i}", thread_id="t1", role="user",
                content=f"発言{i}", resource_id="p1", created_at=t,
            )
            for i, t in enumerate(times)
        ]
        msgs.extend(extra or [])
        msgs.sort(key=lambda m: m.created_at)
        return msgs

    def _capturing_client(self, response="LLM要約本文"):
        """プロンプトを記録するフェイク LLM client。"""
        prompts = []

        def _generate(**kw):
            prompts.append(kw["messages"][0]["content"])
            return response

        return SimpleNamespace(generate=_generate), prompts

    def test_execute_plan_feeds_batch_as_material_and_stamps_in_tx(self):
        # 回帰 (e): 知覚バッチは【知覚】材料として LLM プロンプトへ入り、印は
        # digest 確定と同一 tx で付く。回帰 (c): digest 本文は LLM 出力のみ —
        # 旧設計の機械的な付記ブロックは付かない。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan

        init_arasuji_tables(self.conn)
        # fold 期間 (メッセージ 1000..1400) の途中で消費されたバッチ。
        b1 = _batch(self.conn, "[フィード]\n編纂対象期間の知覚", at=1150)
        msgs = self._make_plan_messages([1000, 1100, 1200, 1300, 1400])
        plan = plan_alignment(msgs, set(), target_chars=10)
        client, prompts = self._capturing_client()
        result = execute_plan(plan, client, self.conn, persona_id="p1")
        self.assertGreaterEqual(result.created_count, 1)
        joined_content = "\n".join(e.content for e in result.created)
        self.assertIn("LLM要約本文", joined_content)
        # (c) digest 本文は LLM 出力そのもの — 付記ブロックも知覚の生文も無い。
        self.assertNotIn("この期間に届いた通知・知覚の記録", joined_content)
        self.assertNotIn("編纂対象期間の知覚", joined_content)
        # バッチ本文は材料として LLM プロンプトに【知覚】ラベル付きで入る。
        joined_prompts = "\n".join(prompts)
        self.assertIn("編纂対象期間の知覚", joined_prompts)
        self.assertIn("【知覚】", joined_prompts)
        # 付記印 (材料として消費済み) が digest 確定と同じ tx で入り、
        # 提示対象から下りている。
        self.assertEqual(list_unannexed_batches(self.conn), [])
        row = self.conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
            (b1,),
        ).fetchone()
        self.assertIn(row[0], [e.id for e in result.created])

    def test_execute_plan_event_message_row_is_material_with_label(self):
        # 回帰 (d): event_message 行は編纂対象になり source_ids (被覆) に入る。
        # 短い通知は全文が【通知】ラベル付きで材料に入る (回帰 a の同族)。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan
        from sai_memory.memory.storage import Message

        init_arasuji_tables(self.conn)
        event_row = Message(
            id="ev1", thread_id="t1", role="user",
            content="<system>[システム通知] 誰かが入室した</system>",
            resource_id="p1", created_at=1050,
            metadata={"tags": ["internal", "event_message"]},
        )
        msgs = self._make_plan_messages([1000, 1100], extra=[event_row])
        plan = plan_alignment(msgs, set(), target_chars=10)
        client, prompts = self._capturing_client()
        result = execute_plan(plan, client, self.conn, persona_id="p1")
        self.assertEqual(result.created_count, 1)
        # 被覆 (source_ids) に event_message 行が入る。
        self.assertIn("ev1", result.created[0].source_ids)
        # 材料には全文 + 【通知】ラベル。
        joined_prompts = "\n".join(prompts)
        self.assertIn("誰かが入室した", joined_prompts)
        self.assertIn("【通知】", joined_prompts)

    def test_execute_plan_long_spell_row_condensed_in_prompt(self):
        # 回帰 (a)(b) の経路統合: 閾値以下のスペル結果は全文が
        # 【スペル結果】ラベルで、閾値超えは決定論の一行だけが材料に入る。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan
        from sai_memory.arasuji.generator import MECHANISM_TEXT_MAX_CHARS
        from sai_memory.memory.storage import Message

        init_arasuji_tables(self.conn)
        short_spell = Message(
            id="sp1", thread_id="t1", role="system",
            content="[Spell Result: dice]\n出目は 6",
            resource_id="p1", created_at=1050,
            metadata={"tags": ["conversation", "spell"]},
        )
        long_body = "x" * (MECHANISM_TEXT_MAX_CHARS * 4)
        long_spell = Message(
            id="sp2", thread_id="t1", role="system",
            content=f"[Spell Result: web_search]\n{long_body}",
            resource_id="p1", created_at=1150,
            metadata={"tags": ["conversation", "spell"]},
        )
        msgs = self._make_plan_messages(
            [1000, 1100, 1200], extra=[short_spell, long_spell],
        )
        # target は材料合計より大きく取る — 全行が 1 チャンクに入る形で
        # 「長文スペルが材料では一行に縮む」ことだけを見る。
        plan = plan_alignment(msgs, set(), target_chars=10_000)
        client, prompts = self._capturing_client()
        result = execute_plan(plan, client, self.conn, persona_id="p1")
        self.assertEqual(result.created_count, 1)
        joined_prompts = "\n".join(prompts)
        # (a) 閾値以下: 全文がそのまま。
        self.assertIn("出目は 6", joined_prompts)
        self.assertIn("【スペル結果】", joined_prompts)
        # (b) 閾値超え: 全文は入らず、決定論の一行に縮む。
        self.assertNotIn(long_body, joined_prompts)
        self.assertIn("[Spell Result: web_search] を受け取った", joined_prompts)
        # 被覆にはどちらも全文の行として入る (DB の行は縮めない)。
        self.assertIn("sp1", result.created[0].source_ids)
        self.assertIn("sp2", result.created[0].source_ids)

    def test_execute_plan_recall_batch_labeled_and_noted(self):
        # 入室想起の再提示 (本文が [想起: で始まるバッチ) は
        # 【想起 (過去の再提示)】とラベルされ、プロンプトに注記が付く。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan

        init_arasuji_tables(self.conn)
        _batch(
            self.conn,
            "[想起: 誰かとの過去の会話]\n- [user] @ 2026-01-01 00:00: 昔の話",
            at=1150,
        )
        msgs = self._make_plan_messages([1000, 1400])
        plan = plan_alignment(msgs, set(), target_chars=10)
        client, prompts = self._capturing_client()
        execute_plan(plan, client, self.conn, persona_id="p1")
        joined_prompts = "\n".join(prompts)
        self.assertIn("【想起 (過去の再提示)】", joined_prompts)
        self.assertIn("過去の出来事の再提示", joined_prompts)

    def test_execute_plan_collection_failure_leaves_batch_for_recovery(self):
        # 収集の失敗は digest を止めず、バッチは未付記のまま提示に残る
        # (fail-open)。次の編纂の一括回収が材料として引き取る。
        from unittest.mock import patch
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan

        init_arasuji_tables(self.conn)
        b1 = _batch(self.conn, "落ちた回の知覚", at=1150)
        msgs = self._make_plan_messages([1000, 1400])
        plan = plan_alignment(msgs, set(), target_chars=10)
        client, prompts = self._capturing_client(response="本文のみ")
        with patch(
            "sai_memory.arasuji.executor.collect_annex_items",
            side_effect=RuntimeError("annex down"),
        ):
            result = execute_plan(plan, client, self.conn, persona_id="p1")
        # digest は確定するが、バッチは未付記のまま (= 提示に残る)。
        self.assertEqual(result.created_count, 1)
        self.assertIn("本文のみ", result.created[0].content)
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )
        self.assertNotIn("落ちた回の知覚", "\n".join(prompts))

        # 次の編纂 (後続期間のチャンク): 先頭チャンクの一括回収が、既存の
        # 編纂被覆 (end_time=1400) 以前の未付記バッチを材料として引き取る。
        msgs2 = self._make_plan_messages([2000, 2400], prefix="n")
        plan2 = plan_alignment(msgs2, set(), target_chars=10)
        client2, prompts2 = self._capturing_client(response="次の本文")
        result2 = execute_plan(plan2, client2, self.conn, persona_id="p1")
        self.assertEqual(result2.created_count, 1)
        self.assertIn("落ちた回の知覚", "\n".join(prompts2))
        self.assertNotIn("落ちた回の知覚", result2.created[0].content)
        self.assertEqual(list_unannexed_batches(self.conn), [])


class PerceptionRecallReadPortTest(unittest.TestCase):
    """§10.5 の読み口: unified_recall のキーワード検索で消費バッチ全文へ到達できる。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.embedder = SimpleNamespace(
            embed=lambda texts, **kw: [[0.0, 0.0, 0.0] for _ in texts],
        )

    def _recall(self, query):
        from sai_memory.unified_recall import unified_recall
        return unified_recall(
            self.conn, self.embedder, query,
            search_chronicle=False, search_memopedia=False,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )

    def test_consumed_batch_hits_by_keyword(self):
        b1 = _batch(
            self.conn,
            "[フィード]\n記事「泳ぐ庭園」が公開された。水面に浮かぶ植栽の話。",
            at=1200,
        )
        hits = self._recall("泳ぐ庭園")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.source_type, "perception")
        self.assertEqual(hit.source_id, f"perception:{b1}")
        self.assertIn("泳ぐ庭園", hit.content)
        self.assertIn("知覚 @", hit.title)

    def test_annexed_batch_still_searchable(self):
        # 付記済み (提示から退場済み) でも検索で全文へ到達できる — 集約の
        # 上限は到達手段とセット、の片割れ。
        b1 = _batch(self.conn, "退場済みの知覚: 銀の犬の噂", at=1200)
        mark_batches_annexed(self.conn, [b1], "entry-x")
        self.conn.commit()
        hits = self._recall("銀の犬")
        self.assertEqual(len(hits), 1)
        self.assertIn("銀の犬", hits[0].content)

    def test_pending_items_are_not_searchable(self):
        # 未消費 (まだ知覚していない) はバッチが無いので出ない。
        push_perception(self.conn, "feed", "未消費の記事「凍る雷鳴」")
        self.assertEqual(self._recall("凍る雷鳴"), [])

    def test_perception_source_is_opt_in(self):
        # 既定 False (opt-in) — フラグ未指定の既存利用者 (ペルソナツール /
        # auto_recall) に perception ヒットが混入しない (Codex 第四巡 #2)。
        from sai_memory.unified_recall import unified_recall
        _batch(self.conn, "opt-in 検証の知覚", at=1200)
        hits = unified_recall(
            self.conn, self.embedder, "opt-in 検証",
            search_chronicle=False, search_memopedia=False,
            search_fragments=False, search_messages=False,
        )
        self.assertEqual(hits, [])

    def test_annexed_batch_merges_into_chronicle_hit(self):
        # 転写先 Chronicle が同じ検索でヒットしたら Chronicle 側へ寄せる
        # (同じ知覚の二重表示をしない)。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.unified_recall import unified_recall
        init_arasuji_tables(self.conn)
        entry = create_entry(
            self.conn, level=1,
            content="あらすじ本文。紫の灯台の噂を聞いた。",
            source_ids=["m1"], start_time=1000, end_time=1400,
            source_count=1, message_count=1,
        )
        b1 = _batch(self.conn, "紫の灯台の噂", at=1150)
        mark_batches_annexed(self.conn, [b1], entry.id)
        self.conn.commit()
        hits = unified_recall(
            self.conn, self.embedder, "紫の灯台",
            search_chronicle=True, search_memopedia=False,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )
        types = [h.source_type for h in hits]
        self.assertIn("chronicle", types)
        self.assertNotIn("perception", types)
        # 転写先がヒットしない検索では perception 単独で出る (到達手段は残る)。
        b2 = _batch(self.conn, "誰の digest にも居ない緑の鐘", at=1500)
        mark_batches_annexed(self.conn, [b2], "entry-gone")
        self.conn.commit()
        hits2 = self._recall("緑の鐘")
        self.assertEqual([h.source_type for h in hits2], ["perception"])

    def test_perception_survives_when_target_chronicle_not_selected(self):
        # 併合の基準は「最終採用結果に転写先がいるか」(Codex 第六巡 #2)。
        # 転写先が候補 (embedding) には居るが採用されない構成 — 旧実装は候補
        # 集合で判定して perception を先に消し、転写先も落選して両方消えていた。
        import json
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.unified_recall import unified_recall
        init_arasuji_tables(self.conn)
        # キーワードにヒットする別 Chronicle。
        other = create_entry(
            self.conn, level=1, content="別件のあらすじ: 白鯨バーの開店",
            source_ids=["m1"], start_time=1000, end_time=1100,
            source_count=1, message_count=1,
        )
        # 転写先 (キーワードを含まない = 落選する) + embedding 候補にだけ載る。
        target = create_entry(
            self.conn, level=1, content="転写先のあらすじ (検索語なし)",
            source_ids=["m2"], start_time=1200, end_time=1300,
            source_count=1, message_count=1,
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO arasuji_embeddings (entry_id, vector) VALUES (?, ?)",
            (target.id, json.dumps([0.0, 0.0, 0.0])),
        )
        self.conn.commit()
        b1 = _batch(self.conn, "白鯨バーの噂の知覚", at=1250)
        mark_batches_annexed(self.conn, [b1], target.id)
        self.conn.commit()

        hits = unified_recall(
            self.conn, self.embedder, "白鯨バー",
            topk=2,
            search_chronicle=True, search_memopedia=False,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )
        types = {h.source_type for h in hits}
        ids = {h.source_id for h in hits}
        # 転写先は落選し、perception はそのまま返る (両方消えない)。
        self.assertIn("perception", types)
        self.assertIn(other.id, ids)
        self.assertNotIn(target.id, ids)

    def test_recall_order_is_deterministic(self):
        # 候補化の安定順 + 決定的タイブレーク (Codex 第八巡 #2): 同一入力で
        # 2 回呼んで同一の並びが返る (固定点併合の採用集合も安定)。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.unified_recall import unified_recall
        init_arasuji_tables(self.conn)
        target = create_entry(
            self.conn, level=1, content="市場のあらすじ (転写先)",
            source_ids=["m1"], start_time=1000, end_time=1100,
            source_count=1, message_count=1,
        )
        create_entry(
            self.conn, level=1, content="市場の別のあらすじ",
            source_ids=["m2"], start_time=1200, end_time=1300,
            source_count=1, message_count=1,
        )
        for i, at in enumerate((1050, 1050, 1060)):
            b = _batch(self.conn, f"市場の知覚{i}", at=at)
            if i == 0:
                mark_batches_annexed(self.conn, [b], target.id)
        self.conn.commit()

        def _run():
            hits = unified_recall(
                self.conn, self.embedder, "市場",
                search_chronicle=True, search_memopedia=False,
                search_fragments=False, search_messages=False,
                search_perceptions=True,
            )
            return [(h.source_type, h.source_id, round(h.score, 9)) for h in hits]

        first = _run()
        self.assertEqual(first, _run())

    def test_focus_perception_slots_refill_after_merge(self):
        # 併合で外れた perception のぶんは RRF 順の未採用候補で再充填する —
        # focus の枠を消費したまま件数が欠けない (Codex 第七巡 #3)。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.unified_recall import unified_recall
        init_arasuji_tables(self.conn)
        # 転写先 c1 は両キーワードに一致 (perception と同じ層で最上位に来る)。
        c1 = create_entry(
            self.conn, level=1, content="赤い市場のあらすじ",
            source_ids=["m1"], start_time=1000, end_time=1100,
            source_count=1, message_count=1,
        )
        c2 = create_entry(
            self.conn, level=1, content="市場の別のあらすじ",
            source_ids=["m2"], start_time=1200, end_time=1300,
            source_count=1, message_count=1,
        )
        c3 = create_entry(
            self.conn, level=1, content="市場のさらに別のあらすじ",
            source_ids=["m3"], start_time=1400, end_time=1500,
            source_count=1, message_count=1,
        )
        for i, at in enumerate((1050, 1060)):
            b = _batch(self.conn, f"赤い市場の知覚{i}", at=at)
            mark_batches_annexed(self.conn, [b], c1.id)
        self.conn.commit()

        hits = unified_recall(
            self.conn, self.embedder, "赤い 市場",
            topk=3, focus="perception",
            search_chronicle=True, search_memopedia=False,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )
        # c1 が採用 → それを指す perception 2 件は併合で外れ、空いた 2 枠は
        # c2/c3 で再充填される (旧実装は後段削除のみで 1 件に痩せた)。
        self.assertEqual(len(hits), 3)
        self.assertEqual({h.source_id for h in hits}, {c1.id, c2.id, c3.id})
        self.assertNotIn("perception", {h.source_type for h in hits})

    def test_three_source_contract_merges_into_single_chronicle_hit(self):
        # unified-recall API の既定 (chronicle/memopedia/perception ON) で、
        # 付記済み perception は Chronicle ヒットへ併合され 1 件になる。
        # Chronicle エントリは memopedia_pages に同居するが、memopedia ソース
        # からは除外される — 同 id の上書きで併合が壊れない (第五巡 #3)。
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memopedia import init_memopedia_tables
        from sai_memory.unified_recall import unified_recall
        init_memopedia_tables(self.conn)
        init_arasuji_tables(self.conn)
        entry = create_entry(
            self.conn, level=1,
            content="あらすじ本文\n- 紅玉の市場の噂",
            source_ids=["m1"], start_time=1000, end_time=1400,
            source_count=1, message_count=1,
        )
        b1 = _batch(self.conn, "紅玉の市場の噂", at=1150)
        mark_batches_annexed(self.conn, [b1], entry.id)
        self.conn.commit()
        hits = unified_recall(
            self.conn, self.embedder, "紅玉の市場",
            search_chronicle=True, search_memopedia=True,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )
        relevant = [h for h in hits if "紅玉" in (h.content or "") or h.source_id == entry.id]
        self.assertEqual(len(relevant), 1)  # 2 件返却にならない
        self.assertEqual(relevant[0].source_type, "chronicle")
        self.assertEqual(relevant[0].source_id, entry.id)
        # 同居ページが memopedia ソースとして紛れ込まない。
        self.assertNotIn("memopedia", [h.source_type for h in hits])


class MemopediaChronicleGuardApiTest(unittest.TestCase):
    """REST / スペル層の保護追従 (Codex 第四巡 #1): 409 と Error 文の回帰。"""

    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        env_patcher = patch.dict(os.environ, {
            "SAIVERSE_HOME": self._tmp.name, "SAIMEMORY_MEMORY": "1",
        })
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        embed_patcher = patch(
            "saiverse_memory.adapter.Embedder", _DummyEmbedder,
        )
        embed_patcher.start()
        self.addCleanup(embed_patcher.stop)

        from saiverse_memory.adapter import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter("tester")
        self.addCleanup(self.adapter.close)
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        with self.adapter._db_lock:
            init_arasuji_tables(self.adapter.conn)
            self.entry = create_entry(
                self.adapter.conn, level=1, content="あらすじ",
                source_ids=["m1"], start_time=1000, end_time=1400,
                source_count=1, message_count=1,
            )
        self.manager = SimpleNamespace(
            personas={"tester": SimpleNamespace(sai_memory=self.adapter)},
            SessionLocal=None,
        )

    def test_rest_delete_returns_409(self):
        from fastapi import HTTPException
        from api.routes.people.memopedia import delete_memopedia_page
        with self.assertRaises(HTTPException) as ctx:
            delete_memopedia_page("tester", self.entry.id, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_rest_trunk_route_409_on_true_ok_on_false(self):
        from fastapi import HTTPException
        from api.routes.people.memopedia import (
            SetTrunkRequest,
            set_memopedia_page_trunk,
        )
        with self.assertRaises(HTTPException) as ctx:
            set_memopedia_page_trunk(
                "tester", self.entry.id, SetTrunkRequest(is_trunk=True),
                manager=self.manager,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        # False は冪等な安全操作として通る。
        resp = set_memopedia_page_trunk(
            "tester", self.entry.id, SetTrunkRequest(is_trunk=False),
            manager=self.manager,
        )
        self.assertTrue(resp["success"])
        self.assertFalse(resp["page"]["is_trunk"])

    def test_rest_update_put_409_on_trunk_true_ok_on_false(self):
        from fastapi import HTTPException
        from api.routes.people.memopedia import (
            UpdateMemopediaPageRequest,
            update_memopedia_page,
        )
        with self.assertRaises(HTTPException) as ctx:
            update_memopedia_page(
                "tester", self.entry.id,
                UpdateMemopediaPageRequest(is_trunk=True),
                manager=self.manager,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        resp = update_memopedia_page(
            "tester", self.entry.id,
            UpdateMemopediaPageRequest(is_trunk=False),
            manager=self.manager,
        )
        self.assertTrue(resp["success"])

    def test_rest_clear_and_import_keep_chronicle(self):
        # Memopedia の一括操作 (全削除 / clear 付き import) が Chronicle を
        # 物理削除しない (Codex 第五巡 #1 — W14 以前からの既存欠陥)。
        # Chronicle の一括削除は arasuji 側の clear_all_entries だけ。
        from api.routes.people.memopedia import (
            delete_all_memopedia_pages,
            import_memopedia,
        )
        from sai_memory.memopedia.storage import create_page, get_page
        with self.adapter._db_lock:
            knowledge = create_page(
                self.adapter.conn, parent_id=None, title="知識ページ",
                summary="", content="本文", category="terms",
            )

        resp = delete_all_memopedia_pages("tester", manager=self.manager)
        self.assertTrue(resp["success"])
        with self.adapter._db_lock:
            # 知識ページは消え、Chronicle entry は互換ビューに残る。
            self.assertIsNone(get_page(self.adapter.conn, knowledge.id))
            row = self.adapter.conn.execute(
                "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?",
                (self.entry.id,),
            ).fetchone()
            self.assertEqual(row[0], 1)

        resp = import_memopedia(
            "tester",
            body={"version": 1, "pages": [{
                "id": "imported-1", "parent_id": None, "title": "輸入",
                "summary": "", "content": "x", "category": "terms",
            }]},
            clear=True, manager=self.manager,
        )
        self.assertTrue(resp["success"])
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?",
                (self.entry.id,),
            ).fetchone()
            self.assertEqual(row[0], 1)

    def test_export_excludes_chronicle(self):
        # export は Chronicle を含まない — metadata (level/source_ids) を運ばない
        # 形式で書き出しても壊れた entry しか復元できない。
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        data = memopedia.export_json()
        self.assertTrue(
            all(p.get("category") != "chronicle" for p in data["pages"])
        )
        # import 側も旧 export の chronicle 残骸を作らない。
        imported = memopedia.import_json({"version": 1, "pages": [{
            "id": "legacy-ch", "parent_id": None, "title": "残骸",
            "summary": "", "content": "x", "category": "chronicle",
        }]})
        self.assertEqual(imported, 0)

    def test_rest_composite_put_rejects_before_mutation(self):
        # 複合 PUT (本文変更 + is_trunk=true) は update_page より前に 409 —
        # 「409 なのに本文だけ変わった」部分適用を作らない (Codex 第五巡 #2)。
        from fastapi import HTTPException
        from api.routes.people.memopedia import (
            UpdateMemopediaPageRequest,
            update_memopedia_page,
        )
        with self.assertRaises(HTTPException) as ctx:
            update_memopedia_page(
                "tester", self.entry.id,
                UpdateMemopediaPageRequest(content="書き換え本文", is_trunk=True),
                manager=self.manager,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT content FROM memopedia_pages WHERE id = ?",
                (self.entry.id,),
            ).fetchone()
        self.assertEqual(row[0], "あらすじ")  # 本文は未変更

    def test_memory_delete_spell_returns_error_prefixed_message(self):
        # スペル層: 保護は "Error" 始まりで返す — 呼び出し側 (memory_delete) の
        # head 変異通知の抑止規約に乗り、「削除成功」と誤通知しない。
        from saiverse import memory_atlas
        with self.adapter._db_lock:
            result = memory_atlas.delete_page(
                self.adapter, f"memopedia:{self.entry.id}",
            )
        self.assertTrue(result.startswith("Error"))
        self.assertIn("保護", result)
        # entry は互換ビューに残る。
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?",
                (self.entry.id,),
            ).fetchone()
        self.assertEqual(row[0], 1)


class AnnexStampLifecycleTest(unittest.TestCase):
    """付記印のライフサイクル (2026-08-19 Codex 第二巡 #1/#2)。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, "
            "role TEXT, content TEXT, resource_id TEXT, created_at INTEGER, "
            "metadata TEXT)"
        )
        from sai_memory.arasuji import init_arasuji_tables
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)
        # プロンプトを記録するフェイク client — 新設計ではバッチは digest 本文
        # ではなく材料 (プロンプト) に入るので、検証はプロンプト側で行う。
        self.prompts = []

        def _generate(**kw):
            self.prompts.append(kw["messages"][0]["content"])
            return "LLM要約"

        self.client = SimpleNamespace(generate=_generate)

    def _make_messages(self, times, prefix="m", insert_rows=False):
        from sai_memory.memory.storage import Message
        msgs = [
            Message(
                id=f"{prefix}{i}", thread_id="t1", role="user",
                content=f"発言{i}", resource_id="p1", created_at=t,
            )
            for i, t in enumerate(times)
        ]
        if insert_rows:
            for m in msgs:
                self.conn.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (m.id, m.thread_id, m.role, m.content, m.resource_id,
                     m.created_at),
                )
            self.conn.commit()
        return msgs

    def _compile(self, times, prefix="m", insert_rows=False):
        from sai_memory.arasuji.alignment import plan_alignment
        from sai_memory.arasuji.executor import execute_plan
        msgs = self._make_messages(times, prefix=prefix, insert_rows=insert_rows)
        plan = plan_alignment(msgs, set(), target_chars=10)
        return execute_plan(plan, self.client, self.conn, persona_id="p1")

    def test_entry_deletion_returns_batch_then_later_compile_recovers(self):
        # entry 削除で付記印が戻り (= 提示に再登場)、後続の編纂の一括回収が
        # 材料として引き取って再付記する (Codex #1)。
        from sai_memory.arasuji.storage import delete_entry
        b1 = _batch(self.conn, "消される entry の知覚", at=1150)
        result1 = self._compile([1000, 1400])
        entry1 = result1.created[0]
        self.assertEqual(list_unannexed_batches(self.conn), [])

        self.assertTrue(delete_entry(self.conn, entry1.id))
        # 印が戻る = 未付記一覧 (提示対象) に再登場。
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )

        # 直後の編纂 (被覆の先端が無いので回収境界も無い) では拾われない。
        self.prompts.clear()
        self._compile([2000, 2400], prefix="n")
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )
        self.assertNotIn("消される entry の知覚", "\n".join(self.prompts))

        # さらに次の編纂: 先頭チャンクの一括回収 (prev_end=2400 ≥ 1150) が
        # 材料として引き取り、再付記される。
        self.prompts.clear()
        result3 = self._compile([3000, 3400], prefix="o")
        self.assertIn("消される entry の知覚", "\n".join(self.prompts))
        # digest 本文には知覚の生文は入らない (LLM 出力のみ)。
        self.assertNotIn("消される entry の知覚", result3.created[0].content)
        self.assertEqual(list_unannexed_batches(self.conn), [])
        row = self.conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
            (b1,),
        ).fetchone()
        self.assertEqual(row[0], result3.created[0].id)

    def test_clear_all_entries_returns_all_stamps(self):
        # 全削除は付記印を全部返す (削除と同一 tx)。
        from sai_memory.arasuji.storage import clear_all_entries
        b1 = _batch(self.conn, "全削除で戻る知覚", at=1150)
        self._compile([1000, 1400])
        self.assertEqual(list_unannexed_batches(self.conn), [])
        self.assertGreaterEqual(clear_all_entries(self.conn), 1)
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )

    def test_memopedia_soft_delete_refuses_chronicle_pages(self):
        # soft delete (is_deleted=1) は互換ビューから entry を消すのに付記印を
        # 戻さない — Chronicle は専用削除経路のみ許可 (Codex 第三巡 #1)。拒否は
        # 「未発見の False」と区別できる専用例外で表明する (第四巡 #1)。
        from sai_memory.memopedia import ChronicleProtectedError, Memopedia
        b1 = _batch(self.conn, "soft delete 検証の知覚", at=1150)
        result = self._compile([1000, 1400])
        entry = result.created[0]
        memopedia = Memopedia(self.conn)
        with self.assertRaises(ChronicleProtectedError):
            memopedia.delete_page(entry.id)
        # entry は互換ビューに残り、バッチも付記済みのまま (恒久不可視なし)。
        row = self.conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(list_unannexed_batches(self.conn), [])
        stamp = self.conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
            (b1,),
        ).fetchone()[0]
        self.assertEqual(stamp, entry.id)
        # 未発見は従来どおり False (例外ではない) — 区別の検算。
        self.assertFalse(memopedia.delete_page("no-such-page"))

    def test_memopedia_set_trunk_refuses_only_promotion(self):
        # is_trunk=True は互換ビュー (is_trunk=0 のみ) から entry を消す可視性
        # 操作なので拒否 (専用例外)。False は可視性を奪わない冪等な安全操作
        # として通す (Codex 第四巡 #3)。
        from sai_memory.memopedia import ChronicleProtectedError, Memopedia
        result = self._compile([1000, 1400])
        entry = result.created[0]
        memopedia = Memopedia(self.conn)
        with self.assertRaises(ChronicleProtectedError):
            memopedia.set_trunk(entry.id, True)
        row = self.conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], 1)
        # False は通り、現在ページが返る (既に 0 なので no-op)。
        page = memopedia.set_trunk(entry.id, False)
        self.assertIsNotNone(page)
        self.assertFalse(page.is_trunk)

    def test_curation_merge_rejects_chronicle_before_mutation(self):
        # curation の merge は変更前に chronicle を検証してプランを失敗させる —
        # 途中の delete_page 例外で部分変更だけが残る形にしない (第四巡 #1)。
        from sai_memory.curation_ops import execute_merge
        from sai_memory.memopedia import Memopedia
        from sai_memory.memopedia.storage import create_page
        result = self._compile([1000, 1400])
        entry = result.created[0]
        survivor = create_page(
            self.conn, parent_id=None, title="残す側", summary="", content="本文",
            category="terms",
        )
        memopedia = Memopedia(self.conn)
        with self.assertRaises(ValueError):
            execute_merge(self.conn, survivor.id, entry.id, memopedia)
        # 変更前拒否: entry は無傷 (ビューに残る)、survivor 本文も不変。
        row = self.conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], 1)
        from sai_memory.memopedia.storage import get_page
        self.assertEqual(get_page(self.conn, survivor.id).content, "本文")

    def test_regenerate_feeds_old_batches_as_material_and_repoints_stamps(self):
        # 再生成の swap は旧 entry の材料バッチを継承する: バッチ本文が再生成
        # LLM の材料 (extra_items) として渡り、印は新 id へ付け替わり、未付記
        # バッチは増えない (Codex 第三巡 #3 → 2026-08-29 裁定で材料方式)。
        from unittest.mock import patch
        from sai_memory.arasuji.storage import create_entry, regenerate_entry

        b1 = _batch(self.conn, "再生成で引き継ぐ知覚", at=1150)
        result = self._compile([1000, 1400], insert_rows=True)
        old_entry = result.created[0]
        self.assertEqual(list_unannexed_batches(self.conn), [])

        seen = {}

        def _fake_regen(conn, messages, model_name, persona_id=None,
                        extra_items=None):
            seen["extra_items"] = extra_items
            return create_entry(
                conn, level=1, content="再生成された本文",
                source_ids=[m.id for m in messages],
                start_time=min(m.created_at for m in messages),
                end_time=max(m.created_at for m in messages),
                source_count=len(messages), message_count=len(messages),
            )

        with patch(
            "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
            side_effect=_fake_regen,
        ):
            new_entry = regenerate_entry(self.conn, old_entry.id)

        self.assertIsNotNone(new_entry)
        self.assertIn("再生成された本文", new_entry.content)
        # 旧バッチが材料として再生成 LLM へ渡っている。
        self.assertEqual(
            [d["text"] for d in seen["extra_items"]], ["再生成で引き継ぐ知覚"],
        )
        # digest 本文への機械的な転写はしない (本文は LLM 出力のみ)。
        self.assertNotIn("再生成で引き継ぐ知覚", new_entry.content)
        # 印は新 id へ付け替え済み — 再生成直後に未付記バッチは増えない。
        self.assertEqual(list_unannexed_batches(self.conn), [])
        stamp = self.conn.execute(
            "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
            (b1,),
        ).fetchone()[0]
        self.assertEqual(stamp, new_entry.id)
        # 旧 entry は消えている。
        row = self.conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?", (old_entry.id,),
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_storage_delete_boundary_protects_chronicle_descendants(self):
        # 保護の境界は storage.delete_page (実際に DELETE を発行する場所)。
        # 「chronicle を通常ページ配下へ移動 → 親ごと削除」の迂回路でも、
        # フラグなしの子孫再帰は chronicle を消さず root_chronicle へ退避する
        # (Codex 第六巡 #1)。
        from sai_memory.memopedia.storage import (
            create_page,
            delete_page,
            get_page,
        )
        b1 = _batch(self.conn, "境界検証の知覚", at=1150)
        result = self._compile([1000, 1400])
        entry = result.created[0]
        parent = create_page(
            self.conn, parent_id=None, title="親ページ", summary="",
            content="x", category="terms",
        )
        # ガード導入前の異常配置を模す (直接 UPDATE — move API は今は拒否する)。
        self.conn.execute(
            "UPDATE memopedia_pages SET parent_id = ? WHERE id = ?",
            (parent.id, entry.id),
        )
        self.conn.commit()

        # 親の削除: 親は消えるが chronicle 子孫は残り、root_chronicle へ退避。
        self.assertTrue(delete_page(self.conn, parent.id))
        self.assertIsNone(get_page(self.conn, parent.id))
        row = self.conn.execute(
            "SELECT parent_id FROM memopedia_pages WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "root_chronicle")
        # 互換ビューにも残り、付記印も無傷。
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM arasuji_entries WHERE id = ?", (entry.id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT annexed_entry_id FROM perception_batches WHERE id = ?",
                (b1,),
            ).fetchone()[0],
            entry.id,
        )
        # 直接名指しのフラグなし削除も拒否 (False)。allow_chronicle=True だけが通る。
        self.assertFalse(delete_page(self.conn, entry.id))
        self.assertTrue(delete_page(self.conn, entry.id, allow_chronicle=True))

    def test_detach_resets_consolidation_and_parent_refs(self):
        # 退避は親替えだけでは足りない (Codex 第七巡 #1): is_consolidated=0 の
        # 同時更新と、旧親 Chronicle (Lv2) の source_ids からの参照除去まで行う。
        from sai_memory.arasuji.storage import (
            add_to_parent_source_ids,
            create_entry,
            get_entry,
        )
        from sai_memory.memopedia.storage import create_page, delete_page
        result = self._compile([1000, 1400])
        entry = result.created[0]
        lv2 = create_entry(
            self.conn, level=2, content="二次あらすじ", source_ids=[],
            start_time=1000, end_time=1400, source_count=0, message_count=0,
        )
        # 正規の統合状態 (is_consolidated=1 + 親 = Lv2 + 親の source_ids に登録)。
        self.assertTrue(add_to_parent_source_ids(self.conn, entry.id, lv2.id))
        self.assertEqual(get_entry(self.conn, entry.id).is_consolidated, 1)
        # 異常配置: 統合済みの子を通常ページ配下へ (ガード導入前の移動を模す)。
        normal = create_page(
            self.conn, parent_id=None, title="通常ページ", summary="",
            content="x", category="terms",
        )
        self.conn.execute(
            "UPDATE memopedia_pages SET parent_id = ? WHERE id = ?",
            (normal.id, entry.id),
        )
        self.conn.commit()

        self.assertTrue(delete_page(self.conn, normal.id))
        detached = get_entry(self.conn, entry.id)
        self.assertIsNotNone(detached)
        # 未統合状態へ完全に戻る: 親 = root (view では None)・is_consolidated=0。
        self.assertIsNone(detached.parent_id)
        self.assertEqual(detached.is_consolidated, 0)
        row = self.conn.execute(
            "SELECT parent_id FROM memopedia_pages WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], "root_chronicle")
        # 旧親 Lv2 の source_ids から参照が外れている。
        lv2_after = get_entry(self.conn, lv2.id)
        self.assertNotIn(entry.id, lv2_after.source_ids)

    def test_move_apis_refuse_chronicle(self):
        # 保護境界の外へ運び出す操作 (親付け替え) も拒否の族 (Codex 第六巡 #1)。
        from sai_memory.memopedia.storage import (
            create_page,
            move_pages_to_parent,
            update_page,
        )
        result = self._compile([1000, 1400])
        entry = result.created[0]
        dest = create_page(
            self.conn, parent_id=None, title="移動先", summary="",
            content="x", category="terms",
        )
        moved = move_pages_to_parent(self.conn, [entry.id], dest.id)
        self.assertEqual(moved, 0)
        row = self.conn.execute(
            "SELECT parent_id FROM memopedia_pages WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], "root_chronicle")
        # 汎用 update_page の parent_id change も拒否 (None)。
        self.assertIsNone(update_page(self.conn, entry.id, parent_id=dest.id))
        row = self.conn.execute(
            "SELECT parent_id FROM memopedia_pages WHERE id = ?", (entry.id,),
        ).fetchone()
        self.assertEqual(row[0], "root_chronicle")

    def test_stale_collection_conflict_does_not_double_supply(self):
        # 収集結果が古く「既に別 entry へ付記済み」のバッチを含んでいた場合、
        # 印の行数不一致で tx を破棄し、再収集 + LLM 呼び直しでチャンクごと
        # やり直す — 同じ知覚が複数 entry の材料にならない (Codex #2 の
        # 材料方式版。材料が変わるのでプロンプトも作り直しになる)。
        from unittest.mock import patch
        import sai_memory.arasuji.executor as executor_mod

        b_stale = _batch(self.conn, "他所へ付記済みの知覚", at=1100)
        mark_batches_annexed(self.conn, [b_stale], "entry-other")
        self.conn.commit()
        b_live = _batch(self.conn, "未付記の知覚", at=1150)

        real_collect = executor_mod.collect_annex_items
        calls = {"n": 0}

        def _stale_then_real(conn, lo, hi, **kwargs):
            calls["n"] += 1
            items, ids = real_collect(conn, lo, hi, **kwargs)
            if calls["n"] == 1:
                # 1 回目だけ「tx の外の古い収集」を装い、付記済みバッチを混ぜる。
                items = (
                    [{"at": 1100, "text": "他所へ付記済みの知覚"}] + items
                )
                ids = [b_stale] + ids
            return items, ids

        with patch.object(
            executor_mod, "collect_annex_items", side_effect=_stale_then_real,
        ):
            result = self._compile([1000, 1400])

        self.assertEqual(calls["n"], 2)  # 不一致 → rollback → 再収集
        # LLM もやり直している (1 回目の材料は stale 混入で無効)。
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("他所へ付記済みの知覚", self.prompts[0])
        self.assertNotIn("他所へ付記済みの知覚", self.prompts[1])
        self.assertIn("未付記の知覚", self.prompts[1])
        entry = result.created[0]
        # 印の整合: stale は他所の印のまま、live は今回の entry へ。
        rows = dict(self.conn.execute(
            "SELECT id, annexed_entry_id FROM perception_batches",
        ).fetchall())
        self.assertEqual(rows[b_stale], "entry-other")
        self.assertEqual(rows[b_live], entry.id)


class DirectInsertMigrationTest(unittest.TestCase):
    """B4/B7: event_message 直挿しの push_perception への移送 (§10.6)。"""

    def test_move_failure_pushes_world_state_perception(self):
        from saiverse.day_plan import _record_move_failure

        pushed = []

        def _push(kind, content, **kwargs):
            pushed.append((kind, content))

        persona = SimpleNamespace(
            persona_id="p1",
            sai_memory=SimpleNamespace(push_perception=_push),
        )
        # NOTE: day_plan には _building_display_name が二重定義されており
        # (3599 行 = building_map 版 / 5292 行 = buildings 版)、モジュール
        # ロード時は後者が勝つ。テストは実効定義 (buildings リスト) に合わせる。
        manager = SimpleNamespace(buildings=[
            SimpleNamespace(building_id="b_target", name="工房"),
            SimpleNamespace(building_id="b_current", name="自宅"),
        ])
        _record_move_failure(
            manager, persona, {"title": "朝の制作"},
            "b_current", "b_target", "満員",
        )
        self.assertEqual(len(pushed), 1)
        kind, content = pushed[0]
        self.assertEqual(kind, "world_state")
        self.assertIn("朝の制作", content)
        self.assertIn("工房", content)
        self.assertIn("自宅", content)
        self.assertIn("満員", content)
        # 直挿し時代の <system> 包みは付けない (整形は flush / マージの仕事)。
        self.assertNotIn("<system>", content)

    def test_move_failure_without_adapter_is_noop(self):
        from saiverse.day_plan import _record_move_failure
        persona = SimpleNamespace(persona_id="p1", sai_memory=None)
        manager = SimpleNamespace(buildings=[])
        _record_move_failure(manager, persona, {}, "a", "b", "x")  # 例外なし

    def test_upgrade_notification_is_idempotent_across_consumption(self):
        import os
        import tempfile
        from unittest.mock import patch

        # Windows では sqlite ハンドル解放が rmtree に間に合わないことがある。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = {"SAIVERSE_HOME": tmp, "SAIMEMORY_MEMORY": "1"}
            with patch.dict(os.environ, env), \
                    patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
                from saiverse.upgrade_handlers import _insert_upgrade_notification
                from saiverse_memory.adapter import SAIMemoryAdapter

                _insert_upgrade_notification("tester")
                adapter = SAIMemoryAdapter("tester")
                try:
                    from sai_memory.perception_buffer import (
                        list_consumed_since,
                        list_pending,
                    )
                    with adapter._db_lock:
                        pending = list_pending(adapter.conn)
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0].kind, "world_state")
                    self.assertIn("v0.3.0", pending[0].content)

                    # 二度目は積まれない (未消費照合)。
                    _insert_upgrade_notification("tester")
                    with adapter._db_lock:
                        self.assertEqual(len(list_pending(adapter.conn)), 1)

                    # 消費された後も積み直さない (消費済みも含む照合 =
                    # upgrade_id で一度きり)。
                    with adapter._db_lock:
                        create_consumption_batch(
                            adapter.conn, [pending[0].id],
                            consumed_at=1000, rendered_text=pending[0].content,
                        )
                    _insert_upgrade_notification("tester")
                    with adapter._db_lock:
                        self.assertEqual(list_pending(adapter.conn), [])
                        self.assertEqual(
                            len(list_consumed_since(adapter.conn, 0)), 1,
                        )
                finally:
                    adapter.close()


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


if __name__ == "__main__":
    unittest.main()
