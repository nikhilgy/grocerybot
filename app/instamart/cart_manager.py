"""Builds and manages the Instamart cart for the confirmation flow."""
from __future__ import annotations

import logging

from app.instamart.mcp_client import Cart, CartItem, SwiggyMCPClient
from app.instamart.product_mapper import MappedProduct

logger = logging.getLogger(__name__)


async def build_cart(
    mapped_products: list[MappedProduct], mcp_client: SwiggyMCPClient, address_id: str
) -> Cart:
    """Clears any stale cart, then adds all mapped products fresh."""
    await mcp_client.clear_cart()
    items = [
        CartItem(spin_id=p.spin_id, sku_id=p.sku_id, quantity=p.units)
        for p in mapped_products
    ]
    return await mcp_client.update_cart(items, address_id)


def reconcile(
    requested: list[MappedProduct], cart: Cart
) -> tuple[list[MappedProduct], list[MappedProduct]]:
    """Split requested products into (accepted, dropped) by matching spin_id
    against what Swiggy actually committed to the cart.

    Swiggy can silently drop an item at commit time (out of stock at the
    address, not serviceable, quantity cap) even though it mapped fine at
    search time — so what we asked for and what's really in the cart can
    diverge. Accepted items get their units/total_price corrected from the
    real cart line (Swiggy may have capped the quantity); dropped items are
    the ones Swiggy left out entirely."""
    lines_by_spin = {line.spin_id: line for line in cart.items}
    accepted: list[MappedProduct] = []
    dropped: list[MappedProduct] = []
    for p in requested:
        line = lines_by_spin.get(p.spin_id)
        if line is None:
            dropped.append(p)
            continue
        accepted.append(
            p.model_copy(
                update={
                    "units": line.quantity or p.units,
                    "total_price": line.total_price or p.total_price,
                }
            )
        )
    return accepted, dropped


async def remove_item(
    spin_id: str, mapped_products: list[MappedProduct], mcp_client: SwiggyMCPClient, address_id: str
) -> tuple[Cart, list[MappedProduct]]:
    """Removes one item from the tracked product list and rebuilds the cart."""
    remaining = [p for p in mapped_products if p.spin_id != spin_id]
    cart = await build_cart(remaining, mcp_client, address_id)
    return cart, remaining
