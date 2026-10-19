import asyncio
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import Update

import bot_app

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
logging.basicConfig(level=logging.INFO)

_loop = asyncio.new_event_loop()   # персистентный цикл «тёплого» контейнера
_bot: Bot | None = None
_dp: Dispatcher | None = None


async def _ensure_ready():
    global _bot, _dp
    if _bot is None:
        await bot_app.db_init()                       # aiohttp-сессия Supabase
        _bot = Bot(bot_app.BOT_TOKEN,
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        _dp = Dispatcher()
        _dp.include_router(bot_app.router)
    return _bot, _dp


async def _process(body: bytes):
    bot, dp = await _ensure_ready()
    update = Update(**json.loads(body))
    result = await dp.feed_update(bot, update)        # вызовет хендлеры бота
    if isinstance(result, TelegramMethod):
        # Ответ, который нельзя отдать синхронно (например, answerCallbackQuery)
        await bot(result)


class handler(BaseHTTPRequestHandler):
    # Имя `handler` обязательно для рантайма @vercel/python
    def do_POST(self):
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        try:
            _loop.run_until_complete(_process(body))
            self.send_response(200)
        except Exception:
            logging.exception("webhook processing error")
            self.send_response(500)
        self.end_headers()

    def do_GET(self):
        # healthcheck-эндпоинт для мониторинга статуса функции
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"card-battle-bot webhook is up")

    def log_message(self, fmt, *args):
        # компактнее в логах Vercel
        logging.info(fmt % args)
