# =====================================================================
#  Telegram-бот «Карточные бои» — реализация согласно плану
#  (игра наподобие «аникард»: коллекция карт + позиционные бои)
#
#  ЭТО ВЕРСИЯ С ПОДРОБНЫМИ КОММЕНТАРИЯМИ.
#  Точно такой же код без комментариев — в файле bot_clean.py.
#
#  Стек: Python 3.10+, aiogram 3.x, aiosqlite (локальная БД SQLite).
#
#  Запуск:
#    1) pip install -r requirements.txt
#    2) впишите токен бота от @BotFather в BOT_TOKEN
#    3) смените ACCESS_PASSWORD на свой пароль
#    4) python bot_commented.py
# =====================================================================

import asyncio
import json
import logging
import os
import random
import re
import tempfile

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# =====================================================================
# НАСТРОЙКИ — все «регуляторы» собраны здесь
# =====================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # Vercel → Environment Variables

# Пароль доступа к боту (по заданию устанавливается прямо в коде).
# Логика такая: пока у пользователя в БД access=0, бот отвечает ТОЛЬКО
# на ввод пароля. После правильного ввода в таблицу users сразу
# пишется access=1 — больше пароль этому пользователю не нужен.
# Состояние «ждём пароль» тоже хранится в БД (awaiting_password),
# а не в FSM-памяти — это обязательно для serverless-деплоя (Vercel).
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "1234")

# Пароль админ-панели (команда /admin) — по заданию заказчика.
# Админ-панель: выдача/прокачка/удаление карт, смена редкости,
# лечение и «прогон боёв» (N < 50 симуляций с полным логом ходов).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "РиЧорт")

# Максимум боёв в админском прогоне (строго меньше 50)
SIM_MAX_BATTLES = 49

# DB_PATH не используется: БД — Supabase (SUPABASE_URL/SUPABASE_KEY)

# Размер колоды. В плане сказано «6 или 7 карт» — выбрано 7.
MAX_DECK = 7

# По заданию кнопка «Получить карту» пока работает БЕЗ ограничений.
# Когда ограничение понадобится — поставьте True: в колоде не сможет
# находиться больше MAX_DECK активных карт.
DECK_LIMIT_ENABLED = False

# По плану здоровье НЕ восстанавливается (во время боя).
# Про восстановление МЕЖДУ боями в плане ничего нет, поэтому это
# авторское решение: после боя выжившие карты полностью лечатся.
# Поставьте False для «хардкора» — раны будут сохраняться навсегда.
HEAL_AFTER_BATTLE = True

# Стоимость заклинаний в манне (в плане не указана — авторский выбор):
MAGE_ATTACK_MANA = 1    # обычная дальняя атака заклинателя
MAGE_STEALTH_MANA = 2   # скрытая атака (выстрел через фронт в тыл)

# Базовая выносливость (по желанию заказчика): 4.
# Тратится по 1 за каждое действие персонажа (ДП), +1 за ход в колоде.
BASE_ENDURANCE = 4

# Анти-столл «Усталость»: с этого раунда юниты на поле начинают терять HP
# (растёт каждые 10 раундов) — бой не может длиться вечно.
FATIGUE_ROUND = 40

# Опыт (план: «Персонажи получают опыт за сражения», цифры — авторские):
KILL_XP_PER_LEVEL = 10  # опыт за убийство = 10 × уровень жертвы
WIN_XP = 30             # бонус каждой выжившей карте за победу
LOSE_XP = 10            # утешительный бонус за поражение/ничью


def xp_to_next(level: int) -> int:
    # Сколько опыта нужно для следующего уровня
    return 50 * level


# =====================================================================
# КЛАССЫ (по плану пункт 1)
# =====================================================================

CLS_WARRIOR = "warrior"   # Воин — ближний бой
CLS_MAGE = "mage"         # Заклинатель — дальние заклинания за манну

CLS_NAME = {CLS_WARRIOR: "Воин ⚔️", CLS_MAGE: "Заклинатель 🔮"}
CLS_EMOJI = {CLS_WARRIOR: "⚔️", CLS_MAGE: "🔮"}
# Парная стата к здоровью у каждого класса своя:
SUB_NAME = {CLS_WARRIOR: "Скрытность 🥷", CLS_MAGE: "Точность 🎯"}

# Типовые значения 1-го уровня (цитата из плана):
#   Воин:         Атака=7,  Здоровье=15, КЗ=15, Скрытность=0
#   Заклинатель:  Атака=10, Мана=4-5,    Здоровье=7, КЗ=10, Точность=0
BASE_STATS = {
    CLS_WARRIOR: {"attack": 7, "hp": 15, "armor": 15, "mana_max": 0},
    CLS_MAGE: {"attack": 10, "hp": 7, "armor": 10, "mana_max": 4},
}

# =====================================================================
# РЕДКОСТИ КАРТ — одобренный вариант 4 «гибрид» (docs/b_rarities.md):
# умеренный множитель статов (×1.00–×1.20 — в пределах одного шага
# системы весов, тактика остаётся важнее грейда) + косметика + pity.
# Механика высоких грейдов — легендарная магия (см. SPELLS ниже).
# =====================================================================

RARITY_INFO = {
    "common":    {"label": "⬜ Обычная",    "mult": 100, "weight": 50, "rank": 0},
    "uncommon":  {"label": "🟩 Необычная",  "mult": 105, "weight": 27, "rank": 1},
    "rare":      {"label": "🟦 Редкая",     "mult": 110, "weight": 15, "rank": 2},
    "epic":      {"label": "🟪 Эпическая",  "mult": 115, "weight": 6,  "rank": 3},
    "legendary": {"label": "🟨 Легендарная", "mult": 120, "weight": 2,  "rank": 4},
}
RARITY_KEYS = list(RARITY_INFO.keys())
PITY_THRESHOLD = 40  # 40 карт подряд без «редкой+» → следующая гарантированно 🟦+


def rarity_rank(r: str) -> int:
    return RARITY_INFO.get(r, RARITY_INFO["common"])["rank"]


def rarity_label(r: str) -> str:
    return RARITY_INFO.get(r, RARITY_INFO["common"])["label"]

# =====================================================================
# КОЛОДЫ (до 3), ОТОБРАЖЕНИЕ КАРТ И СОРТИРОВКА КОЛЛЕКЦИИ
# =====================================================================

# Сколько колод может собрать игрок. Колода карты хранится прямо в
# cards.in_deck: 0 — вне колод, 1/2/3 — номер колоды (старые данные с
# in_deck=1 автоматически считаются колодой №1 — миграция не нужна).
DECK_COUNT = 3

# С какого уровня можно даровать титул (кнопка в карточке коллекции)
TITLE_MIN_LEVEL = 3

# Критерии сортировки коллекции: (код, подпись). Код с «-» на конце —
# по убыванию. По умолчанию — 📅 дата получения (id, как раньше).
SORTS = [("id", "📅 Дата получения"), ("rar", "✨ Редкость"), ("lvl", "⭐ Уровень"),
         ("atk", "⚔️ Атака"), ("def", "🛡 Защита"), ("hp", "❤️ Здоровье"),
         ("mna", "🔮 Запас манны"), ("end", "⚡ Выносливость"),
         ("sub", "🥷 Скрытность/🎯 Точность"), ("cls", "🧬 Класс"),
         ("rac", "🎭 Раса"), ("nm", "🔤 Имя")]


def sanitize_title(text: str) -> str:
    # Титул вводится игроком и попадает в HTML-сообщения — вырезаем
    # опасные символы и ограничиваем длину.
    return re.sub(r"[<>&]", "", text or "").strip()[:32]


def disp_name(x, surname: bool = True) -> str:
    # Имя для показа: если карте дарован титул и включён режим
    # «использовать как имя» — титул идёт ПЕРВЫМ, затем настоящие
    # имя и фамилия: «Гроза Юга» Пупкин Васильев.
    # x — строка из БД или боевой юнит (dict).
    title = (x.get("title") or "").strip() if hasattr(x, "get") else ""
    base = x["name"] + (f" {x['surname']}" if surname and x.get("surname") else "")
    if title and x.get("use_title"):
        return f"«{title}» {base}"
    return base


def main_stat(cls: str, attack: int, armor: int):
    # Главная цифра карты: у воинов — что БОЛЬШЕ из атаки/защиты,
    # у заклинателей — всегда атака. Возвращает (значок, число).
    if cls == CLS_MAGE:
        return "⚔", attack
    return ("⚔", attack) if attack >= armor else ("🛡", armor)


def card_line(c) -> str:
    # Карта в одну строку (коллекция/колоды). Порядок по заказу:
    # класс, редкость, уровень, имя, атака или защита (что больше;
    # у магов всегда атака), здоровье у воинов / манна у заклинателей.
    rem = rarity_label(c.get("rarity") or "common").split(" ")[0]
    ico, val = main_stat(c["cls"], c["attack"], c["armor"])
    if c["cls"] == CLS_MAGE:
        res = f"🔮{c['mana_max']}"
    else:
        res = f"❤{c['hp']}" if c["hp"] >= c["hp_max"] else f"❤{c['hp']}/{c['hp_max']}"
    return f"{CLS_EMOJI[c['cls']]}{rem} L{c['level']} {disp_name(c)} {ico}{val} {res}"


def unit_line(u: dict) -> str:
    # Та же строка, но для боевого юнита (вкладка «Колода»/высадка):
    # ресурсы текущие, а при выносливости ≤1 вместо ❤/🔮 показывается
    # молния с остатком: ⚡1/4.
    ico, val = main_stat(u["cls"], u["attack"], u["armor"])
    if u["end"] <= 1:
        res = f"⚡{u['end']}/{u['end_max']}"
    elif u["cls"] == CLS_MAGE:
        res = f"🔮{u['mana']}"
    else:
        res = f"❤{u['hp']}"
    return f"{CLS_EMOJI[u['cls']]}{ico}{val} {res} {u['name']} L{u['level']}"


def sort_label(s: str) -> str:
    code = s[:-1] if s.endswith("-") else s
    for c, label in SORTS:
        if c == code:
            return label + (" 🔽" if s.endswith("-") else " 🔼")
    return "📅 Дата получения 🔼"


def sort_cards(cards, s: str):
    # s — код критерия из SORTS, «-» на конце = по убыванию.
    code = s[:-1] if s.endswith("-") else s
    desc = s.endswith("-")
    keyf = {
        "id":  lambda c: c["id"],
        "rar": lambda c: rarity_rank(c.get("rarity") or "common"),
        "lvl": lambda c: c["level"],
        "atk": lambda c: c["attack"],
        "def": lambda c: c["armor"],
        "hp":  lambda c: c["hp_max"],
        "mna": lambda c: c["mana_max"],
        "end": lambda c: c["end_max"],
        "sub": lambda c: c["sub"],
        "cls": lambda c: 0 if c["cls"] == CLS_WARRIOR else 1,
        "rac": lambda c: RACE_KEYS.index(c["race"]) if c["race"] in RACE_KEYS else 99,
        "nm":  lambda c: (c["name"] or "").lower(),
    }.get(code)
    if keyf is None:
        return list(cards)
    return sorted(cards, key=lambda c: (keyf(c), c["id"]), reverse=desc)


def decks_of(cards) -> dict:
    # Раскладывает карты по колодам {1: [...], 2: [...], 3: [...]}
    return {n: [c for c in cards if c["in_deck"] == n] for n in range(1, DECK_COUNT + 1)}

# =====================================================================
# РАССЫ И ИМЕНА
# 10 рас × 20 имён × 20 фамилий = 200 имён и 200 фамилий,
# 400 уникальных сочетаний на расу (4000 всего). Имя и фамилия
# карты всегда берутся из пула ОДНОЙ расы, поэтому «эльф-гном»
# не появится. Дополнительно при генерации идёт проверка:
# у одного игрока не должно быть двух карт с одинаковым полным
# именем (перебор до 8 раз, затем римский суффикс «II», «III»...).
# =====================================================================

RACES = {
    "Человек": {
        "emoji": "🧑",
        "names": ["Яромир", "Всеслав", "Радомир", "Борислав", "Ждан",
                  "Тихомир", "Драгомир", "Любояр", "Пересвет", "Светозар",
                  "Мстислав", "Добрыня", "Ратибор", "Гостомысл", "Милослав",
                  "Ярополк", "Святогор", "Богдан", "Велимир", "Ярояр"],
        "surnames": ["Кузнецов", "Молчанов", "Зарецкий", "Колячев", "Стрежнев",
                     "Оболенский", "Ветров", "Дубровин", "Скуратов", "Бережной",
                     "Ратников", "Светлов", "Житомирский", "Брусилов", "Хотенов",
                     "Яругин", "Голицын", "Ртищев", "Межевский", "Солнцев"],
    },
    "Эльф": {
        "emoji": "🧝",
        "names": ["Аэльтарион", "Финориэн", "Галатель", "Мирэлис", "Таэнарин",
                  "Сильварис", "Эленриэль", "Фаэнор", "Ильтэрас", "Ниэннор",
                  "Аэльмир", "Тиндарель", "Элариэль", "Мириандель", "Фелаэнор",
                  "Сариэт", "Талерон", "Ильверин", "Наэвис", "Аэрандир"],
        "surnames": ["Серебролист", "Лунопевец", "Звездосвет", "Тенелес", "Утреннерос",
                     "Сумеркоцвет", "Ветрострун", "Чистолист", "Златозвезд", "Росопев",
                     "Утропевец", "Сереброток", "Звездопад", "Луннолист", "Заренос",
                     "Тихоструй", "Сумерколист", "Росноцвет", "Ветрокрыл", "Алмазносерд"],
    },
    "Гном": {
        "emoji": "⛏️",
        "names": ["Торгрим", "Хальвар", "Брокк", "Дурнар", "Гримбальд",
                  "Стейнар", "Ульвар", "Фергрим", "Магнар", "Регинн",
                  "Дварин", "Стейнольф", "Хрокар", "Балгур", "Орвар",
                  "Тронд", "Синдре", "Альвис", "Рурнок", "Хальдан"],
        "surnames": ["Железнобород", "Сталекуй", "Каменорук", "Глубосекир", "Наковалобой",
                     "Рудозвон", "Златощит", "Горноступ", "Крепковар", "Рунокуй",
                     "Золотожил", "Меднобров", "Глубокодел", "Кремнец", "Сталевар",
                     "Молотобой", "Горнобрат", "Углекоп", "Кварцев", "Обсидианов"],
    },
    "Орк": {
        "emoji": "👹",
        "names": ["Гашнак", "Урукхан", "Моргаш", "Скарфанг", "Грокхар",
                  "Азгаш", "Бурзуг", "Дурбаш", "Харган", "Лугаш",
                  "Ургаш", "Могрок", "Грумаш", "Тарок", "Балгаш",
                  "Шаггар", "Волтаг", "Каргаш", "Доргул", "Зургаш"],
        "surnames": ["Костекруш", "Кровавыйклык", "Железныйкулак", "Черепкол", "Красносекир",
                     "Чернощит", "Шрамолик", "Боешкур", "Тяжкость", "Громокул",
                     "Сталечелюсть", "Кровотопор", "Шкуродёр", "Костолом", "Громоглас",
                     "Смертекруш", "Яроглаз", "Бронноспин", "Каменолом", "Зубодрал"],
    },
    "Гоблин": {
        "emoji": "👺",
        "names": ["Сник", "Зрик", "Крик", "Нибс", "Гизл",
                  "Фикс", "Снаг", "Тибс", "Разз", "Шмик",
                  "Чвик", "Шустр", "Кляп", "Зырка", "Мырга",
                  "Скрип", "Тык", "Нюх", "Чмых", "Вжик"],
        "surnames": ["Длинноух", "Острозуб", "Карманник", "Грязнух", "Крысохон",
                     "Капканик", "Жилорез", "Болторез", "Сквятник", "Тихиш",
                     "Быстрорук", "Отмычник", "Тугокошель", "Ловколап", "Ворглаз",
                     "Мелкобег", "Монетник", "Тихотап", "Шуршун", "Вертяк"],
    },
    "Нежить": {
        "emoji": "💀",
        "names": ["Мортус", "Некрос", "Гриммер", "Кадаврос", "Спектрос",
                  "Мортис", "Тенебрис", "Реквием", "Оссуар", "Танатос",
                  "Сепулькрос", "Фантомос", "Лихтар", "Упырь", "Вурдалак",
                  "Мортифер", "Чумос", "Морок", "Криптос", "Ноктюрн"],
        "surnames": ["Могильный", "Костяной", "Криптов", "Гробов", "Воронов",
                     "Тленов", "Мрачный", "Прахов", "Смердин", "Безмолвный",
                     "Погребный", "Курганов", "Трунов", "Вещий", "Плесеннев",
                     "Заполун", "Тенеголов", "Окостенев", "Мертвов", "Хладнов"],
    },
    "Демон": {
        "emoji": "😈",
        "names": ["Ваэлгор", "Азрат", "Ксаар", "Инфернус", "Дагхар",
                  "Баалзар", "Нергар", "Заргон", "Молокар", "Ахразар",
                  "Велзарин", "Голкарн", "Инфернакс", "Малзагар", "Зарроган",
                  "Тифон", "Казраал", "Бельзар", "Наргул", "Сульфатар"],
        "surnames": ["Пламенный", "Безднин", "Багровый", "Адский", "Грешный",
                     "Жгучий", "Пропастный", "Тьмовой", "Изверский", "Пеклов",
                     "Серный", "Расколот", "Дымный", "Жароглот", "Угольный",
                     "Скверный", "Тартарный", "Кровожад", "Мразный", "Гибельный"],
    },
    "Ангел": {
        "emoji": "👼",
        "names": ["Серафиэль", "Аурион", "Люминар", "Каэлум", "Астраэль",
                  "Эвари", "Солмир", "Итуриэль", "Зефон", "Ориас",
                  "Элоар", "Казриэль", "Мифаэль", "Офаниэль", "Задокар",
                  "Ариэт", "Галалиэль", "Хавриэль", "Цифион", "Элезар"],
        "surnames": ["Светозарный", "Небесный", "Крылатый", "Златопер", "Лучезарный",
                     "Благодатный", "Непорочный", "Тихогласный", "Высокий", "Чистый",
                     "Милосердный", "Праведный", "Белоснежный", "Златоглас", "Рассветный",
                     "Благовестный", "Чистосвет", "Милостный", "Солнцелик", "Добросерд"],
    },
    "Зверолюд": {
        "emoji": "🐺",
        "names": ["Ррар", "Рык", "Клык", "Гронк", "Звер",
                  "Шарх", "Рыск", "Волч", "Барс", "Гррах",
                  "Когот", "Зверояр", "Вой", "Грива", "Хвост",
                  "Злоб", "Хищ", "Троп", "Скок", "Ревун"],
        "surnames": ["Острокогот", "Быстрый", "Яростный", "Серогрив", "Остроглаз",
                     "Мощный", "Дикий", "Клыкастый", "Полосат", "Косматый",
                     "Зверолов", "Густомех", "Твердолоб", "Быстроног", "Гончий",
                     "Тихошаг", "Ночезор", "Сталекогот", "Дикосерд", "Тропоход"],
    },
    "Драконорожденный": {
        "emoji": "🐉",
        "names": ["Драккар", "Вуррим", "Ссарат", "Каррат", "Драксус",
                  "Змеян", "Фафнар", "Бахамир", "Тиамар", "Назгар",
                  "Вайрм", "Кракс", "Ссарик", "Драконис", "Игнар",
                  "Вермитал", "Скайт", "Аургон", "Пирот", "Нажир"],
        "surnames": ["Огнедыш", "Чешуйчатый", "Кожекрыл", "Драконсерд", "Златохвост",
                     "Искроплет", "Магмогрив", "Пепельный", "Раскаленный", "Змеуст",
                     "Златочешуй", "Огнеглаз", "Дымокрыл", "Камнехвост", "Жемчузуб",
                     "Багрошкур", "Сталечешуй", "Громокрыл", "Вулканов", "Пламеносный"],
    },
    # Голем — служебная «раса» для summon-заклинаний; в обычной генерации
    # карт НЕ участвует (см. GEN_RACES ниже).
    "Голем": {
        "emoji": "🗿",
        "names": ["Голем", "Страж", "Колосс", "Истукан"],
        "surnames": ["Каменный", "Глиняный", "Гранитный", "Базальтовый"],
    },
}

# Расы, участвующие в обычной генерации карт (все, кроме служебного Голема)
GEN_RACES = [r for r in RACES if r != "Голем"]

# =====================================================================
# РАСКЛАДКИ ПОЛЯ БОЯ (план п.2 и п.4.3 + правка заказчика: ВСЕГО 4 КЛЕТКИ)
# На линии максимум 3 клетки и минимум 1, поэтому при 4 клетках
# возможны три раскладки:
#   3 фронта + 1 тыл   (тыл из одной клетки нумеруется «2»)
#   2 фронта + 2 тыла  (линии из двух клеток нумеруются «1 и 3»)
#   1 фронт  + 3 тыла  (фронт из одной клетки нумеруется «2»)
# Нумерация по плану: 3 клетки → 1,2,3; 2 клетки → 1 и 3; 1 клетка → 2.
# Поля «перевёрнуты»: наша клетка 1 — самая левая, у врага самая левая
# (с нашей точки зрения) — клетка 3. Поэтому «прямо напротив» клетки p
# стоит клетка (4 - p).
# =====================================================================

TOTAL_CELLS = 4  # клеток у каждой стороны (по заданию; в изначальном плане было 5)

LAYOUTS = {
    "31": {"front": [1, 2, 3], "rear": [2]},
    "22": {"front": [1, 3], "rear": [1, 3]},
    "13": {"front": [2], "rear": [1, 2, 3]},
}


def copy_layout(layout: dict) -> dict:
    # Копия раскладки, чтобы состояние боя не делило списки с константой
    return {"front": list(layout["front"]), "rear": list(layout["rear"])}


def mirror_pos(pos: int) -> int:
    # Клетка врага, стоящая ровно напротив нашей клетки pos
    return 4 - pos


def other_side(side: str) -> str:
    return "enemy" if side == "player" else "player"


def side_name(side: str) -> str:
    return "Вы" if side == "player" else "Противник"


# =====================================================================
# БАЗА ДАННЫХ — SUPABASE (Postgres) через PostgREST REST API (aiohttp).
#
# Этот блок ЗАМЕНЯЕТ sqlite-секцию локального бота при сборке
# vercel-варианта (см. build_vercel.py). Сигнатуры всех функций
# совпадают 1-в-1 с локальной версией — игровой код разницы не замечает.
#
# Почему REST, а не TCP-драйвер Postgres: на serverless каждый вызов
# функции — новый контейнер; TCP-пулы мгновенно исчерпывают лимит
# соединений БД. HTTP-запросы через Supabase REST этого лишены
# (подробности — docs/a_webhook_vercel.md).
#
# Переменные окружения (Vercel → Settings → Environment Variables):
#   SUPABASE_URL — например https://xxxx.supabase.co
#   SUPABASE_KEY — service_role key (или anon + настроенный RLS)
# Схема таблиц — в supabase_schema.sql (применить в SQL Editor один раз).
# =====================================================================

import aiohttp

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_session: aiohttp.ClientSession | None = None


async def db_init():
    # Одна aiohttp-сессия на «тёплый» контейнер (создаётся лениво)
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()


async def db_close():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _rest(method: str, table: str, params: dict = None,
                json_body=None, prefer: str = None):
    # Низкоуровневый запрос к PostgREST: /rest/v1/<table>
    if _session is None or _session.closed:
        await db_init()
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    async with _session.request(method, url, params=params or {},
                                json=json_body, headers=headers) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Supabase {method} {table} → HTTP {r.status}: {text[:300]}")
        if not text:
            return []
        return json.loads(text)


# ---------- пользователи и доступ ----------

async def ensure_user(user_id: int, username):
    # Upsert: insert ... on conflict (user_id) do update username
    await _rest("POST", "users",
                json_body={"user_id": user_id, "username": username},
                prefer="resolution=merge-duplicates")


async def has_access(user_id: int) -> bool:
    rows = await _rest("GET", "users",
                       params={"user_id": f"eq.{user_id}", "select": "access", "limit": "1"})
    return bool(rows and rows[0].get("access"))


async def grant_access(user_id: int):
    # После правильного пароля СРАЗУ отмечаем в БД, что доступ разрешён
    await _rest("PATCH", "users", params={"user_id": f"eq.{user_id}"},
                json_body={"access": 1, "awaiting_password": 0})


async def user_flag(user_id: int, col: str):
    rows = await _rest("GET", "users",
                       params={"user_id": f"eq.{user_id}", "select": col, "limit": "1"})
    return (rows[0].get(col) or 0) if rows else 0


async def set_user_flag(user_id: int, col: str, val: int):
    await _rest("PATCH", "users", params={"user_id": f"eq.{user_id}"},
                json_body={col: val})


# ---------- карты ----------

async def db_add_card(user_id: int, d: dict) -> int:
    # Новая карта попадает в КОЛЛЕКЦИЮ, но не в колоду (in_deck=0)
    rows = await _rest("POST", "cards", json_body={
        "user_id": user_id, "name": d["name"], "surname": d["surname"],
        "race": d["race"], "cls": d["cls"], "rarity": d.get("rarity", "common"),
        "level": d["level"], "exp": d["exp"], "attack": d["attack"], "armor": d["armor"],
        "hp": d["hp_max"], "hp_max": d["hp_max"], "sub": d["sub"],
        "mana_max": d["mana_max"], "end_max": d["end_max"], "in_deck": 0,
    }, prefer="return=representation")
    return rows[0]["id"] if rows else 0


async def db_get_card(card_id: int, user_id: int):
    rows = await _rest("GET", "cards", params={
        "id": f"eq.{card_id}", "user_id": f"eq.{user_id}", "select": "*", "limit": "1"})
    return rows[0] if rows else None


async def db_user_cards(user_id: int):
    # Вся коллекция (погибшие в бою карты НЕ исчезают навсегда)
    return await _rest("GET", "cards", params={
        "user_id": f"eq.{user_id}", "select": "*", "order": "id.asc"})


async def db_battle_cards(user_id: int, deck: int = 1):
    # Карты выбранной колоды (in_deck = номер колоды 1..3) — именно они
    # идут в бой (не больше MAX_DECK, первые по id)
    deck = max(1, min(int(deck), DECK_COUNT))
    return await _rest("GET", "cards", params={
        "user_id": f"eq.{user_id}", "in_deck": f"eq.{deck}", "select": "*",
        "order": "id.asc", "limit": str(MAX_DECK)})


async def db_name_taken(user_id: int, name: str, surname: str) -> bool:
    rows = await _rest("GET", "cards", params={
        "user_id": f"eq.{user_id}", "name": f"eq.{name}", "surname": f"eq.{surname}",
        "select": "id", "limit": "1"})
    return bool(rows)


async def db_admin_heal_all(user_id: int):
    # PostgREST не умеет UPDATE hp=hp_max одной командой — читаем и патчим
    for c in await db_user_cards(user_id):
        if c.get("hp") != c.get("hp_max"):
            await _rest("PATCH", "cards", params={"id": f"eq.{c['id']}"},
                        json_body={"hp": c["hp_max"]})


async def db_set_in_deck(card_id: int, val: int):
    # val: 0 — вне колод, 1..DECK_COUNT — номер колоды
    await _rest("PATCH", "cards", params={"id": f"eq.{card_id}"},
                json_body={"in_deck": val})


async def db_set_title(card_id: int, title: str, user_id: int):
    # Дарованный титул (текст уже очищен sanitize_title)
    await _rest("PATCH", "cards",
                params={"id": f"eq.{card_id}", "user_id": f"eq.{user_id}"},
                json_body={"title": title})


async def db_set_use_title(card_id: int, val: int, user_id: int):
    # Использовать титул как имя в бою/колодах/коллекции (1/0)
    await _rest("PATCH", "cards",
                params={"id": f"eq.{card_id}", "user_id": f"eq.{user_id}"},
                json_body={"use_title": val})


async def db_delete_card(card_id: int, user_id: int):
    await _rest("DELETE", "cards",
                params={"id": f"eq.{card_id}", "user_id": f"eq.{user_id}"})


async def db_set_rarity(card_id: int, rarity: str, user_id: int):
    await _rest("PATCH", "cards",
                params={"id": f"eq.{card_id}", "user_id": f"eq.{user_id}"},
                json_body={"rarity": rarity})


async def db_admin_set_stats(card_id: int, d: dict):
    await _rest("PATCH", "cards", params={"id": f"eq.{card_id}"}, json_body={
        "level": d["level"], "attack": d["attack"], "armor": d["armor"],
        "hp": d["hp_max"], "hp_max": d["hp_max"], "sub": d["sub"],
        "mana_max": d["mana_max"]})


async def db_sync_card_after_battle(u: dict, hp: int):
    await _rest("PATCH", "cards", params={"id": f"eq.{u['card_id']}"}, json_body={
        "level": u["level"], "exp": u["xp"], "attack": u["attack"], "armor": u["armor_base"],
        "hp": hp, "hp_max": u["hp_max"], "sub": u["sub"],
        "mana_max": u["mana_max"], "end_max": u["end_max"]})


# ---------- бои ----------

async def db_save_battle(user_id: int, state: dict):
    # state хранится как JSONB — сериализация на стороне PostgREST
    await _rest("POST", "battles", json_body={"user_id": user_id, "state": state},
                prefer="resolution=merge-duplicates")


async def db_load_battle(user_id: int):
    rows = await _rest("GET", "battles",
                       params={"user_id": f"eq.{user_id}", "select": "state", "limit": "1"})
    if not rows:
        return None
    state = rows[0].get("state")
    if isinstance(state, str):
        state = json.loads(state)
    if not isinstance(state, dict) or "board" not in state:
        # Бой сохранён старой версией бота (другая модель поля) — сбрасываем
        await db_delete_battle(user_id)
        return None
    return state


async def db_delete_battle(user_id: int):
    await _rest("DELETE", "battles", params={"user_id": f"eq.{user_id}"})


async def db_save_sim(user_id: int, n: int, summary: str, log: str) -> int:
    rows = await _rest("POST", "sim_logs", json_body={
        "user_id": user_id, "n": n, "summary": summary, "log": log},
        prefer="return=representation")
    return rows[0]["id"] if rows else 0


# =====================================================================
# ГЕНЕРАЦИЯ КАРТ
# =====================================================================

def d20() -> int:
    # Кубик d20 — единственный источник случайности бросков
    return random.randint(1, 20)


def roman_numeral(n: int) -> str:
    # Римская цифра — суффикс для карт-тёзок («Торгрим Железнобород II»)
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    res = ""
    for v, s in vals:
        while n >= v:
            res += s
            n -= v
    return res


def apply_level_growth(d: dict):
    # Повышение уровня. Темпы роста в плане не заданы — авторский выбор:
    # +1 атака, +2 к максимуму здоровья, +1 КЗ,
    # на каждом чётном уровне +1 скрытность/точность,
    # заклинателю дополнительно +1 к запасу манны.
    d["level"] += 1
    d["attack"] += 1
    d["hp_max"] += 2
    d["armor"] += 1
    if d["level"] % 2 == 0:
        d["sub"] += 1
    if d["cls"] == CLS_MAGE:
        d["mana_max"] += 1


def roll_rarity(pity: bool = False) -> str:
    # Розыгрыш редкости по весам; pity=True — только 🟦 редкая и выше
    keys = RARITY_KEYS[2:] if pity else RARITY_KEYS
    weights = [RARITY_INFO[k]["weight"] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def generate_card_data(level: int = 1, rarity: str = None) -> dict:
    # 0) Редкость (вариант 4): множитель статов ×1.00–×1.20
    if rarity is None:
        rarity = roll_rarity()
    mult = RARITY_INFO[rarity]["mult"]  # в процентах: 100..120

    # 1) Раса, имя и фамилия — из одного расового пула
    race_name = random.choice(GEN_RACES)
    race = RACES[race_name]
    first_name = random.choice(race["names"])
    last_name = random.choice(race["surnames"])

    # 2) Класс — случайно 50/50
    cls = random.choice([CLS_WARRIOR, CLS_MAGE])
    base = BASE_STATS[cls]

    # 3) Система весов (план п.1.1): повышение одной статы пары на 20%
    #    роняет парную на 10%. k — «перекос» в шагах от -2 до +2.
    #    Все округления — в меньшую сторону (целочисленное деление).
    k = random.randint(-2, 2)
    attack = base["attack"] * (10 + 2 * k) // 10
    if cls == CLS_WARRIOR:
        armor = base["armor"] * (10 - k) // 10     # пара: Атака ↔ Защита(КЗ)
        mana_max = 0
    else:
        armor = base["armor"]                      # у заклинателя КЗ без перекоса
        mana_max = random.randint(4, 5)            # план: запас манны 4 или 5
        mana_max = mana_max * (10 - k) // 10       # пара: Атака ↔ Запас Манны

    # 4) Вторая пара: Здоровье ↔ Скрытность/Точность (очко статы = −10% HP)
    m = random.randint(0, 3)
    sub = m
    hp_max = base["hp"] * (10 - m) // 10

    # 5) Множитель редкости — поверх весов, округление вниз
    attack = attack * mult // 100
    armor = armor * mult // 100
    hp_max = hp_max * mult // 100
    mana_max = mana_max * mult // 100

    data = {
        "name": first_name, "surname": last_name, "race": race_name, "cls": cls,
        "rarity": rarity,
        "level": 1, "exp": 0,
        "attack": attack, "armor": armor, "hp_max": hp_max,
        "sub": sub, "mana_max": mana_max, "end_max": BASE_ENDURANCE,
    }
    # Если нужна карта сразу N-го уровня (например, колода противника
    # масштабируется под средний уровень игрока) — «прокручиваем» уровни.
    while data["level"] < level:
        apply_level_growth(data)
    return data

# =====================================================================
# ИГРОВОЙ ДВИЖОК  (версия с общей доской и перемещениями)
# Все функции движка работают с обычным dict'ом состояния боя и НЕ
# обращаются к БД/телеграму — их можно тестировать «в вакууме».
# Состояние боя сериализуется в JSON и хранится в таблице battles.
#
# МОДЕЛЬ ПОЛЯ: доска из 4 фиксированных ЛИНИЙ (сверху вниз):
#   "v2" — тыл врага (В2), "v1" — фронт врага (В1),
#   "s1" — ваш фронт (С1), "s2" — ваш тыл (С2).
# Клетки линий В* задаются раскладкой противника, линий С* — вашей.
# Юниты ЛЮБОЙ стороны могут стоять на ЛЮБОЙ клетке доски: захват клетки
# теперь — это ПЕРЕМЕЩЕНИЕ персонажа на пустую клетку своей или
# соседней линии (порядок соседства: В2 ↔ В1 ↔ С1 ↔ С2; с В1 до С2
# за один шаг не добраться). Принадлежность юнита — поле u["side"].
#
# Структура состояния:
# state = {
#   "round", "turn", "over", "result", "rewards_text", "log", "next_uid",
#   "pending", "bonus_applied",
#   "layouts": {"player": {...}, "enemy": {...}},
#   "board": {"v2": {"2": unit}, "v1": {}, "s1": {}, "s2": {}},
#   "player": {"deck": [unit...], "graveyard": [unit...]},
#   "enemy":  {"deck": [...], "graveyard": [...]},
# }
# =====================================================================

ROWS = ("v2", "v1", "s1", "s2")          # порядок линий сверху вниз
ROW_ORDER = {"v2": 0, "v1": 1, "s1": 2, "s2": 3}
ROW_LABEL = {"v2": "В2", "v1": "В1", "s1": "С1", "s2": "С2"}
ROW_SIDE = {"v2": "enemy", "v1": "enemy", "s1": "player", "s2": "player"}


def row_of(side: str, kind: str) -> str:
    # Линия фронта/тыла указанной стороны (фронт врага для игрока — "v1")
    if side == "enemy":
        return "v1" if kind == "front" else "v2"
    return "s1" if kind == "front" else "s2"


def side_rows(side: str):
    # Обе линии стороны: (фронт, тыл)
    return (row_of(side, "front"), row_of(side, "rear"))


def row_layout(state: dict, line: str):
    # Какие клетки существуют на линии (берётся из раскладки владельца линии)
    lay = state["layouts"][ROW_SIDE[line]]
    return lay["front"] if line in ("v1", "s1") else lay["rear"]


def lines_reach(line: str):
    # Своя линия + соседние — зона досягаемости для ПЕРЕМЕЩЕНИЯ (захвата).
    # Пример: с "v1" (В1) доступны v2, v1, s1, но НЕ s2 («с В1 на С2 не выйдет»).
    i = ROW_ORDER[line]
    return {l for l in ROWS if abs(ROW_ORDER[l] - i) <= 1}


def make_unit(d: dict, side: str, uid: int, card_id=None, hp=None) -> dict:
    # Боевая единица из данных карты. hp — текущее здоровье (важно при
    # HEAL_AFTER_BATTLE=False: карта приходит в бой израненной).
    return {
        "uid": uid, "card_id": card_id, "side": side,
        "name": d["name"], "surname": d["surname"], "race": d["race"], "cls": d["cls"],
        "rarity": d.get("rarity", "common"),
        "level": d["level"], "start_level": d["level"],
        "xp": d.get("exp", 0), "xp_gained": 0,
        "attack": d["attack"],
        "armor": d["armor"], "armor_base": d["armor"],
        "hp": hp if hp is not None else d["hp_max"], "hp_max": d["hp_max"],
        "sub": d["sub"],
        "end": d["end_max"], "end_max": d["end_max"],
        "mana": d["mana_max"], "mana_max": d["mana_max"],
        "zone": "deck", "line": None, "pos": None,
        "defending": False,
        # Очки роста, которые игрок распределяет САМ при повышении уровня:
        "pending_levels": 0, "bonus_xp": 0,
        # Действовала ли карта в ТЕКУЩИЙ ход своей стороны. Сбрасывается в
        # begin_turn; выставляется при любом ДП и при высадке. Нужно для
        # правила «атака при появлении работает только между раундами»:
        # бонусный удар получает лишь тот, кто НЕ действовал в прошлый ход.
        "acted_this_turn": False,
        # Активные эффекты заклинаний: [{"kind","val","turns"}, ...]
        "fx": [],
    }


def create_battle(cards, layout_key: str, enemy_rarity: str = None) -> dict:
    # cards — строки из БД (боевая колода игрока, до MAX_DECK карт).
    # layout_key — ключ выбранной игроком раскладки из LAYOUTS ("31"/"22"/"13").
    # enemy_rarity — фиксированная редкость колоды противника (для админ-
    # прогонов «против колоды аналогичной редкости»); None — случайная.
    state = {
        "round": 1, "turn": "player",
        "over": False, "result": None, "rewards_text": "",
        "log": [], "next_uid": 1,
        "pending": None, "bonus_applied": False,
        "layouts": {
            "player": copy_layout(LAYOUTS[layout_key]),
            # Противнику раскладка выбирается случайно
            "enemy": copy_layout(random.choice(list(LAYOUTS.values()))),
        },
        "board": {line: {} for line in ROWS},
        # Эффекты на клетках: болото 🟢 / стена 🧱 (по линиям, ключи — строки)
        "cell_fx": {line: {} for line in ROWS},
        "player": {"deck": [], "graveyard": []},
        "enemy": {"deck": [], "graveyard": []},
    }
    for c in cards:
        # Титул (если игрок включил «использовать как имя») становится
        # боевым именем юнита — он же попадёт в журнал, на поле и в меню.
        nm = c["name"]
        ttl = (c.get("title") or "").strip()
        if ttl and c.get("use_title"):
            nm = f"«{ttl}» {nm}"
        d = {
            "name": nm, "surname": c["surname"], "race": c["race"], "cls": c["cls"],
            "rarity": c["rarity"] if "rarity" in c.keys() else "common",
            "level": c["level"], "exp": c["exp"], "attack": c["attack"], "armor": c["armor"],
            "hp_max": c["hp_max"], "sub": c["sub"], "mana_max": c["mana_max"], "end_max": c["end_max"],
        }
        u = make_unit(d, "player", state["next_uid"], card_id=c["id"], hp=c["hp"])
        state["next_uid"] += 1
        state["player"]["deck"].append(u)

    # Колода противника: MAX_DECK случайных карт уровня, равного среднему
    # уровню колоды игрока — чтобы бои не были ни проходными, ни безнадёжными.
    avg_level = max(1, round(sum(c["level"] for c in cards) / len(cards)))
    for _ in range(MAX_DECK):
        d = generate_card_data(level=avg_level, rarity=enemy_rarity)
        u = make_unit(d, "enemy", state["next_uid"])
        state["next_uid"] += 1
        state["enemy"]["deck"].append(u)
    return state


# ---------- вспомогательные функции доски ----------

def cell_exists(state: dict, line: str, pos: int) -> bool:
    return pos in row_layout(state, line)


def cell_unit(state: dict, line: str, pos: int):
    return state["board"][line].get(str(pos))


def board_units(state: dict, side: str | None = None):
    # Все юниты на доске: (line, pos, unit). Если side задан — только его юниты.
    for line in ROWS:
        for pos_str, u in state["board"][line].items():
            if side is None or u["side"] == side:
                yield line, int(pos_str), u


def alive_units(state: dict, side: str):
    return [u for _, _, u in board_units(state, side)] + state[side]["deck"]


def all_units(state: dict, side: str):
    # Все карты стороны в бою, включая погибшие (кладбище).
    # Погибшие не могут появиться до конца боя, но сама карта НЕ исчезает
    # навсегда — поэтому, например, очки роста можно распределить и ей.
    return alive_units(state, side) + state[side]["graveyard"]


def alive_count(state: dict, side: str) -> int:
    return len(alive_units(state, side))


def find_unit(state: dict, uid: int):
    # Ищет юнита по uid на всей доске и в колодах/кладбищах.
    # Возвращает (side, unit, line|"deck"|"graveyard"|None, pos|None)
    for line, pos, u in board_units(state):
        if u["uid"] == uid:
            return u["side"], u, line, pos
    for side in ("player", "enemy"):
        for u in state[side]["deck"]:
            if u["uid"] == uid:
                return side, u, "deck", None
        for u in state[side]["graveyard"]:
            if u["uid"] == uid:
                return side, u, "graveyard", None
    return None, None, None, None


def own_empty_cells(state: dict, side: str):
    # Пустые клетки на СВОИХ линиях стороны — куда можно высаживать карты
    # из колоды. Клетка со стеной 🧱 или чужим юнитом-захватчиком занята.
    res = []
    for line in side_rows(side):
        for pos in row_layout(state, line):
            if not cell_blocked(state, line, pos):
                res.append((line, pos))
    return res


def can_move_to(state: dict, u: dict, line: str, pos: int) -> bool:
    # Захват клетки = перемещение на неё (правка заказчика):
    # клетка должна существовать, быть свободной (не юнит и не стена)
    # и находиться на своей или СОСЕДНЕЙ линии относительно линии юнита.
    return (u["end"] > 0
            and cell_exists(state, line, pos)
            and not cell_blocked(state, line, pos)
            and line in lines_reach(u["line"]))


def can_normal_attack(u: dict, tgt_line: str) -> bool:
    # Обычная атака (план п.1.1: воин бьёт первую линию врага) достаёт:
    #  - фронт противника (В1 для игрока / С1 для ИИ) — всегда, по плану;
    #  - ЛЮБУЮ линию в зоне досягаемости атакующего (своя или соседняя) —
    #    иначе враги, прорвавшиеся перемещением на ваши линии, были бы неуязвимы.
    return (tgt_line == row_of(other_side(u["side"]), "front")
            or abs(ROW_ORDER[u["line"]] - ROW_ORDER[tgt_line]) <= 1)


def can_stealth_attack(u: dict, tgt_line: str) -> bool:
    # Скрытая атака — только по тыловой линии противника (В2 / С2)
    return tgt_line == row_of(other_side(u["side"]), "rear")


def find_blocker(state: dict, def_side: str, target_pos: int):
    # «Заслон» для скрытой атаки (план п.3.4): юнит ЗАЩИЩАЮЩЕЙСЯ стороны
    # на её первой линии перед атакуемым (тот же номер клетки); если такой
    # клетки нет или она пуста/занята чужим — следующий по номеру
    # (циклически: после 3 перебор продолжается с 1).
    # СВОЯ СТЕНА (🧱 заклинание) тоже считается заслоном.
    front_line = row_of(def_side, "front")
    for i in range(3):
        p = (target_pos - 1 + i) % 3 + 1
        if p in row_layout(state, front_line):
            u = cell_unit(state, front_line, p)
            if u is not None and u["side"] == def_side:
                return u
            w = get_wall(state, front_line, p)
            if w is not None and w["owner"] == def_side:
                # Псевдо-юнит стены — совместим с eff_armor()
                return {"name": "🧱Стена", "armor": w["armor"], "end": 1,
                        "defending": False, "fx": []}
    return None


def log_add(state: dict, text: str):
    state["log"].append(text)
    # В админ-прогонах боёв журнал НЕ обрезается (no_trim) — там нужна
    # полная последовательность ходов; в живых боях храним последние 30.
    if not state.get("no_trim") and len(state["log"]) > 30:
        state["log"] = state["log"][-30:]


# ---------- эффекты заклинаний (баффы/дебаффы/стены/болото) ----------

def fx_sum(u: dict, kind: str) -> int:
    return sum(f["val"] for f in u.get("fx", []) if f["kind"] == kind)


def has_fx(u: dict, kind: str) -> bool:
    return any(f["kind"] == kind for f in u.get("fx", []))


def add_fx(u: dict, kind: str, val: int = 0, turns: int = None):
    # turns — сколько ХОДОВ ВЛАДЕЛЬЦА эффект активен. Внутри храним +1,
    # потому что тик происходит в начале хода владельца (begin_turn):
    # замороженный с turns=2 пропустит ровно один свой ход.
    u.setdefault("fx", []).append({"kind": kind, "val": val, "turns": turns})


def remove_fx(u: dict, kind: str):
    u["fx"] = [f for f in u.get("fx", []) if f["kind"] != kind]


def dec_fx(u: dict, kind: str, amount: int) -> int:
    # Уменьшает значение эффекта (щит); возвращает сколько списали
    left = amount
    for f in u.get("fx", []):
        if f["kind"] == kind and left > 0:
            take = min(f["val"], left)
            f["val"] -= take
            left -= take
    u["fx"] = [f for f in u.get("fx", []) if f["kind"] != kind or f["val"] > 0]
    return amount - left


def tick_fx(u: dict):
    # Тик длительностей в начале хода владельца
    kept = []
    for f in u.get("fx", []):
        if f["turns"] is None:
            kept.append(f)
            continue
        f["turns"] -= 1
        if f["turns"] > 0:
            kept.append(f)
    u["fx"] = kept


def cell_fx_at(state: dict, line: str, pos: int):
    return state.get("cell_fx", {}).get(line, {}).get(str(pos))


def get_wall(state: dict, line: str, pos: int):
    fx = cell_fx_at(state, line, pos)
    return fx if fx and fx["kind"] == "wall" else None


def cell_blocked(state: dict, line: str, pos: int) -> bool:
    # Клетка занята юнитом ИЛИ стеной (болото не блокирует — в него можно войти)
    return cell_unit(state, line, pos) is not None or get_wall(state, line, pos) is not None


def wall_aura(state: dict, u: dict) -> bool:
    # Аура стены: +3 КЗ союзникам на тыловой линии, пока на их передней
    # линии стоит СВОЯ стена (док. «ж», п.1)
    if u["line"] != row_of(u["side"], "rear"):
        return False
    front = row_of(u["side"], "front")
    for pos_str, fx in state.get("cell_fx", {}).get(front, {}).items():
        if fx["kind"] == "wall" and fx["owner"] == u["side"]:
            return True
    return False


# ---------- статы и формулы (план п.3) ----------

def eff_armor(u: dict, state: dict = None) -> int:
    # Эффективный КЗ защищающегося:
    #  - выносливость 0 или ❄️ заморозка → КЗ равен нулю;
    #  - баффы/дебаффы КЗ от заклинаний, болото (−2), аура стены (+3);
    #  - действие «Защита» → КЗ +50% (округление вниз, план п.3.1).
    if u["end"] <= 0 or has_fx(u, "frozen"):
        return 0
    a = u["armor"] + fx_sum(u, "buff_armor") - fx_sum(u, "debuff_armor")
    if state is not None and u.get("line"):
        cfx = cell_fx_at(state, u["line"], u["pos"])
        if cfx and cfx["kind"] == "swamp":
            a -= 2                       # болото: −2 КЗ стоящему на клетке
        if wall_aura(state, u):
            a += 3                       # стена: +3 КЗ тыловой линии
    if u["defending"]:
        a = a * 3 // 2
    return max(0, a)


def atk_eff(u: dict, state: dict = None) -> int:
    # Эффективная Атака (АА) с учётом заклинаний и болота (−25%)
    a = u["attack"] + fx_sum(u, "buff_attack") - fx_sum(u, "debuff_attack_flat")
    pct = fx_sum(u, "debuff_attack_pct")
    if state is not None and u.get("line"):
        cfx = cell_fx_at(state, u["line"], u["pos"])
        if cfx and cfx["kind"] == "swamp":
            pct += 25
    a = a * (100 - min(90, pct)) // 100
    return max(0, a)


def attack_roll(unit: dict, advantage: int = 0):
    # Попадание Атаки (ПА) = d20 + 3 + 2×уровень  (+2 при преимуществе, план п.3.7)
    roll = d20()
    return roll, roll + 3 + 2 * unit["level"] + advantage


def stealth_roll(unit: dict):
    # Попадание Скрытой Атаки (ПСА) = d20 − 3 + уровень + 2×(скрытность|точность)
    roll = d20()
    return roll, roll - 3 + unit["level"] + 2 * unit["sub"]


def gain_xp(state: dict, u: dict, amount: int):
    u["xp_gained"] += amount
    if u["side"] != "player" or u.get("card_id") is None:
        # Карты противника и призванные големы временные —
        # у них рост применяется автоматически (очки игроку не копим)
        u["xp"] += amount
        while u["xp"] >= xp_to_next(u["level"]):
            u["xp"] -= xp_to_next(u["level"])
            apply_level_growth(u)
            u["armor_base"] += 1
        return
    u["xp"] += amount
    while u["xp"] >= xp_to_next(u["level"]):
        u["xp"] -= xp_to_next(u["level"])
        # По желанию заказчика рост с уровнем выбирает ИГРОК: уровень и
        # его вклад в попадание (ПА = d20+3+2×уровень) растут сразу,
        # а очко статы ждёт распределения через кнопку «🌟 Уровень!».
        u["level"] += 1
        u["pending_levels"] = u.get("pending_levels", 0) + 1
        log_add(state, f"🌟 {u['name']} {u['surname']} получает уровень {u['level']}! "
                       f"Распределите очки роста (кнопка 🌟).")


# Возможные варианты роста на выбор игрока (по одному очку за уровень):
# общие для всех + классовая стата (скрытность/точность; у мага ещё манна).
STAT_CHOICE_LABEL = {
    "attack": "⚔️ Атака (АА) +1",
    "armor": "🛡 Защита (КЗ) +1",
    "hp": "❤️ Макс. здоровье +2 (и вылечить 2)",
    "end": "⚡ Макс. выносливость +1 (и восстановить 1)",
    "mana": "🔮 Макс. манна +1 (и восстановить 1)",
}


def stat_choices_for(u: dict):
    if u["cls"] == CLS_WARRIOR:
        return ["attack", "armor", "hp", "end", "sub"]
    return ["attack", "armor", "hp", "end", "sub", "mana"]


def sub_choice_label(u: dict) -> str:
    return "🥷 Скрытность +1" if u["cls"] == CLS_WARRIOR else "🎯 Точность +1"


def apply_stat_choice(u: dict, stat: str):
    # Применение одного очка роста, выбранного игроком
    u["pending_levels"] = max(0, u.get("pending_levels", 1) - 1)
    if stat == "attack":
        u["attack"] += 1
    elif stat == "armor":
        u["armor"] += 1
        u["armor_base"] += 1
    elif stat == "hp":
        u["hp_max"] += 2
        u["hp"] = min(u["hp_max"], u["hp"] + 2)
    elif stat == "end":
        u["end_max"] += 1
        u["end"] = min(u["end_max"], u["end"] + 1)
    elif stat == "sub":
        u["sub"] += 1
    elif stat == "mana":
        u["mana_max"] += 1
        u["mana"] = min(u["mana_max"], u["mana"] + 1)


def pending_levels_total(state: dict) -> int:
    # Сколько нераспределённых очков роста у карт игрока.
    # Считаем и погибшие в этом бою: карта не исчезает навсегда,
    # и её честно заработанные уровни не должны теряться.
    # Призванные големы (card_id=None) не считаются — они исчезают после боя.
    return sum(u.get("pending_levels", 0) for u in all_units(state, "player")
               if u.get("card_id") is not None)


def kill_unit(state: dict, u: dict):
    line, pos = u["line"], u["pos"]
    del state["board"][line][str(pos)]
    u["zone"] = "graveyard"
    u["line"] = None
    u["pos"] = None
    state[u["side"]]["graveyard"].append(u)


def resolve_attack(state: dict, atk: dict, tgt_line: str, tgt_pos: int,
                   advantage: int = 0, stealth: bool = False,
                   pierce: bool = False, coef_pct: int = 100, cap: int = 2):
    # Ядро боя: разрешение удара по конкретной клетке.
    # pierce — магическое пробитие: КЗ цели считается как eff_armor//2.
    # coef_pct — коэффициент урона в % (шквал 50, метеор 150...).
    # cap — потолок множителя (для заклинаний = 2, «кнопки победы» нет).
    label = ROW_LABEL[tgt_line]
    who = side_name(atk["side"])
    # БАЛАНС МАГИИ (док. «ж»): физическая атака вариативна (множитель 0–2),
    # а заклинания дают СТАБИЛЬНЫЙ множитель ×1, но пробивают половину КЗ.
    # Итог: против «толстяков» с высоким КЗ (обычная атака бьёт на 0) магия
    # незаменима, а против равных — не превосходит обычные атаки и не
    # является «кнопкой победы» (проверяется в test_spells.py).
    if pierce:
        cap = 1

    wall = get_wall(state, tgt_line, tgt_pos)
    tgt = cell_unit(state, tgt_line, tgt_pos)
    if tgt is None:
        if wall is None:
            return  # пустые клетки больше не «атакуются» — на них перемещаются
        # Удар по СТЕНЕ: у стены свои HP и КЗ, она разрушаема
        roll, pa = attack_roll(atk, advantage)
        mult = min(cap, max(0, (10 + pa - wall["armor"]) // 10))
        dmg = max(0, atk_eff(atk, state) * coef_pct // 100 * mult)
        wall["hp"] -= dmg
        log_add(state, f"🧱 Удар по стене ({label}:{tgt_pos}): d20={roll}, ПА={pa} "
                       f"против КЗ стены {wall['armor']} → урон {dmg}, у стены HP {max(0, wall['hp'])}")
        if wall["hp"] <= 0:
            del state["cell_fx"][tgt_line][str(tgt_pos)]
            log_add(state, f"🧱 Стена на {label}:{tgt_pos} разрушена!")
        return

    if stealth:
        # СКРЫТАЯ АТАКА (план п.3.4) — удар через первую линию по тылу.
        # Исправленная формула: УРОН = АА × (10 + (ПСА − ПЗ1//2 − ПЗ2)) // 10.
        blocker = find_blocker(state, tgt["side"], tgt_pos)
        pz1 = eff_armor(blocker, state) if blocker else 0
        roll, psa = stealth_roll(atk)
        kz2 = eff_armor(tgt, state)
        mult = (10 + psa - pz1 // 2 - kz2) // 10
        dmg = max(0, atk_eff(atk, state) * coef_pct // 100 * mult)
        bl_txt = f"{blocker['name']} КЗ={pz1}" if blocker else "заслона нет"
        log_add(state, f"🗡 {who}: {atk['name']} → {tgt['name']} СКРЫТАЯ атака ({label}:{tgt_pos}): "
                       f"d20={roll}, ПСА={psa}, заслон ({bl_txt})//2, КЗ цели={kz2} "
                       f"→ урон {atk_eff(atk, state)}×{mult} = {dmg}")
    else:
        # ОБЫЧНАЯ АТАКА (план п.3.3): УРОН = АА × (10 + (ПА − КЗ)) // 10.
        roll, pa = attack_roll(atk, advantage)
        kz = eff_armor(tgt, state)
        if pierce:
            kz = kz // 2   # магия пробивает половину КЗ
        mult = min(cap, max(0, (10 + pa - kz) // 10))
        dmg = max(0, atk_eff(atk, state) * coef_pct // 100 * mult)
        adv_txt = " с преимуществом (+2 к ПА)" if advantage else ""
        pierce_txt = " (пробитие: КЗ//2)" if pierce else ""
        log_add(state, f"⚔️ {who}: {atk['name']} → {tgt['name']} ({label}:{tgt_pos}){adv_txt}{pierce_txt}: "
                       f"d20={roll}, ПА={pa} против КЗ={kz} "
                       f"→ урон {atk_eff(atk, state)}×{mult} = {dmg}")

    # Магический щит поглощает урон первым
    shield = fx_sum(tgt, "shield")
    if shield > 0 and dmg > 0:
        absorbed = dec_fx(tgt, "shield", dmg)
        dmg -= absorbed
        if absorbed:
            log_add(state, f"🛡 Магический щит {tgt['name']} поглотил {absorbed} урона.")

    tgt["hp"] -= dmg
    # План п.4.2: защита активна лишь для ПЕРВОГО атакующего
    if tgt["defending"]:
        tgt["defending"] = False
        log_add(state, f"🛡 Защита {tgt['name']} поглотила первый удар и рассеялась.")
    # Шипы: атакующий получает фиксированный ответный урон
    th = fx_sum(tgt, "thorns")
    if th > 0 and tgt["side"] != atk["side"]:
        atk["hp"] -= th
        log_add(state, f"🌵 Шипы {tgt['name']} жалят {atk['name']} на {th}!")
        if atk["hp"] <= 0:
            atk["hp"] = 0
            kill_unit(state, atk)
            log_add(state, f"💀 {atk['name']} {atk['surname']} погиб от шипов!")
    if tgt["hp"] <= 0:
        tgt["hp"] = 0
        kill_unit(state, tgt)
        log_add(state, f"💀 {tgt['name']} {tgt['surname']} уничтожен! "
                       f"(карта вернётся в коллекцию после боя)")
        gain_xp(state, atk, KILL_XP_PER_LEVEL * tgt["level"])


# ---------- действия (ДП) и действие игрока (ДИ) ----------

def perform_attack(state: dict, side: str, u: dict, tgt_line: str, tgt_pos: int,
                   stealth: bool, free: bool = False):
    # free=True — только для автоматической атаки при появлении (план п.3.7):
    # она не тратит ни выносливость, ни манну, ни отдельное ДИ.
    advantage = 0
    if not free:
        u["end"] -= 1  # выносливость тратится на каждое ДП (план п.3.6)
        if u["cls"] == CLS_MAGE:
            u["mana"] -= MAGE_STEALTH_MANA if stealth else MAGE_ATTACK_MANA
        u["acted_this_turn"] = True
        # Эффект «Ускорения»: следующая атака с преимуществом +2 — тратится
        if has_fx(u, "adv_next"):
            advantage += fx_sum(u, "adv_next")
            remove_fx(u, "adv_next")
    resolve_attack(state, u, tgt_line, tgt_pos, advantage=advantage, stealth=stealth)


def perform_move(state: dict, side: str, u: dict, line: str, pos: int) -> bool:
    # ЗАХВАТ КЛЕТКИ (правка заказчика): персонаж просто ПЕРЕМЕЩАЕТСЯ
    # на пустую клетку своей или соседней линии. Стоит 1 выносливость
    # (это ДП), но НЕ стоит манны — перемещение не заклинание.
    if not can_move_to(state, u, line, pos):
        return False
    old_label = f"{ROW_LABEL[u['line']]}:{u['pos']}"
    del state["board"][u["line"]][str(u["pos"])]
    state["board"][line][str(pos)] = u
    u["line"], u["pos"] = line, pos
    u["end"] -= 1
    u["acted_this_turn"] = True
    u["defending"] = False  # снялся с позиции — защита рассеялась
    log_add(state, f"🚩 {side_name(side)}: {u['name']} переместился {old_label} → "
                   f"{ROW_LABEL[line]}:{pos} (захват клетки).")
    return True


def perform_defend(state: dict, side: str, u: dict):
    # Действие «Защита»: КЗ +50% до первого удара по юниту или до начала
    # следующего хода его владельца (план п.3.1 и п.4.2).
    u["end"] -= 1
    u["defending"] = True
    u["acted_this_turn"] = True
    log_add(state, f"🛡 {side_name(side)}: {u['name']} встал в защитную стойку "
                   f"(КЗ {u['armor']} → {eff_armor(u)}).")


def perform_retreat(state: dict, side: str, u: dict):
    # Действие «Отход»: карта возвращается в колоду, её защита падает
    # на 50% (план п.3.1). Штраф действует до конца боя (после боя КЗ
    # восстанавливается из armor_base).
    line, pos = u["line"], u["pos"]
    del state["board"][line][str(pos)]
    u["end"] -= 1
    u["armor"] = u["armor"] // 2
    u["zone"] = "deck"
    u["line"] = None
    u["pos"] = None
    u["defending"] = False
    u["acted_this_turn"] = True
    state[side]["deck"].append(u)
    log_add(state, f"↩️ {side_name(side)}: {u['name']} отошёл в колоду "
                   f"(КЗ до конца боя снижен до {u['armor']}).")


def perform_place(state: dict, side: str, line: str, pos: int, u: dict):
    # Вытаскивание карты из колоды на свою клетку — это ДИ игрока, сама
    # карта действие не совершает, поэтому выносливость НЕ тратится.
    # Но карта ПОМЕЧАЕТСЯ как «действовавшая в этот ход» — поэтому в
    # первом раунде атака при появлении ни у кого не сработает.
    state[side]["deck"].remove(u)
    state["board"][line][str(pos)] = u
    u["zone"] = "field"
    u["line"] = line
    u["pos"] = pos
    u["defending"] = False
    u["acted_this_turn"] = True
    log_add(state, f"🎴 {side_name(side)}: {u['name']} {u['surname']} "
                   f"вышел из колоды на {ROW_LABEL[line]}:{pos}.")

    # АТАКА ПРИ ПОЯВЛЕНИИ (план п.3.7 + правка заказчика «только между
    # раундами»): воин, появляясь на поле, автоматически бьёт с
    # преимуществом (+2 к ПА) врага на передней линии противника — но
    # ТОЛЬКО если этот враг не совершал действий в свой прошлый ход
    # (acted_this_turn=False). Цель — клетка прямо напротив (4−pos),
    # а если она пуста/не существует — ближайшая подходящая.
    if u["cls"] == CLS_WARRIOR:
        opp = other_side(side)
        front_line = row_of(opp, "front")
        idle = []
        for p in row_layout(state, front_line):
            t = cell_unit(state, front_line, p)
            if t is not None and t["side"] == opp and not t["acted_this_turn"]:
                idle.append((p, t))
        if idle:
            m = mirror_pos(pos)
            target = None
            for p, t in idle:
                if p == m:
                    target = (p, t)
                    break
            if target is None:
                target = min(idle, key=lambda pt: (abs(pos + pt[0] - 4), pt[0]))
            log_add(state, f"✨ Атака при появлении! {u['name']} застигает "
                           f"{target[1]['name']} врасплох (та не действовала в прошлый "
                           f"ход) — преимущество +2 к ПА.")
            perform_attack(state, side, u, front_line, target[0], stealth=False, free=True)


# ---------- ходы, победа/поражение ----------

def begin_turn(state: dict, side: str):
    # Начало хода стороны:
    #  - карты в колоде восстанавливают +1 выносливости и +1 манны (план п.3.6/3.7);
    #  - незадействованные защиты истекают (план п.4.2);
    #  - сбрасывается флаг «действовал в этот ход» у всех юнитов стороны —
    #    именно его проверяет атака при появлении на СЛЕДУЮЩИЙ ход;
    #  - тикают длительности эффектов заклинаний (юнитов и клеток линии);
    #  - УСТАЛОСТЬ (анти-столл, как в Hearthstone): начиная с FATIGUE_ROUND
    #    юниты на поле теряют HP, урон растёт каждые 10 раундов — бой
    #    гарантированно завершается и не может длиться вечно «хилами».
    for u in state[side]["deck"]:
        if u["end"] < u["end_max"]:
            u["end"] += 1
        if u["mana"] < u["mana_max"]:
            u["mana"] += 1
    for _, _, u in board_units(state, side):
        u["defending"] = False
        u["acted_this_turn"] = False
        tick_fx(u)
    for line in side_rows(side):
        cellfx = state.get("cell_fx", {}).get(line, {})
        for pos_str in list(cellfx.keys()):
            fx = cellfx[pos_str]
            if fx.get("turns") is None:
                continue  # стены живут, пока их не сломают
            fx["turns"] -= 1
            if fx["turns"] <= 0:
                del cellfx[pos_str]
                log_add(state, f"⌛ Эффект на клетке {ROW_LABEL[line]}:{pos_str} рассеялся.")
    if state["round"] >= FATIGUE_ROUND:
        fdmg = (state["round"] - FATIGUE_ROUND) // 10 + 1
        if state["round"] % 10 == FATIGUE_ROUND % 10:
            log_add(state, f"😮‍💨 УСТАЛОСТЬ: бой затянулся (раунд {state['round']}) — "
                           f"все на поле теряют {fdmg} HP за ход и дальше только больше.")
        for _, _, u in list(board_units(state, side)):
            u["hp"] -= fdmg
            if u["hp"] <= 0:
                u["hp"] = 0
                kill_unit(state, u)
                log_add(state, f"💀 {u['name']} {u['surname']} пал от усталости!")


def can_act(state: dict, side: str) -> bool:
    # Может ли сторона вообще что-то сделать в свой ход:
    # высадить карту из колоды ИЛИ совершить ДП любым юнитом с end>0.
    if state[side]["deck"] and own_empty_cells(state, side):
        return True
    return any(u["end"] > 0 for _, _, u in board_units(state, side))


def check_over(state: dict) -> bool:
    # Проверка завершения боя. Победа — когда у противника не осталось
    # живых карт (ни на доске, ни в колоде); поражение — зеркально;
    # ничья — когда обе стороны физически не могут действовать.
    if state["over"]:
        return True
    ap = alive_count(state, "player")
    ae = alive_count(state, "enemy")
    result = None
    if ap == 0 and ae == 0:
        result = "draw"
    elif ae == 0:
        result = "victory"
    elif ap == 0:
        result = "defeat"
    elif not can_act(state, "player") and not can_act(state, "enemy"):
        result = "draw"
    if result:
        state["over"] = True
        state["result"] = result
        msg = {
            "victory": "🏆 ПОБЕДА! Все карты противника уничтожены.",
            "defeat": "☠️ ПОРАЖЕНИЕ... Все ваши карты уничтожены.",
            "draw": "🤝 НИЧЬЯ — ни одна из сторон не может действовать.",
        }[result]
        log_add(state, msg)
    return state["over"]


def advance_turn(state: dict):
    # Вызывается после действия игрока: ход переходит противнику,
    # за него сразу играет ИИ (бой в реальном времени, без ожидания),
    # затем ход возвращается игроку и начинается новый раунд.
    if check_over(state):
        return
    state["turn"] = "enemy"
    begin_turn(state, "enemy")
    if not check_over(state):
        ai_act(state, "enemy")
        check_over(state)
    if state["over"]:
        return
    state["round"] += 1
    state["turn"] = "player"
    begin_turn(state, "player")
    check_over(state)


# =====================================================================
# ЛЕГЕНДАРНАЯ МАГИЯ — 40 заклинаний (концепция: docs/zh_legendary_magic.md)
#
# Правила (по заданию заказчика):
#  • кастуют ТОЛЬКО заклинатели (маги), стоящие на поле;
#  • каст тратит ВЕСЬ пул манны (drain="all") или почти весь ("all_but_1"
#    — оставляет 1🔮); после каста маг почти пуст и должен уйти в колоду
#    восстанавливаться — это естественный кулдаун;
#  • заклинания ОТКРЫВАЮТСЯ ПО УРОВНЯМ мага (lvl) и его РЕДКОСТИ (rarity);
#  • каст — это ДП: −1⚡ выносливости и всё действие игрока (ДИ) на ход;
#  • замороженный/пойманный «временной петлёй» маг кастовать не может.
#
# Баланс («чтобы не было кнопки победы»):
#  • множитель урона заклинаний ограничен cap=2 (как у обычной атаки);
#  • магический урон бьёт по КЗ//2 (пробитие), поэтому коэффициенты скромные;
#  • весь контроль — через спасбросок d20+уровень цели против DC;
#  • заморозка не работает по целям с эффективным КЗ ≥ 20;
#  • стоимость пула = 4–6 обычных атак мага → заклинание должно быть
#    выгоднее примерно во столько же раз, цифры подобраны аналитикой
#    (см. docs/zh_legendary_magic.md) и fuzz-тестами (test_spells.py).
# =====================================================================

# target — что выбирает игрок после каста:
#   enemy_unit / ally_unit        — клик по юниту на карте
#   enemy_cell / own_front_empty / own_empty — клик по клетке
#   enemy_line / ally_line        — выбор линии (кнопки В1/В2 или С1/С2)
#   all_enemies / all_allies / all_enemy_cells — мгновенно (с подтверждением)
#   ally_grave                    — выбор погибшей карты (кнопкой)
SPELLS = {
    # ---------- ⬜ ОБЫЧНЫЕ (доступны всем магам с низких уровней) ----------
    "s_ember":   {"name": "Огненная искра", "emoji": "🔥", "rarity": "common", "lvl": 1,
                  "min_mana": 2, "drain": "all_but_1", "target": "enemy_unit",
                  "eff": ("damage", 0, 100),
                  "desc": "Магический урон одной цели (пробивает половину КЗ)."},
    "s_mend":    {"name": "Малое лечение", "emoji": "💚", "rarity": "common", "lvl": 1,
                  "min_mana": 2, "drain": "all_but_1", "target": "ally_unit",
                  "eff": ("heal", 100),
                  "desc": "Лечит союзника на величину вашей Атаки (магия — единственный способ лечить в бою)."},
    "s_spark":   {"name": "Манная искра", "emoji": "🔵", "rarity": "common", "lvl": 2,
                  "min_mana": 2, "drain": "all", "target": "ally_unit",
                  "eff": ("restore_mana", 2),
                  "desc": "Восстанавливает союзнику 2🔮 манны."},
    "s_vigor":   {"name": "Прилив сил", "emoji": "⚡", "rarity": "common", "lvl": 2,
                  "min_mana": 2, "drain": "all", "target": "ally_unit",
                  "eff": ("restore_end", 2),
                  "desc": "Восстанавливает союзнику 2⚡ выносливости."},
    "s_hex":     {"name": "Ослабляющий сглаз", "emoji": "🌀", "rarity": "common", "lvl": 3,
                  "min_mana": 3, "drain": "all", "target": "enemy_unit",
                  "eff": ("debuff_attack_pct", 25, 2),
                  "desc": "−25% к Атаке цели на 2 её хода."},
    "s_shield1": {"name": "Малый щит", "emoji": "🛡", "rarity": "common", "lvl": 3,
                  "min_mana": 3, "drain": "all_but_1", "target": "ally_unit",
                  "eff": ("shield", 150, 3),
                  "desc": "Щит, поглощающий урон на 1.5× вашей Атаки (3 хода)."},

    # ---------- 🟩 НЕОБЫЧНЫЕ ----------
    "s_flame":   {"name": "Огненная струя", "emoji": "🔥", "rarity": "uncommon", "lvl": 3,
                  "min_mana": 3, "drain": "all", "target": "enemy_unit",
                  "eff": ("damage", 2, 100),
                  "desc": "Урон одной цели с преимуществом +2 к ПА."},
    "s_stone":   {"name": "Каменная кожа", "emoji": "🗿", "rarity": "uncommon", "lvl": 3,
                  "min_mana": 3, "drain": "all_but_1", "target": "ally_unit",
                  "eff": ("buff_armor", 3, 3),
                  "desc": "+3 КЗ союзнику на 3 его хода."},
    "s_sharp":   {"name": "Заточка клинков", "emoji": "⚔️", "rarity": "uncommon", "lvl": 4,
                  "min_mana": 3, "drain": "all", "target": "ally_unit",
                  "eff": ("buff_attack", 2, 3),
                  "desc": "+2 к Атаке союзника на 3 его хода."},
    "s_drain":   {"name": "Вытягивание", "emoji": "💜", "rarity": "uncommon", "lvl": 4,
                  "min_mana": 4, "drain": "all", "target": "enemy_unit",
                  "eff": ("drain", 2),
                  "desc": "Вытягивает 2🔮 у вражеского мага или 2⚡ у воина."},
    "s_cleanse": {"name": "Очищение", "emoji": "✨", "rarity": "uncommon", "lvl": 4,
                  "min_mana": 3, "drain": "all_but_1", "target": "ally_unit",
                  "eff": ("cleanse",),
                  "desc": "Снимает с союзника все дебаффы (включая ❄️ заморозку и ⏳ петлю)."},
    "s_thorns":  {"name": "Шипы", "emoji": "🌵", "rarity": "uncommon", "lvl": 5,
                  "min_mana": 4, "drain": "all", "target": "ally_unit",
                  "eff": ("thorns", 3, 3),
                  "desc": "Атакующие союзника получают 3 урона (3 хода)."},

    # ---------- 🟦 РЕДКИЕ ----------
    "s_fireball": {"name": "Огненный шар", "emoji": "🔥", "rarity": "rare", "lvl": 5,
                   "min_mana": 4, "drain": "all", "target": "enemy_unit",
                   "eff": ("damage", 4, 80),
                   "desc": "Урон ×0.8 одной цели (+4 к ПА, пробитие) — ответ на толстяков с высоким КЗ."},
    "s_swamp":   {"name": "Болото", "emoji": "🟢", "rarity": "rare", "lvl": 5,
                  "min_mana": 4, "drain": "all", "target": "enemy_cell",
                  "eff": ("swamp", 3),
                  "desc": "Клетка врага становится болотом на 3 хода: стоящий на ней теряет 25% Атаки и 2 КЗ. Уход с клетки снимает эффект."},
    "s_wall":    {"name": "Каменная стена", "emoji": "🧱", "rarity": "rare", "lvl": 5,
                  "min_mana": 4, "drain": "all", "target": "own_front_empty",
                  "eff": ("wall", 10, 12),
                  "desc": "Ставит стену (10 HP, КЗ12) на свою переднюю линию: +3 КЗ вашему тылу, заслон от скрытых атак. Разрушаема."},
    "s_freeze":  {"name": "Морозное касание", "emoji": "❄️", "rarity": "rare", "lvl": 6,
                  "min_mana": 5, "drain": "all", "target": "enemy_unit",
                  "eff": ("freeze", 12),
                  "desc": "Цель не может атаковать/кастовать, её КЗ=0 (1 ход). Спасбросок d20+уровень ≥ 12; иммунитет при КЗ ≥ 20."},
    "s_nova":    {"name": "Морозная нова", "emoji": "❄️", "rarity": "rare", "lvl": 7,
                  "min_mana": 5, "drain": "all", "target": "all_enemies",
                  "eff": ("slow_all", 11, 2, 25, 2),
                  "desc": "По всем врагам: спасбросок ≥ 11, иначе −2⚡ и −25% Атаки на 2 хода."},
    "s_bless":   {"name": "Боевое благословение", "emoji": "🌟", "rarity": "rare", "lvl": 6,
                  "min_mana": 4, "drain": "all_but_1", "target": "ally_line",
                  "eff": ("buff_pair", 1, 1, 3),
                  "desc": "+1 Атака и +1 КЗ всей своей линии на 3 хода."},
    "s_quake":   {"name": "Землетрясение", "emoji": "🌋", "rarity": "rare", "lvl": 7,
                  "min_mana": 5, "drain": "all", "target": "enemy_line",
                  "eff": ("quake", 0, 1, 100),
                  "desc": "Урон ×0.7 (пробитие) всем врагам линии и −1⚡ каждому."},
    "s_haste":   {"name": "Ускорение", "emoji": "💨", "rarity": "rare", "lvl": 6,
                  "min_mana": 4, "drain": "all", "target": "ally_unit",
                  "eff": ("haste",),
                  "desc": "Союзник: выносливость до максимума, следующая атака с преимуществом +2."},
    "s_revive":  {"name": "Возрождение", "emoji": "🕯", "rarity": "rare", "lvl": 8,
                  "min_mana": 5, "drain": "all", "target": "ally_grave",
                  "eff": ("revive", 3),
                  "desc": "Погибшая карта возвращается в колоду с ⅓ здоровья."},
    "s_curse":   {"name": "Проклятие гниения", "emoji": "🦠", "rarity": "rare", "lvl": 7,
                  "min_mana": 5, "drain": "all", "target": "enemy_unit",
                  "eff": ("curse", 3, 25, 3),
                  "desc": "−3 КЗ и −25% Атаки цели на 3 хода."},

    # ---------- 🟪 ЭПИЧЕСКИЕ ----------
    "s_inferno": {"name": "Инферно", "emoji": "🔥", "rarity": "epic", "lvl": 8,
                  "min_mana": 5, "drain": "all", "target": "enemy_line",
                  "eff": ("damage_line", 2, 100),
                  "desc": "Урон ×0.7 (пробитие, +2 ПА) всем врагам выбранной линии."},
    "s_blizzard": {"name": "Снежная буря", "emoji": "❄️", "rarity": "epic", "lvl": 9,
                   "min_mana": 6, "drain": "all", "target": "enemy_line",
                   "eff": ("freeze_line", 13),
                   "desc": "Всем врагам линии: спасбросок ≥ 13, иначе заморозка на 1 ход."},
    "s_storm":   {"name": "Цепная молния", "emoji": "⚡", "rarity": "epic", "lvl": 9,
                  "min_mana": 5, "drain": "all", "target": "all_enemies",
                  "eff": ("storm", 3, 0, 80),
                  "desc": "Урон ×0.6 (пробитие) трём случайным врагам."},
    "s_fortress": {"name": "Крепость", "emoji": "🏰", "rarity": "epic", "lvl": 8,
                   "min_mana": 5, "drain": "all_but_1", "target": "all_allies",
                   "eff": ("fortress", 3, 100, 3),
                   "desc": "Всем союзникам: +3 КЗ и щит на 1× Атаки кастующего (3 хода)."},
    "s_plague":  {"name": "Мор", "emoji": "☠️", "rarity": "epic", "lvl": 9,
                  "min_mana": 6, "drain": "all", "target": "all_enemies",
                  "eff": ("plague", 25, 3, 1),
                  "desc": "Все враги: −25% Атаки на 3 хода и −1⚡ (без спасброска)."},
    "s_meteor":  {"name": "Метеоритный удар", "emoji": "☄️", "rarity": "epic", "lvl": 10,
                  "min_mana": 6, "drain": "all", "target": "enemy_cell",
                  "eff": ("damage_splash", 4, 100, 50),
                  "desc": "Клетка врага: урон ×0.7 (+4 ПА, пробитие), соседние клетки ×0.4."},
    "s_sanct":   {"name": "Святилище", "emoji": "🕊", "rarity": "epic", "lvl": 8,
                  "min_mana": 5, "drain": "all_but_1", "target": "ally_line",
                  "eff": ("heal_line_cleanse", 100),
                  "desc": "Лечит всю свою линию на 1× Атаки и снимает дебаффы."},
    "s_mirror":  {"name": "Зеркальный щит", "emoji": "🪞", "rarity": "epic", "lvl": 9,
                  "min_mana": 5, "drain": "all_but_1", "target": "ally_unit",
                  "eff": ("mirror", 300, 4, 4),
                  "desc": "Щит на 3× Атаки и Шипы 4 (4 хода)."},
    "s_time":    {"name": "Временная петля", "emoji": "⏳", "rarity": "epic", "lvl": 10,
                  "min_mana": 6, "drain": "all", "target": "enemy_unit",
                  "eff": ("time_lock", 14),
                  "desc": "Цель не может атаковать 1 ход (КЗ не меняется). Спасбросок ≥ 14. Отход/защита/перемещение остаются."},
    "s_golem":   {"name": "Каменный голем", "emoji": "🗿", "rarity": "epic", "lvl": 9,
                  "min_mana": 6, "drain": "all", "target": "own_empty",
                  "eff": ("summon_golem",),
                  "desc": "Призывает голема (воин, Атака = ваша−2, HP 8+2×уровень, КЗ 12+уровень) на свою пустую клетку. Голем исчезает после боя."},

    # ---------- 🟨 ЛЕГЕНДАРНЫЕ ----------
    "s_armageddon": {"name": "Армагеддон", "emoji": "☄️", "rarity": "legendary", "lvl": 12,
                     "min_mana": 7, "drain": "all", "target": "all_enemies",
                     "eff": ("armageddon", 2, 15, 100),
                     "desc": "Урон ×0.6 (пробитие, +2 ПА) ВСЕМ врагам; спасбросок ≥ 15 уменьшает урон вдвое."},
    "s_absolute": {"name": "Абсолютный ноль", "emoji": "❄️", "rarity": "legendary", "lvl": 12,
                   "min_mana": 7, "drain": "all", "target": "all_enemies",
                   "eff": ("absolute", 14, 3, 2),
                   "desc": "Всем врагам: спасбросок ≥ 14, иначе заморозка на 1 ход и −3 Атаки на 2 хода."},
    "s_ragnarok": {"name": "Рагнарёк", "emoji": "⚔️", "rarity": "legendary", "lvl": 11,
                   "min_mana": 7, "drain": "all", "target": "all_allies",
                   "eff": ("ragnarok", 3, 3, 2, 3),
                   "desc": "Всем союзникам: +3 Атаки, +3 КЗ (3 хода) и +2⚡ сразу."},
    "s_gaia":   {"name": "Благословение Геи", "emoji": "🌿", "rarity": "legendary", "lvl": 11,
                 "min_mana": 6, "drain": "all_but_1", "target": "all_allies",
                 "eff": ("gaia", 200, 200, 3),
                 "desc": "Всем союзникам: лечение 2× Атаки, очищение и щит 2× Атаки (3 хода)."},
    "s_void":   {"name": "Пустота", "emoji": "🌑", "rarity": "legendary", "lvl": 12,
                 "min_mana": 7, "drain": "all", "target": "enemy_unit",
                 "eff": ("execute", 100, 4, 80),
                 "desc": "Добивание: если у цели HP ≤ 1× вашей Атаки — уничтожает мгновенно; иначе сильный урон (+4 ПА, пробитие)."},
    "s_phoenix": {"name": "Феникс", "emoji": "🐦", "rarity": "legendary", "lvl": 11,
                  "min_mana": 7, "drain": "all", "target": "ally_grave",
                  "eff": ("phoenix", 2),
                  "desc": "Погибшая карта возвращается в колоду с половиной здоровья."},
    "s_apoc":   {"name": "Апокалипсис топей", "emoji": "🟢", "rarity": "legendary", "lvl": 12,
                 "min_mana": 7, "drain": "all", "target": "all_enemy_cells",
                 "eff": ("apoc_swamp", 3),
                 "desc": "ВСЕ клетки врага становятся болотом на 3 хода (−25% Атаки, −2 КЗ стоящим)."},
    "s_archangel": {"name": "Архангельский призыв", "emoji": "👼", "rarity": "legendary", "lvl": 13,
                    "min_mana": 7, "drain": "all_but_1", "target": "ally_line",
                    "eff": ("archangel", 300, 4, 4),
                    "desc": "Своя линия: полное лечение, очищение, щит 3× Атаки и Шипы 4 (4 хода)."},
}

# Какие эффекты считаются негативными (снимаются «Очищением»)
NEGATIVE_FX = {"debuff_armor", "debuff_attack_pct", "debuff_attack_flat",
               "frozen", "time_lock"}


def spell_available(u: dict, sp: dict) -> bool:
    # Может ли юнит прямо сейчас скастовать заклинание
    return (u["cls"] == CLS_MAGE
            and u["end"] > 0
            and u["level"] >= sp["lvl"]
            and rarity_rank(u.get("rarity", "common")) >= rarity_rank(sp["rarity"])
            and u["mana"] >= sp["min_mana"]
            and not has_fx(u, "frozen")
            and not has_fx(u, "time_lock"))


def spells_for_unit(u: dict):
    # Все заклинания, доступные юниту по уровню/редкости/манне
    return [sid for sid, sp in SPELLS.items() if spell_available(u, sp)]


def saving_throw(state: dict, u: dict, dc: int) -> bool:
    # Спасбросок от контроля: d20 + уровень ≥ DC
    roll = d20()
    ok = roll + u["level"] >= dc
    log_add(state, f"🎲 Спасбросок {u['name']}: d20={roll}+ур{u['level']} против {dc} → "
                   f"{'сопротивился' if ok else 'НЕ смог'}")
    return ok


def heal_unit(state: dict, u: dict, amount: int):
    if amount <= 0 or u["hp"] <= 0:
        return
    before = u["hp"]
    u["hp"] = min(u["hp_max"], u["hp"] + amount)
    if u["hp"] != before:
        log_add(state, f"💚 {u['name']} вылечен на {u['hp'] - before} (HP {u['hp']}/{u['hp_max']}).")


def cleanse_unit(state: dict, u: dict):
    bad = [f["kind"] for f in u.get("fx", []) if f["kind"] in NEGATIVE_FX]
    if bad:
        u["fx"] = [f for f in u["fx"] if f["kind"] not in NEGATIVE_FX]
        log_add(state, f"✨ С {u['name']} сняты эффекты: {', '.join(bad)}.")


def try_freeze(state: dict, caster_side: str, u: dict, dc: int):
    # Заморозка с предохранителями: иммунитет КЗ≥20 и спасбросок
    if eff_armor(u, state) >= 20:
        log_add(state, f"❄️ Заморозка не пробивает броню {u['name']} (КЗ ≥ 20).")
        return
    if saving_throw(state, u, dc):
        return
    add_fx(u, "frozen", 0, 2)  # пропустит ровно один свой ход
    log_add(state, f"❄️ {u['name']} ЗАМОРОЖЕН: не может атаковать, КЗ=0 (1 ход)!")


def summon_golem(state: dict, side: str, caster: dict, line: str, pos: int):
    d = {
        "name": random.choice(RACES["Голем"]["names"]),
        "surname": random.choice(RACES["Голем"]["surnames"]),
        "race": "Голем", "cls": CLS_WARRIOR, "rarity": caster.get("rarity", "common"),
        "level": caster["level"], "exp": 0,
        "attack": max(3, atk_eff(caster, state) - 2),
        "armor": 12 + caster["level"],
        "hp_max": 8 + 2 * caster["level"],
        "sub": 0, "mana_max": 0, "end_max": 3,
    }
    g = make_unit(d, side, state["next_uid"])
    state["next_uid"] += 1
    g["zone"] = "field"
    g["line"], g["pos"] = line, pos
    g["acted_this_turn"] = True  # свежий голем не даёт врагу бонус «между раундами»
    state["board"][line][str(pos)] = g
    log_add(state, f"🗿 {caster['name']} призывает голема {g['name']} на {ROW_LABEL[line]}:{pos}! "
                   f"(⚔{g['attack']} ❤{g['hp_max']} 🛡{g['armor']})")


def cast_spell(state: dict, side: str, caster: dict, sid: str, spec):
    # Розыгрыш заклинания: стоимость — всё ДИ, 1⚡ и весь (почти весь) пул манны
    sp = SPELLS[sid]
    if not spell_available(caster, sp):
        return False
    pool = caster["mana"]
    caster["end"] -= 1
    caster["acted_this_turn"] = True
    caster["mana"] = 1 if sp["drain"] == "all_but_1" else 0
    log_add(state, f"✨ {side_name(side)}: {caster['name']} кастует "
                   f"«{sp['emoji']} {sp['name']}» (слито {pool}🔮)!")
    apply_spell(state, side, caster, sp, spec)
    return True


def apply_spell(state: dict, side: str, caster: dict, sp: dict, spec):
    # Диспетчер эффектов. spec: {"kind":"unit"|"cell"|"line"|"grave", ...} или None.
    eff = sp["eff"]
    kind = eff[0]
    opp = other_side(side)

    def spec_unit():
        if not spec or spec.get("kind") not in ("unit", "grave"):
            return None
        return find_unit(state, spec["uid"])[1]

    def enemies():
        return list(board_units(state, opp))

    def allies():
        return list(board_units(state, side))

    # --- урон ---
    if kind == "damage":
        u = spec_unit()
        if u:
            resolve_attack(state, caster, u["line"], u["pos"],
                           advantage=eff[1], pierce=True, coef_pct=eff[2])
    elif kind == "damage_line":
        coef = eff[2] if len(eff) > 2 else 100
        for ln, ps, u in enemies():
            if ln == spec["line"]:
                resolve_attack(state, caster, ln, ps, advantage=eff[1], pierce=True, coef_pct=coef)
    elif kind == "quake":
        coef = eff[3] if len(eff) > 3 else 100
        for ln, ps, u in enemies():
            if ln == spec["line"]:
                resolve_attack(state, caster, ln, ps, advantage=eff[1], pierce=True, coef_pct=coef)
                if u["hp"] > 0:
                    u["end"] = max(0, u["end"] - eff[2])
    elif kind == "damage_splash":
        ln, ps = spec["line"], spec["pos"]
        if cell_unit(state, ln, ps) is not None:
            resolve_attack(state, caster, ln, ps, advantage=eff[1], pierce=True, coef_pct=eff[2])
        lay = row_layout(state, ln)
        near = sorted([q for q in lay if q != ps], key=lambda q: abs(q - ps))[:2]
        for q in near:
            if cell_unit(state, ln, q) is not None:
                resolve_attack(state, caster, ln, q, pierce=True, coef_pct=eff[3])
    elif kind == "storm":
        coef = eff[3] if len(eff) > 3 else 100
        units = enemies()
        random.shuffle(units)
        for ln, ps, u in units[:eff[1]]:
            resolve_attack(state, caster, ln, ps, advantage=eff[2], pierce=True, coef_pct=coef)
    elif kind == "armageddon":
        coef = eff[3] if len(eff) > 3 else 100
        for ln, ps, u in enemies():
            c = coef // 2 if saving_throw(state, u, eff[2]) else coef
            resolve_attack(state, caster, ln, ps, advantage=eff[1], pierce=True, coef_pct=c)
    elif kind == "execute":
        u = spec_unit()
        if u:
            threshold = atk_eff(caster, state) * eff[1] // 100
            if u["hp"] <= threshold:
                log_add(state, f"🌑 Пустота поглощает {u['name']} {u['surname']} без остатка!")
                u["hp"] = 0
                kill_unit(state, u)
                gain_xp(state, caster, KILL_XP_PER_LEVEL * u["level"])
            else:
                coef = eff[3] if len(eff) > 3 else 100
                resolve_attack(state, caster, u["line"], u["pos"], advantage=eff[2],
                               pierce=True, coef_pct=coef)

    # --- лечение ---
    elif kind == "heal":
        u = spec_unit()
        if u:
            heal_unit(state, u, atk_eff(caster, state) * eff[1] // 100)
    elif kind == "heal_line_cleanse":
        for ln, ps, u in allies():
            if ln == spec["line"]:
                heal_unit(state, u, atk_eff(caster, state) * eff[1] // 100)
                cleanse_unit(state, u)
    elif kind == "gaia":
        for _, _, u in allies():
            heal_unit(state, u, atk_eff(caster, state) * eff[1] // 100)
            cleanse_unit(state, u)
            add_fx(u, "shield", atk_eff(caster, state) * eff[2] // 100, eff[3] + 1)
    elif kind == "archangel":
        for ln, ps, u in allies():
            if ln == spec["line"]:
                heal_unit(state, u, u["hp_max"])   # полное лечение линии
                cleanse_unit(state, u)
                add_fx(u, "shield", atk_eff(caster, state) * eff[1] // 100, eff[3] + 1)
                add_fx(u, "thorns", eff[2], eff[3] + 1)

    # --- баффы/дебаффы ---
    elif kind == "buff_armor":
        u = spec_unit()
        if u:
            add_fx(u, "buff_armor", eff[1], eff[2] + 1)
            log_add(state, f"🗿 {u['name']}: +{eff[1]} КЗ на {eff[2]} хода.")
    elif kind == "buff_attack":
        u = spec_unit()
        if u:
            add_fx(u, "buff_attack", eff[1], eff[2] + 1)
            log_add(state, f"⚔️ {u['name']}: +{eff[1]} Атаки на {eff[2]} хода.")
    elif kind == "buff_pair":
        for ln, ps, u in allies():
            if ln == spec["line"]:
                add_fx(u, "buff_attack", eff[1], eff[3] + 1)
                add_fx(u, "buff_armor", eff[2], eff[3] + 1)
        log_add(state, f"🌟 Линия {ROW_LABEL[spec['line']]} благословлена: +{eff[1]}⚔ +{eff[2]}🛡.")
    elif kind == "fortress":
        for _, _, u in allies():
            add_fx(u, "buff_armor", eff[1], eff[3] + 1)
            add_fx(u, "shield", atk_eff(caster, state) * eff[2] // 100, eff[3] + 1)
        log_add(state, f"🏰 Крепость: всем союзникам +{eff[1]} КЗ и щит.")
    elif kind == "ragnarok":
        for _, _, u in allies():
            add_fx(u, "buff_attack", eff[1], eff[4] + 1)
            add_fx(u, "buff_armor", eff[2], eff[4] + 1)
            u["end"] += eff[3]
        log_add(state, f"⚔️ РАГНАРЁК: всем союзникам +{eff[1]}⚔ +{eff[2]}🛡 +{eff[3]}⚡!")
    elif kind == "debuff_attack_pct":
        u = spec_unit()
        if u:
            add_fx(u, "debuff_attack_pct", eff[1], eff[2] + 1)
            log_add(state, f"🌀 {u['name']} ослаблен: −{eff[1]}% Атаки на {eff[2]} хода.")
    elif kind == "curse":
        u = spec_unit()
        if u:
            add_fx(u, "debuff_armor", eff[1], eff[3] + 1)
            add_fx(u, "debuff_attack_pct", eff[2], eff[3] + 1)
            log_add(state, f"🦠 {u['name']} проклят: −{eff[1]} КЗ, −{eff[2]}% Атаки на {eff[3]} хода.")
    elif kind == "shield":
        u = spec_unit()
        if u:
            val = atk_eff(caster, state) * eff[1] // 100
            add_fx(u, "shield", val, eff[2] + 1)
            log_add(state, f"🛡 {u['name']} получает магический щит на {val}.")
    elif kind == "mirror":
        u = spec_unit()
        if u:
            add_fx(u, "shield", atk_eff(caster, state) * eff[1] // 100, eff[3] + 1)
            add_fx(u, "thorns", eff[2], eff[3] + 1)
            log_add(state, f"🪞 {u['name']} укрыт зеркальным щитом и шипами.")
    elif kind == "thorns":
        u = spec_unit()
        if u:
            add_fx(u, "thorns", eff[1], eff[2] + 1)
            log_add(state, f"🌵 {u['name']} покрыт шипами ({eff[1]} урона атакующим).")
    elif kind == "haste":
        u = spec_unit()
        if u:
            u["end"] = u["end_max"]
            add_fx(u, "adv_next", 2, 4)
            log_add(state, f"💨 {u['name']} ускорен: ⚡ восстановлена, следующая атака +2 ПА.")

    # --- контроль ---
    elif kind == "freeze":
        u = spec_unit()
        if u:
            try_freeze(state, side, u, eff[1])
    elif kind == "freeze_line":
        for ln, ps, u in enemies():
            if ln == spec["line"]:
                try_freeze(state, side, u, eff[1])
    elif kind == "time_lock":
        u = spec_unit()
        if u and not saving_throw(state, u, eff[1]):
            add_fx(u, "time_lock", 0, 2)
            log_add(state, f"⏳ {u['name']} пойман во временную петлю: атаки невозможны 1 ход.")
    elif kind == "slow_all":
        for _, _, u in enemies():
            if not saving_throw(state, u, eff[1]):
                u["end"] = max(0, u["end"] - eff[2])
                add_fx(u, "debuff_attack_pct", eff[3], eff[4] + 1)
        log_add(state, f"❄️ Морозная нова прокатилась по врагам.")
    elif kind == "absolute":
        for _, _, u in enemies():
            if not saving_throw(state, u, eff[1]):
                add_fx(u, "frozen", 0, 2)
                add_fx(u, "debuff_attack_flat", eff[2], eff[3] + 1)
        log_add(state, f"❄️ АБСОЛЮТНЫЙ НОЛЬ: поле сковала стужа!")
    elif kind == "plague":
        for _, _, u in enemies():
            add_fx(u, "debuff_attack_pct", eff[1], eff[2] + 1)
            u["end"] = max(0, u["end"] - eff[3])
        log_add(state, f"☠️ Мор охватил вражеские порядки: −{eff[1]}% Атаки, −{eff[3]}⚡.")

    # --- ресурсы ---
    elif kind == "restore_mana":
        u = spec_unit()
        if u:
            u["mana"] = min(u["mana_max"], u["mana"] + eff[1])
            log_add(state, f"🔵 {u['name']}: +{eff[1]}🔮.")
    elif kind == "restore_end":
        u = spec_unit()
        if u:
            u["end"] = min(u["end_max"], u["end"] + eff[1])
            log_add(state, f"⚡ {u['name']}: +{eff[1]}⚡.")
    elif kind == "drain":
        u = spec_unit()
        if u:
            if u["mana_max"] > 0 and u["mana"] > 0:
                got = min(u["mana"], eff[1])
                u["mana"] -= got
                log_add(state, f"💜 {caster['name']} вытягивает {got}🔮 из {u['name']}.")
            elif u["end"] > 0:
                got = min(u["end"], eff[1])
                u["end"] -= got
                log_add(state, f"💜 {caster['name']} вытягивает {got}⚡ из {u['name']}.")
    elif kind == "cleanse":
        u = spec_unit()
        if u:
            cleanse_unit(state, u)
            log_add(state, f"✨ {u['name']} очищен.")

    # --- клетки/местность ---
    elif kind == "wall":
        ln, ps = spec["line"], spec["pos"]
        state["cell_fx"][ln][str(ps)] = {"kind": "wall", "hp": eff[1], "armor": eff[2],
                                         "owner": side, "turns": None}
        log_add(state, f"🧱 Стена воздвигнута на {ROW_LABEL[ln]}:{ps} "
                       f"(HP {eff[1]}, КЗ {eff[2]}). Тыл получает +3 КЗ, пока она цела.")
    elif kind == "swamp":
        ln, ps = spec["line"], spec["pos"]
        state["cell_fx"][ln][str(ps)] = {"kind": "swamp", "turns": eff[1] + 1, "owner": side}
        log_add(state, f"🟢 Болото разлилось на {ROW_LABEL[ln]}:{ps} ({eff[1]} хода): "
                       f"−25% Атаки, −2 КЗ стоящим на нём.")
    elif kind == "apoc_swamp":
        n = 0
        for ln in side_rows(opp):
            for ps in row_layout(state, ln):
                cur = state["cell_fx"][ln].get(str(ps))
                if cur and cur["kind"] == "wall":
                    continue  # стену не затопить
                state["cell_fx"][ln][str(ps)] = {"kind": "swamp", "turns": eff[1] + 1, "owner": side}
                n += 1
        log_add(state, f"🟢 АПОКАЛИПСИС ТОПЕЙ: {n} вражеских клеток затоплено болотом!")

    # --- призыв/возвращение ---
    elif kind == "summon_golem":
        summon_golem(state, side, caster, spec["line"], spec["pos"])
    elif kind in ("revive", "phoenix"):
        u = spec_unit()
        if u and u in state[side]["graveyard"]:
            state[side]["graveyard"].remove(u)
            u["hp"] = max(1, u["hp_max"] // eff[1])
            u["zone"] = "deck"
            u["fx"] = []
            u["defending"] = False
            state[side]["deck"].append(u)
            log_add(state, f"{'🐦 Феникс' if kind == 'phoenix' else '🕯 Возрождение'}: "
                           f"{u['name']} {u['surname']} возвращается в колоду (HP {u['hp']})!")


# ---------- ИИ и заклинания ----------

def est_spell_dmg(state: dict, m: dict, t: dict, adv: int = 0, coef_pct: int = 100) -> int:
    # Оценка магического урона при среднем броске (d20 ≈ 10), пробитие КЗ//2
    kz = eff_armor(t, state) // 2
    pa = 10 + 3 + 2 * m["level"] + adv
    mult = min(2, max(0, (10 + pa - kz) // 10))
    return atk_eff(m, state) * coef_pct // 100 * mult


def ai_pick_line_by_wounded(state: dict, side: str):
    best, bn = None, -1
    for ln in ROWS:
        n = sum(1 for l2, _, t in board_units(state, side) if l2 == ln and t["hp"] < t["hp_max"])
        if n > bn:
            bn, best = n, ln
    return best


def ai_spell_options(state: dict, side: str):
    # ИИ перебирает доступные заклинания и оценивает каждое простой
    # эвристикой. Возвращает список (score, sid, caster, spec).
    opp = other_side(side)
    enemies = list(board_units(state, opp))
    allies = list(board_units(state, side))
    opts = []
    mages = [m for _, _, m in allies
             if m["cls"] == CLS_MAGE and m["end"] > 0
             and not has_fx(m, "frozen") and not has_fx(m, "time_lock")]
    for m in mages:
        atk_m = atk_eff(m, state)
        for sid in spells_for_unit(m):
            sp = SPELLS[sid]
            kind = sp["eff"][0]
            if kind == "damage":
                for ln, ps, t in enemies:
                    d = est_spell_dmg(state, m, t, sp["eff"][1], sp["eff"][2])
                    if d > 0:
                        opts.append((d * 3 + (600 if d >= t["hp"] else 0), sid, m,
                                     {"kind": "unit", "uid": t["uid"]}))
            elif kind in ("damage_line", "quake", "freeze_line"):
                for ln in ROWS:
                    tg = [t for l2, _, t in enemies if l2 == ln]
                    if not tg:
                        continue
                    coef = sp["eff"][2] if kind == "damage_line" and len(sp["eff"]) > 2 else (
                        sp["eff"][3] if kind == "quake" and len(sp["eff"]) > 3 else 100)
                    d = sum(est_spell_dmg(state, m, t, 0, coef) for t in tg)
                    sc = d * 2.5 + 150 * (len(tg) - 1) + (200 if kind == "freeze_line" else 0)
                    if sc > 0:
                        opts.append((sc, sid, m, {"kind": "line", "line": ln}))
            elif kind == "damage_splash":
                best = None
                for ln in ROWS:
                    for ps in row_layout(state, ln):
                        t = cell_unit(state, ln, ps)
                        if t is None or t["side"] != opp:
                            continue
                        d = est_spell_dmg(state, m, t, sp["eff"][1], sp["eff"][2])
                        if best is None or d > best[0]:
                            best = (d, ln, ps)
                if best and best[0] > 0:
                    opts.append((best[0] * 3 + 60, sid, m, {"kind": "cell", "line": best[1], "pos": best[2]}))
            elif kind == "storm":
                if len(enemies) >= 2:
                    coef = sp["eff"][3] if len(sp["eff"]) > 3 else 100
                    ds = sorted((est_spell_dmg(state, m, t, sp["eff"][2], coef) for _, _, t in enemies), reverse=True)
                    opts.append((sum(ds[:sp["eff"][1]]) * 2.5 + 80, sid, m, None))
            elif kind in ("armageddon", "absolute"):
                if len(enemies) >= 2:
                    opts.append((200 + 40 * len(enemies), sid, m, None))
            elif kind in ("plague", "slow_all"):
                if len(enemies) >= 2:
                    opts.append((100 + 30 * len(enemies), sid, m, None))
            elif kind == "execute":
                thr = atk_m * sp["eff"][1] // 100
                for ln, ps, t in enemies:
                    if t["hp"] <= thr:
                        opts.append((950, sid, m, {"kind": "unit", "uid": t["uid"]}))
            elif kind == "freeze":
                dc = sp["eff"][1]
                for ln, ps, t in enemies:
                    if eff_armor(t, state) >= 20:
                        continue
                    p_fail = max(0.0, min(1.0, (dc - t["level"] - 1) / 20))
                    if p_fail >= 0.4:
                        opts.append((300 * p_fail + 50, sid, m, {"kind": "unit", "uid": t["uid"]}))
            elif kind == "time_lock":
                dc = sp["eff"][1]
                for ln, ps, t in enemies:
                    p_fail = max(0.0, min(1.0, (dc - t["level"] - 1) / 20))
                    if p_fail >= 0.5:
                        opts.append((180 * p_fail, sid, m, {"kind": "unit", "uid": t["uid"]}))
            elif kind == "heal":
                hurt = [(t["hp"] / max(1, t["hp_max"]), t) for _, _, t in allies if t["hp"] < t["hp_max"]]
                if hurt:
                    frac, t = min(hurt, key=lambda x: x[0])
                    if frac <= 0.5:
                        # Лечение не должно доминировать над атакой (иначе бой
                        # «затягивается»); лечим только действительно раненых
                        opts.append((min(180, 40 + atk_m * 3 * (1.0 - frac)), sid, m,
                                     {"kind": "unit", "uid": t["uid"]}))
            elif kind in ("heal_line_cleanse", "heal_line_full_shield_thorns", "archangel"):
                ln = ai_pick_line_by_wounded(state, side)
                n_hurt = sum(1 for l2, _, t in allies if l2 == ln and t["hp"] < t["hp_max"])
                if ln and n_hurt >= 1:
                    opts.append((80 * n_hurt + (100 if kind != "heal_line_cleanse" else 0), sid, m,
                                 {"kind": "line", "line": ln}))
            elif kind == "gaia":
                n_hurt = sum(1 for _, _, t in allies if t["hp"] < t["hp_max"])
                if n_hurt >= 2:
                    opts.append((90 * n_hurt, sid, m, None))
            elif kind == "fortress":
                if len(allies) >= 2:
                    opts.append((40 * len(allies), sid, m, None))
            elif kind == "ragnarok":
                if len(allies) >= 2:
                    opts.append((90 * len(allies), sid, m, None))
            elif kind in ("buff_pair",):
                ln = ai_pick_line_by_wounded(state, side)
                n = sum(1 for l2, _, _ in allies if l2 == ln)
                if n >= 1:
                    opts.append((40 * n, sid, m, {"kind": "line", "line": ln}))
            elif kind in ("buff_armor", "buff_attack", "shield", "mirror", "thorns"):
                front = [t for _, _, t in allies if t["line"] == row_of(side, "front")]
                tgt = front[0] if front else (allies[0][2] if allies else None)
                if tgt:
                    opts.append((35, sid, m, {"kind": "unit", "uid": tgt["uid"]}))
            elif kind == "haste":
                strong = [t for _, _, t in allies if t["cls"] == CLS_WARRIOR and t["end"] > 0]
                if strong:
                    t = max(strong, key=lambda x: atk_eff(x, state))
                    opts.append((45, sid, m, {"kind": "unit", "uid": t["uid"]}))
            elif kind == "wall":
                own_front = row_of(side, "front")
                has_wall = any(fx["kind"] == "wall" and fx["owner"] == side
                               for fx in state["cell_fx"].get(own_front, {}).values())
                cells = [(ln, ps) for ln, ps in own_empty_cells(state, side) if ln == own_front]
                if not has_wall and cells and enemies:
                    opts.append((70, sid, m, {"kind": "cell", "line": cells[0][0], "pos": cells[0][1]}))
            elif kind == "swamp":
                cand = [(atk_eff(t, state), ln, ps) for ln, ps, t in enemies]
                if cand:
                    cand.sort(reverse=True)
                    a, ln, ps = cand[0]
                    if a >= 6:
                        opts.append((90, sid, m, {"kind": "cell", "line": ln, "pos": ps}))
            elif kind == "apoc_swamp":
                if len(enemies) >= 2:
                    opts.append((200, sid, m, None))
            elif kind == "summon_golem":
                cells = own_empty_cells(state, side)
                if cells and len(allies) <= 2:
                    ln, ps = cells[0]
                    opts.append((130, sid, m, {"kind": "cell", "line": ln, "pos": ps}))
            elif kind in ("revive", "phoenix"):
                if state[side]["graveyard"]:
                    g = state[side]["graveyard"][0]
                    opts.append((180 if kind == "phoenix" else 150, sid, m,
                                 {"kind": "grave", "uid": g["uid"]}))
            elif kind == "cleanse":
                dirty = [t for _, _, t in allies if any(f["kind"] in NEGATIVE_FX for f in t.get("fx", []))]
                if dirty:
                    opts.append((140, sid, m, {"kind": "unit", "uid": dirty[0]["uid"]}))
            elif kind == "drain":
                tgt = [t for _, _, t in enemies if t["cls"] == CLS_MAGE and t["mana"] >= 2]
                if tgt:
                    opts.append((60, sid, m, {"kind": "unit", "uid": tgt[0]["uid"]}))
            elif kind in ("restore_mana",):
                tgt = [t for _, _, t in allies if t["cls"] == CLS_MAGE and t["mana_max"] - t["mana"] >= 3]
                if tgt:
                    opts.append((30, sid, m, {"kind": "unit", "uid": tgt[0]["uid"]}))
            elif kind in ("restore_end",):
                tgt = [t for _, _, t in allies if t["end_max"] - t["end"] >= 3 and t["hp"] > 0]
                if tgt:
                    opts.append((30, sid, m, {"kind": "unit", "uid": tgt[0]["uid"]}))
            elif kind == "debuff_attack_pct":
                strong = [t for _, _, t in enemies if atk_eff(t, state) >= 6]
                if strong:
                    t = max(strong, key=lambda x: atk_eff(x, state))
                    opts.append((60, sid, m, {"kind": "unit", "uid": t["uid"]}))
            elif kind == "curse":
                if enemies:
                    t = max(enemies, key=lambda x: atk_eff(x[2], state))[2]
                    if atk_eff(t, state) >= 5:
                        opts.append((100, sid, m, {"kind": "unit", "uid": t["uid"]}))
    return opts


def estimate_damage(state: dict, u: dict, tgt: dict, pz1: int = 0, stealth: bool = False) -> int:
    # Оценка будущего урона при среднем броске d20 ≈ 10 — нужна ИИ,
    # чтобы выбирать самую выгодную цель (реального броска нет).
    kz = eff_armor(tgt, state)
    if stealth:
        psa = 10 - 3 + u["level"] + 2 * u["sub"]
        mult = (10 + psa - pz1 // 2 - kz) // 10
    else:
        pa = 10 + 3 + 2 * u["level"]
        mult = (10 + pa - kz) // 10
    return max(0, atk_eff(u, state) * mult)


def ai_act(state: dict, side: str):
    # Простой, но осмысленный бот-противник. Приоритеты:
    # 1) выставить карту (воинов — на фронт, желательно напротив «спящего»
    #    врага, чтобы сработала атака при появлении; магов — в тыл);
    # 2) лучшее из «атака / заклинание» по единой оценке (добивание в приоритете);
    # 3) увести в колоду выдохнувшегося юнита;
    # 4) продвинуться вперёд перемещением (захват клеток);
    # 5) ударить вражескую стену, если больше бить некого;
    # 6) встать в защиту самым избитым юнитом; 7) иначе — пропуск.
    if state["over"]:
        return
    opp = other_side(side)
    s = state[side]
    opp_front = row_of(opp, "front")

    # --- 1) Высадка карты на свои пустые клетки ---
    cells = own_empty_cells(state, side)
    if s["deck"] and cells:
        front_line = row_of(side, "front")
        front_cells = [c for c in cells if c[0] == front_line]
        rear_cells = [c for c in cells if c[0] != front_line]
        warriors = [u for u in s["deck"] if u["cls"] == CLS_WARRIOR]
        mages = [u for u in s["deck"] if u["cls"] == CLS_MAGE]
        # «Спящие» враги на передней линии — по ним сработает бонус появления
        idle_front = []
        for p in row_layout(state, opp_front):
            t = cell_unit(state, opp_front, p)
            if t is not None and t["side"] == opp and not t["acted_this_turn"]:
                idle_front.append(p)
        if warriors and front_cells:
            pref = [c for c in front_cells if mirror_pos(c[1]) in idle_front]
            cell = random.choice(pref) if pref else random.choice(front_cells)
            perform_place(state, side, cell[0], cell[1], warriors[0])
            return
        if mages and rear_cells:
            cell = random.choice(rear_cells)
            perform_place(state, side, cell[0], cell[1], mages[0])
            return
        cell = random.choice(cells)
        perform_place(state, side, cell[0], cell[1], s["deck"][0])
        return

    attackers = [(line, pos, u) for line, pos, u in board_units(state, side) if u["end"] > 0
                 and not has_fx(u, "frozen") and not has_fx(u, "time_lock")]

    # --- 2) Единый конкурс «атака против заклинания» ---
    options = []  # (score, tag, payload)
    for line, pos, u in attackers:
        mage = u["cls"] == CLS_MAGE
        for tl, tp, t in board_units(state, opp):
            if can_normal_attack(u, tl) and (not mage or u["mana"] >= MAGE_ATTACK_MANA):
                est = estimate_damage(state, u, t)
                score = est * 3 + (600 if est >= t["hp"] else 0)
                options.append((score, "atk", (u, tl, tp, False)))
            if can_stealth_attack(u, tl) and (not mage or u["mana"] >= MAGE_STEALTH_MANA):
                blocker = find_blocker(state, opp, tp)
                pz1 = eff_armor(blocker, state) if blocker else 0
                est = estimate_damage(state, u, t, pz1, stealth=True)
                score = est * 3 + (600 if est >= t["hp"] else 0) - 5
                options.append((score, "atk", (u, tl, tp, True)))
    for score, sid, caster, spec in ai_spell_options(state, side):
        if score > 0:
            options.append((score, "spell", (caster, sid, spec)))
    if options:
        options.sort(key=lambda o: -o[0])
        best = options[0][0]
        top = [o for o in options if o[0] >= best - 1]
        score, tag, payload = random.choice(top)
        if tag == "atk":
            u, tl, tp, stealth = payload
            perform_attack(state, side, u, tl, tp, stealth=stealth)
        else:
            caster, sid, spec = payload
            cast_spell(state, side, caster, sid, spec)
        return

    # --- 3) Отход выдохнувшихся ---
    tired = [u for _, _, u in attackers if u["end"] <= 1]
    if tired:
        perform_retreat(state, side, min(tired, key=lambda x: x["end"]))
        return

    # --- 4) Продвижение вперёд перемещением ---
    if attackers and random.random() < 0.6:
        line, pos, u = random.choice(attackers)
        step = 1 if side == "enemy" else -1
        ni = ROW_ORDER[line] + step
        dest = None
        if 0 <= ni < len(ROWS):
            fwd = ROWS[ni]
            empt = [q for q in row_layout(state, fwd) if not cell_blocked(state, fwd, q)]
            if empt:
                dest = (fwd, min(empt, key=lambda q: abs(pos + q - 4)))
        if dest is None:
            empt = [q for q in row_layout(state, line)
                    if not cell_blocked(state, line, q) and q != pos]
            if empt and random.random() < 0.5:
                dest = (line, random.choice(empt))
        if dest is not None:
            perform_move(state, side, u, dest[0], dest[1])
            return

    # --- 5) Удар по вражеской стене, если больше бить некого ---
    any_attacker = [(l, p, u) for l, p, u in board_units(state, side) if u["end"] > 0]
    for wl, wpos, wfx in [(ln, ps, fx) for ln in ROWS for ps, fx in state["cell_fx"].get(ln, {}).items()
                          if fx["kind"] == "wall" and fx["owner"] == opp]:
        for l, p, u in any_attacker:
            if can_normal_attack(u, wl) and (u["cls"] != CLS_MAGE or u["mana"] >= MAGE_ATTACK_MANA):
                perform_attack(state, side, u, wl, wpos, stealth=False)
                return

    # --- 6) Защита ---
    if attackers:
        perform_defend(state, side, min(attackers, key=lambda x: x[2]["hp"])[2])
        return

    # --- 7) Пропуск ---
    log_add(state, f"{side_name(side)}: пропуск (нет доступных действий).")


# =====================================================================
# ИНТЕРФЕЙС БОЯ: карта поля — ЭТО КНОПКИ
# Сообщение боя:
#   • первые четыре строки кнопок — карта боя (сверху противник, снизу вы):
#       ряд 1: В2 тыл врага   (клетки слева направо 3,2,1 — поле перевёрнуто)
#       ряд 2: В1 фронт врага (3,2,1)
#       ряд 3: С1 ваш фронт   (1,2,3)
#       ряд 4: С2 ваш тыл     (1,2,3)
#     Каждая клетка — инлайн-кнопка:
#       ▫️ — клетки нет в раскладке, ⬜ — пустая клетка,
#       иначе юнит (👹 — вражеский, в т.ч. прорвавшийся на ваши линии).
#   • ниже — вкладка «🃏 Колода» (и «🌟 Уровень!» при наличии очков роста);
#   • в самом низу — «Пропустить ход» / «Покинуть бой» и «✖ Отменить»
#     (кнопка отмены показывается, пока длится выбор цели/клетки).
#
# Логика нажатий:
#   • клик по ВРАЖЕСКОМУ юниту (на любой линии) → «кем атаковать?» — список
#     ваших юнитов с доступными видами удара (⚔️ обычная / 🗡 скрытая).
#     Скрытая атака всегда спрашивает «уверены ли вы».
#   • клик по ПУСТОЙ клетке → «кем переместиться?» — ЗАХВАТ клетки теперь
#     работает как ПЕРЕМЕЩЕНИЕ персонажа на свою или соседнюю линию.
#   • клик по СВОЕМУ юниту → меню ДП: Атака / Скрытая атака / Перемещение /
#     Защита / Отход. «Атака», «Скрытая» и «Перемещение» возвращают на
#     поле, и СЛЕДУЮЩИЙ клик считается целью (клик по союзнику запрещён;
#     внизу есть «✖ Отменить»).
#   • клик по СВОЕЙ пустой клетке → выбрать карту из колоды и высадить.
# =====================================================================

def ib(text: str, cb: str) -> InlineKeyboardButton:
    # Короткий помощник для инлайн-кнопок
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def unit_flags(u: dict) -> str:
    # Флаги состояния юнита для кнопок и списков
    fl = ""
    if u["defending"]:
        fl += "🔰"
    if u["end"] <= 0:
        fl += "💤"
    if has_fx(u, "frozen"):
        fl += "❄️"
    if has_fx(u, "time_lock"):
        fl += "⏳"
    if fx_sum(u, "shield") > 0:
        fl += f"🔮щ{fx_sum(u, 'shield')}"
    return fl


def cell_btn(state: dict, line: str, pos: int) -> InlineKeyboardButton:
    # Одна клетка карты = одна кнопка. Порядок значков (по заказу):
    #   стикер класса → главная цифра (⚔ атака или 🛡 защита — что больше;
    #   у заклинателей всегда ⚔) → ❤ здоровье у воинов / 🔮 манна у магов;
    #   если выносливость ≤ 1 — ВМЕСТО здоровья/манны показываем ⚡остаток/запас.
    # Затем имя и флаги состояния. 👹 — враг, 🧱 — стена, 🟢 — болото, ⬜ — пусто.
    cb = f"c:{line}:{pos}"
    if not cell_exists(state, line, pos):
        return ib("▫️", cb)
    w = get_wall(state, line, pos)
    if w is not None:
        mark = "" if w["owner"] == "player" else "👹"
        return ib(f"🧱{w['hp']}{mark}", cb)
    u = cell_unit(state, line, pos)
    swamp = cell_fx_at(state, line, pos)
    pre = "🟢" if swamp and swamp["kind"] == "swamp" else ""
    if u is None:
        return ib(pre + "⬜", cb)
    flags = unit_flags(u)
    ce = CLS_EMOJI[u["cls"]]
    ico, val = main_stat(u["cls"], atk_eff(u, state), eff_armor(u, state))
    if u["end"] <= 1:
        res = f"⚡{u['end']}/{u['end_max']}"
    elif u["cls"] == CLS_MAGE:
        res = f"🔮{u['mana']}"
    else:
        res = f"❤{u['hp']}"
    foe = "" if u["side"] == "player" else "👹"
    text = f"{pre}{foe}{ce}{ico}{val} {res} {u['name']} {flags}"
    return ib(text[:64], cb)


def row_cells(state: dict, line: str):
    # По заказу: если на линии две клетки — показываем ДВЕ кнопки, если
    # одна — одну (без заглушек). Порядок: вражеские линии слева направо
    # 3→1 (поле перевёрнуто), свои — 1→3.
    lay = row_layout(state, line)
    order = (3, 2, 1) if ROW_SIDE[line] == "enemy" else (1, 2, 3)
    return [p for p in order if p in lay]


def kb_battle(state: dict) -> InlineKeyboardMarkup:
    # Клавиатура боя: 4 ряда карты (только реально существующие клетки)
    # + вкладка колоды + управление
    rows = [[cell_btn(state, line, p) for p in row_cells(state, line)] for line in ROWS]
    bottom = [ib(f"🃏 Колода ({len(state['player']['deck'])})", "DK")]
    if not state["over"]:
        # Книга заклинаний теперь прямо на карте боя: кастер → заклинание → цель
        bottom.append(ib("✨ Магия", "SB"))
    pl = pending_levels_total(state)
    if pl > 0:
        bottom.append(ib(f"🌟 Уровень! ({pl})", "LU"))
    rows.append(bottom)
    if state["over"]:
        rows.append([ib("⚔️ Ещё раз", "bt:again"), ib("🏠 Меню", "mn:home")])
    else:
        rows.append([ib("⏭ Пропустить ход", "bt:skip"), ib("🏳 Покинуть бой", "bt:fl")])
    if state.get("pending"):
        # Кнопка отмены — внизу, пока идёт выбор цели/клетки
        rows.append([ib("✖ Отменить", "X")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


RESULT_TEXT = {
    "victory": "🏆 <b>ПОБЕДА!</b>",
    "defeat": "☠️ <b>ПОРАЖЕНИЕ.</b>",
    "draw": "🤝 <b>НИЧЬЯ.</b>",
    "flee": "🏳 <b>Вы покинули бой.</b>",
}


def roster_unit_line(u: dict, state: dict = None) -> str:
    # Строка списка карт на поле (по заказу, формат:
    # «имя: хп, выносливость, атака, манна если есть, скрытность, защита»)
    mana = f", 🔮 {u['mana']}/{u['mana_max']}" if u["cls"] == CLS_MAGE else ""
    sub_e = "🥷" if u["cls"] == CLS_WARRIOR else "🎯"
    flags = unit_flags(u)
    if state is not None and u.get("line"):
        cfx = cell_fx_at(state, u["line"], u["pos"])
        if cfx and cfx["kind"] == "swamp":
            flags += "🟢"
    atk = atk_eff(u, state) if state is not None else u["attack"]
    return (f"• {ROW_LABEL[u['line']]}:{u['pos']} {u['name']}: "
            f"❤{u['hp']}, ⚡{u['end']}, ⚔{atk}{mana}, {sub_e}{u['sub']}, "
            f"🛡{eff_armor(u, state) if state is not None else u['armor']}{flags}")


def roster_text(state: dict, side: str) -> str:
    # Список всех карт стороны на поле (сортировка: линия сверху вниз, позиция)
    units = sorted(board_units(state, side), key=lambda x: (ROW_ORDER[x[0]], x[1]))
    if not units:
        return "• (на поле карт нет)"
    return "\n".join(roster_unit_line(u, state) for _, _, u in units)


def battle_text(state: dict) -> str:
    # Текстовая часть сообщения: шапка, подсказка текущего режима,
    # СПИСОК ВСЕХ КАРТ НА ПОЛЕ (заказ) и короткий журнал.
    # Сама карта рисуется кнопками (kb_battle).
    if state["over"]:
        head = (f"🏁 <b>Бой завершён</b> · раундов: {state['round']}\n"
                f"{RESULT_TEXT.get(state['result'], '')}")
        if not state.get("rewards_text") and pending_levels_total(state) > 0:
            head += ("\n\n🌟 Сначала распределите очки роста "
                     "(кнопка «🌟 Уровень!») — потом итоги запишутся в карты.")
    else:
        head = (f"⚔️ <b>Раунд {state['round']}</b> · Ваш ход — одно действие\n"
                f"👹 противник сверху · 🧑 вы снизу")
    hint = ""
    pend = state.get("pending")
    if pend:
        _, u, _, _ = find_unit(state, pend["uid"])
        nm = u["name"] if u else "…"
        if pend["act"] == "attack":
            hint = (f"\n\n🎯 <b>Выбор цели</b>: нажмите ВРАЖЕСКОГО юнита, которого "
                    f"достанет {nm} (фронт В1 — всегда, либо своя/соседняя линия). "
                    f"По союзнику бить нельзя. Внизу «✖ Отменить».")
        elif pend["act"] == "stealth":
            hint = (f"\n\n🗡 <b>Выбор цели скрытой атаки</b>: нажмите вражеского юнита "
                    f"в тылу (В2) — ударит {nm}. Внизу «✖ Отменить».")
        elif pend["act"] == "move":
            reach = ", ".join(ROW_LABEL[l] for l in ROWS if u and l in lines_reach(u["line"]))
            hint = (f"\n\n🚶 <b>Перемещение (захват)</b>: нажмите пустую клетку — "
                    f"{nm} перейдёт на неё. Доступные линии: {reach}. Внизу «✖ Отменить».")
        elif pend["act"] == "place":
            hint = f"\n\n📥 <b>Высадка</b>: нажмите свою пустую клетку (С1/С2), чтобы выставить {nm}."
        elif pend["act"] == "spell":
            sp = SPELLS[pend["spell"]]
            t = sp["target"]
            if t == "enemy_unit":
                goal = "нажмите ВРАЖЕСКОГО юнита"
            elif t == "ally_unit":
                goal = "нажмите СОЮЗНОГО юнита (можно себя)"
            elif t == "enemy_cell":
                goal = "нажмите любую клетку вражеских линий (В1/В2)"
            else:
                goal = "нажмите свою пустую клетку"
            hint = (f"\n\n{sp['emoji']} <b>«{sp['name']}»</b>: {goal}. {sp['desc']} "
                    f"Внизу «✖ Отменить».")
    roster = ("👹 <b>Противник на поле:</b>\n" + roster_text(state, "enemy") +
              "\n🧑 <b>Вы на поле:</b>\n" + roster_text(state, "player"))
    log_tail = "\n".join("• " + x for x in state["log"][-4:])
    text = f"{head}{hint}\n\n{roster}\n\n📜 <b>Журнал</b>:\n{log_tail}"
    if state["over"] and state.get("rewards_text"):
        text += "\n\n" + state["rewards_text"]
    return text


def unit_info(u: dict) -> str:
    mana = f" · 🔮 {u['mana']}/{u['mana_max']}" if u["cls"] == CLS_MAGE else ""
    sub_lbl = "🥷 Скрытность" if u["cls"] == CLS_WARRIOR else "🎯 Точность"
    pos_lbl = f" · 📍 {ROW_LABEL[u['line']]}:{u['pos']}" if u.get("line") else ""
    rar = rarity_label(u.get("rarity", "common"))
    return (f"{RACES[u['race']]['emoji']} <b>{u['name']} {u['surname']}</b> · {u['race']}\n"
            f"{rar} · {CLS_NAME[u['cls']]} · ⭐ Уровень {u['level']}{pos_lbl}\n"
            f"❤️ {u['hp']}/{u['hp_max']} · ⚔️ {atk_eff(u)} · 🛡 КЗ {u['armor']} (эфф. {eff_armor(u)})\n"
            f"{sub_lbl}: {u['sub']} · ⚡ {u['end']}/{u['end_max']}{mana}")


def attacker_error(u: dict, stealth: bool):
    # Почему юнит не может атаковать/кастовать (None — может)
    if u["end"] <= 0:
        return "💤 Карта истощена (выносливость 0) и не может действовать!"
    if has_fx(u, "frozen"):
        return "❄️ Карта заморожена: атака и магия невозможны (ждите разморозки или «Очищения»)!"
    if has_fx(u, "time_lock"):
        return "⏳ Временная петля: атака невозможна (защита/отход/перемещение — можно)."
    if u["cls"] == CLS_MAGE:
        cost = MAGE_STEALTH_MANA if stealth else MAGE_ATTACK_MANA
        if u["mana"] < cost:
            return f"🔮 Не хватает манны: {u['mana']}/{cost}. Манна восстанавливается только в колоде."
    return None


def known_spells(u: dict):
    # Заклинания, ОТКРЫТЫЕ магу по уровню и редкости (без проверки манны)
    if u["cls"] != CLS_MAGE:
        return []
    return [sid for sid, sp in SPELLS.items()
            if u["level"] >= sp["lvl"]
            and rarity_rank(u.get("rarity", "common")) >= rarity_rank(sp["rarity"])]


def spell_ready(state: dict, u: dict, sid: str):
    # Полная проверка возможности каста: текст ошибки или None
    sp = SPELLS[sid]
    if u["cls"] != CLS_MAGE:
        return "Кастовать могут только заклинатели 🔮."
    if u["level"] < sp["lvl"]:
        return f"«{sp['name']}» откроется на {sp['lvl']} уровне (у карты {u['level']})."
    if rarity_rank(u.get("rarity", "common")) < rarity_rank(sp["rarity"]):
        return (f"«{sp['name']}» доступно магам редкости {rarity_label(sp['rarity'])} и выше "
                f"(у карты {rarity_label(u.get('rarity', 'common'))}).")
    if u["end"] <= 0:
        return "💤 Карта истощена и не может действовать!"
    if has_fx(u, "frozen"):
        return "❄️ Заморозка мешает касту!"
    if has_fx(u, "time_lock"):
        return "⏳ Временная петля мешает касту!"
    if u["mana"] < sp["min_mana"]:
        return f"Нужно минимум {sp['min_mana']}🔮 (сейчас {u['mana']}). Манна восстанавливается только в колоде."
    side = u["side"]
    opp = other_side(side)
    t = sp["target"]
    has_enemies = any(True for _ in board_units(state, opp))
    if t in ("enemy_unit", "enemy_line") and not has_enemies:
        return "На поле нет вражеских юнитов."
    if t in ("all_enemies", "all_enemy_cells") and not has_enemies:
        return "На поле нет вражеских юнитов."
    if t == "enemy_cell":
        if not any(cell_exists(state, ln, p) and get_wall(state, ln, p) is None
                   for ln in side_rows(opp) for p in row_layout(state, ln)):
            return "Нет доступных вражеских клеток."
    if t == "own_front_empty":
        fr = row_of(side, "front")
        if not any(not cell_blocked(state, fr, p) for p in row_layout(state, fr)):
            return "На передней линии нет пустых клеток."
    if t == "own_empty" and not own_empty_cells(state, side):
        return "Нет своих пустых клеток."
    if t == "ally_grave" and not state[side]["graveyard"]:
        return "Никто не погиб — воскрешать некого."
    return None


def spell_target_error(state: dict, side: str, sp: dict, line: str, pos: int,
                       target, wall) -> str | None:
    # Валидация выбранной клетки/юнита для заклинания (клик в режиме цели)
    t = sp["target"]
    opp = other_side(side)
    if t == "enemy_unit":
        if target is None or target["side"] != opp:
            return "Нужен ВРАЖЕСКИЙ юнит."
    elif t == "ally_unit":
        if target is None or target["side"] != side:
            return "Нужен СОЮЗНЫЙ юнит."
    elif t == "enemy_cell":
        if ROW_SIDE[line] != opp:
            return "Нужна клетка на линиях врага (В1/В2)."
        if wall is not None:
            return "Стену затопить нельзя."
    elif t in ("own_front_empty", "own_empty"):
        if ROW_SIDE[line] != side:
            return "Нужна клетка на СВОИХ линиях (С1/С2)."
        if t == "own_front_empty" and line != row_of(side, "front"):
            return "Стена ставится только на переднюю линию (С1)."
        if target is not None or wall is not None:
            return "Клетка занята."
    return None


# =====================================================================
# ТЕКСТЫ ИНТЕРФЕЙСА
# =====================================================================

MENU_TEXT = ("🏠 <b>Главное меню</b>\n\n"
             "📚 Коллекция — все ваши карты (фильтры, сортировка, просмотр)\n"
             "🃏 Колода — до 3 боевых колод; ⭐приоритетная идёт в бой без вопроса\n"
             "🎲 Получить карту — новая случайная карта\n"
             "⚔️ Бой — сражение с ботом-противником\n"
             "❓ Помощь — правила игры")

HELP_TEXT = """❓ <b>Правила «Карточных боёв»</b>

🃏 <b>Карты и классы</b>
• <b>Воин ⚔️</b> — ближний бой. Пары стат по системе весов (+20% к одной → −10% к парной): Атака↔Защита(КЗ), Здоровье↔Скрытность.
• <b>Заклинатель 🔮</b> — дальние заклинания за манну. Пары: Атака↔Запас Манны, Здоровье↔Точность.
• Карты генерируются случайно: 10 рас, по 20 имён и 20 фамилий на расу (200×200 = 4000 сочетаний). Полные имена в вашей коллекции не повторяются: при исчерпании пула добавляется римский суффикс («Торгрим Железнобород II»). Пока получение карт не ограничено.
• ✨ РЕДКОСТИ: ⬜ Обычная (50%) / 🟩 Необычная (27%) / 🟦 Редкая (15%) / 🟪 Эпическая (6%) / 🟨 Легендарная (2%). Множитель статов ×1.00–×1.20, высокие редкости открывают сильные заклинания. Pity: 40 карт без 🟦+ → следующая гарантированно 🟦+.
• 📜 ТИТУЛЫ: с 3-го уровня в карточке коллекции можно «Даровать титул» (свой текст). Переключатель 🎭 ставит титул ПЕРЕД именем — так карта видна в бою, колодах и коллекции: «Гроза Юга» Пупкин Васильев.
• Колоды (до 3): новые карты попадают в КОЛЛЕКЦИЮ (📚), а «🃏 Колода» открывается списком всех карт — нажатие на карту кладёт/убирает её из открытой колоды. ⭐Приоритетная колода идёт в бой без вопроса; если не выбрана — бот спросит при старте (одна непустая колода берётся сама). В бой идут первые 7 карт колоды.

⚔️ <b>Поле боя — 4 клетки и 4 линии</b>
• Линии сверху вниз: В2 (тыл врага), В1 (фронт врага), С1 (ваш фронт), С2 (ваш тыл). У каждой стороны по 4 клетки, максимум 3 на линию. Раскладки: 3+1, 2+2 или 1+3.
• Нумерация: 3 клетки → 1,2,3; 2 клетки → 1 и 3; 1 клетка → 2. Поля перевёрнуты: ваша клетка 1 напротив вражеской 3.
• Карта — это кнопки: нажимайте прямо на клетки! 👹 — вражеский юнит (в т.ч. прорвавшийся на ваши линии).

🚩 <b>Захват клеток = перемещение</b>
• Персонаж просто ПЕРЕМЕЩАЕТСЯ на пустую клетку своей или СОСЕДНЕЙ линии (В2↔В1↔С1↔С2). Пример: с В1 на С2 за один шаг не попасть.
• Перемещение стоит 1⚡ выносливости и НЕ тратит манну. Клик по пустой клетке → «кем переместиться?», или «🚶 Переместиться» в меню своего юнита.

🎮 <b>Управление</b>
• Клик по вражескому юниту → выбрать, КЕМ атаковать (⚔️ обычная и/или 🗡 скрытая). Клик по вражеской стене 🧱 → кем сломать.
• Клик по своему юниту → ДП: Атака / Скрытая атака / 🚶 Перемещение / Защита / Отход / ✨ Магия (у заклинателей). Режимы атаки/перемещения/магии возвращают на поле — следующий клик считается целью (есть «✖ Отменить»; бить союзника нельзя).
• Скрытая атака всегда спрашивает подтверждение.
• Клик по пустой клетке → 🚶 переместиться (захват) или 📥 высадить карту из колоды (на своих линиях). Вкладка «🃏 Колода» — внизу.
• «✨ Магия» внизу карты боя — книга заклинаний прямо с поля: сначала кастер (видно манну 🔮, выносливость ⚡ и известные заклинания), затем заклинание, затем цель.
• 🌟 При повышении уровня вы САМИ выбираете, какую стату поднять (очки погибших карт тоже можно распределить).

✨ <b>МАГИЯ (40 заклинаний)</b>
• Кастуют только ЗАКЛИНАТЕЛИ с поля; заклинания открываются по УРОВНЮ и РЕДКОСТИ карты.
• Каст тратит ВЕСЬ (или почти весь — часть заклинаний оставляет 1🔮) пул манны, 1⚡ и всё действие хода. Маг после каста пуст — восстанавливаться в колоду; это естественный кулдаун.
• 🧱 Стена (🟦): своя передняя линия — +3 КЗ вашему тылу, заслон от скрытых атак, блокирует клетку; разрушаема (10 HP, КЗ 12).
• 🟢 Болото (🟦): клетка врага на 3 хода — стоящий теряет 25% Атаки и 2 КЗ; уход с клетки снимает эффект.
• ❄️ Заморозка (🟦+): цель не атакует и не кастует, КЗ=0, 1 ход. Спасбросок d20+уровень ≥ 12; иммунитет при КЗ ≥ 20.
• Магический урон пробивает половину КЗ, но множитель ограничен ×2 — «кнопки победы» нет; контроль всегда проходит через спасброски.

🎲 <b>Ход</b>
• За ход доступно ОДНО действие: ДП юнита (атака/скрытая/перемещение/защита/отход/магия) ИЛИ высадка карты из колоды. Действия не накапливаются.

📐 <b>Формулы</b>
• Попадание атаки: ПА = d20 + 3 + 2×уровень. Урон = АА × (10 + (ПА − КЗ)) // 10.
• Обычная атака достаёт фронт врага (В1) всегда, а также любую линию, соседнюю с позицией атакующего (чтобы прорвавшихся врагов можно было бить).
• Скрытая атака (по тылу В2 через заслон фронта): ПСА = d20 − 3 + уровень + 2×(скрытность|точность); Урон = АА × (10 + (ПСА − КЗзаслона//2 − КЗцели)) // 10.
• Защита: КЗ +50% до первого удара по юниту или до начала вашего следующего хода.
• Отход: карта уходит в колоду, КЗ падает на 50% до конца боя.

✨ <b>Атака при появлении — только между раундами!</b>
• Воин, выходя из колоды, автоматически бьёт с преимуществом (+2 к ПА) врага на передней линии — НО только если этот враг НЕ совершал действий в свой прошлый ход («застал врасплох»).
• Поэтому в первом раунде, когда обе стороны только выставляют карты, бонус не срабатывает: свежая карта считается уже действовавшей.
• Бонус не тратит ни выносливость, ни манну.

📊 <b>Ресурсы</b>
• ⚡ Выносливость (старт — 4): −1 за каждое действие (включая перемещение), +1 за ход в колоде. На нуле КЗ=0 и карта не может действовать.
• 🔮 Манна: 1 за атаку заклинателя, 2 за скрытую; восстанавливается только в колоде.
• ❤️ Здоровье в бою не восстанавливается (кроме магии лечения).
• 😮‍💨 Усталость: начиная с 40-го раунда юниты на поле теряют HP каждый ход (урон растёт каждые 10 раундов) — бои не затягиваются навсегда.
• 💀 Погибшая карта выбывает до конца текущего боя, но НЕ исчезает навсегда: после боя восстановится и останется в коллекции.
• 🌟 Опыт за убийства и итог боя; уровень поднимает попадание сразу, а очко статы вы распределяете сами.

<b>Обозначения на карте</b>: 👹 вражеский юнит/стена · 🔰 держит защиту · 💤 истощён · ❄️ заморожен · ⏳ временная петля · 🧱 стена · 🟢 болото · ⬜ пустая клетка · ▫️ клетки нет."""


# =====================================================================
# ХЕНДЛЕРЫ: доступ по паролю + админ-панель
# Состояние «ждём пароль» хранится в БД (users.awaiting_password), а не
# в FSM-памяти: бот одинаково работает и локально (polling), и на
# serverless (Vercel webhook), где память между запросами не живёт.
# =====================================================================

router = Router()


KB_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📚 Коллекция"), KeyboardButton(text="🃏 Колода")],
        [KeyboardButton(text="🎲 Получить карту"), KeyboardButton(text="⚔️ Бой")],
        [KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
)


async def ensure_access_msg(m: Message) -> bool:
    # Страж для обычных сообщений: если доступа нет — просим пароль
    await ensure_user(m.from_user.id, m.from_user.username)
    if await has_access(m.from_user.id):
        return True
    await set_user_flag(m.from_user.id, "awaiting_password", 1)
    await m.answer("🔒 <b>Доступ ограничен.</b>\n\nВведите пароль:")
    return False


@router.callback_query.middleware()
async def cb_access_middleware(handler, event: CallbackQuery, data):
    # Тот же страж, но для нажатий на инлайн-кнопки
    if not await has_access(event.from_user.id):
        await safe_answer(event, "🔒 Доступ к боту защищён паролем.\nОтправьте /start и введите пароль.",
                           show_alert=True)
        return
    return await handler(event, data)


@router.message(CommandStart())
async def cmd_start(m: Message):
    await ensure_user(m.from_user.id, m.from_user.username)
    if not await has_access(m.from_user.id):
        await set_user_flag(m.from_user.id, "awaiting_password", 1)
        await m.answer("🔒 <b>«Карточные бои» — доступ ограничен!</b>\n\n"
                       "Этот бот защищён паролем. Введите пароль:")
        return
    await m.answer(MENU_TEXT, reply_markup=KB_MENU)
    st = await db_load_battle(m.from_user.id)
    if st:
        await m.answer("⚔️ У вас есть активный бой!", reply_markup=kb(ib("▶️ Продолжить бой", "bt:view")))


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    # Админ-панель (заказ): /admin → админ-пароль ADMIN_PASSWORD → меню кнопок.
    # Флаг is_admin сохраняется в БД — повторно пароль вводить не нужно.
    await ensure_user(m.from_user.id, m.from_user.username)
    uid = m.from_user.id
    if not await has_access(uid):
        await set_user_flag(uid, "awaiting_password", 1)
        await m.answer("🔒 Сначала получите доступ к боту (отправьте /start и основной пароль).")
        return
    if await user_flag(uid, "is_admin"):
        await m.answer(admin_text(), reply_markup=admin_kb())
        return
    await set_user_flag(uid, "awaiting_admin", 1)
    await m.answer("🛠 <b>Админ-панель</b>\n\nВведите админ-пароль:")


# =====================================================================
# ХЕНДЛЕРЫ: главное меню
# =====================================================================

@router.message(F.text == "📚 Коллекция")
async def msg_collection(m: Message):
    if not await ensure_access_msg(m):
        return
    text, markup = await collection_screen(m.from_user.id, "all", 0)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "🃏 Колода")
async def msg_deck(m: Message):
    if not await ensure_access_msg(m):
        return
    pref = await user_flag(m.from_user.id, "pref_deck") or 1
    text, markup = await deck_screen(m.from_user.id, pref, 0)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "🎲 Получить карту")
async def msg_get_card(m: Message):
    if not await ensure_access_msg(m):
        return
    text, markup = await get_new_card(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "⚔️ Бой")
async def msg_battle(m: Message):
    if not await ensure_access_msg(m):
        return
    st = await db_load_battle(m.from_user.id)
    if st:
        await m.answer(battle_text(st), reply_markup=kb_battle(st))
    else:
        text, markup = await battle_setup_screen(m.from_user.id)
        await m.answer(text, reply_markup=markup)


@router.message(F.text == "❓ Помощь")
async def msg_help(m: Message):
    if not await ensure_access_msg(m):
        return
    await m.answer(HELP_TEXT)


@router.message(F.text)
async def msg_catchall(m: Message):
    # Единая точка входа для текста:
    #  1) нет доступа — любое сообщение трактуется как пароль;
    #  2) ждём админ-пароль (после /admin) — проверяем ADMIN_PASSWORD;
    #  3) иначе — показываем меню (заодно возвращаем пропавшую клавиатуру).
    await ensure_user(m.from_user.id, m.from_user.username)
    uid = m.from_user.id
    text = (m.text or "").strip()
    if not await has_access(uid):
        if text == ACCESS_PASSWORD:
            # Пароль верный — СРАЗУ отмечаем в БД, что доступ разрешён
            await grant_access(uid)
            await m.answer("✅ <b>Доступ разрешён!</b> Отметка сохранена в базе — "
                           "пароль больше вводить не нужно.\n\n" + MENU_TEXT,
                           reply_markup=KB_MENU)
        else:
            await set_user_flag(uid, "awaiting_password", 1)
            await m.answer("🔒 <b>Доступ ограничен.</b>\n\nВведите пароль:")
        return
    if await user_flag(uid, "awaiting_admin"):
        await set_user_flag(uid, "awaiting_admin", 0)
        if text == ADMIN_PASSWORD:
            await set_user_flag(uid, "is_admin", 1)
            await m.answer("🛠 <b>Админ-доступ разрешён.</b>\n\n" + admin_text(),
                           reply_markup=admin_kb())
        else:
            await m.answer("❌ Неверный админ-пароль.", reply_markup=KB_MENU)
        return
    t_card = await user_flag(uid, "awaiting_title")
    if t_card:
        # Ждём текст титула для карты №t_card (кнопка «📜 Даровать титул»)
        if text.lower() in ("/cancel", "отмена"):
            await set_user_flag(uid, "awaiting_title", 0)
            await m.answer("📜 Ввод титула отменён.", reply_markup=KB_MENU)
            return
        title = sanitize_title(text)
        if not title:
            await m.answer("📜 Титул не может быть пустым (символы «&lt;» «&gt;» «&amp;» "
                           "не допускаются). Отправьте другой текст или /cancel.")
            return
        await db_set_title(int(t_card), title, uid)
        await set_user_flag(uid, "awaiting_title", 0)
        row = await db_get_card(int(t_card), uid)
        if row is None:
            await m.answer("Карта не найдена — титул не сохранён.", reply_markup=KB_MENU)
            return
        await m.answer(f"📜 Титул <b>«{title}»</b> дарован! Использовать его как имя — "
                       "переключатель 🎭 на карточке.\n\n" + card_text(row),
                       reply_markup=card_kb(row, "all", "0"))
        return
    await m.answer(MENU_TEXT, reply_markup=KB_MENU)


@router.callback_query(F.data == "mn:home")
async def cb_home(call: CallbackQuery):
    await safe_answer(call)
    await safe_edit(call, MENU_TEXT, None)


async def safe_answer(call, text=None, show_alert=False, cache_time=None):
    # answerCallbackQuery падает с «query is too old and response timeout
    # expired or query ID is invalid», если с момента нажатия кнопки прошло
    # ~10-15 секунд. Так бывает, когда Telegram повторно доставляет апдейт
    # после медленной или упавшей первой обработки (cold start, долгий бой,
    # прогон симуляций). Это НЕ повод ронять весь апдейт и пугать
    # пользователя «⚠️ Внутренняя ошибка бота»: молча глотаем именно эту
    # ошибку — хендлер доработает и обновит экран через safe_edit.
    try:
        await call.answer(text, show_alert=show_alert, cache_time=cache_time)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "query is too old" in msg or "query id is invalid" in msg:
            return
        raise


async def safe_edit(call: CallbackQuery, text: str, markup=None):
    # edit_text падает, если сообщение не изменилось или его его нельзя
    # редактировать — не считаем это ошибкой.
    # Важно: если передать reply_markup=None, Telegram оставит СТАРУЮ
    # клавиатуру, поэтому для «пустого» экрана явно шлём пустую клавиатуру.
    if markup is None:
        markup = InlineKeyboardMarkup(inline_keyboard=[])
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        # Если Telegram не смог разобрать HTML-разметку (например, в текст
        # попал голый «<» или «&»), повторяем отправку БЕЗ parse_mode —
        # пользователь увидит простой текст вместо «залипшего» экрана.
        if "can't parse entities" in str(e):
            try:
                await call.message.edit_text(text, reply_markup=markup,
                                             parse_mode=None)
            except TelegramBadRequest:
                pass
        # Остальные ошибки (сообщение не изменилось и т.п.) — игнорируем.


# =====================================================================
# ХЕНДЛЕРЫ: коллекция и боевая колода
# По заказу добавлен раздел «📚 Коллекция» — в нём видны ВСЕ карты
# (с фильтрами по классу/колоде/расе и пагинацией), а «🃏 Колода»
# показывает только боевой набор, который пойдёт в сражение.
# =====================================================================

CARDS_PER_PAGE = 5

# Коды фильтров коллекции (участвуют в callback'ах, поэтому короткие):
#   all — все, w — воины, m — заклинатели, in — в колоде, out — вне колоды,
#   r0..r9 — по расе (индекс в порядке RACES).
COL_FILTERS = [("all", "🧾 Все"), ("w", "⚔️ Воины"), ("m", "🔮 Заклинатели"),
               ("in", "✅ В колоде"), ("out", "➖ Вне колоды")]
RACE_KEYS = list(RACES.keys())


def col_filter_cards(cards, f: str):
    if f == "w":
        return [c for c in cards if c["cls"] == CLS_WARRIOR]
    if f == "m":
        return [c for c in cards if c["cls"] == CLS_MAGE]
    if f == "in":
        return [c for c in cards if c["in_deck"]]
    if f == "out":
        return [c for c in cards if not c["in_deck"]]
    if f.startswith("q") and f[1:].isdigit() and int(f[1:]) < len(RARITY_KEYS):
        rk = RARITY_KEYS[int(f[1:])]
        return [c for c in cards if (c["rarity"] if "rarity" in c.keys() else "common") == rk]
    if f.startswith("r") and f[1:].isdigit() and int(f[1:]) < len(RACE_KEYS):
        race = RACE_KEYS[int(f[1:])]
        return [c for c in cards if c["race"] == race]
    return list(cards)


def col_filter_label(f: str) -> str:
    for code, label in COL_FILTERS:
        if code == f:
            return label
    if f.startswith("q") and f[1:].isdigit() and int(f[1:]) < len(RARITY_KEYS):
        return rarity_label(RARITY_KEYS[int(f[1:])])
    if f.startswith("r") and f[1:].isdigit() and int(f[1:]) < len(RACE_KEYS):
        race = RACE_KEYS[int(f[1:])]
        return f"{RACES[race]['emoji']} {race}"
    return "🧾 Все"


def card_text(row) -> str:
    sub_lbl = "🥷 Скрытность" if row["cls"] == CLS_WARRIOR else "🎯 Точность"
    mana_line = f"\n🔮 Запас манны: {row['mana_max']}" if row["cls"] == CLS_MAGE else ""
    in_deck_line = (f"✅ В колоде №{row['in_deck']} — участвует в бою" if row["in_deck"]
                    else "➖ Вне колод — бой пропускает")
    rarity = row["rarity"] if "rarity" in row.keys() else "common"
    ttl = (row.get("title") or "").strip() if hasattr(row, "get") else ""
    title_line = ""
    if ttl:
        title_line = (f"\n📜 Титул: «{ttl}»" +
                      (" — используется как имя 🎭" if row.get("use_title") else
                       " (не используется как имя)"))
    return "\n".join([
        f"🃏 <b>{disp_name(row)}</b> · #{row['id']}",
        f"{rarity_label(rarity)} · {RACES[row['race']]['emoji']} {row['race']} · {CLS_NAME[row['cls']]}",
        f"⭐ Уровень {row['level']} · Опыт {row['exp']}/{xp_to_next(row['level'])}{title_line}",
        "",
        f"⚔️ Атака (АА): {row['attack']}",
        f"❤️ Здоровье: {row['hp']}/{row['hp_max']}",
        f"🛡 Защита (КЗ): {row['armor']}",
        f"{sub_lbl}: {row['sub']}",
        f"⚡ Выносливость: {row['end_max']}/{row['end_max']}{mana_line}",
        "",
        in_deck_line,
    ])


def card_kb(row, ctx: str, page: str) -> InlineKeyboardMarkup:
    # Кнопки карточки; «Назад» возвращает туда, откуда пришли (ctx+page).
    # Колода — через выбор одной из DECK_COUNT; титул — с L{TITLE_MIN_LEVEL}.
    cid = row["id"]
    cur = f" (сейчас №{row['in_deck']})" if row["in_deck"] else ""
    rows = [[ib(f"📥 В колоду…{cur}"[:64], f"dks:{cid}:{ctx}:{page}")],
            [ib("🗑 Сбросить карту", f"dkd:{cid}:{ctx}:{page}")]]
    ttl = (row.get("title") or "").strip() if hasattr(row, "get") else ""
    if row["level"] >= TITLE_MIN_LEVEL:
        if ttl:
            use = "используется как имя 🎭" if row.get("use_title") else "не используется как имя"
            rows.append([ib(f"🎭 «{ttl}» — {use}"[:64], f"tu:{cid}:{ctx}:{page}")])
            rows.append([ib("📜 Сменить титул", f"tt:{cid}:{ctx}:{page}")])
        else:
            rows.append([ib("📜 Даровать титул", f"tt:{cid}:{ctx}:{page}")])
    rows.append([ib("⬅️ Назад", back_cb(ctx, page))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_cb(ctx: str, page: str) -> str:
    # Куда ведёт «Назад»: dk — колоды, иначе коллекция с фильтром~сортировкой
    return "dk:1:0" if ctx == "dk" else f"col:{ctx}:{page}"


async def ctx_screen(user_id: int, ctx: str, page: str):
    # Экран по контексту возврата
    if ctx == "dk":
        return await deck_screen(user_id, 1, 0)
    return await collection_screen(user_id, ctx, int(page) if page.isdigit() else 0)


async def collection_screen(user_id: int, f: str, page: int):
    # «📚 Коллекция» — ВСЕ карты игрока с фильтром, сортировкой и пагинацией.
    # f = «<фильтр>» или «<фильтр>~<код_сортировки>» (коды — SORTS,
    # «-» на конце = по убыванию; пусто = 📅 дата получения, как раньше).
    flt, _, srt = f.partition("~")
    srt = srt or "id"
    cards = await db_user_cards(user_id)
    if not cards:
        return ("📚 <b>Коллекция пуста.</b>\n\nПолучите первую карту!",
                kb([ib("🎲 Получить карту", "getc")], [ib("🏠 Меню", "mn:home")]))
    deck_counts = [sum(1 for c in cards if c["in_deck"] == n)
                   for n in range(1, DECK_COUNT + 1)]
    in_deck_n = sum(deck_counts)
    decks_line = " ".join(f"{n}️⃣{deck_counts[n - 1]}" for n in range(1, DECK_COUNT + 1))
    filt = sort_cards(col_filter_cards(cards, flt), srt)
    if not filt:
        return (f"📚 <b>Коллекция</b>: всего {len(cards)} карт · в колодах: {in_deck_n} ({decks_line})\n\n"
                f"Под фильтром «{col_filter_label(flt)}» карт нет.",
                kb([ib("🧾 Сбросить фильтр", f"col:all~{srt}:{page}")], [ib("🏠 Меню", "mn:home")]))
    n_pages = (len(filt) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    page = max(0, min(page, n_pages - 1))
    chunk = filt[page * CARDS_PER_PAGE:(page + 1) * CARDS_PER_PAGE]
    lines = [f"📚 <b>Коллекция</b>: всего {len(cards)} карт · в колодах: {in_deck_n} ({decks_line})",
             f"Фильтр: {col_filter_label(flt)} · Сортировка: {sort_label(srt)} · показано {len(filt)}",
             f"В бой идут первые {MAX_DECK} карт каждой колоды.", ""]
    rows = []
    for c in chunk:
        mark = f"✅{c['in_deck']}" if c["in_deck"] else "➖"
        line = card_line(c)
        lines.append(f"{mark} {line}")
        rows.append([ib(f"{mark} {line}"[:64], f"card:{c['id']}:{f}:{page}")])
    nav = []
    if page > 0:
        nav.append(ib("◀️", f"col:{f}:{page - 1}"))
    nav.append(ib("🎲 Получить карту", "getc"))
    if page < n_pages - 1:
        nav.append(ib("▶️", f"col:{f}:{page + 1}"))
    rows.append(nav)
    rows.append([ib(label, f"col:{code}~{srt}:{page}") for code, label in COL_FILTERS[:3]])
    rows.append([ib(label, f"col:{code}~{srt}:{page}") for code, label in COL_FILTERS[3:]] +
                [ib("🎭 Раса", f"colr:{f}"), ib("✨ Редк.", f"colq:{f}")])
    rows.append([ib("🔃 Сортировка", f"srt:{f}:{page}"),
                 ib("🃏 Колоды", "dk:1:0"), ib("🏠 Меню", "mn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def deck_screen(user_id: int, deck: int = 1, page: int = 0):
    # «🃏 Колоды» — до DECK_COUNT колод. Открывается как коллекция:
    # список ВСЕХ карт с пагинацией; тык на карту = положить/убрать
    # из текущей колоды (если карта была в другой колоде — перенесётся).
    # Состав текущей колоды выводится в тексте сообщения построчно.
    # ⭐ Приоритетная колода идёт в бой без вопроса при старте.
    deck = max(1, min(int(deck), DECK_COUNT))
    cards = await db_user_cards(user_id)
    if not cards:
        return ("🃏 <b>Карт пока нет.</b>\n\nПолучите первую карту — и добавьте её в колоду.",
                kb([ib("🎲 Получить карту", "getc")], [ib("🏠 Меню", "mn:home")]))
    pref = await user_flag(user_id, "pref_deck")
    decks = decks_of(cards)
    in_deck = decks[deck]
    star = " · ⭐ приоритетная" if pref == deck else ""
    lines = [f"🃏 <b>Колода №{deck}</b>: {len(in_deck)} карт{star}", ""]
    if in_deck:
        if len(in_deck) > MAX_DECK:
            lines.append(f"⚠️ Карт больше {MAX_DECK} — в бой пойдут только первые {MAX_DECK} (⚔️).")
        for i, c in enumerate(in_deck):
            mark = "⚔️" if i < MAX_DECK else "▪️"
            lines.append(f"{mark} {card_line(c)}")
    else:
        lines.append("Колода пуста — нажмите на карты в списке ниже, чтобы добавить их.")
    lines += ["", f"<b>Все карты</b> (нажатие = в колоду №{deck} / из колоды):"]
    rows = []
    tabs = []
    for n in range(1, DECK_COUNT + 1):
        cur = "🔹" if n == deck else ""
        star_n = "⭐" if pref == n else ""
        tabs.append(ib(f"{cur}{n}️⃣{star_n}·{len(decks[n])}", f"dk:{n}:0"))
    rows.append(tabs)
    rows.append([ib("⭐ Сделать приоритетной" if pref != deck else "⭐ Убрать приоритет",
                    f"pdk:{deck}")])
    n_pages = max(1, (len(cards) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE)
    page = max(0, min(int(page), n_pages - 1))
    for c in cards[page * CARDS_PER_PAGE:(page + 1) * CARDS_PER_PAGE]:
        if c["in_deck"] == deck:
            mark = "✅"
        elif c["in_deck"]:
            mark = f"🔸{c['in_deck']}"     # карта лежит в другой колоде
        else:
            mark = "➖"
        rows.append([ib(f"{mark} {card_line(c)}"[:64], f"dkg:{c['id']}:{deck}:{page}")])
    nav = []
    if page > 0:
        nav.append(ib("◀️", f"dk:{deck}:{page - 1}"))
    if page < n_pages - 1:
        nav.append(ib("▶️", f"dk:{deck}:{page + 1}"))
    nav.append(ib("📚 Коллекция", "col:all:0"))
    nav.append(ib("🏠 Меню", "mn:home"))
    rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("col:"))
async def cb_collection(call: CallbackQuery):
    _, f, page = call.data.split(":")
    text, markup = await collection_screen(call.from_user.id, f, int(page))
    await safe_answer(call)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("colr"))
async def cb_collection_races(call: CallbackQuery):
    # Выбор фильтра по расе (текущий фильтр~сортировка едут в «colr:{f}»)
    parts = call.data.split(":", 1)
    f = parts[1] if len(parts) > 1 else "all"
    _, _, srt = f.partition("~")
    srt = srt or "id"
    rows = []
    for i, race in enumerate(RACE_KEYS):
        rows.append([ib(f"{RACES[race]['emoji']} {race}", f"col:r{i}~{srt}:0")])
    rows.append([ib("🧾 Все карты", f"col:all~{srt}:0")])
    await safe_answer(call)
    await safe_edit(call, "🎭 <b>Коллекция — фильтр по расе:</b>",
                    InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("colq"))
async def cb_collection_rarities(call: CallbackQuery):
    # Выбор фильтра по редкости (сортировка сохраняется)
    parts = call.data.split(":", 1)
    f = parts[1] if len(parts) > 1 else "all"
    _, _, srt = f.partition("~")
    srt = srt or "id"
    rows = [[ib(rarity_label(k), f"col:q{i}~{srt}:0")] for i, k in enumerate(RARITY_KEYS)]
    rows.append([ib("🧾 Все карты", f"col:all~{srt}:0")])
    await safe_answer(call)
    await safe_edit(call, "✨ <b>Коллекция — фильтр по редкости:</b>",
                    InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("srt:"))
async def cb_sort_menu(call: CallbackQuery):
    # Меню критериев сортировки коллекции
    _, f, page = call.data.split(":")
    _, _, srt = f.partition("~")
    srt = srt or "id"
    cur = srt[:-1] if srt.endswith("-") else srt
    rows = []
    for code, label in SORTS:
        mark = (" 🔽" if srt.endswith("-") else " 🔼") if code == cur else ""
        rows.append([ib(f"{label}{mark}", f"srs:{code}:{f}:{page}")])
    rows.append([ib("⬅️ Назад к коллекции", f"col:{f}:{page}")])
    await safe_answer(call)
    await safe_edit(call, "🔃 <b>Сортировка коллекции</b>\n\nПервое нажатие на критерий — "
                          "по возрастанию (🔼), повторное нажатие на текущий — "
                          "по убыванию (🔽).",
                    InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("srs:"))
async def cb_sort_pick(call: CallbackQuery):
    # Выбран критерий: тот же — меняет направление, новый — по возрастанию
    _, code, f, page = call.data.split(":")
    flt, _, srt = f.partition("~")
    srt = srt or "id"
    if srt == code:
        new = code + "-"          # возрастание → убывание
    elif srt == code + "-":
        new = code                # убывание → снова возрастание
    else:
        new = code
    text, markup = await collection_screen(call.from_user.id, f"{flt}~{new}",
                                           int(page) if page.isdigit() else 0)
    await safe_answer(call)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("dk:"))
async def cb_deck(call: CallbackQuery):
    # «🃏 Колоды»: dk:{номер}:{страница}. Старый формат «dk:0» → колода №1.
    parts = call.data.split(":")
    deck = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 1
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    text, markup = await deck_screen(call.from_user.id, deck, page)
    await safe_answer(call)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("pdk:"))
async def cb_pref_deck(call: CallbackQuery):
    # ⭐ Приоритетная колода: с ней бой стартует без вопроса «какая колода?»
    n = int(call.data.split(":")[1])
    uid = call.from_user.id
    cur = await user_flag(uid, "pref_deck")
    new = 0 if cur == n else n
    await set_user_flag(uid, "pref_deck", new)
    text, markup = await deck_screen(uid, n, 0)
    await safe_answer(call, "Приоритет убран" if new == 0 else f"Колода №{n} — приоритетная ⭐")
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("dkg:"))
async def cb_deck_grid_toggle(call: CallbackQuery):
    # Тык на карту в списке колод: положить в текущую / убрать
    # (если была в другой колоде — переносится в текущую).
    _, cid_s, deck_s, page_s = call.data.split(":")
    cid, deck, page = int(cid_s), int(deck_s), int(page_s)
    row = await db_get_card(cid, call.from_user.id)
    if not row:
        await safe_answer(call, "Карта не найдена.", show_alert=True)
        return
    if row["in_deck"] == deck:
        await db_set_in_deck(cid, 0)
        msg = f"Убрано из колоды №{deck}"
    else:
        if DECK_LIMIT_ENABLED:
            cards = await db_user_cards(call.from_user.id)
            if sum(1 for c in cards if c["in_deck"] == deck) >= MAX_DECK:
                await safe_answer(call, f"В колоде №{deck} уже {MAX_DECK} карт!",
                                  show_alert=True)
                return
        await db_set_in_deck(cid, deck)
        msg = (f"Добавлено в колоду №{deck}" if not row["in_deck"]
               else f"Перенесено из колоды №{row['in_deck']} в №{deck}")
    await safe_answer(call, msg)
    text, markup = await deck_screen(call.from_user.id, deck, page)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("card:"))
async def cb_card(call: CallbackQuery):
    parts = call.data.split(":")
    cid, ctx, page = parts[1], parts[2], parts[3] if len(parts) > 3 else "0"
    row = await db_get_card(int(cid), call.from_user.id)
    await safe_answer(call)
    if not row:
        await safe_edit(call, "Карта не найдена.", kb([ib("⬅️ Назад", back_cb(ctx, page))]))
        return
    await safe_edit(call, card_text(row), card_kb(row, ctx, page))


@router.callback_query(F.data.startswith("dks:"))
async def cb_deck_picker(call: CallbackQuery):
    # «📥 В колоду…» — в какую из колод положить карту (или убрать)
    _, cid, ctx, page = call.data.split(":")
    row = await db_get_card(int(cid), call.from_user.id)
    await safe_answer(call)
    if not row:
        await safe_edit(call, "Карта не найдена.", kb([ib("⬅️ Назад", back_cb(ctx, page))]))
        return
    rows = []
    for n in range(1, DECK_COUNT + 1):
        mark = " ✅" if row["in_deck"] == n else ""
        rows.append([ib(f"🃏 Колода №{n}{mark}", f"dka:{cid}:{n}:{ctx}:{page}")])
    if row["in_deck"]:
        rows.append([ib("📤 Убрать из колоды", f"dka:{cid}:0:{ctx}:{page}")])
    rows.append([ib("⬅️ Назад", f"card:{cid}:{ctx}:{page}")])
    await safe_edit(call, f"📥 В какую колоду положить «{disp_name(row)}»?",
                    InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("dka:"))
async def cb_deck_apply(call: CallbackQuery):
    # Положить карту в колоду №n (0 = убрать из колод)
    _, cid, n, ctx, page = call.data.split(":")
    cid_i, n_i = int(cid), int(n)
    row = await db_get_card(cid_i, call.from_user.id)
    if not row:
        await safe_answer(call, "Карта не найдена.", show_alert=True)
        return
    if n_i > 0 and DECK_LIMIT_ENABLED and row["in_deck"] != n_i:
        cards = await db_user_cards(call.from_user.id)
        if sum(1 for c in cards if c["in_deck"] == n_i) >= MAX_DECK:
            await safe_answer(call, f"В колоде №{n_i} не может быть больше {MAX_DECK} карт!",
                              show_alert=True)
            return
    await db_set_in_deck(cid_i, n_i)
    row = await db_get_card(cid_i, call.from_user.id)
    await safe_answer(call, "Убрано из колоды" if n_i == 0 else f"В колоде №{n_i}")
    await safe_edit(call, card_text(row), card_kb(row, ctx, page))


@router.callback_query(F.data.startswith("ttc:"))
async def cb_title_cancel(call: CallbackQuery):
    # «⬅️ Отменить» на экране ввода титула — сбрасываем ожидание
    _, cid, ctx, page = call.data.split(":")
    await set_user_flag(call.from_user.id, "awaiting_title", 0)
    row = await db_get_card(int(cid), call.from_user.id)
    await safe_answer(call, "Ввод титула отменён")
    if row:
        await safe_edit(call, card_text(row), card_kb(row, ctx, page))
    else:
        text, markup = await ctx_screen(call.from_user.id, ctx, page)
        await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("tt:"))
async def cb_title_ask(call: CallbackQuery):
    # «📜 Даровать титул» (карта L3+): ждём текст титула обычным сообщением.
    # Состояние — в users.awaiting_title (id карты), как у паролей (без FSM).
    _, cid, ctx, page = call.data.split(":")
    row = await db_get_card(int(cid), call.from_user.id)
    if not row:
        await safe_answer(call, "Карта не найдена.", show_alert=True)
        return
    if row["level"] < TITLE_MIN_LEVEL:
        await safe_answer(call, f"Титул даруется с {TITLE_MIN_LEVEL}-го уровня "
                                f"(у карты сейчас L{row['level']}).", show_alert=True)
        return
    await set_user_flag(call.from_user.id, "awaiting_title", int(cid))
    await safe_answer(call)
    await safe_edit(call, f"📜 <b>Дарование титула</b> · {disp_name(row)}\n\n"
                          "Отправьте титул обычным сообщением (до 32 символов; "
                          "символы «&lt;» «&gt;» «&amp;» будут вырезаны).\n"
                          "Для отмены отправьте /cancel.",
                    kb([ib("⬅️ Отменить", f"ttc:{cid}:{ctx}:{page}")]))


@router.callback_query(F.data.startswith("tu:"))
async def cb_title_use(call: CallbackQuery):
    # 🎭 Вкл/выкл «титул как имя» (бой, колоды, коллекция)
    _, cid, ctx, page = call.data.split(":")
    row = await db_get_card(int(cid), call.from_user.id)
    if not row:
        await safe_answer(call, "Карта не найдена.", show_alert=True)
        return
    if not ((row.get("title") or "").strip() if hasattr(row, "get") else ""):
        await safe_answer(call, "Сначала даруйте карте титул.", show_alert=True)
        return
    new = 0 if row.get("use_title") else 1
    await db_set_use_title(int(cid), new, call.from_user.id)
    row = await db_get_card(int(cid), call.from_user.id)
    await safe_answer(call)
    await safe_edit(call, card_text(row), card_kb(row, ctx, page))


@router.callback_query(F.data.startswith("dkt:"))
async def cb_toggle_deck(call: CallbackQuery):
    # Устаревший колбэк старых сообщений: быстрый тумблер колоды №1.
    # Новые экраны используют dks:/dka:/dkg:.
    parts = call.data.split(":")
    cid, ctx, page = parts[1], parts[2], parts[3] if len(parts) > 3 else "0"
    row = await db_get_card(int(cid), call.from_user.id)
    if not row:
        await safe_answer(call, "Карта не найдена.", show_alert=True)
        return
    if row["in_deck"]:
        await db_set_in_deck(row["id"], 0)
        await safe_answer(call, "Убрано из колоды")
    else:
        if DECK_LIMIT_ENABLED:
            cards = await db_user_cards(call.from_user.id)
            if sum(1 for c in cards if c["in_deck"] == 1) >= MAX_DECK:
                await safe_answer(call, f"В колоде не может быть больше {MAX_DECK} карт!", show_alert=True)
                return
        await db_set_in_deck(row["id"], 1)
        await safe_answer(call, "Добавлено в колоду №1")
    row = await db_get_card(int(cid), call.from_user.id)
    await safe_edit(call, card_text(row), card_kb(row, ctx, page))


@router.callback_query(F.data.startswith("dkd:"))
async def cb_delete_ask(call: CallbackQuery):
    parts = call.data.split(":")
    cid, ctx, page = parts[1], parts[2], parts[3] if len(parts) > 3 else "0"
    await safe_answer(call)
    await safe_edit(call, "🗑 <b>Сбросить карту навсегда?</b>",
                    kb([ib("✅ Да, сбросить", f"dkx:{cid}:{ctx}:{page}"),
                        ib("⬅️ Отмена", f"card:{cid}:{ctx}:{page}")]))


@router.callback_query(F.data.startswith("dkx:"))
async def cb_delete_yes(call: CallbackQuery):
    parts = call.data.split(":")
    cid, ctx, page = parts[1], parts[2], parts[3] if len(parts) > 3 else "0"
    await db_delete_card(int(cid), call.from_user.id)
    await safe_answer(call, "Карта сброшена")
    text, markup = await ctx_screen(call.from_user.id, ctx, page)
    await safe_edit(call, text, markup)


# =====================================================================
# ХЕНДЛЕРЫ: получение карты
# =====================================================================

async def get_new_card(user_id: int):
    # Генерация новой карты. Ограничение колоды сейчас ОТКЛЮЧЕНО
    # (DECK_LIMIT_ENABLED=False) — по заданию кнопка работает без лимита.
    if DECK_LIMIT_ENABLED:
        cards = await db_user_cards(user_id)
        if len(cards) >= MAX_DECK:
            return (f"🃏 Колода полна (максимум {MAX_DECK} карт).\nСбросьте лишние карты.",
                    kb([ib("📚 Коллекция", "col:all:0")], [ib("🏠 Меню", "mn:home")]))
    # Pity-система (вариант 4): 40 карт подряд без «редкой+» → следующая
    # гарантированно 🟦 редкая или выше
    pity = await user_flag(user_id, "pity")
    if pity >= PITY_THRESHOLD:
        d = generate_card_data(rarity=roll_rarity(pity=True))
        pity = 0
    else:
        d = generate_card_data()
        pity = 0 if rarity_rank(d["rarity"]) >= 2 else pity + 1
    # УНИКАЛЬНОСТЬ имени (правка заказчика): до 8 попыток подобрать полное
    # имя, которого у игрока ещё нет. Если все попытки дали тёзок (пул расы
    # почти исчерпан) — добавляем римский суффикс: «Торгрим Железнобород II».
    for _ in range(8):
        if not await db_name_taken(user_id, d["name"], d["surname"]):
            break
        d = generate_card_data(rarity=d["rarity"])
    else:
        # Все попытки дали тёзок — подбираем первый свободный римский суффикс
        # (проверяем именно итоговое имя, чтобы «II» не повторился дважды)
        n = 2
        while await db_name_taken(user_id, d["name"], f"{d['surname']} {roman_numeral(n)}"):
            n += 1
        d["surname"] = f"{d['surname']} {roman_numeral(n)}"
    cid = await db_add_card(user_id, d)
    await set_user_flag(user_id, "pity", pity)
    row = await db_get_card(cid, user_id)
    if d["rarity"] == "legendary":
        fanf = "🎉🟨 <b>ЛЕГЕНДАРНАЯ КАРТА!</b> 🟨🎉\n\n"
    elif d["rarity"] == "epic":
        fanf = "🌟 <b>Эпическая карта!</b>\n\n"
    elif d["rarity"] == "rare":
        fanf = "✨ <b>Редкая карта!</b>\n\n"
    else:
        fanf = "🎲 <b>Новая карта получена!</b>\n\n"
    text = (fanf + card_text(row) +
            f"\n\n🍀 Pity: {pity}/{PITY_THRESHOLD} до гарантированной 🟦 Редкой."
            "\n💡 Колоды пусты — откройте «🃏 Колода» и нажмите на карты, "
            "которые возьмёте в бой (колод до 3).")
    markup = kb([ib("🎲 Ещё одну", "getc")],
                [ib("🃏 Колода", "dk:1:0"), ib("📚 Коллекция", "col:all:0")],
                [ib("🏠 Меню", "mn:home")])
    return text, markup


@router.callback_query(F.data == "getc")
async def cb_get_card(call: CallbackQuery):
    text, markup = await get_new_card(call.from_user.id)
    await safe_answer(call)
    await safe_edit(call, text, markup)


# =====================================================================
# ХЕНДЛЕРЫ: бой — подготовка, старт, завершение
# =====================================================================

async def battle_setup_screen(user_id: int, deck: int = 0, force_pick: bool = False):
    # Экран подготовки к бою. Колод теперь до DECK_COUNT:
    #  • колода передана явно (deck>0) — сразу выбор раскладки;
    #  • не передана — берём ⭐приоритетную; если её нет и колода одна —
    #    берём её без вопросов; иначе спрашиваем, какая колода идёт в бой.
    all_cards = await db_user_cards(user_id)
    if not all_cards:
        return ("⚔️ <b>У вас пока нет карт!</b>\n\nПолучите первую карту — "
                "и не забудьте добавить её в колоду.",
                kb([ib("🎲 Получить карту", "getc")], [ib("🏠 Меню", "mn:home")]))
    decks = decks_of(all_cards)
    non_empty = [n for n in sorted(decks) if decks[n]]
    if not non_empty:
        return ("⚔️ <b>Все колоды пусты!</b>\n\nОткройте «🃏 Колода» и нажмите на карты, "
                "которые возьмёте в бой.",
                kb([ib("🃏 Колоды", "dk:1:0")], [ib("🏠 Меню", "mn:home")]))
    if deck <= 0 and not force_pick:
        pref = await user_flag(user_id, "pref_deck")
        if pref in non_empty:
            deck = pref
        elif len(non_empty) == 1:
            deck = non_empty[0]
    if deck <= 0:
        # Колода не определена — спрашиваем каждый раз (или по «🔁 Другая колода»)
        rows = [[ib(f"🃏 Колода №{n} — {len(decks[n])} карт", f"bds:{n}")] for n in non_empty]
        rows.append([ib("🏠 Меню", "mn:home")])
        return ("⚔️ <b>Подготовка к бою</b>\n\nКакая колода идёт в бой? "
                "(Чтобы не спрашивал каждый раз — отметь колоду ⭐ в «🃏 Колода».)",
                InlineKeyboardMarkup(inline_keyboard=rows))
    cards = decks[deck]
    battle_cards = cards[:MAX_DECK]
    note = (f"\n⚠️ В колоде больше {MAX_DECK} карт — в бой пойдут первые {MAX_DECK}."
            if len(cards) > MAX_DECK else "")
    text = (f"⚔️ <b>Подготовка к бою</b> · колода №{deck}\n\n"
            f"Карт в бою: {len(battle_cards)}.{note}\n\n"
            "Выберите расстановку (<b>всего 4 клетки</b>, максимум 3 на линию, обе линии обязательны):\n"
            "• <b>3 фронта / 1 тыл</b> — фронт 1,2,3; тыл 2\n"
            "• <b>2 фронта / 2 тыла</b> — обе линии: 1 и 3\n"
            "• <b>1 фронт / 3 тыла</b> — фронт 2; тыл 1,2,3")
    rows = [[ib("🛡 3 фронта / 1 тыл", f"bl:31:{deck}")],
            [ib("⚖️ 2 фронта / 2 тыла", f"bl:22:{deck}")],
            [ib("🏹 1 фронт / 3 тыла", f"bl:13:{deck}")]]
    if len(non_empty) > 1:
        rows.append([ib("🔁 Другая колода", "bds:0")])
    rows.append([ib("🏠 Меню", "mn:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("bds:"))
async def cb_battle_deck_select(call: CallbackQuery):
    # Выбор колоды перед боем (bds:0 = «🔁 Другая колода» → снова список)
    n = int(call.data.split(":")[1])
    text, markup = await battle_setup_screen(call.from_user.id, n, force_pick=(n == 0))
    await safe_answer(call)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("bl:"))
async def cb_battle_start(call: CallbackQuery):
    parts = call.data.split(":")
    layout_key = parts[1]
    # Старые кнопки «bl:31» (без колоды) → колода №1
    deck = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    user_id = call.from_user.id
    if layout_key not in LAYOUTS:
        await safe_answer(call, "Неизвестная раскладка.", show_alert=True)
        return
    if await db_load_battle(user_id):
        await safe_answer(call, "У вас уже идёт бой!", show_alert=True)
        return
    cards = await db_battle_cards(user_id, deck)
    if not cards:
        await safe_answer(call, "Колода пуста! Добавьте карты: «🃏 Колода».", show_alert=True)
        return
    state = create_battle(cards, layout_key)
    log_add(state, f"⚔️ Бой начался (колода №{deck})! Нажмите на клетку поля, "
                   "чтобы действовать. Удачи!")
    await db_save_battle(user_id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


async def load_active_battle(call: CallbackQuery):
    # Загружает активный бой или объясняет пользователю, что боя нет
    state = await db_load_battle(call.from_user.id)
    if state is None:
        await safe_answer(call, "У вас нет активного боя.", show_alert=True)
        return None
    if state["over"]:
        await safe_answer(call, "Бой уже завершён.", show_alert=True)
        return None
    return state


async def finish_player_action(call: CallbackQuery, state: dict):
    # Единый финал любого действия игрока:
    # ход противника (ИИ сразу) → если бой завершён, подводим итоги
    # (возможно, отложенно — пока не распределены очки роста) → рендер.
    advance_turn(state)
    if state["over"]:
        await end_battle(call.from_user.id, state)
    else:
        await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


async def end_battle(user_id: int, state: dict) -> bool:
    # Завершение боя в три этапа. Возвращает True, если бой окончательно
    # закрыт (карты синхронизированы с БД), False — если ждём, пока игрок
    # распределит очки роста через кнопку «🌟 Уровень!».
    #
    # Этап 1: бонусный опыт за исход боя (начисляется один раз).
    if not state.get("bonus_applied"):
        bonus = {"victory": WIN_XP, "defeat": LOSE_XP, "draw": LOSE_XP, "flee": 0}.get(state["result"], 0)
        for u in alive_units(state, "player"):
            u["bonus_xp"] = bonus
            u["xp"] += bonus
            while u["xp"] >= xp_to_next(u["level"]):
                u["xp"] -= xp_to_next(u["level"])
                u["level"] += 1
                u["pending_levels"] = u.get("pending_levels", 0) + 1
        state["bonus_applied"] = True

    # Этап 2: есть нераспределённые очки роста — бой остаётся в БД,
    # игрок распределяет их (рост выбирает сам), затем финал повторится.
    if pending_levels_total(state) > 0:
        await db_save_battle(user_id, state)
        return False

    # Этап 3: переносим результаты в БД карт:
    #  - погибшие карты НЕ исчезают навсегда (правка заказчика): они лишь
    #    выбывают до конца текущего боя, а после боя возвращаются в
    #    коллекцию восстановленными (hp = hp_max; при HEAL_AFTER_BATTLE=
    #    False — «выживают на 1 hp»);
    #  - выжившие лечатся, если HEAL_AFTER_BATTLE=True;
    #  - КЗ восстанавливается к базовому (штраф отхода — только на бой).
    units = [u for u in all_units(state, "player") if u.get("card_id") is not None]
    # (призванные големы card_id не имеют — после боя они просто исчезают)
    lines = ["🎖 <b>Итоги боя</b>"]
    lost = 0
    for u in units:
        dead = u["hp"] <= 0
        if dead:
            lost += 1
        total_xp = u["xp_gained"] + (u.get("bonus_xp", 0) if not dead else 0)
        if total_xp > 0:
            lines.append(f"✳️ {u['name']} {u['surname']}: +{total_xp} опыта")
        if u["level"] > u["start_level"]:
            lines.append(f"⬆️ {u['name']} → <b>уровень {u['level']}!</b>")
        if dead:
            hp = u["hp_max"] if HEAL_AFTER_BATTLE else 1
            lines.append(f"💀 {u['name']} {u['surname']} пал в бою — "
                         f"карта осталась в коллекции и восстановится к следующему бою")
        else:
            hp = u["hp_max"] if HEAL_AFTER_BATTLE else u["hp"]
        await db_sync_card_after_battle(u, hp)
    await db_delete_battle(user_id)
    if HEAL_AFTER_BATTLE and lost < len(units):
        lines.append("❤️ Выжившие карты вылечены.")
    state["rewards_text"] = "\n".join(lines)
    return True


# =====================================================================
# ХЕНДЛЕРЫ: бой — карта-кнопки и действия
# =====================================================================

@router.callback_query(F.data == "bt:view")
async def cb_battle_view(call: CallbackQuery):
    state = await db_load_battle(call.from_user.id)
    await safe_answer(call)
    if state is None:
        await safe_edit(call, "У вас нет активного боя.\n\n" + MENU_TEXT, None)
        return
    await safe_edit(call, battle_text(state), kb_battle(state))


@router.callback_query(F.data == "bt:again")
async def cb_battle_again(call: CallbackQuery):
    st = await db_load_battle(call.from_user.id)
    if st:
        # Бой ещё в БД: либо не распределены очки роста, либо финал
        # прервался перезапуском — показываем экран завершения/очков.
        if st["over"] and pending_levels_total(st) == 0:
            await end_battle(call.from_user.id, st)
        await safe_answer(call)
        await safe_edit(call, battle_text(st), kb_battle(st))
        return
    text, markup = await battle_setup_screen(call.from_user.id)
    await safe_answer(call)
    await safe_edit(call, text, markup)


@router.callback_query(F.data.startswith("c:"))
async def cb_cell_click(call: CallbackQuery):
    # Клик по клетке карты — главная точка входа интерфейса
    state = await load_active_battle(call)
    if not state:
        return
    _, line, pos_s = call.data.split(":")
    pos = int(pos_s)
    if line not in ROWS or not cell_exists(state, line, pos):
        await safe_answer(call, "Здесь нет клетки.", show_alert=True)
        return

    pend = state.get("pending")
    if pend:
        await resolve_pending_cell(call, state, pend, line, pos)
        return

    u = cell_unit(state, line, pos)
    w = get_wall(state, line, pos)
    if u is not None:
        if u["side"] == "enemy":
            # Клик по вражескому юниту: предложить атаковать — выбрать кем и как
            await show_attacker_choice(call, state, line, pos, u)
        else:
            # Клик по своему юниту: меню ДП (+ магия для заклинателей)
            await show_unit_menu(call, state, u)
    elif w is not None:
        # Клик по стене: вражескую можно ломать, своя — показать информацию
        if w["owner"] == "enemy":
            await show_wall_choice(call, state, line, pos, w)
        else:
            await safe_answer(call, f"🧱 Это ваша стена: HP {w['hp']}, КЗ {w['armor']}. "
                              f"Она даёт +3 КЗ вашему тылу и служит заслоном.", show_alert=True)
    else:
        # Клик по пустой клетке: захват = выбрать, кем переместиться
        await show_move_choice(call, state, line, pos)


async def resolve_pending_cell(call: CallbackQuery, state: dict, pend: dict,
                               line: str, pos: int):
    # Обработка клика по карте, когда включён режим «следующий клик — цель»
    act = pend["act"]
    p_side, u, u_line, _ = find_unit(state, pend["uid"])
    if u is None or p_side != "player":
        state["pending"] = None
        await db_save_battle(call.from_user.id, state)
        await safe_answer(call)
        await safe_edit(call, battle_text(state), kb_battle(state))
        return
    # Для высадки карта ДОЛЖНА быть в колоде; для остальных режимов — на доске
    if act == "place":
        if u_line != "deck":
            state["pending"] = None
            await db_save_battle(call.from_user.id, state)
            await safe_answer(call, "Эта карта уже не в колоде.", show_alert=True)
            await safe_edit(call, battle_text(state), kb_battle(state))
            return
    elif u_line in (None, "deck", "graveyard"):
        state["pending"] = None
        await db_save_battle(call.from_user.id, state)
        await safe_answer(call)
        await safe_edit(call, battle_text(state), kb_battle(state))
        return

    target = cell_unit(state, line, pos)
    wall = get_wall(state, line, pos)

    if act in ("attack", "stealth"):
        if target is None and not (wall is not None and act == "attack" and wall["owner"] != "player"):
            await safe_answer(call, "Пустая клетка — атаковать некого. "
                              "Для захвата клетки используйте 🚶 Перемещение.", show_alert=True)
            return
        if target is not None and target["side"] == "player":
            # По заданию: бить союзника нельзя (свою стену — тоже)
            await safe_answer(call, "❌ Атаковать союзника нельзя! Выберите вражеского юнита.", show_alert=True)
            return
        if wall is not None and wall["owner"] == "player":
            await safe_answer(call, "Это ваша собственная стена.", show_alert=True)
            return
        stealth = act == "stealth"
        if stealth and wall is not None:
            await safe_answer(call, "🗡 Скрытая атака бьёт только по юнитам тыла (В2).", show_alert=True)
            return
        if stealth and not can_stealth_attack(u, line):
            await safe_answer(call, "🗡 Скрытая атака бьёт только по тылу врага (В2).", show_alert=True)
            return
        if not stealth and not can_normal_attack(u, line):
            await safe_answer(call, "⚔️ Обычная атака не достаёт эту линию: нужен фронт врага (В1) "
                              "или своя/соседняя линия. Попробуйте переместиться ближе.", show_alert=True)
            return
        err = attacker_error(u, stealth)
        if err:
            await safe_answer(call, err, show_alert=True)
            return
        state["pending"] = None
        perform_attack(state, "player", u, line, pos, stealth=stealth)
        await finish_player_action(call, state)
        return

    if act == "spell":
        # Режим «следующий клик — цель заклинания»
        sp = SPELLS[pend["spell"]]
        err = spell_target_error(state, "player", sp, line, pos, target, wall)
        if err:
            await safe_answer(call, err, show_alert=True)
            return
        state["pending"] = None
        spec = ({"kind": "unit", "uid": target["uid"]} if sp["target"] in ("enemy_unit", "ally_unit")
                else {"kind": "cell", "line": line, "pos": pos})
        cast_spell(state, "player", u, pend["spell"], spec)
        await finish_player_action(call, state)
        return

    if act == "move":
        if target is not None or wall is not None:
            await safe_answer(call, "Клетка занята — переместиться на неё нельзя.", show_alert=True)
            return
        if not can_move_to(state, u, line, pos):
            await safe_answer(call, "Сюда не добраться: нужна пустая клетка на своей или "
                              "соседней линии (В2↔В1↔С1↔С2).", show_alert=True)
            return
        state["pending"] = None
        perform_move(state, "player", u, line, pos)
        await finish_player_action(call, state)
        return

    if act == "place":
        if ROW_SIDE[line] != "player":
            await safe_answer(call, "Высаживать карты можно только на свои линии (С1/С2). "
                              "Дальше — только перемещением!", show_alert=True)
            return
        if target is not None or wall is not None:
            await safe_answer(call, "Клетка занята.", show_alert=True)
            return
        if u_line != "deck":
            await safe_answer(call, "Эта карта уже не в колоде.", show_alert=True)
            return
        state["pending"] = None
        perform_place(state, "player", line, pos, u)
        await finish_player_action(call, state)


async def show_attacker_choice(call: CallbackQuery, state: dict, line: str, pos: int, tgt: dict):
    # «Предлагает атаковать — можно выбрать кем» (клик по вражескому юниту).
    # Для каждого своего юнита показываем доступные виды удара:
    # ⚔️ обычная (фронт В1 всегда + своя/соседняя линия) и 🗡 скрытая (только В2).
    btns = []
    for _, _, a in board_units(state, "player"):
        if a["end"] <= 0:
            continue
        if can_normal_attack(a, line) and (a["cls"] != CLS_MAGE or a["mana"] >= MAGE_ATTACK_MANA):
            btns.append(ib(f"⚔️ {a['name']} — атака", f"EA:{a['uid']}:{tgt['uid']}"))
        if can_stealth_attack(a, line) and (a["cls"] != CLS_MAGE or a["mana"] >= MAGE_STEALTH_MANA):
            cost = f" ({MAGE_STEALTH_MANA}🔮)" if a["cls"] == CLS_MAGE else ""
            btns.append(ib(f"🗡 {a['name']} — скрытая{cost}", f"SA:{a['uid']}:{tgt['uid']}"))
    if not btns:
        await safe_answer(call, "Никто из ваших юнитов сейчас не может достать эту цель "
                          "(истощение, не хватает манны или линии не в досягаемости).", show_alert=True)
        return
    text = (f"🎯 <b>Цель: {ROW_LABEL[line]}:{pos}</b>\n{unit_info(tgt)}\n\n"
            f"Кем и как атаковать?")
    rows = [[b] for b in btns]
    rows.append([ib("✖ Отменить", "X")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def show_move_choice(call: CallbackQuery, state: dict, line: str, pos: int):
    # Клик по пустой клетке: ЗАХВАТ = перемещение (стоимость 1⚡, без манны).
    # Если клетка своя и в колоде есть карты — дополнительно предлагаем высадку.
    cands = [u for _, _, u in board_units(state, "player") if can_move_to(state, u, line, pos)]
    can_place = (ROW_SIDE[line] == "player" and bool(state["player"]["deck"]))
    if not cands and not can_place:
        await safe_answer(call, "Сюда никто не может переместиться: нужна пустая клетка на "
                          "своей или соседней линии и выносливость ≥ 1.", show_alert=True)
        return
    text = (f"🚩 <b>Клетка {ROW_LABEL[line]}:{pos} пуста.</b>\n"
            f"Что сделать? Перемещение стоит 1⚡ и не тратит манну.")
    rows = []
    for u in cands:
        rows.append([ib(f"🚶 {u['name']} ({ROW_LABEL[u['line']]}:{u['pos']} → сюда)",
                        f"MV:{u['uid']}:{line}:{pos}")])
    if can_place:
        rows.append([ib("📥 Высадить карту из колоды сюда", f"PCP:{line}:{pos}")])
    rows.append([ib("✖ Отменить", "X")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("PCP:"))
async def cb_place_pick(call: CallbackQuery):
    # «Высадить карту сюда» из экрана пустой клетки → выбор карты из колоды
    state = await load_active_battle(call)
    if not state:
        return
    _, line, pos_s = call.data.split(":")
    pos = int(pos_s)
    if ROW_SIDE[line] != "player" or not cell_exists(state, line, pos) \
            or cell_unit(state, line, pos) is not None:
        await safe_answer(call, "Сюда нельзя поставить карту.", show_alert=True)
        return
    if not state["player"]["deck"]:
        await safe_answer(call, "Колода пуста.", show_alert=True)
        return
    await show_cards_for_cell(call, state, line, pos)


async def show_unit_menu(call: CallbackQuery, state: dict, u: dict):
    # Клик по своему юниту — выбор одного из ДП
    if u["end"] <= 0:
        text = (unit_info(u) + "\n\n💤 <b>Истощение!</b> Выносливость на нуле: КЗ=0 и карта "
                "не может действовать (даже отступить — отход тоже требует выносливость).")
        markup = kb([ib("⬅️ Назад", "bt:view")])
    else:
        atk_cost = f" ({MAGE_ATTACK_MANA}🔮)" if u["cls"] == CLS_MAGE else ""
        stl_cost = f" ({MAGE_STEALTH_MANA}🔮)" if u["cls"] == CLS_MAGE else ""
        text = unit_info(u) + "\n\nВыберите действие персонажа (одно на весь ход):"
        rows = [
            [ib(f"⚔️ Атака{atk_cost}", f"A:{u['uid']}"), ib(f"🗡 Скрытая атака{stl_cost}", f"S:{u['uid']}")],
            [ib("🚶 Переместиться (захват клетки)", f"M:{u['uid']}")],
            [ib("🛡 Защита (КЗ +50%)", f"D:{u['uid']}"), ib("↩️ Отход (в колоду, КЗ −50%)", f"R:{u['uid']}")],
        ]
        if u["cls"] == CLS_MAGE:
            # Маги дополнительно умеют кастовать заклинания (✨ Магия)
            rows.append([ib(f"✨ Магия (известно: {len(known_spells(u))})", f"SP:{u['uid']}")])
        rows.append([ib("✖ Отменить", "X")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_answer(call)
    await safe_edit(call, text, markup)


async def show_cards_for_cell(call: CallbackQuery, state: dict, line: str, pos: int):
    # Клик по своей пустой клетке — выбрать карту из колоды для высадки
    text = (f"📥 Клетка <b>{ROW_LABEL[line]}:{pos}</b> — выберите карту из колоды "
            f"(высадка не тратит выносливость):")
    rows = []
    for u in state["player"]["deck"]:
        rows.append([ib(unit_line(u)[:64], f"PC:{line}:{pos}:{u['uid']}")])
    rows.append([ib("✖ Отменить", "X")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


# ---------- ДП из меню своего юнита ----------

async def get_board_unit(call: CallbackQuery, state: dict, uid: int):
    # Достаёт своего юнита с доски или объясняет, почему не получилось
    side, u, line, pos = find_unit(state, uid)
    if u is None or side != "player" or line in (None, "deck", "graveyard"):
        await safe_answer(call, "Карта не на поле.", show_alert=True)
        return None
    return u


@router.callback_query(F.data.startswith("A:"))
async def cb_enter_attack_mode(call: CallbackQuery):
    # «Атака» из меню юнита: возвращаем на поле, следующий клик — цель
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    err = attacker_error(u, stealth=False)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    # Есть ли хоть одна доступная цель для обычной атаки?
    has_target = any(can_normal_attack(u, tl)
                     for tl, _, t in board_units(state, "enemy"))
    if not has_target:
        await safe_answer(call, "Нет целей, которых достанет обычная атака "
                          "(фронт В1 или своя/соседняя линия).", show_alert=True)
        return
    state["pending"] = {"act": "attack", "uid": uid}
    await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


@router.callback_query(F.data.startswith("S:"))
async def cb_stealth_ask(call: CallbackQuery):
    # «Скрытая атака» из меню юнита: сначала спрашиваем подтверждение
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    err = attacker_error(u, stealth=True)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    enemy_rear = row_of("enemy", "rear")
    if not any(tl == enemy_rear for tl, _, _ in board_units(state, "enemy")):
        await safe_answer(call, "Во вражеском тылу (В2) нет целей.", show_alert=True)
        return
    await safe_answer(call)
    text = (f"🗡 <b>Скрытая атака</b>\n{unit_info(u)}\n\n"
            f"Карта ударит по тылу врага (В2) через заслон фронта — попадание ниже обычного. "
            f"Уверены, что хотите совершить скрытую атаку?")
    await safe_edit(call, text, kb([ib("✅ Да, уверен(а)", f"SY:{uid}")], [ib("✖ Нет", "X")]))


@router.callback_query(F.data.startswith("SY:"))
async def cb_stealth_yes(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    state["pending"] = {"act": "stealth", "uid": uid}
    await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


@router.callback_query(F.data.startswith("M:"))
async def cb_enter_move_mode(call: CallbackQuery):
    # «Перемещение» из меню юнита: возвращаем на поле, следующий клик — клетка
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    if u["end"] <= 0:
        await safe_answer(call, "💤 Карта истощена и не может действовать!", show_alert=True)
        return
    has_cell = any(can_move_to(state, u, line, pos)
                   for line in ROWS for pos in row_layout(state, line))
    if not has_cell:
        await safe_answer(call, "Нет доступных клеток: своя и соседние линии заняты "
                          "или не существуют.", show_alert=True)
        return
    state["pending"] = {"act": "move", "uid": uid}
    await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


@router.callback_query(F.data.startswith("D:"))
async def cb_defend(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    if u["end"] <= 0:
        await safe_answer(call, "💤 Карта истощена и не может действовать!", show_alert=True)
        return
    perform_defend(state, "player", u)
    await finish_player_action(call, state)


@router.callback_query(F.data.startswith("R:"))
async def cb_retreat(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    if u["end"] <= 0:
        await safe_answer(call, "💤 Карта истощена — даже отход требует выносливость!", show_alert=True)
        return
    perform_retreat(state, "player", u)
    await finish_player_action(call, state)


# ---------- магия: книга заклинаний, выбор цели, каст ----------

@router.callback_query(F.data.startswith("UM:"))
async def cb_unit_menu_back(call: CallbackQuery):
    # Возврат в меню юнита (например, с экрана книги заклинаний)
    state = await load_active_battle(call)
    if not state:
        return
    u = await get_board_unit(call, state, int(call.data.split(":")[1]))
    if u is None:
        return
    await show_unit_menu(call, state, u)


@router.callback_query(F.data == "SB")
async def cb_spellbook(call: CallbackQuery):
    # «✨ Магия» с основной карты боя: сначала выбор КАСТЕРА (у каждого
    # видны манна, выносливость и известные заклинания), затем книга
    # выбранного мага (SP:) и выбор цели (SPL: — как раньше).
    state = await load_active_battle(call)
    if not state:
        return
    casters = [u for _, _, u in board_units(state, "player") if u["cls"] == CLS_MAGE]
    if not casters:
        await safe_answer(call, "На поле нет заклинателей 🔮 — сначала высадите "
                                "мага из колоды (клетка на своей линии).", show_alert=True)
        return
    lines = ["✨ <b>Книга заклинаний</b> — выберите кастера:", ""]
    rows = []
    for u in casters:
        known = known_spells(u)
        fl = unit_flags(u)
        lines.append(f"• {ROW_LABEL[u['line']]}:{u['pos']} <b>{u['name']} {u['surname']}</b> "
                     f"L{u['level']} — 🔮 {u['mana']}/{u['mana_max']}, ⚡ {u['end']}/{u['end_max']}"
                     + (f" {fl}" if fl else ""))
        if known:
            lines.append("  знает: " + " · ".join(
                f"{SPELLS[s]['emoji']} {SPELLS[s]['name']}" + ("" if spell_available(u, SPELLS[s]) else " 🔒")
                for s in known))
        else:
            lines.append("  заклинаний пока не знает (открываются с уровнем/редкостью).")
        rows.append([ib(f"🔮 {u['name']} · 🔮{u['mana']} ⚡{u['end']} · ✨{len(known)}",
                        f"SP:{u['uid']}")])
    rows.append([ib("⬅️ Назад на поле", "bt:view")])
    await safe_answer(call)
    await safe_edit(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("SP:"))
async def cb_spell_menu(call: CallbackQuery):
    # Книга заклинаний мага: что открыто по уровню и редкости
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = await get_board_unit(call, state, uid)
    if u is None:
        return
    if u["cls"] != CLS_MAGE:
        await safe_answer(call, "Кастовать могут только заклинатели 🔮.", show_alert=True)
        return
    known = known_spells(u)
    if not known:
        await safe_answer(call, f"Заклинания откроются с ростом уровня и редкости карты "
                          f"(сейчас L{u['level']}, {rarity_label(u.get('rarity', 'common'))}).",
                          show_alert=True)
        return
    await safe_answer(call)
    text = (unit_info(u) + "\n\n✨ <b>Книга заклинаний</b> — каст тратит ВЕСЬ пул манны, "
            "1⚡ и всё действие хода. 🔒 — пока недоступно (уровень/редкость/манна):")
    rows = []
    for sid in known:
        sp = SPELLS[sid]
        lock = "" if spell_available(u, sp) else "🔒"
        rows.append([ib(f"{sp['emoji']} {lock}{sp['name']} · L{sp['lvl']} · ≥{sp['min_mana']}🔮",
                        f"SPL:{uid}:{sid}")])
    rows.append([ib("⬅️ Назад", f"UM:{uid}")])
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("SPL:"))
async def cb_spell_pick(call: CallbackQuery):
    # Выбрано заклинание: маршрутизация по типу цели
    state = await load_active_battle(call)
    if not state:
        return
    parts = call.data.split(":")
    uid, sid = int(parts[1]), parts[2]
    if sid not in SPELLS:
        await safe_answer(call, "Неизвестное заклинание.", show_alert=True)
        return
    side, u, line, _ = find_unit(state, uid)
    if u is None or side != "player" or line not in ROWS:
        await safe_answer(call, "Карта не на поле.", show_alert=True)
        return
    err = spell_ready(state, u, sid)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    sp = SPELLS[sid]
    t = sp["target"]

    if t in ("all_enemies", "all_allies", "all_enemy_cells"):
        # Мгновенное массовое заклинание — с подтверждением
        await safe_answer(call)
        scope = {"all_enemies": "ВСЕМ врагам", "all_allies": "ВСЕМ союзникам",
                 "all_enemy_cells": "ВСЕМ клеткам врага"}[t]
        text = (f"{sp['emoji']} <b>«{sp['name']}»</b> — {scope}.\n{sp['desc']}\n\n"
                f"Слить ВЕСЬ пул ({u['mana']}🔮) и ход на этот каст? Уверены?")
        await safe_edit(call, text, kb([ib("✅ Да, уверен(а)", f"SPC:{uid}:{sid}")],
                                       [ib("✖ Нет", f"UM:{uid}")]))
        return

    if t in ("enemy_line", "ally_line"):
        # Выбор линии кнопками
        want_side = "enemy" if t == "enemy_line" else "player"
        rows = []
        for ln in ROWS:
            n = sum(1 for l2, _, x in board_units(state, want_side) if l2 == ln)
            if n > 0:
                rows.append([ib(f"{ROW_LABEL[ln]} — юнитов: {n}", f"SPLT:{uid}:{sid}:{ln}")])
        if not rows:
            await safe_answer(call, "Нет подходящих линий с юнитами.", show_alert=True)
            return
        rows.append([ib("✖ Отменить", f"UM:{uid}")])
        await safe_answer(call)
        await safe_edit(call, f"{sp['emoji']} <b>«{sp['name']}»</b> — выберите линию:\n{sp['desc']}",
                        InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if t == "ally_grave":
        # Выбор погибшей карты для воскрешения
        rows = []
        for g in state["player"]["graveyard"]:
            rows.append([ib(f"💀 {g['name']} {g['surname']} L{g['level']}", f"SPG:{uid}:{sid}:{g['uid']}")])
        rows.append([ib("✖ Отменить", f"UM:{uid}")])
        await safe_answer(call)
        await safe_edit(call, f"{sp['emoji']} <b>«{sp['name']}»</b> — кого вернуть из павших?\n{sp['desc']}",
                        InlineKeyboardMarkup(inline_keyboard=rows))
        return

    # Целевые заклинания (юнит/клетка): режим «следующий клик — цель»
    state["pending"] = {"act": "spell", "uid": uid, "spell": sid}
    await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


@router.callback_query(F.data.startswith("SPC:"))
async def cb_spell_cast_global(call: CallbackQuery):
    # Подтверждённый каст массового заклинания
    state = await load_active_battle(call)
    if not state:
        return
    parts = call.data.split(":")
    uid, sid = int(parts[1]), parts[2]
    side, u, line, _ = find_unit(state, uid)
    if u is None or side != "player" or line not in ROWS:
        await safe_answer(call, "Карта не на поле.", show_alert=True)
        return
    err = spell_ready(state, u, sid)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    cast_spell(state, "player", u, sid, None)
    await finish_player_action(call, state)


@router.callback_query(F.data.startswith("SPLT:"))
async def cb_spell_cast_line(call: CallbackQuery):
    # Каст по выбранной линии
    state = await load_active_battle(call)
    if not state:
        return
    parts = call.data.split(":")
    uid, sid, line = int(parts[1]), parts[2], parts[3]
    side, u, u_line, _ = find_unit(state, uid)
    if u is None or side != "player" or u_line not in ROWS:
        await safe_answer(call, "Карта не на поле.", show_alert=True)
        return
    err = spell_ready(state, u, sid)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    sp = SPELLS[sid]
    want_side = "enemy" if sp["target"] == "enemy_line" else "player"
    if not any(l2 == line for l2, _, _ in board_units(state, want_side)):
        await safe_answer(call, "На этой линии больше нет целей.", show_alert=True)
        return
    cast_spell(state, "player", u, sid, {"kind": "line", "line": line})
    await finish_player_action(call, state)


@router.callback_query(F.data.startswith("SPG:"))
async def cb_spell_cast_grave(call: CallbackQuery):
    # Каст воскрешения на выбранную погибшую карту
    state = await load_active_battle(call)
    if not state:
        return
    parts = call.data.split(":")
    uid, sid, duid = int(parts[1]), parts[2], int(parts[3])
    side, u, line, _ = find_unit(state, uid)
    if u is None or side != "player" or line not in ROWS:
        await safe_answer(call, "Карта не на поле.", show_alert=True)
        return
    err = spell_ready(state, u, sid)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    dead = next((g for g in state["player"]["graveyard"] if g["uid"] == duid), None)
    if dead is None:
        await safe_answer(call, "Эта карта уже недоступна.", show_alert=True)
        return
    cast_spell(state, "player", u, sid, {"kind": "grave", "uid": duid})
    await finish_player_action(call, state)


# ---------- стены: информация и слом ----------

async def show_wall_choice(call: CallbackQuery, state: dict, line: str, pos: int, w: dict):
    # Клик по вражеской стене: выбрать, кто её сломает
    cands = [u for _, _, u in board_units(state, "player")
             if u["end"] > 0 and can_normal_attack(u, line)
             and not has_fx(u, "frozen") and not has_fx(u, "time_lock")
             and (u["cls"] != CLS_MAGE or u["mana"] >= MAGE_ATTACK_MANA)]
    if not cands:
        await safe_answer(call, "Никто из юнитов не может ударить по этой стене.", show_alert=True)
        return
    text = (f"🧱 <b>Вражеская стена {ROW_LABEL[line]}:{pos}</b>: HP {w['hp']}, КЗ {w['armor']}.\n"
            f"Она мешает двигаться, даёт тылу врага +3 КЗ и служит заслоном от скрытых атак. Кем сломать?")
    rows = [[ib(f"⚔️ {u['name']} ⚔{atk_eff(u, state)}", f"EW:{u['uid']}:{line}:{pos}")] for u in cands]
    rows.append([ib("✖ Отменить", "X")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("EW:"))
async def cb_attack_wall(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    parts = call.data.split(":")
    uid, line, pos = int(parts[1]), parts[2], int(parts[3])
    side, u, u_line, _ = find_unit(state, uid)
    if u is None or side != "player" or u_line not in ROWS:
        await safe_answer(call, "Атакующий недоступен.", show_alert=True)
        return
    err = attacker_error(u, stealth=False)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    w = get_wall(state, line, pos)
    if w is None or w["owner"] == "player" or not can_normal_attack(u, line):
        await safe_answer(call, "Стена больше недоступна для удара.", show_alert=True)
        return
    perform_attack(state, "player", u, line, pos, stealth=False)
    await finish_player_action(call, state)


@router.callback_query(F.data == "X")
async def cb_cancel(call: CallbackQuery):
    # «✖ Отменить» — сбрасывает режим выбора цели/клетки
    state = await db_load_battle(call.from_user.id)
    await safe_answer(call)
    if state is None:
        await safe_edit(call, MENU_TEXT, None)
        return
    state["pending"] = None
    if not state["over"]:
        await db_save_battle(call.from_user.id, state)
    await safe_edit(call, battle_text(state), kb_battle(state))


# ---------- атака «от цели»: выбрали врага → выбираем кем ----------

async def exec_attack_on_unit(call: CallbackQuery, state: dict, a_uid: int, t_uid: int, stealth: bool):
    # Общая реализация удара по конкретному вражескому юниту
    a_side, a, a_line, _ = find_unit(state, a_uid)
    if a is None or a_side != "player" or a_line in (None, "deck", "graveyard"):
        await safe_answer(call, "Атакующий недоступен.", show_alert=True)
        return
    err = attacker_error(a, stealth)
    if err:
        await safe_answer(call, err, show_alert=True)
        return
    t_side, t, t_line, t_pos = find_unit(state, t_uid)
    if t is None or t_side != "enemy" or t_line in (None, "deck", "graveyard"):
        await safe_answer(call, "Цель больше недоступна.", show_alert=True)
        return
    ok = can_stealth_attack(a, t_line) if stealth else can_normal_attack(a, t_line)
    if not ok:
        await safe_answer(call, "Цель вне досягаемости этого вида атаки — выберите заново.", show_alert=True)
        return
    perform_attack(state, "player", a, t_line, t_pos, stealth=stealth)
    await finish_player_action(call, state)


@router.callback_query(F.data.startswith("EA:"))
async def cb_attack_from_target(call: CallbackQuery):
    # Обычная атака по вражескому юниту — выполняется сразу
    state = await load_active_battle(call)
    if not state:
        return
    _, a_s, t_s = call.data.split(":")
    await exec_attack_on_unit(call, state, int(a_s), int(t_s), stealth=False)


@router.callback_query(F.data.startswith("SA:"))
async def cb_stealth_from_target(call: CallbackQuery):
    # Скрытая атака по тылу — сначала подтверждаем
    state = await load_active_battle(call)
    if not state:
        return
    _, a_s, t_s = call.data.split(":")
    a = find_unit(state, int(a_s))[1]
    t = find_unit(state, int(t_s))[1]
    if a is None or t is None:
        await safe_answer(call, "Цель или атакующий недоступны.", show_alert=True)
        return
    await safe_answer(call)
    text = (f"🗡 <b>Скрытая атака</b>\n{a['name']} ({CLS_NAME[a['cls']]}) → {t['name']} (тыл врага)\n\n"
            f"Уверены, что хотите совершить скрытую атаку?")
    await safe_edit(call, text,
                    kb([ib("✅ Да, уверен(а)", f"SAY:{a_s}:{t_s}")], [ib("✖ Нет", "X")]))


@router.callback_query(F.data.startswith("SAY:"))
async def cb_stealth_from_target_yes(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    _, a_s, t_s = call.data.split(":")
    await exec_attack_on_unit(call, state, int(a_s), int(t_s), stealth=True)


# ---------- перемещение (захват клетки) ----------

@router.callback_query(F.data.startswith("MV:"))
async def cb_move_exec(call: CallbackQuery):
    # Перемещение конкретного юнита на конкретную пустую клетку
    state = await load_active_battle(call)
    if not state:
        return
    _, uid_s, line, pos_s = call.data.split(":")
    uid, pos = int(uid_s), int(pos_s)
    side, u, u_line, _ = find_unit(state, uid)
    if u is None or side != "player" or u_line in (None, "deck", "graveyard"):
        await safe_answer(call, "Юнит недоступен.", show_alert=True)
        return
    if not can_move_to(state, u, line, pos):
        await safe_answer(call, "Перемещение туда невозможно (занято, нет клетки или "
                          "линия не соседняя).", show_alert=True)
        return
    perform_move(state, "player", u, line, pos)
    await finish_player_action(call, state)


# ---------- высадка из колоды ----------

@router.callback_query(F.data.startswith("PC:"))
async def cb_place_on_cell(call: CallbackQuery):
    # Высадка конкретной карты на конкретную свою клетку
    state = await load_active_battle(call)
    if not state:
        return
    _, line, pos_s, uid_s = call.data.split(":")
    pos, uid = int(pos_s), int(uid_s)
    if ROW_SIDE[line] != "player" or not cell_exists(state, line, pos) \
            or cell_blocked(state, line, pos):
        await safe_answer(call, "Сюда нельзя поставить карту.", show_alert=True)
        return
    u = next((x for x in state["player"]["deck"] if x["uid"] == uid), None)
    if u is None:
        await safe_answer(call, "Эта карта больше недоступна.", show_alert=True)
        return
    perform_place(state, "player", line, pos, u)
    await finish_player_action(call, state)


@router.callback_query(F.data == "DK")
async def cb_deck_tab(call: CallbackQuery):
    # Вкладка «Колода» внизу карты боя
    state = await load_active_battle(call)
    if not state:
        return
    deck = state["player"]["deck"]
    text = battle_text(state) + ("\n\n🃏 <b>Колода в бою</b> — здесь карты восстанавливают "
                                 "+1⚡ и +1🔮 каждый ход. Нажмите карту, чтобы выставить на поле:")
    rows = []
    for u in deck:
        rows.append([ib(unit_line(u)[:64], f"DKP:{u['uid']}")])
    if not deck:
        text += "\n\n(пусто)"
    rows.append([ib("⬅️ Назад на поле", "bt:view")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("DKP:"))
async def cb_deck_place(call: CallbackQuery):
    # Выбрали карту в колоде → возвращаем на поле: следующий клик — своя клетка
    state = await load_active_battle(call)
    if not state:
        return
    uid = int(call.data.split(":")[1])
    u = next((x for x in state["player"]["deck"] if x["uid"] == uid), None)
    if u is None:
        await safe_answer(call, "Эта карта больше не в колоде.", show_alert=True)
        return
    if not own_empty_cells(state, "player"):
        await safe_answer(call, "Нет свободных клеток для высадки.", show_alert=True)
        return
    state["pending"] = {"act": "place", "uid": uid}
    await db_save_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


# ---------- распределение очков роста (игрок выбирает сам) ----------

@router.callback_query(F.data == "LU")
async def cb_levelup_list(call: CallbackQuery):
    state = await db_load_battle(call.from_user.id)
    if state is None:
        await safe_answer(call, "У вас нет активного боя.", show_alert=True)
        return
    cands = [u for u in all_units(state, "player") if u.get("pending_levels", 0) > 0]
    if not cands:
        # Очков нет: если бой завершён — это страховка финала после сбоя
        if state["over"] and state.get("bonus_applied"):
            await end_battle(call.from_user.id, state)
        await safe_answer(call)
        await safe_edit(call, battle_text(state), kb_battle(state))
        return
    text = "🌟 <b>Повышение уровня!</b>\nВыберите карту, чтобы распределить очки роста:"
    rows = [[ib(f"{'💀 ' if u['hp'] <= 0 else '🌟 '}{u['name']} L{u['level']} — очков: {u['pending_levels']}",
                f"LUS:{u['uid']}")]
            for u in cands]
    rows.append([ib("⬅️ Назад", "bt:view")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("LUS:"))
async def cb_levelup_stats(call: CallbackQuery):
    state = await db_load_battle(call.from_user.id)
    if state is None:
        await safe_answer(call, "У вас нет активного боя.", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    _, u, _, _ = find_unit(state, uid)
    if u is None or u["side"] != "player" or u.get("pending_levels", 0) <= 0:
        await safe_answer(call, "Нет доступных очков роста.", show_alert=True)
        return
    text = (unit_info(u) + f"\n\n🌟 Свободных очков роста: <b>{u['pending_levels']}</b>\n"
            f"Выберите, что повысить (одно очко = один выбор):")
    rows = []
    for stat in stat_choices_for(u):
        label = sub_choice_label(u) if stat == "sub" else STAT_CHOICE_LABEL[stat]
        rows.append([ib(label, f"lup:{uid}:{stat}")])
    rows.append([ib("⬅️ Назад", "LU")])
    await safe_answer(call)
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("lup:"))
async def cb_levelup_apply(call: CallbackQuery):
    state = await db_load_battle(call.from_user.id)
    if state is None:
        await safe_answer(call, "У вас нет активного боя.", show_alert=True)
        return
    _, uid_s, stat = call.data.split(":")
    uid = int(uid_s)
    _, u, _, _ = find_unit(state, uid)
    if u is None or u["side"] != "player" or u.get("pending_levels", 0) <= 0:
        await safe_answer(call, "Нет доступных очков роста.", show_alert=True)
        return
    if stat not in stat_choices_for(u):
        await safe_answer(call, "Недоступный рост для этого класса.", show_alert=True)
        return
    apply_stat_choice(u, stat)
    label = sub_choice_label(u) if stat == "sub" else STAT_CHOICE_LABEL[stat]
    log_add(state, f"🌟 {u['name']} {u['surname']}: выбран рост — {label}")
    await safe_answer(call, "✅ Рост применён")
    if state["over"]:
        # Бой уже завершён: применяем очко и пробуем финализировать
        # (если очков больше нет — итоги запишутся в БД).
        await end_battle(call.from_user.id, state)
        await safe_edit(call, battle_text(state), kb_battle(state))
        return
    await db_save_battle(call.from_user.id, state)
    if u.get("pending_levels", 0) > 0:
        # У карты остались очки — показываем выбор снова
        text = (unit_info(u) + f"\n\n🌟 Свободных очков роста: <b>{u['pending_levels']}</b>\n"
                f"Выберите, что повысить (одно очко = один выбор):")
        rows = []
        for st2 in stat_choices_for(u):
            lbl = sub_choice_label(u) if st2 == "sub" else STAT_CHOICE_LABEL[st2]
            rows.append([ib(lbl, f"lup:{uid}:{st2}")])
        rows.append([ib("⬅️ Назад", "LU")])
        await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await safe_edit(call, battle_text(state), kb_battle(state))


# ---------- пропуск хода и бегство ----------

@router.callback_query(F.data == "bt:skip")
async def cb_skip(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    log_add(state, "⏭ Вы пропустили ход.")
    await finish_player_action(call, state)


@router.callback_query(F.data == "bt:fl")
async def cb_flee_ask(call: CallbackQuery):
    state = await load_active_battle(call)
    if not state:
        return
    await safe_answer(call)
    text = battle_text(state) + ("\n\n🏳 <b>Покинуть бой?</b>\nОпыт за убийства сохранится, "
                                 "но бонус за победу вы не получите, а бой будет проигран.")
    await safe_edit(call, text,
                    kb([ib("🏳 Да, покинуть", "bt:fl2")], [ib("⬅️ Вернуться в бой", "bt:view")]))


@router.callback_query(F.data == "bt:fl2")
async def cb_flee_do(call: CallbackQuery):
    state = await db_load_battle(call.from_user.id)
    if state is None:
        await safe_answer(call, "У вас нет активного боя.", show_alert=True)
        return
    if not state["over"]:
        state["over"] = True
        state["result"] = "flee"
        state["pending"] = None
        log_add(state, "🏳 Вы покинули поле боя.")
        await end_battle(call.from_user.id, state)
    await safe_answer(call)
    await safe_edit(call, battle_text(state), kb_battle(state))


# =====================================================================
# АДМИН-ПАНЕЛЬ (/admin + пароль ADMIN_PASSWORD)
# Возможности (по заданию): получить карту (любой редкости), прокачать,
# сменить редкость, удалить, вылечить все карты и ПРОГОН БОЁВ:
# N сражений (N < 50) собранной колодой игрока против колоды
# АНАЛОГИЧНОЙ РЕДКОСТИ; полные логи ходов сохраняются в БД и
# отправляются игроку файлом.
# =====================================================================

ADMIN_PAGE = 8


def admin_text() -> str:
    return ("🛠 <b>Админ-панель</b>\n\n"
            "🎲 Выдать карту — любая редкость, сразу в колоду\n"
            "⬆️ Прокачать — +1/+5/+10 уровней или «до 10»\n"
            "✨ Редкость — изменить грейд карты\n"
            "🗑 Удалить карту\n"
            "❤️ Вылечить все карты\n"
            f"⚔️ Прогон боёв — N боёв (N &lt; 50) вашей колодой против колоды "
            f"аналогичной редкости; полный лог ходов придёт файлом.")


def admin_kb() -> InlineKeyboardMarkup:
    return kb(
        [ib("🎲 Выдать карту", "adm:new"), ib("⬆️ Прокачать", "adm:lvl:0")],
        [ib("✨ Редкость", "adm:rar:0"), ib("🗑 Удалить", "adm:del:0")],
        [ib("❤️ Вылечить все карты", "adm:heal")],
        [ib("⚔️ Прогон боёв", "adm:sim")],
        [ib("🏠 Меню", "mn:home")],
    )


async def admin_card_list(call: CallbackQuery, op: str, page: int):
    # Список карт для админ-операций (прокачка/редкость/удаление)
    uid = call.from_user.id
    cards = await db_user_cards(uid)
    titles = {"lvl": "⬆️ Прокачать — выберите карту:",
              "rar": "✨ Смена редкости — выберите карту:",
              "del": "🗑 Удаление — выберите карту:"}
    if not cards:
        await safe_answer(call)
        await safe_edit(call, "Карт нет. Выдайте их кнопкой «🎲 Выдать карту».",
                        kb([ib("🎲 Выдать карту", "adm:new")], [ib("🛠 Панель", "adm:root")]))
        return
    n_pages = (len(cards) + ADMIN_PAGE - 1) // ADMIN_PAGE
    page = max(0, min(page, n_pages - 1))
    chunk = cards[page * ADMIN_PAGE:(page + 1) * ADMIN_PAGE]
    rows = []
    for c in chunk:
        rem = rarity_label(c["rarity"] if "rarity" in c.keys() else "common").split(" ")[0]
        rows.append([ib(f"{rem}{CLS_EMOJI[c['cls']]} {c['name']} {c['surname']} · L{c['level']}",
                        f"adm:{op}:pick:{c['id']}")])
    nav = []
    if page > 0:
        nav.append(ib("◀️", f"adm:{op}:{page - 1}"))
    nav.append(ib("🛠 Панель", "adm:root"))
    if page < n_pages - 1:
        nav.append(ib("▶️", f"adm:{op}:{page + 1}"))
    rows.append(nav)
    await safe_answer(call)
    await safe_edit(call, titles[op], InlineKeyboardMarkup(inline_keyboard=rows))


async def run_battle_sims(user_id: int, n: int):
    # Прогон N боёв: колода игрока против сгенерированной колоды АНАЛОГИЧНОЙ
    # РЕДКОСТИ (средний ранг редкости и средний уровень колоды игрока).
    # Оба игрока — движковый ИИ. Колода: ⭐приоритетная, иначе первая
    # непустая (№1→№2→№3). Возвращает (summary, full_log) или (None, None).
    all_cards = await db_user_cards(user_id)
    decks = decks_of(all_cards)
    pref = await user_flag(user_id, "pref_deck")
    deck = pref if decks.get(pref) else next((d for d in sorted(decks) if decks[d]), 1)
    bcards = await db_battle_cards(user_id, deck)
    if not bcards:
        return None, None
    n = max(1, min(n, SIM_MAX_BATTLES))
    ranks = [rarity_rank(c["rarity"] if "rarity" in c.keys() else "common") for c in bcards]
    enemy_rarity = RARITY_KEYS[max(0, min(4, round(sum(ranks) / len(ranks))))]
    avg_level = max(1, round(sum(c["level"] for c in bcards) / len(bcards)))
    deck_desc = ", ".join(
        f"{c['name']} {c['surname']} ({rarity_label(c['rarity'] if 'rarity' in c.keys() else 'common')}, "
        f"L{c['level']}, {CLS_NAME[c['cls']]})" for c in bcards)
    results = {"victory": 0, "defeat": 0, "draw": 0}
    rounds_total = 0
    spells_cast = 0
    full_log = [
        "⚔️ ПРОГОН БОЁВ (админ-панель)",
        f"Боёв: {n} · Колода игрока №{deck}: {deck_desc}",
        f"Противник: колода аналогичной редкости ({rarity_label(enemy_rarity)}), средний уровень {avg_level}",
        "",
    ]
    for i in range(n):
        state = create_battle([dict(c) for c in bcards],
                              random.choice(list(LAYOUTS)), enemy_rarity=enemy_rarity)
        state["no_trim"] = True  # полный журнал без обрезки
        seen = 0
        battle_log = [f"=== БОЙ {i + 1}/{n} ==="]
        side = "player"
        it = 0
        while not state["over"] and it < 3000:
            begin_turn(state, side)
            if check_over(state):
                break
            ai_act(state, side)
            check_over(state)
            new_lines = state["log"][seen:]
            seen = len(state["log"])
            who = "ВЫ " if side == "player" else "ВРАГ"
            battle_log += [f"[Р{state['round']}·{who}] {l}" for l in new_lines]
            side = other_side(side)
            if side == "player":
                state["round"] += 1   # раунд = полный круг (ваш ход + ход противника)
            it += 1
        res = state["result"] or "draw"
        results[res] = results.get(res, 0) + 1
        rounds_total += state["round"]
        spells_cast += sum(1 for l in state["log"] if "кастует" in l)
        battle_log.append(f"--- ИТОГ БОЯ {i + 1}: {res.upper()}, раундов: {state['round']} ---")
        full_log += battle_log + [""]
    winrate = results["victory"] * 100 // max(1, n)
    summary = (f"⚔️ <b>Прогон: {n} боёв</b> · колода №{deck}\n"
               f"🏆 Побед: {results['victory']} ({winrate}%) · ☠️ Поражений: {results['defeat']} · "
               f"🤝 Ничьих: {results['draw']}\n"
               f"⏱ Средняя длина боя: {rounds_total // max(1, n)} раундов · "
               f"✨ Заклинаний скастовано: {spells_cast}\n"
               f"Противник: {rarity_label(enemy_rarity)}, средний уровень {avg_level}.")
    return summary, "\n".join(full_log)


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    if not await user_flag(uid, "is_admin"):
        await safe_answer(call, "Нужен админ-доступ: отправьте /admin.", show_alert=True)
        return
    parts = call.data.split(":")
    op = parts[1]

    if op == "root":
        await safe_answer(call)
        await safe_edit(call, admin_text(), admin_kb())
        return

    if op == "new":
        if len(parts) == 2:
            rows = [[ib("🎰 Случайная редкость", "adm:new:rand")]]
            rows += [[ib(rarity_label(k), f"adm:new:{k}")] for k in RARITY_KEYS]
            rows.append([ib("⬅️ Назад", "adm:root")])
            await safe_answer(call)
            await safe_edit(call, "🎲 <b>Выдача карты</b> — выберите редкость:",
                            InlineKeyboardMarkup(inline_keyboard=rows))
        else:
            r = None if parts[2] == "rand" else parts[2]
            d = generate_card_data(rarity=r)
            cid = await db_add_card(uid, d)
            await db_set_in_deck(cid, 1)  # админская карта сразу в колоде
            row = await db_get_card(cid, uid)
            await safe_answer(call, "Карта выдана ✅")
            await safe_edit(call, "🎲 <b>Карта выдана</b> (сразу добавлена в колоду):\n\n" + card_text(row),
                            kb([ib("🎲 Выдать ещё", "adm:new")], [ib("🛠 Панель", "adm:root")]))
        return

    if op == "heal":
        await db_admin_heal_all(uid)
        await safe_answer(call, "❤️ Все карты вылечены")
        await safe_edit(call, admin_text(), admin_kb())
        return

    if op in ("lvl", "rar", "del"):
        if len(parts) == 3 and parts[2].isdigit():
            await admin_card_list(call, op, int(parts[2]))
            return
        if parts[2] == "pick":
            cid = int(parts[3])
            row = await db_get_card(cid, uid)
            if not row:
                await safe_answer(call, "Карта не найдена.", show_alert=True)
                return
            if op == "lvl":
                markup = kb([ib("+1 уровень", f"adm:lvl:up:{cid}:1"), ib("+5 уровней", f"adm:lvl:up:{cid}:5")],
                            [ib("+10 уровней", f"adm:lvl:up:{cid}:10"), ib("⭐ До 10 уровня", f"adm:lvl:set:{cid}:10")],
                            [ib("⬅️ Назад", "adm:lvl:0")])
                await safe_answer(call)
                await safe_edit(call, "⬆️ <b>Прокачка карты</b>\n\n" + card_text(row), markup)
            elif op == "rar":
                rows = [[ib(rarity_label(k), f"adm:rar:set:{cid}:{k}")] for k in RARITY_KEYS]
                rows.append([ib("⬅️ Назад", "adm:rar:0")])
                await safe_answer(call)
                await safe_edit(call, "✨ <b>Смена редкости</b>\n\n" + card_text(row),
                                InlineKeyboardMarkup(inline_keyboard=rows))
            else:
                await safe_answer(call)
                await safe_edit(call, "🗑 <b>Удалить карту навсегда?</b>\n\n" + card_text(row),
                                kb([ib("✅ Удалить", f"adm:del:yes:{cid}")], [ib("⬅️ Назад", "adm:del:0")]))
            return
        if op == "lvl" and parts[2] in ("up", "set"):
            cid, n = int(parts[3]), int(parts[4])
            row = await db_get_card(cid, uid)
            if not row:
                await safe_answer(call, "Карта не найдена.", show_alert=True)
                return
            d = {"level": row["level"], "attack": row["attack"], "armor": row["armor"],
                 "hp_max": row["hp_max"], "sub": row["sub"], "mana_max": row["mana_max"],
                 "cls": row["cls"]}
            target = d["level"] + n if parts[2] == "up" else max(d["level"], n)
            while d["level"] < target:
                apply_level_growth(d)  # админ-прокачка: рост применяется сразу
            await db_admin_set_stats(cid, d)
            row = await db_get_card(cid, uid)
            await safe_answer(call, "⬆️ Прокачано")
            await safe_edit(call, card_text(row),
                            kb([ib("⬆️ Прокачать ещё", f"adm:lvl:pick:{cid}")], [ib("🛠 Панель", "adm:root")]))
            return
        if op == "rar" and parts[2] == "set":
            cid, key = int(parts[3]), parts[4]
            if key in RARITY_INFO:
                await db_set_rarity(cid, key, uid)
            row = await db_get_card(cid, uid)
            await safe_answer(call, "✨ Редкость изменена")
            await safe_edit(call, card_text(row),
                            kb([ib("✨ Изменить ещё", f"adm:rar:pick:{cid}")], [ib("🛠 Панель", "adm:root")]))
            return
        if op == "del" and parts[2] == "yes":
            cid = int(parts[3])
            await db_delete_card(cid, uid)
            await safe_answer(call, "🗑 Карта удалена")
            cards_left = await db_user_cards(uid)
            if cards_left:
                await admin_card_list(call, "del", 0)
            else:
                await safe_edit(call, admin_text(), admin_kb())
            return

    if op == "sim":
        if len(parts) == 2:
            ns = sorted({5, 10, 25, SIM_MAX_BATTLES})
            rows = [[ib(f"⚔️ Прогнать {x} боёв", f"adm:sim:run:{x}")] for x in ns if x < 50]
            rows.append([ib("⬅️ Назад", "adm:root")])
            await safe_answer(call)
            await safe_edit(call, "⚔️ <b>Прогон боёв</b> (N &lt; 50):\nВаша боевая колода против "
                                   "колоды аналогичной редкости. Полный лог ходов придёт файлом.",
                            InlineKeyboardMarkup(inline_keyboard=rows))
            return
        if parts[2] == "run":
            n = min(int(parts[3]), SIM_MAX_BATTLES)
            await safe_answer(call, f"⚔️ Прогоняю {n} боёв, пару секунд…")
            summary, log_text = await run_battle_sims(uid, n)
            if summary is None:
                await safe_edit(call, "⚔️ Боевая колода пуста — сначала соберите колоду (🃏 Колода → 📥 В колоду).",
                                kb([ib("🛠 Панель", "adm:root")]))
                return
            sim_id = await db_save_sim(uid, n, summary, log_text)
            path = os.path.join(tempfile.gettempdir(), f"sim_{uid}_{sim_id}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(log_text)
            doc = FSInputFile(path, filename=f"sim_{n}_battles.txt")
            try:
                await bot.send_document(uid, doc, caption=summary.replace("<b>", "").replace("</b>", ""))
            finally:
                if os.path.exists(path):
                    os.unlink(path)
            await safe_edit(call, summary + f"\n\n📄 Полный лог отправлен файлом и сохранён в БД (запись #{sim_id}).",
                            kb([ib("⚔️ Прогнать ещё", "adm:sim")], [ib("🛠 Панель", "adm:root")]))
            return

    await safe_answer(call)
    await safe_edit(call, admin_text(), admin_kb())


# =====================================================================
# ЗАПУСК НА VERCEL
# Polling здесь не используется: точки входа — api/tg.py (webhook).
# Локально этот файл можно импортировать для тестов (см. test_vercel_db.py).
# =====================================================================
