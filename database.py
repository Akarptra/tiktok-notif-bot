"""
database.py — Layer akses SQLite untuk data member TikTok. (REVISI v2)

Perubahan dari v1:
- PRIMARY KEY sekarang `tiktok_username` (bukan `discord_user_id`), sehingga
  satu user Discord bisa mendaftarkan BANYAK akun TikTok.
- Semua operasi update/delete menggunakan `tiktok_username` sebagai acuan
  WHERE clause.

Skema tabel `tiktok_members`:
    tiktok_username  TEXT  PRIMARY KEY  → username TikTok tanpa '@' (unik)
    discord_user_id  TEXT  NOT NULL     → pemilik akun (boleh muncul di banyak baris)
    last_video_id    TEXT               → aweme_id video terakhir yang sudah dinotif
    is_live          INTEGER DEFAULT 0  → 0 = offline, 1 = sedang live (disimpan
                                          untuk pemakaian nanti; fitur live
                                          sementara dinonaktifkan di cog)

⚠️ Jika kamu sudah punya `bot_data.db` dari versi lama, hapus dulu file
   database lamanya (atau rename) karena skema PRIMARY KEY-nya berubah.
"""

import sqlite3
from typing import Optional


class Database:
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # ── Inisialisasi ─────────────────────────────────────────────────────────
    def init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tiktok_members (
                tiktok_username TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                last_video_id   TEXT,
                is_live         INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.commit()

    # ── Write operations ─────────────────────────────────────────────────────
    def set_tiktok_account(self, discord_user_id: int, tiktok_username: str) -> None:
        """Daftarkan akun TikTok baru (atau update pemiliknya jika sudah ada).

        Karena PRIMARY KEY = tiktok_username, mendaftarkan username KEDUA oleh
        user yang sama akan membuat BARIS BARU — akun pertama tidak tertimpa.

        Jika username yang sama didaftarkan ulang, hanya kolom pemilik
        (discord_user_id) yang diperbarui; last_video_id sengaja TIDAK di-reset
        supaya baseline tracking tidak hilang dan tidak memicu notif dobel.
        """
        self.conn.execute(
            """
            INSERT INTO tiktok_members (tiktok_username, discord_user_id, last_video_id, is_live)
            VALUES (?, ?, NULL, 0)
            ON CONFLICT(tiktok_username) DO UPDATE SET
                discord_user_id = excluded.discord_user_id
            """,
            (tiktok_username, str(discord_user_id)),
        )
        self.conn.commit()

    def update_last_video(self, tiktok_username: str, video_id: str) -> None:
        self.conn.execute(
            "UPDATE tiktok_members SET last_video_id = ? WHERE tiktok_username = ?",
            (video_id, tiktok_username),
        )
        self.conn.commit()

    def update_live_status(self, tiktok_username: str, is_live: bool) -> None:
        """Disimpan untuk pemakaian nanti saat fitur live diaktifkan kembali."""
        self.conn.execute(
            "UPDATE tiktok_members SET is_live = ? WHERE tiktok_username = ?",
            (1 if is_live else 0, tiktok_username),
        )
        self.conn.commit()

    def remove_member(self, tiktok_username: str) -> bool:
        """Hapus satu akun TikTok dari daftar. Return True jika ada yang terhapus."""
        cur = self.conn.execute(
            "DELETE FROM tiktok_members WHERE tiktok_username = ?",
            (tiktok_username,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── Read operations ──────────────────────────────────────────────────────
    def get_all_members(self) -> list[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM tiktok_members")
        return cur.fetchall()

    def get_member(self, tiktok_username: str) -> Optional[sqlite3.Row]:
        """Ambil satu baris berdasarkan username TikTok."""
        cur = self.conn.execute(
            "SELECT * FROM tiktok_members WHERE tiktok_username = ?",
            (tiktok_username,),
        )
        return cur.fetchone()

    def get_accounts_by_user(self, discord_user_id: int) -> list[sqlite3.Row]:
        """Ambil semua akun TikTok milik satu user Discord."""
        cur = self.conn.execute(
            "SELECT * FROM tiktok_members WHERE discord_user_id = ?",
            (str(discord_user_id),),
        )
        return cur.fetchall()

    def close(self) -> None:
        self.conn.close()
