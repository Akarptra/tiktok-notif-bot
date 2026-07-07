"""
cogs/tiktok.py — Fitur inti: registrasi akun TikTok & loop notifikasi. (REVISI v2)

Perubahan dari v1:
1. Parameter API disesuaikan dengan ScrapTik: {"unique_id": username, "count": 10}
   (sebelumnya {"username": ...} → menyebabkan 404).
2. Semua pemanggilan database sekarang keyed by `tiktok_username`
   (menyesuaikan skema baru di database.py di mana username = PRIMARY KEY).
3. Fitur pengecekan LIVE dinonaktifkan sementara (di-comment) karena endpoint
   live di ScrapTik belum ditemukan. Kodenya dibiarkan utuh agar mudah
   diaktifkan kembali nanti.
4. /set_tiktok kini mendukung banyak akun per user; /remove_tiktok menerima
   parameter username agar user bisa memilih akun mana yang dihapus.
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
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "YOUR_RAPIDAPI_KEY_HERE")

VIDEO_ENDPOINT = f"https://{RAPIDAPI_HOST}/user/posts"
# LIVE_ENDPOINT = f"https://{RAPIDAPI_HOST}/user/live"  # ⏸️ belum dipakai — endpoint live belum ditemukan

API_HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}

CHECK_INTERVAL_MINUTES = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)  # detik
DELAY_BETWEEN_USERS = 2  # detik jeda antar akun, biar tidak kena rate limit


class TikTokCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.notif_channel_id = int(os.getenv("NOTIF_CHANNEL_ID", "0"))
        self.session: Optional[aiohttp.ClientSession] = None

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

        # Pastikan akun ini memang milik user yang memanggil command
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
            log.warning(
                "Channel notifikasi (ID: %s) tidak ditemukan. "
                "Cek NOTIF_CHANNEL_ID di .env dan pastikan bot punya akses.",
                self.notif_channel_id,
            )
            return

        log.info("Mulai pengecekan %d akun TikTok...", len(members))

        for row in members:
            # Error per-akun diisolasi: satu akun gagal, sisanya tetap dicek.
            try:
                await self._check_single_member(row, channel)
            except Exception:
                log.exception(
                    "Error tak terduga saat mengecek @%s", row["tiktok_username"]
                )
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

        # 1) Cek video terbaru
        video = await self._fetch_latest_video(username)
        if video is not None:
            video_id, video_url, video_desc = video
            if row["last_video_id"] is None:
                # Pertama kali dicek: simpan baseline saja, JANGAN notif,
                # supaya video lama tidak dianggap "baru".
                await asyncio.to_thread(self.db.update_last_video, username, video_id)
            elif video_id != row["last_video_id"]:
                await self._send_video_notification(
                    channel, user_id, username, video_url, video_desc
                )
                await asyncio.to_thread(self.db.update_last_video, username, video_id)

        # ─────────────────────────────────────────────────────────────────────
        # 2) Cek status live — ⏸️ DINONAKTIFKAN SEMENTARA
        # Endpoint live di ScrapTik belum ditemukan. Blok ini di-comment agar
        # loop tidak error. Aktifkan lagi (uncomment) + buka comment
        # LIVE_ENDPOINT di atas jika endpoint-nya sudah ketemu.
        # ─────────────────────────────────────────────────────────────────────
        # is_live_now = await self._fetch_live_status(username)
        # if is_live_now is not None:
        #     was_live = bool(row["is_live"])
        #     if is_live_now and not was_live:
        #         # Baru mulai live → notif sekali
        #         await self._send_live_notification(channel, user_id, username)
        #         await asyncio.to_thread(self.db.update_live_status, username, True)
        #     elif not is_live_now and was_live:
        #         # Live selesai → reset flag agar live berikutnya dinotif lagi
        #         await asyncio.to_thread(self.db.update_live_status, username, False)

    # ── API calls (aiohttp) ──────────────────────────────────────────────────
    async def _api_get(self, url: str, params: dict) -> Optional[dict]:
        """GET request generik dengan error handling lengkap.

        Return None jika gagal (timeout, rate limit, error server, dll.)
        sehingga pemanggil bisa skip dengan aman.
        """
        try:
            async with self.session.get(url, params=params, headers=API_HEADERS) as resp:
                if resp.status == 429:
                    log.warning("Rate limit RapidAPI tercapai (429). Skip: %s", params)
                    return None
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
        """Ambil video terbaru user via ScrapTik.

        Parameter sesuai spesifikasi ScrapTik: unique_id (bukan username) + count.
        Return (video_id, url, deskripsi) atau None.

        ⚠️ Cek struktur response asli di playground RapidAPI — umumnya ScrapTik
        mengembalikan list video di key `aweme_list` dengan ID di `aweme_id`.
        Sesuaikan parsing di bawah jika berbeda.
        """
        data = await self._api_get(
            VIDEO_ENDPOINT, {"unique_id": username, "count": 10}
        )
        if not data:
            return None

        try:
            # ScrapTik umumnya memakai struktur ala API internal TikTok:
            videos = data.get("aweme_list") or data.get("data", {}).get("videos") or []
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

    # async def _fetch_live_status(self, username: str) -> Optional[bool]:
    #     """⏸️ DINONAKTIFKAN — aktifkan kembali saat endpoint live ditemukan."""
    #     data = await self._api_get(LIVE_ENDPOINT, {"unique_id": username})
    #     if not data:
    #         return None
    #     try:
    #         return bool(data.get("data", {}).get("is_live", False))
    #     except (KeyError, TypeError) as e:
    #         log.warning("Struktur response live tak terduga (@%s): %r", username, e)
    #         return None

    # ── Pengiriman notifikasi ────────────────────────────────────────────────
    async def _send_video_notification(
        self, channel, discord_user_id: str, username: str, url: str, desc: str
    ):
        # Cukup kirim teks biasa, Discord bakal otomatis nampilin preview-nya
        content = f"@here 🎬 **Video TikTok Baru!** dari @{username}\n\n{url}"
        
        try:
            await channel.send(content)
            log.info("Notif link TikTok (non-embed) terkirim: @%s", username)
        except discord.HTTPException:
            log.exception("Gagal mengirim notif @%s", username)

    # async def _send_live_notification(
    #     self, channel, discord_user_id: str, username: str
    # ):
    #     """⏸️ DINONAKTIFKAN — aktifkan kembali bersama fitur live."""
    #     live_url = f"https://www.tiktok.com/@{username}/live"
    #     embed = discord.Embed(
    #         title="🔴 Sedang LIVE di TikTok!",
    #         description=f"<@{discord_user_id}> sedang live sekarang — yuk gabung!",
    #         url=live_url,
    #         color=discord.Color.red(),
    #     )
    #     embed.add_field(name="Nonton Live", value=f"[Klik di sini]({live_url})")
    #     try:
    #         await channel.send(embed=embed)
    #         log.info("Notif live terkirim: @%s", username)
    #     except discord.HTTPException:
    #         log.exception("Gagal mengirim notif live @%s", username)


async def setup(bot: commands.Bot):
    await bot.add_cog(TikTokCog(bot))
