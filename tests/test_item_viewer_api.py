"""User item viewing keeps metadata, content, identity and ownership consistent.

Exercise the actual read routes, AdminService and ItemService against temporary
SQLite/files. No live manager, persona, LLM, events or world operations run.
User-facing item reads are global; persona inventory membership remains scoped.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from api.routes import info, uri, world
from api.routes.people import inventory
from database.models import Base, Item, ItemLocation
from manager.admin import AdminService
from manager.items import ItemService


OWNERS = {
    "inventory": ("persona", "synthetic-owner"),
    "other_inventory": ("persona", "synthetic-other"),
    "building": ("building", "synthetic-room"),
    "world": ("world", ""),
}


@pytest.fixture
def viewer_world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(home / "user_data"))
    monkeypatch.delenv("SAIVERSE_EXTERNAL_FILE_ROOTS", raising=False)
    engine = create_engine(
        f"sqlite:///{home / 'world.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    examples = {}
    with sessions() as db:
        for owner_label, (owner_kind, owner_id) in OWNERS.items():
            for item_type in ("picture", "document"):
                short_id = 82 + len(examples)
                item_id = str(UUID(int=short_id))
                name = f"{owner_label} {item_type} title"
                if item_type == "picture":
                    relative_path = f"image/{short_id}.png"
                    path = home / relative_path
                    path.parent.mkdir(exist_ok=True)
                    Image.new("RGB", (2, 2), color=(short_id, 20, 30)).save(path)
                    content = path.read_bytes()
                else:
                    relative_path = f"documents/{short_id}.txt"
                    path = home / relative_path
                    path.parent.mkdir(exist_ok=True)
                    content = f"{name}\n本文 {short_id}\n"
                    path.write_text(content, encoding="utf-8")
                db.add(Item(
                    ITEM_ID=item_id, SHORT_ID=short_id, NAME=name, TYPE=item_type,
                    DESCRIPTION=f"{name} description", FILE_PATH=relative_path,
                    STATE_JSON='{"is_open":false}',
                ))
                if owner_kind != "world":
                    db.add(ItemLocation(
                        ITEM_ID=item_id, OWNER_KIND=owner_kind, OWNER_ID=owner_id,
                        SLOT_NUMBER=1 if item_type == "picture" else 2,
                    ))
                examples[owner_label, item_type] = SimpleNamespace(
                    id=item_id, short_id=short_id, name=name, type=item_type,
                    content=content, path=path, relative_path=relative_path,
                    owner_kind=owner_kind, owner_id=owner_id,
                )
        db.commit()

    state = SimpleNamespace(items={})
    manager = SimpleNamespace(
        SessionLocal=sessions, saiverse_home=home, state=state,
        buildings=[], building_map={}, personas={},
        user_current_building_id="unrelated-current-room",
    )
    service = ItemService(manager, state)
    manager.item_service = service
    service.load_items_from_db()
    manager.items = service.items
    admin = AdminService.__new__(AdminService)
    admin.SessionLocal = sessions
    manager.get_item_details = admin.get_item_details

    app = FastAPI()
    app.include_router(world.router, prefix="/api/world")
    app.include_router(info.router, prefix="/api/info")
    app.include_router(inventory.router, prefix="/api/people")
    app.include_router(uri.router, prefix="/api/uri")
    app.dependency_overrides[get_manager] = lambda: manager
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, manager=manager, service=service, sessions=sessions,
                examples=examples, home=home,
            )
    finally:
        engine.dispose()


def _assert_content(response, example):
    assert response.status_code == 200, response.text
    if example.type == "picture":
        assert response.headers["content-type"] == "image/png"
        assert response.content == example.content
    else:
        assert response.json() == {"type": "document", "content": example.content}


@pytest.mark.parametrize("owner", OWNERS)
@pytest.mark.parametrize("item_type", ["picture", "document"])
@pytest.mark.parametrize("key_kind", ["id", "short_id"])
def test_metadata_and_content_agree_for_uuid_and_short_id(viewer_world, owner, item_type, key_kind):
    example = viewer_world.examples[owner, item_type]
    key = getattr(example, key_kind)
    response = viewer_world.client.get(f"/api/world/items/{key}")
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["ITEM_ID"] == example.id
    assert metadata["SHORT_ID"] == example.short_id
    assert metadata["TYPE"] == item_type
    assert metadata["NAME"] == example.name
    assert metadata["FILE_PATH"] == example.relative_path
    # Resolving a short ID must not accidentally look up its location by that ID.
    assert metadata["OWNER_KIND"] == example.owner_kind
    assert metadata["OWNER_ID"] == example.owner_id
    _assert_content(viewer_world.client.get(f"/api/info/item/{key}"), example)
    # The viewer uses the canonical UUID returned by metadata for its content.
    _assert_content(
        viewer_world.client.get(f"/api/info/item/{metadata['ITEM_ID']}"), example,
    )


@pytest.mark.parametrize("owner", ["inventory", "other_inventory"])
def test_inventory_lists_only_that_personas_items_with_real_metadata(viewer_world, owner):
    persona_id = OWNERS[owner][1]
    response = viewer_world.client.get(f"/api/people/{persona_id}/items")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()}
    expected = [viewer_world.examples[owner, kind] for kind in ("picture", "document")]
    assert set(items) == {item.id for item in expected}
    for example in expected:
        item = items[example.id]
        assert item["type"] == example.type
        assert item["name"] == example.name
        _assert_content(viewer_world.client.get(f"/api/info/item/{item['id']}"), example)


@pytest.mark.parametrize("item_type", ["picture", "document"])
@pytest.mark.parametrize("key_kind", ["id", "short_id"])
def test_content_uri_resolves_to_the_same_inventory_item(viewer_world, item_type, key_kind):
    example = viewer_world.examples["inventory", item_type]
    response = viewer_world.client.get("/api/uri/resolve", params={
        "uri": f"saiverse://item/{getattr(example, key_kind)}/content",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["metadata"] == {
        "item_id": example.id, "title": example.name, "type": item_type,
    }
    if item_type == "picture":
        assert data["content_type"] == "image"
        assert data["content"] == f"/api/info/item/{example.id}"
        _assert_content(viewer_world.client.get(data["content"]), example)
    else:
        assert data["content_type"] == "item_content"
        assert data["content"] == example.content


@pytest.mark.parametrize("key", ["99999", str(UUID(int=99999))])
@pytest.mark.parametrize("route", ["/api/world/items/", "/api/info/item/"])
def test_missing_item_is_404(viewer_world, route, key):
    response = viewer_world.client.get(f"{route}{key}")
    assert response.status_code == 404
    assert "Item not found" in response.json()["detail"]


def test_inventory_for_unrelated_persona_is_empty(viewer_world):
    response = viewer_world.client.get("/api/people/unrelated-persona/items")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("item_type", ["picture", "document"])
@pytest.mark.parametrize("key_kind", ["id", "short_id"])
def test_content_still_rejects_files_outside_managed_roots(viewer_world, item_type, key_kind):
    example = viewer_world.examples["inventory", item_type]
    outside = viewer_world.home.parent / f"outside-{example.path.name}"
    outside.write_bytes(example.path.read_bytes())
    viewer_world.service.items[example.id]["file_path"] = str(outside)
    response = viewer_world.client.get(f"/api/info/item/{getattr(example, key_kind)}")
    assert response.status_code == 403
    assert response.json()["detail"] == "File path is outside configured storage roots"


def test_reading_items_does_not_change_ownership_open_state_or_files(viewer_world):
    def rows():
        with viewer_world.sessions() as db:
            return (
                db.execute(select(Item.__table__).order_by(Item.ITEM_ID)).all(),
                db.execute(select(ItemLocation.__table__).order_by(ItemLocation.ITEM_ID)).all(),
            )

    before = rows()
    files_before = {example.path: example.path.read_bytes() for example in viewer_world.examples.values()}
    for example in viewer_world.examples.values():
        assert viewer_world.client.get(f"/api/world/items/{example.short_id}").status_code == 200
        _assert_content(viewer_world.client.get(f"/api/info/item/{example.id}"), example)
    assert rows() == before
    assert {path: path.read_bytes() for path in files_before} == files_before
    assert all(item["state"] == {"is_open": False} for item in viewer_world.service.items.values())


@pytest.mark.parametrize("owner", ["other_inventory", "world"])
def test_global_metadata_lookup_does_not_grant_persona_document_edit_access(viewer_world, owner):
    example = viewer_world.examples[owner, "document"]
    viewer_world.manager.personas["synthetic-owner"] = SimpleNamespace(
        current_building_id="synthetic-room", is_proxy=False,
    )
    assert viewer_world.client.get(f"/api/world/items/{example.short_id}").status_code == 200
    with pytest.raises(RuntimeError, match="インベントリまたは現在いる建物にありません"):
        viewer_world.service._validate_document_access("synthetic-owner", example.id)
