"""Claude-based classification of free text into a handling intent.

YouTube URLs are deliberately NOT a classifier output — that's a deterministic
regex check done before this runs (cheaper and never misclassified).
"""
from __future__ import annotations

import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel

from app import config

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Classify this user message into one of these intents:
- "greeting": hi, hello, good morning, hey
- "thanks": thank you, great, awesome, perfect
- "quick_add": user wants to add specific items to cart (e.g. "add 1kg oats", "order paneer")
- "recipe": user wants to cook something (e.g. "make palak paneer", "I want to cook biryani for 4")
- "guest": user mentions guests coming (e.g. "2 guests for dinner tonight")
- "youtube": message contains a YouTube URL
- "plan_query": asking about meal plan (what's for lunch, dinner tomorrow)
- "inventory_query": asking what's in their kitchen (do I have X, what's in the fridge)
- "cart_query": asking about current cart status
- "correction": correcting a previous analysis (I actually have more X, that's wrong)
- "cancel": wants to cancel current action
- "help": asking what the bot can do
- "general": anything else / off-topic

Extract into "details" only for the intents below (all others get an empty object):
- "recipe": {"recipe_name": "<name>", "servings": <int or null if not mentioned>}.
- "guest": {"guest_count": <int>, "meal": "lunch"|"dinner"|"both"|null if not mentioned}.
- "plan_query": {"meal": "breakfast"|"lunch"|"snack"|"dinner"|null if asking about the whole day}.
- "inventory_query": {"item": "<item name>"|null, "zone": "<zone name/hint the user gave>"|null}.
- "correction": {"item": "<item name>", "correction_text": "<verbatim what they said about it>",
  "zone": "<zone name/hint>"|null}.

If the message BOTH names a specific dish to cook AND mentions a guest/people count (e.g. "make palak paneer
for 6 guests"), classify it as "recipe" and set "servings" to the guest count — do NOT use "guest" intent in
that case, since the guest count and the recipe's serving count are the same number.

Return JSON only: {"intent": "...", "details": {...}}
"""


class Intent(BaseModel):
    intent: Literal[
        "greeting",
        "thanks",
        "quick_add",
        "recipe",
        "guest",
        "youtube",
        "plan_query",
        "inventory_query",
        "cart_query",
        "correction",
        "cancel",
        "help",
        "general",
    ]
    details: dict = {}


async def classify_intent(text: str) -> Intent:
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
    except Exception:
        logger.exception("Intent classification request failed")
        return Intent(intent="general", details={})

    raw = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1])
        return Intent(**data)
    except Exception:
        logger.warning("Could not parse intent classification response: %r", raw)
        return Intent(intent="general", details={})
