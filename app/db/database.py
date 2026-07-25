"""SQLite connection management + schema initialization.

Single-user bot, so a single module-level connection is sufficient — no
pooling. No migration framework: schema changes are additive `CREATE TABLE
IF NOT EXISTS` / manual `ALTER TABLE`, matching the fact that Phase 1 had no
database at all.
"""
from __future__ import annotations

import os

import aiosqlite

from app import config

_DEFAULT_ZONES = [
    ("fridge", "Fridge"),
    ("freezer", "Freezer"),
    ("pantry", "Pantry Shelf"),
    ("dal_shelf", "Dal Shelf"),
    ("masala_rack", "Masala Rack"),
    ("countertop", "Countertop"),
]

_connection: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _connection
    if _connection is None:
        os.makedirs(os.path.dirname(config.DATABASE_PATH) or ".", exist_ok=True)
        _connection = await aiosqlite.connect(config.DATABASE_PATH)
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA foreign_keys = ON")
    return _connection


async def init_db() -> None:
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS zones (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id TEXT NOT NULL REFERENCES zones(id),
            item_name TEXT NOT NULL,
            quantity TEXT,
            brand TEXT,
            container TEXT,
            stock_level TEXT,
            scanned_at TIMESTAMP NOT NULL,
            FOREIGN KEY (zone_id) REFERENCES zones(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_inventory_zone ON inventory(zone_id)"
    )
    await db.commit()

    async with db.execute("SELECT COUNT(*) FROM zones") as cursor:
        row = await cursor.fetchone()
        zone_count = row[0]

    if zone_count == 0:
        await db.executemany(
            "INSERT INTO zones (id, display_name) VALUES (?, ?)", _DEFAULT_ZONES
        )
        await db.commit()
