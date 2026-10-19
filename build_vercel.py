#!/usr/bin/env python3
"""
Сборщик vercel-варианта бота из основного файла bot_commented.py.

Логика (движок, UI, админка) копируется 1-в-1; заменяются ТОЛЬКО:
  1) БД-секция (SQLite/aiosqlite)  → Supabase PostgREST (db_supabase_block.py);
  2) конфиг-константы              → переменные окружения Vercel;
  3) секция ЗАПУСК (polling main)  → удалена (точка входа — api/tg.py webhook).

Запуск:  python build_vercel.py   (из папки vercel/)
Результат: api/bot_app.py — всегда синхронен с основным ботом.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "bot_commented.py"))
DB_TEMPLATE = os.path.join(HERE, "db_supabase_block.py")
OUT = os.path.join(HERE, "api", "bot_app.py")

DB_START = "# =====================================================================\n# ЛОКАЛЬНАЯ БАЗА ДАННЫХ"
DB_END = "# =====================================================================\n# ГЕНЕРАЦИЯ КАРТ"
RUN_START = "# =====================================================================\n# ЗАПУСК"


def main():
    src = open(SRC, encoding="utf-8").read()
    db_block = open(DB_TEMPLATE, encoding="utf-8").read()

    # 1) замена БД-секции
    i1 = src.index(DB_START)
    i2 = src.index(DB_END)
    src = src[:i1] + db_block.rstrip("\n") + "\n\n\n" + src[i2:]

    # 2) конфиг → env-переменные Vercel
    reps = [
        ('BOT_TOKEN = "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА"  # токен от @BotFather',
         'BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # Vercel → Environment Variables'),
        ('ACCESS_PASSWORD = "1234"',
         'ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "1234")'),
        ('ADMIN_PASSWORD = "РиЧорт"',
         'ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "РиЧорт")'),
        ("import aiosqlite\n", ""),
        ('DB_PATH = "cards_game.db"  # локальная БД — обычный файл SQLite',
         '# DB_PATH не используется: БД — Supabase (SUPABASE_URL/SUPABASE_KEY)'),
    ]
    for old, new in reps:
        if old not in src:
            print(f"ВНИМАНИЕ: не найдено для замены: {old[:60]!r}")
        src = src.replace(old, new, 1)

    # 3) убираем polling-запуск (точка входа — api/tg.py)
    i3 = src.index(RUN_START)
    src = src[:i3].rstrip("\n") + "\n\n\n" + (
        "# =====================================================================\n"
        "# ЗАПУСК НА VERCEL\n"
        "# Polling здесь не используется: точки входа — api/tg.py (webhook).\n"
        "# Локально этот файл можно импортировать для тестов (см. test_vercel_db.py).\n"
        "# =====================================================================\n"
    )

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(src)

    # самопроверка: файл должен компилироваться
    compile(src, OUT, "exec")
    print(f"✅ vercel/api/bot_app.py собран ({len(src.splitlines())} строк), компилируется.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
