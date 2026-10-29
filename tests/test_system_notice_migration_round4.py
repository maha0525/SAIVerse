"""システム通知の移行の、敵対レビュー 4 巡目の指摘への回帰試験 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「追補」の「移行の規則の第二版」と「時刻の境」。
合成した一時 DB だけを使う。固定すること:

- 記憶の行が ``building_msg_ref`` を持ち、その元の行 (本体 DB の ``building_messages``) が
  画面からの送信の印 ``client_message_id`` を持つなら、どの版のどの規則に当たっても自動では
  変換せず確認待ちにする (:data:`SENT_FROM_SCREEN_REASON`)。全体の走査でも、規則の版を
  上げた走査し直しでも同じ。本体 DB の ``voice_call`` の行も、``client_message_id`` を持てば
  変換しない。
- 固いリンクを作れない Windows 以外の環境では、境を記録できないものとして
  :class:`CutoverUnavailable` を送り、何も変換しない。
- 移行の中の「この media の項目は音声か」は、音声を読み取る側
  (``saiverse.media_utils.iter_audio_media``) と同じ判定 (``type`` の大文字小文字を区別
  しない・``type`` が無ければ ``mime_type`` が ``audio/`` で始まるか) を使う。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database import system_notice_migration as mig
from database.system_notice_migration import (
    CUTOVER_FILE_NAME,
    SENT_FROM_SCREEN_REASON,
    VOICE_INPUT_NOTICE_KIND,
    VOICE_SOURCE_UNKNOWN_REASON,
    BuildingSourceRows,
    CutoverUnavailable,
    migrate_main_db,
    migrate_persona_db,
    run_startup_system_notice_migration,
)
from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, SYSTEM_NOTICE_REVIEW_KEY
from tests.test_system_notice_migration_round2 import (
    _insert_building_row,
    _make_main_db,
    _make_persona,
    _output_dir,
    _rows,
    _set_marker,
    _sql_time,
)
from tests.test_system_notice_migration_round3 import _make_voice_persona

CUTOVER = datetime(2026, 10, 28, 5, 0, 0, tzinfo=timezone.utc)
PRE = int((CUTOVER - timedelta(hours=1)).timestamp())
STAMP = "20261029_000000"

_VOICE_TEXT = "ｽﾀｯｸﾁｬﾝから音声入力を受信しました。添付の音声を聴いて応答してください。"
_VOICE_CONTENT = f"<system>{_VOICE_TEXT}</system>"


def _migrate(db: Path, tmp_path: Path, source_rows):
    return migrate_persona_db(
        db, persona_id="p1", output_dir=tmp_path / "out", stamp=STAMP,
        playbook_names=frozenset({"memory_research"}), source_rows=source_rows,
        cutover=int(CUTOVER.timestamp()),
    )


# ---------------------------------------------------------------------------
# 1. 画面から送られた発言は、どの規則でも自動で変換しない
# ---------------------------------------------------------------------------

#: 旧規則 (版 1) に当たる行。版 2 (:func:`mig.legacy_notice`) と版 3 の規則 1〜4 は
#: ``building_msg_ref`` を持つ行に当たらない (来歴の縛り) ので、穴は版 1 の規則と規則 5。
#: (role, 本文, tags)
_RULE_ROWS = [
    pytest.param("user", "<system>tool の結果</system>", ["tool", "event_message"], id="event_message"),
    pytest.param("user", "<system>今日の予定</system>", ["event_message", "day_plan"], id="day_plan"),
    pytest.param("user", "<system>状況が変わった</system>", ["situation_change"], id="situation_change"),
    pytest.param("user", "<system>切り替え</system>", ["track_context"], id="track_context"),
    pytest.param("user", "<system>記録</system>", ["playbook:x"], id="playbook_tag"),
    pytest.param("system", "スペルの結果", ["spell"], id="unmarked_system"),
]


@pytest.mark.parametrize("rescan", [False, True], ids=["full_scan", "rescan_from_v2"])
@pytest.mark.parametrize("role, content, tags", _RULE_ROWS)
def test_a_row_sent_from_the_screen_is_held_by_every_rule(tmp_path, role, content, tags, rescan):
    """版 2 の完了の後に、古い建物履歴から転記された行を版 3 で走査し直す場合も含む。"""
    meta = {"tags": tags, "building_msg_ref": "b1:b1:1"}
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content=content, raw_meta="{}", cmid="cmid-1")
    db = _make_persona(tmp_path, [("screen", role, content, meta, PRE)])
    if rescan:
        _set_marker(db, f"v2 {(CUTOVER - timedelta(days=1)).isoformat()}")

    with BuildingSourceRows(main) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    if rescan:
        assert result.rescan_from_version == 2
    assert _rows(db)["screen"][:2] == (role, content)
    raw = _rows(db)["screen"][2]
    assert json.loads(raw) == {**meta, SYSTEM_NOTICE_REVIEW_KEY: SENT_FROM_SCREEN_REASON}
    assert [(s.row_id, s.reason) for s in result.skipped] == [("screen", SENT_FROM_SCREEN_REASON)]
    assert result.changes == []


@pytest.mark.parametrize("role, content, tags", _RULE_ROWS)
def test_a_row_whose_source_has_no_client_message_id_is_still_converted(tmp_path, role, content, tags):
    meta = {"tags": tags, "building_msg_ref": "b1:b1:1"}
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content=content, raw_meta="{}")
    db = _make_persona(tmp_path, [("relay", role, content, meta, PRE)])

    with BuildingSourceRows(main) as source_rows:
        _migrate(db, tmp_path, source_rows)

    stored_role, _stored, raw = _rows(db)["relay"]
    assert stored_role == "system"
    assert SYSTEM_NOTICE_KEY in json.loads(raw)


def test_a_row_whose_source_cannot_be_found_keeps_the_old_handling(tmp_path):
    """元の行を決められない行は、規則 5 以外では今までどおり変換する (旧来の残余と同じ)。"""
    content = "<system>tool の結果</system>"
    meta = {"tags": ["tool", "event_message"], "building_msg_ref": "b1:b1:404"}
    main = _make_main_db(tmp_path / "saiverse.db")
    db = _make_persona(tmp_path, [("gone", "user", content, meta, PRE)])

    with BuildingSourceRows(main) as source_rows:
        _migrate(db, tmp_path, source_rows)

    assert _rows(db)["gone"][0] == "system"


def test_a_voice_row_sent_from_the_screen_uses_the_general_reason(tmp_path):
    meta = {
        "tags": ["conversation"], "with": ["user"], "source": "stackchan_voice",
        "media": [{"type": "audio", "uri": "saiverse://audio/s.ogg"}], "building_msg_ref": "b1:b1:1",
    }
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content=_VOICE_CONTENT, raw_meta=json.dumps(meta), cmid="c")
    db = _make_voice_persona(tmp_path, [("screen", meta)])

    with BuildingSourceRows(main) as source_rows:
        _migrate(db, tmp_path, source_rows)

    assert json.loads(_rows(db)["screen"][2])[SYSTEM_NOTICE_REVIEW_KEY] == SENT_FROM_SCREEN_REASON


@pytest.mark.parametrize("cmid, converted", [("cmid-1", False), (None, True)])
def test_a_voice_call_row_in_the_main_db_sent_from_the_screen_is_not_converted(tmp_path, cmid, converted):
    """本体 DB の voice_call の行も、画面からの送信の印を持てば変換しない。"""
    content = "<system>通話: こんにちは</system>"
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(
        main, seq=1, content=content, cmid=cmid, created_at=_sql_time(PRE),
        raw_meta=json.dumps({"tags": ["internal", "event_message"], "voice_call": True}),
    )

    result = migrate_main_db(
        main, output_dir=tmp_path / "out", stamp=STAMP, cutover=int(CUTOVER.timestamp()),
    )

    with closing(sqlite3.connect(str(main))) as conn:
        role, stored = conn.execute("SELECT role, content FROM building_messages").fetchone()
    if converted:
        assert (role, stored) == ("host", "通話: こんにちは")
    else:
        assert (role, stored) == ("user", content)
        assert [s.reason for s in result.skipped] == [SENT_FROM_SCREEN_REASON]


# ---------------------------------------------------------------------------
# 2. 固いリンクを作れない Windows 以外の環境では、境を記録しない
# ---------------------------------------------------------------------------


class _PosixOsWithoutLinks:
    """``os`` の代わり。名前を posix にし、固いリンクを作れない (ハードリンク非対応の
    ファイルシステム)。rename は POSIX の意味 (既存の名前を上書き)。"""

    name = "posix"

    def __getattr__(self, attr):
        return getattr(os, attr)

    @staticmethod
    def link(*_args, **_kwargs):
        raise OSError("hard links are not supported here")

    @staticmethod
    def rename(src, dst):
        os.replace(src, dst)


def _as_posix_without_links(monkeypatch) -> None:
    monkeypatch.setattr(mig, "os", _PosixOsWithoutLinks())
    monkeypatch.setattr(mig, "_fsync_dir", lambda directory: None)


def test_without_hard_links_outside_windows_the_cutover_is_not_recorded(tmp_path, monkeypatch):
    path = _output_dir(tmp_path) / CUTOVER_FILE_NAME
    _as_posix_without_links(monkeypatch)

    with pytest.raises(CutoverUnavailable):
        mig._record_cutover(path, mig.Cutover(CUTOVER, mig.CUTOVER_FROM_START), saiverse_home=tmp_path)

    assert not path.exists()
    assert [p.name for p in path.parent.iterdir()] == []  # 仮のファイルもロックも残さない


def test_without_hard_links_outside_windows_nothing_is_converted(tmp_path, monkeypatch):
    content = "<system>Email sent.</system>"
    db = _make_persona(tmp_path, [("pre_tool", "user", content, {"tags": ["tool"]}, PRE)])
    before = _rows(db)
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    _as_posix_without_links(monkeypatch)

    alerts = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))

    assert [a["id"] for a in alerts] == ["system_notice_migration_cutover_unavailable"]
    assert _rows(db) == before
    assert not (_output_dir(tmp_path) / CUTOVER_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# 3. 音声の判定は、音声を読み取る側と同じ
# ---------------------------------------------------------------------------

_URI = "saiverse://audio/same.ogg"

#: 音声を読み取る側 (iter_audio_media) が音声として扱う項目の、type="audio" 以外の形。
_AUDIO_FORMS = [
    pytest.param({"type": "Audio", "uri": _URI}, id="type_capitalized"),
    pytest.param({"type": "AUDIO", "uri": _URI, "mime_type": "audio/ogg"}, id="type_upper"),
    pytest.param({"mime_type": "audio/ogg", "uri": _URI}, id="mime_only"),
    pytest.param({"type": "", "mime_type": "AUDIO/WAV", "uri": _URI}, id="mime_upper_empty_type"),
]


@pytest.mark.parametrize("item", _AUDIO_FORMS)
def test_a_screen_candidate_in_another_audio_form_is_counted(tmp_path, item):
    """同じ uri の中継の行と、別の形の音声を持つ画面からの送信の行 — 候補は二つ。"""
    memory_meta = {
        "tags": ["conversation"], "with": ["user"], "source": "stackchan_voice",
        "media": [{"type": "audio", "uri": _URI, "mime_type": "audio/ogg"}],
    }
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(memory_meta))
    _insert_building_row(main, seq=2, content="x", cmid="cmid-2", raw_meta=json.dumps({"media": [item]}))
    db = _make_voice_persona(tmp_path, [("voice", memory_meta)])

    with BuildingSourceRows(main) as source_rows:
        result = _migrate(db, tmp_path, source_rows)

    role, content, raw = _rows(db)["voice"]
    assert (role, content) == ("user", _VOICE_CONTENT)
    assert json.loads(raw)[SYSTEM_NOTICE_REVIEW_KEY] == VOICE_SOURCE_UNKNOWN_REASON
    assert [(s.row_id, s.reason) for s in result.skipped] == [("voice", VOICE_SOURCE_UNKNOWN_REASON)]


@pytest.mark.parametrize("item", _AUDIO_FORMS)
def test_a_voice_row_in_another_audio_form_is_rule_5(tmp_path, item):
    """記憶の行の音声が別の形でも規則 5 に当たり、同じ形の中継の行一つで二行に分ける。"""
    meta = {"tags": ["conversation"], "with": ["user"], "source": "stackchan_voice", "media": [item]}
    main = _make_main_db(tmp_path / "saiverse.db")
    _insert_building_row(main, seq=1, content="x", raw_meta=json.dumps(meta))
    db = _make_voice_persona(tmp_path, [("voice", meta)])

    with BuildingSourceRows(main) as source_rows:
        _migrate(db, tmp_path, source_rows)

    rows = _rows(db)
    role, content, raw = rows["voice"]
    assert (role, content) == ("system", _VOICE_TEXT)
    assert json.loads(raw)[SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND
    assert len(rows) == 2


@pytest.mark.parametrize("item, expected", [
    ({"type": "audio"}, True),
    ({"type": "Audio"}, True),
    ({"mime_type": "audio/ogg"}, True),
    ({"type": "", "mime_type": "Audio/Ogg"}, True),
    # type があれば type だけで決める (読み取る側と同じ)
    ({"type": "image", "mime_type": "audio/ogg"}, False),
    ({"mime_type": "video/mp4"}, False),
    ({}, False),
])
def test_the_audio_check_is_the_one_the_reader_uses(item, expected):
    from saiverse.media_utils import is_audio_media_item

    assert is_audio_media_item(item) is expected
    assert mig._has_audio_media({"media": [item]}) is expected
