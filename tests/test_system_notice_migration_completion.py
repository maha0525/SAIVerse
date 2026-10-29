"""システム通知の移行の、記憶 DB の完了の印を立てる前提と、起動時の知らせ。

database/system_notice_migration.py。合成した一時 DB だけを使う。固定すること:

- 完了の印は「今の版の規則を、欠けの無い材料で、その DB に届きうる旧形式の行の
  全部へ当てた」記録。前提が欠けた起動 (規則 3 の Playbook 名を完全に読めない・
  本体 DB の移行が失敗した・前の起動の仮の結果の JSON の採否が決まっていない) では、
  変換と確認待ちの印だけをコミットして完了の印は今のまま残し、次の起動で走査し直す。
- 起動時の知らせの文言は frontend の文言表 (frontend/src/i18n/messages.json) にあり、
  バックエンドは翻訳の鍵と引数 (``api.ui_messages.ui_message``) だけを返す。
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from database import system_notice_migration as mig
from database.system_notice_migration import (
    MARKER_HELD_MAIN_DB_FAILED,
    MARKER_HELD_PLAYBOOK_NAMES,
    MARKER_HELD_STAGED_RESULT,
    MIGRATION_MARKER_KEY,
    MIGRATION_RULES_VERSION,
    OUTPUT_DIR_NAME,
    RESULTS_DIR_NAME,
    StoreResult,
    marker_rules_version,
    run_migration,
    run_startup_system_notice_migration,
)
from sai_memory.memory.storage import (
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    init_db,
)

_V2_MARKER = "v2 2026-10-28T00:00:00+00:00"
_TABLE = json.loads(
    (Path(__file__).resolve().parent.parent / "frontend" / "src" / "i18n" / "messages.json")
    .read_text(encoding="utf-8"),
)


def _render(value, locale: str = "ja"):
    """frontend の decodeUIMessages + t と同じく、翻訳の鍵と引数を文言にする。"""
    if isinstance(value, list):
        return [_render(v, locale) for v in value]
    if isinstance(value, dict) and set(value) == {"$ui", "params"}:
        params = {k: _render(v, locale) for k, v in value["params"].items()}
        text = _TABLE[value["$ui"]][locale]
        return re.sub(
            r"(?<!\$)\{(\w+)\}",
            lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0),
            text,
        )
    if isinstance(value, dict):
        return {k: _render(v, locale) for k, v in value.items()}
    return value


def _envelopes(value):
    if isinstance(value, list):
        for v in value:
            yield from _envelopes(v)
    elif isinstance(value, dict) and set(value) == {"$ui", "params"}:
        yield value
        yield from _envelopes(list(value["params"].values()))
    elif isinstance(value, dict):
        yield from _envelopes(list(value.values()))


def _assert_localized(alert):
    """title と message は翻訳の鍵で、鍵は文言表にあり、両言語で引数がそろう。"""
    for field_name in ("title", "message"):
        assert isinstance(alert[field_name], dict) and "$ui" in alert[field_name], field_name
    for env in _envelopes(alert):
        key = env["$ui"]
        assert key.startswith("api."), key
        assert _TABLE[key]["ja"] and _TABLE[key]["en"], key
        for locale in ("ja", "en"):
            for param in env["params"]:
                assert "{" + param + "}" in _TABLE[key][locale], (key, locale, param)
    # 英語で描いても日本語が混ざらない (利用者の persona_id などの引数は除く)
    rendered = _render({"title": alert["title"], "message": alert["message"]}, "en")
    assert not re.search(r"[぀-ヿ一-鿿]", json.dumps(rendered, ensure_ascii=False))


def _insert(conn, row_id, role, content, metadata, *, created_at=0):
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?, 't', ?, ?, 'p1', ?, ?)",
        (row_id, role, content, created_at, json.dumps(metadata, ensure_ascii=False)),
    )


def _rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]: (r[1], r[2], json.loads(r[3]) if r[3] else {})
            for r in conn.execute("SELECT id, role, content, metadata FROM messages").fetchall()
        }


def _marker(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        row = conn.execute(
            "SELECT value FROM embed_metadata WHERE key = ?", (MIGRATION_MARKER_KEY,),
        ).fetchone()
    return row[0] if row else None


def _set_marker(path: Path, value: str) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embed_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (MIGRATION_MARKER_KEY, value, "2026-10-28T00:00:00+00:00"),
        )
        conn.commit()


@pytest.fixture
def persona(tmp_path):
    """版 2 で移行を済ませた記憶。版 3 の規則 1 に当たる行と、規則 3 に当たる行がある。"""
    db = tmp_path / "personas" / "p1" / "memory.db"
    db.parent.mkdir(parents=True)
    with closing(init_db(str(db))) as conn:
        _insert(conn, "tool", "user", "<system>Email sent.</system>",
                {"tags": ["tool"], SYSTEM_NOTICE_REVIEW_KEY: "系統を判定できなかった"})
        _insert(conn, "flow", "user", "<system>章を書き終えた</system>",
                {"tags": ["lost_flow"], SYSTEM_NOTICE_REVIEW_KEY: "系統を判定できなかった"},
                created_at=1)
        conn.commit()
    _set_marker(db, _V2_MARKER)
    return db


def _playbook_dir(tmp_path: Path, *, broken: bool) -> Path:
    root = tmp_path / "builtin_playbooks"
    root.mkdir(exist_ok=True)
    (root / "a.json").write_text(json.dumps({"name": "other_flow"}), encoding="utf-8")
    (root / "lost.json").write_text(
        "{broken" if broken else json.dumps({"name": "lost_flow"}), encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# 規則 3 の材料が欠けた起動
# ---------------------------------------------------------------------------


def test_incomplete_playbook_names_hold_the_marker_until_they_can_be_read(
    persona, tmp_path, monkeypatch,
):
    root = _playbook_dir(tmp_path, broken=True)
    monkeypatch.setattr(mig, "_builtin_playbooks_dir", lambda: root)

    names, complete = mig.known_playbook_names(None)
    assert names == {"other_flow"} and not complete

    (result,), _ = run_migration(db_path=None, saiverse_home=tmp_path)

    rows = _rows(persona)
    # 欠けていない規則の変換はコミットする
    assert rows["tool"][0] == "system"
    # 欠けた名前に当たる行は確認待ちのまま、完了の印は版 2 のまま
    assert rows["flow"][0] == "user"
    assert result.marker_held == [MARKER_HELD_PLAYBOOK_NAMES]
    assert _marker(persona) == _V2_MARKER
    report = mig.render_report([result], dry_run=False, stamp="s")
    assert "完全に読めなかった" in report

    # 読めるようになった起動で、走査し直して印を進める
    _playbook_dir(tmp_path, broken=False)
    (again,), _ = run_migration(db_path=None, saiverse_home=tmp_path)
    assert not again.marker_held
    assert _rows(persona)["flow"][0] == "system"
    assert marker_rules_version(_marker(persona)) == MIGRATION_RULES_VERSION


def test_an_unreadable_main_db_holds_the_marker(persona, tmp_path):
    main = tmp_path / "saiverse.db"
    main.write_bytes(b"this is not a database" * 100)

    names, complete = mig.known_playbook_names(main)
    assert not complete

    results, _ = run_migration(db_path=main, saiverse_home=tmp_path)

    main_result, persona_result = results
    assert main_result.error
    assert persona_result.marker_held == [MARKER_HELD_PLAYBOOK_NAMES, MARKER_HELD_MAIN_DB_FAILED]
    assert _marker(persona) == _V2_MARKER


def test_a_failed_main_db_migration_holds_the_persona_marker(persona, tmp_path, monkeypatch):
    """本体 DB の送信トレイには、記憶へ書かれる前の旧形式の本文が残りうる。"""
    def _fail(_db_path, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    main = tmp_path / "saiverse.db"
    with closing(sqlite3.connect(str(main))) as conn:
        conn.execute("CREATE TABLE playbooks (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO playbooks (name) VALUES ('lost_flow')")
        conn.commit()
    monkeypatch.setattr(mig, "migrate_main_db", _fail)

    (main_result, persona_result), _ = run_migration(db_path=main, saiverse_home=tmp_path)

    assert main_result.error
    assert persona_result.marker_held == [MARKER_HELD_MAIN_DB_FAILED]
    # 変換はコミットする (規則 3 は本体 DB の playbooks 表から名前を読めている)
    assert {r for r, (role, _c, _m) in _rows(persona).items() if role == "system"} == {
        "tool", "flow",
    }
    assert _marker(persona) == _V2_MARKER

    monkeypatch.undo()
    (_main, again), _ = run_migration(db_path=main, saiverse_home=tmp_path)
    assert not again.marker_held
    assert marker_rules_version(_marker(persona)) == MIGRATION_RULES_VERSION


# ---------------------------------------------------------------------------
# 前の起動の仮の結果の JSON の採否が決まっていない起動
# ---------------------------------------------------------------------------


def _leave_committed_v2_result(tmp_path: Path, persona: Path) -> Path:
    """版 2 の移行をコミットした後、正式な名前へ付け替える前に止まった起動の仮の JSON。"""
    run_dir = tmp_path / "backups" / OUTPUT_DIR_NAME / RESULTS_DIR_NAME / "20261028_000000_1_aaaaaa"
    run_dir.mkdir(parents=True)
    staged = run_dir / "persona_p1.json.tmp"
    staged.write_text(json.dumps(StoreResult(
        label="ペルソナ p1", path=str(persona), persona=True, marker_value=_V2_MARKER,
        changes=[mig.Change("messages", "x", "schedule", "<system>a</system>", "a")],
    ).to_json(), ensure_ascii=False), encoding="utf-8")
    return staged


def test_an_unsettled_staged_result_holds_the_marker(persona, tmp_path, monkeypatch):
    """仮の JSON を読めず採否を決められなかった起動では、印を書き換えない (Codex 1 巡目 指摘 11)。

    書き換えると、次の起動で仮の JSON に書いた印の値と一致せず、コミット済みの版 2 の
    結果が捨てられる。
    """
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    staged = _leave_committed_v2_result(tmp_path, persona)
    real_read_bytes = Path.read_bytes

    def _flaky(self):
        if self == staged:
            raise PermissionError("locked by another process")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _flaky)
    alerts = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))

    assert staged.exists()
    assert _marker(persona) == _V2_MARKER
    assert _rows(persona)["tool"][0] == "system"  # 変換はコミットする
    held = [a for a in alerts if a["id"] == "system_notice_migration_held"]
    assert len(held) == 1
    _assert_localized(held[0])
    assert "前回の移行の結果のファイル" in _render(held[0]["message"])
    assert _render(held[0]["details"]) == {"held": ["ペルソナ p1"]}

    # 読めるようになった起動で、版 2 の結果を採用してから印を進める
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
    alerts = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))
    assert not staged.exists()
    assert (staged.parent / "persona_p1.json").exists()
    assert marker_rules_version(_marker(persona)) == MIGRATION_RULES_VERSION
    assert not [a for a in alerts if a["id"] == "system_notice_migration_held"]


def test_a_staged_result_of_a_running_start_does_not_hold_the_marker(persona, tmp_path):
    staged = _leave_committed_v2_result(tmp_path, persona)
    assert mig._unsettled_staged_persona_ids(tmp_path / "backups" / OUTPUT_DIR_NAME) == {"p1"}
    # この起動のフォルダのものは数えない
    assert mig._unsettled_staged_persona_ids(
        tmp_path / "backups" / OUTPUT_DIR_NAME, current_run=staged.parent,
    ) == set()


# ---------------------------------------------------------------------------
# 起動時の知らせは翻訳の鍵と引数で返す
# ---------------------------------------------------------------------------


def test_the_done_alert_is_a_localized_message(persona, tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    alerts = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))

    (done,) = [a for a in alerts if a["id"] == "system_notice_migration_done"]
    _assert_localized(done)
    assert _render(done["message"]) == (
        "1 件の記録を新しい形式へ変換しました。移行前の写しを保存してあります。"
    )
    assert _render(done["message"], "en").startswith("Converted 1 records")


def test_the_aborted_alert_is_a_localized_message(tmp_path, monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    monkeypatch.setattr(mig, "run_migration", _boom)
    (alert,) = run_startup_system_notice_migration(str(tmp_path / "no_main.db"))
    assert alert["id"] == "system_notice_migration_failed"
    _assert_localized(alert)


def test_the_failed_alert_lists_localized_labels(persona, tmp_path, monkeypatch):
    def _fail(_db_path, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    main = tmp_path / "saiverse.db"
    sqlite3.connect(str(main)).close()
    monkeypatch.setattr(mig, "migrate_main_db", _fail)
    alerts = run_startup_system_notice_migration(str(main))

    (failed,) = [a for a in alerts if a["id"] == "system_notice_migration_failed"]
    _assert_localized(failed)
    assert _render(failed["details"]) == {"failed": ["本体 DB"]}
    assert _render(failed["details"], "en") == {"failed": ["Main database"]}
