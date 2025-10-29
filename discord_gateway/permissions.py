from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .mapping import ChannelContext, ChannelMapping


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str | None = None


class InvitationRegistry:
    def __init__(self) -> None:
        self._by_channel: dict[str, set[str]] = defaultdict(set)

    def register(self, channel_id: str, discord_user_id: str) -> None:
        self._by_channel[channel_id].add(discord_user_id)

    def unregister(self, channel_id: str, discord_user_id: str) -> None:
        self._by_channel[channel_id].discard(discord_user_id)

    def clear_channel(self, channel_id: str) -> None:
        self._by_channel.pop(channel_id, None)

    def is_invited(self, channel_id: str, discord_user_id: str) -> bool:
        return discord_user_id in self._by_channel.get(channel_id, set())


class PermissionPolicy:
    def __init__(self, mapping: ChannelMapping, invitations: InvitationRegistry | None = None):
        self.mapping = mapping
        self.invitations = invitations or InvitationRegistry()

    def evaluate(
        self,
        context: ChannelContext,
        *,
        discord_user_id: str,
        roles: Iterable[str] | Mapping[str, Iterable[str]] | None,
    ) -> PermissionDecision:
        if discord_user_id == context.host_user_id:
            return PermissionDecision(True)

        role_tokens = self._build_token_set(roles)
        allowed_tokens = self._build_token_set(context.allowed_roles)
        if allowed_tokens and role_tokens.intersection(allowed_tokens):
            return PermissionDecision(True)

        if not context.allowed_roles and not context.invite_required:
            return PermissionDecision(True)

        if self.invitations.is_invited(context.channel_id, discord_user_id):
            return PermissionDecision(True)

        return PermissionDecision(False, "invite_required")

    def register_invite(self, channel_id: str, discord_user_id: str) -> None:
        self.invitations.register(channel_id, discord_user_id)

    def revoke_invite(self, channel_id: str, discord_user_id: str) -> None:
        self.invitations.unregister(channel_id, discord_user_id)

    def clear_invites(self, channel_id: str) -> None:
        self.invitations.clear_channel(channel_id)

    @staticmethod
    def _build_token_set(source) -> set[str]:
        tokens: set[str] = set()

        def collect(value) -> None:
            if value is None:
                return
            if isinstance(value, str):
                tokens.add(value)
                tokens.add(value.lower())
                return
            if isinstance(value, Mapping):
                for item in value.values():
                    collect(item)
                return
            if isinstance(value, Iterable):
                for item in value:
                    collect(item)
                return
            text = str(value)
            tokens.add(text)
            tokens.add(text.lower())

        collect(source)
        return tokens
