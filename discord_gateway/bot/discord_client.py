from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import BotSettings
from .router import MessageRouter

logger = logging.getLogger(__name__)


class SAIVerseDiscordClient(commands.Bot):
    """Discord bot client that forwards events to the gateway."""

    def __init__(self, settings: BotSettings, router: MessageRouter):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.router = router
        self.router.attach_discord_client(self)
        self.add_command(self.saiverse)

    async def setup_hook(self) -> None:
        logger.info("SAIVerse Discord bot setup complete.")

    async def on_ready(self) -> None:
        logger.info(
            "SAIVerse Discord bot logged in as %s (id=%s)",
            self.user,
            self.user.id if self.user else "unknown",
        )

    async def on_message(self, message: discord.Message) -> None:
        await super().on_message(message)
        await self.router.handle_message(message)

    @commands.group(name="saiverse", invoke_without_command=True)
    async def saiverse(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Available subcommands: invite grant, invite revoke, invite clear.")

    @saiverse.group(name="invite", invoke_without_command=True)
    async def saiverse_invite(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Usage: !saiverse invite grant @member [#channel]\n"
                "       !saiverse invite revoke @member [#channel]\n"
                "       !saiverse invite clear [#channel]"
            )

    @saiverse_invite.command(name="grant")
    async def saiverse_invite_grant(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._handle_invite_command(ctx, action="grant", member=member, channel=channel)

    @saiverse_invite.command(name="revoke")
    async def saiverse_invite_revoke(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._handle_invite_command(ctx, action="revoke", member=member, channel=channel)

    @saiverse_invite.command(name="clear")
    async def saiverse_invite_clear(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._handle_invite_command(ctx, action="clear", channel=channel)

    async def _handle_invite_command(
        self,
        ctx: commands.Context,
        *,
        action: str,
        member: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target_channel = channel or (
            ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        )
        if target_channel is None:
            await ctx.send("This command must be used in a server text channel.")
            return

        owner_id = self.router.get_owner_id(target_channel.id)
        if not owner_id:
            await ctx.send("This channel is not linked to a SAIVerse City.")
            return

        if owner_id != str(ctx.author.id):
            await ctx.send("Only the City host can manage invites for this channel.")
            return

        target_id = str(member.id) if member else None
        if action in {"grant", "revoke"} and not target_id:
            await ctx.send("You must mention a member for this command.")
            return

        dispatched = await self.router.emit_invite_event(
            owner_discord_id=owner_id,
            channel_id=target_channel.id,
            action=action,
            target_discord_id=target_id,
        )
        if not dispatched:
            await ctx.send(
                "The SAIVerse gateway for this City is currently offline. "
                "Please ensure the host application is running."
            )
            return

        if action == "grant" and member:
            await ctx.send(f"Granted invite for {member.mention} in {target_channel.mention}.")
        elif action == "revoke" and member:
            await ctx.send(f"Revoked invite for {member.mention} in {target_channel.mention}.")
        elif action == "clear":
            await ctx.send(f"Cleared all invites for {target_channel.mention}.")
        else:
            await ctx.send("Invite command completed.")
