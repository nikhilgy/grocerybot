"""Ties vision, gap analysis, product mapping and cart management together.

Owns two kinds of ephemeral per-chat_id session state:
- CART_STATE / DUPLICATE_STATE: the running cart + duplicate-quantity prompts
  (unchanged from Phase 1, no database — cart state is intentionally ephemeral,
  this is a single-user personal bot).
- FLOW_STATE: a single generic "the bot asked a question, waiting for a
  button/text reply" slot per chat, used by every Phase 2 multi-turn flow
  (zone picks, servings picks, guest meal picks, running-low picks, inventory
  confirmation, voice-intent confirmation). One flow per chat at a time — a
  new flow overwrites an unresolved one; an orphaned callback press just
  replies that it's expired rather than queuing.

Kitchen inventory itself IS persisted now, in app/db/ (SQLite) — that's the
one piece of real state Phase 2 adds.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import anthropic
from telegram import Bot, CallbackQuery
from telegram.constants import ParseMode

from app import config
from app.bot import keyboards
from app.bot.copy import HELP_TEXT
from app.db import queries
from app.db.models import StoredInventoryItem, Zone, ZoneStaleness
from app.instamart import cart_manager
from app.instamart.mcp_client import SwiggyMCPError, client as mcp_client
from app.instamart.product_mapper import MappedProduct, UnavailableItem, map_shopping_list
from app.nlu.correction_interpreter import interpret_correction
from app.nlu.intent_classifier import Intent, classify_intent
from app.planner.gap_analyzer import (
    GapAnalysis,
    ShoppingListItem,
    analyze_gap,
    analyze_gap_for_needed_items,
    full_restock_list,
    get_tomorrow_meals,
)
from app.recipes.recipe_generator import generate_recipe, scale_ingredients
from app.recipes.youtube import NotARecipeError, YOUTUBE_URL_RE, extract_recipe_from_video
from app.vision.analyzer import FridgeInventory, ZoneGroup, analyze_photos

logger = logging.getLogger(__name__)


@dataclass
class PendingCart:
    day_name: str
    mapped_products: list[MappedProduct]
    unavailable: list[UnavailableItem] = field(default_factory=list)


@dataclass
class PendingDuplicates:
    """Items the user just asked for that are already in the cart, awaiting
    a yes/no on whether to add the extra quantity on top."""
    additions: list[tuple[MappedProduct, MappedProduct]]  # (existing item, proposed extra)


@dataclass
class PendingFlow:
    kind: str
    chat_id: int
    created_at: datetime
    payload: dict


# chat_id -> running cart accumulated across messages (quick-add, voice,
# /restock, photos all merge into the same one)
CART_STATE: dict[int, PendingCart] = {}

# chat_id -> duplicate-quantity additions awaiting confirmation
DUPLICATE_STATE: dict[int, PendingDuplicates] = {}

# chat_id -> whatever multi-turn question is currently pending (Phase 2)
FLOW_STATE: dict[int, PendingFlow] = {}

STALENESS_ICON = {
    "fresh": "✅",
    "recent": "⚠️",
    "warning": "⚠️",
    "critical": "🔴",
    "no_data": "❓",
}
STALENESS_LABEL = {
    "fresh": "",
    "recent": "",
    "warning": "might be outdated",
    "critical": "likely outdated — consider rescanning",
    "no_data": "no data",
}

ZONE_EMOJI = {
    "fridge": "🧊",
    "freezer": "❄️",
    "pantry": "📦",
    "dal_shelf": "🫙",
    "masala_rack": "🧂",
    "countertop": "🍳",
}


def humanize_age(last_scanned: Optional[datetime]) -> str:
    if last_scanned is None:
        return "never scanned"
    seconds = (datetime.now() - last_scanned).total_seconds()
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def _match_zone(zones: list[Zone], hint: str) -> Optional[Zone]:
    """Loose match of a free-text zone hint (e.g. "the fridge") against known zone ids/names."""
    needle = hint.lower().strip()
    for z in zones:
        if needle == z.id or needle == z.display_name.lower():
            return z
    for z in zones:
        if needle in z.id or needle in z.display_name.lower() or z.id in needle or z.display_name.lower() in needle:
            return z
    return None


async def format_zone_inventory_text(zone_id: str) -> str:
    """Shared by /inventory <zone> and the inventory_query intent."""
    items = await queries.get_zone_inventory(zone_id)
    zones = await queries.get_zones()
    display_name = next((z.display_name for z in zones if z.id == zone_id), zone_id)
    if not items:
        return f"No inventory recorded for {display_name}."
    lines = [f"📋 <b>{display_name}</b>:", ""]
    for i in items:
        qty = f" {i.quantity}" if i.quantity else ""
        brand = f" ({i.brand})" if i.brand else ""
        lines.append(f"• {i.item_name}{qty}{brand}")
    return "\n".join(lines)


async def format_full_inventory_text() -> str:
    """Shared by bare /inventory and the inventory_query intent."""
    all_inventory = await queries.get_all_inventory()
    staleness_by_zone = {z.zone_id: z for z in await queries.get_all_zones_staleness()}
    total_items = sum(len(items) for items in all_inventory.values())
    summarize = total_items > 40

    lines = ["📋 Kitchen inventory:", ""]
    for zone_id, items in all_inventory.items():
        z = staleness_by_zone.get(zone_id)
        icon = ZONE_EMOJI.get(zone_id, STALENESS_ICON[z.tier] if z else "❓")
        display_name = z.display_name if z else zone_id

        if not items:
            lines.append(f"{icon} {display_name}: no data")
            lines.append("")
            continue

        age = humanize_age(z.last_scanned) if z else ""
        if summarize:
            lines.append(
                f"{icon} <b>{display_name}</b> ({age}): {len(items)} items — "
                f"reply <code>/inventory {zone_id}</code> for the full list"
            )
        else:
            lines.append(f"{icon} <b>{display_name}</b> ({age}):")
            for i in items[:15]:
                qty = f" {i.quantity}" if i.quantity else ""
                lines.append(f"  • {i.item_name}{qty}")
            if len(items) > 15:
                lines.append(f"  +{len(items) - 15} more — reply <code>/inventory {zone_id}</code> for the full list")
        lines.append("")

    return "\n".join(lines)


async def _send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
    )


def _format_cart_message(pending: PendingCart) -> str:
    lines = [f"🛒 Grocery cart ({pending.day_name})", ""]

    if not pending.mapped_products:
        lines.append("Nothing found to add.")
    else:
        for i, p in enumerate(pending.mapped_products, start=1):
            lines.append(f"{i}. {p.name} × {p.units} — ₹{p.total_price:.0f}")

    total = sum(p.total_price for p in pending.mapped_products)
    lines.append("")
    lines.append(f"💰 Estimated total: ₹{total:.0f}")

    if pending.unavailable:
        unavailable_str = ", ".join(f"{u.item_name} ({u.qty})" for u in pending.unavailable)
        lines.append("")
        lines.append(f"⚠️ Not available: {unavailable_str}")

    lines.append("")
    lines.append("👉 This cart is synced to your Swiggy account — open the app below to review and pay.")

    return "\n".join(lines)


def _format_duplicate_message(dupes: list[tuple[MappedProduct, MappedProduct]]) -> str:
    lines = ["You already have these in your cart:", ""]
    for existing, addition in dupes:
        lines.append(f"• {existing.name} — currently × {existing.units}, add {addition.units} more?")
    lines.append("")
    lines.append("Add the extra quantity, or skip?")
    return "\n".join(lines)


async def _reconcile_cart_state(bot: Bot, chat_id: int) -> None:
    """If we think there's a pending cart but Swiggy's real cart is empty,
    the user must have checked out (or cleared it) in the app — reset our
    local state so the next addition starts a fresh cart instead of trying
    to merge on top of an order that's already gone."""
    pending = CART_STATE.get(chat_id)
    if not pending or not pending.mapped_products:
        return

    try:
        real_cart = await mcp_client.get_cart()
    except SwiggyMCPError:
        return  # can't check right now — don't block the flow over this

    if not real_cart.items:
        CART_STATE.pop(chat_id, None)
        DUPLICATE_STATE.pop(chat_id, None)
        await _send(
            bot,
            chat_id,
            "🎉 Looks like that order went through — your Swiggy cart is empty now. "
            "What would you like to add next?",
        )


async def _merge_and_confirm(
    bot: Bot, chat_id: int, day_name: str, mapped: list[MappedProduct], unavailable: list[UnavailableItem]
) -> None:
    existing = CART_STATE.get(chat_id)
    existing_by_spin = {p.spin_id: p for p in existing.mapped_products} if existing else {}

    brand_new = [p for p in mapped if p.spin_id not in existing_by_spin]
    dupes = [(existing_by_spin[p.spin_id], p) for p in mapped if p.spin_id in existing_by_spin]

    if not brand_new and not dupes:
        if unavailable:
            names = ", ".join(f"{u.item_name} ({u.qty})" for u in unavailable)
            await _send(
                bot,
                chat_id,
                f"❌ None of the items I need are available on Instamart right now.\n"
                f"Missing: {names}\nThis sometimes happens late at night.",
            )
        elif not existing:
            await _send(bot, chat_id, "✅ You're all stocked up for tomorrow! Nothing to order.")
        return

    if brand_new:
        merged_products = (existing.mapped_products if existing else []) + brand_new
        try:
            await cart_manager.build_cart(merged_products, mcp_client, config.DELIVERY_ADDRESS_ID)
        except SwiggyMCPError:
            logger.exception("Failed to build cart")
            await _send(
                bot,
                chat_id,
                "⚠️ Can't reach Swiggy right now. Please try again in a moment.",
            )
            return

        pending = PendingCart(day_name=day_name, mapped_products=merged_products, unavailable=unavailable)
        CART_STATE[chat_id] = pending
        text = _format_cart_message(pending)
        await _send(bot, chat_id, text, reply_markup=keyboards.cart_summary_keyboard(merged_products))
    elif unavailable:
        names = ", ".join(f"{u.item_name} ({u.qty})" for u in unavailable)
        await _send(bot, chat_id, f"⚠️ Not available: {names}")

    if dupes:
        DUPLICATE_STATE[chat_id] = PendingDuplicates(additions=dupes)
        await _send(
            bot, chat_id, _format_duplicate_message(dupes), reply_markup=keyboards.duplicate_confirm_keyboard()
        )


async def _map_and_confirm(bot: Bot, chat_id: int, gap: GapAnalysis) -> None:
    await _reconcile_cart_state(bot, chat_id)
    await _send(bot, chat_id, "🛒 Finding items on Instamart...")

    if not gap.shopping_list:
        if not CART_STATE.get(chat_id):
            await _send(bot, chat_id, "✅ You're all stocked up for tomorrow! Nothing to order.")
        return

    if not config.DELIVERY_ADDRESS_ID:
        await _send(bot, chat_id, "⚠️ No delivery address configured (DELIVERY_ADDRESS_ID). Can't search Instamart.")
        return

    try:
        mapped, unavailable = await map_shopping_list(
            [item.model_dump() for item in gap.shopping_list], mcp_client, config.DELIVERY_ADDRESS_ID
        )
    except SwiggyMCPError:
        logger.exception("Product mapping failed")
        await _send(
            bot,
            chat_id,
            "⚠️ Can't reach Swiggy right now. Please try again in a moment.",
        )
        return

    await _merge_and_confirm(bot, chat_id, gap.tomorrow, mapped, unavailable)


# --------------------------------------------------------------------------
# Feature 1: Kitchen zones — photo flow
# --------------------------------------------------------------------------

async def process_fridge_photos(bot: Bot, chat_id: int, images: list[bytes]) -> None:
    await _send(bot, chat_id, "📸 Analyzing your photos...")

    zones = await queries.get_zones()
    known_zones = [(z.id, z.display_name) for z in zones]
    zone_ids = {z.id for z in zones}

    try:
        inventory: FridgeInventory = await analyze_photos(images, known_zones)
    except Exception:
        logger.exception("Vision analysis failed")
        await _send(bot, chat_id, "⚠️ Having trouble analyzing right now. Please try again in a moment.")
        return

    if inventory.is_not_food:
        await _send(
            bot,
            chat_id,
            "🤔 This doesn't look like a fridge or pantry. Send me a photo of your fridge and I'll figure out what you need to order!",
        )
        return

    if inventory.is_blurry:
        await _send(
            bot,
            chat_id,
            "📸 I couldn't clearly identify items in this photo. Could you take another one with better lighting?",
        )
        return

    resolved_groups = [
        g for g in inventory.zones if g.zone_confidence == "high" and g.detected_zone in zone_ids
    ]
    ambiguous_groups = [g for g in inventory.zones if g not in resolved_groups]

    # High-confidence zones save immediately — no reason to hold them back
    # while waiting on a separate zone's clarification.
    running_low_items = await _save_zone_groups(bot, chat_id, resolved_groups)

    if ambiguous_groups:
        FLOW_STATE[chat_id] = PendingFlow(
            kind="zone_pick",
            chat_id=chat_id,
            created_at=datetime.now(),
            payload={
                "batch_zone_ids": [g.detected_zone for g in resolved_groups],
                "running_low_items": running_low_items,
                "pending_groups": [g.model_dump() for g in ambiguous_groups],
                "current_index": 0,
                "known_zones": known_zones,
            },
        )
        await _prompt_zone_pick(bot, chat_id)
        return

    batch_zone_ids = {g.detected_zone for g in resolved_groups}
    await _finish_zone_save(bot, chat_id, batch_zone_ids, running_low_items)


async def _prompt_zone_pick(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "zone_pick":
        return
    pending_groups = flow.payload["pending_groups"]
    idx = flow.payload["current_index"]
    group = pending_groups[idx]
    items_str = ", ".join(f"{i['name']} {i.get('quantity') or ''}".strip() for i in group["items"])
    known_zones = [tuple(z) for z in flow.payload["known_zones"]]
    await _send(
        bot,
        chat_id,
        f"🤔 Which zone is this? 📸 found: {items_str}",
        reply_markup=keyboards.zone_picker_keyboard(known_zones),
    )


async def zone_picked(bot: Bot, chat_id: int, zone_id: str) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "zone_pick":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return

    pending_groups = flow.payload["pending_groups"]
    idx = flow.payload["current_index"]
    group_dict = pending_groups[idx]
    group_dict["detected_zone"] = zone_id
    group_dict["zone_confidence"] = "high"
    group = ZoneGroup(**group_dict)

    running_low_items = await _save_zone_groups(bot, chat_id, [group])
    flow.payload["running_low_items"].extend(running_low_items)
    flow.payload["batch_zone_ids"].append(zone_id)

    idx += 1
    if idx < len(pending_groups):
        flow.payload["current_index"] = idx
        await _prompt_zone_pick(bot, chat_id)
        return

    batch_zone_ids = set(flow.payload["batch_zone_ids"])
    all_running_low = flow.payload["running_low_items"]
    FLOW_STATE.pop(chat_id, None)
    await _finish_zone_save(bot, chat_id, batch_zone_ids, all_running_low)


async def _save_zone_groups(bot: Bot, chat_id: int, groups: list[ZoneGroup]) -> list[dict]:
    """Persists each zone group's items, sends the per-zone "found" message,
    and collects any low/critical items for the (deferred, combined) running-low alert."""
    now = datetime.now()
    zones = await queries.get_zones()
    zone_names = {z.id: z.display_name for z in zones}
    running_low_items: list[dict] = []

    for group in groups:
        items_payload = [
            {
                "item_name": item.name,
                "quantity": item.quantity,
                "brand": item.brand,
                "container": item.container,
                "stock_level": item.stock_level,
            }
            for item in group.items
        ]
        # Always save, even when empty — an empty scan still replaces whatever
        # stale data was there before (a truly-emptied zone should read as
        # empty, not keep showing last week's contents).
        await queries.save_inventory(group.detected_zone, items_payload, scanned_at=now)

        display_name = zone_names.get(group.detected_zone, group.detected_zone)
        if group.items:
            item_lines = ", ".join(f"{i.name} {i.quantity}" if i.quantity else i.name for i in group.items)
            await _send(bot, chat_id, f"📸 <b>{display_name}</b> — I found: {item_lines}")
        else:
            await _send(bot, chat_id, f"📸 <b>{display_name}</b> — looks empty.")

        for item in group.items:
            if item.stock_level in ("low", "critical"):
                running_low_items.append(
                    {"name": item.name, "qty": item.quantity or "", "stock_level": item.stock_level}
                )

    return running_low_items


async def _finish_zone_save(bot: Bot, chat_id: int, batch_zone_ids: set[str], running_low_items: list[dict]) -> None:
    if running_low_items:
        await _send_running_low_alert(bot, chat_id, running_low_items)

    resume_flow = FLOW_STATE.get(chat_id)
    if resume_flow and resume_flow.kind == "awaiting_rescan":
        FLOW_STATE.pop(chat_id, None)
        await _resolve_needed_items_to_gap(bot, chat_id, **resume_flow.payload["resume"])
        return

    await _send(bot, chat_id, "📝 Checking tomorrow's meal plan...")
    combined = await queries.get_all_inventory_flat()

    other_zones_present = any(item.zone_id not in batch_zone_ids for item in combined)
    if other_zones_present:
        await _resolve_needed_items_to_gap(
            bot,
            chat_id,
            origin="photo_multizone",
            context_name="Tomorrow's meals",
            needed_items=[],
            assumed_available=[],
        )
        return

    try:
        gap = await analyze_gap(combined)
    except Exception:
        logger.exception("Gap analysis failed")
        await _send(bot, chat_id, "⚠️ Having trouble analyzing right now. Please try again in a moment.")
        return

    await _map_and_confirm(bot, chat_id, gap)


# --------------------------------------------------------------------------
# Feature 4: Running low detection
# --------------------------------------------------------------------------

async def _send_running_low_alert(bot: Bot, chat_id: int, items: list[dict]) -> None:
    lines = ["⚠️ Running low:", ""]
    for item in items:
        icon = "🔴" if item["stock_level"] == "critical" else "🟡"
        qty_str = f" — {item['qty']} left" if item.get("qty") else ""
        lines.append(f"{icon} {item['name'].title()}{qty_str} ({item['stock_level']})")

    FLOW_STATE[chat_id] = PendingFlow(
        kind="running_low", chat_id=chat_id, created_at=datetime.now(), payload={"items": items}
    )
    await _send(bot, chat_id, "\n".join(lines), reply_markup=keyboards.running_low_alert_keyboard())


async def running_low_add_all(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.pop(chat_id, None)
    if not flow or flow.kind != "running_low":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    await _add_running_low_items_to_cart(bot, chat_id, flow.payload["items"])


async def running_low_start_pick(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "running_low":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    items = [{**item, "selected": True} for item in flow.payload["items"]]
    flow.kind = "running_low_pick"
    flow.payload["items"] = items
    await _send(bot, chat_id, "Pick which items to add:", reply_markup=keyboards.running_low_pick_keyboard(items))


async def running_low_toggle(bot: Bot, chat_id: int, index: int, query: CallbackQuery) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "running_low_pick":
        await query.answer("This has expired — please try again.", show_alert=True)
        return
    items = flow.payload["items"]
    if 0 <= index < len(items):
        items[index]["selected"] = not items[index]["selected"]
    await query.edit_message_reply_markup(reply_markup=keyboards.running_low_pick_keyboard(items))


async def running_low_confirm_pick(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.pop(chat_id, None)
    if not flow or flow.kind != "running_low_pick":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    selected = [item for item in flow.payload["items"] if item["selected"]]
    if not selected:
        await _send(bot, chat_id, "👍 Nothing selected — skipped.")
        return
    await _add_running_low_items_to_cart(bot, chat_id, selected)


async def running_low_skip(bot: Bot, chat_id: int) -> None:
    FLOW_STATE.pop(chat_id, None)
    await _send(bot, chat_id, "👍 Skipped.")


async def _add_running_low_items_to_cart(bot: Bot, chat_id: int, items: list[dict]) -> None:
    gap = GapAnalysis(
        tomorrow="Running low restock",
        meals=[],
        shopping_list=[
            ShoppingListItem(item=item["name"], qty=item.get("qty") or "1", reason="Running low")
            for item in items
        ],
    )
    await _map_and_confirm(bot, chat_id, gap)


# --------------------------------------------------------------------------
# Feature 6: Inventory confirmation — shared funnel for recipe/youtube/guest/
# multi-zone-photo flows
# --------------------------------------------------------------------------

def _needs_confirmation(origin: str, staleness: list[ZoneStaleness]) -> bool:
    if origin == "photo_multizone":
        return True  # caller already determined other, non-fresh zones are contributing
    return not all(z.tier == "fresh" for z in staleness)


async def _compute_gap(
    origin: str,
    context_name: str,
    needed_items: list[dict],
    assumed_available: list[str],
    combined: list[StoredInventoryItem],
    guest_count: Optional[int],
    guest_meals: Optional[list[str]],
) -> GapAnalysis:
    if origin == "guest":
        return await analyze_gap(combined, guest_count=guest_count, guest_meals=guest_meals)
    if origin == "photo_multizone":
        return await analyze_gap(combined)
    return await analyze_gap_for_needed_items(context_name, needed_items, assumed_available, combined)


def _format_inventory_confirmation(
    staleness: list[ZoneStaleness], combined: list[StoredInventoryItem], gap: GapAnalysis
) -> str:
    by_zone: dict[str, list[StoredInventoryItem]] = {}
    for item in combined:
        by_zone.setdefault(item.zone_id, []).append(item)

    lines = ["📋 Here's what I think you currently have:", ""]
    for z in staleness:
        icon = STALENESS_ICON[z.tier]
        if z.tier == "no_data":
            lines.append(f"{icon} <b>{z.display_name}</b>: no data")
            continue
        age = humanize_age(z.last_scanned)
        items = by_zone.get(z.zone_id, [])
        names = ", ".join(f"{i.item_name} {i.quantity}" if i.quantity else i.item_name for i in items[:8])
        if len(items) > 8:
            names += f", +{len(items) - 8} more"
        label = STALENESS_LABEL[z.tier]
        suffix = f" — {label}" if label else ""
        lines.append(f"{icon} <b>{z.display_name}</b> (scanned {age}){suffix}:")
        lines.append(f"  {names}" if names else "  (empty)")
    lines.append("")
    lines.append(f"For <b>{gap.tomorrow}</b>, you need to order:")
    if not gap.shopping_list:
        lines.append("  Nothing — you're all set!")
    else:
        for item in gap.shopping_list:
            lines.append(f"  • {item.item} {item.qty} — {item.reason}")
    lines.append("")
    lines.append("Does this look right?")
    return "\n".join(lines)


async def _resolve_needed_items_to_gap(
    bot: Bot,
    chat_id: int,
    origin: Literal["recipe", "youtube", "guest", "photo_multizone"],
    context_name: str,
    needed_items: list[dict],
    assumed_available: list[str],
    guest_count: Optional[int] = None,
    guest_meals: Optional[list[str]] = None,
) -> None:
    combined = await queries.get_all_inventory_flat()
    staleness = await queries.get_all_zones_staleness()

    try:
        gap = await _compute_gap(
            origin, context_name, needed_items, assumed_available, combined, guest_count, guest_meals
        )
    except Exception:
        logger.exception("Gap analysis failed for origin=%s", origin)
        await _send(bot, chat_id, "⚠️ Having trouble analyzing right now. Please try again in a moment.")
        return

    if _needs_confirmation(origin, staleness):
        FLOW_STATE[chat_id] = PendingFlow(
            kind="inventory_confirm",
            chat_id=chat_id,
            created_at=datetime.now(),
            payload={
                "gap": gap.model_dump(),
                "resume": {
                    "origin": origin,
                    "context_name": context_name,
                    "needed_items": needed_items,
                    "assumed_available": assumed_available,
                    "guest_count": guest_count,
                    "guest_meals": guest_meals,
                },
            },
        )
        text = _format_inventory_confirmation(staleness, combined, gap)
        await _send(bot, chat_id, text, reply_markup=keyboards.inventory_confirm_keyboard())
        return

    await _map_and_confirm(bot, chat_id, gap)


async def inventory_confirm_ok(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.pop(chat_id, None)
    if not flow or flow.kind != "inventory_confirm":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    gap = GapAnalysis(**flow.payload["gap"])
    await _map_and_confirm(bot, chat_id, gap)


async def inventory_confirm_rescan(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "inventory_confirm":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    flow.kind = "awaiting_rescan"
    await _send(bot, chat_id, "📸 Send photos of the zones you want to update.")


async def inventory_confirm_cancel(bot: Bot, chat_id: int) -> None:
    FLOW_STATE.pop(chat_id, None)
    await _send(bot, chat_id, "🚫 Cancelled.")


# --------------------------------------------------------------------------
# Feature 2: Recipe mode
# --------------------------------------------------------------------------

async def process_recipe_intent(bot: Bot, chat_id: int, recipe_name: str, servings: Optional[int]) -> None:
    if not servings:
        FLOW_STATE[chat_id] = PendingFlow(
            kind="servings_pick",
            chat_id=chat_id,
            created_at=datetime.now(),
            payload={"origin": "recipe", "recipe_name": recipe_name},
        )
        await _send(
            bot, chat_id, f"How many servings for <b>{recipe_name}</b>?", reply_markup=keyboards.servings_keyboard()
        )
        return
    await _generate_and_resolve_recipe(bot, chat_id, recipe_name, servings)


async def _generate_and_resolve_recipe(bot: Bot, chat_id: int, recipe_name: str, servings: int) -> None:
    await _send(bot, chat_id, f"👩‍🍳 Working out what you need for {recipe_name}...")
    try:
        recipe = await generate_recipe(recipe_name, servings)
    except Exception:
        logger.exception("Recipe generation failed")
        await _send(bot, chat_id, "⚠️ Having trouble working out that recipe right now. Please try again in a moment.")
        return

    await _resolve_needed_items_to_gap(
        bot,
        chat_id,
        origin="recipe",
        context_name=f"{recipe.recipe_name} ({recipe.servings} servings)",
        needed_items=[i.model_dump() for i in recipe.need_to_check],
        assumed_available=recipe.assumed_available,
    )


# --------------------------------------------------------------------------
# Feature 5: YouTube recipe import
# --------------------------------------------------------------------------

async def process_youtube_url(bot: Bot, chat_id: int, url: str) -> None:
    match = YOUTUBE_URL_RE.search(url)
    if not match:
        await _send(bot, chat_id, "🤔 That doesn't look like a YouTube link I can read.")
        return

    await _send(bot, chat_id, "🎬 Extracting the recipe from that video...")
    try:
        extracted = await extract_recipe_from_video(match.group(0))
    except NotARecipeError:
        await _send(
            bot,
            chat_id,
            "🤔 This doesn't seem to be a recipe video. Try sending a recipe name instead.",
        )
        return
    except Exception:
        logger.exception("YouTube extraction failed")
        extracted = None

    if not extracted:
        await _send(
            bot,
            chat_id,
            "This video doesn't have captions available. Try sharing a recipe link from a site like "
            "Hebbar's Kitchen or send me the recipe name and I'll look it up.",
        )
        return

    ingredients_str = "\n".join(f"• {i.item} {i.qty}" for i in extracted.need_to_check)
    channel_str = f" by {extracted.channel}" if extracted.channel else ""
    text = (
        f"🎬 Found recipe: <b>{extracted.recipe_name}</b>{channel_str}\n"
        f"Default servings: {extracted.servings}\n\n"
        f"Ingredients:\n{ingredients_str}\n\n"
        "How many servings do you want to make?"
    )
    FLOW_STATE[chat_id] = PendingFlow(
        kind="servings_pick",
        chat_id=chat_id,
        created_at=datetime.now(),
        payload={"origin": "youtube", "extracted": extracted.model_dump()},
    )
    await _send(bot, chat_id, text, reply_markup=keyboards.servings_keyboard([1, 2, 4, 6]))


# --------------------------------------------------------------------------
# Servings picker (shared by recipe + youtube)
# --------------------------------------------------------------------------

async def servings_picked(bot: Bot, chat_id: int, value: str) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "servings_pick":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return

    if value == "other":
        flow.kind = "servings_pick_awaiting_number"
        await _send(bot, chat_id, "How many servings? Send me a number.")
        return

    servings = int(value)
    payload = flow.payload
    FLOW_STATE.pop(chat_id, None)
    await _resolve_servings(bot, chat_id, payload, servings)


async def handle_flow_text_reply(bot: Bot, chat_id: int, text: str) -> None:
    flow = FLOW_STATE.get(chat_id)
    if not flow or flow.kind != "servings_pick_awaiting_number":
        return
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        await _send(bot, chat_id, "I didn't catch a number — how many servings?")
        return
    servings = max(1, int(digits))
    payload = flow.payload
    FLOW_STATE.pop(chat_id, None)
    await _resolve_servings(bot, chat_id, payload, servings)


async def _resolve_servings(bot: Bot, chat_id: int, payload: dict, servings: int) -> None:
    origin = payload["origin"]
    if origin == "recipe":
        await _generate_and_resolve_recipe(bot, chat_id, payload["recipe_name"], servings)
        return

    extracted = dict(payload["extracted"])
    old_servings = extracted["servings"]
    scaled_items, scaled_available = await scale_ingredients(
        extracted["need_to_check"], extracted["assumed_available"], old_servings, servings
    )
    await _resolve_needed_items_to_gap(
        bot,
        chat_id,
        origin="youtube",
        context_name=f"{extracted['recipe_name']} ({servings} servings)",
        needed_items=[i.model_dump() for i in scaled_items],
        assumed_available=scaled_available,
    )


# --------------------------------------------------------------------------
# Feature 3: Guest mode
# --------------------------------------------------------------------------

async def process_guest_intent(bot: Bot, chat_id: int, guest_count: int, meal: Optional[str]) -> None:
    if not meal:
        FLOW_STATE[chat_id] = PendingFlow(
            kind="guest_meal_pick",
            chat_id=chat_id,
            created_at=datetime.now(),
            payload={"guest_count": guest_count},
        )
        await _send(
            bot, chat_id, f"🎉 {guest_count} guests — which meal?", reply_markup=keyboards.guest_meal_keyboard()
        )
        return
    await _run_guest_flow(bot, chat_id, guest_count, meal)


async def guest_meal_picked(bot: Bot, chat_id: int, meal: str) -> None:
    flow = FLOW_STATE.pop(chat_id, None)
    if not flow or flow.kind != "guest_meal_pick":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    await _run_guest_flow(bot, chat_id, flow.payload["guest_count"], meal)


async def _run_guest_flow(bot: Bot, chat_id: int, guest_count: int, meal: str) -> None:
    guest_meals = ["lunch", "dinner"] if meal == "both" else [meal]
    await _send(bot, chat_id, "📝 Checking tomorrow's meal plan...")
    await _resolve_needed_items_to_gap(
        bot,
        chat_id,
        origin="guest",
        context_name=f"Guests ({guest_count} people, {meal})",
        needed_items=[],
        assumed_available=[],
        guest_count=guest_count,
        guest_meals=guest_meals,
    )


# --------------------------------------------------------------------------
# Greeting / thanks / help
# --------------------------------------------------------------------------

async def process_greeting(bot: Bot, chat_id: int) -> None:
    await _send(
        bot, chat_id, "👋 Hey! Send a fridge photo, tell me what to cook, or type /help to see everything I can do."
    )


async def process_thanks(bot: Bot, chat_id: int) -> None:
    await _send(bot, chat_id, "🙌 Anytime! Let me know if you need anything else.")


async def process_help_intent(bot: Bot, chat_id: int) -> None:
    await _send(bot, chat_id, HELP_TEXT)


# --------------------------------------------------------------------------
# Plan / inventory / cart queries
# --------------------------------------------------------------------------

async def process_plan_query(bot: Bot, chat_id: int, meal: Optional[str]) -> None:
    day_name, meals = get_tomorrow_meals()
    if meal and meal in meals:
        await _send(bot, chat_id, f"🍽️ Tomorrow's {meal}: <b>{meals[meal]['name']}</b>")
        return
    lines = [f"🍽️ Tomorrow ({day_name}):", ""]
    for meal_type, meal_info in meals.items():
        lines.append(f"• <b>{meal_type.title()}</b>: {meal_info['name']}")
    await _send(bot, chat_id, "\n".join(lines))


async def process_inventory_query(bot: Bot, chat_id: int, item: Optional[str], zone_hint: Optional[str]) -> None:
    zones = await queries.get_zones()
    target_zone = _match_zone(zones, zone_hint) if zone_hint else None

    if item:
        matches = await queries.find_items_by_name(item, zone_id=target_zone.id if target_zone else None)
        if not matches:
            where = f" in your {target_zone.display_name}" if target_zone else ""
            await _send(bot, chat_id, f"🤔 I don't have any {item} recorded{where}. Send a photo and I'll check.")
            return
        zone_names = {z.id: z.display_name for z in zones}
        lines = [f"📋 Found <b>{item}</b>:", ""]
        for i in matches:
            qty = f" {i.quantity}" if i.quantity else ""
            lines.append(f"• {i.item_name}{qty} — {zone_names.get(i.zone_id, i.zone_id)}")
        await _send(bot, chat_id, "\n".join(lines))
        return

    if target_zone:
        await _send(bot, chat_id, await format_zone_inventory_text(target_zone.id))
        return

    await _send(bot, chat_id, await format_full_inventory_text())


async def process_cart_query(bot: Bot, chat_id: int) -> None:
    pending = CART_STATE.get(chat_id)
    if not pending or not pending.mapped_products:
        await _send(bot, chat_id, "🛒 Your cart is empty right now.")
        return
    await _send(
        bot, chat_id, _format_cart_message(pending), reply_markup=keyboards.cart_summary_keyboard(pending.mapped_products)
    )


# --------------------------------------------------------------------------
# Cancel (generic, text-driven)
# --------------------------------------------------------------------------

async def process_cancel_intent(bot: Bot, chat_id: int) -> None:
    if FLOW_STATE.pop(chat_id, None):
        await _send(bot, chat_id, "🚫 Cancelled — go ahead and try something else.")
        return
    if CART_STATE.get(chat_id):
        await cancel_order(bot, chat_id)
        return
    await _send(bot, chat_id, "There's nothing active for me to cancel right now.")


# --------------------------------------------------------------------------
# Inventory correction
# --------------------------------------------------------------------------

async def process_correction_intent(
    bot: Bot, chat_id: int, item_name: str, correction_text: str, zone_hint: Optional[str]
) -> None:
    zones = await queries.get_zones()
    target_zone = _match_zone(zones, zone_hint) if zone_hint else None

    matches = await queries.find_items_by_name(item_name, zone_id=target_zone.id if target_zone else None)
    if not matches:
        where = f" in your {target_zone.display_name}" if target_zone else " recorded anywhere"
        await _send(bot, chat_id, f"🤔 I don't have {item_name}{where} yet. Send a photo of where it is and I'll add it.")
        return

    item = max(matches, key=lambda i: i.scanned_at)
    result = await interpret_correction(
        item.item_name, {"quantity": item.quantity, "stock_level": item.stock_level}, correction_text
    )

    if result.quantity is None and result.stock_level is None:
        await _send(
            bot,
            chat_id,
            f"🤔 I couldn't tell what to update for {item.item_name} — try being more specific "
            f"(e.g. \"I have 500g {item.item_name} now\").",
        )
        return

    await queries.update_item(item.id, quantity=result.quantity, stock_level=result.stock_level)

    zone_names = {z.id: z.display_name for z in zones}
    new_qty = result.quantity or item.quantity or "updated"
    await _send(
        bot, chat_id, f"✅ Got it — updated {item.item_name} in {zone_names.get(item.zone_id, item.zone_id)} to {new_qty}."
    )


# --------------------------------------------------------------------------
# Text/voice intent routing
# --------------------------------------------------------------------------

async def route_text_input(bot: Bot, chat_id: int, text: str, source: Literal["text", "voice"] = "text") -> None:
    flow = FLOW_STATE.get(chat_id)
    if flow and flow.kind == "servings_pick_awaiting_number":
        await handle_flow_text_reply(bot, chat_id, text)
        return

    if YOUTUBE_URL_RE.search(text):
        await process_youtube_url(bot, chat_id, text)
        return

    intent = await classify_intent(text)
    await route_intent(bot, chat_id, intent, text=text, source=source)


async def route_intent(bot: Bot, chat_id: int, intent: Intent, *, text: str, source: str) -> None:
    details = intent.details or {}

    if intent.intent == "quick_add":
        await process_quick_add(bot, chat_id, text)
        return

    if intent.intent == "recipe":
        recipe_name = details.get("recipe_name") or text
        servings = details.get("servings")
        if source == "voice":
            await _show_voice_confirmation(bot, chat_id, intent, text)
            return
        await process_recipe_intent(bot, chat_id, recipe_name, servings)
        return

    if intent.intent == "guest":
        guest_count = details.get("guest_count")
        if not guest_count:
            await _send(bot, chat_id, "🤔 How many guests?")
            return
        if source == "voice":
            await _show_voice_confirmation(bot, chat_id, intent, text)
            return
        await process_guest_intent(bot, chat_id, guest_count, details.get("meal"))
        return

    if intent.intent == "greeting":
        await process_greeting(bot, chat_id)
        return

    if intent.intent == "thanks":
        await process_thanks(bot, chat_id)
        return

    if intent.intent == "help":
        await process_help_intent(bot, chat_id)
        return

    if intent.intent == "youtube":
        match = YOUTUBE_URL_RE.search(text)
        if match:
            await process_youtube_url(bot, chat_id, text)
        else:
            await _send(
                bot,
                chat_id,
                "I'm your grocery assistant — try sending a fridge photo or tell me what you want to cook!",
            )
        return

    if intent.intent == "plan_query":
        await process_plan_query(bot, chat_id, details.get("meal"))
        return

    if intent.intent == "inventory_query":
        await process_inventory_query(bot, chat_id, details.get("item"), details.get("zone"))
        return

    if intent.intent == "cart_query":
        await process_cart_query(bot, chat_id)
        return

    if intent.intent == "cancel":
        await process_cancel_intent(bot, chat_id)
        return

    if intent.intent == "correction":
        item_name = details.get("item")
        if not item_name:
            await _send(bot, chat_id, "🤔 Which item should I update?")
            return
        await process_correction_intent(bot, chat_id, item_name, details.get("correction_text") or text, details.get("zone"))
        return

    await _send(
        bot,
        chat_id,
        "I'm your grocery assistant — try sending a fridge photo or tell me what you want to cook!",
    )


# --------------------------------------------------------------------------
# Voice confirmation
# --------------------------------------------------------------------------

async def _show_voice_confirmation(bot: Bot, chat_id: int, intent: Intent, transcript: str) -> None:
    details = intent.details or {}
    if intent.intent == "recipe":
        summary = f"Make {details.get('recipe_name', '?')}"
        if details.get("servings"):
            summary += f" for {details['servings']} servings"
    else:
        summary = f"{details.get('guest_count', '?')} guests"
        if details.get("meal"):
            summary += f" for {details['meal']}"

    FLOW_STATE[chat_id] = PendingFlow(
        kind="voice_intent_confirm",
        chat_id=chat_id,
        created_at=datetime.now(),
        payload={"intent": intent.intent, "details": details},
    )
    await _send(
        bot,
        chat_id,
        f"🎙️ I heard: '{transcript}' → {summary}. Is that right?",
        reply_markup=keyboards.voice_confirm_keyboard(),
    )


async def voice_confirm_yes(bot: Bot, chat_id: int) -> None:
    flow = FLOW_STATE.pop(chat_id, None)
    if not flow or flow.kind != "voice_intent_confirm":
        await _send(bot, chat_id, "⚠️ This has expired — please try again.")
        return
    details = flow.payload["details"]
    if flow.payload["intent"] == "recipe":
        await process_recipe_intent(bot, chat_id, details.get("recipe_name"), details.get("servings"))
    else:
        await process_guest_intent(bot, chat_id, details.get("guest_count"), details.get("meal"))


async def voice_confirm_retype(bot: Bot, chat_id: int) -> None:
    FLOW_STATE.pop(chat_id, None)
    await _send(bot, chat_id, "👍 No problem — go ahead and type it instead.")


# --------------------------------------------------------------------------
# Phase 1 flows — unchanged
# --------------------------------------------------------------------------

async def process_restock(bot: Bot, chat_id: int) -> None:
    await _send(bot, chat_id, "📝 Checking tomorrow's meal plan...")
    gap = await full_restock_list()
    await _map_and_confirm(bot, chat_id, gap)


async def process_quick_add(bot: Bot, chat_id: int, text: str) -> None:
    await _send(bot, chat_id, "🔍 Reading your request...")

    _anthropic = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    system = (
        "Extract grocery items and quantities from the user's message. "
        'Return JSON only: {"items": [{"item": "oats", "qty": "1kg"}]}. '
        'If no quantity is mentioned, use "qty": "1".'
    )
    try:
        response = await _anthropic.messages.create(
            model=config.CLAUDE_TEXT_MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in response.content if hasattr(b, "text"))
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1])
        items = data.get("items", [])
    except Exception:
        logger.exception("Quick-add extraction failed")
        await _send(bot, chat_id, "⚠️ Having trouble understanding that. Please try again in a moment.")
        return

    if not items:
        await _send(bot, chat_id, "🤔 I couldn't find any grocery items in that message.")
        return

    gap = GapAnalysis(
        tomorrow="Quick add",
        meals=[],
        shopping_list=[
            ShoppingListItem(item=i["item"], qty=i.get("qty", "1"), reason="Quick add request") for i in items
        ],
    )
    await _map_and_confirm(bot, chat_id, gap)


# --------------------------------------------------------------------------
# Cart confirmation callbacks — unchanged
# --------------------------------------------------------------------------

async def cancel_order(bot: Bot, chat_id: int) -> None:
    CART_STATE.pop(chat_id, None)
    DUPLICATE_STATE.pop(chat_id, None)
    try:
        await mcp_client.clear_cart()
    except SwiggyMCPError:
        logger.warning("clear_cart failed during cancel for chat %s", chat_id)
    await _send(bot, chat_id, "🚫 Order cancelled.")


async def remove_item_from_cart(bot: Bot, chat_id: int, spin_id: str) -> None:
    pending = CART_STATE.get(chat_id)
    if not pending:
        await _send(bot, chat_id, "⚠️ No pending order to edit.")
        return

    remaining = [p for p in pending.mapped_products if p.spin_id != spin_id]

    if not remaining:
        CART_STATE.pop(chat_id, None)
        DUPLICATE_STATE.pop(chat_id, None)
        try:
            await mcp_client.clear_cart()
        except SwiggyMCPError:
            pass
        await _send(bot, chat_id, "🚫 Cart is empty now. Order cancelled.")
        return

    try:
        await cart_manager.build_cart(remaining, mcp_client, config.DELIVERY_ADDRESS_ID)
    except SwiggyMCPError:
        logger.exception("Failed to rebuild cart after removal")
        await _send(bot, chat_id, "⚠️ Can't reach Swiggy right now. Please try again in a moment.")
        return

    pending.mapped_products = remaining
    text = _format_cart_message(pending)
    await _send(bot, chat_id, text, reply_markup=keyboards.cart_summary_keyboard(remaining))


async def confirm_duplicate_additions(bot: Bot, chat_id: int) -> None:
    pending_dupes = DUPLICATE_STATE.pop(chat_id, None)
    if not pending_dupes:
        await _send(bot, chat_id, "⚠️ Nothing pending to confirm.")
        return

    existing = CART_STATE.get(chat_id)
    if not existing:
        await _send(bot, chat_id, "⚠️ Your cart session has changed — please re-add the items.")
        return

    by_spin = {p.spin_id: p for p in existing.mapped_products}
    for existing_item, addition in pending_dupes.additions:
        current = by_spin.get(existing_item.spin_id, existing_item)
        new_units = current.units + addition.units
        by_spin[current.spin_id] = MappedProduct(
            item_name=current.item_name,
            spin_id=current.spin_id,
            sku_id=current.sku_id,
            name=current.name,
            units=new_units,
            unit_price=current.unit_price,
            total_price=round(current.unit_price * new_units, 2),
        )
    existing.mapped_products = list(by_spin.values())

    try:
        await cart_manager.build_cart(existing.mapped_products, mcp_client, config.DELIVERY_ADDRESS_ID)
    except SwiggyMCPError:
        logger.exception("Failed to rebuild cart after duplicate merge")
        await _send(bot, chat_id, "⚠️ Can't reach Swiggy right now. Please try again in a moment.")
        return

    text = _format_cart_message(existing)
    await _send(bot, chat_id, text, reply_markup=keyboards.cart_summary_keyboard(existing.mapped_products))


async def skip_duplicate_additions(bot: Bot, chat_id: int) -> None:
    DUPLICATE_STATE.pop(chat_id, None)
    await _send(bot, chat_id, "👍 Skipped — kept your cart as-is.")
