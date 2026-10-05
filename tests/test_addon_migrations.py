"""アップロード済みファイルの移行 (saiverse/addon_migrations.py) のテスト。

旧規約では本体のアップロード処理がファイルを ``user_data/addon_files/<id>/`` に
置き、その絶対パスを ``addon_persona_config.params_json`` に記録していた。新規約の
``user_data/addon_data/<id>/inputs/`` へフォルダを動かすときに、記録された絶対
パスとアップロード先も同じ規則で揃っていることを確かめる。

``SAIVERSE_HOME`` と ``USER_DATA_DIR`` は tmp_path に向け、本番の ``~/.saiverse`` には
触れない。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import saiverse.addon_migrations as addon_migrations
import saiverse.addon_paths as addon_paths
from saiverse.addon_migrations import (
    ENABLED_ADDONS_FOR_STARTUP,
    migrate_addon_data_dirs,
    rewrite_addon_file_paths_in_db,
)

VOICE_TTS = "saiverse-voice-tts"


@pytest.fixture
def user_data(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".saiverse"
    ud = home / "user_data"
    ud.mkdir(parents=True)
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setattr(addon_migrations, "USER_DATA_DIR", ud)
    monkeypatch.setattr(addon_paths, "USER_DATA_DIR", ud)
    return ud


def _make_db(path: Path, persona_rows: list[tuple[str, str, dict]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE addon_config (addon_name TEXT PRIMARY KEY, is_enabled INTEGER, "
            "params_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE addon_persona_config (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "addon_name TEXT NOT NULL, persona_id TEXT NOT NULL, params_json TEXT NOT NULL)"
        )
        for addon_name, persona_id, params in persona_rows:
            conn.execute(
                "INSERT INTO addon_persona_config (addon_name, persona_id, params_json) "
                "VALUES (?, ?, ?)",
                (addon_name, persona_id, json.dumps(params, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def _persona_params(path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT persona_id, params_json FROM addon_persona_config"
        ).fetchall()
    finally:
        conn.close()
    return {pid: json.loads(pj) for pid, pj in rows}


def _write(path: Path, data: bytes = b"RIFF") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_起動時の対象に_voice_tts_が入っている() -> None:
    assert VOICE_TTS in ENABLED_ADDONS_FOR_STARTUP


def test_参照音声のフォルダと記録されたパスが_inputs_へ揃って移る(user_data, tmp_path) -> None:
    """``\\`` 区切りと ``/`` 区切りのどちらで記録されたパスも新しい場所を指すようになる。"""
    old_a = _write(user_data / "addon_files" / VOICE_TTS / "personas" / "a" / "ref_audio.wav")
    old_b = _write(user_data / "addon_files" / VOICE_TTS / "personas" / "b" / "ref_audio.wav")
    db = tmp_path / "saiverse.db"
    _make_db(db, [
        (VOICE_TTS, "a", {
            "ref_audio": str(old_a).replace("/", "\\"),
            "ref_text": "こんにちは",
        }),
        (VOICE_TTS, "b", {
            "ref_audio": str(old_b).replace("\\", "/"),
            # ネストした値と、パスでない文字列は書き換え対象の判定だけ通る
            "extra": {"files": [str(old_b)], "note": "addon_files/のことではない"},
        }),
    ])

    migrate_addon_data_dirs(addon_ids=ENABLED_ADDONS_FOR_STARTUP)
    stats = rewrite_addon_file_paths_in_db(str(db))

    new_a = user_data / "addon_data" / VOICE_TTS / "inputs" / "personas" / "a" / "ref_audio.wav"
    new_b = user_data / "addon_data" / VOICE_TTS / "inputs" / "personas" / "b" / "ref_audio.wav"
    assert new_a.is_file() and new_b.is_file()
    assert not (user_data / "addon_files").exists()

    params = _persona_params(db)
    assert params["a"]["ref_audio"] == str(new_a)
    assert params["a"]["ref_text"] == "こんにちは"
    assert params["b"]["ref_audio"] == str(new_b)
    assert params["b"]["extra"]["files"] == [str(new_b)]
    assert params["b"]["extra"]["note"] == "addon_files/のことではない"
    assert Path(params["a"]["ref_audio"]).is_file()
    assert stats == {"rows": 2, "rewritten": 3, "kept": 0}

    # 二度目の起動では何も変わらない
    migrate_addon_data_dirs(addon_ids=ENABLED_ADDONS_FOR_STARTUP)
    stats2 = rewrite_addon_file_paths_in_db(str(db))
    assert stats2 == {"rows": 0, "rewritten": 0, "kept": 0}
    assert _persona_params(db) == params
    assert new_a.is_file() and new_b.is_file()


def test_ほかのアドオンのアップロードも_起動時の対象外でも取り残さない(user_data, tmp_path) -> None:
    other = "saiverse-other-addon"
    assert other not in ENABLED_ADDONS_FOR_STARTUP
    old = _write(user_data / "addon_files" / other / "global" / "icon.png")
    db = tmp_path / "saiverse.db"
    _make_db(db, [(other, "p", {"icon": str(old)})])

    results = migrate_addon_data_dirs(addon_ids=ENABLED_ADDONS_FOR_STARTUP)
    rewrite_addon_file_paths_in_db(str(db))

    new = user_data / "addon_data" / other / "inputs" / "global" / "icon.png"
    assert new.is_file()
    assert any(status == "moved" for status in results.values())
    assert _persona_params(db)["p"]["icon"] == str(new)


def test_フォルダの移行が衝突で止まったら_記録されたパスを書き換えない(user_data, tmp_path) -> None:
    """ファイルが旧い場所にしか無いのに新しいパスへ書き換えると、参照音声を見失う。"""
    old = _write(user_data / "addon_files" / VOICE_TTS / "personas" / "a" / "ref_audio.wav")
    # inputs/ に既に別のものがあると、フォルダの移行は上書きせずにスキップする
    _write(user_data / "addon_data" / VOICE_TTS / "inputs" / "something_else.txt")
    db = tmp_path / "saiverse.db"
    _make_db(db, [(VOICE_TTS, "a", {"ref_audio": str(old)})])

    migrate_addon_data_dirs(addon_ids=ENABLED_ADDONS_FOR_STARTUP)
    stats = rewrite_addon_file_paths_in_db(str(db))

    assert old.is_file()
    assert _persona_params(db)["a"]["ref_audio"] == str(old)
    assert stats == {"rows": 0, "rewritten": 0, "kept": 1}


def test_移行先に実在しないファイルを指すパスも新しい規則で書き換える(user_data, tmp_path) -> None:
    """旧い場所にも無いファイル (記録だけ残っている) は、書き換えても失うものが無い。"""
    missing = user_data / "addon_files" / VOICE_TTS / "personas" / "a" / "ref_audio.wav"
    db = tmp_path / "saiverse.db"
    _make_db(db, [(VOICE_TTS, "a", {"ref_audio": str(missing)})])

    rewrite_addon_file_paths_in_db(str(db))

    expected = user_data / "addon_data" / VOICE_TTS / "inputs" / "personas" / "a" / "ref_audio.wav"
    assert _persona_params(db)["a"]["ref_audio"] == str(expected)


def test_フォルダそのものを指す記録も_inputs_へ書き換える(user_data, tmp_path) -> None:
    """<addon_files>/<id> 止まりの値も、フォルダ側の移行と揃えて inputs/ を指すようにする。"""
    folder = user_data / "addon_files" / VOICE_TTS
    db = tmp_path / "saiverse.db"
    _make_db(db, [(VOICE_TTS, "a", {"dir": str(folder)})])

    rewrite_addon_file_paths_in_db(str(db))

    expected = user_data / "addon_data" / VOICE_TTS / "inputs"
    assert _persona_params(db)["a"]["dir"] == str(expected)


def test_別の場所や相対パスは書き換えない(user_data, tmp_path) -> None:
    elsewhere = tmp_path / "other_home" / "user_data" / "addon_files" / VOICE_TTS / "x.wav"
    relative = "user_data/addon_files/saiverse-voice-tts/x.wav"
    db = tmp_path / "saiverse.db"
    _make_db(db, [(VOICE_TTS, "a", {"p1": str(elsewhere), "p2": relative, "n": 3})])

    stats = rewrite_addon_file_paths_in_db(str(db))

    assert _persona_params(db)["a"] == {"p1": str(elsewhere), "p2": relative, "n": 3}
    assert stats["rows"] == 0


def test_DB_ファイルが無ければ作らずに何もしない(user_data, tmp_path) -> None:
    db = tmp_path / "missing.db"
    assert rewrite_addon_file_paths_in_db(str(db)) == {"rows": 0, "rewritten": 0, "kept": 0}
    assert not db.exists()


def test_アップロード先は全アドオン共通で_addon_data_の_inputs(user_data) -> None:
    from api.routes.addon import _resolve_file_dir

    assert _resolve_file_dir(VOICE_TTS, "a", "ref_audio") == (
        user_data / "addon_data" / VOICE_TTS / "inputs" / "personas" / "a"
    )
    assert _resolve_file_dir("saiverse-other-addon", None, "icon") == (
        user_data / "addon_data" / "saiverse-other-addon" / "inputs" / "global"
    )
