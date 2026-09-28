"""リアルタイム情報の項目別トグルと、前回発言時刻の読み取りの契約。

docs/intent/realtime_info.md の不変条件をここで固定する:

- 「現在時刻」「あなたの前回発言」はペルソナごとに別々に ON/OFF できる
  (AI.REALTIME_CURRENT_TIME_ENABLED / AI.REALTIME_LAST_UTTERANCE_ENABLED)。
- 前回発言時刻は epoch 秒 (SAIMemory の ``created_at`` の形) / datetime /
  ISO 文字列のどれでも読める。v0.3.14 まで epoch 秒を読めずに黙って捨てて
  いたため「あなたの前回発言」が一度も出なかった (ユーザー報告 2026-09-26)。
- 読めない時刻は黙って捨てず DEBUG ログに残す。
- 出すものが無ければメッセージ自体を返さない (None)。
- 時刻の 2 項目が両方 OFF でも、リアルタイムスペルの結果は
  ``_execute_realtime_spells`` が自前でメッセージを作って届ける。
- 旧 REALTIME_INFO_ENABLED は全書換マイグレーションで「現在時刻」へ引き継ぎ、
  「前回発言」は OFF (リリースの瞬間に全ペルソナの文面が変わらない)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from sea.runtime import SEARuntime

JST = timezone(timedelta(hours=9))

#: 2026-09-26 03:00 UTC = 2026-09-26 12:00 JST (土)
EPOCH_SECONDS = 1790391600
EXPECTED_PREV_LINE = "あなたの前回発言: 2026年09月26日(土) 12:00"


def _persona():
    return SimpleNamespace(
        persona_id="p1",
        persona_name="Persona",
        timezone=JST,
    )


def _build(flags, history):
    """``_build_realtime_context`` をフラグ固定の偽 self で呼ぶ。"""
    fake_self = SimpleNamespace(
        _realtime_info_flags_for_persona=lambda p: flags,
        _parse_history_timestamp=SEARuntime._parse_history_timestamp,
    )
    return SEARuntime._build_realtime_context(fake_self, _persona(), "b1", history)


def _history(created_at):
    return [
        {"role": "user", "content": "こんにちは", "created_at": EPOCH_SECONDS - 60},
        {"role": "assistant", "content": "やあ", "created_at": created_at},
        {"role": "user", "content": "元気?"},
    ]


# ---------------------------------------------------------------------------
# 前回発言時刻の読み取り (不具合の再現 → 修正の証明)
# ---------------------------------------------------------------------------


class LastUtteranceTimestampParsingTest(unittest.TestCase):
    def _assert_prev_line(self, created_at):
        msg = _build((False, True), _history(created_at))
        self.assertIsNotNone(msg)
        self.assertIn(EXPECTED_PREV_LINE, msg["content"])

    def test_epoch_seconds_int_is_read(self):
        """SAIMemory の created_at (epoch 秒の int) で前回発言の行が出る。"""
        self._assert_prev_line(EPOCH_SECONDS)

    def test_epoch_seconds_float_is_read(self):
        self._assert_prev_line(float(EPOCH_SECONDS) + 0.25)

    def test_epoch_seconds_digit_string_is_read(self):
        self._assert_prev_line(str(EPOCH_SECONDS))

    def test_iso_string_is_read(self):
        self._assert_prev_line("2026-09-26T03:00:00Z")

    def test_datetime_object_is_read(self):
        self._assert_prev_line(datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc))

    def test_timestamp_key_fallback_is_read(self):
        history = [{"role": "assistant", "content": "やあ", "timestamp": EPOCH_SECONDS}]
        msg = _build((False, True), history)
        self.assertIsNotNone(msg)
        self.assertIn(EXPECTED_PREV_LINE, msg["content"])

    def test_timestamp_key_is_tried_when_created_at_is_unreadable(self):
        """created_at が読めない値でも、同じ行の timestamp を諦めない。"""
        history = [{
            "role": "assistant", "content": "やあ",
            "created_at": "not-a-time", "timestamp": EPOCH_SECONDS,
        }]
        msg = _build((False, True), history)
        self.assertIsNotNone(msg)
        self.assertIn(EXPECTED_PREV_LINE, msg["content"])

    def test_epoch_zero_means_unrecorded_not_1970(self):
        """0 は「時刻が記録されていない」— 1970 年を前回発言として見せない。"""
        history = [{"role": "assistant", "content": "やあ", "created_at": 0}]
        self.assertIsNone(_build((False, True), history))

    def test_epoch_zero_string_means_unrecorded_not_1970(self):
        """文字列の "0" も数値の 0 と同じ「未記録」— 1970 年に化けない。"""
        history = [{"role": "assistant", "content": "やあ", "created_at": "0"}]
        self.assertIsNone(_build((False, True), history))

    def test_eight_digit_zero_string_is_unrecorded_without_log(self):
        """"00000000" はゼロなので「未記録」— 日付分岐へ入れずログも出さない。"""
        with mock.patch.object(logging.getLogger("sea.runtime"), "debug") as debug:
            self.assertIsNone(SEARuntime._parse_history_timestamp("00000000"))
        debug.assert_not_called()

    def test_epoch_zero_string_does_not_block_timestamp_key(self):
        """created_at="0" でも同じ行の timestamp を諦めない。"""
        history = [{
            "role": "assistant", "content": "やあ",
            "created_at": "0", "timestamp": EPOCH_SECONDS,
        }]
        msg = _build((False, True), history)
        self.assertIsNotNone(msg)
        self.assertIn(EXPECTED_PREV_LINE, msg["content"])

    def test_unreadable_timestamp_is_logged_not_silently_dropped(self):
        """読めない時刻は DEBUG に値と型を残し、その次に古い発言を探す。"""
        history = [
            {"role": "assistant", "content": "古い", "created_at": EPOCH_SECONDS},
            {"role": "assistant", "content": "新しい", "created_at": "not-a-time"},
        ]
        with self.assertLogs("sea.runtime", level="DEBUG") as logs:
            msg = _build((False, True), history)
        self.assertTrue(
            any("not-a-time" in line and "str" in line for line in logs.output),
            logs.output,
        )
        self.assertIsNotNone(msg)
        self.assertIn(EXPECTED_PREV_LINE, msg["content"])

    def test_bool_is_not_taken_as_epoch(self):
        self.assertIsNone(SEARuntime._parse_history_timestamp(True))

    def test_basic_iso_date_string_is_not_taken_as_epoch(self):
        """8 桁の数字列 ("20260928") は基本形式の ISO 日付 — epoch 秒に化けない。"""
        parsed = SEARuntime._parse_history_timestamp("20260928")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 9, 28))


# ---------------------------------------------------------------------------
# トグルの 4 組合せ
# ---------------------------------------------------------------------------


class RealtimeToggleCombinationTest(unittest.TestCase):
    def _lines(self, msg):
        return [line for line in msg["content"].splitlines() if line.startswith("- ")]

    def test_both_on_renders_two_lines(self):
        msg = _build((True, True), _history(EPOCH_SECONDS))
        self.assertIsNotNone(msg)
        lines = self._lines(msg)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("- 現在時刻: "))
        self.assertEqual(lines[1], f"- {EXPECTED_PREV_LINE}")
        self.assertTrue(msg["metadata"]["__realtime_context__"])
        self.assertEqual(msg["role"], "user")

    def test_current_time_only(self):
        msg = _build((True, False), _history(EPOCH_SECONDS))
        self.assertIsNotNone(msg)
        lines = self._lines(msg)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("- 現在時刻: "))
        self.assertNotIn("あなたの前回発言", msg["content"])

    def test_last_utterance_only(self):
        msg = _build((False, True), _history(EPOCH_SECONDS))
        self.assertIsNotNone(msg)
        self.assertEqual(self._lines(msg), [f"- {EXPECTED_PREV_LINE}"])
        self.assertNotIn("現在時刻", msg["content"])

    def test_both_off_returns_none(self):
        """出すものが無ければメッセージ自体を送らない。"""
        self.assertIsNone(_build((False, False), _history(EPOCH_SECONDS)))

    def test_last_utterance_on_without_history_returns_none(self):
        """前回発言だけ ON で発言履歴が無い — 空の枠を見せない。"""
        self.assertIsNone(_build((False, True), []))


# ---------------------------------------------------------------------------
# 仮想クロック中は前回発言の行を出さない (実時刻の混入を防ぐ)
# ---------------------------------------------------------------------------


class VirtualClockSuppressesLastUtteranceTest(unittest.TestCase):
    """履歴の created_at は実時刻なので、仮想の「現在時刻」と並べない
    (docs/intent/realtime_info.md「時刻の読み取りの修正」)。"""

    def test_last_utterance_only_returns_none_under_virtual_clock(self):
        from saiverse import clock

        with mock.patch.object(clock, "is_virtual", return_value=True):
            self.assertIsNone(_build((False, True), _history(EPOCH_SECONDS)))

    def test_both_on_renders_only_virtual_current_time(self):
        from saiverse import clock

        virtual_now = datetime(2026, 9, 26, 9, 0)  # naive (シナリオの仮想時刻)
        with mock.patch.object(clock, "is_virtual", return_value=True), \
             mock.patch.object(clock, "now", return_value=virtual_now):
            msg = _build((True, True), _history(EPOCH_SECONDS))
        self.assertIsNotNone(msg)
        self.assertIn("現在時刻: 2026年09月26日(土) 09:00", msg["content"])
        self.assertNotIn("あなたの前回発言", msg["content"])


# ---------------------------------------------------------------------------
# フラグの読み取り (1 回のクエリで 2 列 / 既定値)
# ---------------------------------------------------------------------------


class RealtimeFlagsReadTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        db = self.SessionLocal()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(AI(AIID="p_default", HOME_CITYID=1, AINAME="Default"))
            db.add(AI(
                AIID="p_flipped", HOME_CITYID=1, AINAME="Flipped",
                REALTIME_CURRENT_TIME_ENABLED=False,
                REALTIME_LAST_UTTERANCE_ENABLED=True,
            ))
            db.commit()
        finally:
            db.close()

    def _flags(self, persona_id, manager=True):
        fake_self = SimpleNamespace(
            manager=SimpleNamespace(SessionLocal=self.SessionLocal) if manager else None,
            _REALTIME_FLAGS_FALLBACK=SEARuntime._REALTIME_FLAGS_FALLBACK,
        )
        persona = SimpleNamespace(persona_id=persona_id)
        return SEARuntime._realtime_info_flags_for_persona(fake_self, persona)

    def test_new_persona_defaults_to_current_time_only(self):
        self.assertEqual(self._flags("p_default"), (True, False))

    def test_stored_values_are_read(self):
        self.assertEqual(self._flags("p_flipped"), (False, True))

    def test_missing_row_falls_back_to_column_defaults(self):
        self.assertEqual(self._flags("nobody"), (True, False))

    def test_no_manager_falls_back_to_column_defaults(self):
        self.assertEqual(self._flags("p_flipped", manager=False), (True, False))


# ---------------------------------------------------------------------------
# 両方 OFF でもリアルタイムスペルの結果は届く (不変条件 1)
# ---------------------------------------------------------------------------


class _FakeBinding:
    SPELL_NAME = "see"
    SPELL_ARGS_JSON = None
    LABEL = "気圧"
    BINDING_ID = 1
    PRIORITY = 0
    ENABLED = True
    OWNER_KIND = "persona"
    OWNER_ID = "p1"


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._result


class _FakeSession:
    def __init__(self, bindings):
        self._bindings = bindings

    def query(self, *a, **k):
        return _FakeQuery(self._bindings)

    def close(self):
        pass


class SpellResultsSurviveBothTogglesOffTest(unittest.TestCase):
    def _run_spells(self, messages):
        from sea import runtime_llm

        async def fake_spell(spell_name, args, persona, state, marker, cb, messages=None):
            return "1008.8 hPa", {}, True

        state = {"_messages": messages}
        session = _FakeSession([_FakeBinding()])
        runtime = SimpleNamespace(manager=SimpleNamespace(SessionLocal=lambda: session))
        with mock.patch.object(runtime_llm, "SPELL_TOOL_NAMES", {"see"}), \
             mock.patch.object(runtime_llm, "canonicalize_spell_name", lambda n: n), \
             mock.patch.object(runtime_llm, "_run_spell_tool_async", fake_spell):
            asyncio.run(
                runtime_llm._execute_realtime_spells(runtime, _persona(), "b1", state, None)
            )

    def test_spell_creates_the_message_when_times_are_off(self):
        history = _history(EPOCH_SECONDS)
        # 時刻の 2 項目が両方 OFF → 組み立て側はメッセージを作らない
        self.assertIsNone(_build((False, False), history))

        messages = [dict(m) for m in history]
        self._run_spells(messages)

        realtime = [
            (i, m) for i, m in enumerate(messages)
            if m.get("metadata", {}).get("__realtime_context__")
        ]
        self.assertEqual(len(realtime), 1)
        idx, msg = realtime[0]
        # 最後のユーザー発言の直前に置かれる (不変条件 2)
        self.assertEqual(idx, len(messages) - 2)
        self.assertEqual(messages[-1]["content"], "元気?")
        self.assertIn("- 気圧: 1008.8 hPa", msg["content"])
        self.assertNotIn("現在時刻", msg["content"])
        self.assertNotIn("あなたの前回発言", msg["content"])


# ---------------------------------------------------------------------------
# 旧 REALTIME_INFO_ENABLED からのマイグレーション写像
# ---------------------------------------------------------------------------


def _seed_world(db_path, ai_ids):
    """現行スキーマのファイル DB に User / City / AI 行を ORM で入れる。

    NOT NULL 列の既定値は Python 側 default なので、生 SQL の INSERT では
    埋まらない — 行は ORM で作ってから AI テーブルを旧形へ戻す。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            for ai_id in ai_ids:
                db.add(AI(AIID=ai_id, HOME_CITYID=1, AINAME=ai_id))
            db.commit()
        finally:
            db.close()
    finally:
        engine.dispose()


class RealtimeInfoMigrationTest(unittest.TestCase):
    """旧列 → 2 列の写像。全書換パスに載ることも併せて固定する。"""

    def _make_legacy_db(self, tmpdir):
        """現行スキーマから AI テーブルだけ旧形 (全体トグル 1 列) に戻した DB。"""
        db_path = os.path.join(tmpdir, "saiverse.db")
        _seed_world(db_path, ("p_on", "p_off", "p_off_str", "p_off_word", "p_junk"))
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute('ALTER TABLE ai DROP COLUMN "REALTIME_CURRENT_TIME_ENABLED"')
            conn.execute('ALTER TABLE ai DROP COLUMN "REALTIME_LAST_UTTERANCE_ENABLED"')
            conn.execute(
                'ALTER TABLE ai ADD COLUMN "REALTIME_INFO_ENABLED" BOOLEAN NOT NULL DEFAULT 1'
            )
            conn.execute(
                "UPDATE ai SET REALTIME_INFO_ENABLED = 0 WHERE AIID = 'p_off'"
            )
            # SQLite は列型を強制しない — 手作業の DB で文字列 '0' が入っていても
            # OFF の選択が ON へ反転しないこと (bool('0') is True の罠)
            conn.execute(
                "UPDATE ai SET REALTIME_INFO_ENABLED = '0' WHERE AIID = 'p_off_str'"
            )
            conn.execute(
                "UPDATE ai SET REALTIME_INFO_ENABLED = 'false' WHERE AIID = 'p_off_word'"
            )
            # 許可表現の外の値 — 黙って OFF にせず、警告付きで旧列の既定 (True) に倒す
            conn.execute(
                "UPDATE ai SET REALTIME_INFO_ENABLED = 'banana' WHERE AIID = 'p_junk'"
            )
            conn.commit()
        return db_path

    def _read_flags(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT AIID, REALTIME_CURRENT_TIME_ENABLED, "
                    "REALTIME_LAST_UTTERANCE_ENABLED FROM ai ORDER BY AIID"
                )).fetchall()
            for r in rows:
                # NULL を bool() で False に潰すと「コピー段が列既定を書いた」
                # 証拠にならない — 素の値の非 NULL を先に確かめる
                assert r[1] is not None and r[2] is not None, rows
            return {r[0]: (bool(r[1]), bool(r[2])) for r in rows}
        finally:
            engine.dispose()

    def test_full_rewrite_maps_legacy_toggle(self):
        from database.migrate import (
            migrate_database_in_place,
            needs_migration,
            try_additive_migration,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_legacy_db(tmpdir)

            self.assertTrue(needs_migration(db_path))
            # 旧列の削除を含む差分なので追加系では解消できない (全書換に落ちる)。
            # 早期削除 (KNOWN_COLUMN_DROPS) に載っていないので値も残っている。
            self.assertFalse(try_additive_migration(db_path))
            with closing(sqlite3.connect(db_path)) as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ai)")}
            self.assertIn("REALTIME_INFO_ENABLED", cols)

            with self.assertLogs(level=logging.INFO) as logs:
                migrate_database_in_place(db_path)
            self.assertTrue(
                any("REALTIME_INFO_ENABLED -> REALTIME_CURRENT_TIME_ENABLED" in m
                    for m in logs.output),
                logs.output,
            )
            # 解釈できない値は黙って倒さず WARNING を残す
            self.assertTrue(
                any("banana" in m and "WARNING" in m for m in logs.output),
                logs.output,
            )

            self.assertFalse(needs_migration(db_path))
            self.assertEqual(
                self._read_flags(db_path),
                {
                    "p_junk": (True, False),       # 解釈不能 → 警告付きで旧既定 (True)
                    "p_off": (False, False),       # 旧 OFF → 両方 OFF
                    "p_off_str": (False, False),   # 文字列 '0' も OFF のまま (反転させない)
                    "p_off_word": (False, False),  # 文字列 'false' も OFF のまま
                    "p_on": (True, False),         # 旧 ON → 現在時刻だけ ON
                },
            )

    def test_legacy_column_is_not_an_early_drop(self):
        """早期削除に入れると、コピー段より先に値が消えて引き継げない。"""
        from database.migrate import KNOWN_COLUMN_DROPS

        for columns in KNOWN_COLUMN_DROPS.values():
            self.assertNotIn("REALTIME_INFO_ENABLED", columns)

    def test_no_op_when_source_lacks_legacy_column(self):
        from database.migrate import _migrate_realtime_info_to_item_toggles

        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = os.path.join(tmpdir, "src.db")
            tgt_path = os.path.join(tmpdir, "tgt.db")
            _seed_world(src_path, ("p1",))
            _seed_world(tgt_path, ("p1",))
            src = create_engine(f"sqlite:///{src_path}")
            tgt = create_engine(f"sqlite:///{tgt_path}")
            try:
                with tgt.begin() as conn:
                    conn.execute(text(
                        "UPDATE ai SET REALTIME_CURRENT_TIME_ENABLED = 0, "
                        "REALTIME_LAST_UTTERANCE_ENABLED = 1"
                    ))
                _migrate_realtime_info_to_item_toggles(src, tgt)
                with tgt.connect() as conn:
                    row = conn.execute(text(
                        "SELECT REALTIME_CURRENT_TIME_ENABLED, "
                        "REALTIME_LAST_UTTERANCE_ENABLED FROM ai"
                    )).fetchone()
                self.assertEqual((bool(row[0]), bool(row[1])), (False, True))
            finally:
                src.dispose()
                tgt.dispose()


if __name__ == "__main__":
    unittest.main()
