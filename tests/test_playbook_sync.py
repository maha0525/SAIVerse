"""playbook_sync (起動時プレイブック自動同期) の回帰テスト。

対象は「提供元の層の移動」(例: builtin → addon 切り出し) の扱い:
内容が同一 (ハッシュ一致) のままファイルの置き場所だけが変わった場合、
source_file を新しい提供元へ付け替えないと、直後の orphan prune が
「提供元あり」の Playbook を孤児と誤認して削除し、PlaybookPermission も
道連れになる (permission は再 import では戻らない)。

2026-08-01 ComfyUI アドオン切り出しの Codex レビュー指摘で発覚。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Playbook, PlaybookPermission
from saiverse import playbook_sync
from saiverse.playbook_sync import _canonical_hash, sync_playbooks_from_files


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _playbook_data(name):
    return {
        "name": name,
        "description": "d",
        "start_node": "n1",
        "nodes": [{"id": "n1", "type": "memorize", "action": "x", "next": None}],
    }


def _insert_playbook(session_factory, name, source_file, source_hash):
    db = session_factory()
    try:
        data = _playbook_data(name)
        db.add(Playbook(
            name=name,
            scope="public",
            schema_json=json.dumps({"name": name}),
            nodes_json=json.dumps(data),
            source_file=source_file,
            source_hash=source_hash,
        ))
        db.add(PlaybookPermission(CITYID=1, playbook_name=name))
        db.commit()
    finally:
        db.close()


def test_layer_move_repoints_source_file_and_survives_prune(
    session_factory, tmp_path, monkeypatch
):
    """内容同一のままの層移動で、Playbook と Permission が prune されないこと。"""
    name = "pb_moved"
    data = _playbook_data(name)
    digest = _canonical_hash(data)

    # 新しい提供元 (addon 側) は実在するファイル
    new_src = tmp_path / f"{name}_playbook.json"
    new_src.write_text(json.dumps(data), encoding="utf-8")

    # DB の記録は消滅済みの旧 builtin パス + 同一ハッシュ
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_moved_playbook.json",
        source_hash=digest,
    )

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {
        name: {
            "path": new_src,
            "data": data,
            "source_rel": str(new_src),
            "hash": digest,
        },
    })

    counts = sync_playbooks_from_files(session_factory)

    assert counts["pruned"] == 0
    assert counts["errors"] == 0
    db = session_factory()
    try:
        pb = db.query(Playbook).filter(Playbook.name == name).first()
        assert pb is not None, "層移動した Playbook が prune で消えてはならない"
        assert pb.source_file == str(new_src)
        perm = (
            db.query(PlaybookPermission)
            .filter(PlaybookPermission.playbook_name == name)
            .first()
        )
        assert perm is not None, "PlaybookPermission が道連れ削除されてはならない"
    finally:
        db.close()


def test_genuine_orphan_is_still_pruned(session_factory, monkeypatch):
    """どの層にも提供元が無い Playbook は従来どおり prune されること。"""
    name = "pb_gone"
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_gone_playbook.json",
        source_hash="deadbeefdeadbeef",
    )

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {})

    counts = sync_playbooks_from_files(session_factory)

    assert counts["pruned"] == 1
    db = session_factory()
    try:
        assert db.query(Playbook).filter(Playbook.name == name).first() is None
    finally:
        db.close()


def test_hash_change_updates_content(session_factory, tmp_path, monkeypatch):
    """ハッシュが変わった場合は従来どおり内容ごと更新されること (退行確認)。"""
    name = "pb_changed"
    old_data = _playbook_data(name)
    _insert_playbook(
        session_factory, name,
        source_file="builtin_data/playbooks/public/pb_changed_playbook.json",
        source_hash=_canonical_hash(old_data),
    )

    new_data = _playbook_data(name)
    new_data["description"] = "changed"
    new_src = tmp_path / f"{name}_playbook.json"
    new_src.write_text(json.dumps(new_data), encoding="utf-8")

    monkeypatch.setattr(playbook_sync, "_collect_file_playbooks", lambda: {
        name: {
            "path": new_src,
            "data": new_data,
            "source_rel": str(new_src),
            "hash": _canonical_hash(new_data),
        },
    })

    counts = sync_playbooks_from_files(session_factory)

    assert counts["updated"] == 1
    db = session_factory()
    try:
        pb = db.query(Playbook).filter(Playbook.name == name).first()
        assert pb.description == "changed"
        assert pb.source_file == str(new_src)
    finally:
        db.close()
