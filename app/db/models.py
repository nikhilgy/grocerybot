"""Pydantic models for the SQLite-backed kitchen inventory."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class Zone(BaseModel):
    id: str
    display_name: str
    created_at: datetime


class StoredInventoryItem(BaseModel):
    id: int
    zone_id: str
    item_name: str
    quantity: Optional[str] = None
    brand: Optional[str] = None
    container: Optional[str] = None
    stock_level: Optional[str] = None  # full/adequate/low/critical/empty
    scanned_at: datetime


class ZoneStaleness(BaseModel):
    zone_id: str
    display_name: str
    item_count: int
    last_scanned: Optional[datetime]
    tier: Literal["fresh", "recent", "warning", "critical", "no_data"]
