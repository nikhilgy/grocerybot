"""Interprets a freeform inventory correction ("I actually have more paneer", "that's wrong,
it's empty") against one item's currently stored quantity/stock_level.

Single-purpose Claude-backed module, same shape as recipe_generator.py / product_mapper.py.
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

SYSTEM_PROMPT = """You help keep a kitchen inventory accurate. The user is correcting what's
currently stored for ONE item, based on what they just said.

You're given:
- The item's currently stored quantity and stock_level (full/adequate/low/critical/empty).
- The user's correction, verbatim.

Work out the corrected quantity and/or stock_level. If the correction doesn't clearly imply a new
value for a field, return null for that field so it stays unchanged (don't guess).

Return JSON only:
{"quantity": "<new quantity string, or null to leave unchanged>",
 "stock_level": "full"|"adequate"|"low"|"critical"|"empty"|null}
"""


class CorrectionResult(BaseModel):
    quantity: Optional[str] = None
    stock_level: Optional[str] = None


async def interpret_correction(item_name: str, current: dict, correction_text: str) -> CorrectionResult:
    payload = {
        "item_name": item_name,
        "current": current,
        "correction": correction_text,
    }
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
    except Exception:
        logger.exception("Correction interpretation request failed for %s", item_name)
        return CorrectionResult()

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1])
        return CorrectionResult(**data)
    except Exception:
        logger.warning("Could not parse correction response for %s: %r", item_name, text)
        return CorrectionResult()
