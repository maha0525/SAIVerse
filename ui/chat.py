from __future__ import annotations

import logging
import mimetypes
import shutil
import uuid
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
import uuid

from typing import Any, Dict, Iterable, List, Optional

import gradio as gr

from media_utils import iter_image_media, path_to_data_url
from ui import state as ui_state

USER_AVATAR_ICON_PATH = Path("assets/icons/user.png")
_USER_AVATAR_DATA_URL: Optional[str] = None


def _store_uploaded_image(file_path: Optional[str]) -> Optional[Dict[str, str]]:
    if not file_path:
        return None
    source = Path(file_path)
    if not source.exists():
        logging.warning("Uploaded image path missing: %s", file_path)
        return None

    dest_dir = Path.home() / ".saiverse" / "image"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.exception("Failed to prepare image directory: %s", dest_dir)
        return None
    suffix = source.suffix or ".png"
    dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}{suffix}"
    dest_path = dest_dir / dest_name
    try:
        shutil.copy2(source, dest_path)
    except OSError:
        logging.exception("Failed to store uploaded image: %s", file_path)
        return None

    mime_type = mimetypes.guess_type(dest_path)[0] or "image/png"
    return {
        "type": "image",
        "uri": f"saiverse://image/{dest_name}",
        "mime_type": mime_type,
        "source": "user_upload",
    }


def _extract_image_path(value: Optional[Any]) -> Optional[str]:
    """Normalize the upload component value to a filesystem path."""
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        candidate = value.get("name") or value.get("path")
        return str(candidate) if candidate else None
    if isinstance(value, list):
        for item in value:
            candidate = _extract_image_path(item)
            if candidate:
                return candidate
        return None
    name_attr = getattr(value, "name", None)
    if name_attr:
        return str(name_attr)
    path_attr = getattr(value, "path", None)
    if path_attr:
        return str(path_attr)
    return None


def _render_message_images(metadata: Optional[Dict[str, Any]]) -> str:
    attachments: Iterable[Dict[str, Any]] = iter_image_media(metadata)
    found: List[str] = []
    for att in attachments:
        data_url = path_to_data_url(att["path"], att["mime_type"])
        if not data_url:
            continue
        found.append(f"<img src='{data_url}' alt='attachment'>")
    if not found:
        return ""
    return "<div class=\"saiv-image-grid\">" + "".join(found) + "</div>"


def _get_user_avatar_data_url() -> str:
    global _USER_AVATAR_DATA_URL
    manager = ui_state.manager
    if manager and getattr(manager, "user_avatar_data", None):
        return manager.user_avatar_data
    if _USER_AVATAR_DATA_URL is None:
        if USER_AVATAR_ICON_PATH.exists():
            mime = mimetypes.guess_type(USER_AVATAR_ICON_PATH.name)[0] or "image/png"
            data_url = path_to_data_url(USER_AVATAR_ICON_PATH, mime)
            _USER_AVATAR_DATA_URL = data_url or ""
        else:
            _USER_AVATAR_DATA_URL = ""
    return _USER_AVATAR_DATA_URL or ""


def reset_user_avatar_cache() -> None:
    global _USER_AVATAR_DATA_URL
    _USER_AVATAR_DATA_URL = None


def format_history_for_chatbot(raw_history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Convert raw conversation history entries to Gradio Chatbot HTML."""
    display: List[Dict[str, str]] = []
    manager = ui_state.manager

    avatar_box = (
        "width:60px;height:60px;min-width:60px;"
        "border-radius:12px;overflow:hidden;display:inline-block;margin:0;"
    )
    avatar_img = (
        "width:100%;height:100%;object-fit:cover;display:block;"
        "margin:0;border-radius:inherit;clip-path: inset(0 round 12px);"
    )

    def render_block(role_class: str, avatar_src: Optional[str], speaker_name: str, body_html: str, timestamp_text: str) -> str:
        avatar_html = ""
        if avatar_src:
            avatar_html = (
                f"<div class='avatar-top saiv-avatar' style=\"{avatar_box}\">"
                f"<img class='saiv-avatar-img' src='{avatar_src}' style=\"{avatar_img}\"></div>"
            )
        name_html = f"<span class='speaker-name'>{html_escape(speaker_name)}</span>" if speaker_name else ""
        header_inner = ""
        if avatar_html or name_html:
            header_inner = f"<div class='message-header'>{avatar_html}{name_html}</div>"
        timestamp_html = f"<div class='bubble-meta'>{html_escape(timestamp_text)}</div>" if timestamp_text else ""
        return (
            f"<div class='message-block {role_class}'>"
            f"{header_inner}"
            f"<div class='bubble'><div class='bubble-content'>{body_html}</div>{timestamp_html}</div>"
            "</div>"
        )

    user_avatar_src = _get_user_avatar_data_url() or (manager.default_avatar if manager else "")

    def _format_timestamp(ts_raw: Optional[str]) -> str:
        if not ts_raw:
            return ""
        ts_value = str(ts_raw).strip()
        if not ts_value:
            return ""
        if ts_value.endswith("Z"):
            ts_value = ts_value[:-1] + "+00:00"
        try:
            dt_obj = datetime.fromisoformat(ts_value)
        except ValueError:
            return ts_value
        return dt_obj.strftime("%Y-%m-%d %H:%M:%S")

    for entry in raw_history:
        role = entry.get("role", "")
        content = entry.get("content", "") or ""
        metadata = entry.get("metadata")
        timestamp = entry.get("display_timestamp") or entry.get("timestamp") or ""

        # Resolve speaker display name depending on role
        speaker_name = entry.get("speaker_name")
        if role == "user":
            if not speaker_name:
                default_user_name = ""
                if manager:
                    default_user_name = (getattr(manager, "user_display_name", "") or "").strip()
                speaker_name = default_user_name or "ユーザー"
        elif role == "assistant":
            if not speaker_name:
                persona_id = entry.get("persona_id")
                resolved_name = ""
                if manager and persona_id:
                    resolved_name = manager.id_to_name_map.get(str(persona_id), "")
                if not resolved_name:
                    resolved_name = entry.get("persona_name") or ""
                speaker_name = resolved_name or "AI"
        else:
            speaker_name = speaker_name or ""

        html_body = content
        if metadata:
            html_body += _render_message_images(metadata)

        timestamp_text = _format_timestamp(timestamp)

        if role == "user":
            block = render_block("user", user_avatar_src, speaker_name, html_body, timestamp_text)
        else:
            avatar_src = entry.get("avatar_image")
            persona_id = entry.get("persona_id")
            if manager and not avatar_src and persona_id:
                avatar_src = manager.avatar_map.get(str(persona_id), "")
            if manager and not avatar_src:
                persona_name = entry.get("persona_name")
                if persona_name:
                    resolved_id = manager.persona_map.get(persona_name)
                    if resolved_id:
                        avatar_src = manager.avatar_map.get(str(resolved_id), "")
            if manager:
                avatar_src = avatar_src or manager.default_avatar
            else:
                avatar_src = avatar_src or ""
            block = render_block("host", avatar_src, speaker_name, html_body, timestamp_text)
        display.append({"role": role, "content": block})
    return display


def _limit_history(raw_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    limit = ui_state.chat_history_limit
    if limit <= 0:
        return raw_history
    return raw_history[-limit:]


def get_current_building_history() -> List[Dict[str, str]]:
    manager = ui_state.manager
    if not manager:
        return []
    current_building_id = manager.user_current_building_id
    if not current_building_id:
        return []
    raw_history = _limit_history(manager.get_building_history(current_building_id))
    return format_history_for_chatbot(raw_history)


def respond_stream(message: str, media: Optional[Any] = None):
    manager = ui_state.manager
    if not manager:
        raise RuntimeError("Manager not initialised")

    metadata: Optional[Dict[str, Any]] = None
    if media:
        image_path = _extract_image_path(media)
        stored_info = _store_uploaded_image(image_path)
        if stored_info:
            metadata = {"attachments": [stored_info]}

    current_building_id = manager.user_current_building_id

    attachments = metadata.get("attachments", []) if metadata else []
    logging.debug("[respond_stream] attachments prepared: %s", len(attachments))

    if not (message and message.strip()) and not metadata:
        dropdown_update, radio_update = _prepare_move_component_updates()
        base_history = format_history_for_chatbot(manager.get_building_history(current_building_id))
        base_history.append({"role": "assistant", "content": '<div class="note-box">テキストか画像を入力してね。</div>'})
        yield (
            base_history,
            dropdown_update,
            radio_update,
            gr.update(),
            gr.update(),
            gr.update(),
        )
        return

    history = format_history_for_chatbot(manager.get_building_history(current_building_id))

    user_payload: Dict[str, Any] = {"role": "user", "content": message}
    if metadata:
        user_payload["metadata"] = metadata

    user_display_entry = format_history_for_chatbot([user_payload])[0]
    history.append(user_display_entry)

    ai_message = ""
    stream = manager.handle_user_input_stream(message, metadata=metadata)
    for token in stream:
        ai_message += token
        yield (
            history + [{"role": "assistant", "content": ai_message}],
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
        )

    final_raw = _limit_history(manager.get_building_history(current_building_id))
    final_history_formatted = format_history_for_chatbot(final_raw)

    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    dropdown_update, radio_update = _prepare_move_component_updates()
    yield (
        final_history_formatted,
        dropdown_update,
        radio_update,
        gr.update(choices=summonable_personas, value=None),
        gr.update(choices=conversing_personas, value=None),
        gr.update(value=None),
    )


def get_current_location_name() -> str:
    manager = ui_state.manager
    if not manager or not manager.user_current_building_id:
        return "不明な場所"
    if manager.user_current_building_id in manager.building_map:
        return manager.building_map.get(manager.user_current_building_id).name
    return "不明な場所"


def format_location_label(location_name: str) -> str:
    return f"現在地: {location_name}"


def _prepare_move_component_updates(force_dropdown_value: Optional[str] = None, force_radio: bool = False):
    manager = ui_state.manager
    if not manager:
        return gr.update(), gr.update()
    dropdown_kwargs: Dict[str, Any] = {}
    radio_kwargs: Dict[str, Any] = {}

    current_names = sorted(ui_state.building_choices)
    fresh_names = sorted([b.name for b in manager.buildings])
    if current_names != fresh_names:
        logging.info("Building list has changed. Updating selection components.")
        ui_state.refresh_building_caches()
        dropdown_kwargs["choices"] = ui_state.building_choices
        radio_kwargs["choices"] = ui_state.building_choices

    if force_dropdown_value is not None:
        dropdown_kwargs["value"] = force_dropdown_value
    if force_radio or "choices" in radio_kwargs:
        radio_kwargs["value"] = get_current_location_name()

    dropdown_update = gr.update(**dropdown_kwargs) if dropdown_kwargs else gr.update()
    radio_update = gr.update(**radio_kwargs) if radio_kwargs else gr.update()
    return dropdown_update, radio_update


def _normalize_client_location_state(state: Optional[dict], current_location: str) -> dict:
    if not isinstance(state, dict):
        return {"initialized": False, "value": current_location, "session": None}
    initialized = bool(state.get("initialized"))
    value = state.get("value")
    if not isinstance(value, str):
        value = current_location
    session = state.get("session")
    if not isinstance(session, str) or not session.strip():
        session = None
    return {"initialized": initialized, "value": value, "session": session}


def _perform_user_move(building_name: Optional[str], client_state: Optional[dict]):
    manager = ui_state.manager
    if not manager or not manager.user_current_building_id:
        location_name = get_current_location_name()
        return (
            [],
            location_name,
            gr.update(value=format_location_label(location_name)),
            gr.update(),
            gr.update(),
            _normalize_client_location_state(client_state, location_name),
        )

    import time
    start_time = time.time()

    server_location = get_current_location_name()
    state = _normalize_client_location_state(client_state, server_location)
    if state["session"] is None:
        state["session"] = f"py-{uuid.uuid4().hex[:8]}"

    logging.debug(
        "[ui] move handler session=%s building_name=%s state_before=%s server_location=%s",
        state["session"],
        building_name,
        state,
        server_location,
    )

    target_name = building_name or server_location
    if not state["initialized"]:
        state["initialized"] = True
        logging.debug("[ui] initial sync completed for session=%s", state["session"])

    if target_name and target_name != server_location:
        target_building_id = ui_state.building_name_to_id.get(target_name)
        if target_building_id:
            logging.debug(
                "[ui] move request from %s to %s",
                manager.user_current_building_id,
                target_building_id,
            )
            manager.move_user(target_building_id)
            logging.debug(
                "[ui] move request completed in %.2fs",
                time.time() - start_time,
            )
            server_location = get_current_location_name()
        else:
            logging.debug("[ui] move request ignored (unknown target %s)", target_name)
    else:
        logging.debug("[ui] move request synchronising to current location (%s)", server_location)

    state["value"] = server_location

    new_history = get_current_building_history()
    new_location_name = get_current_location_name()
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    logging.debug(
        "[ui] total move handler time %.2fs",
        time.time() - start_time,
    )
    logging.debug(
        "[ui] move handler session=%s state_after=%s",
        state["session"],
        state,
    )
    return (
        new_history,
        new_location_name,
        gr.update(value=format_location_label(new_location_name)),
        gr.update(choices=summonable_personas, value=None),
        gr.update(choices=conversing_personas, value=None),
        state,
    )


def move_user_ui(building_name: str, client_state: Optional[dict]):
    """UI handler for moving the user."""
    (
        history,
        new_location_name,
        location_markdown_update,
        summon_update,
        conversing_update,
        client_state,
    ) = _perform_user_move(building_name, client_state)
    dropdown_update, radio_update = _prepare_move_component_updates(force_radio=True)
    return (
        history,
        new_location_name,
        location_markdown_update,
        dropdown_update,
        radio_update,
        summon_update,
        conversing_update,
        client_state,
    )


def move_user_radio_ui(building_name: str, client_state: Optional[dict]):
    """Radio handler for moving the user and syncing dropdown."""
    (
        history,
        new_location_name,
        location_markdown_update,
        summon_update,
        conversing_update,
        client_state,
    ) = _perform_user_move(building_name, client_state)
    dropdown_update, radio_update = _prepare_move_component_updates(
        force_dropdown_value=new_location_name,
        force_radio=False,
    )
    logging.info("[ui] move_user_radio_ui completed target=%s", new_location_name)
    return (
        dropdown_update,
        history,
        new_location_name,
        location_markdown_update,
        radio_update,
        summon_update,
        conversing_update,
        client_state,
    )


def go_to_user_room_ui(client_state: Optional[dict]):
    manager = ui_state.manager
    home_name: Optional[str] = None
    if manager and manager.user_room_id in manager.building_map:
        home_name = manager.building_map[manager.user_room_id].name
        if not manager.user_current_building_id:
            manager.move_user(manager.user_room_id)

    (
        history,
        new_location_name,
        location_markdown_update,
        summon_update,
        conversing_update,
        client_state,
    ) = _perform_user_move(home_name, client_state)

    dropdown_update, radio_update = _prepare_move_component_updates(
        force_dropdown_value=new_location_name,
        force_radio=True,
    )

    logging.info("[ui] go_to_user_room_ui target=%s", new_location_name)
    return (
        history,
        new_location_name,
        location_markdown_update,
        dropdown_update,
        radio_update,
        summon_update,
        conversing_update,
        client_state,
    )


def select_model(model_name: str):
    manager = ui_state.manager
    if not manager:
        return []
    manager.set_model(model_name or "None")
    current_building_id = manager.user_current_building_id
    if not current_building_id:
        return []
    raw_history = _limit_history(manager.get_building_history(current_building_id))
    return format_history_for_chatbot(raw_history)


def call_persona_ui(persona_name: str):
    manager = ui_state.manager
    if not manager:
        return [], gr.update(), gr.update()
    if not persona_name:
        return get_current_building_history(), gr.update(), gr.update()

    persona_id = manager.persona_map.get(persona_name)
    if persona_id:
        manager.summon_persona(persona_id)
        manager._load_occupancy_from_db()

    new_history = get_current_building_history()
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    return new_history, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)


def end_conversation_ui(persona_id: str):
    manager = ui_state.manager
    if not manager:
        return [], gr.update(), gr.update()
    if not persona_id:
        current_history = get_current_building_history()
        conversing_personas = manager.get_conversing_personas()
        manager._load_occupancy_from_db()
        return current_history, gr.update(), gr.update(choices=conversing_personas, value=None)

    result = manager.end_conversation(persona_id)
    if result:
        if result.startswith("Error:"):
            gr.Warning(result)
        else:
            gr.Info(result)
    manager._load_occupancy_from_db()

    new_history = get_current_building_history()
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    return new_history, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)


def get_autonomous_log(building_name: str):
    manager = ui_state.manager
    if not manager:
        return []
    building_id = ui_state.autonomous_building_map.get(building_name)
    if building_id:
        raw_history = _limit_history(manager.get_building_history(building_id))
        return format_history_for_chatbot(raw_history)
    return []


def start_conversations_ui():
    manager = ui_state.manager
    if not manager:
        return "未初期化"
    manager.start_autonomous_conversations()
    return "実行中"


def stop_conversations_ui():
    manager = ui_state.manager
    if not manager:
        return "未初期化"
    manager.stop_autonomous_conversations()
    return "停止中"


def login_ui():
    manager = ui_state.manager
    if not manager:
        return "未初期化", gr.update(), gr.update()
    manager._load_occupancy_from_db()
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    status = manager.set_user_login_status(1, True)
    return status, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)


def logout_ui():
    manager = ui_state.manager
    if not manager:
        return "未初期化"
    return manager.set_user_login_status(1, False)
