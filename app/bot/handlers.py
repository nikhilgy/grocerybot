"""Telegram update routing: commands, photos, voice notes, quick-add text, callbacks."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import config
from app.bot import keyboards
from app.bot.copy import HELP_TEXT, START_TEXT
from app.bot.voice import download_voice, transcribe
from app.db import queries
from app.instamart.mcp_client import SwiggyMCPError, client as mcp_client
from app.recipes.youtube import YOUTUBE_URL_RE
from app.services import orchestrator

logger = logging.getLogger(__name__)

# --- Photo batching state (per chat_id) -----------------------------------

_photo_buffers: dict[int, list[bytes]] = defaultdict(list)
_photo_flush_tasks: dict[int, asyncio.Task] = {}

# chat_id -> {content hash: seen_at} of recently processed photos, so an exact
# resend within PHOTO_DEDUP_WINDOW_SECONDS is dropped instead of reprocessed.
_recent_photo_hashes: dict[int, dict[str, datetime]] = defaultdict(dict)


async def _flush_photos(chat_id: int, bot) -> None:
    await asyncio.sleep(config.PHOTO_BATCH_WINDOW_SECONDS)
    images = _photo_buffers.pop(chat_id, [])
    _photo_flush_tasks.pop(chat_id, None)
    if not images:
        return
    await orchestrator.process_fridge_photos(bot, chat_id, images)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    photo_sizes = update.message.photo
    if not photo_sizes:
        return

    largest = photo_sizes[-1]
    telegram_file = await context.bot.get_file(largest.file_id)
    image_bytes = bytes(await telegram_file.download_as_bytearray())

    digest = hashlib.sha256(image_bytes).hexdigest()
    now = datetime.now()
    seen = _recent_photo_hashes[chat_id]
    for h in [h for h, t in seen.items() if (now - t).total_seconds() > config.PHOTO_DEDUP_WINDOW_SECONDS]:
        del seen[h]
    if digest in seen:
        return
    seen[digest] = now

    _photo_buffers[chat_id].append(image_bytes)

    existing_task = _photo_flush_tasks.get(chat_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    _photo_flush_tasks[chat_id] = asyncio.create_task(_flush_photos(chat_id, context.bot))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "🎙️ Processing voice note...")

    telegram_file = await context.bot.get_file(update.message.voice.file_id)
    audio_bytes = await download_voice(telegram_file)

    try:
        text = await transcribe(audio_bytes)
    except Exception:
        logger.exception("Voice transcription failed")
        await context.bot.send_message(chat_id, "⚠️ Having trouble analyzing right now. Please try again in a moment.")
        return

    await orchestrator.route_text_input(context.bot, chat_id, text, source="voice")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text
    await orchestrator.route_text_input(context.bot, chat_id, text, source="text")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_restock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await orchestrator.process_restock(context.bot, chat_id)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        orders = await mcp_client.get_orders()
    except SwiggyMCPError:
        logger.exception("get_orders failed")
        await context.bot.send_message(chat_id, "⚠️ Can't reach Swiggy right now. Please try again in a moment.")
        return

    if not orders:
        await context.bot.send_message(chat_id, "No past orders found.")
        return

    recent = orders[:10]
    lines = ["<b>📦 Recent orders</b>", ""]
    for o in recent:
        date_str = o.placed_at or "unknown date"
        lines.append(f"• {date_str} — {o.item_count} items — ₹{o.total_amount:.0f} ({o.status or 'unknown'})")

    await context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_spend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        orders = await mcp_client.get_orders()
    except SwiggyMCPError:
        logger.exception("get_orders failed")
        await context.bot.send_message(chat_id, "⚠️ Can't reach Swiggy right now. Please try again in a moment.")
        return

    now = datetime.now()
    this_week_start = now - timedelta(days=now.weekday())
    last_week_start = this_week_start - timedelta(days=7)

    this_week_total, this_week_count = 0.0, 0
    last_week_total, last_week_count = 0.0, 0

    for o in orders:
        if not o.placed_at:
            continue
        try:
            placed = datetime.fromisoformat(o.placed_at)
        except ValueError:
            continue
        if placed >= this_week_start:
            this_week_total += o.total_amount
            this_week_count += 1
        elif placed >= last_week_start:
            last_week_total += o.total_amount
            last_week_count += 1

    this_avg = this_week_total / 7
    last_avg = last_week_total / 7

    text = (
        "<b>📊 Spending summary</b>\n\n"
        f"This week: ₹{this_week_total:.0f} across {this_week_count} orders\n"
        f"Last week: ₹{last_week_total:.0f} across {last_week_count} orders\n\n"
        f"Average daily: ₹{this_avg:.0f} this week vs ₹{last_avg:.0f} last week"
    )
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def cmd_zones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    staleness = await queries.get_all_zones_staleness()

    lines = ["🗺️ Your kitchen zones:", ""]
    for z in staleness:
        icon = orchestrator.ZONE_EMOJI.get(z.zone_id, orchestrator.STALENESS_ICON[z.tier])
        if z.tier == "no_data":
            lines.append(f"{icon} {z.display_name} — never scanned")
        else:
            age = orchestrator.humanize_age(z.last_scanned)
            lines.append(f"{icon} {z.display_name} — scanned {age} ({z.item_count} items)")

    await context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    zone_arg = context.args[0] if context.args else None

    if zone_arg:
        text = await orchestrator.format_zone_inventory_text(zone_arg)
    else:
        text = await orchestrator.format_full_inventory_text()

    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def cmd_addzone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await context.bot.send_message(chat_id, "Usage: /addzone <name>, e.g. /addzone spice_drawer")
        return

    raw_name = " ".join(context.args)
    zone_id = re.sub(r"[^a-z0-9]+", "_", raw_name.lower()).strip("_")
    if not zone_id:
        await context.bot.send_message(chat_id, "That name didn't produce a valid zone id — try again.")
        return

    zone = await queries.create_zone(zone_id, raw_name.strip().title())
    await context.bot.send_message(chat_id, f"✅ Added zone: {zone.display_name} ({zone.id})")


async def cmd_removezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await context.bot.send_message(chat_id, "Usage: /removezone <zone_id> (see /zones for ids)")
        return

    zone_id = context.args[0]
    ok = await queries.delete_zone(zone_id)
    if ok:
        await context.bot.send_message(chat_id, f"🗑️ Removed zone: {zone_id}")
    else:
        await context.bot.send_message(chat_id, f"⚠️ No zone found with id '{zone_id}'.")


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await context.bot.send_message(
            chat_id,
            "Send me a recipe name or paste a YouTube link, e.g. "
            "<code>/recipe palak paneer</code> or <code>/recipe https://youtu.be/...</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    query = " ".join(context.args)
    if YOUTUBE_URL_RE.search(query):
        await orchestrator.process_youtube_url(context.bot, chat_id, query)
        return

    await orchestrator.process_recipe_intent(context.bot, chat_id, query, servings=None)


# --------------------------------------------------------------------------
# Callback dispatch
# --------------------------------------------------------------------------

_EXACT_ROUTES: dict[str, Callable[..., Awaitable]] = {
    keyboards.CANCEL_ACTION: orchestrator.cancel_order,
    keyboards.DUPLICATE_ADD_ACTION: orchestrator.confirm_duplicate_additions,
    keyboards.DUPLICATE_SKIP_ACTION: orchestrator.skip_duplicate_additions,
    keyboards.RUNLOW_ADD_ALL: orchestrator.running_low_add_all,
    keyboards.RUNLOW_PICK: orchestrator.running_low_start_pick,
    keyboards.RUNLOW_SKIP: orchestrator.running_low_skip,
    keyboards.RUNLOW_CONFIRM: orchestrator.running_low_confirm_pick,
    keyboards.INV_CONFIRM_OK: orchestrator.inventory_confirm_ok,
    keyboards.INV_CONFIRM_RESCAN: orchestrator.inventory_confirm_rescan,
    keyboards.INV_CONFIRM_CANCEL: orchestrator.inventory_confirm_cancel,
    keyboards.VOICE_CONFIRM_YES: orchestrator.voice_confirm_yes,
    keyboards.VOICE_CONFIRM_RETYPE: orchestrator.voice_confirm_retype,
}

_PREFIX_ROUTES: list[tuple[str, Callable[..., Awaitable]]] = [
    (keyboards.REMOVE_PREFIX, orchestrator.remove_item_from_cart),
    (keyboards.ZONE_PICK_PREFIX, orchestrator.zone_picked),
    (keyboards.SERVINGS_PREFIX, orchestrator.servings_picked),
    (keyboards.GUEST_MEAL_PREFIX, orchestrator.guest_meal_picked),
]


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    handler = _EXACT_ROUTES.get(data)
    if handler:
        await handler(context.bot, chat_id)
        return

    if data.startswith(keyboards.RUNLOW_TOGGLE_PREFIX):
        index = keyboards.parse_runlow_toggle_callback(data)
        if index is not None:
            await orchestrator.running_low_toggle(context.bot, chat_id, index, query)
        return

    for prefix, handler_fn in _PREFIX_ROUTES:
        if data.startswith(prefix):
            await handler_fn(context.bot, chat_id, data[len(prefix) :])
            return

    logger.warning("Unhandled callback data: %r", data)
