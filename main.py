"""
main.py — Entry point bot Discord TikTok Notifier.

Menjalankan bot, memuat semua Cogs, dan melakukan sync slash command.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

# ── Setup ────────────────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

COGS = [
    "cogs.tiktok",
]


class TikTokNotifBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Tidak butuh message_content karena kita pakai slash command
        super().__init__(command_prefix="!", intents=intents)

        # Instance database dibuat sekali, dipakai bersama oleh semua Cog
        self.db = Database("bot_data.db")

    async def setup_hook(self):
        """Dipanggil sekali sebelum bot connect — tempat ideal load cogs & sync."""
        # Inisialisasi tabel database
        self.db.init_db()
        log.info("Database siap.")

        # Load semua cogs
        for cog in COGS:
            await self.load_extension(cog)
            log.info("Cog dimuat: %s", cog)

        # Sync slash commands ke Discord
        # Tips: saat development, sync ke guild spesifik jauh lebih cepat.
        dev_guild_id = os.getenv("DEV_GUILD_ID")
        if dev_guild_id:
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Slash commands di-sync ke guild dev (%d command).", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Slash commands di-sync secara global (%d command).", len(synced))

    async def on_ready(self):
        log.info("Bot login sebagai %s (ID: %s)", self.user, self.user.id)
        log.info("Terhubung ke %d server.", len(self.guilds))


async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN tidak ditemukan. Pastikan file .env sudah dibuat "
            "dan berisi DISCORD_TOKEN=..."
        )

    bot = TikTokNotifBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot dimatikan manual (Ctrl+C).")
