from __future__ import annotations

import asyncio

import discord

from utils.favorites import FavoriteStore
from utils.formatters import (
    build_error_embed,
    build_now_playing_embed,
    build_queue_embed,
    build_success_embed,
    build_track_embed,
)


class PlayerControlsView(discord.ui.View):
    def __init__(self, bot: discord.Client, guild_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.music = bot.music
        self.favorites = FavoriteStore()
        self.guild_id = guild_id
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        state = self.music.get_state(self.guild_id) if self.guild_id is not None else None
        pause_button = self.pause_resume
        skip_button = self.skip
        stop_button = self.stop
        autoplay_button = self.autoplay
        dashboard_button = self.dashboard
        like_button = self.like

        skip_button.label = "Skip"
        skip_button.style = discord.ButtonStyle.secondary
        stop_button.label = "Stop"
        stop_button.style = discord.ButtonStyle.danger
        dashboard_button.label = "Panel"
        dashboard_button.style = discord.ButtonStyle.secondary
        like_button.label = "Like"
        like_button.style = discord.ButtonStyle.secondary

        if state and state.voice_client and state.voice_client.is_paused():
            pause_button.label = "Resume"
            pause_button.style = discord.ButtonStyle.success
        else:
            pause_button.label = "Pause"
            pause_button.style = discord.ButtonStyle.primary

        autoplay_button.label = "Auto"
        autoplay_button.style = (
            discord.ButtonStyle.success if state and state.autoplay_enabled else discord.ButtonStyle.secondary
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await interaction.response.defer()
            return False
        state = self.music.get_state(interaction.guild_id)
        if state.current is None:
            if not interaction.response.is_done():
                await interaction.response.defer()
            if interaction.message:
                try:
                    await interaction.message.edit(view=None)
                except discord.HTTPException:
                    pass
            return False
        if state.active_panel and interaction.message and interaction.message.id != state.active_panel.message_id:
            if not interaction.response.is_done():
                await interaction.response.defer()
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, custom_id="aisak:pause_resume", row=0)
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            state = self.music.get_state(interaction.guild_id)
            await interaction.response.defer()
            if state.voice_client and state.voice_client.is_paused():
                heading = "Reproduccion reanudada"
                await self.music.resume(interaction.guild_id)
            else:
                heading = "Reproduccion pausada"
                await self.music.pause(interaction.guild_id)
            await self.music.refresh_active_panel(interaction.guild_id, heading=heading)
            await self._clear_if_stale(interaction)
        except Exception as exc:
            await self._send_error(interaction, str(exc))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, custom_id="aisak:skip", row=0)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer()
            await self.music.skip(interaction.guild_id, 1)
            await asyncio.sleep(0.35)
            state = self.music.get_state(interaction.guild_id)
            if state.current:
                await self.music.refresh_active_panel(interaction.guild_id, heading="Reproduccion iniciada")
            else:
                await self.music.disable_active_panel(interaction.guild_id)
            await self._clear_if_stale(interaction)
        except Exception as exc:
            await self._send_error(interaction, str(exc))

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="aisak:stop", row=0)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer()
            await self.music.stop(interaction.guild_id)
            if interaction.message:
                await interaction.message.edit(
                    embed=build_success_embed(
                        "Reproduccion detenida",
                        "Limpie la cola y me desconecte del canal de voz.",
                        self.bot.settings.bot_color,
                    ),
                    view=None,
                )
        except Exception as exc:
            await self._send_error(interaction, str(exc))

    @discord.ui.button(label="Auto", style=discord.ButtonStyle.secondary, custom_id="aisak:autoplay", row=1)
    async def autoplay(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        enabled = self.music.toggle_autoplay(interaction.guild_id)
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.defer()
        if state.current:
            await self.music.refresh_active_panel(interaction.guild_id, heading="Reproduccion iniciada")
            active_message = await self._active_message(interaction.guild_id)
            if active_message and active_message.embeds:
                embed = active_message.embeds[0].copy()
                embed.set_footer(text=f"AutoPlay {'activado' if enabled else 'desactivado'}")
                await active_message.edit(embed=embed, view=PlayerControlsView(self.bot, interaction.guild_id))
            await self._clear_if_stale(interaction)
            return

        if interaction.message:
            await interaction.message.edit(
                embed=build_success_embed(
                    "AutoPlay actualizado",
                    f"El AutoPlay quedo **{'activado' if enabled else 'desactivado'}**.",
                    self.bot.settings.bot_color,
                ),
                view=None,
            )

    @discord.ui.button(label="Panel", style=discord.ButtonStyle.secondary, custom_id="aisak:dashboard", row=1)
    async def dashboard(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.music.refresh_active_panel(interaction.guild_id, dashboard=True)
        await self._clear_if_stale(interaction)

    @discord.ui.button(label="Like", style=discord.ButtonStyle.secondary, custom_id="aisak:like", row=1)
    async def like(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.music.get_state(interaction.guild_id)
        if state.current is None:
            await interaction.response.defer()
            return

        saved = self.favorites.add(interaction.user.id, state.current)
        await interaction.response.defer()
        active_message = await self._active_message(interaction.guild_id)
        if active_message and active_message.embeds:
            embed = active_message.embeds[0].copy()
            embed.set_footer(text="Guardada en favoritos" if saved else "Ya estaba en favoritos")
            await active_message.edit(embed=embed, view=PlayerControlsView(self.bot, interaction.guild_id))
        await self._clear_if_stale(interaction)

    async def _send_error(self, interaction: discord.Interaction, message: str) -> None:
        state = self.music.get_state(interaction.guild_id) if interaction.guild_id else None
        if state and state.current:
            if not interaction.response.is_done():
                await interaction.response.defer()
            active_message = await self._active_message(interaction.guild_id)
            if active_message is not None:
                await active_message.edit(
                    embed=build_error_embed(message, self.bot.settings.bot_color),
                    view=PlayerControlsView(self.bot, interaction.guild_id),
                )
                await self._clear_if_stale(interaction)
                return
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    embed=build_error_embed(message, self.bot.settings.bot_color),
                    view=PlayerControlsView(self.bot, interaction.guild_id),
                )
                return
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _active_message(self, guild_id: int):
        state = self.music.get_state(guild_id)
        if state.active_panel is None:
            return None
        return await self.music._fetch_message(state.active_panel)

    async def _clear_if_stale(self, interaction: discord.Interaction) -> None:
        state = self.music.get_state(interaction.guild_id)
        if not interaction.message:
            return
        try:
            if state.active_panel is None:
                await interaction.message.edit(view=None)
                return
            if interaction.message.id == state.active_panel.message_id:
                return
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
