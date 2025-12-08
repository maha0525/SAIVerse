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
from model_configs import get_model_parameters, get_model_parameter_defaults
from ui import state as ui_state

CLIENT_PARAM_SCOPE = "chat"


def _is_param_supported_for_client(conf: Optional[Dict[str, Any]], client_scope: str = CLIENT_PARAM_SCOPE) -> bool:
    if not isinstance(conf, dict):
        return False
    scopes = conf.get("client_support")
    if not scopes:
        return True
    if isinstance(scopes, str):
        scopes = [scopes]
    normalized = {str(scope).lower() for scope in scopes}
    return client_scope in normalized


def _filter_supported_parameter_values(model_name: str, values: Dict[str, Any]) -> Dict[str, Any]:
    if not values:
        return {}
    spec = get_model_parameters(model_name)
    filtered: Dict[str, Any] = {}
    for key, value in values.items():
        conf = spec.get(key)
        if _is_param_supported_for_client(conf):
            filtered[key] = value
    return filtered

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


def _hidden_param_update():
    return gr.update(visible=False, value=None, interactive=False)


def _build_parameter_updates(
    model_name: str,
    preset_values: Optional[Dict[str, Any]] = None,
):
    if not model_name or model_name == "None":
        hidden = _hidden_param_update()
        return hidden, hidden, hidden, hidden, hidden, {}

    spec = get_model_parameters(model_name)
    if not spec:
        hidden = _hidden_param_update()
        return hidden, hidden, hidden, hidden, hidden, {}

    state_values = dict(get_model_parameter_defaults(model_name))
    if preset_values:
        state_values.update({k: v for k, v in preset_values.items() if v is not None})
    state_values = _filter_supported_parameter_values(model_name, state_values)

    def _get_conf(name: str) -> Optional[Dict[str, Any]]:
        conf = spec.get(name)
        if not isinstance(conf, dict):
            return None
        return conf if _is_param_supported_for_client(conf) else None

    visible_values = {k: v for k, v in state_values.items() if _get_conf(k)}

    def slider_update(param_name: str, fallback_label: str):
        conf = _get_conf(param_name)
        if not conf:
            return _hidden_param_update()
        return gr.update(
            visible=True,
            minimum=conf.get("min", 0),
            maximum=conf.get("max", 1),
            step=conf.get("step", 0.1),
            value=visible_values.get(param_name, conf.get("default")),
            label=conf.get("label", fallback_label),
            info=conf.get("description", ""),
            interactive=True,
        )

    def number_update(param_name: str, fallback_label: str):
        conf = _get_conf(param_name)
        if not conf:
            return _hidden_param_update()
        return gr.update(
            visible=True,
            value=visible_values.get(param_name, conf.get("default")),
            label=conf.get("label", fallback_label),
            info=conf.get("description", ""),
            minimum=conf.get("min"),
            maximum=conf.get("max"),
            interactive=True,
            precision=0,
        )

    def dropdown_update(param_name: str, fallback_label: str):
        conf = _get_conf(param_name)
        if not conf:
            return _hidden_param_update()
        choices = conf.get("options") or []
        default_value = visible_values.get(param_name, conf.get("default"))
        if default_value not in choices and choices:
            default_value = choices[0]
        return gr.update(
            visible=True,
            choices=choices,
            value=default_value,
            label=conf.get("label", fallback_label),
            info=conf.get("description", ""),
            interactive=True,
        )

    temperature_update = slider_update("temperature", "temperature")
    top_p_update = slider_update("top_p", "top_p")
    max_tokens_update = number_update("max_completion_tokens", "max_completion_tokens")
    reasoning_update = dropdown_update("reasoning_effort", "reasoning_effort")
    verbosity_update = dropdown_update("verbosity", "verbosity")
    return (
        temperature_update,
        top_p_update,
        max_tokens_update,
        reasoning_update,
        verbosity_update,
        state_values,
    )


def update_model_parameter(
    param_name: str,
    value: Any,
    current_state: Optional[Dict[str, Any]],
    model_name: str,
):
    state = dict(current_state or {})
    if not model_name or model_name == "None":
        return state
    spec = get_model_parameters(model_name)
    conf = spec.get(param_name)
    if not _is_param_supported_for_client(conf):
        state.pop(param_name, None)
        return state
    if value is None or (isinstance(value, str) and not value.strip()):
        state.pop(param_name, None)
    else:
        state[param_name] = value
    state = _filter_supported_parameter_values(model_name, state)
    manager = ui_state.manager
    if manager:
        manager.set_model_parameters(state)
    return state


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


def respond_stream(message: str, media: Optional[Any] = None, meta_playbook: Optional[str] = None):
    manager = ui_state.manager
    if not manager:
        raise RuntimeError("Manager not initialised")

    logging.debug("[respond_stream] raw message=%r", message)

    metadata: Optional[Dict[str, Any]] = None
    if media:
        image_path = _extract_image_path(media)
        stored_info = _store_uploaded_image(image_path)
        if stored_info:
            metadata = {"media": [stored_info]}

    current_building_id = manager.user_current_building_id

    attachment_preview = metadata.get("media", []) if metadata else []
    logging.debug("[respond_stream] attachments prepared: %s", len(attachment_preview))

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
    stream = manager.handle_user_input_stream(message, metadata=metadata, meta_playbook=meta_playbook)
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

    # Mark the current building as read when user enters it
    current_building_id = manager.user_current_building_id
    if current_building_id:
        ui_state.mark_building_as_read(current_building_id)
        logging.debug("[ui] marked building %s as read", current_building_id)

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
    # Strip unread indicator if present
    clean_building_name = strip_unread_indicator(building_name) if building_name else building_name
    (
        history,
        new_location_name,
        location_markdown_update,
        summon_update,
        conversing_update,
        client_state,
    ) = _perform_user_move(clean_building_name, client_state)
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
    sanitized = model_name or "None"
    parameter_defaults: Dict[str, Any] = {}
    history: List[Dict[str, str]] = []
    if manager:
        if sanitized != "None":
            parameter_defaults = _filter_supported_parameter_values(
                sanitized, get_model_parameter_defaults(sanitized)
            )
        manager.set_model(sanitized, parameters=parameter_defaults)
        current_building_id = manager.user_current_building_id
        if current_building_id:
            raw_history = _limit_history(manager.get_building_history(current_building_id))
            history = format_history_for_chatbot(raw_history)
    updates = _build_parameter_updates(sanitized, parameter_defaults)
    return (history, *updates)


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


# ========================================
# Detail Panel Helper Functions
# ========================================

def get_building_details(building_id: str = None):
    """Get building details including occupants, items, and prompt."""
    manager = ui_state.manager
    if not manager:
        return {"occupants": [], "items": [], "prompt": ""}

    if building_id is None:
        building_id = manager.user_current_building_id

    if not building_id or building_id not in manager.building_map:
        return {"occupants": [], "items": [], "prompt": ""}

    building = manager.building_map[building_id]

    # Get occupants
    occupants_list = []
    if building_id in manager.occupancy_manager.occupants:
        occupant_ids = manager.occupancy_manager.occupants[building_id]
        # Sort to ensure stable order
        sorted_ids = sorted(occupant_ids) if occupant_ids else []
        for oid in sorted_ids:
            if oid in manager.personas:
                persona = manager.personas[oid]
                occupants_list.append({
                    "id": oid,
                    "name": persona.persona_name,
                })

    # Get items
    items_list = []
    if building_id in manager.items_by_building:
        item_ids = manager.items_by_building[building_id]
        # Sort to ensure stable order
        sorted_item_ids = sorted(item_ids) if item_ids else []
        for item_id in sorted_item_ids:
            if item_id in manager.item_registry:
                item_data = manager.item_registry[item_id]
                raw_name = item_data.get("name", "") or ""
                display_name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                items_list.append({
                    "id": item_id,
                    "name": display_name,
                    "description": item_data.get("description", ""),
                    "type": item_data.get("type", "object"),
                    "file_path": item_data.get("file_path"),
                })

    # Get prompt
    prompt = building.system_instruction or ""

    return {
        "occupants": occupants_list,
        "items": items_list,
        "prompt": prompt,
    }


def get_persona_details(persona_id: str = None):
    """Get persona details including inventory, active thread, and active task."""
    manager = ui_state.manager
    if not manager:
        return {"inventory": [], "thread": "", "task": None}

    # If persona_id not specified, try to get the first persona in current building
    if persona_id is None:
        building_id = manager.user_current_building_id
        if building_id and building_id in manager.occupancy_manager.occupants:
            occupants = manager.occupancy_manager.occupants[building_id]
            # Find first persona (not user)
            for oid in occupants:
                if oid in manager.personas:
                    persona_id = oid
                    break

    if not persona_id or persona_id not in manager.personas:
        return {"inventory": [], "thread": "", "task": None}

    persona = manager.personas[persona_id]

    # Get inventory
    inventory_list = []
    # Sort to ensure stable order
    sorted_inventory = sorted(persona.inventory_item_ids) if persona.inventory_item_ids else []
    for item_id in sorted_inventory:
        if item_id in manager.items:
            item_data = manager.items[item_id]
            raw_name = item_data.get("name", "") or ""
            display_name = raw_name.strip() if raw_name.strip() else "(名前なし)"
            inventory_list.append({
                "id": item_id,
                "name": display_name,
                "description": item_data.get("description", ""),
                "type": item_data.get("type", "object"),
                "file_path": item_data.get("file_path"),
            })

    # Get active thread
    active_thread = ""
    if hasattr(persona, "sai_memory") and persona.sai_memory:
        try:
            active_thread = persona.sai_memory.get_current_thread() or "main"
        except Exception:
            active_thread = "main"

    # Get active task
    active_task = None
    try:
        task_info = persona.task_storage.get_active_task()
        if task_info:
            active_task = {
                "id": task_info.get("task_id"),
                "title": task_info.get("title", ""),
                "description": task_info.get("description", ""),
            }
    except Exception:
        pass

    return {
        "persona_id": persona_id,
        "persona_name": persona.persona_name,
        "inventory": inventory_list,
        "thread": active_thread,
        "task": active_task,
    }


def get_execution_states():
    """Get execution states for all personas in current building."""
    manager = ui_state.manager
    if not manager:
        return []

    building_id = manager.user_current_building_id
    if not building_id or building_id not in manager.occupancy_manager.occupants:
        return []

    states = []
    occupant_ids = manager.occupancy_manager.occupants[building_id]
    # Sort to ensure stable order
    sorted_occupants = sorted(occupant_ids) if occupant_ids else []
    for oid in sorted_occupants:
        if oid in manager.personas:
            persona = manager.personas[oid]
            exec_state = persona.get_execution_state()
            states.append({
                "persona_id": oid,
                "persona_name": persona.persona_name,
                "playbook": exec_state.get("playbook"),
                "node": exec_state.get("node"),
                "status": exec_state.get("status", "idle"),
            })

    return states


def format_building_details():
    """Format building details for display in UI (HTML)."""
    import html
    details = get_building_details()

    result = "<div class='building-details'>"

    # Format occupants
    result += "<h3>建物内のペルソナ</h3>"
    if details["occupants"]:
        result += "<ul>"
        for occ in details["occupants"]:
            result += f"<li><strong>{html.escape(occ['name'])}</strong> (<code>{html.escape(occ['id'])}</code>)</li>"
        result += "</ul>"
    else:
        result += "<p><em>(誰もいません)</em></p>"

    # Format items
    result += "<h3>建物内のアイテム</h3>"
    if details["items"]:
        result += "<ul class='item-list'>"
        for item in details["items"]:
            desc = item['description'] or "(説明なし)"
            item_type = item.get('type', 'object')
            file_path = item.get('file_path', '')
            # クリッカブル＆ツールチップ付きアイテム
            result += f"""<li>
                <span class='item-link'
                      data-item-id='{html.escape(item['id'])}'
                      data-item-name='{html.escape(item['name'])}'
                      data-item-desc='{html.escape(desc)}'
                      data-item-type='{html.escape(item_type)}'
                      data-file-path='{html.escape(file_path or "")}'
                      title='ID: {html.escape(item['id'])}&#10;{html.escape(desc)}'>
                    {html.escape(item['name'])}
                </span>
            </li>"""
        result += "</ul>"
    else:
        result += "<p><em>(アイテムがありません)</em></p>"

    # Format prompt
    result += "<h3>システムプロンプト</h3>"
    if details["prompt"]:
        result += f"<pre><code>{html.escape(details['prompt'])}</code></pre>"
    else:
        result += "<p><em>(プロンプトが設定されていません)</em></p>"

    result += "</div>"
    return result


def format_persona_details():
    """Format persona details for display in UI (HTML)."""
    import html
    details = get_persona_details()

    if not details.get("persona_id"):
        return "<p><em>(ペルソナが選択されていません)</em></p>"

    result = "<div class='persona-details'>"
    result += f"<h2>{html.escape(details['persona_name'])}</h2>"

    # Format inventory
    result += "<h3>インベントリ</h3>"
    if details["inventory"]:
        result += "<ul class='item-list'>"
        for item in details["inventory"]:
            desc = item['description'] or "(説明なし)"
            item_type = item.get('type', 'object')
            file_path = item.get('file_path', '')
            # クリッカブル＆ツールチップ付きアイテム
            result += f"""<li>
                <span class='item-link'
                      data-item-id='{html.escape(item['id'])}'
                      data-item-name='{html.escape(item['name'])}'
                      data-item-desc='{html.escape(desc)}'
                      data-item-type='{html.escape(item_type)}'
                      data-file-path='{html.escape(file_path or "")}'
                      title='ID: {html.escape(item['id'])}&#10;{html.escape(desc)}'>
                    {html.escape(item['name'])}
                </span>
            </li>"""
        result += "</ul>"
    else:
        result += "<p><em>(アイテムを持っていません)</em></p>"

    # Format thread
    result += f"<h3>アクティブスレッド</h3><p><code>{html.escape(details['thread'])}</code></p>"

    # Format task
    result += "<h3>アクティブタスク</h3>"
    if details["task"]:
        task = details["task"]
        result += f"<p><strong>{html.escape(task['title'])}</strong> (<code>{html.escape(task['id'])}</code>)</p>"
        if task['description']:
            result += f"<blockquote>{html.escape(task['description'])}</blockquote>"
    else:
        result += "<p><em>(タスクがありません)</em></p>"

    result += "</div>"
    return result


def format_execution_states():
    """Format execution states for display in UI (HTML)."""
    import html
    states = get_execution_states()

    if not states:
        return "<p><em>(実行中のペルソナがいません)</em></p>"

    result = "<div class='execution-states'>"
    for state in states:
        status_emoji = {
            "idle": "⚪",
            "running": "🔄",
            "waiting": "⏸️",
            "completed": "✅"
        }.get(state["status"], "❓")

        result += f"<h3>{status_emoji} {html.escape(state['persona_name'])}</h3>"

        if state["status"] == "idle":
            result += "<p><em>(待機中)</em></p>"
        else:
            if state["playbook"]:
                result += f"<p><strong>Playbook:</strong> <code>{html.escape(state['playbook'])}</code></p>"
            if state["node"]:
                result += f"<p><strong>Node:</strong> <code>{html.escape(state['node'])}</code></p>"
            result += f"<p><strong>Status:</strong> {html.escape(state['status'])}</p>"

    result += "</div>"
    return result


# Cache for detail panel updates to reduce flicker
_detail_cache = {
    "building": None,
    "persona": None,
    "execution": None
}

def update_detail_panels():
    """Update detail panels only if content has changed."""
    import logging
    global _detail_cache

    LOGGER = logging.getLogger(__name__)

    building = format_building_details()
    persona = format_persona_details()
    execution = format_execution_states()

    # Debug logging
    building_changed = building != _detail_cache["building"]
    persona_changed = persona != _detail_cache["persona"]
    execution_changed = execution != _detail_cache["execution"]

    if building_changed or persona_changed or execution_changed:
        LOGGER.debug(f"[Detail Panel Update] Building changed: {building_changed}, Persona changed: {persona_changed}, Execution changed: {execution_changed}")
        # Update cache
        _detail_cache["building"] = building
        _detail_cache["persona"] = persona
        _detail_cache["execution"] = execution

    # Always return current values (Gradio will handle rendering optimization)
    return building, persona, execution


# --- Unread message check functions for Timer ---

def check_and_update_unread():
    """
    Check for new messages in all buildings.
    Returns tuple: (has_changes, unread_building_ids, current_building_has_new)

    This function is called by gr.Timer periodically.
    It only triggers UI updates when there are actual changes.
    """
    manager = ui_state.manager
    if not manager:
        return False, set(), False

    # Check for newly unread buildings
    newly_unread = ui_state.check_for_new_messages()
    unread_buildings = ui_state.get_unread_buildings()

    # Check if current building has new messages
    current_building_id = manager.user_current_building_id
    current_has_new = current_building_id in newly_unread if current_building_id else False

    has_changes = len(newly_unread) > 0

    if has_changes:
        logging.debug(
            "[unread] New messages detected: newly_unread=%s, total_unread=%s, current_has_new=%s",
            newly_unread, unread_buildings, current_has_new
        )

    return has_changes, unread_buildings, current_has_new


def get_building_choices_with_unread():
    """
    Get building choices with unread indicators.
    Returns list of building names with ● prefix for unread buildings.
    """
    manager = ui_state.manager
    if not manager:
        return []

    unread_buildings = ui_state.get_unread_buildings()
    choices = []

    for building in manager.buildings:
        name = building.name
        if building.building_id in unread_buildings:
            # Add unread indicator
            choices.append(f"● {name}")
        else:
            choices.append(name)

    return choices


def strip_unread_indicator(building_name: str) -> str:
    """Remove unread indicator from building name."""
    if building_name and building_name.startswith("● "):
        return building_name[2:]
    return building_name
