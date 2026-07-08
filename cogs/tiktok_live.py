"""
cogs/tiktok_live.py — Notifikasi LIVE TikTok real-time via library TikTokLive. (v1)

Konsep "Satpam Pengetuk Pintu":
Untuk tiap username terdaftar, bot menjalankan SATU background task
(asyncio.Task) berisi infinite loop yang terus "mengetuk pintu":
1. Buat TikTokLiveClient baru.
2. Coba connect (await client.connect() → blocking sampai live selesai).
3. Kalau user OFFLINE → UserOfflineError dilempar → tidur 5 menit → ulangi.
4. Kalau ONLINE → ConnectEvent terpicu → kirim notif @here ke Discord.
5. Live selesai / disconnect → connect() selesai → tidur → ketuk lagi.

Verifikasi API (TikTokLive v6.x dari PyPI, pip install TikTokLive):
- Exception offline  : TikTokLive.client.errors.UserOfflineError
  (di versi lama v4/v5 namanya LiveNotFound — sudah TIDAK ada di v6)
- Event              : from TikTokLive.events import ConnectEvent, DisconnectEvent
- Blocking connect   : await client.connect()  ← menunggu sampai live berakhir
  (client.start() itu NON-blocking dan return asyncio.Task — bukan yang
  kita mau untuk pola while-True ini)

Keamanan event loop:
TikTokLive berbasis asyncio murni (httpx + websockets), jadi client-nya
berjalan di event loop yang SAMA dengan discord.py — tidak ada thread
tambahan, tidak ada blocking call. channel.send() dari dalam event handler
TikTokLive aman karena masih satu loop.

.env yang dibutuhkan:
LIVE_NOTIF_CHANNEL_ID=...   (channel khusus notif live, beda dari video)

requirements.txt: tambahkan  TikTokLive>=6.0
"""

import asyncio
import logging
import os
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent
from TikTokLive.client.errors import (
    SignatureRateLimitError,
    UserNotFoundError,
    UserOfflineError,
)

log = logging.getLogger("cogs.tiktok_live")

# --- KONFIGURASI BACKOFF (DIPERBARUI) ---
OFFLINE_RETRY_SECONDS = 1800     # 30 menit sekali (sebelumnya 15m/5m)
AFTER_LIVE_COOLDOWN = 3600       # 1 jam (mencegah spam kalau koneksi goyang)
RATE_LIMIT_BACKOFF = 3600        # 1 jam (kalau kena rate limit sign server, istirahat lama)
ERROR_RETRY_SECONDS = 600        # 10 menit (error tak terduga lainnya)
WATCHER_SYNC_MINUTES = 5         # interval sinkronisasi watcher ↔ database


class TikTokLiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.live_channel_id = int(os.getenv("LIVE_NOTIF_CHANNEL_ID", "0"))

        # username → asyncio.Task (satu "satpam" per akun)
        self.watchers: Dict[str, asyncio.Task] = {}

        if not self.live_channel_id:
            log.warning(
                "LIVE_NOTIF_CHANNEL_ID belum di-set di .env — "
                "notifikasi live tidak akan terkirim."
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def cog_load(self):
        # Loop sinkronisasi ini juga yang men-spawn watcher pertama kali
        # (iterasi pertamanya jalan segera setelah bot ready).
        self.sync_watchers.start()

    async def cog_unload(self):
        self.sync_watchers.cancel()
        for username, task in self.watchers.items():
            task.cancel()
        self.watchers.clear()
        log.info("Semua watcher live dihentikan.")

    # ═════════════════════════════════════════════════════════════════════════
    # SINKRONISASI WATCHER ↔ DATABASE
    # Spawn watcher untuk akun baru (didaftarkan via /set_tiktok saat bot hidup)
    # dan hentikan watcher untuk akun yang dihapus — tanpa perlu restart bot.
    # ═════════════════════════════════════════════════════════════════════════
    @tasks.loop(minutes=WATCHER_SYNC_MINUTES)
    async def sync_watchers(self):
        rows = await asyncio.to_thread(self.db.get_all_members)
        db_usernames = {row["tiktok_username"] for row in rows}

        # 1) Spawn watcher untuk username baru (atau yang task-nya sudah mati)
        for username in db_usernames:
            existing = self.watchers.get(username)
            if existing is None or existing.done():
                self.watchers[username] = asyncio.create_task(
                    self._watch_user(username),
                    name=f"tiktok-live-watcher:{username}",
                )
                log.info("Watcher live dinyalakan untuk @%s", username)
                
                # Jeda 5 detik biar nggak dikira spammer sama TikTok saat inisiasi banyak akun
                await asyncio.sleep(5) 

        # 2) Matikan watcher untuk username yang sudah dihapus dari database
        for username in list(self.watchers.keys()):
            if username not in db_usernames:
                self.watchers.pop(username).cancel()
                log.info("Watcher live dimatikan untuk @%s (dihapus dari DB)", username)

    @sync_watchers.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    # ═════════════════════════════════════════════════════════════════════════
    # SI "SATPAM" — satu infinite loop per username
    # ═════════════════════════════════════════════════════════════════════════
    async def _watch_user(self, username: str) -> None:
        while True:
            try:
                # Client dibuat BARU tiap iterasi — instance TikTokLiveClient
                # tidak dirancang untuk di-reuse setelah disconnect.
                # Penambahan Custom header untuk menyamar sebagai browser desktop normal
                client = TikTokLiveClient(
                    unique_id=f"@{username}",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    }
                )
                self._attach_handlers(client, username)

                # Blocking: baris ini "menggantung" selama live berlangsung
                # dan baru lanjut ketika live berakhir / koneksi putus.
                await client.connect(fetch_room_info=False)

                # Sampai sini artinya live tadi sudah selesai.
                log.info("Live @%s berakhir. Cooldown %ds sebelum ketuk lagi.",
                         username, AFTER_LIVE_COOLDOWN)
                await asyncio.sleep(AFTER_LIVE_COOLDOWN)

            except asyncio.CancelledError:
                # Watcher dimatikan (cog unload / akun dihapus) — keluar bersih.
                log.info("Watcher @%s dibatalkan.", username)
                raise
            except UserOfflineError:
                # Normal & paling sering: user sedang tidak live. Tidur, ulangi.
                log.debug("@%s offline. Cek lagi dalam %ds.",
                          username, OFFLINE_RETRY_SECONDS)
                await asyncio.sleep(OFFLINE_RETRY_SECONDS)
            except UserNotFoundError:
                # Akun tidak eksis (typo / di-banned / ganti username).
                # Percuma diulang — matikan satpam untuk akun ini.
                log.warning(
                    "Akun TikTok @%s tidak ditemukan. Watcher dihentikan permanen "
                    "(perbaiki username via /remove_tiktok lalu /set_tiktok).",
                    username,
                )
                return
            except SignatureRateLimitError:
                # Sign server TikTokLive punya kuota koneksi. Backoff lebih lama.
                log.warning("Rate limit sign server untuk @%s. Backoff %ds.",
                            username, RATE_LIMIT_BACKOFF)
                await asyncio.sleep(RATE_LIMIT_BACKOFF)
            except Exception as e:
                # Cek apakah error karena 403 (Forbidden) atau 404 (diblokir TikTok)
                if "403" in str(e) or "404" in str(e):
                    log.warning(f"TikTok memblokir request untuk @{username}. Jeda lebih lama.")
                    await asyncio.sleep(3600) # Tunggu 1 jam kalau kena blokir
                else:
                    # Jaring pengaman: error apa pun tidak boleh mematikan loop.
                    log.exception("Error tak terduga di watcher @%s. Retry dalam %ds.",
                                  username, ERROR_RETRY_SECONDS)
                    await asyncio.sleep(ERROR_RETRY_SECONDS)

    # ── Registrasi event handler per client ──────────────────────────────────
    def _attach_handlers(self, client: TikTokLiveClient, username: str) -> None:
        async def on_connect(event: ConnectEvent):
            # Connect berhasil = user SEDANG live → kirim notif.
            log.info("@%s terdeteksi LIVE (room_id=%s).", username, client.room_id)
            await self._send_live_notification(username)

        async def on_disconnect(event: DisconnectEvent):
            # Cukup log saja — loop di _watch_user yang mengurus siklus ulang.
            log.info("Terputus dari live @%s.", username)

        client.add_listener(ConnectEvent, on_connect)
        client.add_listener(DisconnectEvent, on_disconnect)

    # ── Pengiriman notifikasi ────────────────────────────────────────────────
    async def _send_live_notification(self, username: str) -> None:
        channel = self.bot.get_channel(self.live_channel_id)
        if channel is None:
            log.warning(
                "Channel live (ID: %s) tidak ditemukan. Cek LIVE_NOTIF_CHANNEL_ID "
                "dan pastikan bot punya akses.", self.live_channel_id,
            )
            return

        content = (
            f"@here 🔴 **{username}** sedang LIVE sekarang! "
            f"Tonton di: https://www.tiktok.com/@{username}/live"
        )
        try:
            await channel.send(content)
            log.info("Notif live terkirim: @%s", username)
        except discord.HTTPException:
            log.exception("Gagal mengirim notif live @%s", username)


async def setup(bot: commands.Bot):
    await bot.add_cog(TikTokLiveCog(bot))