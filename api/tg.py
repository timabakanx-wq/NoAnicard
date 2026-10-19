import asyncio
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

# ИСПРАВЛЕНО: file -> __file__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

import bot_app

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
logging.basicConfig(level=logging.INFO)

# Безопасный Event Loop для Serverless
try:
    _loop = asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

_bot: Bot | None = None
_dp: Dispatcher | None = None

async def _ensure_ready():
    global _bot, _dp
    if _bot is None:
        if not bot_app.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is not set in Vercel Environment Variables!")
        await bot_app.db_init()
        _bot = Bot(token=bot_app.BOT_TOKEN,
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        _dp = Dispatcher()
        _dp.include_router(bot_app.router)
    return _bot, _dp

async def _process(body: bytes):
    bot, dp = await _ensure_ready()
    update = Update(**json.loads(body))
    # feed_update сам выполнит все хендлеры и дождется await'ов внутри них
    await dp.feed_update(bot, update)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden")
            return
            
        try:
            _loop.run_until_complete(_process(body))
            # ВАЖНО: Корректное закрытие HTTP-ответа
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as e:
            logging.exception("webhook processing error")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"error")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"card-battle-bot webhook is up")

    def log_message(self, fmt, *args):
        logging.info(fmt % args)
