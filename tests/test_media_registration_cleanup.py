"""New document ownership across real files/SQLite, without production or paid APIs."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Building, Item, ItemLocation
from manager.items import ItemService, MissingBuildingError
from saiverse.media_cleanup import NewMediaFiles
from saiverse.media_utils import save_media_summary, store_document_text
from tool_loader import load_builtin_tool
from tools.context import persona_context


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'world.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(Building(CITYID=1, BUILDINGID="room", BUILDINGNAME="Test room"))
        db.commit()
    persona = SimpleNamespace(
        persona_id="synthetic", persona_name="Test", current_building_id="room", is_proxy=False,
    )
    manager = SimpleNamespace(
        saiverse_home=tmp_path, SessionLocal=sessions,
        personas={"synthetic": persona}, all_personas={"synthetic": persona},
        building_map={}, record_persona_event=Mock(), _append_building_history_note=Mock(),
        state=SimpleNamespace(image_default_quality="high"),
    )
    service = ItemService(manager, SimpleNamespace())
    service.refresh_building_system_instruction = Mock()
    manager.item_service = service
    manager.create_document_item = service.create_document_item
    manager.create_picture_item = service.create_picture_item
    # Exercise the actual summary read/write path with a fake inference client.
    client = Mock()
    client.generate.return_value = "Synthetic summary"
    monkeypatch.setattr("saiverse.media_summary._get_summary_client", lambda: client)
    yield SimpleNamespace(home=tmp_path, manager=manager, service=service, sessions=sessions)
    engine.dispose()


def _create(world):
    with persona_context("synthetic", world.home, manager=world.manager):
        return load_builtin_tool("document_create").document_create(
            "Test document", "Description", "Synthetic document body",
        )


def _files(world):
    return sorted((world.home / "documents").glob("*"))


def _rows(world):
    with world.sessions() as db:
        return db.query(Item).count(), db.query(ItemLocation).count()


def _fail_registration(world, monkeypatch, stage, *, session_number=2):
    calls = 0

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {stage}")

    def factory():
        nonlocal calls
        calls += 1
        if calls == session_number and stage == "session":
            fail()
        db = world.sessions()
        if calls == session_number:
            if stage in {"add", "flush", "commit"}:
                monkeypatch.setattr(db, stage, fail)
            elif stage in {"after_flush", "after_commit", "close"}:
                method = stage.removeprefix("after_")
                original = getattr(db, method)

                def after():
                    original()
                    fail()

                monkeypatch.setattr(db, method, after)
        return db

    monkeypatch.setattr(world.manager, "SessionLocal", factory)


@pytest.mark.parametrize("stage", ["session", "add", "flush", "after_flush"])
def test_document_precommit_failure_removes_body_and_summary(world, monkeypatch, stage):
    _fail_registration(world, monkeypatch, stage)
    with pytest.raises(RuntimeError, match=f"injected {stage}"):
        _create(world)
    assert _rows(world) == (0, 0)
    assert _files(world) == []


@pytest.mark.parametrize("stage,expected_rows", [("commit", (0, 0)), ("after_commit", (1, 1)), ("close", (1, 1))])
def test_commit_exception_preserves_files_even_when_outcome_is_unknown(world, monkeypatch, stage, expected_rows):
    _fail_registration(world, monkeypatch, stage)
    with pytest.raises(RuntimeError, match=f"injected {stage}"):
        _create(world)
    assert _rows(world) == expected_rows
    assert len(_files(world)) == 2
    assert {path.read_text() for path in _files(world)} == {"Synthetic document body", "Synthetic summary"}


@pytest.mark.parametrize("notification", ["refresh", "event", "history", "short_id"])
def test_document_postcommit_failure_preserves_registered_files(world, monkeypatch, notification):
    failure = Mock(side_effect=RuntimeError("injected notification"))
    if notification == "refresh":
        monkeypatch.setattr(world.service, "refresh_building_system_instruction", failure)
    elif notification == "event":
        monkeypatch.setattr(world.manager, "record_persona_event", failure)
    elif notification == "history":
        monkeypatch.setattr(world.manager, "_append_building_history_note", failure)
    else:
        monkeypatch.setattr(world.service, "_short_id_of", failure)
    with pytest.raises(RuntimeError, match="injected notification"):
        _create(world)
    assert _rows(world) == (1, 1)
    assert len(_files(world)) == 2


def test_document_success_retains_readable_body_summary_and_references(world):
    message = _create(world)
    assert _rows(world) == (1, 1)
    assert len(_files(world)) == 2
    with world.sessions() as db:
        item = db.query(Item).one()
        assert f"item:{item.SHORT_ID}" in message
        assert (world.home / item.FILE_PATH).read_text() == "Synthetic document body"
        assert item.DESCRIPTION == "Synthetic summary"
        assert db.query(ItemLocation).one().ITEM_ID == item.ITEM_ID


def test_document_second_failure_does_not_remove_first_success(world, monkeypatch):
    _create(world)
    old_files = {path: path.read_bytes() for path in _files(world)}
    _fail_registration(world, monkeypatch, "after_flush")
    with pytest.raises(RuntimeError):
        _create(world)
    assert _rows(world) == (1, 1)
    assert {path: path.read_bytes() for path in _files(world)} == old_files


def test_document_building_removed_after_precheck_cleans_new_files(world, monkeypatch):
    def summarize(path):
        with world.sessions() as db:
            db.query(Building).delete()
            db.commit()
        return "Synthetic summary"

    monkeypatch.setattr("saiverse.media_summary._generate_document_summary", summarize)
    with pytest.raises(MissingBuildingError):
        _create(world)
    assert _files(world) == []
    assert _rows(world) == (0, 0)


def test_document_summary_exception_cleans_body(world, monkeypatch):
    monkeypatch.setattr(
        "saiverse.media_summary._generate_document_summary",
        Mock(side_effect=RuntimeError("summary failed")),
    )
    with pytest.raises(RuntimeError, match="summary failed"):
        _create(world)
    assert _files(world) == []


def test_document_partial_write_is_owned_before_write_finishes(world, monkeypatch):
    original = NewMediaFiles.write_bytes

    def partial(self, path, data):
        original(self, path, data[:3])
        raise OSError("disk full")

    monkeypatch.setattr(NewMediaFiles, "write_bytes", partial)
    with pytest.raises(RuntimeError, match="disk full"):
        _create(world)
    assert _files(world) == []
    assert _rows(world) == (0, 0)


def test_new_files_cleanup_multiple_paths_but_not_preexisting_summary(tmp_path):
    document = tmp_path / "document.txt"
    summary = document.with_suffix(".txt.summary.txt")
    summary.write_text("Existing shared summary")
    extra = tmp_path / "extra.txt"
    with pytest.raises(RuntimeError, match="registration failed"):
        with NewMediaFiles() as files:
            files.write_bytes(document, b"new body")
            files.write_bytes(extra, b"another new file")
            save_media_summary(document, "Replacement summary", new_files=files)
            raise RuntimeError("registration failed")
    assert not document.exists()
    assert not extra.exists()
    assert summary.read_text() == "Existing shared summary"


@pytest.mark.parametrize("symlink", [False, True])
def test_exclusive_create_never_owns_preexisting_file_or_symlink(tmp_path, symlink):
    original = tmp_path / "original.txt"
    original.write_bytes(b"owned by somebody else")
    target = tmp_path / "target.txt"
    if symlink:
        target.symlink_to(original)
    else:
        target.write_bytes(b"preexisting")
    with pytest.raises(FileExistsError):
        with NewMediaFiles() as files:
            files.write_bytes(target, b"overwrite")
    assert original.read_bytes() == b"owned by somebody else"
    assert target.read_bytes() == (original.read_bytes() if symlink else b"preexisting")


def test_document_filename_collision_does_not_overwrite_or_delete(world, monkeypatch):
    class FixedTime:
        @staticmethod
        def now():
            return SimpleNamespace(strftime=lambda fmt: "fixed")

    monkeypatch.setattr("saiverse.media_utils.datetime", FixedTime)
    monkeypatch.setattr("saiverse.media_utils.uuid4", lambda: SimpleNamespace(hex="id"))
    _, existing = store_document_text("existing body")
    with pytest.raises(RuntimeError, match="ファイルの保存に失敗"):
        _create(world)
    assert existing.read_text() == "existing body"
    assert _files(world) == [existing]


@pytest.mark.parametrize("replacement", ["file", "symlink", "hardlink"])
def test_cleanup_preserves_replaced_or_shared_files(tmp_path, replacement):
    path = tmp_path / "new.txt"
    other = tmp_path / "other.txt"
    other.write_bytes(b"existing")
    with pytest.raises(RuntimeError):
        with NewMediaFiles() as files:
            files.write_bytes(path, b"new")
            if replacement == "hardlink":
                os.link(path, tmp_path / "shared.txt")
            else:
                # Keep the old inode alive so replacement identity is deterministic.
                path.rename(tmp_path / "moved.txt")
                if replacement == "file":
                    path.write_bytes(b"replacement")
                else:
                    path.symlink_to(other)
            raise RuntimeError("failed")
    assert path.exists()
    assert other.read_bytes() == b"existing"


def test_unlink_error_does_not_hide_original_failure_or_skip_other_files(tmp_path, monkeypatch, caplog):
    original = Path.unlink
    blocked = tmp_path / "blocked.txt"
    removable = tmp_path / "removable.txt"

    def unlink(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(ValueError, match="original registration failure"):
        with NewMediaFiles() as files:
            files.write_bytes(removable, b"new")
            files.write_bytes(blocked, b"new")
            raise ValueError("original registration failure")
    assert blocked.exists()
    assert not removable.exists()
    assert "Failed to remove unregistered media file" in caplog.text


@pytest.mark.parametrize("item_type", ["picture", "document", "audio", "video"])
def test_registration_of_existing_paths_never_deletes_files(world, monkeypatch, item_type):
    existing = world.home / f"existing-{item_type}.bin"
    existing.write_bytes(b"shared with conversation history")
    _fail_registration(world, monkeypatch, "add", session_number=1)
    create = getattr(world.service, f"create_{item_type}_item_for_user")
    with pytest.raises(RuntimeError, match="injected add"):
        create("Upload", "Description", str(existing), "room")
    assert existing.read_bytes() == b"shared with conversation history"
    assert _rows(world) == (0, 0)


@pytest.mark.parametrize("stage", ["add", "after_commit"])
def test_generated_image_remains_reachable_after_registration_error(world, monkeypatch, stage):
    generator = load_builtin_tool("image_generator")
    monkeypatch.setattr(generator, "_is_image_model_available", lambda model: model == "nano_banana_2")
    monkeypatch.setattr(generator, "_generate_with_nano_banana_2", lambda *args: (b"synthetic image", "image/png"))
    _fail_registration(world, monkeypatch, stage, session_number=1)
    with persona_context("synthetic", world.home, manager=world.manager):
        text, result, file_path, metadata, item_ref = generator.generate_image("Synthetic test image")
    assert Path(file_path).read_bytes() == b"synthetic image"
    assert file_path in result.history_snippet
    assert metadata["media"][0]["uri"] == f"saiverse://image/{Path(file_path).name}"
    assert "画像が生成されました" in text
    assert item_ref is None
    assert _rows(world) == ((0, 0) if stage == "add" else (1, 1))


@pytest.mark.parametrize("item_type", ["image", "document", "audio", "video", "video_uri"])
def test_upload_failure_still_returns_readable_attachment(world, monkeypatch, item_type):
    import base64

    from api.routes.chat import AttachmentData, _store_uploaded_attachment_v2

    for kind in ("picture", "document", "audio", "video"):
        method = f"create_{kind}_item_for_user"
        monkeypatch.setattr(world.manager, method, getattr(world.service, method), raising=False)
    monkeypatch.setattr("saiverse.ffmpeg_runner.is_ffmpeg_available", lambda: True)

    def normalize(source, destination, **kwargs):
        destination.write_bytes(source.read_bytes())
        return True, ""

    monkeypatch.setattr("saiverse.ffmpeg_runner.normalize_audio", normalize)
    monkeypatch.setattr("saiverse.ffmpeg_runner.normalize_video", normalize)
    kind = "video" if item_type == "video_uri" else item_type
    if item_type == "video_uri":
        existing = world.home / "video" / "existing.mp4"
        existing.parent.mkdir()
        existing.write_bytes(b"synthetic upload")
        attachment = AttachmentData(
            type=kind, filename="existing.mp4", mime_type="video/mp4",
            uri="saiverse://video/existing.mp4",
        )
    else:
        mime_type = {"image": "image/png", "document": "text/plain", "audio": "audio/ogg", "video": "video/mp4"}[kind]
        attachment = AttachmentData(
            type=kind, filename="synthetic.txt", mime_type=mime_type,
            data=base64.b64encode(b"synthetic upload").decode(),
        )
    _fail_registration(world, monkeypatch, "add", session_number=1)
    result = _store_uploaded_attachment_v2(attachment, world.manager, "room")
    assert result["item_id"] is None
    assert Path(result["path"]).read_bytes() == b"synthetic upload"
    assert result["uri"].startswith("saiverse://")
    assert _rows(world) == (0, 0)
