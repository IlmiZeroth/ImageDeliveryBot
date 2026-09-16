from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import CloudItem, Source

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS superadmins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('google_drive', 'yandex_disk')),
    location TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_key TEXT NOT NULL UNIQUE,
    scheduled_for TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    target_guilds_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'deleting', 'completed', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed')),
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(dispatch_id, guild_id)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        return db

    async def is_superadmin(self, user_id: int) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT 1 FROM superadmins WHERE user_id=?", (user_id,))
            return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def add_superadmin(self, user_id: int, added_by: int | None) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO superadmins(user_id, added_by, added_at) VALUES (?, ?, ?)",
                (user_id, added_by, utc_now()),
            )
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def remove_superadmin(self, user_id: int) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute("DELETE FROM superadmins WHERE user_id=?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def clear_superadmins(self) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute("DELETE FROM superadmins")
            await db.commit()
            return cursor.rowcount
        finally:
            await db.close()

    async def list_superadmins(self) -> list[int]:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT user_id FROM superadmins ORDER BY added_at")
            return [int(row["user_id"]) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def set_guild_channel(self, guild_id: int, channel_id: int, updated_by: int) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO guild_settings(guild_id, channel_id, enabled, updated_by, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    enabled=1,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (guild_id, channel_id, updated_by, utc_now()),
            )
            await db.commit()
        finally:
            await db.close()

    async def disable_guild(self, guild_id: int) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "UPDATE guild_settings SET enabled=0, updated_at=? WHERE guild_id=?", (utc_now(), guild_id)
            )
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def get_guild_channel(self, guild_id: int) -> int | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT channel_id FROM guild_settings WHERE guild_id=? AND enabled=1", (guild_id,)
            )
            row = await cursor.fetchone()
            return int(row["channel_id"]) if row else None
        finally:
            await db.close()

    async def list_enabled_guilds(self) -> dict[int, int]:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT guild_id, channel_id FROM guild_settings WHERE enabled=1")
            return {int(row["guild_id"]): int(row["channel_id"]) for row in await cursor.fetchall()}
        finally:
            await db.close()

    async def get_schedule_times(self, default: tuple[str, ...]) -> tuple[tuple[str, ...], datetime | None]:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT value, updated_at FROM bot_settings WHERE key='schedule_times'")
            row = await cursor.fetchone()
            if not row:
                return default, None
            values = json.loads(row["value"])
            return tuple(str(value) for value in values), datetime.fromisoformat(row["updated_at"])
        finally:
            await db.close()

    async def set_schedule_times(self, values: tuple[str, ...], updated_by: int) -> datetime:
        updated_at = datetime.now(UTC)
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO bot_settings(key, value, updated_by, updated_at)
                VALUES ('schedule_times', ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (json.dumps(values), updated_by, updated_at.isoformat()),
            )
            await db.commit()
            return updated_at
        finally:
            await db.close()

    async def add_source(self, name: str, kind: str, location: str, created_by: int) -> int:
        db = await self._connect()
        try:
            existing_cursor = await db.execute(
                "SELECT id, enabled FROM sources WHERE name=? COLLATE NOCASE", (name.strip(),)
            )
            existing = await existing_cursor.fetchone()
            if existing:
                if bool(existing["enabled"]):
                    raise aiosqlite.IntegrityError("source name already exists")
                await db.execute(
                    """
                    UPDATE sources
                    SET kind=?, location=?, enabled=1, created_by=?, created_at=?
                    WHERE id=?
                    """,
                    (kind, location.strip(), created_by, utc_now(), int(existing["id"])),
                )
                await db.commit()
                return int(existing["id"])
            cursor = await db.execute(
                "INSERT INTO sources(name, kind, location, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (name.strip(), kind, location.strip(), created_by, utc_now()),
            )
            await db.commit()
            return int(cursor.lastrowid)
        finally:
            await db.close()

    async def remove_source(self, source_id: int) -> bool:
        db = await self._connect()
        try:
            # Источник остаётся в истории уже выполненных рассылок.
            cursor = await db.execute("UPDATE sources SET enabled=0 WHERE id=? AND enabled=1", (source_id,))
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def list_sources(self, *, enabled_only: bool = False) -> list[Source]:
        where = " WHERE enabled=1" if enabled_only else ""
        db = await self._connect()
        try:
            cursor = await db.execute(f"SELECT id, name, kind, location, enabled FROM sources{where} ORDER BY id")
            rows = await cursor.fetchall()
            return [
                Source(int(row["id"]), str(row["name"]), str(row["kind"]), str(row["location"]), bool(row["enabled"]))
                for row in rows
            ]
        finally:
            await db.close()

    async def get_source(self, source_id: int) -> Source | None:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT id, name, kind, location, enabled FROM sources WHERE id=?", (source_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Source(
                int(row["id"]), str(row["name"]), str(row["kind"]), str(row["location"]), bool(row["enabled"])
            )
        finally:
            await db.close()

    async def dispatch_exists(self, slot_key: str) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT 1 FROM dispatches WHERE slot_key=?", (slot_key,))
            return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def create_dispatch(
        self,
        *,
        slot_key: str,
        scheduled_for: str,
        source_id: int,
        category_id: str,
        category_name: str,
        items: list[CloudItem],
        target_guild_ids: list[int],
    ) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                INSERT INTO dispatches(
                    slot_key, scheduled_for, source_id, category_id, category_name,
                    items_json, target_guilds_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slot_key,
                    scheduled_for,
                    source_id,
                    category_id,
                    category_name,
                    json.dumps([item.to_dict() for item in items], ensure_ascii=False),
                    json.dumps(target_guild_ids),
                    utc_now(),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)
        finally:
            await db.close()

    async def list_pending_dispatches(self) -> list[dict[str, Any]]:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT * FROM dispatches WHERE status IN ('pending', 'deleting') ORDER BY id")
            rows = await cursor.fetchall()
            return [self._decode_dispatch(row) for row in rows]
        finally:
            await db.close()

    async def reserved_item_keys(self) -> set[tuple[str, str]]:
        """Return (provider kind, remote item id) reserved by unfinished posts."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT s.kind, d.items_json
                FROM dispatches d
                JOIN sources s ON s.id=d.source_id
                WHERE d.status IN ('pending', 'deleting')
                """
            )
            result: set[tuple[str, str]] = set()
            for row in await cursor.fetchall():
                for item in json.loads(row["items_json"]):
                    result.add((str(row["kind"]), str(item["id"])))
            return result
        finally:
            await db.close()

    async def get_dispatch(self, dispatch_id: int) -> dict[str, Any] | None:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            row = await cursor.fetchone()
            return self._decode_dispatch(row) if row else None
        finally:
            await db.close()

    @staticmethod
    def _decode_dispatch(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        data["items"] = [CloudItem.from_dict(item) for item in json.loads(data.pop("items_json"))]
        data["target_guild_ids"] = [int(value) for value in json.loads(data.pop("target_guilds_json"))]
        return data

    async def delivery_succeeded(self, dispatch_id: int, guild_id: int) -> bool:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT 1 FROM deliveries WHERE dispatch_id=? AND guild_id=? AND status='success'",
                (dispatch_id, guild_id),
            )
            return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def record_delivery(
        self,
        dispatch_id: int,
        guild_id: int,
        channel_id: int | None,
        *,
        success: bool,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO deliveries(dispatch_id, guild_id, channel_id, message_id, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dispatch_id, guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id,
                    status=excluded.status,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (dispatch_id, guild_id, channel_id, message_id, "success" if success else "failed", error, utc_now()),
            )
            await db.commit()
        finally:
            await db.close()

    async def set_dispatch_status(self, dispatch_id: int, status: str, error: str | None = None) -> None:
        db = await self._connect()
        try:
            completed_at = utc_now() if status in {"completed", "failed"} else None
            await db.execute(
                "UPDATE dispatches SET status=?, error=?, completed_at=? WHERE id=?",
                (status, error, completed_at, dispatch_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def summary(self) -> dict[str, int]:
        db = await self._connect()
        try:
            result: dict[str, int] = {}
            for key, query in {
                "superadmins": "SELECT COUNT(*) AS count FROM superadmins",
                "guilds": "SELECT COUNT(*) AS count FROM guild_settings WHERE enabled=1",
                "sources": "SELECT COUNT(*) AS count FROM sources WHERE enabled=1",
                "pending": "SELECT COUNT(*) AS count FROM dispatches WHERE status IN ('pending','deleting')",
                "completed": "SELECT COUNT(*) AS count FROM dispatches WHERE status='completed'",
            }.items():
                cursor = await db.execute(query)
                row = await cursor.fetchone()
                result[key] = int(row["count"])
            return result
        finally:
            await db.close()
