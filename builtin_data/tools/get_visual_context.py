"""Build visual context messages for LLM with structured environment info.

2026-09-06 (docs/intent/room_state_packages.md): 部屋の眺めは**パッケージの束**
として組む。パッケージ = ``{key, family, label, lines, media, state}`` で、
知覚向けの全文テキストはその決定論的な結合 (:func:`sai_memory.room_state.
render_room_full`) から導出する — 構造が正、文字列は導出物。差分
(sai_memory/room_state.py) はこの束のキー照合で組まれる。

head の VisualContextSection は退役した (部屋の様子の置き場は知覚一つ)。
本モジュールはツール (スペル・API) と、知覚への供給 (:func:`build_room_bundle`)
として残る。「あの時の思い出」(_fetch_item_memory_recall) は 2026-09-06 に
機能退役 — 後継の約束は docs/intent/persona_cognition/
recall_tags_and_track_reduction.md 冒頭の 📌。
"""
from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Marker to identify visual context messages so they can be exempt from attachment limits
VISUAL_CONTEXT_MARKER = "__visual_context__"

# API media URL prefix
API_MEDIA_PREFIX = "/api/media/images/"

# Maximum characters for open document content
DOCUMENT_CONTENT_MAX_CHARS = 8000


def _resolve_image_path(path_or_url: Optional[str]) -> Optional[str]:
    """
    Resolve an image path or API URL to an actual filesystem path.

    Handles:
    - API URLs like /api/media/images/filename.jpg -> ~/.saiverse/image/filename.jpg
    - Already absolute filesystem paths -> returned as-is
    - saiverse:// URIs -> resolved via media_utils
    """
    if not path_or_url:
        return None

    # Handle API URL format
    if path_or_url.startswith(API_MEDIA_PREFIX):
        filename = path_or_url[len(API_MEDIA_PREFIX):]
        from saiverse.data_paths import get_saiverse_home
        return str(get_saiverse_home() / "image" / filename)

    # Handle saiverse:// URI format
    if path_or_url.startswith("saiverse://"):
        try:
            from saiverse.media_utils import resolve_media_uri
            resolved = resolve_media_uri(path_or_url)
            return str(resolved) if resolved else None
        except ImportError:
            pass

    # Already an absolute or relative filesystem path
    return path_or_url


def _resolve_item_file_path(manager, file_path_str: str) -> Optional[str]:
    """
    Resolve an item file path to an actual filesystem path.

    Handles:
    - Relative paths (e.g., "image/filename.png") -> saiverse_home / relative_path
    - Legacy WSL absolute paths (e.g., "/home/maha/.saiverse/image/...") -> extract and remap
    """
    if not file_path_str:
        return None

    path = Path(file_path_str)

    # If path exists as-is, return it
    if path.exists():
        return str(path)

    # Try recovery strategies using saiverse_home
    home = getattr(manager, 'saiverse_home', None)
    if not home:
        from saiverse.data_paths import get_saiverse_home
        home = get_saiverse_home()

    # Strategy 0: Relative path (new format)
    if not path.is_absolute():
        candidate = home / file_path_str
        if candidate.exists():
            return str(candidate)

    # Strategy 1: Extract from legacy paths containing 'image' or 'documents'
    parts = path.parts
    for folder in ['image', 'documents']:
        if folder in parts:
            idx = parts.index(folder)
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                return str(candidate)

    # Strategy 2: Just filename fallback
    for folder in ['image', 'documents']:
        candidate = home / folder / path.name
        if candidate.exists():
            return str(candidate)

    return None


def _add_to_media_list(file_path: str, media_list: List[Dict[str, str]]) -> None:
    """Add an image file to the media list with its MIME type."""
    mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
    media_list.append({
        "path": file_path,
        "mime_type": mime_type,
        "type": "image",
    })


def _add_audio_to_media_list(file_path: str, media_list: List[Dict[str, str]]) -> None:
    """Add an audio file to the media list with its MIME type."""
    mime_type = mimetypes.guess_type(file_path)[0] or "audio/ogg"
    media_list.append({
        "path": file_path,
        "mime_type": mime_type,
        "type": "audio",
    })


def _add_video_to_media_list(file_path: str, media_list: List[Dict[str, str]]) -> None:
    """Add a video file to the media list with its MIME type."""
    mime_type = mimetypes.guess_type(file_path)[0] or "video/mp4"
    media_list.append({
        "path": file_path,
        "mime_type": mime_type,
        "type": "video",
    })


def _render_bag_contents(
    contents: List[Dict[str, Any]],
    text_parts: List[str],
    media_list: List[Dict[str, str]],
    manager: Any,
    indent: int = 1,
) -> None:
    """Render bag contents as indented list (all items shown as closed)."""
    prefix = "  " * indent
    type_labels = {
        "picture": "Image", "document": "Document",
        "object": "Object", "bag": "Bag",
    }
    for entry in contents:
        child_name = entry.get("name", "不明なアイテム")
        child_type = (entry.get("type") or "").lower()
        child_desc = (entry.get("description") or "").strip() or "(説明なし)"
        if len(child_desc) > 160:
            child_desc = child_desc[:157] + "..."
        label = type_labels.get(child_type, child_type.capitalize() or "Item")

        # 入れ子アイテムも自分の item:N (安定 short_id) を持つので、位置チェーン
        # (旧 b:5>2) ではなく同一性で指す。
        child_short_id = entry.get("short_id")
        child_ref = f"item:{child_short_id}" if child_short_id is not None else "item:?"

        text_parts.append(f"{prefix}- [{child_ref}] [{label}] {child_name}")
        text_parts.append(f"{prefix}  {child_desc}")

        # Recurse into nested bags
        children = entry.get("_children", [])
        if children and child_type == "bag":
            _render_bag_contents(children, text_parts, media_list, manager, indent + 1)


def _format_item_created_at(item: Dict[str, Any]) -> str:
    """Format item creation datetime for display."""
    from datetime import datetime
    created_at = item.get("created_at")
    if isinstance(created_at, datetime):
        return created_at.strftime("%Y-%m-%d %H:%M")
    if created_at is not None:
        try:
            return datetime.utcfromtimestamp(float(created_at)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
    return ""


def _render_item(
    item: Dict[str, Any],
    text_parts: List[str],
    media_list: List[Dict[str, str]],
    manager: Any,
    ref: Optional[str] = None,
) -> None:
    """Render a single item into the visual context text and media list."""
    item_id = item.get("item_id", "")
    item_type = (item.get("type") or "").lower()
    item_name = item.get("name", "不明なアイテム")
    description = (item.get("description") or "").strip() or "(説明なし)"
    state = item.get("state", {})
    is_open = isinstance(state, dict) and state.get("is_open", False)
    file_path_str = item.get("file_path")
    created_at_str = _format_item_created_at(item)

    # AI 可視の item アドレスは安定 short_id (item:N)。メディア URI も short_id を
    # 使い、UUID は裏方 (ファイル解決) に留める。
    short_id = item.get("short_id")
    item_uri_key = short_id if short_id is not None else item_id

    ref_label = f"[{ref}] " if ref else ""

    type_label = {
        "picture": "Image",
        "document": "Document",
        "object": "Object",
        "bag": "Bag",
    }.get(item_type, item_type.capitalize() or "Item")

    if item_type == "object":
        # Objects have no open/closed concept
        text_parts.append(f"{ref_label}[{type_label}] {item_name}")
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")
        text_parts.append(description)
        text_parts.append("")

    elif item_type == "picture":
        open_label = "(Open)" if is_open else "(Closed)"
        text_parts.append(f"{ref_label}[{type_label}] {item_name}")
        text_parts.append(open_label)
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                text_parts.append(f"saiverse://item/{item_uri_key}/image")
                _add_to_media_list(resolved, media_list)
                LOGGER.debug("get_visual_context: Added open picture item: %s", item_name)
                # Append description as caption when image is displayed
                text_parts.append(description)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    elif item_type == "document":
        open_label = "(Open)" if is_open else "(Closed)"
        text_parts.append(f"{ref_label}[{type_label}] {item_name}")
        text_parts.append(open_label)
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                try:
                    content = Path(resolved).read_text(encoding="utf-8")
                    if len(content) > DOCUMENT_CONTENT_MAX_CHARS:
                        content = content[:DOCUMENT_CONTENT_MAX_CHARS] + "\n... (以下省略)"
                    text_parts.append("```")
                    text_parts.append(content)
                    text_parts.append("```")
                    LOGGER.debug("get_visual_context: Added open document: %s (%d chars)", item_name, len(content))
                except Exception as exc:
                    LOGGER.warning("get_visual_context: Failed to read document %s: %s", item_name, exc)
                    text_parts.append(description)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    elif item_type == "audio":
        open_label = "(Open)" if is_open else "(Closed)"
        text_parts.append(f"{ref_label}[Audio] {item_name}")
        text_parts.append(open_label)
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                text_parts.append(f"saiverse://item/{item_uri_key}/audio")
                _add_audio_to_media_list(resolved, media_list)
                LOGGER.debug("get_visual_context: Added open audio item: %s", item_name)
                text_parts.append(description)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    elif item_type == "video":
        open_label = "(Open)" if is_open else "(Closed)"
        text_parts.append(f"{ref_label}[Video] {item_name}")
        text_parts.append(open_label)
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                text_parts.append(f"saiverse://item/{item_uri_key}/video")
                _add_video_to_media_list(resolved, media_list)
                LOGGER.debug("get_visual_context: Added open video item: %s", item_name)
                text_parts.append(description)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    elif item_type == "bag":
        open_label = "(Open)" if is_open else "(Closed)"
        text_parts.append(f"{ref_label}[{type_label}] {item_name}")
        text_parts.append(open_label)
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")
        text_parts.append(description)

        if is_open and manager and hasattr(manager, 'get_bag_contents_recursive'):
            contents = manager.get_bag_contents_recursive(item_id)
            if contents:
                text_parts.append("")
                _render_bag_contents(contents, text_parts, media_list, manager, indent=1)
            else:
                text_parts.append("  (空)")
        text_parts.append("")

    else:
        # Unknown type — show as generic item
        text_parts.append(f"{ref_label}[{type_label}] {item_name}")
        if created_at_str:
            text_parts.append(f"作成日時: {created_at_str}")
        text_parts.append(description)
        text_parts.append("")


#: is_open の概念を持つアイテム型 (Object と不明型には無い —
#: docs/issues/room_state_diff_built_on_string_parsing.md 洗い出し)。
_OPENABLE_ITEM_TYPES = frozenset({"picture", "document", "audio", "video", "bag"})


@dataclass
class _RenderedItem:
    """アイテム 1 件を描いた結果 (パッケージの材料)。"""
    key: str = ""
    label: str = ""
    state: Optional[str] = None   # "open" / "closed" / None (Object と不明型)
    lines: List[str] = field(default_factory=list)
    media: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class _RenderedOccupant:
    """他ペルソナ 1 人の描画 (パッケージの材料)。"""
    persona_id: str
    lines: List[str] = field(default_factory=list)
    media: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class _RenderedFixture:
    """設置物 1 件の描画 (パッケージの材料)。"""
    fixture_id: str
    name: str = ""
    lines: List[str] = field(default_factory=list)


@dataclass
class _WorldRead:
    """一度の読みで取った、その瞬間の Building の姿。

    ここから先は世界を読まない — 姿ごとの違い (ツール向けの head 記法 /
    知覚向けのパッケージの束) は、この同じ材料の組み替えだけで作る。
    """
    persona_id: str
    persona_name: str
    building_id: str
    building_name: str
    base_system_instruction: str = ""
    persona_count: int = 0
    has_others: bool = False
    self_lines: List[str] = field(default_factory=list)
    self_media: List[Dict[str, str]] = field(default_factory=list)
    other_personas: List[_RenderedOccupant] = field(default_factory=list)
    users: List[Tuple[str, str]] = field(default_factory=list)   # (uid, 表示名)
    building_image_lines: List[str] = field(default_factory=list)
    building_image_media: List[Dict[str, str]] = field(default_factory=list)
    inventory: List[_RenderedItem] = field(default_factory=list)
    building_items: List[_RenderedItem] = field(default_factory=list)
    fixtures: List[_RenderedFixture] = field(default_factory=list)


def get_visual_context(
    building_id: Optional[str] = None,
    include_self: bool = True,
    include_building: bool = True,
    include_other_personas: bool = True,
    for_perception: bool = False,
) -> List[Dict[str, Any]]:
    """Build structured visual context message for LLM.

    Returns a single-element list containing a user message with structured
    environment info: persona presence, building details, and all items.

    Args:
        building_id: Building ID. Defaults to current building.
        include_self: Include the active persona's appearance image.
        include_building: Include the current building's interior image.
        include_other_personas: Include appearance images of other personas in the building.
        for_perception: 知覚向けの記法で出力する。パッケージの束
            (:func:`build_room_bundle`) を組み、その決定論的な結合
            (:func:`sai_memory.room_state.render_room_full`) を本文にする —
            入室時に知覚台帳へ積まれる「部屋の様子」と同じ文面。自分の外見と
            インベントリは含まれず、``<system>`` では包まない。

    Returns:
        List of message dicts with 'role', 'content', and 'metadata' keys.
    """
    if for_perception:
        bundle = build_room_bundle(building_id)
        if not bundle:
            return []
        from sai_memory.room_state import bundle_media, render_room_full
        return [{
            "role": "user",
            "content": render_room_full(bundle),
            "metadata": {
                "media": bundle_media(bundle),
                VISUAL_CONTEXT_MARKER: True,
            },
        }]

    world = _read_active_world(
        building_id,
        include_self=include_self,
        include_other_personas=include_other_personas,
        include_building=include_building,
        include_inventory=True,
    )
    if world is None:
        return []
    return _render_head_view(
        world,
        include_self=include_self,
        include_building=include_building,
        include_other_personas=include_other_personas,
    )


def build_room_bundle(building_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """今いる (または指定した) Building のパッケージの束を組む。

    束の形は sai_memory/room_state.py のモジュール規約:
    ``{"building_id", "building_name", "packages": [...]}``。パッケージは
    family ごとに決定論の順 (キーの昇順) で並ぶ — 同じ部屋を同じペルソナが
    何度読んでも同じ束になる (差分の照合と指紋の前提)。

    自分の外見とインベントリは含めない (部屋の性質ではなく見る側の持ち物)。
    アクティブなペルソナ / manager が引けない回は None (従来の縮退と同じ)。
    """
    world = _read_active_world(
        building_id,
        include_self=False,
        include_other_personas=True,
        include_building=True,
        include_inventory=False,
    )
    if world is None:
        return None
    return _bundle_from_world(world)


def _read_active_world(
    building_id: Optional[str],
    *,
    include_self: bool,
    include_other_personas: bool,
    include_building: bool,
    include_inventory: bool,
) -> Optional[_WorldRead]:
    """アクティブな persona/manager コンテキストで世界を一度だけ読む。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        LOGGER.debug("get_visual_context: No active persona")
        return None

    manager = get_active_manager()
    if not manager:
        LOGGER.debug("get_visual_context: No manager available")
        return None

    persona = manager.all_personas.get(persona_id)
    if not persona:
        LOGGER.debug("get_visual_context: Persona %s not found", persona_id)
        return None

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        LOGGER.debug("get_visual_context: No building_id")
        return None

    return _read_world(
        manager, persona, persona_id, building_id,
        include_self=include_self,
        include_other_personas=include_other_personas,
        include_building=include_building,
        include_inventory=include_inventory,
    )


def _sort_key_for_item(entry: _RenderedItem) -> Tuple[int, str]:
    """アイテムの決定論の並び (short_id の数値順、引けなければキー文字列順)。"""
    ref = entry.key
    if ref.startswith("item:"):
        tail = ref[len("item:"):]
        try:
            return (int(tail), "")
        except ValueError:
            pass
    return (1 << 30, ref)


def _read_world(
    manager: Any,
    persona: Any,
    persona_id: str,
    building_id: str,
    *,
    include_self: bool,
    include_other_personas: bool,
    include_building: bool,
    include_inventory: bool,
) -> _WorldRead:
    """その瞬間の Building を一度だけ読む (描き分けはここではしない)。"""
    building_obj = getattr(persona, "buildings", {}).get(building_id)
    world = _WorldRead(
        persona_id=persona_id,
        persona_name=getattr(persona, "persona_name", persona_id),
        building_id=building_id,
        building_name=building_obj.name if building_obj else building_id,
    )

    if building_obj:
        base_sys = getattr(building_obj, "base_system_instruction", "") or ""
        world.base_system_instruction = base_sys.strip()

    # ========== Section 1: ペルソナ ==========
    all_occupants = manager.occupants.get(building_id, [])
    # ユーザーIDはall_personasに存在しないのでフィルタしてAIペルソナのみに絞る
    occupants = [oid for oid in all_occupants if manager.all_personas.get(oid)]
    world.persona_count = len(occupants)
    world.has_others = any(oid != persona_id for oid in occupants)

    if include_self:
        world.self_lines.append(f"[あなた自身（{world.persona_name}）の外見]")
        world.self_lines.append(f"saiverse://persona/{persona_id}/image")
        self_image_path = _resolve_image_path(
            _get_persona_appearance_path(manager, persona_id),
        )
        if self_image_path and os.path.exists(self_image_path):
            _add_to_media_list(self_image_path, world.self_media)
            LOGGER.debug("get_visual_context: Added self image: %s", self_image_path)
        world.self_lines.append("")

    if include_other_personas:
        for other_id in sorted(str(oid) for oid in occupants if oid != persona_id):
            other_persona = manager.all_personas.get(other_id)
            other_name = getattr(other_persona, "persona_name", other_id) if other_persona else other_id
            occupant = _RenderedOccupant(persona_id=other_id)
            occupant.lines.append(f"[{other_name}の外見]")
            occupant.lines.append(f"saiverse://persona/{other_id}/image")

            other_image_path = _resolve_image_path(
                _get_persona_appearance_path(manager, other_id),
            )
            if other_image_path and os.path.exists(other_image_path):
                _add_to_media_list(other_image_path, occupant.media)
                LOGGER.debug("get_visual_context: Added other persona image: %s (%s)", other_id, other_image_path)
            world.other_personas.append(occupant)

    # ========== Section 1b: ユーザー ==========
    user_occupants = sorted(
        str(oid) for oid in all_occupants if not manager.all_personas.get(oid)
    )
    if user_occupants:
        try:
            from database.session import SessionLocal as _SessionLocal
            from database.models import User as UserModel
            db = _SessionLocal()
            try:
                for uid in user_occupants:
                    user = db.query(UserModel).filter(UserModel.USERID == int(uid)).first()
                    uname = user.USERNAME if user else uid
                    world.users.append((uid, uname))
            finally:
                db.close()
        except Exception as exc:
            LOGGER.debug("get_visual_context: Failed to fetch user names: %s", exc)
            world.users = [(uid, uid) for uid in user_occupants]

    # ========== Section 2: Building ==========
    if include_building:
        building_image_path = _resolve_image_path(
            _get_building_image_path(manager, building_id),
        )
        if building_image_path and os.path.exists(building_image_path):
            world.building_image_lines.append("[内装]")
            world.building_image_lines.append(f"saiverse://building/{building_id}/image")
            _add_to_media_list(building_image_path, world.building_image_media)
            LOGGER.debug("get_visual_context: Added building image: %s", building_image_path)

    # ========== Section 3: Item ==========
    # インベントリは部屋の性質ではない (持ち物は移動に付いてくる) ので、束には
    # 載せない。誰も要らない回は読みにも行かない。
    if include_inventory and hasattr(manager, 'get_all_items_for_persona'):
        world.inventory = [
            _render_item_entry(item, manager)
            for item in manager.get_all_items_for_persona(persona_id)
        ]
        world.inventory.sort(key=_sort_key_for_item)

    if hasattr(manager, 'get_all_items_in_building'):
        world.building_items = [
            _render_item_entry(item, manager)
            for item in manager.get_all_items_in_building(building_id)
        ]
        world.building_items.sort(key=_sort_key_for_item)

    # ========== Section 4: Fixture ==========
    obs_mgr = getattr(manager, "observer_manager", None)
    if obs_mgr:
        fixtures = obs_mgr.get_building_fixtures(building_id)
        for f in sorted(fixtures or [], key=lambda f: str(f.FIXTURE_ID)):
            world.fixtures.append(_render_fixture(f))

    return world


def _render_fixture(f: Any) -> _RenderedFixture:
    """設置物 1 件を描く (観測値・フィードスタンドの表示込み)。"""
    rendered = _RenderedFixture(
        fixture_id=str(f.FIXTURE_ID), name=str(f.NAME or ""),
    )
    lines = rendered.lines
    lines.append(f"- **{f.NAME}** (種別: {f.TYPE or 'object'}, ID: `{f.FIXTURE_ID}`)")
    if f.DESCRIPTION:
        lines.append(f"  {f.DESCRIPTION}")
    if f.STATE_JSON:
        import json as _json
        try:
            state = _json.loads(f.STATE_JSON)
            if isinstance(state, dict):
                # feed_stand キー (feed_manager.update_fixture_display が
                # 唯一の書き手) は観測値形式 (value_num/value_text) では
                # ないため専用に描画する。購読タイトルと直近見出しは
                # 書き手側で件数・文字数を制御済み (5 件 × 100 字)。
                feed_display = state.pop("feed_stand", None)
                if isinstance(feed_display, dict):
                    subs_titles = feed_display.get("subscriptions")
                    if isinstance(subs_titles, list) and subs_titles:
                        lines.append(
                            "  購読フィード: "
                            + " / ".join(str(s) for s in subs_titles)
                        )
                    latest_titles = feed_display.get("latest")
                    if isinstance(latest_titles, list) and latest_titles:
                        lines.append("  新着記事の見出し:")
                        for t in latest_titles:
                            lines.append(f"  - {t}")
            if state:
                state_parts = []
                for k, v in state.items():
                    val = v.get("value_num") if isinstance(v, dict) and v.get("value_num") is not None else (v.get("value_text") if isinstance(v, dict) else v)
                    if val is not None:
                        state_parts.append(f"{k}={val}")
                if state_parts:
                    lines.append(f"  最新観測値: {', '.join(state_parts)}")
        except (TypeError, _json.JSONDecodeError):
            pass
    return rendered


def _render_item_entry(item: Dict[str, Any], manager: Any) -> _RenderedItem:
    """アイテム 1 件を描く (読みと同じく一度だけ — パッケージの材料になる)。"""
    short_id = item.get("short_id")
    ref = f"item:{short_id}" if short_id is not None else None
    item_type = (item.get("type") or "").lower()
    state = item.get("state", {})
    is_open = isinstance(state, dict) and state.get("is_open", False)
    entry = _RenderedItem(
        key=ref if ref is not None else f"item:{item.get('item_id', '?')}",
        state=(
            ("open" if is_open else "closed")
            if item_type in _OPENABLE_ITEM_TYPES else None
        ),
    )
    _render_item(item, entry.lines, entry.media, manager, ref=ref)
    # 末尾の空行はパッケージには持たせない (結合側が区切りを足す)。
    while entry.lines and not entry.lines[-1].strip():
        entry.lines.pop()
    entry.label = entry.lines[0] if entry.lines else entry.key
    return entry


def _normalize_lines(parts: List[str]) -> List[str]:
    """描画部品の列を**本物の行**の列にする (改行入りの一要素を分解する)。

    ドキュメント本文などは一要素の複数行文字列として描かれる — そのまま束に
    載せると、行単位の diff (sai_memory/room_state.py §4) が本文全体を「一行」
    として扱い、一字の編集で全文を再掲してしまう。空文字列の要素 (意図した
    空行) は空行のまま残す。
    """
    lines: List[str] = []
    for part in parts:
        text = str(part)
        if not text:
            lines.append("")
            continue
        split = text.splitlines()
        lines.extend(split if split else [""])
    return lines


def _bundle_from_world(world: _WorldRead) -> Dict[str, Any]:
    """読み終えた材料から、パッケージの束を組む (世界は読まない)。

    key の割り当ては intent §3 の表: ``persona:<ペルソナID>`` /
    ``user:<ユーザーID>`` / ``building:image`` / ``building:prompt`` /
    ``item:N`` / ``fixture:<FIXTURE_ID>``。**族の接頭辞は組成の時点でキーに
    焼き込む** — ペルソナ ID・ユーザー ID・設置物 ID は独立の名前空間なので、
    生の ID のままだと同じ文字列を持つ別族が差分の辞書
    (sai_memory/room_state.render_room_diff の key 照合) で片方を上書きし、
    退出・消滅の報告が黙って消える (2026-09-06 四巡目修正 3)。
    パッケージの lines は :func:`_normalize_lines` を通した本物の行の列。

    同一キーの重複は**先勝ち** (最初の一枚を残し、後から来た同キーは WARN を
    出して積まない) — 組成順が決定論なので、規則も決定論になる。
    """
    packages: List[Dict[str, Any]] = []
    seen_keys: set = set()

    def _add(package: Dict[str, Any]) -> None:
        key = package["key"]
        if key in seen_keys:
            LOGGER.warning(
                "build_room_bundle: duplicate package key %r in building %s; "
                "keeping the first package and dropping the later one",
                key, world.building_id,
            )
            return
        seen_keys.add(key)
        packages.append(package)

    for occupant in world.other_personas:
        _add({
            "key": f"persona:{occupant.persona_id}",
            "family": "persona",
            "label": occupant.lines[0] if occupant.lines else occupant.persona_id,
            "lines": _normalize_lines(occupant.lines),
            "media": [dict(m) for m in occupant.media],
            "state": None,
        })

    for uid, uname in world.users:
        _add({
            "key": f"user:{uid}",
            "family": "user",
            "label": f"{uname} (ID:{uid})",
            "lines": [f"- {uname} (ID:{uid})"],
            "media": [],
            "state": None,
        })

    if world.building_image_lines:
        _add({
            "key": "building:image",
            "family": "interior",
            "label": "[内装]",
            "lines": _normalize_lines(world.building_image_lines),
            "media": [dict(m) for m in world.building_image_media],
            "state": None,
        })

    if world.base_system_instruction:
        _add({
            "key": "building:prompt",
            "family": "prompt",
            "label": "[システムプロンプト]",
            "lines": _normalize_lines(
                ["[システムプロンプト]"]
                + world.base_system_instruction.splitlines(),
            ),
            "media": [],
            "state": None,
        })

    for entry in world.building_items:
        _add({
            "key": entry.key,
            "family": "item",
            "label": entry.label,
            "lines": _normalize_lines(entry.lines),
            "media": [dict(m) for m in entry.media],
            "state": entry.state,
        })

    for fixture in world.fixtures:
        _add({
            "key": f"fixture:{fixture.fixture_id}",
            "family": "fixture",
            "label": f"{fixture.name} (ID: {fixture.fixture_id})",
            "lines": _normalize_lines(fixture.lines),
            "media": [],
            "state": None,
        })

    return {
        "building_id": world.building_id,
        "building_name": world.building_name,
        "packages": packages,
    }


def _render_head_view(
    world: _WorldRead,
    *,
    include_self: bool,
    include_building: bool,
    include_other_personas: bool,
) -> List[Dict[str, Any]]:
    """ツール向けの姿 (自分の外見・インベントリ込み、``<system>`` 包み)。"""
    text_parts: List[str] = []
    media_list: List[Dict[str, str]] = []

    text_parts.append("<system>")
    text_parts.append("# ビジュアルコンテキスト")
    # NOTE: かつて「常にリアルタイム状態を反映」と書いていたが、head の
    # visual_context は Metabolism まで凍結されるため嘘だった (2026-07-09 削除)。
    text_parts.append("以下は現在の状況を視覚的に示す情報です。")
    text_parts.append("")
    text_parts.append("---")
    text_parts.append("")

    # ========== Section 1: ペルソナ ==========
    text_parts.append("## ペルソナ")
    if world.persona_count <= 1:
        text_parts.append("現在、このBuildingにはあなただけがいます。")
    else:
        text_parts.append(f"現在、このBuildingにはあなた含め{world.persona_count}人のペルソナがいます。")
    text_parts.append("")

    if include_self:
        text_parts.extend(world.self_lines)
        media_list.extend(world.self_media)

    if include_other_personas:
        for occupant in world.other_personas:
            text_parts.extend(occupant.lines)
            text_parts.append("")
            media_list.extend(occupant.media)

    # ========== Section 1b: ユーザー ==========
    if world.users:
        text_parts.append("## ユーザー")
        text_parts.append(f"現在、このBuildingには{len(world.users)}人のユーザーがいます。")
        for uid, uname in world.users:
            text_parts.append(f"- {uname} (ID:{uid})")
        text_parts.append("")

    # ========== Section 2: Building ==========
    text_parts.append("---")
    text_parts.append("")
    text_parts.append("## Building")
    text_parts.append(f"現在、「{world.building_name}」にいます。")
    text_parts.append("")

    if include_building and world.building_image_lines:
        text_parts.extend(world.building_image_lines)
        text_parts.append("")
        media_list.extend(world.building_image_media)

    if world.base_system_instruction:
        text_parts.append("[システムプロンプト]")
        text_parts.append(world.base_system_instruction)
        text_parts.append("")

    # ========== Section 3: Item ==========
    text_parts.append("---")
    text_parts.append("")
    text_parts.append("## Item")
    text_parts.append("")

    if world.inventory:
        text_parts.append(f"### あなた自身（{world.persona_name}）のインベントリ内")
        text_parts.append("")
        for entry in world.inventory:
            text_parts.extend(entry.lines)
            text_parts.append("")
            media_list.extend(entry.media)

    if world.building_items:
        text_parts.append("### Building内")
        text_parts.append("")
        for entry in world.building_items:
            text_parts.extend(entry.lines)
            text_parts.append("")
            media_list.extend(entry.media)

    if not world.inventory and not world.building_items:
        text_parts.append("アイテムはありません。")
        text_parts.append("")

    # ========== Section 4: Fixture ==========
    if world.fixtures:
        text_parts.append("---")
        text_parts.append("")
        text_parts.append("## 設置物 (Fixture)")
        text_parts.append("")
        for fixture in world.fixtures:
            text_parts.extend(fixture.lines)
            text_parts.append("")

    text_parts.append("</system>")

    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": "\n".join(text_parts),
            "metadata": {
                "media": media_list,
                VISUAL_CONTEXT_MARKER: True,
            },
        },
    ]

    LOGGER.info(
        "get_visual_context: Generated visual context (%d images, %d inventory items, %d building items)",
        len(media_list), len(world.inventory), len(world.building_items),
    )
    return messages


def _get_building_image_path(manager, building_id: str) -> Optional[str]:
    """Get the IMAGE_PATH for a building from the database."""
    try:
        from database.session import SessionLocal
        from database.models import Building
        session = SessionLocal()
        try:
            building = session.query(Building).filter(Building.BUILDINGID == building_id).first()
            if building and building.IMAGE_PATH:
                return building.IMAGE_PATH
        finally:
            session.close()
    except Exception as exc:
        LOGGER.debug("Failed to get building image path: %s", exc)
    return None


def _get_persona_appearance_path(manager, persona_id: str) -> Optional[str]:
    """Get the APPEARANCE_IMAGE_PATH for a persona from the database."""
    try:
        from database.session import SessionLocal
        from database.models import AI
        session = SessionLocal()
        try:
            ai = session.query(AI).filter(AI.AIID == persona_id).first()
            if ai and ai.APPEARANCE_IMAGE_PATH:
                return ai.APPEARANCE_IMAGE_PATH
        finally:
            session.close()
    except Exception as exc:
        LOGGER.debug("Failed to get persona appearance path: %s", exc)
    return None


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_visual_context",
        description="Build visual context messages containing structured environment info for LLM context.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                },
                "include_self": {
                    "type": "boolean",
                    "description": "Include active persona's appearance image. Default: true.",
                    "default": True
                },
                "include_building": {
                    "type": "boolean",
                    "description": "Include building interior image. Default: true.",
                    "default": True
                },
                "include_other_personas": {
                    "type": "boolean",
                    "description": "Include other personas' appearance images. Default: true.",
                    "default": True
                }
            },
            "required": [],
        },
        result_type="array",
    )
