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


async def remove_item(
    spin_id: str, mapped_products: list[MappedProduct], mcp_client: SwiggyMCPClient, address_id: str
) -> tuple[Cart, list[MappedProduct]]:
    """Removes one item from the tracked product list and rebuilds the cart."""
    remaining = [p for p in mapped_products if p.spin_id != spin_id]
    cart = await build_cart(remaining, mcp_client, address_id)
    return cart, remaining
