"""Compares stored kitchen inventory against tomorrow's meal plan (or a
specific recipe's ingredient list) to find what's missing."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import anthropic
from pydantic import BaseModel

from app import config
from app.db.models import StoredInventoryItem

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a meal planning assistant. Given:
1. What's currently in the kitchen (inventory, drawn from multiple storage zones, each item stamped with
   which zone it's in and when it was last scanned)
2. What meals are planned for tomorrow (from the diet plan)
3. A list of pantry staples the user always has (ignore these, don't order them)

Figure out what ingredients are missing or insufficient for tomorrow's meals.

Rules:
- If the fridge has ~200g paneer and tomorrow needs 400g, order 200g more (the deficit).
- If something is "nearly empty" or "half packet", assume it's not enough and order a fresh one.
- Never include pantry staples in the shopping list (salt, oil, basic spices, etc.).
- If an ingredient can be reasonably substituted (e.g. Greek yogurt ↔ hung curd), note it but still list the original.
- Items scanned more than 3 days ago are possibly stale — if that affects your call on an item, mention it in "reason".
- For each missing item, specify a quantity to order.

If the inventory payload includes a "guest_scaling" object with "guest_count" and "meals_to_scale", scale ONLY
those meals' ingredient quantities by guest_count people (the base plan is for 1 person). Leave all other
meals at normal single-serving quantities — do not scale meals not listed in "meals_to_scale".

Return as JSON:
{
  "tomorrow": "Monday",
  "meals": ["Oats with banana", "Soya chunk curry with rice", "Protein smoothie", "Paneer bhurji with roti"],
  "shopping_list": [
    {"item": "paneer", "qty": "200g", "reason": "Have ~200g, need 400g"},
    {"item": "soya chunks", "qty": "200g", "reason": "Not found in kitchen"},
    {"item": "banana", "qty": "4 pieces", "reason": "Needed for breakfast + smoothie"}
  ]
}
"""

RECIPE_GAP_SYSTEM_PROMPT = """You are a kitchen inventory assistant helping a user cook a specific recipe or meal. Given:
1. What's currently in the kitchen (inventory, drawn from multiple storage zones, each item stamped with
   which zone it's in and when it was last scanned)
2. The ingredients the recipe needs, with quantities
3. Ingredients assumed already available (pantry staples — never include these in the shopping list)

Figure out what's missing or insufficient.

Rules:
- If the kitchen has some of an ingredient but not enough, order only the deficit (e.g. have 200g paneer,
  need 400g -> order 200g more).
- If something is "nearly empty" or otherwise clearly insufficient, treat it as needing a fresh order.
- Never include the assumed-available ingredients in the shopping list.
- Items scanned more than 3 days ago are possibly stale — if that affects your call on an item, mention it in "reason".
- For each missing item, specify a quantity to order.

Return as JSON:
{
  "tomorrow": "<the context name you were given, e.g. the recipe name or 'Guest dinner for 6'>",
  "meals": ["<the same context name>"],
  "shopping_list": [
    {"item": "spinach", "qty": "2 bunches", "reason": "Not found in kitchen"},
    {"item": "paneer", "qty": "200g", "reason": "Have ~200g, need 400g"}
  ]
}
"""

_DAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class ShoppingListItem(BaseModel):
    item: str
    qty: str
    reason: str


class GapAnalysis(BaseModel):
    tomorrow: str
    meals: list[str]
    shopping_list: list[ShoppingListItem]


def _load_diet_plan() -> dict:
    with open(config.DIET_PLAN_PATH, "r") as f:
        return json.load(f)


def get_tomorrow_meals(reference_date: Optional[datetime] = None) -> tuple[str, dict]:
    """Returns (day_name_title_case, meals_dict) for the day after reference_date."""
    reference_date = reference_date or datetime.now()
    tomorrow = reference_date + timedelta(days=1)
    day_key = _DAY_NAMES[tomorrow.weekday()]
    plan = _load_diet_plan()
    meals = plan["weekly_plan"][day_key]
    return day_key.title(), meals


def build_ingredient_list(meals: dict) -> list[dict]:
    """Flattens a day's meals dict into a raw ingredient list (with duplicates)."""
    ingredients = []
    for meal in meals.values():
        ingredients.extend(meal.get("ingredients", []))
    return ingredients


def _inventory_payload(combined_items: list[StoredInventoryItem]) -> list[dict]:
    return [
        {
            "item": item.item_name,
            "quantity": item.quantity,
            "brand": item.brand,
            "zone": item.zone_id,
            "scanned_at": item.scanned_at.isoformat(),
        }
        for item in combined_items
    ]


async def analyze_gap(
    combined_items: list[StoredInventoryItem],
    reference_date: Optional[datetime] = None,
    guest_count: Optional[int] = None,
    guest_meals: Optional[list[str]] = None,
) -> GapAnalysis:
    """Compares stored multi-zone inventory to tomorrow's meal plan via Claude."""
    day_name, meals = get_tomorrow_meals(reference_date)
    plan = _load_diet_plan()
    pantry_staples = plan["pantry_staples"]

    user_payload = {
        "tomorrow": day_name,
        "meals": meals,
        "inventory": _inventory_payload(combined_items),
        "pantry_staples": pantry_staples,
    }
    if guest_count:
        user_payload["guest_scaling"] = {
            "guest_count": guest_count,
            "meals_to_scale": guest_meals or [],
        }

    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(user_payload, indent=2),
                }
            ],
        )
    except Exception:
        logger.exception("Claude gap analysis request failed")
        raise

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(text)
    return GapAnalysis(**data)


async def analyze_gap_for_needed_items(
    context_name: str,
    needed_items: list[dict],
    assumed_available: list[str],
    combined_items: list[StoredInventoryItem],
) -> GapAnalysis:
    """Reconciles a recipe/YouTube ingredient list against stored inventory.
    Returns the same GapAnalysis shape as analyze_gap so downstream cart
    building doesn't need to know which flow produced it."""
    user_payload = {
        "context_name": context_name,
        "needed_items": needed_items,
        "assumed_available": assumed_available,
        "inventory": _inventory_payload(combined_items),
    }

    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=2048,
            system=RECIPE_GAP_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(user_payload, indent=2),
                }
            ],
        )
    except Exception:
        logger.exception("Claude recipe gap analysis request failed")
        raise

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(text)
    return GapAnalysis(**data)


async def full_restock_list(reference_date: Optional[datetime] = None) -> GapAnalysis:
    """No-photo restock flow: everything needed for tomorrow, minus pantry staples."""
    day_name, meals = get_tomorrow_meals(reference_date)
    plan = _load_diet_plan()
    pantry_staples = {s.lower() for s in plan["pantry_staples"]}

    raw_ingredients = build_ingredient_list(meals)
    meal_names = [meal["name"] for meal in meals.values()]

    merged: dict[str, list[str]] = {}
    for ing in raw_ingredients:
        name = ing["item"]
        if name.lower() in pantry_staples:
            continue
        merged.setdefault(name, []).append(ing["qty"])

    shopping_list = [
        ShoppingListItem(
            item=name,
            qty=" + ".join(qtys) if len(qtys) > 1 else qtys[0],
            reason="Needed for tomorrow's meals" if len(qtys) == 1 else "Needed across multiple meals tomorrow",
        )
        for name, qtys in merged.items()
    ]

    return GapAnalysis(tomorrow=day_name, meals=meal_names, shopping_list=shopping_list)


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in Claude response: {text!r}")
    return json.loads(text[start : end + 1])
