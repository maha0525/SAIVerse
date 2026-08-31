"""Tests for the v0.3.0.dev6 playbook wholesale replacement upgrade handler.

v0.2 時代の Playbook 取り込みは source_file / source_hash を記録しなかったため、
v0.3 の起動時同期 (saiverse.playbook_sync) がそれらを「ユーザー作 = 保護対象」と
誤認し、退役済み Playbook が DB に永久残留する。2026-08-29 まはー裁定により、
選別せずバックアップだけ取って全削除 → 直後の起動時同期で現行セットを再取り込み
する (main.py は run_startup_upgrade → sync_playbooks_from_files の順で呼ぶ)。

流儀は tests/test_upgrade_handlers_retired_autonomy.py に従う。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base, City, Playbook, PlaybookPermission, User
from saiverse.upgrade_handlers import replace_all_playbooks


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    sess.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
    sess.commit()
    sess.add(City(CITYID=1, CITY_SLUG="test_city", USERID=1, UI_PORT=3000, API_PORT=8000))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


@pytest.fixture
def city(session: Session) -> City:
    return session.query(City).first()


def _make_playbook(
    session: Session,
    name: str,
    *,
    source_file: str | None = None,
    source_hash: str | None = None,
) -> Playbook:
    pb = Playbook(
        name=name,
        scope="public",
        schema_json=json.dumps({"name": name}),
        nodes_json=json.dumps({"name": name, "nodes": []}),
        source_file=source_file,
        source_hash=source_hash,
    )
    session.add(pb)
    session.commit()
    return pb


def _make_permission(session: Session, name: str) -> PlaybookPermission:
    perm = PlaybookPermission(
        CITYID=1, playbook_name=name, permission_level="ask_every_time"
    )
    session.add(perm)
    session.commit()
    return perm


def _backup_files(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("playbooks_pre_v030_*.json"))


# ---------------------------------------------------------------------------
# 置き換え本体
# ---------------------------------------------------------------------------


def test_wholesale_replacement(session: Session, tmp_path: Path) -> None:
    """退役名 (source 情報 NULL) も現行名も選別せず全削除し、permission は
    退役名だけ消えて現行名は残り、バックアップ JSON が全行と一致する。"""
    retired = _make_playbook(session, "meta_user")  # v0.2 取り込み様式 (source NULL)
    current = _make_playbook(
        session,
        "track_user_conversation",
        source_file="builtin_data/playbooks/public/track_user_conversation.json",
        source_hash="abc123",
    )
    _make_permission(session, "meta_user")
    _make_permission(session, "track_user_conversation")
    expected = {
        retired.name: retired.nodes_json,
        current.name: current.nodes_json,
    }

    replace_all_playbooks(
        session,
        current_playbook_names={"track_user_conversation"},
        backup_dir=tmp_path,
    )
    session.commit()

    # playbooks は空
    assert session.query(Playbook).count() == 0
    # permission は現行名だけ残る
    remaining = [p.playbook_name for p in session.query(PlaybookPermission).all()]
    assert remaining == ["track_user_conversation"]
    # バックアップは全行・全カラムを保持
    files = _backup_files(tmp_path)
    assert len(files) == 1
    backup = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(backup) == 2
    assert {row["name"]: row["nodes_json"] for row in backup} == expected
    for row in backup:
        assert "source_file" in row and "source_hash" in row and "scope" in row


def test_backup_failure_is_fail_closed(session: Session, tmp_path: Path) -> None:
    """バックアップ書き込みが失敗したら例外で止まり、playbooks は消えない。"""
    _make_playbook(session, "meta_user")
    _make_permission(session, "meta_user")
    # 書き込み先の親をファイルで塞ぎ、ディレクトリ作成を失敗させる
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        replace_all_playbooks(
            session,
            current_playbook_names={"track_user_conversation"},
            backup_dir=blocker / "sub",
        )
    session.rollback()

    assert session.query(Playbook).count() == 1
    assert session.query(PlaybookPermission).count() == 1


def test_empty_table_is_no_op(session: Session, tmp_path: Path) -> None:
    """0 行なら no-op — バックアップも作らない。"""
    replace_all_playbooks(
        session,
        current_playbook_names={"track_user_conversation"},
        backup_dir=tmp_path,
    )
    assert _backup_files(tmp_path) == []


def test_no_file_playbooks_aborts_before_backup(
    session: Session, tmp_path: Path
) -> None:
    """現行ファイル Playbook が 0 件なら削除に進まない — 直後の起動時同期が
    何も再取り込みできず、Playbook ゼロの世界で起動が完了してしまうため。"""
    _make_playbook(session, "meta_user")

    with pytest.raises(RuntimeError):
        replace_all_playbooks(
            session, current_playbook_names=set(), backup_dir=tmp_path
        )
    session.rollback()

    assert session.query(Playbook).count() == 1
    assert _backup_files(tmp_path) == []


def test_broken_playbook_file_aborts_collection(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """収集経路 (current_playbook_names 注入なし) を実際に通し、壊れた JSON が
    1 件でもあれば RuntimeError で止まり、playbooks は消えないこと。
    壊れた分が欠けた名前集合で削除に進むと、その Playbook と permission が
    欠けたまま確定するため。"""
    _make_playbook(session, "meta_user")
    _make_permission(session, "meta_user")

    playbooks_dir = tmp_path / "playbooks"
    public_dir = playbooks_dir / "public"
    public_dir.mkdir(parents=True)
    (public_dir / "good.json").write_text(
        json.dumps({"name": "track_user_conversation", "nodes": []}),
        encoding="utf-8",
    )
    (public_dir / "broken.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(
        "saiverse.data_paths.iter_project_subdirs",
        lambda subdir: iter([playbooks_dir]),
    )

    backup_dir = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="broken.json"):
        replace_all_playbooks(session, backup_dir=backup_dir)
    session.rollback()

    assert session.query(Playbook).count() == 1
    assert session.query(PlaybookPermission).count() == 1
    assert not backup_dir.exists()


def test_empty_table_still_prunes_orphan_permissions(
    session: Session, tmp_path: Path
) -> None:
    """playbooks 0 行 + 退役名の permission 行あり → permission だけ掃除される。
    バックアップは作らない (playbooks の削除が無いため)。"""
    _make_permission(session, "meta_user")
    _make_permission(session, "track_user_conversation")

    replace_all_playbooks(
        session,
        current_playbook_names={"track_user_conversation"},
        backup_dir=tmp_path,
    )
    session.commit()

    remaining = [p.playbook_name for p in session.query(PlaybookPermission).all()]
    assert remaining == ["track_user_conversation"]
    assert _backup_files(tmp_path) == []


def test_fully_empty_tables_skip_file_collection(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """playbooks 0 行 + permission 0 行 → 完全 no-op。ファイル収集も走らない
    (新規インストールで収集失敗が起動を止めないこと)。"""
    calls: list[str] = []

    def _collect_spy(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        calls.append("called")
        return {}

    monkeypatch.setattr(
        "saiverse.playbook_sync._collect_file_playbooks", _collect_spy
    )

    replace_all_playbooks(session, backup_dir=tmp_path)

    assert calls == []
    assert _backup_files(tmp_path) == []


# ---------------------------------------------------------------------------
# 登録・遷移・一度きり実行の門
# ---------------------------------------------------------------------------


def test_handlers_registered_with_dev6_edge() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    city_h = next(
        h for h in HANDLERS if h.name == "v0_3_0_dev6_playbook_wholesale_replacement"
    )
    assert city_h.scope == "city"
    assert city_h.from_version == "0.3.0.dev5"
    assert city_h.to_version == "0.3.0.dev6"

    ai_h = next(h for h in HANDLERS if h.name == "ai_noop_v0_3_0_dev6")
    assert ai_h.scope == "ai"
    assert ai_h.from_version == "0.3.0.dev5"
    assert ai_h.to_version == "0.3.0.dev6"


def test_v02_chain_reaches_dev6_for_both_scopes() -> None:
    """v0.2 DB (LAST_KNOWN_VERSION NULL = 0.0.0) からの遷移で、両スコープの
    ハンドラ連鎖が dev6 まで途切れず、city 側に置き換えハンドラが含まれる。"""
    from saiverse import upgrade
    from saiverse.upgrade import parse_version, select_handlers
    from saiverse.upgrade_handlers import HANDLERS as MODULE_HANDLERS

    original = list(upgrade.HANDLERS)
    upgrade.HANDLERS.clear()
    upgrade.HANDLERS.extend(MODULE_HANDLERS)
    try:
        current = parse_version("0.0.0")
        target = parse_version("0.3.0.dev6")
        city_selected = {h.name for h in select_handlers("city", current, target)}
        ai_selected = {h.name for h in select_handlers("ai", current, target)}
    finally:
        upgrade.HANDLERS.clear()
        upgrade.HANDLERS.extend(original)

    assert "v0_3_0_dev6_playbook_wholesale_replacement" in city_selected
    assert "ai_noop_v0_3_0_dev6" in ai_selected


def test_current_version_is_dev6_or_later() -> None:
    """VERSION ファイルが 0.3.0.dev6 以上であること — dev6 ハンドラは
    コードバージョンが dev6 に達していないと永遠に走らない。"""
    from saiverse import __version__
    from saiverse.upgrade import parse_version

    assert parse_version(__version__) >= parse_version("0.3.0.dev6")


def test_version_gate_runs_once(
    session: Session, city: City, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一度きり実行の門: dev5 → dev6 の遷移で 1 回だけ走り、LAST_KNOWN_VERSION が
    dev6 に刻まれた後の再実行では Playbook に触らない。"""
    from saiverse import upgrade
    from saiverse.upgrade import _run_handlers_for_entity, parse_version
    from saiverse.upgrade_handlers import HANDLERS as MODULE_HANDLERS

    # ハンドラ既定経路 (引数注入なし) を通すため、収集とバックアップ先を差し替える
    monkeypatch.setattr(
        "saiverse.playbook_sync._collect_file_playbooks",
        lambda collect_errors=None: {"track_user_conversation": {}},
    )
    monkeypatch.setattr(
        "saiverse.data_paths.get_saiverse_home", lambda: tmp_path
    )

    city.LAST_KNOWN_VERSION = "0.3.0.dev5"
    session.commit()
    _make_playbook(session, "meta_user")

    original = list(upgrade.HANDLERS)
    upgrade.HANDLERS.clear()
    upgrade.HANDLERS.extend(MODULE_HANDLERS)
    try:
        target = parse_version("0.3.0.dev6")
        ok = _run_handlers_for_entity(
            session, scope="city", entity=city, entity_id="1", target=target
        )
        assert ok is True
        assert city.LAST_KNOWN_VERSION == "0.3.0.dev6"
        assert session.query(Playbook).count() == 0
        assert len(_backup_files(tmp_path / "backups" / "playbooks")) == 1

        # 2 回目: バージョンが刻まれているので no-op — 再投入した行は消えない
        _make_playbook(session, "meta_user")
        ok = _run_handlers_for_entity(
            session, scope="city", entity=city, entity_id="1", target=target
        )
        assert ok is True
        assert session.query(Playbook).count() == 1
        assert len(_backup_files(tmp_path / "backups" / "playbooks")) == 1
    finally:
        upgrade.HANDLERS.clear()
        upgrade.HANDLERS.extend(original)
