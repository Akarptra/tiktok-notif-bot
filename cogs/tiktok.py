"""
cogs/tiktok.py — Fitur inti: registrasi akun TikTok & loop notifikasi. (REVISI v4)

Perubahan dari v3 (migrasi ScrapTik/RapidAPI → Social Fetch API):
1. Endpoint & header diganti ke Social Fetch:
   GET https://api.socialfetch.dev/v1/tiktok/profiles/{username}/videos?sortBy=latest
   Header: {"x-api-key": SOCIALFETCH_API_KEY}
2. Sistem rotasi API key (RAPIDAPI_KEYS) DIHAPUS — Social Fetch cukup 1 key.
3. Interval loop diubah: 10 menit → 1x24 jam (hemat kredit API).
4. Parser _fetch_latest_video disesuaikan dengan struktur response Social Fetch.
   Logika status "EMPTY" (akun tanpa video) TETAP DIPERTAHANKAN.
5. Logika iterasi multi-akun di dalam loop TIDAK DIUBAH.

.env yang dibutuhkan sekarang:
    SOCIALFETCH_API_KEY=key_kamu
    NOTIF_CHANNEL_ID=...
(RAPIDAPI_KEYS dan RAPIDAPI_HOST sudah tidak dipakai, boleh dihapus dari .env)
"""

import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("cogs.tiktok")

# ── Konfigurasi API (Social Fetch) ───────────────────────────────────────────
SOCIALFETCH_BASE = "https://api.socialfetch.dev/v1/tiktok/profiles"
SOCIALFETCH_API_KEY = os.getenv("SOCIALFETCH_API_KEY", "")

API_HEADERS = {
    "x-api-key": SOCIALFETCH_API_KEY,
}

CHECK_INTERVAL_HOURS = 24  # 1x sehari — hemat kredit API
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)  # detik
DELAY_BETWEEN_USERS = 2  # detik jeda antar akun, biar tidak kena rate limit


class TikTokCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.notif_channel_id = int(os.getenv("NOTIF_CHANNEL_ID", "0"))
        self.session: Optional[aiohttp.ClientSession] = None

        if not SOCIALFETCH_API_KEY:
            log.warning(
                "SOCIALFETCH_API_KEY kosong! Bot tidak bisa mengambil data. "
                "Isi di file .env: SOCIALFETCH_API_KEY=..."
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        self.check_tiktok_updates.start()

    async def cog_unload(self):
        self.check_tiktok_updates.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ═════════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ═════════════════════════════════════════════════════════════════════════
    @app_commands.command(
        name="set_tiktok",
        description="Daftarkan satu atau banyak username TikTok sekaligus (pisahkan dengan koma/spasi).",
    )
    @app_commands.describe(usernames="Contoh: paoogultom, ic.darren3, akarptra")
    async def set_tiktok(self, interaction: discord.Interaction, usernames: str):
        raw_names = re.split(r'[,\s]+', usernames)

        berhasil = []
        gagal = []
        for uname in raw_names:
            uname = uname.strip().lstrip("@").lower()
            if not uname:
                continue
            if len(uname) <= 30 and all(c.isalnum() or c in "._" for c in uname):
                await asyncio.to_thread(self.db.set_tiktok_account, interaction.user.id, uname)
                berhasil.append(f"@{uname}")
            else:
                gagal.append(f"@{uname}")

        msg = ""
        if berhasil:
            msg += f"✅ **Berhasil ({len(berhasil)} akun):** {', '.join(berhasil)}\n"
        if gagal:
            msg += f"❌ **Gagal/Format Salah ({len(gagal)} akun):** {', '.join(gagal)}"

        if not msg:
            msg = "⚠️ Tidak ada username yang valid."

        await interaction.response.send_message(msg, ephemeral=True)
        log.info("Member %s mendaftarkan TikTok massal: %s", interaction.user, berhasil)

    @app_commands.command(
        name="remove_tiktok",
        description="Hapus salah satu akun TikTok kamu dari daftar notifikasi.",
    )
    @app_commands.describe(username="Username TikTok yang mau dihapus (tanpa @)")
    async def remove_tiktok(self, interaction: discord.Interaction, username: str):
        username = username.strip().lstrip("@").lower()

        row = await asyncio.to_thread(self.db.get_member, username)
        if row is None:
            await interaction.response.send_message(
                f"ℹ️ Akun **@{username}** tidak ditemukan di daftar.",
                ephemeral=True,
            )
            return
        if row["discord_user_id"] != str(interaction.user.id):
            await interaction.response.send_message(
                "❌ Akun itu terdaftar atas nama member lain, kamu tidak bisa "
                "menghapusnya.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(self.db.remove_member, username)
        await interaction.response.send_message(
            f"✅ Akun **@{username}** sudah dihapus dari daftar notifikasi.",
            ephemeral=True,
        )

    @app_commands.command(
        name="list_tiktok",
        description="Lihat semua akun TikTok yang kamu daftarkan.",
    )
    async def list_tiktok(self, interaction: discord.Interaction):
        rows = await asyncio.to_thread(
            self.db.get_accounts_by_user, interaction.user.id
        )
        if not rows:
            await interaction.response.send_message(
                "ℹ️ Kamu belum mendaftarkan akun TikTok apa pun.",
                ephemeral=True,
            )
            return
        daftar = "\n".join(f"• **@{r['tiktok_username']}**" for r in rows)
        await interaction.response.send_message(
            f"📋 Akun TikTok kamu yang terdaftar:\n{daftar}",
            ephemeral=True,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # BACKGROUND TASK — 1x24 jam (sehari sekali)
    # ═════════════════════════════════════════════════════════════════════════
    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def check_tiktok_updates(self):
        members = await asyncio.to_thread(self.db.get_all_members)
        if not members:
            return

        channel = self.bot.get_channel(self.notif_channel_id)
        if channel is None:
            log.warning("Channel notifikasi tidak ditemukan.")
            return

        log.info("Mulai pengecekan %d akun TikTok...", len(members))

        for row in members:
            try:
                await self._check_single_member(row, channel)
            except Exception:
                log.exception("Error saat mengecek @%s", row["tiktok_username"])
            await asyncio.sleep(DELAY_BETWEEN_USERS)

        log.info("Pengecekan selesai.")

    @check_tiktok_updates.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @check_tiktok_updates.error
    async def on_loop_error(self, error: BaseException):
        log.exception("Loop pengecekan crash: %r — restart dalam 60 detik.", error)
        await asyncio.sleep(60)
        self.check_tiktok_updates.restart()

    # ── Logika pengecekan per akun (TIDAK DIUBAH) ────────────────────────────
    async def _check_single_member(
        self, row, channel: discord.abc.Messageable
    ) -> None:
        username = row["tiktok_username"]
        user_id = row["discord_user_id"]

        video = await self._fetch_latest_video(username)
        if video is not None:
            video_id, video_url, video_desc = video

            # KONDISI 1: Akun ini nggak punya video sama sekali (Kosong)
            if video_id == "EMPTY":
                if row["last_video_id"] != "EMPTY":
                    await asyncio.to_thread(self.db.update_last_video, username, "EMPTY")
                return

            # KONDISI 2: Pertama kali dicek dan ternyata ada videonya (Baseline)
            if row["last_video_id"] is None:
                await asyncio.to_thread(self.db.update_last_video, username, video_id)

            # KONDISI 3: Tadinya kosong ("EMPTY"), sekarang ada videonya (Upload Pertama!)
            elif row["last_video_id"] == "EMPTY":
                await self._send_video_notification(
                    channel, user_id, username, video_url, video_desc
                )
                await asyncio.to_thread(self.db.update_last_video, username, video_id)

            # KONDISI 4: Akun normal, ada video baru
            elif video_id != row["last_video_id"]:
                await self._send_video_notification(
                    channel, user_id, username, video_url, video_desc
                )
                await asyncio.to_thread(self.db.update_last_video, username, video_id)

    # ── API calls (aiohttp) ──────────────────────────────────────────────────
    async def _api_get(self, url: str, params: dict) -> Optional[dict]:
        """GET request ke Social Fetch dengan error handling lengkap.

        Return None jika gagal (timeout, rate limit, kunci invalid, dll.)
        sehingga pemanggil bisa skip dengan aman.
        """
        if not SOCIALFETCH_API_KEY:
            log.error("SOCIALFETCH_API_KEY belum di-set. Skip request.")
            return None

        try:
            async with self.session.get(url, params=params, headers=API_HEADERS) as resp:
                if resp.status == 429:
                    log.warning("Rate limit Social Fetch tercapai (429). Skip: %s", url)
                    return None
                if resp.status in (401, 403):
                    log.error(
                        "API key ditolak (%s). Cek SOCIALFETCH_API_KEY atau sisa kredit.",
                        resp.status,
                    )
                    return None
                if resp.status == 404:
                    log.info("Profil tidak ditemukan (404): %s", url)
                    return None
                if resp.status != 200:
                    log.warning("API balas status %s untuk %s", resp.status, url)
                    return None

                return await resp.json(content_type=None)

        except asyncio.TimeoutError:
            log.warning("Timeout request API untuk %s", url)
        except aiohttp.ClientError as e:
            log.warning("Error koneksi API untuk %s: %r", url, e)
        except ValueError:
            log.warning("Response API bukan JSON valid untuk %s", url)

        return None

    async def _fetch_latest_video(
        self, username: str
    ) -> Optional[tuple[str, str, str]]:
        """Ambil video terbaru user via Social Fetch API.

        Endpoint: /v1/tiktok/profiles/{username}/videos?sortBy=latest
        Return:
        - (video_id, url, deskripsi)  → normal
        - ("EMPTY", "", "")           → profil valid tapi 0 video
        - None                        → request gagal / response tak terduga

        ⚠️ Verifikasi struktur response asli di dokumentasi/playground Social
        Fetch. Parser di bawah mencoba beberapa pola umum (data.videos,
        videos, items, data sebagai list) dan beberapa nama field ID/desc/url
        yang lazim. Kalau ada mismatch, sesuaikan bagian ini saja.
        """
        url = f"{SOCIALFETCH_BASE}/{username}/videos"
        data = await self._api_get(url, {"sortBy": "latest"})
        if not data:
            return None

        try:
            # Cari list video-nya — coba beberapa pola umum.
            # PENTING: cek pakai `is not None` / isinstance, BUKAN operator `or`,
            # karena list kosong [] itu falsy — kalau pakai `or`, status EMPTY
            # (profil valid tanpa video) akan tertelan.
            if isinstance(data, list):
                videos = data
            else:
                videos = None
                inner = data.get("data")
                for candidate in (
                    data.get("videos"),
                    inner if isinstance(inner, list) else None,
                    (inner or {}).get("videos") if isinstance(inner, dict) else None,
                    data.get("items"),
                ):
                    if isinstance(candidate, list):
                        videos = candidate
                        break

            # Profil valid tapi memang belum punya video sama sekali
            if videos == []:
                return "EMPTY", "", ""

            if not videos:
                return None

            latest = videos[0]

            video_id = str(
                latest.get("id")
                or latest.get("videoId")
                or latest.get("video_id")
                or ""
            )
            video_desc = (
                latest.get("description")
                or latest.get("desc")
                or latest.get("caption")
                or ""
            )
            # Pakai URL dari response kalau ada; kalau tidak, bangun manual
            video_url = (
                latest.get("url")
                or latest.get("shareUrl")
                or latest.get("share_url")
                or f"https://www.tiktok.com/@{username}/video/{video_id}"
            )

            if video_id in ("None", ""):
                log.warning("ID video tidak ditemukan di response (@%s)", username)
                return None

            return video_id, video_url, video_desc

        except (KeyError, IndexError, TypeError, AttributeError) as e:
            log.warning("Struktur response video tak terduga (@%s): %r", username, e)
            return None

    # ── Pengiriman notifikasi ────────────────────────────────────────────────
    async def _send_video_notification(
        self, channel, discord_user_id: str, username: str, url: str, desc: str
    ):
        content = f"@here 🎬 **Video TikTok Baru!** dari @{username}\n\n{url}"
        try:
            await channel.send(content)
            log.info("Notif link TikTok terkirim: @%s", username)
        except discord.HTTPException:
            log.exception("Gagal mengirim notif @%s", username)


async def setup(bot: commands.Bot):
    await bot.add_cog(TikTokCog(bot))