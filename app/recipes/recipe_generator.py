"""Claude-based recipe ingredient generation. No recipe database or API —
Claude knows how to cook, and Indian home cooking varies by household anyway.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic
from pydantic import BaseModel

from app import config

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

RECIPE_SYSTEM_PROMPT = """You are an expert Indian home cook. The user wants to make: {recipe_name} for {servings} servings.

List every ingredient needed with exact quantities for {servings} servings. Use quantities available on Swiggy Instamart (Indian grocery delivery app) — so use grams, ml, pieces, bunches as units.

Separate ingredients into:
- "need_to_check": items that might already be in the kitchen (vegetables, dairy, proteins, grains)
- "assumed_available": pantry staples and basic spices (salt, oil, common spices)

Return as JSON:
{{
  "recipe_name": "Palak Paneer",
  "servings": 4,
  "cook_time": "40 mins",
  "need_to_check": [
    {{"item": "paneer", "qty": "400g"}},
    {{"item": "spinach", "qty": "2 bunches"}},
    {{"item": "onion", "qty": "2 medium"}},
    {{"item": "tomato", "qty": "2 medium"}},
    {{"item": "cream", "qty": "100ml"}},
    {{"item": "ginger", "qty": "1 inch piece"}},
    {{"item": "garlic", "qty": "4 cloves"}},
    {{"item": "green chili", "qty": "2"}}
  ],
  "assumed_available": [
    "salt", "cooking oil", "cumin seeds", "turmeric", "garam masala", "red chili powder", "kasuri methi"
  ]
}}
"""


class RecipeIngredient(BaseModel):
    item: str
    qty: str


class RecipeBase(BaseModel):
    recipe_name: str
    servings: int
    need_to_check: list[RecipeIngredient]
    assumed_available: list[str] = []


class GeneratedRecipe(RecipeBase):
    cook_time: Optional[str] = None


SCALE_SYSTEM_PROMPT = """You are an expert Indian home cook. Below is an ingredient list for a recipe
originally sized for {from_servings} servings. Rescale every quantity to {to_servings} servings, keeping
the same units style (grams, ml, pieces, bunches) as used on Swiggy Instamart.

Return as JSON:
{{
  "need_to_check": [{{"item": "...", "qty": "..."}}],
  "assumed_available": ["...", "..."]
}}
"""


async def scale_ingredients(
    need_to_check: list[dict], assumed_available: list[str], from_servings: int, to_servings: int
) -> tuple[list[RecipeIngredient], list[str]]:
    """Rescales an ingredient list via Claude — free-text quantities like '2 bunches' or
    '1 inch piece' aren't safely multipliable in Python, so this stays Claude's job."""
    if from_servings == to_servings:
        return [RecipeIngredient(**i) for i in need_to_check], assumed_available

    system = SCALE_SYSTEM_PROMPT.format(from_servings=from_servings, to_servings=to_servings)
    payload = {"need_to_check": need_to_check, "assumed_available": assumed_available}
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
    except Exception:
        logger.exception("Ingredient scaling request failed")
        return [RecipeIngredient(**i) for i in need_to_check], assumed_available

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        data = _extract_json(text)
        return [RecipeIngredient(**i) for i in data["need_to_check"]], data.get(
            "assumed_available", assumed_available
        )
    except Exception:
        logger.warning("Could not parse scaling response: %r", text)
        return [RecipeIngredient(**i) for i in need_to_check], assumed_available


async def generate_recipe(recipe_name: str, servings: int) -> GeneratedRecipe:
    system = RECIPE_SYSTEM_PROMPT.format(recipe_name=recipe_name, servings=servings)
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=1536,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate the ingredient list for {recipe_name}, {servings} servings.",
                }
            ],
        )
    except Exception:
        logger.exception("Recipe generation request failed")
        raise

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    data = _extract_json(text)
    return GeneratedRecipe(**data)


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in Claude response: {text!r}")
    return json.loads(text[start : end + 1])
