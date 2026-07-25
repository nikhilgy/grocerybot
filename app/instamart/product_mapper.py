"""Maps a shopping-list item to the best matching Instamart product."""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic
from pydantic import BaseModel

from app import config
from app.instamart.mcp_client import Product, SwiggyMCPClient

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

MATCH_SYSTEM_PROMPT = """You are a grocery shopping assistant picking the best product match on an Indian quick-commerce app (Swiggy Instamart).

Given a shopping list item (name + desired quantity) and a list of candidate products, pick the single best match considering:
- Quantity match: prefer a pack size close to (or a clean multiple of) what's needed over grabbing the largest pack available.
- Price: reasonable, not the most premium option, unless brand preference (from go-to items) suggests otherwise.
- Brand preference: if a go-to item brand is present among candidates, strongly prefer it.

Also work out how many units of the chosen pack size are needed to cover the requested quantity (round up for partial packs).

Return JSON only:
{
  "spin_id": "<spin_id of chosen product, or null if none of the candidates are a reasonable match>",
  "units_needed": <integer>,
  "reasoning": "<short explanation>"
}

If no candidate is a reasonable match for the requested item, return "spin_id": null.
"""


class MappedProduct(BaseModel):
    item_name: str
    spin_id: str
    sku_id: Optional[str] = None
    name: str
    units: int
    unit_price: float
    total_price: float


class UnavailableItem(BaseModel):
    item_name: str
    qty: str
    reason: str


def _find_go_to_match(item_name: str, go_to_items: list[Product]) -> list[Product]:
    """Loose substring match against the user's previously-ordered items."""
    needle = item_name.lower()
    matches = []
    for product in go_to_items:
        haystack = product.name.lower()
        if needle in haystack or any(word in haystack for word in needle.split()):
            matches.append(product)
    return matches


async def _pick_best_match(
    item_name: str, qty: str, candidates: list[Product]
) -> Optional[dict]:
    if not candidates:
        return None

    payload = {
        "requested_item": item_name,
        "requested_qty": qty,
        "candidates": [c.model_dump() for c in candidates],
    }

    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=1024,
            system=MATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        )
    except Exception:
        logger.exception("Claude product matching request failed for %s", item_name)
        return None

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    try:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        logger.warning("Could not parse Claude match response for %s: %r", item_name, text)
        return None

    if not data.get("spin_id"):
        return None
    return data


async def map_item(
    shopping_item: dict, mcp_client: SwiggyMCPClient, address_id: str
) -> MappedProduct | UnavailableItem:
    """Maps one {"item": ..., "qty": ...} shopping list entry to an Instamart product."""
    item_name = shopping_item["item"]
    qty = shopping_item.get("qty", "")

    go_to_items = await mcp_client.get_go_to_items(address_id)
    candidates = _find_go_to_match(item_name, go_to_items)

    if not candidates:
        try:
            candidates = await mcp_client.search_products(item_name, address_id)
        except Exception:
            logger.exception("search_products failed for %s", item_name)
            candidates = []

    candidates = [c for c in candidates if c.in_stock]

    match = await _pick_best_match(item_name, qty, candidates)
    if not match:
        return UnavailableItem(
            item_name=item_name, qty=qty, reason="No matching product found on Instamart"
        )

    chosen = next((c for c in candidates if c.spin_id == match["spin_id"]), None)
    if not chosen:
        return UnavailableItem(
            item_name=item_name, qty=qty, reason="Matched product no longer available"
        )

    units = max(1, int(match.get("units_needed", 1)))
    return MappedProduct(
        item_name=item_name,
        spin_id=chosen.spin_id,
        sku_id=chosen.sku_id,
        name=chosen.name,
        units=units,
        unit_price=chosen.price,
        total_price=round(chosen.price * units, 2),
    )


async def map_shopping_list(
    shopping_list: list[dict], mcp_client: SwiggyMCPClient, address_id: str
) -> tuple[list[MappedProduct], list[UnavailableItem]]:
    found: list[MappedProduct] = []
    unavailable: list[UnavailableItem] = []

    for shopping_item in shopping_list:
        result = await map_item(shopping_item, mcp_client, address_id)
        if isinstance(result, MappedProduct):
            found.append(result)
        else:
            unavailable.append(result)

    return found, unavailable
