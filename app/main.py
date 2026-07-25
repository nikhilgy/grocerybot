"""FastAPI app: Telegram webhook receiver + health check."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app import config
from app.bot import handlers
from app.db.database import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

telegram_app: Application = Application.builder().token(config.BOT_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", handlers.cmd_start))
telegram_app.add_handler(CommandHandler("help", handlers.cmd_help))
telegram_app.add_handler(CommandHandler("restock", handlers.cmd_restock))
telegram_app.add_handler(CommandHandler("history", handlers.cmd_history))
telegram_app.add_handler(CommandHandler("spend", handlers.cmd_spend))
telegram_app.add_handler(CommandHandler("zones", handlers.cmd_zones))
telegram_app.add_handler(CommandHandler("inventory", handlers.cmd_inventory))
telegram_app.add_handler(CommandHandler("addzone", handlers.cmd_addzone))
telegram_app.add_handler(CommandHandler("removezone", handlers.cmd_removezone))
telegram_app.add_handler(CommandHandler("recipe", handlers.cmd_recipe))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
telegram_app.add_handler(MessageHandler(filters.VOICE, handlers.handle_voice))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text))
telegram_app.add_handler(CallbackQueryHandler(handlers.handle_callback))


async def _register_webhook() -> None:
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/setWebhook",
            json={"url": config.WEBHOOK_URL},
        )
        resp.raise_for_status()
        logger.info("Webhook registered: %s", resp.json())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    await _register_webhook()
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    asyncio.create_task(telegram_app.process_update(update))
    return {"ok": True}
