"""
cogs/tiktok.py — Fitur inti: registrasi akun TikTok & loop notifikasi. (REVISI v3)

Fitur Baru:
1. Menangani akun yang benar-benar kosong (0 video) menggunakan status "EMPTY".
2. Sistem API Key Rotation (Failover) - pindah ke key berikutnya jika limit (429/403).
"""

import asyncio
import logging
import os
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("cogs.tiktok")

# ── Konfigurasi API (ScrapTik via RapidAPI) ──────────────────────────────────
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "scraptik.p.rapidapi.com")
VIDEO_ENDPOINT = f"https://{RAPIDAPI_HOST}/user/posts"
# LIVE_ENDPOINT = f"https://{RAPIDAPI_HOST}/user/live"  # ⏸️ belum dipakai

CHECK_INTERVAL_MINUTES = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)  # detik
DELAY_BETWEEN_USERS = 2  # detik jeda antar akun, biar tidak kena rate limit


class TikTokCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.notif_channel_id = int(os.getenv("NOTIF_CHANNEL_ID", "0"))
        self.session: Optional[aiohttp.ClientSession] = None
        
        # --- ROTASI API KEY ---
        # Pastikan di file .env lu nulisnya: RAPIDAPI_KEYS=key1,key2,key3
        keys_env = os.getenv("RAPIDAPI_KEYS", "")
        self.api_keys = [k.strip() for k in keys_env.split(",") if k.strip()]
        self.current_key_index = 0
        if not self.api_keys:
            log.warning("RAPIDAPI_KEYS kosong! Bot tidak bisa mengambil data.")

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
        import re
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
    # BACKGROUND TASK — tiap 10 menit
    # ═════════════════════════════════════════════════════════════════════════
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
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

    # ── Logika pengecekan per akun ───────────────────────────────────────────
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
    async def _api_get(self, url: str, params: dict, retries=0) -> Optional[dict]:
        """GET request dengan fitur auto-ganti API Key kalau limit."""
        if not self.api_keys:
            return None

        # Kalau udah nyoba sebanyak jumlah key yang kita punya tapi gagal semua
        if retries >= len(self.api_keys):
            log.error("Semua API Key udah limit/habis! Skip request dulu.")
            return None

        # Ambil key yang aktif sekarang
        current_key = self.api_keys[self.current_key_index]
        
        # Header dinamis
        headers = {
            "X-RapidAPI-Key": current_key,
            "X-RapidAPI-Host": RAPIDAPI_HOST,
        }

        try:
            async with self.session.get(url, params=params, headers=headers) as resp:
                # 429 = Too Many Requests, 403 = Forbidden (Kuota bulanan habis)
                if resp.status in (429, 403):
                    log.warning(f"API Key index-{self.current_key_index} limit. Pindah ke key berikutnya...")
                    self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
                    return await self._api_get(url, params, retries + 1)

                if resp.status == 404:
                    log.info("Data tidak ditemukan (404) untuk: %s", params)
                    return None
                if resp.status != 200:
                    log.warning("API balas status %s untuk %s", resp.status, params)
                    return None
                    
                return await resp.json(content_type=None)

        except asyncio.TimeoutError:
            log.warning("Timeout request API untuk %s", params)
        except aiohttp.ClientError as e:
            log.warning("Error koneksi API untuk %s: %r", params, e)
        except ValueError:
            log.warning("Response API bukan JSON valid untuk %s", params)
            
        return None

    async def _fetch_latest_video(
        self, username: str
    ) -> Optional[tuple[str, str, str]]:
        data = await self._api_get(
            VIDEO_ENDPOINT, {"unique_id": username, "count": 10}
        )
        if not data:
            return None

        try:
            videos = data.get("aweme_list") or data.get("data", {}).get("videos")
            
            if videos == []:  
                return "EMPTY", "", ""
                
            if not videos:
                return None

            latest = videos[0]
            video_id = str(latest.get("aweme_id") or latest.get("video_id"))
            video_desc = latest.get("desc", "") or latest.get("title", "")
            video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"
            
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