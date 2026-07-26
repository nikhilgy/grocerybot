"""Inline keyboard builders for the cart confirmation flow and Phase 2 conversation flows."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.instamart.mcp_client import Address
from app.instamart.product_mapper import MappedProduct

REMOVE_PREFIX = "cart_remove:"
CANCEL_ACTION = "cart_cancel"
CART_EDIT_ACTION = "cart_edit"
CART_DONE_ACTION = "cart_done"
DUPLICATE_ADD_ACTION = "dup_add"
DUPLICATE_SKIP_ACTION = "dup_skip"
INSTAMART_URL = "https://www.swiggy.com/instamart"

# --- Phase 2 conversation-flow callback data -------------------------------

ZONE_PICK_PREFIX = "zone_pick:"
ADDRESS_PICK_PREFIX = "addr_pick:"
SERVINGS_PREFIX = "servings:"
GUEST_MEAL_PREFIX = "guestmeal:"

RUNLOW_ADD_ALL = "runlow_add_all"
RUNLOW_PICK = "runlow_pick"
RUNLOW_SKIP = "runlow_skip"
RUNLOW_CONFIRM = "runlow_confirm"
RUNLOW_TOGGLE_PREFIX = "runlow_toggle:"

INV_CONFIRM_OK = "invconf_ok"
INV_CONFIRM_RESCAN = "invconf_rescan"
INV_CONFIRM_CANCEL = "invconf_cancel"

VOICE_CONFIRM_YES = "voiceconf_yes"
VOICE_CONFIRM_RETYPE = "voiceconf_retype"

PHOTO_ANALYZE = "photo_analyze"
PHOTO_CLEAR = "photo_clear"


def cart_summary_keyboard(mapped_products: list[MappedProduct]) -> InlineKeyboardMarkup:
    """Clean main cart view: open-app, plus an Edit-items / Cancel row.

    Removing items lives behind "Edit items" (a compact numbered grid) so the
    main view isn't dominated by one remove button per item.

    Checkout happens in the Swiggy app itself (so the user can pay with a
    saved card) rather than via the bot — the cart built through MCP syncs
    to the app's account cart, so opening the app shows the same items.
    """
    rows = [[InlineKeyboardButton("🛍️ Open cart in Swiggy app", url=INSTAMART_URL)]]
    edit_cancel_row = []
    if mapped_products:
        edit_cancel_row.append(InlineKeyboardButton("✏️ Edit items", callback_data=CART_EDIT_ACTION))
    edit_cancel_row.append(InlineKeyboardButton("🚫 Cancel", callback_data=CANCEL_ACTION))
    rows.append(edit_cancel_row)
    return InlineKeyboardMarkup(rows)


def cart_edit_keyboard(mapped_products: list[MappedProduct]) -> InlineKeyboardMarkup:
    """Compact grid of numbered remove buttons (4 per row) matching the numbered
    item list in the cart message, plus a Done button back to the main view."""
    buttons = [
        InlineKeyboardButton(f"❌ {i}", callback_data=f"{REMOVE_PREFIX}{p.spin_id}")
        for i, p in enumerate(mapped_products, start=1)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("⬅️ Done", callback_data=CART_DONE_ACTION)])
    return InlineKeyboardMarkup(rows)


def duplicate_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Add extra", callback_data=DUPLICATE_ADD_ACTION),
                InlineKeyboardButton("🚫 Skip", callback_data=DUPLICATE_SKIP_ACTION),
            ]
        ]
    )


def parse_remove_callback(data: str) -> str | None:
    """Extracts the spin_id from a remove-button callback, or None if it's not one."""
    if data.startswith(REMOVE_PREFIX):
        return data[len(REMOVE_PREFIX) :]
    return None


# --- Phase 2 keyboard builders ---------------------------------------------


def zone_picker_keyboard(zones: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """One button per known zone: `zones` is a list of (zone_id, display_name)."""
    rows = [
        [InlineKeyboardButton(display_name, callback_data=f"{ZONE_PICK_PREFIX}{zone_id}")]
        for zone_id, display_name in zones
    ]
    return InlineKeyboardMarkup(rows)


def address_picker_keyboard(addresses: list[Address]) -> InlineKeyboardMarkup:
    """One button per saved Swiggy address, keyed by address_id."""
    rows = []
    for a in addresses:
        label = a.label or a.full_address or a.address_id
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"{ADDRESS_PICK_PREFIX}{a.address_id}")]
        )
    return InlineKeyboardMarkup(rows)


def servings_keyboard(options: list[int] = [1, 2, 3, 4, 6]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(str(n), callback_data=f"{SERVINGS_PREFIX}{n}") for n in options]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Other", callback_data=f"{SERVINGS_PREFIX}other")]])


def guest_meal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Lunch", callback_data=f"{GUEST_MEAL_PREFIX}lunch"),
                InlineKeyboardButton("Dinner", callback_data=f"{GUEST_MEAL_PREFIX}dinner"),
                InlineKeyboardButton("Both", callback_data=f"{GUEST_MEAL_PREFIX}both"),
            ]
        ]
    )


def photo_tray_keyboard(count: int) -> InlineKeyboardMarkup:
    """Scan-tray controls: analyze the buffered photos as one batch, or clear them."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ Analyze ({count})", callback_data=PHOTO_ANALYZE)],
            [InlineKeyboardButton("🗑️ Clear", callback_data=PHOTO_CLEAR)],
        ]
    )


def running_low_alert_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Add all to cart", callback_data=RUNLOW_ADD_ALL)],
            [InlineKeyboardButton("Pick items", callback_data=RUNLOW_PICK)],
            [InlineKeyboardButton("Skip", callback_data=RUNLOW_SKIP)],
        ]
    )


def running_low_pick_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    """`items` is a list of {"name", "qty", "stock_level", "selected"} dicts."""
    rows = [
        [
            InlineKeyboardButton(
                f"{'☑️' if item['selected'] else '⬜'} {item['name']} ({item['qty']})",
                callback_data=f"{RUNLOW_TOGGLE_PREFIX}{i}",
            )
        ]
        for i, item in enumerate(items)
    ]
    rows.append([InlineKeyboardButton("✅ Add selected", callback_data=RUNLOW_CONFIRM)])
    return InlineKeyboardMarkup(rows)


def inventory_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Looks right, order", callback_data=INV_CONFIRM_OK)],
            [InlineKeyboardButton("📸 Let me rescan first", callback_data=INV_CONFIRM_RESCAN)],
            [InlineKeyboardButton("🚫 Cancel", callback_data=INV_CONFIRM_CANCEL)],
        ]
    )


def voice_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, that's right", callback_data=VOICE_CONFIRM_YES),
                InlineKeyboardButton("✏️ Let me type it", callback_data=VOICE_CONFIRM_RETYPE),
            ]
        ]
    )


def _parse_prefixed(data: str, prefix: str) -> str | None:
    if data.startswith(prefix):
        return data[len(prefix) :]
    return None


def parse_zone_pick_callback(data: str) -> str | None:
    return _parse_prefixed(data, ZONE_PICK_PREFIX)


def parse_address_pick_callback(data: str) -> str | None:
    return _parse_prefixed(data, ADDRESS_PICK_PREFIX)


def parse_servings_callback(data: str) -> str | None:
    return _parse_prefixed(data, SERVINGS_PREFIX)


def parse_guest_meal_callback(data: str) -> str | None:
    return _parse_prefixed(data, GUEST_MEAL_PREFIX)


def parse_runlow_toggle_callback(data: str) -> int | None:
    raw = _parse_prefixed(data, RUNLOW_TOGGLE_PREFIX)
    return int(raw) if raw is not None else None
