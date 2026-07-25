"""Swiggy Instamart MCP client.

Connects to Swiggy's Instamart MCP server over streamable HTTP, handles
OAuth 2.1 + PKCE token storage/refresh, and exposes each MCP tool as a
typed async Python function.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl, BaseModel

from app import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Models
#
# Field names/shapes below were reverse-engineered from the live Swiggy
# Instamart MCP server (mcp.swiggy.com/im), not just its tool descriptions —
# search_products/your_go_to_items group results by product with a nested
# "variations" list; each variation (a spinId+skuId pair) is what's actually
# orderable, so we flatten those into one Product per variant.
# --------------------------------------------------------------------------

class Product(BaseModel):
    spin_id: str
    sku_id: Optional[str] = None
    name: str
    brand: Optional[str] = None
    quantity_label: Optional[str] = None  # e.g. "200 g", "1 L"
    price: float
    in_stock: bool = True


class CartItem(BaseModel):
    spin_id: str
    sku_id: Optional[str] = None
    quantity: int = 1


class CartLine(BaseModel):
    spin_id: str
    sku_id: Optional[str] = None
    name: str
    quantity: int
    unit_price: float
    total_price: float


class Cart(BaseModel):
    items: list[CartLine] = []
    total: float = 0.0


class Order(BaseModel):
    order_id: str
    placed_at: Optional[str] = None
    item_count: int = 0
    total_amount: float = 0.0
    status: Optional[str] = None


class Address(BaseModel):
    address_id: str
    label: Optional[str] = None
    full_address: Optional[str] = None


class SwiggyMCPError(Exception):
    """Raised when the Swiggy MCP server returns an error or is unreachable."""


def _parse_money(value: Any) -> float:
    """Swiggy returns money as either a number or a string like "₹196"."""
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value or "0").replace("₹", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _flatten_products(data: Optional[dict]) -> list[Product]:
    """search_products / your_go_to_items group results by product, each
    with a nested "variations" list (pack sizes). Each variation is a
    separately orderable spinId+skuId, so we flatten to one Product per
    variant, carrying the parent's name/brand as a fallback."""
    out: list[Product] = []
    for group in (data or {}).get("products", []):
        for v in group.get("variations", []):
            price = v.get("price") or {}
            out.append(
                Product(
                    spin_id=v["spinId"],
                    sku_id=v.get("skuId"),
                    name=v.get("displayName") or group.get("displayName", ""),
                    brand=v.get("brandName") or group.get("brand"),
                    quantity_label=v.get("quantityDescription"),
                    price=_parse_money(price.get("offerPrice") or price.get("mrp") or 0),
                    in_stock=bool(v.get("isInStockAndAvailable", group.get("isAvail", True))),
                )
            )
    return out


def _parse_cart(data: Optional[dict]) -> Cart:
    lines = []
    for item in (data or {}).get("items", []):
        quantity = item.get("quantity", 1)
        unit_price = _parse_money(item.get("discountedFinalPrice") or item.get("mrp") or 0)
        lines.append(
            CartLine(
                spin_id=item["spinId"],
                sku_id=item.get("skuId"),
                name=item.get("itemName", ""),
                quantity=quantity,
                unit_price=unit_price,
                total_price=round(unit_price * quantity, 2),
            )
        )
    total = _parse_money((data or {}).get("cartTotalAmount", 0))
    return Cart(items=lines, total=total)


def _parse_order(data: dict) -> Order:
    items = data.get("items")
    return Order(
        order_id=str(data.get("orderId") or data.get("id") or ""),
        placed_at=data.get("orderTime") or data.get("createdAt") or data.get("placedAt"),
        item_count=len(items) if isinstance(items, list) else int(data.get("itemCount", 0)),
        total_amount=_parse_money(
            data.get("totalAmount") or data.get("orderTotal") or data.get("total") or 0
        ),
        status=data.get("orderStatus") or data.get("status"),
    )


# --------------------------------------------------------------------------
# Token storage
# --------------------------------------------------------------------------

class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + Dynamic Client Registration info to a local
    JSON file, implementing the `mcp` SDK's TokenStorage protocol.

    Seeds initial tokens from SWIGGY_ACCESS_TOKEN / SWIGGY_REFRESH_TOKEN env
    vars if no file exists yet, so a first deploy can be pre-seeded without
    running the interactive browser flow. That env seed is only ever offered
    once per process — if it turns out to be invalid (a 401 from Swiggy) and
    gets cleared, `get_tokens()` won't keep resurrecting the same bad token
    on every retry, which would otherwise block the real browser-login flow
    from ever running.
    """

    def __init__(self, path: str = config.TOKEN_STORE_PATH):
        self.path = path
        self._env_seed_used = False

    def _read(self) -> dict[str, Any]:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {}

    def _write(self, data: dict[str, Any]) -> None:
        with open(self.path, "w") as f:
            json.dump(data, f)

    async def get_tokens(self) -> Optional[OAuthToken]:
        data = self._read()
        if data.get("tokens"):
            return OAuthToken(**data["tokens"])
        if not self._env_seed_used and config.SWIGGY_ACCESS_TOKEN:
            self._env_seed_used = True
            return OAuthToken(
                access_token=config.SWIGGY_ACCESS_TOKEN,
                refresh_token=config.SWIGGY_REFRESH_TOKEN,
            )
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def clear(self) -> None:
        """Drops any persisted (and the one-time env-seeded) tokens so the
        next `get_tokens()` call returns None, forcing a real refresh or
        full OAuth re-auth instead of resurrecting a known-bad token."""
        data = self._read()
        data.pop("tokens", None)
        self._write(data)
        self._env_seed_used = True

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        data = self._read()
        if data.get("client_info"):
            return OAuthClientInformationFull(**data["client_info"])
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


async def _open_browser_for_auth(auth_url: str) -> None:
    logger.info("Opening browser for Swiggy OAuth login: %s", auth_url)
    webbrowser.open(auth_url)


async def _local_callback_server() -> tuple[str, Optional[str]]:
    """Runs a one-shot local HTTP server on the OAuth redirect port and
    returns the (code, state) query params from the first GET /callback.

    Swiggy whitelists http://localhost and http://localhost/callback as
    redirect URIs for local/dev OAuth clients.
    """
    result: asyncio.Future[tuple[str, Optional[str]]] = asyncio.get_event_loop().create_future()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        try:
            method, path, _ = request_line.decode().split(" ")
        except ValueError:
            writer.close()
            return
        # Drain remaining headers.
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break

        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        body = b"<html><body>GroceryBot: Swiggy login complete, you can close this tab.</body></html>"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        writer.write(response)
        await writer.drain()
        writer.close()

        if code and not result.done():
            result.set_result((code, state))

    server = await asyncio.start_server(
        handle_client, host="localhost", port=config.SWIGGY_OAUTH_REDIRECT_PORT
    )
    async with server:
        return await asyncio.wait_for(result, timeout=300)


def _is_unauthorized(error: BaseException) -> bool:
    """Walks an exception (and any anyio ExceptionGroup / __cause__ chain)
    looking for an underlying httpx 401, since streamablehttp_client's
    internal task group wraps the real error before it reaches us."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401:
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_unauthorized(e) for e in error.exceptions)
    if error.__cause__ is not None:
        return _is_unauthorized(error.__cause__)
    return False


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class SwiggyMCPClient:
    """Thin async wrapper around the Swiggy Instamart MCP tools.

    The underlying `mcp` SDK handles OAuth 2.1 + PKCE and Dynamic Client
    Registration transparently: on first use (no stored tokens), it opens
    a local browser for Swiggy phone + OTP login against the whitelisted
    `http://localhost/callback` redirect URI. After that, tokens are cached
    in `token_store.json` and refreshed automatically (~5 day lifetime).
    """

    def __init__(self):
        self.token_storage = FileTokenStorage()
        self._auth = OAuthClientProvider(
            server_url=config.SWIGGY_MCP_ENDPOINT,
            client_metadata=OAuthClientMetadata(
                redirect_uris=[AnyUrl(config.SWIGGY_OAUTH_REDIRECT_URI)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                client_name="GroceryBot",
            ),
            storage=self.token_storage,
            redirect_handler=_open_browser_for_auth,
            callback_handler=_local_callback_server,
        )
        self._go_to_items_cache: Optional[list[Product]] = None

    @asynccontextmanager
    async def _session(self):
        try:
            async with streamablehttp_client(
                config.SWIGGY_MCP_ENDPOINT, auth=self._auth
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
        except Exception as exc:
            logger.exception("Swiggy MCP connection failed")
            raise SwiggyMCPError(f"Could not reach Swiggy MCP: {exc}") from exc

    async def _call_tool_once(self, name: str, arguments: dict[str, Any]):
        async with self._session() as session:
            try:
                return await session.call_tool(name, arguments)
            except Exception as exc:
                logger.exception("MCP tool call failed: %s", name)
                raise SwiggyMCPError(f"Swiggy tool '{name}' failed: {exc}") from exc

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await self._call_tool_once(name, arguments)
        except SwiggyMCPError as exc:
            if not _is_unauthorized(exc):
                raise
            logger.warning(
                "Swiggy rejected the stored/seeded token (401) — clearing it and "
                "re-authenticating (this will open a browser for login)"
            )
            await self.token_storage.clear()
            self._auth._current_tokens = None
            result = await self._call_tool_once(name, arguments)

        if result.isError:
            error_text = "".join(
                part.text for part in result.content if hasattr(part, "text")
            )
            raise SwiggyMCPError(f"Swiggy MCP error from '{name}': {error_text}")

        # Some tools (e.g. get_addresses) return structured data via the
        # structuredContent field with prose in `content` for display.
        # Others (search_products, get_cart, update_cart, ...) put a JSON
        # blob straight into the text content, wrapped as {"success", "data"}.
        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured

        text_parts = [part.text for part in result.content if hasattr(part, "text")]
        payload = "".join(text_parts)
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload

        if isinstance(parsed, dict) and "data" in parsed and "success" in parsed:
            return parsed["data"]
        return parsed

    # ---- Tool wrappers ----------------------------------------------

    async def search_products(self, query: str, address_id: str) -> list[Product]:
        data = await self._call_tool("search_products", {"addressId": address_id, "query": query})
        return _flatten_products(data)

    async def get_go_to_items(self, address_id: str, force_refresh: bool = False) -> list[Product]:
        if self._go_to_items_cache is not None and not force_refresh:
            return self._go_to_items_cache
        data = await self._call_tool("your_go_to_items", {"addressId": address_id})
        items = _flatten_products(data)
        self._go_to_items_cache = items
        return items

    async def update_cart(self, items: list[CartItem], address_id: str) -> Cart:
        data = await self._call_tool(
            "update_cart",
            {
                "selectedAddressId": address_id,
                "items": [
                    {"spinId": i.spin_id, "skuId": i.sku_id, "quantity": i.quantity}
                    for i in items
                ],
            },
        )
        return _parse_cart(data)

    async def get_cart(self) -> Cart:
        data = await self._call_tool("get_cart", {})
        return _parse_cart(data)

    async def clear_cart(self) -> bool:
        await self._call_tool("clear_cart", {})
        return True

    async def checkout(self, address_id: str, payment_method: str = "Cash") -> Order:
        data = await self._call_tool(
            "checkout", {"addressId": address_id, "paymentMethod": payment_method}
        )
        return _parse_order(data or {})

    async def get_orders(self, count: int = 10) -> list[Order]:
        data = await self._call_tool("get_orders", {"count": count, "orderType": "INSTAMART"})
        return [_parse_order(o) for o in (data or {}).get("orders", [])]

    async def get_addresses(self, page: int = 1, page_size: int = 10) -> list[Address]:
        data = await self._call_tool("get_addresses", {"page": page, "pageSize": page_size})
        return [
            Address(
                address_id=a["id"],
                label=a.get("addressTag") or a.get("addressCategory"),
                full_address=a.get("addressLine"),
            )
            for a in (data or {}).get("addresses", [])
        ]


# Module-level singleton — the bot is single-user, so one shared client
# with a cached go-to-items list is sufficient.
client = SwiggyMCPClient()
