"""User state management mixin extracted from saiverse_manager.py."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from manager.state import CoreState

LOGGER = logging.getLogger(__name__)


class UserStateMixin:
    """Manages user presence and profile state."""

    # These attributes are expected to be provided by the main class
    state: "CoreState"
    default_avatar: str
    city_host_avatar_path: Optional[str]

    def _refresh_user_state_cache(self) -> None:
        """Mirror CoreState's user info onto the manager-level attributes."""
        self.user_presence_status = self.state.user_presence_status
        self.user_is_online = self.state.user_presence_status != "offline"  # Backward compat
        self.user_display_name = self.state.user_display_name
        self.user_current_building_id = self.state.user_current_building_id
        self.user_current_city_id = self.state.user_current_city_id
        self.user_avatar_data = getattr(self.state, "user_avatar_data", None) or self.default_avatar

    def reload_user_profile(self) -> None:
        """Reload the user's profile (name/avatar) from the database."""
        self._load_user_state_from_db()
        try:
            from ui import chat as chat_ui

            if hasattr(chat_ui, "reset_user_avatar_cache"):
                chat_ui.reset_user_avatar_cache()
        except ImportError:
            pass

    def reload_host_avatar(self, avatar_path: Optional[str]) -> None:
        """Refresh the host avatar asset from the given path."""
        self.city_host_avatar_path = avatar_path
        data = None
        if avatar_path:
            data = self._load_avatar_data(Path(avatar_path))
        self.host_avatar = data or self.default_avatar
        self.state.host_avatar = self.host_avatar

    def _load_user_state_from_db(self) -> None:
        """Load user state from the database."""
        if getattr(self, "runtime", None) is not None:
            self.runtime.load_user_state_from_db()
        else:
            from database.models import User as UserModel
            
            db = self.SessionLocal()
            try:
                user = (
                    db.query(UserModel)
                    .filter(UserModel.USERID == self.state.user_id)
                    .first()
                )
                if user:
                    # Map DB boolean to presence status string
                    self.state.user_presence_status = "online" if user.LOGGED_IN else "offline"
                    self.state.user_current_city_id = user.CURRENT_CITYID
                    self.state.user_current_building_id = user.CURRENT_BUILDINGID
                    self.state.user_display_name = (
                        (user.USERNAME or "ユーザー").strip() or "ユーザー"
                    )
                    avatar_data = None
                    if getattr(user, "AVATAR_IMAGE", None):
                        avatar_data = self._load_avatar_data(Path(user.AVATAR_IMAGE))
                    self.state.user_avatar_data = avatar_data or self.default_avatar
                    self.id_to_name_map[str(self.state.user_id)] = (
                        self.state.user_display_name
                    )
                else:
                    self.state.user_presence_status = "offline"
                    self.state.user_current_building_id = None
                    self.state.user_current_city_id = None
                    self.state.user_display_name = "ユーザー"
                    self.state.user_avatar_data = self.default_avatar
                    self.id_to_name_map[str(self.state.user_id)] = (
                        self.state.user_display_name
                    )
            except Exception as exc:
                LOGGER.error(
                    "Failed to load user status from DB: %s", exc, exc_info=True
                )
                self.state.user_presence_status = "offline"
                self.state.user_current_building_id = None
                self.state.user_current_city_id = None
                self.state.user_display_name = "ユーザー"
                self.state.user_avatar_data = self.default_avatar
                self.id_to_name_map[str(self.state.user_id)] = (
                    self.state.user_display_name
                )
            finally:
                db.close()
        self._refresh_user_state_cache()


__all__ = ["UserStateMixin"]
