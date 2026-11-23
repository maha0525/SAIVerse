"""ペルソナの VRM (docs/intent/screen_avatar.md 設計 4) の契約テスト。

固定する契約:

- ``POST /api/people/{id}/avatar-vrm`` は ``.vrm`` で終わり先頭が ``glTF`` のファイルだけを
  受け取り、``<SAIVERSE_HOME>/personas/<id>/avatar/`` に置き、``AI.AVATAR_VRM_PATH`` に
  SAIVERSE_HOME からの相対パスを書く。入れ直したら古いファイルは消える。断ったときと
  上限を超えたときは、一時ファイルを残さない。
- ``GET`` は同じ中身を返し、DB のパスが avatar フォルダの外を指すときは返さない。
- ``DELETE`` はファイルと列を消し、二度目も成功する。
- ``PATCH /config`` の ``avatar_3d_enabled`` は送ったときだけ効く。
- ``/api/info/details`` の ``avatar_vrm_url`` は「VRM あり かつ 3D が False でない」ときだけ入る。
- ペルソナを消すと avatar フォルダが消え、ペルソナまるごとパックには VRM が入らない。
- 列の無い古い DB には、追加系のマイグレーションで二つの列が足される。
- Godot のアドオンから呼ぶ ``current_vrm_file`` は、avatar フォルダの中の実際にあるファイル
  だけを返す。``adopt_local_vrm`` は、VRM の無いペルソナにだけ、同じ PC の上の VRM を中身を
  変えずに写し、写せないときは理由の status を返して何も変えない (source も変えない)。
  ``post_vrm_adoption_failed_alert`` は、ペルソナごとの key で警告の知らせを出し、例外を上げない。

``SAIVERSE_HOME`` は一時フォルダ、DB は一時の SQLite にする (本番のデータに触れない)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_manager
from database.models import AI, Base, Building, City, User
from saiverse import persona_avatar_vrm

PERSONA_ID = "aoi"
OTHER_ID = "kai"
BUILDING_ID = "cafe"
VRM_BYTES = b"glTF" + b"\x02\x00\x00\x00" + b"x" * 64


class _World:
    def __init__(self, tmp_path: Path, monkeypatch):
        from manager.admin import AdminService

        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("SAIVERSE_HOME", str(self.home))

        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(Building(CITYID=1, BUILDINGID=BUILDING_ID, BUILDINGNAME="カフェ"))
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="アオイ"))
            db.add(AI(AIID=OTHER_ID, HOME_CITYID=1, AINAME="カイ"))
            db.commit()
        finally:
            db.close()

        admin = AdminService.__new__(AdminService)
        admin.SessionLocal = self.Session
        admin.building_map = {}
        admin.state = SimpleNamespace(model=None, city_id=1)
        admin.manager = SimpleNamespace(
            model=None, model_parameter_overrides={}, ensure_autonomy_for=lambda ai_id: None,
        )
        admin.personas = {}
        admin.persona_map = {}
        admin.id_to_name_map = {}
        admin.avatar_map = {}
        admin.occupants = {}
        admin.saiverse_home = self.home
        admin._set_persona_avatar = lambda ai_id, value: None
        self.admin = admin

        personas = {
            pid: SimpleNamespace(persona_id=pid, persona_name=name, avatar_image=None, autonomy_enabled=True)
            for pid, name in ((PERSONA_ID, "アオイ"), (OTHER_ID, "カイ"))
        }
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            db_path=tmp_path / "absent.db",
            get_ai_details=admin.get_ai_details,
            update_ai=admin.update_ai,
            personas=personas,
            occupancy_manager=SimpleNamespace(occupants={BUILDING_ID: [PERSONA_ID, OTHER_ID]}),
            building_map={BUILDING_ID: SimpleNamespace(name="カフェ", description="")},
            state=SimpleNamespace(user_id=1),
            item_registry={},
            items_by_building={},
        )

        from api.routes import info as info_route
        from api.routes import people as people_route

        app = FastAPI()
        app.include_router(people_route.router, prefix="/api/people")
        app.include_router(info_route.router, prefix="/api/info")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    @property
    def avatar_dir(self) -> Path:
        return self.home / "personas" / PERSONA_ID / "avatar"

    def stored(self, persona_id: str = PERSONA_ID):
        db = self.Session()
        try:
            return db.query(AI.AVATAR_VRM_PATH, AI.AVATAR_3D_ENABLED).filter(AI.AIID == persona_id).one()
        finally:
            db.close()

    def set_columns(self, persona_id: str = PERSONA_ID, **values):
        db = self.Session()
        try:
            ai = db.query(AI).filter(AI.AIID == persona_id).one()
            for key, value in values.items():
                setattr(ai, key, value)
            db.commit()
        finally:
            db.close()

    def upload(self, name: str = "Aoi.vrm", data: bytes = VRM_BYTES, persona_id: str = PERSONA_ID):
        return self.client.post(
            f"/api/people/{persona_id}/avatar-vrm",
            files={"file": (name, data, "application/octet-stream")},
        )

    def details(self):
        with patch("database.session.SessionLocal", self.Session):
            response = self.client.get(f"/api/info/details?building_id={BUILDING_ID}")
        assert response.status_code == 200, response.text
        return {o["id"]: o for o in response.json()["occupants"]}

    def leftover_temp_files(self):
        if not self.avatar_dir.is_dir():
            return []
        return [p.name for p in self.avatar_dir.iterdir() if p.name.startswith(persona_avatar_vrm.UPLOAD_TEMP_PREFIX)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch)
    yield w
    w.client.close()
    w.engine.dispose()


def test_upload_returns_info_and_get_returns_the_same_bytes(world):
    response = world.upload()
    assert response.status_code == 200, response.text
    info = response.json()
    path = world.avatar_dir / "Aoi.vrm"
    assert path.read_bytes() == VRM_BYTES
    assert info["filename"] == "Aoi.vrm"
    assert info["size_bytes"] == len(VRM_BYTES)
    assert info["updated_at"] == int(path.stat().st_mtime)
    assert info["url"] == f"/api/people/{PERSONA_ID}/avatar-vrm?v={path.stat().st_mtime_ns}-{len(VRM_BYTES)}"
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/Aoi.vrm"

    got = world.client.get(f"/api/people/{PERSONA_ID}/avatar-vrm")
    assert got.status_code == 200
    assert got.content == VRM_BYTES
    assert got.headers["content-type"].startswith("model/gltf-binary")

    config = world.client.get(f"/api/people/{PERSONA_ID}/config").json()
    assert config["avatar_vrm"] == info
    assert config["avatar_3d_enabled"] is True


def test_reupload_removes_the_old_file(world):
    assert world.upload("first.vrm").status_code == 200
    second = VRM_BYTES + b"second"
    response = world.upload("second.vrm", second)
    assert response.status_code == 200, response.text
    assert not (world.avatar_dir / "first.vrm").exists()
    assert (world.avatar_dir / "second.vrm").read_bytes() == second
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/second.vrm"
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["second.vrm"]


def test_reupload_with_the_same_name_keeps_the_new_file(world):
    assert world.upload("same.vrm").status_code == 200
    second = VRM_BYTES + b"second"
    assert world.upload("same.vrm", second).status_code == 200
    assert (world.avatar_dir / "same.vrm").read_bytes() == second
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["same.vrm"]


def test_url_changes_when_the_file_is_replaced_within_the_same_second(world):
    """同じ一秒の中で入れ替えても、URL が変わる。

    画面は URL が同じなら読み込み済みの VRM を使い続け、読み込めなかった URL は
    読み直さないので、入れ替えたのに URL が同じだと、前の姿のままになる。
    """
    assert world.upload("same.vrm").status_code == 200
    path = world.avatar_dir / "same.vrm"
    second_start = 1_700_000_000 * 1_000_000_000
    os.utime(path, ns=(second_start, second_start + 1_000))
    first = persona_avatar_vrm.vrm_info(PERSONA_ID, path)

    assert world.upload("same.vrm", VRM_BYTES + b"second").status_code == 200
    os.utime(path, ns=(second_start, second_start + 2_000))
    second = persona_avatar_vrm.vrm_info(PERSONA_ID, path)

    assert first["updated_at"] == second["updated_at"]
    assert first["url"] != second["url"]

    # 大きさが同じでも、更新時刻が一秒より細かく違えば URL が変わる
    path.write_bytes(VRM_BYTES + b"SECOND")
    os.utime(path, ns=(second_start, second_start + 3_000))
    third = persona_avatar_vrm.vrm_info(PERSONA_ID, path)
    assert third["size_bytes"] == second["size_bytes"]
    assert third["url"] != second["url"]


def _failing_db_write(*args, **kwargs):
    raise RuntimeError("the database could not be written")


def test_same_name_reupload_never_reports_failure_after_replacing_the_file(world, monkeypatch):
    """同じ名前で入れ替えるとき、失敗を返したのに中身だけ替わっている、を起こさない。

    前の VRM と同じ名前なら DB の値は変わらないので、DB には書かない。ファイルを
    置き換えた後に失敗しうる処理が残っていないこと (置き換えが最後の一手) を固定する。
    """
    assert world.upload("same.vrm").status_code == 200
    stored_before = world.stored().AVATAR_VRM_PATH
    monkeypatch.setattr(persona_avatar_vrm, "write_stored_vrm_path", _failing_db_write)

    second = VRM_BYTES + b"second"
    response = world.upload("same.vrm", second)
    assert response.status_code == 200, response.text
    assert (world.avatar_dir / "same.vrm").read_bytes() == second
    assert world.stored().AVATAR_VRM_PATH == stored_before
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["same.vrm"]


def test_same_name_reupload_keeps_the_old_file_when_the_replace_fails(world, monkeypatch):
    """同じ名前で入れ替えるとき、置き換えに失敗したら、前の VRM がそのまま残る。"""
    assert world.upload("same.vrm").status_code == 200
    stored_before = world.stored().AVATAR_VRM_PATH

    def failing_replace(src, dst):
        raise PermissionError("the file is in use")

    monkeypatch.setattr(persona_avatar_vrm.os, "replace", failing_replace)
    response = world.upload("same.vrm", VRM_BYTES + b"second")
    assert response.status_code == 500
    assert "in use" in response.json()["detail"]
    assert (world.avatar_dir / "same.vrm").read_bytes() == VRM_BYTES
    assert world.stored().AVATAR_VRM_PATH == stored_before
    assert world.leftover_temp_files() == []


def test_different_name_reupload_keeps_the_old_file_when_the_db_write_fails(world, monkeypatch):
    """別の名前で入れ替えるとき、DB に書けなかったら、前の VRM と DB の値がそのまま残る。"""
    assert world.upload("first.vrm").status_code == 200
    stored_before = world.stored().AVATAR_VRM_PATH
    monkeypatch.setattr(persona_avatar_vrm, "write_stored_vrm_path", _failing_db_write)

    with pytest.raises(RuntimeError):
        world.upload("second.vrm", VRM_BYTES + b"second")
    assert (world.avatar_dir / "first.vrm").read_bytes() == VRM_BYTES
    assert world.stored().AVATAR_VRM_PATH == stored_before
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["first.vrm"]


def test_unsafe_filename_is_made_safe(world):
    response = world.upload("..\\..\\evil/../x.vrm")
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "x.vrm"
    assert (world.avatar_dir / "x.vrm").is_file()
    assert persona_avatar_vrm.safe_vrm_filename("../.vrm") == persona_avatar_vrm.DEFAULT_VRM_FILENAME
    assert persona_avatar_vrm.safe_vrm_filename("..vrm") == persona_avatar_vrm.DEFAULT_VRM_FILENAME
    assert persona_avatar_vrm.safe_vrm_filename(".hidden.VRM") == "hidden.VRM"
    assert persona_avatar_vrm.safe_vrm_filename("a:b?.vrm") == "a_b_.vrm"


@pytest.mark.parametrize(
    "name, data",
    [
        ("avatar.glb", VRM_BYTES),  # .vrm でない
        ("avatar.vrm", b"PK\x03\x04 not a glb"),  # 先頭が glTF でない
        ("avatar.vrm", b"gl"),  # 4 バイトに足りない
    ],
)
def test_rejected_files_leave_no_temp_files(world, name, data):
    assert world.upload("keep.vrm").status_code == 200
    response = world.upload(name, data)
    assert response.status_code == 400
    assert response.json()["detail"]
    assert world.leftover_temp_files() == []
    # 前の VRM はそのまま
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/keep.vrm"
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["keep.vrm"]


def test_oversized_upload_is_refused_with_413(world, monkeypatch):
    from api.routes.people import avatar_vrm as route

    monkeypatch.setattr(route, "MAX_VRM_BYTES", 16)
    response = world.upload("big.vrm", VRM_BYTES)
    assert response.status_code == 413
    assert world.leftover_temp_files() == []
    assert not (world.avatar_dir / "big.vrm").exists()
    assert world.stored().AVATAR_VRM_PATH is None


def test_upload_streams_in_chunks_instead_of_reading_everything(world, monkeypatch):
    """全体をメモリに読まず、決まった大きさずつ読む (write_upload_bounded を通る)。"""
    from starlette.datastructures import UploadFile

    sizes = []
    original = UploadFile.read

    async def recording_read(self, size=-1):
        sizes.append(size)
        return await original(self, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    assert world.upload().status_code == 200
    assert sizes and all(size > 0 for size in sizes)


def test_delete_removes_the_file_and_succeeds_twice(world):
    assert world.upload().status_code == 200
    first = world.client.delete(f"/api/people/{PERSONA_ID}/avatar-vrm")
    assert first.status_code == 200
    assert not (world.avatar_dir / "Aoi.vrm").exists()
    assert world.stored().AVATAR_VRM_PATH is None
    assert world.client.get(f"/api/people/{PERSONA_ID}/avatar-vrm").status_code == 404
    second = world.client.delete(f"/api/people/{PERSONA_ID}/avatar-vrm")
    assert second.status_code == 200
    assert world.client.get(f"/api/people/{PERSONA_ID}/config").json()["avatar_vrm"] is None


def test_state_changing_routes_go_through_the_browser_origin_guard(world):
    """ローカル起動で main.py が付ける BrowserOriginMiddleware が、新しいルートにも効く。"""
    from api.owner_auth import BrowserOriginMiddleware

    app = FastAPI()
    from api.routes import people as people_route

    app.include_router(people_route.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: world.manager
    app.add_middleware(BrowserOriginMiddleware)
    evil = {"Origin": "https://evil.example"}
    with TestClient(app) as client:
        post = client.post(
            f"/api/people/{PERSONA_ID}/avatar-vrm",
            files={"file": ("a.vrm", VRM_BYTES, "application/octet-stream")},
            headers=evil,
        )
        assert post.status_code == 403
        assert client.delete(f"/api/people/{PERSONA_ID}/avatar-vrm", headers=evil).status_code == 403
        assert client.patch(
            f"/api/people/{PERSONA_ID}/config", json={"avatar_3d_enabled": False}, headers=evil,
        ).status_code == 403
    assert not world.avatar_dir.exists()
    assert world.stored().AVATAR_3D_ENABLED is None


def test_unknown_persona_is_404(world):
    assert world.upload(persona_id="nobody").status_code == 404
    assert world.client.get("/api/people/nobody/avatar-vrm").status_code == 404
    assert world.client.delete("/api/people/nobody/avatar-vrm").status_code == 404


def test_get_refuses_a_path_outside_the_avatar_folder(world):
    other = world.home / "personas" / OTHER_ID / "avatar"
    other.mkdir(parents=True)
    (other / "kai.vrm").write_bytes(VRM_BYTES)
    secret = world.home / "secret.vrm"
    secret.write_bytes(VRM_BYTES)
    for stored in (
        f"personas/{OTHER_ID}/avatar/kai.vrm",
        f"personas/{PERSONA_ID}/avatar/../../{OTHER_ID}/avatar/kai.vrm",
        "secret.vrm",
        str(secret),
        f"personas/{PERSONA_ID}/avatar",
    ):
        world.set_columns(AVATAR_VRM_PATH=stored)
        response = world.client.get(f"/api/people/{PERSONA_ID}/avatar-vrm")
        assert response.status_code == 403, stored
        assert world.details()[PERSONA_ID]["avatar_vrm_url"] is None
        # DELETE は外のファイルを消さない
        assert world.client.delete(f"/api/people/{PERSONA_ID}/avatar-vrm").status_code == 200
        assert (other / "kai.vrm").is_file() and secret.is_file()


def test_patch_avatar_3d_enabled_applies_only_when_sent(world):
    with patch("sea.session_lifecycle.invalidate_cold_sweep_fingerprints"):
        url = f"/api/people/{PERSONA_ID}/config"
        assert world.client.patch(url, json={"avatar_3d_enabled": False}).status_code == 200
        assert world.stored().AVATAR_3D_ENABLED is False
        assert world.client.get(url).json()["avatar_3d_enabled"] is False

        # 送らない保存では変わらない
        assert world.client.patch(url, json={"description": "新しい説明"}).status_code == 200
        assert world.stored().AVATAR_3D_ENABLED is False

        assert world.client.patch(url, json={"avatar_3d_enabled": True}).status_code == 200
        assert world.stored().AVATAR_3D_ENABLED is True


def test_config_reports_null_avatar_3d_enabled_as_true(world):
    assert world.stored().AVATAR_3D_ENABLED is None
    assert world.client.get(f"/api/people/{PERSONA_ID}/config").json()["avatar_3d_enabled"] is True


def test_info_details_avatar_vrm_url_only_when_vrm_and_not_disabled(world, caplog):
    occupants = world.details()
    assert occupants[PERSONA_ID]["avatar_vrm_url"] is None
    assert occupants[OTHER_ID]["avatar_vrm_url"] is None

    info = world.upload().json()
    occupants = world.details()
    assert occupants[PERSONA_ID]["avatar_vrm_url"] == info["url"]
    assert occupants[OTHER_ID]["avatar_vrm_url"] is None

    world.set_columns(AVATAR_3D_ENABLED=False)
    assert world.details()[PERSONA_ID]["avatar_vrm_url"] is None
    world.set_columns(AVATAR_3D_ENABLED=True)
    assert world.details()[PERSONA_ID]["avatar_vrm_url"] == info["url"]

    # DB にパスはあるがファイルが消えている
    (world.avatar_dir / "Aoi.vrm").unlink()
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert world.details()[PERSONA_ID]["avatar_vrm_url"] is None
    assert any("does not exist" in r.getMessage() for r in caplog.records)


def test_deleting_the_persona_removes_the_avatar_folder(world):
    assert world.upload().status_code == 200
    memory_file = world.home / "personas" / PERSONA_ID / "memory.db"
    memory_file.write_bytes(b"keep")
    result = world.admin.delete_ai(PERSONA_ID)
    assert "deleted successfully" in result
    assert not world.avatar_dir.exists()
    assert memory_file.read_bytes() == b"keep"  # 記憶フォルダは消さない


def test_persona_mixin_delete_also_removes_the_avatar_folder(world):
    from manager.persona import PersonaMixin

    assert world.upload().status_code == 200
    mixin = PersonaMixin.__new__(PersonaMixin)
    mixin.SessionLocal = world.Session
    mixin.personas = {}
    mixin.persona_map = {}
    mixin.id_to_name_map = {}
    mixin.avatar_map = {}
    mixin.occupants = {}
    mixin.saiverse_home = world.home
    mixin._is_seeded_entity = lambda ai_id: False
    result = mixin.delete_ai(PERSONA_ID)
    assert "deleted successfully" in result
    assert not world.avatar_dir.exists()


def test_persona_pack_export_leaves_out_the_vrm(world, tmp_path):
    from saiverse_memory.persona_pack import export_persona_pack

    assert world.upload().status_code == 200
    persona_dir = world.home / "personas" / PERSONA_ID
    (persona_dir / "log.json").write_text("[]", encoding="utf-8")

    db_path = tmp_path / "saiverse.db"
    source = world.engine.raw_connection()
    try:
        target = sqlite3.connect(db_path)
        try:
            source.driver_connection.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    out = tmp_path / "pack.zip"
    export_persona_pack(
        PERSONA_ID, saiverse_db_path=db_path, saiverse_home=world.home, output_path=out,
        generator_version="test",
    )
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        profile = json.loads(zf.read("profile/profile.json"))
    assert "extensions/saiverse/persona_dir/log.json" in names
    assert not [n for n in names if n.lower().endswith(".vrm") or "/avatar/" in n]
    assert "AVATAR_VRM_PATH" not in profile["ai"]
    assert "AVATAR_3D_ENABLED" in profile["ai"]


_LEGACY_AI_COLUMNS = ("AVATAR_VRM_PATH", "AVATAR_3D_ENABLED")


def test_additive_migration_adds_the_columns_to_an_old_db(tmp_path):
    from database.migrate import try_additive_migration

    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO user (USERID, PASSWORD, USERNAME, LOGGED_IN) VALUES (1, 'x', 'u', 0)"
        )
        for col in _LEGACY_AI_COLUMNS:
            con.execute(f'ALTER TABLE ai DROP COLUMN "{col}"')
        con.commit()
        cols = {r[1] for r in con.execute("PRAGMA table_info(ai)")}
        assert not cols & set(_LEGACY_AI_COLUMNS)
    finally:
        con.close()

    assert try_additive_migration(str(db_path))

    con = sqlite3.connect(db_path)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(ai)")}
    finally:
        con.close()
    for col in _LEGACY_AI_COLUMNS:
        assert col in info
        assert info[col][3] == 0  # nullable
    assert os.path.getsize(db_path) > 0


# --- Godot のアドオンから呼ぶ関数 (docs/intent/screen_avatar.md の段階 2 の (3)) ---


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_current_vrm_file_returns_the_absolute_path_after_upload(world):
    assert persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID) is None
    assert world.upload().status_code == 200
    path = persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID)
    assert path is not None and path.is_absolute()
    assert path == (world.avatar_dir / "Aoi.vrm").resolve()
    assert path.read_bytes() == VRM_BYTES


def test_current_vrm_file_is_none_after_delete(world):
    assert world.upload().status_code == 200
    assert world.client.delete(f"/api/people/{PERSONA_ID}/avatar-vrm").status_code == 200
    assert persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID) is None


def test_current_vrm_file_is_none_when_the_file_was_removed_outside(world):
    assert world.upload().status_code == 200
    (world.avatar_dir / "Aoi.vrm").unlink()
    assert world.stored().AVATAR_VRM_PATH is not None
    assert persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID) is None


def test_current_vrm_file_is_none_for_a_path_outside_the_avatar_folder(world):
    other = world.home / "personas" / OTHER_ID / "avatar"
    other.mkdir(parents=True)
    (other / "kai.vrm").write_bytes(VRM_BYTES)
    secret = world.home / "secret.vrm"
    secret.write_bytes(VRM_BYTES)
    for stored in (
        f"personas/{OTHER_ID}/avatar/kai.vrm",
        f"personas/{PERSONA_ID}/avatar/../../{OTHER_ID}/avatar/kai.vrm",
        "secret.vrm",
        str(secret),
    ):
        world.set_columns(AVATAR_VRM_PATH=stored)
        assert persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID) is None, stored


def test_current_vrm_file_is_none_for_an_unknown_persona(world):
    assert persona_avatar_vrm.current_vrm_file(world.manager, "nobody") is None


def test_current_vrm_file_is_none_when_the_db_cannot_be_read(world):
    def broken_session():
        raise RuntimeError("the database is locked")

    manager = SimpleNamespace(SessionLocal=broken_session)
    assert persona_avatar_vrm.current_vrm_file(manager, PERSONA_ID) is None


@pytest.fixture
def source_dir(tmp_path):
    folder = tmp_path / "godot_addon_settings"
    folder.mkdir()
    return folder


def test_adopt_local_vrm_copies_the_file_unchanged(world, source_dir):
    source = source_dir / "Aoi Godot.vrm"
    data = VRM_BYTES + b"from-godot"
    source.write_bytes(data)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_ADOPTED
    assert result.path == world.avatar_dir / "Aoi Godot.vrm"
    assert _sha256(result.path) == _sha256(source)
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/Aoi Godot.vrm"
    assert source.read_bytes() == data  # source は残り、変わらない
    assert world.leftover_temp_files() == []
    assert persona_avatar_vrm.current_vrm_file(world.manager, PERSONA_ID) == result.path.resolve()
    assert world.client.get(f"/api/people/{PERSONA_ID}/avatar-vrm").content == data


def test_adopt_local_vrm_does_nothing_when_the_persona_already_has_a_vrm(world, source_dir):
    assert world.upload("keep.vrm").status_code == 200
    stored_before = world.stored().AVATAR_VRM_PATH
    source = source_dir / "other.vrm"
    source.write_bytes(VRM_BYTES + b"other")

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_ALREADY_HAS_VRM
    assert result.path is None
    assert world.stored().AVATAR_VRM_PATH == stored_before
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["keep.vrm"]
    assert (world.avatar_dir / "keep.vrm").read_bytes() == VRM_BYTES
    assert source.read_bytes() == VRM_BYTES + b"other"


def test_adopt_local_vrm_adopts_when_the_stored_file_is_gone(world, source_dir):
    """DB に値はあるがファイルが無いときは、VRM が無いものとして写す。"""
    assert world.upload("gone.vrm").status_code == 200
    (world.avatar_dir / "gone.vrm").unlink()
    source = source_dir / "new.vrm"
    source.write_bytes(VRM_BYTES)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_ADOPTED
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/new.vrm"


def _assert_nothing_changed(world):
    assert world.stored().AVATAR_VRM_PATH is None
    assert world.leftover_temp_files() == []
    if world.avatar_dir.is_dir():
        assert list(world.avatar_dir.iterdir()) == []


def _missing(folder: Path) -> Path:
    return folder / "absent.vrm"


def _a_folder(folder: Path) -> Path:
    path = folder / "folder.vrm"
    path.mkdir()
    return path


def _file(name: str, data: bytes):
    def make(folder: Path) -> Path:
        path = folder / name
        path.write_bytes(data)
        return path

    return make


@pytest.mark.parametrize(
    "make_source, status",
    [
        (_missing, persona_avatar_vrm.VRM_ADOPT_SOURCE_MISSING),
        (_a_folder, persona_avatar_vrm.VRM_ADOPT_SOURCE_MISSING),
        (_file("avatar.glb", VRM_BYTES), persona_avatar_vrm.VRM_ADOPT_NOT_VRM),
        (_file("zip.vrm", b"PK\x03\x04 not a glb"), persona_avatar_vrm.VRM_ADOPT_NOT_VRM),
        (_file("short.vrm", b"gl"), persona_avatar_vrm.VRM_ADOPT_NOT_VRM),
    ],
    ids=["missing", "folder", "not_vrm_suffix", "wrong_header", "too_short"],
)
def test_adopt_local_vrm_refuses_unusable_sources(world, source_dir, make_source, status):
    source = make_source(source_dir)
    before = source.read_bytes() if source.is_file() else None

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == status
    assert result.path is None
    _assert_nothing_changed(world)
    if before is not None:
        assert source.read_bytes() == before


def test_adopt_local_vrm_refuses_a_file_over_the_limit(world, source_dir, monkeypatch):
    monkeypatch.setattr(persona_avatar_vrm, "MAX_VRM_BYTES", 16)
    source = source_dir / "big.vrm"
    source.write_bytes(VRM_BYTES)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_TOO_LARGE
    _assert_nothing_changed(world)
    assert source.read_bytes() == VRM_BYTES


def test_adopt_local_vrm_reports_an_unknown_persona(world, source_dir):
    source = source_dir / "a.vrm"
    source.write_bytes(VRM_BYTES)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, "nobody", source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_PERSONA_MISSING
    assert not (world.home / "personas" / "nobody").exists()


def test_adopt_local_vrm_removes_the_placed_file_when_the_db_write_fails(world, source_dir, monkeypatch):
    source = source_dir / "a.vrm"
    source.write_bytes(VRM_BYTES)
    monkeypatch.setattr(persona_avatar_vrm, "write_stored_vrm_path", _failing_db_write)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_FAILED
    assert result.path is None
    assert result.detail
    _assert_nothing_changed(world)
    assert source.read_bytes() == VRM_BYTES


def test_adopt_local_vrm_keeps_a_vrm_uploaded_while_copying(world, source_dir, monkeypatch):
    """写している間にアップロードで VRM が入ったら、そちらを残し、写したものは消す。"""
    source = source_dir / "a.vrm"
    source.write_bytes(VRM_BYTES + b"godot")
    original_copy = persona_avatar_vrm.shutil.copyfile

    def copy_then_upload(src, dst):
        copied = original_copy(src, dst)
        assert world.upload("uploaded.vrm").status_code == 200
        return copied

    monkeypatch.setattr(persona_avatar_vrm.shutil, "copyfile", copy_then_upload)

    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)

    assert result.status == persona_avatar_vrm.VRM_ADOPT_ALREADY_HAS_VRM
    assert world.stored().AVATAR_VRM_PATH == f"personas/{PERSONA_ID}/avatar/uploaded.vrm"
    assert sorted(p.name for p in world.avatar_dir.iterdir()) == ["uploaded.vrm"]
    assert (world.avatar_dir / "uploaded.vrm").read_bytes() == VRM_BYTES


def test_adopt_local_vrm_reports_failed_instead_of_raising(world, source_dir, monkeypatch):
    source = source_dir / "a.vrm"
    source.write_bytes(VRM_BYTES)

    def broken_copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(persona_avatar_vrm.shutil, "copyfile", broken_copy)
    result = persona_avatar_vrm.adopt_local_vrm(world.manager, PERSONA_ID, source)
    assert result.status == persona_avatar_vrm.VRM_ADOPT_FAILED
    assert "disk full" in result.detail
    _assert_nothing_changed(world)


@pytest.mark.parametrize(
    "status, message_key",
    [
        (persona_avatar_vrm.VRM_ADOPT_SOURCE_MISSING, "api.runtimeAlerts.vrmAdoptionFailed.sourceMissing"),
        (persona_avatar_vrm.VRM_ADOPT_NOT_VRM, "api.runtimeAlerts.vrmAdoptionFailed.notVrm"),
        (persona_avatar_vrm.VRM_ADOPT_TOO_LARGE, "api.runtimeAlerts.vrmAdoptionFailed.tooLarge"),
        (persona_avatar_vrm.VRM_ADOPT_FAILED, "api.runtimeAlerts.vrmAdoptionFailed.failed"),
        ("unexpected", "api.runtimeAlerts.vrmAdoptionFailed.failed"),
    ],
)
def test_post_vrm_adoption_failed_alert_posts_a_warning(monkeypatch, status, message_key):
    from saiverse import runtime_alerts

    calls = []

    def recording_post(key, level, title, message, details=None):
        calls.append((key, level, title, message, details))

    monkeypatch.setattr(runtime_alerts, "post_runtime_alert", recording_post)

    persona_avatar_vrm.post_vrm_adoption_failed_alert(PERSONA_ID, "アオイ", "C:/vrm/aoi.vrm", status)

    assert calls == [(
        f"vrm_adoption_failed:{PERSONA_ID}",
        "warning",
        {"$ui": "api.runtimeAlerts.vrmAdoptionFailed.title", "params": {}},
        {"$ui": message_key, "params": {"persona": "アオイ"}},
        {"persona_id": PERSONA_ID, "source": "C:/vrm/aoi.vrm", "status": status},
    )]


def test_post_vrm_adoption_failed_alert_message_keys_exist_in_both_languages():
    messages = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n" / "messages.json"
    table = json.loads(messages.read_text(encoding="utf-8"))
    keys = {"api.runtimeAlerts.vrmAdoptionFailed.title", *persona_avatar_vrm._ADOPTION_FAILED_MESSAGE_KEYS.values()}
    for key in keys:
        assert table[key]["ja"] and table[key]["en"], key


def test_post_vrm_adoption_failed_alert_swallows_errors(monkeypatch):
    from saiverse import runtime_alerts

    def broken(*args, **kwargs):
        raise RuntimeError("alerts are broken")

    monkeypatch.setattr(runtime_alerts, "post_runtime_alert", broken)
    persona_avatar_vrm.post_vrm_adoption_failed_alert(PERSONA_ID, "アオイ", "C:/vrm/aoi.vrm", "not_vrm")


@pytest.mark.parametrize("persona_id", ["", ".", "..", "../outside", "a/b", "a" + chr(92) + "b"])
def test_ensure_avatar_dir_refuses_ids_that_are_not_one_folder_name(tmp_path, persona_id):
    """persona_id に区切りや ``..`` が入っていたら、avatar フォルダを作らない。"""
    home = tmp_path / "home"
    (home / "personas").mkdir(parents=True)
    with pytest.raises(persona_avatar_vrm.AvatarFolderOutsideError):
        persona_avatar_vrm.ensure_avatar_dir(persona_id, home)
    assert list((home / "personas").iterdir()) == []
    assert not (tmp_path / "outside").exists()
