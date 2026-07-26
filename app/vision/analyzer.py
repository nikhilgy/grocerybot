"""Claude Vision analysis of fridge/pantry photos."""
from __future__ import annotations

import base64
import json
import logging
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel

from app import config

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a kitchen inventory analyst. You are given one or more photos of a kitchen — these
may show a single storage area (e.g. just the fridge) or several different areas in the same batch
(e.g. the fridge AND the masala rack). The user message will list the known kitchen zone ids you can
choose from (e.g. "fridge", "freezer", "pantry", "dal_shelf", "masala_rack", "countertop").

Your job is to group everything you see into zones and, for each zone, identify grocery items visible —
specifically, items a user could plausibly reorder from a quick-commerce app like Swiggy Instamart, Blinkit,
or Zepto. This means packaged/branded grocery products (dairy, snacks, sauces, condiments, spices, beverages,
staples like atta/rice/dal in packaging) and common raw produce (vegetables, fruits, herbs).

Do NOT report:
- Cooked/prepared/leftover food (home-cooked dishes, cooked dal/chana, leftovers in bowls)
- Plain/unbranded water (tap water bottles, ice, ice trays)
- Unidentifiable miscellaneous containers or clutter that isn't a recognizable grocery item

For each zone group:
- "detected_zone": the id of the zone this group of items belongs to, from the known zones list. A fridge
  has shelves behind a door; a masala rack has spice jars/containers; a dal shelf has bags/containers of
  lentils/pulses; a freezer has ice/frozen packaging; a countertop is an open surface. If you cannot
  confidently tell which known zone a group of items belongs to, use "unknown".
- "zone_confidence": "high" if you're confident, "low" if you're genuinely torn between two zones, "unknown"
  if none of the known zones plausibly fit.
- "items": every food item visible in that zone.

For each item:
- Name the item (use common Indian grocery names where applicable, e.g. "paneer" not "cottage cheese", "atta" not "wheat flour")
- Estimate the quantity remaining (e.g. "~500ml", "half packet", "3 pieces", "nearly empty")
- Note the brand if the label is visible
- Note the container if relevant (e.g. "tetra pack", "steel dabba", "plastic wrap")
- Assess the stock level:
  - "full": newly purchased, mostly full
  - "adequate": enough for several days
  - "low": will run out in 1-2 days, should restock soon
  - "critical": nearly empty, needs immediate restock
  - "empty": completely out
  Stock level is a visual estimate, not a precise measurement — frame it as your best guess.

Return your analysis as a JSON object:
{
  "zones": [
    {
      "detected_zone": "fridge",
      "zone_confidence": "high",
      "items": [
        {
          "name": "toned milk",
          "quantity": "~500ml",
          "brand": "Amul",
          "container": "tetra pack",
          "stock_level": "low"
        },
        {
          "name": "paneer",
          "quantity": "~200g",
          "brand": null,
          "container": "plastic wrap",
          "stock_level": "adequate"
        }
      ]
    },
    {
      "detected_zone": "masala_rack",
      "zone_confidence": "high",
      "items": [
        {"name": "turmeric", "quantity": "half jar", "brand": null, "container": "glass jar", "stock_level": "adequate"}
      ]
    }
  ],
  "notes": "Fridge appears moderately stocked. Several steel containers with contents not visible."
}

Be thorough. If you can see a container but can't identify what's inside (e.g. steel dabba, opaque container), mention it in the notes. Indian kitchens commonly have steel containers — note their presence even if contents are unclear.

If the photos are blurry or unclear, set "notes" to explain that and leave "zones" as an empty list.
If the photos do not show any kitchen storage area at all (e.g. it's a person, a street, a document), set "notes" to say so explicitly starting with "NOT_FOOD:" and leave "zones" empty.
"""


class InventoryItem(BaseModel):
    name: str
    quantity: Optional[str] = None
    brand: Optional[str] = None
    container: Optional[str] = None
    stock_level: Optional[str] = None  # full/adequate/low/critical/empty


class ZoneGroup(BaseModel):
    detected_zone: str  # a known zone id, or "unknown"
    zone_confidence: Literal["high", "low", "unknown"] = "unknown"
    items: list[InventoryItem] = []


class FridgeInventory(BaseModel):
    zones: list[ZoneGroup] = []
    notes: str = ""

    @property
    def is_blurry(self) -> bool:
        return not self.zones and "blurry" in self.notes.lower()

    @property
    def is_not_food(self) -> bool:
        return self.notes.startswith("NOT_FOOD:")

    @property
    def is_empty(self) -> bool:
        return not self.zones and not self.is_blurry and not self.is_not_food

    @property
    def needs_zone_clarification(self) -> bool:
        return any(z.zone_confidence != "high" or z.detected_zone == "unknown" for z in self.zones)


def _image_block(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        },
    }


async def analyze_photos(images: list[bytes], known_zones: list[tuple[str, str]]) -> FridgeInventory:
    """Send one or more kitchen photos to Claude Vision in a single call.

    `known_zones` is a list of (zone_id, display_name) pairs the response's
    "detected_zone" values should be drawn from.
    """
    if not images:
        raise ValueError("analyze_photos requires at least one image")

    zones_desc = ", ".join(f"{zid} ({name})" for zid, name in known_zones)

    content = [_image_block(img) for img in images]
    content.append(
        {
            "type": "text",
            "text": (
                f"Known zones: {zones_desc}. "
                "Analyze these kitchen photos and return the JSON inventory as instructed, "
                "grouping items by zone."
            ),
        }
    )

    try:
        response = await _client.messages.create(
            model=config.CLAUDE_VISION_MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        logger.exception("Claude Vision request failed")
        raise

    # A truncated response (hit the token cap) yields invalid JSON further down;
    # fail with a clear message instead of a confusing JSONDecodeError.
    if response.stop_reason == "max_tokens":
        logger.error("Claude Vision response truncated at max_tokens; increase the limit")
        raise ValueError("Vision response was truncated (max_tokens reached)")

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(text)
    return FridgeInventory(**data)


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in Claude Vision response: {text!r}")
    return json.loads(text[start : end + 1])
