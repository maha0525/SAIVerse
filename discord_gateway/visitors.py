from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class VisitorProfile:
    """訪問中ペルソナの状態情報。"""

    discord_user_id: str
    persona_id: str
    owner_user_id: str
    current_city_id: str
    current_building_id: str
    metadata: dict = field(default_factory=dict)


class VisitorRegistry:
    """訪問中ペルソナを追跡するレジストリ。"""

    def __init__(self, visitors: Iterable[VisitorProfile] | None = None):
        self._by_discord: dict[str, VisitorProfile] = {}
        self._by_persona: dict[str, VisitorProfile] = {}
        if visitors:
            for profile in visitors:
                self.register(profile)

    def register(self, profile: VisitorProfile) -> None:
        self._by_discord[profile.discord_user_id] = profile
        self._by_persona[profile.persona_id] = profile

    def update_location(self, persona_id: str, *, city_id: str, building_id: str) -> None:
        profile = self._by_persona.get(persona_id)
        if not profile:
            raise KeyError(f"Persona '{persona_id}' is not registered as visitor.")
        profile.current_city_id = city_id
        profile.current_building_id = building_id

    def unregister_by_discord(self, discord_user_id: str) -> VisitorProfile | None:
        profile = self._by_discord.pop(discord_user_id, None)
        if profile:
            self._by_persona.pop(profile.persona_id, None)
        return profile

    def unregister_by_persona(self, persona_id: str) -> VisitorProfile | None:
        profile = self._by_persona.pop(persona_id, None)
        if profile:
            self._by_discord.pop(profile.discord_user_id, None)
        return profile

    def get_by_discord(self, discord_user_id: str) -> VisitorProfile | None:
        return self._by_discord.get(discord_user_id)

    def get_by_persona(self, persona_id: str) -> VisitorProfile | None:
        return self._by_persona.get(persona_id)

    def list_in_city(self, city_id: str) -> Iterable[VisitorProfile]:
        return (
            profile for profile in self._by_persona.values() if profile.current_city_id == city_id
        )
