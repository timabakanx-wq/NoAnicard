from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)          # чтобы работал `import bot_app`

try:
    # Статический «якорь»: бандлер Vercel видит импорт пакета _vendor
    # и гарантированно кладёт папку api/_vendor в состав функции.
    import _vendor  # noqa: F401
except Exception:
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg")
log.setLevel(logging.INFO)   # рантайм может поставить корневой логгер в WARNING


def say(msg: str) -> None:
    """Печать в stdout — в логах Vercel видно ВСЕГДА (logging может глушиться)."""
    print(msg, flush=True)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "")   # только для локальных тестов
IMPORT_ERROR = ""

# Журнал последних событий контейнера (виден на /diag?key=...).
# Память «тёплого» контейнера живёт между запросами — этого хватает,
# чтобы понять, что происходило за последние минуты.
JOURNAL: deque[dict] = deque(maxlen=60)
STARTED_AT = time.time()
REQUESTS_TOTAL = 0
ERRORS_TOTAL = 0

# Синтетический пользователь для сквозной проверки (probe) на /diag.
PROBE_UID = 987654321


def _import_bot_stack():
    """Импортирует aiogram и сгенерированное приложение бота."""
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.enums import ParseMode
    from aiogram.methods import TelegramMethod
    from aiogram.types import Update
    import bot_app
    return (Bot, Dispatcher, DefaultBotProperties, AiohttpSession, TelegramAPIServer,
            ParseMode, TelegramMethod, Update, UNHANDLED, bot_app)


try:
    (Bot, Dispatcher, DefaultBotProperties, AiohttpSession, TelegramAPIServer,
     ParseMode, TelegramMethod, Update, UNHANDLED, bot_app) = _import_bot_stack()
except ImportError:
    # План Б: зависимости не установлены билдером — берём вендор из
    # api/_vendor (cp312/manylinux — совпадает с рантаймом Vercel).
    _vendor_dir = os.path.join(_HERE, "_vendor")
    if os.path.isdir(_vendor_dir) and _vendor_dir not in sys.path:
        sys.path.append(_vendor_dir)
    try:
        (Bot, Dispatcher, DefaultBotProperties, AiohttpSession, TelegramAPIServer,
         ParseMode, TelegramMethod, Update, UNHANDLED, bot_app) = _import_bot_stack()
        say("[tg] зависимости загружены из api/_vendor")
    except Exception:
        IMPORT_ERROR = traceback.format_exc()
        say("[tg] НЕ УДАЛОСЬ ИМПОРТИРОВАТЬ ЗАВИСИМОСТИ:\n" + IMPORT_ERROR)
except Exception:
    IMPORT_ERROR = traceback.format_exc()
    UNHANDLED = None                                     # noqa: F811
    say("[tg] НЕ УДАЛОСЬ ИМПОРТИРОВАТЬ ЗАВИСИМОСТИ:\n" + IMPORT_ERROR)


# =====================================================================
# Журнал и конфиг
# =====================================================================

def _journal(event_kind: str, **fields):
    """Пишет событие в журнал контейнера и в логи Vercel (одной строкой).

    Служебные ключи (`t`, `kind`) не могут быть перезаписаны данными
    апдейта — иначе журнал врал бы, а вызов падал с TypeError.
    """
    global ERRORS_TOTAL
    entry = {"t": time.strftime("%H:%M:%S"), "kind": event_kind}
    for k, v in fields.items():
        entry[("_" + k) if k in ("t", "kind") else k] = v
    JOURNAL.append(entry)
    if event_kind == "error":
        ERRORS_TOTAL += 1
    say("[tg] " + json.dumps(entry, ensure_ascii=False, default=str)[:1500])
    return entry


# Переменные, без которых бот в принципе не может работать
CRITICAL_ENV = ("BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY")
# У этих в коде есть разумные значения по умолчанию
OPTIONAL_ENV = ("WEBHOOK_SECRET", "ACCESS_PASSWORD", "ADMIN_PASSWORD")


def _masked(name: str, value: str) -> str:
    """Показываем факт наличия переменной, не раскрывая секрет целиком."""
    if not value:
        if name in CRITICAL_ENV:
            return "❌ НЕ ЗАДАНА"
        if name == "WEBHOOK_SECRET":
            return "⚠️ не задан (webhook будет без проверки секрета)"
        return "не задана → берётся значение по умолчанию из кода"
    if name == "BOT_TOKEN":
        head = value.split(":")[0]
        return f"ok (bot id {head}, длина {len(value)})"
    if name in ("SUPABASE_URL",):
        return f"ok ({value})"
    return f"ok (длина {len(value)})"


def _aiogram_version() -> str:
    try:
        import aiogram
        return getattr(aiogram, "__version__", "?")
    except Exception:                                     # noqa: BLE001
        return "?"


def _env_status() -> dict:
    return {n: _masked(n, os.environ.get(n, ""))
            for n in CRITICAL_ENV + OPTIONAL_ENV}


def _config_problems() -> list[str]:
    """Что явно не настроено (главная причина «бот молчит»)."""
    problems = []
    if not os.environ.get("BOT_TOKEN"):
        problems.append("BOT_TOKEN не задан в Vercel → Environment Variables")
    if not os.environ.get("SUPABASE_URL"):
        problems.append("SUPABASE_URL не задан → бот не может читать/писать БД")
    if not os.environ.get("SUPABASE_KEY"):
        problems.append("SUPABASE_KEY не задан → Supabase вернёт 401")
    return problems


# =====================================================================
# aiogram: Dispatcher — один на контейнер, Bot/сессии — на каждый запрос
# =====================================================================

_dp = None
_LOOP_LOCK = threading.Lock()


def _get_dispatcher():
    """Создаёт (один раз) Dispatcher с роутером бота."""
    global _dp
    if _dp is None:
        _dp = Dispatcher()
        _dp.include_router(bot_app.router)
    return _dp


def _make_bot():
    """Новый Bot со СВЕЖЕЙ aiohttp-сессией (создаётся на каждый запрос)."""
    token = os.environ.get("BOT_TOKEN", "") or getattr(bot_app, "BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN пуст: задайте переменную окружения в Vercel")
    if TELEGRAM_API_BASE:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_BASE))
    else:
        session = AiohttpSession()
    return Bot(token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def _chat_id_of(update) -> int | None:
    """chat_id из любого типа апдейта — нужен, чтобы сообщить об ошибке."""
    for attr in ("message", "edited_message", "callback_query",
                 "inline_query", "channel_post"):
        obj = getattr(update, attr, None)
        if obj is None:
            continue
        chat = getattr(obj, "chat", None)
        if chat is not None and getattr(chat, "id", None):
            return chat.id
        message = getattr(obj, "message", None)
        if message is not None and getattr(message, "chat", None):
            return message.chat.id
    return None


def _update_kind(update) -> str:
    for attr in ("message", "edited_message", "callback_query", "inline_query",
                 "channel_post", "chosen_inline_result", "poll"):
        if getattr(update, attr, None) is not None:
            return attr
    return "unknown"


async def _process_update(body: bytes) -> dict:
    """Прогоняет один апдейт через бота. Всё — в рамках одного запроса."""
    dp = _get_dispatcher()
    t0 = time.time()
    await bot_app.db_init()                 # свежая сессия Supabase (REST)
    bot = _make_bot()                       # свежая сессия Telegram API
    info: dict = {"handled": None, "chat_id": None, "reply_method": None}
    try:
        update = Update.model_validate(json.loads(body), context={"bot": bot})
        info["update_id"] = update.update_id
        info["update_type"] = _update_kind(update)
        info["chat_id"] = _chat_id_of(update)
        t1 = time.time()
        result = await dp.feed_update(bot, update)      # вызовет хендлеры бота
        # Замер фаз — видно в журнале /diag: init_ms (сессии+разбор апдейта)
        # и feed_ms (сам хендлер: все походы в БД и к Telegram API).
        info["init_ms"] = round((t1 - t0) * 1000)
        info["feed_ms"] = round((time.time() - t1) * 1000)
        if isinstance(result, TelegramMethod):
            # Ответ, который нельзя отдать синхронно (например, answerCallbackQuery)
            await bot(result)
            info["reply_method"] = type(result).__name__
        info["handled"] = result is not UNHANDLED
        return info
    finally:
        # Закрываем ОБЕ сессии до возврата ответа: контейнер сейчас заморозят.
        for closer in (bot.session.close(), bot_app.db_close()):
            try:
                await closer
            except Exception as e:                        # noqa: BLE001
                say(f"[tg] не удалось закрыть сессию: {type(e).__name__}: {e}")


async def _notify_user_error(bot, chat_id: int | None, text: str) -> str:
    """Пытается сказать пользователю в Telegram, что произошла ошибка."""
    if not chat_id:
        return "нет chat_id — сообщить пользователю невозможно"
    try:
        await bot.send_message(chat_id, text)
        return "ok"
    except Exception as e:                                # noqa: BLE001
        return f"не удалось отправить: {type(e).__name__}: {e}"


def _run(coro_func, *args, **kwargs):
    """Выполняет корутину в свежем event loop текущего потока.

    Свежий цикл на каждый запрос — страховка от «замороженного» контейнера
    (старый цикл может проснуться с невалидным состоянием), а лок — от
    одновременного запуска цикла из двух потоков рантайма.
    """
    with _LOOP_LOCK:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro_func(*args, **kwargs))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:                             # noqa: BLE001
                pass
            asyncio.set_event_loop(None)
            loop.close()


# =====================================================================
# Диагностика (/diag?key=...)
# =====================================================================

def _import_diagnostics() -> dict:
    def _ls(path: str):
        try:
            return sorted(os.listdir(path))[:40]
        except OSError as e:
            return f"<нет доступа: {e}>"

    root = os.path.dirname(_HERE)
    return {
        "status": "dependencies not installed — функция не может импортировать aiogram",
        "hint": ("Проверьте: 1) Root Directory проекта Vercel = vercel; "
                 "2) requirements.txt лежит в корне деплоя; "
                 "3) папка api/_vendor задеплоена вместе с кодом "
                 "(пересоздать: python vercel/vendor_deps.py)"),
        "traceback": IMPORT_ERROR.strip().splitlines()[-8:],
        "python": sys.version,
        "cwd": os.getcwd(),
        "deploy_root": _ls(root),
        "api_dir": _ls(_HERE),
        "vendor_present": os.path.isdir(os.path.join(_HERE, "_vendor")),
        "requirements_root": os.path.isfile(os.path.join(root, "requirements.txt")),
        "sys_path_head": sys.path[:8],
    }


async def _probe(bot_app_module) -> dict:
    """Сквозная проверка: synthetic /start → БД → Telegram (и уборка за собой)."""
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": int(time.time()),
            "chat": {"id": PROBE_UID, "type": "private", "first_name": "diag"},
            "from": {"id": PROBE_UID, "is_bot": False,
                     "first_name": "diag", "username": "diag_probe"},
            "text": "/start",
        },
    }
    out: dict = {}
    try:
        out["result"] = await _process_update(json.dumps(payload).encode())
        out["verdict"] = "✅ /start прошёл весь тракт (БД + Telegram)"
    except Exception as e:                                # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        out["error"] = msg[:600]
        low = msg.lower()
        if "chat not found" in low:
            out["verdict"] = ("✅ БД работает, Telegram API отвечает "
                              "(ошибка «chat not found» у синтетического "
                              "пользователя — это норма: токен рабочий)")
        elif "unauthorized" in low:
            out["verdict"] = "❌ BOT_TOKEN неверный (Telegram: Unauthorized)"
        elif ("cannot connect" in low or "connectorerror" in low
              or "timeout" in low or "supabase" in low
              or "http 4" in low or "http 5" in low):
            out["verdict"] = ("❌ Supabase недоступен: проверьте SUPABASE_URL/"
                              "SUPABASE_KEY, белый список IP и что применён "
                              "supabase_schema.sql")
        elif "relation" in low and "does not exist" in low:
            out["verdict"] = ("❌ в Supabase нет таблиц: выполните "
                              "supabase_schema.sql в SQL Editor")
        else:
            out["verdict"] = "❌ ошибка внутри бота — смотрите поле error"
    finally:
        # убираем синтетического пользователя из БД (и закрываем сессию!)
        try:
            await bot_app_module.db_init()
            try:
                await bot_app_module._rest(
                    "DELETE", "users", params={"user_id": f"eq.{PROBE_UID}"})
            finally:
                await bot_app_module.db_close()
        except Exception:                                 # noqa: BLE001
            pass
    return out


async def _collect_diagnostics(probe: bool = False) -> dict:
    """Полный отчёт: Telegram (getMe/getWebhookInfo) + Supabase + журнал."""
    diag: dict = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "uptime_sec": round(time.time() - STARTED_AT, 1),
        "requests_total": REQUESTS_TOTAL,
        "errors_total": ERRORS_TOTAL,
        "env": _env_status(),
        "config_problems": _config_problems() or ["нет — все переменные заданы"],
    }
    diag["aiogram"] = _aiogram_version()

    # --- Telegram: кто мы и что зарегистрировано ---
    try:
        bot = _make_bot()
        try:
            me = await bot.get_me()
            diag["telegram_me"] = {"id": me.id, "username": me.username,
                                   "first_name": me.first_name}
            wh = await bot.get_webhook_info()
            diag["telegram_webhook"] = {
                "url": wh.url or "❌ ПУСТО — webhook НЕ зарегистрирован",
                "has_custom_certificate": wh.has_custom_certificate,
                "pending_update_count": wh.pending_update_count,
                "ip_address": wh.ip_address,
                "last_error_date": wh.last_error_date,
                "last_error_message": wh.last_error_message,
                "last_synchronization_error_date": wh.last_synchronization_error_date,
                "max_connections": wh.max_connections,
                "allowed_updates": wh.allowed_updates,
            }
        finally:
            await bot.session.close()
    except Exception as e:                                # noqa: BLE001
        diag["telegram_error"] = f"{type(e).__name__}: {e}"

    # --- Supabase: доступность каждой таблицы ---
    tables = {}
    try:
        await bot_app.db_init()
        try:
            for table in ("users", "cards", "battles", "sim_logs"):
                try:
                    rows = await bot_app._rest("GET", table,
                                               params={"select": "*", "limit": "1"})
                    tables[table] = f"ok (прочитано строк: {len(rows)})"
                except Exception as e:                    # noqa: BLE001
                    tables[table] = f"❌ {type(e).__name__}: {str(e)[:200]}"
        finally:
            await bot_app.db_close()
    except Exception as e:                                # noqa: BLE001
        tables["session"] = f"❌ {type(e).__name__}: {e}"
    diag["supabase"] = tables

    if probe:
        diag["probe"] = await _probe(bot_app)

    diag["journal"] = list(JOURNAL)[-25:]
    return diag


# =====================================================================
# HTTP-обработчик Vercel
# =====================================================================

class handler(BaseHTTPRequestHandler):
    # Имя `handler` обязательно для рантайма @vercel/python
    server_version = "CardBattleBot/1.0"

    # ---------- служебное ----------

    def _respond(self, code: int, body: bytes,
                 ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, code: int, obj) -> None:
        self._respond(code, json.dumps(obj, ensure_ascii=False, indent=2,
                                       default=str).encode(),
                      "application/json; charset=utf-8")

    def _parsed(self):
        return urlparse(self.path)

    def _query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(self._parsed().query).items()}

    def _key_ok(self, q: dict) -> bool:
        """Доступ к /diag: по WEBHOOK_SECRET или BOT_TOKEN (оба — секреты)."""
        key = q.get("key") or q.get("diag") or ""
        secrets = {s for s in (WEBHOOK_SECRET,
                               os.environ.get("BOT_TOKEN", "")) if s}
        return bool(key) and key in secrets

    # ---------- GET ----------

    def do_GET(self):
        global REQUESTS_TOTAL
        REQUESTS_TOTAL += 1
        path = self._parsed().path.rstrip("/") or "/"
        q = self._query()

        if IMPORT_ERROR:
            _journal("error", where="import", detail=IMPORT_ERROR.splitlines()[-1])
            return self._json(500, _import_diagnostics())

        # /diag?key=<WEBHOOK_SECRET> (или /?diag=<...>) — полный отчёт
        if path == "/diag" or "diag" in q or "key" in q:
            if not self._key_ok(q):
                _journal("warn", where="diag", detail="нет/неверный key")
                return self._json(403, {
                    "error": "нужен ключ",
                    "how": "/diag?key=<WEBHOOK_SECRET> (подойдёт и BOT_TOKEN)",
                    "webhook_secret_set": bool(WEBHOOK_SECRET),
                })
            probe = q.get("probe") in ("1", "true", "yes")
            try:
                diag = _run(_collect_diagnostics, probe)
            except Exception:                             # noqa: BLE001
                diag = {"diag_error": traceback.format_exc()[-1500:]}
            _journal("diag", probe=probe,
                     problems=diag.get("config_problems"),
                     webhook=(diag.get("telegram_webhook") or {}).get("url"))
            return self._json(200, diag)

        # служебный мусор браузеров — тихо отдаём 204, не пишем в журнал
        if path in ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png"):
            return self._respond(200, b"")   # пустой ответ, без записи в журнал

        # healthcheck: статус конфигурации (без секретов)
        problems = _config_problems()
        status = "✅ card-battle-bot: webhook-функция работает"
        if problems:
            status = "⚠️ card-battle-bot: функция запущена, но НЕ НАСТРОЕНА"
        lines = [
            status,
            f"python {sys.version.split()[0]} | aiogram {_aiogram_version()} | "
            f"vendor: {'да' if os.path.isdir(os.path.join(_HERE, '_vendor')) else 'нет'}",
            "переменные окружения:",
        ]
        lines += [f"  {k}: {v}" for k, v in _env_status().items()]
        if problems:
            lines.append("что исправить:")
            lines += [f"  ❌ {p}" for p in problems]
        lines.append(f"запросов: {REQUESTS_TOTAL}, ошибок: {ERRORS_TOTAL}, "
                     f"аптайм контейнера: {int(time.time() - STARTED_AT)} c")
        lines.append("")
        lines.append("👉 полный отчёт (webhook у Telegram, таблицы Supabase, "
                     "журнал, сквозной тест /start):")
        lines.append("   /diag?key=<WEBHOOK_SECRET>&probe=1")
        body = "\n".join(lines).encode()
        _journal("get", path=self.path, code=200, problems=len(problems))
        return self._respond(200, body)

    # ---------- POST: сам webhook ----------

    def do_POST(self):
        global REQUESTS_TOTAL
        REQUESTS_TOTAL += 1
        path = self._parsed().path

        if IMPORT_ERROR:
            _journal("error", where="import", detail=IMPORT_ERROR.splitlines()[-1])
            return self._json(500, _import_diagnostics())

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

        # --- проверка секрета ---
        if WEBHOOK_SECRET:
            if secret == WEBHOOK_SECRET:
                pass
            elif WEBHOOK_SECRET in path:
                # webhook зарегистрирован «криво» (секрет попал в URL) —
                # работать не мешаем, но предупреждаем в журнале
                _journal("warn", where="secret",
                         detail="секрет приехал в пути, а не в заголовке — "
                                "перерегистрируйте webhook: webhook_tool.py set")
            else:
                _journal("error", where="secret", code=403, path=path,
                         detail="заголовок X-Telegram-Bot-Api-Secret-Token не "
                                "совпал с WEBHOOK_SECRET")
                return self._respond(
                    403,
                    ("403: secret mismatch. Перерегистрируйте webhook:\n"
                     "python3 webhook_tool.py set --url https://<проект>.vercel.app/tg\n"
                     ).encode())

        if not body:
            _journal("warn", where="post", path=path, detail="пустое тело")
            return self._respond(400, b"empty body")

        # --- обработка апдейта ---
        started = time.time()
        try:
            info = _run(_process_update, body)
        except Exception as e:                            # noqa: BLE001
            tb = traceback.format_exc()
            detail = f"{type(e).__name__}: {e}"
            # Апдейт, который мы в принципе не можем разобрать (битый JSON или
            # новый тип события, которого ещё нет в нашей версии aiogram),
            # бессмысленно повторять — иначе Telegram будет долбить вечно.
            unparseable = type(e).__name__ in ("ValidationError", "JSONDecodeError")
            _journal("error", where="parse" if unparseable else "handler",
                     path=path, detail=detail[:500])
            say("[tg] ОШИБКА ОБРАБОТКИ АПДЕЙТА:\n" + tb)
            # 1) пробуем сказать об этом пользователю прямо в Telegram
            notified = "не пытались"
            try:
                chat_id = self._chat_id_from_body(body)
                if chat_id:
                    text = ("⚠️ Бот получил событие, которое не смог разобрать "
                            "(устаревшая сборка):\n" if unparseable else
                            "⚠️ Внутренняя ошибка бота:\n")
                    notified = _run(
                        _notify_user_error_wrap, chat_id,
                        text + f"<code>{html.escape(detail[:300])}</code>\n\n"
                        "Диагностика: откройте адрес функции с "
                        "<code>/diag?key=WEBHOOK_SECRET&amp;probe=1</code>")
            except Exception as e2:                       # noqa: BLE001
                notified = f"сбой уведомления: {type(e2).__name__}: {e2}"
            _journal("error_notify", detail=notified)
            if unparseable:
                return self._respond(200, f"ignored: {detail}".encode())
            # 2) если сообщить не удалось — 500, Telegram повторит доставку
            if notified == "ok":
                return self._respond(200, f"handled with error: {detail}".encode())
            return self._respond(500, f"error: {detail}".encode())

        info["ms"] = round((time.time() - started) * 1000)
        info["path"] = path
        info["code"] = 200
        _journal("update", **info)
        if not info.get("handled"):
            # апдейт дошёл, но ни один хендлер не сработал — частая причина
            # «бот молчит» (например, не тот allowed_updates или текст)
            say("[tg] ВНИМАНИЕ: ни один хендлер не подошёл под апдейт "
                f"(тип: {info.get('update_type')}, id {info.get('update_id')})")
        self._respond(200, b"ok")

    def _chat_id_from_body(self, body: bytes):
        try:
            data = json.loads(body)
        except Exception:                                 # noqa: BLE001
            return None
        for key in ("message", "edited_message", "channel_post"):
            if isinstance(data.get(key), dict):
                return (data[key].get("chat") or {}).get("id")
        cb = data.get("callback_query")
        if isinstance(cb, dict):
            msg = cb.get("message") or {}
            return (msg.get("chat") or {}).get("id")
        return None

    # ---------- прочее ----------

    def log_message(self, fmt, *args):
        # свой формат в stdout (стандартный stderr рантайм может не показать)
        say(f"[http] {self.command} {fmt % args}")


async def _notify_user_error_wrap(chat_id: int, text: str) -> str:
    """Отдельный корутин-враппер: свой Bot/сессия на уведомление."""
    bot = _make_bot()
    try:
        return await _notify_user_error(bot, chat_id, text)
    finally:
        await bot.session.close()
