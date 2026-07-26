"""All async database operations for kitchen zones + inventory."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db.database import get_db
from app.db.models import StoredInventoryItem, Zone, ZoneStaleness

_STALENESS_TIERS = [
    (1, "fresh"),
    (3, "recent"),
    (7, "warning"),
]


def _staleness_tier(last_scanned: Optional[datetime]) -> str:
    if last_scanned is None:
        return "no_data"
    age_days = (datetime.now() - last_scanned).total_seconds() / 86400
    for max_days, tier in _STALENESS_TIERS:
        if age_days < max_days:
            return tier
    return "critical"


def _row_to_zone(row) -> Zone:
    return Zone(
        id=row["id"],
        display_name=row["display_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_item(row) -> StoredInventoryItem:
    return StoredInventoryItem(
        id=row["id"],
        zone_id=row["zone_id"],
        item_name=row["item_name"],
        quantity=row["quantity"],
        brand=row["brand"],
        container=row["container"],
        stock_level=row["stock_level"],
        scanned_at=datetime.fromisoformat(row["scanned_at"]),
    )


async def create_zone(zone_id: str, display_name: str) -> Zone:
    db = await get_db()
    now = datetime.now()
    await db.execute(
        "INSERT INTO zones (id, display_name, created_at) VALUES (?, ?, ?)",
        (zone_id, display_name, now.isoformat()),
    )
    await db.commit()
    return Zone(id=zone_id, display_name=display_name, created_at=now)


async def get_zones() -> list[Zone]:
    db = await get_db()
    async with db.execute("SELECT * FROM zones ORDER BY created_at") as cursor:
        rows = await cursor.fetchall()
    return [_row_to_zone(r) for r in rows]


async def delete_zone(zone_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
    await db.commit()
    return cursor.rowcount > 0


# --- Settings (key/value) --------------------------------------------------

DELIVERY_ADDRESS_KEY = "delivery_address_id"


async def get_setting(key: str) -> Optional[str]:
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    db = await get_db()
    await db.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, datetime.now().isoformat()),
    )
    await db.commit()


async def save_inventory(zone_id: str, items: list[dict], scanned_at: Optional[datetime] = None) -> None:
    """Full replacement: deletes all existing rows for this zone, inserts the new set."""
    scanned_at = scanned_at or datetime.now()
    db = await get_db()
    await db.execute("DELETE FROM inventory WHERE zone_id = ?", (zone_id,))
    if items:
        await db.executemany(
            """
            INSERT INTO inventory (zone_id, item_name, quantity, brand, container, stock_level, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    zone_id,
                    item["item_name"],
                    item.get("quantity"),
                    item.get("brand"),
                    item.get("container"),
                    item.get("stock_level"),
                    scanned_at.isoformat(),
                )
                for item in items
            ],
        )
    await db.commit()


async def get_zone_inventory(zone_id: str) -> list[StoredInventoryItem]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM inventory WHERE zone_id = ? ORDER BY item_name", (zone_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_item(r) for r in rows]


async def get_all_inventory() -> dict[str, list[StoredInventoryItem]]:
    zones = await get_zones()
    result: dict[str, list[StoredInventoryItem]] = {}
    for zone in zones:
        result[zone.id] = await get_zone_inventory(zone.id)
    return result


async def get_all_inventory_flat() -> list[StoredInventoryItem]:
    db = await get_db()
    async with db.execute("SELECT * FROM inventory ORDER BY zone_id, item_name") as cursor:
        rows = await cursor.fetchall()
    return [_row_to_item(r) for r in rows]


async def find_items_by_name(name: str, zone_id: Optional[str] = None) -> list[StoredInventoryItem]:
    """Loose, case-insensitive substring match on item_name (either direction),
    optionally scoped to one zone. Used by the inventory_query and correction intents."""
    needle = name.lower()
    items = await get_zone_inventory(zone_id) if zone_id else await get_all_inventory_flat()
    matches = []
    for item in items:
        haystack = item.item_name.lower()
        if needle in haystack or haystack in needle:
            matches.append(item)
    return matches


async def update_item(item_id: int, quantity: Optional[str] = None, stock_level: Optional[str] = None) -> None:
    """Targeted update of one stored item's quantity/stock_level (the correction intent) —
    unlike save_inventory, this does not touch the rest of the zone."""
    if quantity is None and stock_level is None:
        return
    db = await get_db()
    fields, values = [], []
    if quantity is not None:
        fields.append("quantity = ?")
        values.append(quantity)
    if stock_level is not None:
        fields.append("stock_level = ?")
        values.append(stock_level)
    values.append(item_id)
    await db.execute(f"UPDATE inventory SET {', '.join(fields)} WHERE id = ?", values)
    await db.commit()


async def get_zone_last_scanned(zone_id: str) -> Optional[datetime]:
    db = await get_db()
    async with db.execute(
        "SELECT MAX(scanned_at) AS last_scanned FROM inventory WHERE zone_id = ?", (zone_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row["last_scanned"] is None:
        return None
    return datetime.fromisoformat(row["last_scanned"])


async def get_all_zones_staleness() -> list[ZoneStaleness]:
    zones = await get_zones()
    result = []
    for zone in zones:
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) AS cnt, MAX(scanned_at) AS last_scanned FROM inventory WHERE zone_id = ?",
            (zone.id,),
        ) as cursor:
            row = await cursor.fetchone()
        last_scanned = datetime.fromisoformat(row["last_scanned"]) if row["last_scanned"] else None
        result.append(
            ZoneStaleness(
                zone_id=zone.id,
                display_name=zone.display_name,
                item_count=row["cnt"],
                last_scanned=last_scanned,
                tier=_staleness_tier(last_scanned),
            )
        )
    return result
