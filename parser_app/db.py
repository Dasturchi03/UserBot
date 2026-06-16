from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        await self.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_activity_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'New',
                source_chat_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS donors (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                invite_link TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                last_message_id INTEGER NOT NULL DEFAULT 0,
                last_parsed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO settings(key, value) VALUES ('depth_days', '1');
            """
        )

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    async def executescript(self, sql: str) -> None:
        async with self._lock:
            self.conn.executescript(sql)
            self.conn.commit()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        async with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        async with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("???? ?????? ?? ????????????????.")
        return self._conn

    async def get_depth_days(self) -> int:
        row = await self.fetchone("SELECT value FROM settings WHERE key = 'depth_days'")
        return int(row["value"]) if row else 1

    async def set_depth_days(self, days: int) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES('depth_days', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(days),),
        )

    async def upsert_donor(
        self,
        chat_id: int,
        title: str | None,
        username: str | None,
        invite_link: str | None = None,
        status: str = "active",
    ) -> None:
        await self.execute(
            """
            INSERT INTO donors(chat_id, title, username, invite_link, status)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                invite_link = COALESCE(excluded.invite_link, donors.invite_link),
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, title, username, invite_link, status),
        )

    async def add_pending_donor(self, invite_link: str) -> None:
        digest = hashlib.sha256(invite_link.encode("utf-8")).hexdigest()[:15]
        pending_id = -int(digest, 16)
        await self.execute(
            """
            INSERT INTO donors(chat_id, title, invite_link, status)
            VALUES(?, ?, ?, 'pending_approval')
            ON CONFLICT(chat_id) DO UPDATE SET
                invite_link = excluded.invite_link,
                status = 'pending_approval',
                updated_at = CURRENT_TIMESTAMP
            """,
            (pending_id, "??????? ?????????", invite_link),
        )

    async def get_active_donors(self) -> list[sqlite3.Row]:
        return await self.fetchall("SELECT * FROM donors WHERE status = 'active' ORDER BY title")

    async def get_all_donors(self) -> list[sqlite3.Row]:
        return await self.fetchall("SELECT * FROM donors ORDER BY status, title")

    async def remove_donor(self, chat_id: int) -> None:
        await self.execute("DELETE FROM donors WHERE chat_id = ?", (chat_id,))

    async def update_donor_progress(self, chat_id: int, last_message_id: int) -> None:
        await self.execute(
            """
            UPDATE donors
            SET last_message_id = MAX(last_message_id, ?),
                last_parsed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ?
            """,
            (last_message_id, chat_id),
        )

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_activity_date: str,
        source_chat_id: int,
    ) -> bool:
        cur = await self.execute(
            """
            INSERT INTO users(user_id, username, first_name, last_activity_date, source_chat_id)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name),
                last_activity_date = CASE
                    WHEN excluded.last_activity_date > users.last_activity_date
                    THEN excluded.last_activity_date
                    ELSE users.last_activity_date
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name, last_activity_date, source_chat_id),
        )
        return cur.rowcount > 0

    async def count_users(self, status: str | None = None) -> int:
        if status:
            row = await self.fetchone("SELECT COUNT(*) AS c FROM users WHERE status = ?", (status,))
        else:
            row = await self.fetchone("SELECT COUNT(*) AS c FROM users")
        return int(row["c"])

    async def export_rows(self, only_new: bool) -> list[sqlite3.Row]:
        if only_new:
            return await self.fetchall("SELECT * FROM users WHERE status = 'New' ORDER BY last_activity_date DESC")
        return await self.fetchall("SELECT * FROM users ORDER BY last_activity_date DESC")

    async def mark_exported(self, user_ids: list[int]) -> None:
        if not user_ids:
            return
        placeholders = ",".join("?" for _ in user_ids)
        await self.execute(f"UPDATE users SET status = 'Exported' WHERE user_id IN ({placeholders})", user_ids)
