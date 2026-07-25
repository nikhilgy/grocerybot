"""YouTube recipe import: metadata + caption extraction via yt-dlp, Claude-based recipe parsing."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

import anthropic
import httpx
import yt_dlp
from pydantic import BaseModel

from app import config
from app.recipes.recipe_generator import RecipeBase

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

YOUTUBE_URL_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]+)")

_YDL_OPTS = {"skip_download": True, "quiet": True, "no_warnings": True}
_PREFERRED_CAPTION_LANGS = ("en", "hi")


class NotARecipeError(Exception):
    """Raised when the video has a transcript but Claude determines it isn't a recipe."""


YOUTUBE_EXTRACT_SYSTEM_PROMPT = """You are a recipe extraction expert. You're given a cooking
video's title and a transcript (real captions, or the video description if no captions exist).

Extract:
1. The recipe name — the title almost always names the dish; trust it over the transcript when
   they disagree (e.g. strip channel branding like "| Ranveer Brar" or "Full Recipe" from the
   title to get the clean dish name).
2. The default number of servings (if mentioned, otherwise assume 2)
3. Every ingredient with exact quantities
4. Brief cooking steps (optional, keep concise)

If the transcript is not a recipe or is unclear, say so.

Separate ingredients into "need_to_check" (perishables, proteins, vegetables) and "assumed_available"
(pantry staples, basic spices — plain strings, not objects).

Return as JSON:
{
  "recipe_name": "Palak Paneer",
  "source": "YouTube",
  "default_servings": 4,
  "need_to_check": [
    {"item": "paneer", "qty": "400g"},
    {"item": "spinach", "qty": "2 bunches"}
  ],
  "assumed_available": ["salt", "cooking oil", "black pepper"],
  "steps": ["Step 1...", "Step 2..."],
  "is_recipe": true
}

If the transcript is clearly not a recipe, set "is_recipe": false and leave the other fields empty/default.
"""


class ExtractedRecipe(RecipeBase):
    source: str = "YouTube"
    channel: Optional[str] = None
    steps: list[str] = []


class _VideoData(BaseModel):
    transcript: str
    title: Optional[str] = None
    channel: Optional[str] = None


def _video_id(url: str) -> Optional[str]:
    match = YOUTUBE_URL_RE.search(url)
    return match.group(1) if match else None


async def _extract_video_info(video_id: str) -> Optional[dict]:
    """Runs yt-dlp's (synchronous, blocking) metadata extraction off the event loop."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    def _run() -> dict:
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        logger.exception("yt-dlp extraction failed for %s", video_id)
        return None


def _pick_caption_url(info: dict) -> Optional[str]:
    """Prefers manual subtitles over auto-generated ones, en/hi over other languages, vtt format."""
    for key in ("subtitles", "automatic_captions"):
        tracks: dict = info.get(key) or {}
        if not tracks:
            continue
        langs = [lang for lang in _PREFERRED_CAPTION_LANGS if lang in tracks] or list(tracks.keys())
        for lang in langs:
            formats = tracks.get(lang) or []
            if not formats:
                continue
            chosen = next((f for f in formats if f.get("ext") == "vtt"), formats[0])
            if chosen.get("url"):
                return chosen["url"]
    return None


def _vtt_to_text(vtt_content: str) -> str:
    """Strips VTT headers/cue-numbers/timestamps/tags and de-dupes YouTube auto-caption's
    repeated rolling-caption lines, down to plain transcript text."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in vtt_content.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


async def extract_transcript_and_metadata(video_id: str) -> Optional[_VideoData]:
    """yt-dlp gives us title/channel/captions/description in one call. Prefers real captions;
    falls back to the video description as a pseudo-transcript if no caption track exists at all."""
    info = await _extract_video_info(video_id)
    if not info:
        return None

    title = info.get("title")
    channel = info.get("uploader")

    transcript = None
    caption_url = _pick_caption_url(info)
    if caption_url:
        try:
            async with httpx.AsyncClient(timeout=10) as http_client:
                resp = await http_client.get(caption_url)
                resp.raise_for_status()
            transcript = _vtt_to_text(resp.text)
        except Exception:
            logger.warning("Failed to fetch/parse captions for %s", video_id)

    if not transcript or not transcript.strip():
        transcript = info.get("description")

    if not transcript or not transcript.strip():
        return None

    return _VideoData(transcript=transcript, title=title, channel=channel)


async def extract_recipe_from_video(url: str) -> Optional[ExtractedRecipe]:
    """None if there's no usable transcript/description, or Claude determines it isn't a recipe video."""
    video_id = _video_id(url)
    if not video_id:
        return None

    video_data = await extract_transcript_and_metadata(video_id)
    if not video_data:
        return None

    user_payload = {"title": video_data.title, "transcript": video_data.transcript[:15000]}
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=1536,
            system=YOUTUBE_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(user_payload)}],
        )
    except Exception:
        logger.exception("YouTube recipe extraction request failed")
        return None

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        data = _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        logger.warning("Could not parse YouTube extraction response: %r", text)
        return None

    if not data.get("is_recipe", True):
        raise NotARecipeError()

    data["servings"] = data.get("default_servings") or 2
    data.pop("default_servings", None)
    data.pop("is_recipe", None)
    data["channel"] = video_data.channel  # ground truth from yt-dlp, not Claude's guess

    try:
        return ExtractedRecipe(**data)
    except Exception:
        logger.warning("YouTube extraction JSON didn't match expected shape: %r", data)
        return None


def _extract_json(text: str) -> dict:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in Claude response: {text!r}")
    return json.loads(text[start : end + 1])
