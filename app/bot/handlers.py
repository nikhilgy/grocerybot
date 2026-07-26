"""Telegram update routing: commands, photos, voice notes, quick-add text, callbacks."""
from __future__ import annotations

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

# --- Photo scan tray (per chat_id) ----------------------------------------
#
# Photos accumulate in a per-chat "scan tray" and are only analyzed when the
# user taps ✅ Analyze. Time can't tell "walking to the next shelf" apart from
# "done", so batching is driven entirely by the user's explicit intent.

_photo_buffers: dict[int, list[bytes]] = defaultdict(list)

# chat_id -> content hashes already in the current tray, so an identical frame
# (e.g. Telegram redelivery) isn't double-counted. Cleared on analyze/clear.
_photo_buffer_hashes: dict[int, set[str]] = defaultdict(set)

# chat_id -> message_id of the tray message, so it's edited in place rather than
# re-sent for every new photo.
_tray_message_ids: dict[int, int] = {}


def _reset_tray(chat_id: int) -> None:
    _photo_buffers.pop(chat_id, None)
    _photo_buffer_hashes.pop(chat_id, None)
    _tray_message_ids.pop(chat_id, None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    photo_sizes = update.message.photo
    if not photo_sizes:
        return

    largest = photo_sizes[-1]
    telegram_file = await context.bot.get_file(largest.file_id)
    image_bytes = bytes(await telegram_file.download_as_bytearray())

    digest = hashlib.sha256(image_bytes).hexdigest()
    if digest in _photo_buffer_hashes[chat_id]:
        return
    _photo_buffer_hashes[chat_id].add(digest)
    _photo_buffers[chat_id].append(image_bytes)

    count = len(_photo_buffers[chat_id])
    text = f"📸 {count} photo{'s' if count != 1 else ''} ready to analyze."
    keyboard = keyboards.photo_tray_keyboard(count)

    tray_message_id = _tray_message_ids.get(chat_id)
    if tray_message_id is not None:
        await context.bot.edit_message_text(
            text, chat_id=chat_id, message_id=tray_message_id, reply_markup=keyboard
        )
    else:
        sent = await context.bot.send_message(chat_id, text, reply_markup=keyboard)
        _tray_message_ids[chat_id] = sent.message_id


async def analyze_photo_tray(bot, chat_id: int) -> None:
    images = list(_photo_buffers.get(chat_id, []))
    tray_message_id = _tray_message_ids.get(chat_id)
    if not images:
        _reset_tray(chat_id)
        await bot.send_message(chat_id, "🤔 Nothing to analyze yet — send some photos first.")
        return
    _reset_tray(chat_id)
    # Drop the now-stale tray buttons before the analysis pipeline takes over.
    if tray_message_id is not None:
        count = len(images)
        await bot.edit_message_text(
            f"📸 {count} photo{'s' if count != 1 else ''} — analyzing…",
            chat_id=chat_id,
            message_id=tray_message_id,
        )
    await orchestrator.process_fridge_photos(bot, chat_id, images)


async def clear_photo_tray(bot, chat_id: int) -> None:
    tray_message_id = _tray_message_ids.get(chat_id)
    _reset_tray(chat_id)
    if tray_message_id is not None:
        await bot.edit_message_text("🗑️ Cleared.", chat_id=chat_id, message_id=tray_message_id)


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


async def cmd_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await orchestrator.set_delivery_address(context.bot, chat_id)


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
    keyboards.PHOTO_ANALYZE: analyze_photo_tray,
    keyboards.PHOTO_CLEAR: clear_photo_tray,
}

_PREFIX_ROUTES: list[tuple[str, Callable[..., Awaitable]]] = [
    (keyboards.ZONE_PICK_PREFIX, orchestrator.zone_picked),
    (keyboards.ADDRESS_PICK_PREFIX, orchestrator.address_picked),
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

    # Cart-edit callbacks edit the cart message in place, so they need the query.
    if data == keyboards.CART_EDIT_ACTION:
        await orchestrator.show_cart_edit(context.bot, chat_id, query)
        return
    if data == keyboards.CART_DONE_ACTION:
        await orchestrator.cart_edit_done(context.bot, chat_id, query)
        return
    if data.startswith(keyboards.REMOVE_PREFIX):
        spin_id = data[len(keyboards.REMOVE_PREFIX) :]
        await orchestrator.remove_item_from_cart(context.bot, chat_id, spin_id, query)
        return

    for prefix, handler_fn in _PREFIX_ROUTES:
        if data.startswith(prefix):
            await handler_fn(context.bot, chat_id, data[len(prefix) :])
            return

    logger.warning("Unhandled callback data: %r", data)
