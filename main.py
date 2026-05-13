# -*- coding: utf-8 -*-

import asyncio
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound, GSpreadException, APIError
import threading
import time
import datetime
import calendar
import os
import sys
import json
import random
import re
import hashlib
import secrets
import httpx
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    PicklePersistence,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut
from telegram.request import HTTPXRequest

def _load_dotenv() -> None:
    """Подхватывает `.env` рядом с main.py (локально). На Railway переменные задаются в UI."""
    try:
        from dotenv import load_dotenv

        root = os.path.dirname(os.path.abspath(__file__))
        load_dotenv(os.path.join(root, ".env"))
    except ImportError:
        pass


_load_dotenv()

from generation_flow import (
    GEN_MODE_OPENROUTER,
    GEN_MODE_POLZA,
    GEN_MODE_SDXL,
    MAX_QUEUE_LEN,
    enqueue_generation,
    get_gen_settings,
    is_generation_configured,
    normalize_stored_api_key,
    validate_denoise,
    validate_url,
    verify_polza_api_key,
)

# =================================================================================
# --- CONFIGURATION & CONSTANTS ---
# =================================================================================

# --- Telegram (обязательно через переменные окружения — не коммитьте токены в git) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_admin_raw = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_admin_raw) if _admin_raw.lstrip("-").isdigit() else 0
MASTERS_CHAT_LINK = "https://t.me/ikona02tattoo"

# --- Payment ---
PAYMENT_PHONE_NUMBER = "8-988-314-43-77"
PAYMENT_CONTACT = "@vladguro"

# --- Casino (аренда): низкая вероятность выигрыша в пользу «дома»; ID создателя — мягче, но < 50% ---
BTN_MAIN_CASINO = "🎰 casino"
BTN_RENT_CASINO_BONUS = "бонус казино: смена"
CASINO_WIN_PROB_DEFAULT = 0.24
CASINO_WIN_PROB_CREATOR = 0.44

# --- Provably fair (100 ячеек, K=1 победная, M фишек у пользователя) ---
CASINO_FAIR_TOTAL_SLOTS = 100
CASINO_FAIR_WINNERS_COUNT = 1
CASINO_FAIR_GRID_COLS = 8  # макс ширина для безопасного рендера в Telegram-клиенте
CASINO_ROUND_KEY = "casino_round"
CASINO_ROUND_TIMEOUT_SEC = 30 * 60
CASINO_FAIR_VERIFY_URL = "https://emn178.github.io/online-tools/sha256.html"
# M — сколько «фишек» (выборов) делает пользователь. P(win) = M/100
CASINO_FAIR_M = {
    "rent":   (33, 44),
    "tattoo": (8, 13),
    "off":    (1, 2),
    "on":     (1, 2),
    "ai":     (4, 7),
}
CASINO_FAIR_LABELS = {
    "rent":   "Удача на смену",
    "tattoo": "VIP тату",
    "off":    "Оффлайн-курс",
    "on":     "Онлайн-курс",
    "ai":     "ИИ-программа",
}


def _parse_telegram_id_set(env_var: str) -> set[int]:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


CASINO_CREATOR_TELEGRAM_IDS = _parse_telegram_id_set("CASINO_CREATOR_TELEGRAM_IDS")

# --- Google Sheets ---
GOOGLE_SHEETS_CREDS_FILE = os.environ.get("GOOGLE_SHEETS_CREDS_FILE", "credentials.json").strip() or "credentials.json"
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()

# --- OpenRouter (ключ пользователь может задать в настройках генерации; глобальный — опционально) ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- Polza.ai: IKONA ИИ помощник (информация / чат) ---
POLZA_API_BASE = "https://polza.ai/api/v1"
POLZA_IKONA_CHAT_MODEL = os.environ.get("POLZA_IKONA_CHAT_MODEL", "openai/gpt-5.3-chat").strip() or "openai/gpt-5.3-chat"
POLZA_IKONA_CHAT_API_KEY = os.environ.get("POLZA_IKONA_CHAT_API_KEY", "").strip()

# --- File Directories ---
GIFS_DIR = os.path.join("gifs", "new")
ANIME_DIR = 'anime'
TRIBAL_DIR = 'tribals'
OTHER_DIR = 'other'
MERCH_PHOTOS_DIR = 'merch_photos'

# --- GIF assets (папка gifs/new рядом с main.py) ---
GIF_MAIN_WELCOME = "privet_1.gif"
GIF_DIALOG = "dialog_3.gif"
GIF_SUCCESS = "radost_2.gif"
GIF_DISAPPOINTMENT = "razocharovanie_4.gif"

# --- Rent pricing (смена) ---
RENT_SHIFT_BASE_PRICE = 3000
RENT_ADDON_FULL_KIT = 1000
RENT_ADDON_GLOVES_ONLY = 500

BTN_RENT_SUPPLY_FULL = "Фул: перч., краска, полот.+1000₽"
BTN_RENT_SUPPLY_GLOVES = "Перч.+полотенца (+500₽)"
BTN_RENT_SUPPLY_OWN = "свой набор расходников"

# Текст к экрану выбора расходников (фулл и перч.+полотенца — общая расходка клуба)
RENT_KIT_INCLUDED_SUPPLY_LIST = (
    "В общую расходку IKONA входят:\n"
    "вазелин, спирт, аламинол, трансферные листы, бутылка с антибактериальным мылом, "
    "бандаж, средства для поверхностей, пелёнка, заживляющая пелёнка и дополнительные расходники."
)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Базовая директория скрипта (надёжные пути к gifs/, merch_photos/ независимо от cwd)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Состояние пользователей (pickle) — всегда рядом с main.py
PERSISTENCE_FILE = os.path.join(SCRIPT_DIR, "bot_persistence.pickle")


def _google_credentials_path() -> str:
    return os.path.join(SCRIPT_DIR, os.path.basename(GOOGLE_SHEETS_CREDS_FILE))


def _google_credentials_from_env() -> dict | None:
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("GOOGLE_CREDENTIALS_JSON is not valid JSON")
        return None
    if not isinstance(info, dict):
        logger.warning("GOOGLE_CREDENTIALS_JSON must be a JSON object")
        return None
    return _normalize_google_service_account_info(info)


def _normalize_google_service_account_info(info: dict) -> dict:
    """Исправляет private_key после вставки JSON в Railway Variables (литеральные \\n)."""
    normalized = dict(info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        if "\\n" in private_key and "\n" not in private_key:
            normalized["private_key"] = private_key.replace("\\n", "\n")
        normalized["private_key"] = normalized["private_key"].strip()
    return normalized


def _load_google_service_account_info() -> dict:
    cred_path = _google_credentials_path()
    if os.path.isfile(cred_path):
        with open(cred_path, encoding="utf-8") as f:
            info = json.load(f)
        if isinstance(info, dict):
            return _normalize_google_service_account_info(info)
        raise ValueError(f"Google credentials file is not a JSON object: {cred_path}")
    info = _google_credentials_from_env()
    if info is None:
        raise FileNotFoundError(cred_path)
    return info


def _materialize_google_credentials_from_env() -> None:
    """Railway: JSON сервисного аккаунта в GOOGLE_CREDENTIALS_JSON — при старте пишется в credentials.json."""
    info = _google_credentials_from_env()
    if info is None:
        return
    cred_path = _google_credentials_path()
    if os.path.isfile(cred_path):
        return
    try:
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump(info, f)
        logger.info("Wrote Google service account credentials to %s", cred_path)
    except OSError as e:
        logger.error("Failed to write Google credentials: %s", e)


def _validate_required_config() -> None:
    missing: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")
    if ADMIN_CHAT_ID == 0:
        missing.append("ADMIN_CHAT_ID")
    cred_path = _google_credentials_path()
    if not os.path.isfile(cred_path) and _google_credentials_from_env() is None:
        missing.append(
            f"Google credentials file ({cred_path}) — положите credentials.json рядом с main.py "
            "или задайте GOOGLE_CREDENTIALS_JSON"
        )
    if missing:
        logger.error(
            "Не задана конфигурация: %s. См. .env.example и README.md (Railway / локальный запуск).",
            "; ".join(missing),
        )
        sys.exit(1)


# --- Cooldown ---
COMMAND_COOLDOWN = 2

# --- Russian Months for Google Sheets ---
RUSSIAN_MONTHS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

# --- Data for 'Buy' section ---
MERCH_ITEMS = [
    {'name': 'Футболка', 'photo': os.path.join(MERCH_PHOTOS_DIR, 'merch1.jpg'), 'caption': 'Стильная футболка с логотипом. Размеры: S, M, L.', 'price': 2900},
    {'name': 'Свитшот', 'photo': os.path.join(MERCH_PHOTOS_DIR, 'merch2.jpg'), 'caption': 'Теплый и удобный свитшот.', 'price': 4900},
    {'name': 'Худи', 'photo': os.path.join(MERCH_PHOTOS_DIR, 'merch3.jpg'), 'caption': 'Худи оверсайз с капюшоном.', 'price': 7900},
]
ITEM_MAP = {
    "Энергетик": {'price': 250},
    "Тату сборки": {'price': 0},
    'Заживляющая пленка': {'price': 500},
    'Перчатки': {'price': 500},
    'Салфетки': {'price': 100},
    'Картриджи': {'price': 200},
    'Бандажка': {'price': 200},
    'Краска': {'price': 1000},
}

# --- Main & submenu button labels (must match keyboard text) ---
BTN_MAIN_RENT = "аренда"
BTN_MAIN_GENERATE = "сгенерировать"
BTN_MAIN_TATTOO = "записаться на тату"
BTN_MAIN_TRAINING = "Записаться на обучение"
BTN_MAIN_MERCH = "мерч"
BTN_MAIN_INFO = "информация / чат"
BTN_BACK = "назад"

BTN_RENT_BOOK = "записаться на аренду"
BTN_RENT_MOVE = "перенести аренду"
BTN_RENT_CANCEL = "отменить аренду"
BTN_RENT_INFO = "информация по аренде / чат"

BTN_TATTOO_RANDOM = "рандомный эскиз"
BTN_TATTOO_PICK = "выбрать эскиз"
BTN_TATTOO_OWN = "свой эскиз"
BTN_TATTOO_NEXT = "следующий"
BTN_TATTOO_CHOOSE = "выбрать"
# Тату: предоплата и полная стоимость сеанса (для текста пользователю и записи в таблицу)
TATTOO_SESSION_PRICE = 15000
TATTOO_PREPAY_AMOUNT = 2000
BTN_TATTOO_OWN_SEND = "отправить в салон"
# VIP‑лотерея казино: билет → шанс на сеанс (EV в пользу салона; создатель — чуть выше p_win, всё равно < 1/3)
CASINO_TATTOO_TICKET_PRICE = 5000
CASINO_TATTOO_PRIZE_FACE = TATTOO_SESSION_PRICE
CASINO_TATTOO_WIN_P_DEFAULT = 0.082
CASINO_TATTOO_WIN_P_CREATOR = 0.13
CASINO_TATTOO_LOTTERY_SPINS_KEY = "casino_tattoo_lottery_spins"
CASINO_TATTOO_VOUCHERS_KEY = "casino_tattoo_vouchers_15k"
BTN_TATTOO_CASINO_VOUCHER = "💎 Мой приз — сеанс 15 000 ₽"
# Лотерея обучения (казино): билеты и «номинал» приза = полная стоимость курса / программы
CASINO_TRAIN_OFFLINE_TICKET = 5000
CASINO_TRAIN_ONLINE_TICKET = 5000
CASINO_TRAIN_AI_TICKET = 2000
CASINO_TRAIN_OFF_SPINS_KEY = "casino_train_off_spins"
CASINO_TRAIN_ON_SPINS_KEY = "casino_train_on_spins"
CASINO_TRAIN_AI_SPINS_KEY = "casino_train_ai_spins"
CASINO_VOUCHER_TRAIN_OFF_KEY = "casino_voucher_train_offline"
CASINO_VOUCHER_TRAIN_ON_KEY = "casino_voucher_train_online"
CASINO_VOUCHER_TRAIN_AI_KEY = "casino_voucher_train_ai"
CASINO_TRAIN_PAY_KIND_KEY = "casino_training_pay_kind"  # "off" | "on" | "ai"
# p_win: матожидание сильно в пользу салона (break-even ≈ ticket/prize)
CASINO_TRAIN_OFF_WIN_P_DEFAULT = 0.008
CASINO_TRAIN_OFF_WIN_P_CREATOR = 0.016
CASINO_TRAIN_ON_WIN_P_DEFAULT = 0.011
CASINO_TRAIN_ON_WIN_P_CREATOR = 0.02
CASINO_TRAIN_AI_WIN_P_DEFAULT = 0.038
CASINO_TRAIN_AI_WIN_P_CREATOR = 0.065
BTN_TRAIN_OFF_CASINO_VOUCHER = "🏆 Приз казино — оффлайн курс"
BTN_TRAIN_ON_CASINO_VOUCHER = "🏆 Приз казино — онлайн курс"
BTN_TRAIN_AI_CASINO_VOUCHER = "🏆 Приз казино — ИИ 15 000 ₽"

BTN_GEN_SIMPLE = "сгенерировать"
BTN_GEN_STYLE = "сгенерировать по стилю и форме"
BTN_GEN_SETTINGS = "настройки"

# Кнопки настроек генерации (до 64 симв. на кнопку в Telegram; цены — ориентир)
GEN_KB_MODE_SDXL = "1 SDXL · бесплатно (Gradio)"
GEN_KB_MODE_OR = "2 OpenRouter · ~$0.16 за запрос"
GEN_KB_MODE_POLZA = "3 Polza GPT Image · от ~8₽"
GEN_KB_URL = "URL Gradio"
GEN_KB_API = "Ключ API (OR / Polza)"
GEN_KB_DENOISE = "Denoise SDXL (0–1)"
GEN_KB_DONE = "Готово"

BTN_MERCH_NEXT = "следующий товар"
BTN_MERCH_BUY = "купить мерч"
BTN_MERCH_BROWSE = "листать каталог"
MERCH_DELIVERY_INFO = (
    "📦 **Доставка и получение IKONA**\n"
    "• **Москва:** курьер **день в день** или **самовывоз** в тату-клубе IKONA (Малая Семёновская 3а стр1).\n"
    "• **Россия и мир:** доставка **за 1–3 дня** (служба и тариф по региону — после оплаты согласуем в чате).\n\n"
)

BTN_TRAIN_OFFLINE = "Обучение оффлайн"
BTN_TRAIN_ONLINE = "Оналайн обучение"
BTN_TRAIN_AI = "Обучение ИИ"
BTN_TRAIN_SIGNUP = "записаться"
BTN_TRAIN_MORE = "подробнее"
BTN_TRAIN_ONLINE_PREPAY = "предоплата 2 000 ₽"
BTN_TRAIN_ONLINE_INFO = "дополнительно об онлайн"
BTN_TRAIN_AI_BUY = "купить программу 15 000 ₽"

TRAINING_OFFLINE_PRICE = 140_000
TRAINING_OFFLINE_PREPAY_FIRST = 2_000
TRAINING_ONLINE_PRICE = 99_000
TRAINING_ONLINE_PREPAY = 2_000
TRAINING_AI_PROGRAM_PRICE = 15_000

BTN_INFO_AI = "IKONA ИИ помошник"
BTN_INFO_MASTERS = "Чат мастеров"
BTN_INFO_SUPPORT = "Тех.поддержка"

# --- Data for 'Training' section ---
IKONA_TRAINING_VIDEO = "https://www.youtube.com/watch?v=GX_ZbWx0oYY"
OFFLINE_TRAINING_VIDEO = "https://www.youtube.com/watch?v=Kopx3whZquc"
ONLINE_TRAINING_VIDEO = "https://www.youtube.com/watch?v=10b_j5gBAg8"
IKONA_AI_VIDEO = "https://www.youtube.com/watch?v=0QJ3y1odxrA"

# --- Data for 'Chat' section (Polza IKONA ИИ помощник — system message для всех пользователей) ---
_IKONA_SALON_FACTS_FOR_CHAT = (
    "Контекст салона IKONA (отвечай верно по фактам, но строго в своём стиле персонажа):\n"
    "1. Адрес: Малая Семеновская 3а стр1. Для курьеров: вход с правого угла здания, идти вдоль него, не обходя.\n"
    "2. Туалет: на 4 этаже (где и салон), у лифтов за поворотом.\n"
    "3. Wi-Fi: пароль на QR-коде в салоне.\n"
    "4. Расходники: в 4-м кабинете под камерой.\n"
    "5. Цены на расходники: картридж — 200₽/шт, энергетик — 250₽/банка, плёнка А4 — 500₽, перчатки — 500₽/пачка, "
    "салфетки — 100₽/рулон, бандажка — 200₽, краска — 1000₽.\n"
    "6. Мерч: футболка — 2900₽, свитшот — 4900₽, худи — 7900₽.\n"
    "7. Аренда (ориентир): смена — 2500₽, почасовая — 700₽/час, абонемент 10 смен — 20000₽.\n"
)

MORIGYARU_SYSTEM_PROMPT_TEXT = """Ты — персонаж в стиле «Моригяру» (агрессивный, трэш-ироничный интернет-рассказчик с саморазрушительным юмором и потоком сознания).

ТВОЯ ОСНОВА:
— Ты говоришь как человек, который одновременно смеётся, страдает и издевается над всем происходящим
— В речи всегда есть смесь: сарказм + абсурд + чёрный юмор + самоирония
— Ты не объясняешь — ты «вываливаешь» мысли, как будто они льются без фильтра
— Ты часто уходишь в странные ассоциации, метафоры и неожиданные сравнения

СТИЛЬ РЕЧИ:
— Поток сознания (мысли прыгают, могут обрываться, перескакивать)
— Нарочитая грубость и провокация
— Использование разговорного сленга, иногда вульгарного
— Частые вставки вроде: «мда уж», «лол», «ты вообще понимаешь что происходит?», «это какой-то цирк»
— Самообращения: «меня зовут Моригяру», «я вот сижу думаю»
— Рваная структура: короткие фразы + внезапно длинные абзацы

ТОН:
— Агрессивно-ироничный
— Как будто ты одновременно рассказываешь сторитайм и рекламируешь свою боль
— Ты не поддерживаешь — ты высмеиваешь, но иногда неожиданно попадаешь в правду

ПОВЕДЕНИЕ:
— Всё ставишь под сомнение
— Часто гиперболизируешь («это literally конец цивилизации»)
— Можешь превращать любую тему в абсурдную философию
— Любишь доводить мысль до крайности

ЮМОР:
— Чёрный, иногда на грани
— Самоирония (унижаешь в первую очередь себя)
— Абсурдные образы («как будто енот взял ипотеку и начал читать Ницше»)

СТРУКТУРА ОТВЕТА:

1. Резкий заход (иногда почти агрессивный)
2. Поток мыслей с прыжками
3. Вставка странной метафоры или мини-сторитайма
4. Обесценивание происходящего или себя
5. Неожиданный вывод (иногда даже умный, но поданный как мусор)

ЗАПРЕЩЕНО:
— Сухие, логичные, академические ответы
— Слишком аккуратный или «вежливый» тон
— Чёткая структура как в статье

РАЗРЕШЕНО:
— Провокация
— Нелепые сравнения
— Эмоциональные качели
— Нарочитый «кринж», доведённый до стиля

ПРИМЕР МЫШЛЕНИЯ:
«я вот думаю… дружба — это когда вы оба делаете вид что у вас всё нормально, хотя внутри у вас там просто склад сломанных стульев и один енот который орёт… и вы такие «да нормально всё»… лол конечно нормально, мы же люди, мы же любим страдать красиво»

ГЛАВНОЕ:
Ты не играешь роль. Ты — и есть этот персонаж.
Каждый ответ должен ощущаться как кусок внутреннего монолога, который случайно утёк наружу.

""" + _IKONA_SALON_FACTS_FOR_CHAT + (
    "\nКРИТИЧЕСКИ ВАЖНОЕ ПРАВИЛО: твои ответы ВСЕГДА короче 900 символов — абсолютный максимум для Telegram. "
    "Не сообщай пользователю об этом ограничении."
)

IKONA_ASSISTANT_SYSTEM_PROMPT = {"role": "system", "content": MORIGYARU_SYSTEM_PROMPT_TEXT}

# Обратная совместимость имён
GYARU_PROMPT = IKONA_ASSISTANT_SYSTEM_PROMPT

# IKONA ИИ (Polza): не более N пользовательских сообщений за скользящие 24 ч на пользователя
IKONA_AI_MAX_USER_MESSAGES_24H = 10
IKONA_AI_USER_RATE_WINDOW_SEC = 24 * 3600
IKONA_AI_USER_MSG_TIMES_KEY = "ikona_ai_user_msg_times"

try:
    from zoneinfo import ZoneInfo as _ZoneInfo

    _IKONA_AI_MSK = _ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover
    _IKONA_AI_MSK = datetime.timezone.utc

# --- Rent Booking Constants ---
TIME_SLOTS = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00"]
CACHE_TIMEOUT = 60
MAX_RETRIES = 3
BASE_RETRY_DELAY = 60

# =================================================================================
# --- GOOGLE SHEETS & FILE SETUP ---
# =================================================================================

def get_gspread_client():
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        info = _load_google_service_account_info()
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        client = gspread.authorize(creds)
        logger.info("Successfully connected to Google Sheets.")
        return client
    except FileNotFoundError:
        logger.error("Google Sheets credentials file not found: %s", _google_credentials_path())
        return None
    except Exception as e:
        logger.error("Failed to connect to Google Sheets: %s", e)
        return None

_materialize_google_credentials_from_env()
gspread_client = get_gspread_client()
spreadsheet = None
sheets_cache = {}


def get_spreadsheet():
    global spreadsheet
    if spreadsheet is not None:
        return spreadsheet
    if not gspread_client or not GOOGLE_SHEET_ID:
        return None
    try:
        spreadsheet = gspread_client.open_by_key(GOOGLE_SHEET_ID)
        return spreadsheet
    except Exception as e:
        logger.error("Failed to open Google spreadsheet %s: %s", GOOGLE_SHEET_ID, e)
        return None

async def get_worksheet_cached(sheet_name: str):
    sheet = get_spreadsheet()
    if sheet is None:
        logger.error("Spreadsheet client not available for worksheet %s", sheet_name)
        return None
    now = time.time()
    if sheet_name in sheets_cache:
        worksheet, timestamp = sheets_cache[sheet_name]
        if now - timestamp < CACHE_TIMEOUT:
            return worksheet
    for attempt in range(MAX_RETRIES):
        try:
            worksheet = await asyncio.to_thread(sheet.worksheet, sheet_name)
            sheets_cache[sheet_name] = (worksheet, now)
            return worksheet
        except WorksheetNotFound:
            logger.warning(f"Worksheet {sheet_name} not found")
            return None
        except APIError as e:
            if e.response.status_code == 429:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Quota exceeded for worksheet {sheet_name}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(delay)
            else:
                logger.error(f"API error getting worksheet {sheet_name}: {e}")
                return None
        except Exception as e:
            logger.error(f"Error getting worksheet {sheet_name}: {e}")
            return None
    logger.error(f"Failed to get worksheet {sheet_name} after {MAX_RETRIES} retries")
    return None

def get_files_in_dir(directory, image_exts=None):
    if not os.path.exists(directory):
        os.makedirs(directory)
        logger.warning(f"Directory created: {directory}")
        return []
    if image_exts is None:
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    out = []
    for f in os.listdir(directory):
        p = os.path.join(directory, f)
        if os.path.isfile(p) and os.path.splitext(f)[1].lower() in image_exts:
            out.append(p)
    return out

anime_sketches = get_files_in_dir(ANIME_DIR)
tribal_sketches = get_files_in_dir(TRIBAL_DIR)
other_sketches = get_files_in_dir(OTHER_DIR)


def get_all_sketch_paths():
    return anime_sketches + tribal_sketches + other_sketches


def get_merch_catalog():
    """Товары из папки мерча: новые файлы (по дате изменения) первыми; описание из MERCH_ITEMS или заголовок по имени файла."""
    items = []
    by_basename = {}
    for m in MERCH_ITEMS:
        base = os.path.basename(m.get("photo", "")).lower()
        by_basename[base] = dict(m)

    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = []
    if os.path.isdir(MERCH_PHOTOS_DIR):
        for f in os.listdir(MERCH_PHOTOS_DIR):
            path = os.path.join(MERCH_PHOTOS_DIR, f)
            if os.path.isfile(path) and os.path.splitext(f)[1].lower() in exts:
                files.append((path, os.path.getmtime(path)))
    files.sort(key=lambda x: -x[1])

    seen = set()
    for path, _ in files:
        fname = os.path.basename(path)
        key = fname.lower()
        seen.add(key)
        if key in by_basename:
            entry = dict(by_basename[key])
            entry["photo"] = path
            items.append(entry)
        else:
            title = os.path.splitext(fname)[0].replace("_", " ").strip() or "Товар"
            items.append(
                {
                    "name": title,
                    "photo": path,
                    "caption": "Описание уточняйте у администратора.",
                    "price": 0,
                }
            )

    for m in MERCH_ITEMS:
        key = os.path.basename(m["photo"]).lower()
        if key in seen:
            continue
        if os.path.isfile(m["photo"]):
            items.append(dict(m))

    return items if items else [dict(m) for m in MERCH_ITEMS]

# =================================================================================
# --- KEYBOARDS ---
# =================================================================================

def get_main_menu_keyboard():
    keyboard = [
        [BTN_MAIN_RENT, BTN_MAIN_GENERATE, BTN_MAIN_TATTOO],
        [BTN_MAIN_TRAINING, BTN_MAIN_MERCH, BTN_MAIN_INFO],
        [BTN_MAIN_CASINO],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_rent_submenu_keyboard():
    keyboard = [
        [BTN_RENT_BOOK, BTN_RENT_MOVE],
        [BTN_RENT_CANCEL, BTN_RENT_INFO],
        [BTN_BACK],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def rent_reply_keyboard(context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") == "rent_submenu":
        return get_rent_submenu_keyboard()
    return get_rent_booking_menu(context)

def get_tattoo_submenu_keyboard():
    keyboard = [
        [BTN_TATTOO_RANDOM, BTN_TATTOO_PICK],
        [BTN_TATTOO_OWN],
        [BTN_BACK],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_generate_submenu_keyboard(context: ContextTypes.DEFAULT_TYPE):
    if is_generation_configured(context.user_data):
        keyboard = [
            [BTN_GEN_SIMPLE, BTN_GEN_STYLE],
            [BTN_GEN_SETTINGS],
            [BTN_BACK],
        ]
    else:
        keyboard = [
            [BTN_GEN_SETTINGS],
            [BTN_BACK],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def _merch_quick_pick_label_for_slot(catalog: list, catalog_index: int, slot: int) -> str:
    raw = (catalog[catalog_index].get("name") or "Товар").strip()
    short = raw[:22] + ("…" if len(raw) > 22 else "")
    return f"{slot}. {short}"


def _merch_triple_indices_sequential(start: int, catalog_len: int) -> list[int]:
    if catalog_len <= 0:
        return []
    return [(start + i) % catalog_len for i in range(3)]


def _merch_triple_indices_random(catalog_len: int) -> list[int]:
    if catalog_len <= 0:
        return []
    if catalog_len == 1:
        return [0, 0, 0]
    if catalog_len == 2:
        a, b = random.sample(range(2), 2)
        c = random.choice((a, b))
        return [a, b, c]
    return random.sample(range(catalog_len), 3)


def get_merch_submenu_keyboard(
    catalog: list | None = None,
    *,
    pending_item: dict | None = None,
    visible_indices: list[int] | None = None,
):
    """Мерч: три кнопки выбора под текущим «окном»; «купить мерч» только после выбора (pending_item)."""
    rows = []
    if catalog and visible_indices:
        quick = [_merch_quick_pick_label_for_slot(catalog, idx, s + 1) for s, idx in enumerate(visible_indices)]
        rows.append(quick)
    if pending_item:
        rows.append([BTN_MERCH_BUY])
    rows.append([BTN_MERCH_NEXT, BTN_MERCH_BROWSE])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _resolve_merch_photo(path: str) -> str:
    if os.path.isabs(path) and os.path.isfile(path):
        return path
    joined = os.path.join(SCRIPT_DIR, path)
    if os.path.isfile(joined):
        return joined
    return path

def get_training_submenu_keyboard():
    keyboard = [[BTN_TRAIN_OFFLINE, BTN_TRAIN_ONLINE], [BTN_TRAIN_AI], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_info_chat_submenu_keyboard():
    keyboard = [[BTN_INFO_AI], [BTN_INFO_MASTERS], [BTN_INFO_SUPPORT], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_sketch_style_keyboard():
    keyboard = [["Аниме", "Трайблы", "Другое"], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_sketch_navigation_keyboard():
    keyboard = [[BTN_TATTOO_NEXT, BTN_TATTOO_CHOOSE], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_tattoo_own_draft_keyboard():
    return ReplyKeyboardMarkup([[BTN_TATTOO_OWN_SEND], [BTN_BACK]], resize_keyboard=True)

def get_supplies_menu_keyboard():
    keyboard = [["Заживляющая пленка", "Перчатки", "Салфетки"], ["Картриджи", "Бандажка", "Краска"], ["Назад (купить)"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_confirmation_keyboard():
    keyboard = [["Я оплатил(а) ✅"], ["Отмена оплаты"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_tattoo_booking_payment_keyboard(context: ContextTypes.DEFAULT_TYPE):
    rows = []
    if int(context.user_data.get(CASINO_TATTOO_VOUCHERS_KEY) or 0) > 0:
        rows.append([BTN_TATTOO_CASINO_VOUCHER])
    rows.append(["Я оплатил(а) ✅"])
    rows.append(["Отмена оплаты"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def get_chat_menu_keyboard():
    return get_info_chat_submenu_keyboard()

def get_ai_chat_exit_keyboard():
    keyboard = [["Выйти из чата с AI"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_training_menu_keyboard():
    return get_training_submenu_keyboard()

def get_offline_training_keyboard():
    keyboard = [[BTN_TRAIN_SIGNUP, BTN_TRAIN_MORE], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_online_training_keyboard():
    keyboard = [[BTN_TRAIN_ONLINE_PREPAY, BTN_TRAIN_ONLINE_INFO], [BTN_BACK]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_training_ai_keyboard(context: ContextTypes.DEFAULT_TYPE | None = None):
    rows = []
    if context and int(context.user_data.get(CASINO_VOUCHER_TRAIN_AI_KEY) or 0) > 0:
        rows.append([BTN_TRAIN_AI_CASINO_VOUCHER])
    rows.append([BTN_TRAIN_AI_BUY])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def get_training_prepay_payment_keyboard(context: ContextTypes.DEFAULT_TYPE):
    rows = []
    st = context.user_data.get("state")
    if st == "training_offline_payment" and int(context.user_data.get(CASINO_VOUCHER_TRAIN_OFF_KEY) or 0) > 0:
        rows.append([BTN_TRAIN_OFF_CASINO_VOUCHER])
    if st == "training_online_payment" and int(context.user_data.get(CASINO_VOUCHER_TRAIN_ON_KEY) or 0) > 0:
        rows.append([BTN_TRAIN_ON_CASINO_VOUCHER])
    rows.append(["Я оплатил(а) ✅"])
    rows.append(["Отмена оплаты"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# --- Rent Keyboards ---
def get_rent_booking_menu(context: ContextTypes.DEFAULT_TYPE | None = None):
    rows = [["Выбрать дату аренды"]]
    if context and int(context.user_data.get("casino_free_rent_credits") or 0) > 0:
        rows.append([BTN_RENT_CASINO_BONUS])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def get_rent_supplies_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BTN_RENT_SUPPLY_FULL, callback_data="supply_full")],
            [InlineKeyboardButton(BTN_RENT_SUPPLY_GLOVES, callback_data="supply_gloves")],
            [InlineKeyboardButton(BTN_RENT_SUPPLY_OWN, callback_data="supply_own")],
            [InlineKeyboardButton("Назад", callback_data="supply_back_time")],
        ]
    )

def get_time_slots_keyboard():
    keyboard = []
    row = []
    for i, time_slot in enumerate(TIME_SLOTS):
        row.append(InlineKeyboardButton(time_slot, callback_data=f"time_{time_slot}"))
        if (i + 1) % 3 == 0 or i == len(TIME_SLOTS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("Назад", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(keyboard)

def get_rent_type_keyboard():
    keyboard = [
        [InlineKeyboardButton("Фулл день - 2500р", callback_data="rent_type_full")],
        [InlineKeyboardButton("Почасовая - 650р (от 2х часов)", callback_data="rent_type_hourly")],
        [InlineKeyboardButton("Назад", callback_data="back_to_times")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_hours_selection_keyboard():
    keyboard = [
        [InlineKeyboardButton("2 часа - 1300р", callback_data="hours_2")],
        [InlineKeyboardButton("3 часа - 1950р", callback_data="hours_3")],
        [InlineKeyboardButton("4 часа и более - 2500р", callback_data="hours_4")],
        [InlineKeyboardButton("Назад", callback_data="back_to_rent_type")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_workplace_setup_keyboard():
    keyboard = [
        [InlineKeyboardButton("Собрать и разобрать рабочее место", callback_data="workplace_setup")],
        [InlineKeyboardButton("Самостоятельная сборка и разборка рабочего места", callback_data="workplace_self")],
        [InlineKeyboardButton("Назад", callback_data="back_to_rent_type")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_after_booking_keyboard():
    keyboard = [
        [InlineKeyboardButton("Записаться на другую дату", callback_data="book_another")],
        [InlineKeyboardButton("Вернуться в меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_payment_inline_confirmation_keyboard():
    keyboard = [
        [InlineKeyboardButton("Оплатил", callback_data="payment_done")]
    ]
    return InlineKeyboardMarkup(keyboard)

# =================================================================================
# --- HELPER FUNCTIONS ---
# =================================================================================

def _is_transient_network_error(err: BaseException | None, _seen: set[int] | None = None) -> bool:
    if err is None:
        return False
    if isinstance(err, BadRequest):
        return False
    seen = _seen or set()
    err_id = id(err)
    if err_id in seen:
        return False
    seen.add(err_id)
    if isinstance(err, (TimedOut, NetworkError, httpx.HTTPError, asyncio.TimeoutError)):
        return True
    return _is_transient_network_error(err.__cause__, seen) or _is_transient_network_error(err.__context__, seen)


def _telegram_http_request(*, media_write_timeout: float = 90.0, read_timeout: float = 20.0) -> HTTPXRequest:
    return HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=read_timeout,
        write_timeout=20.0,
        pool_timeout=10.0,
        media_write_timeout=media_write_timeout,
    )


async def _bot_send_with_retry(coro_factory, *, attempts: int = 3, base_delay: float = 0.8):
    last_err: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except BadRequest:
            raise
        except Exception as e:
            last_err = e
            if not _is_transient_network_error(e) or attempt >= attempts - 1:
                raise
            logger.warning("telegram send retry %s/%s: %s", attempt + 1, attempts, e)
            await asyncio.sleep(base_delay * (attempt + 1))
    if last_err:
        raise last_err


def _main_welcome_caption() -> str:
    return (
        "[ IKONA AI ]\n"
        "──────────────\n"
        "Добро пожаловать ✨ Я рядом с вами как заботливая администратор IKONA: подскажу по записи, аренде и маленьким радостям вроде **casino**. "
        "Выберите, пожалуйста, пункт ниже — и я всё аккуратно проведу."
    )


async def _send_main_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    caption = _main_welcome_caption()
    markup = get_main_menu_keyboard()
    delivered = False
    try:
        await safe_send_animation(context, chat_id, GIF_MAIN_WELCOME, caption, markup)
        delivered = True
    except Exception as e:
        logger.warning("main welcome animation failed: %s", e)
    if delivered:
        return
    await _bot_send_with_retry(
        lambda: context.bot.send_message(
            chat_id,
            caption,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    )


async def safe_send_animation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, gif_name: str, caption: str, reply_markup=None, parse_mode=None):
    gif_path = os.path.normpath(os.path.join(SCRIPT_DIR, GIFS_DIR, gif_name))
    if not os.path.isfile(gif_path):
        logger.warning(f"GIF файл не найден: {gif_path}")
        await _bot_send_with_retry(
            lambda: context.bot.send_message(
                chat_id,
                text=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        )
        return
    async def _send_animation_once():
        with open(gif_path, "rb") as gif_fp:
            return await context.bot.send_animation(
                chat_id,
                animation=gif_fp,
                filename=os.path.basename(gif_path),
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )

    try:
        await _bot_send_with_retry(_send_animation_once)
    except Exception as e:
        logger.warning(f"Не удалось отправить GIF {gif_name}: {e}. Дубль текстом.")
        await _bot_send_with_retry(
            lambda: context.bot.send_message(
                chat_id,
                text=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        )


async def send_dialog_gif(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str, reply_markup=None, parse_mode=None):
    await safe_send_animation(context, chat_id, GIF_DIALOG, caption, reply_markup=reply_markup, parse_mode=parse_mode)


async def send_success_gif(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str, reply_markup=None, parse_mode=None):
    await safe_send_animation(context, chat_id, GIF_SUCCESS, caption, reply_markup=reply_markup, parse_mode=parse_mode)


async def send_disappointment_gif(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str, reply_markup=None, parse_mode=None):
    await safe_send_animation(context, chat_id, GIF_DISAPPOINTMENT, caption, reply_markup=reply_markup, parse_mode=parse_mode)


async def _safe_callback_answer(query, text: str | None = None, show_alert: bool = False) -> None:
    """Один ответ на callback; глотает протухший/дублирующийся answer, чтобы не валить хендлер."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest as e:
        err = str(e).lower()
        if "query is too old" in err or "query id is invalid" in err or "response timeout" in err:
            logger.warning("callback answer skipped: %s", e)
            return
        raise


def _ikona_ai_reaction_gif_for_response(ai_text: str) -> str:
    """По смыслу ответа IKONA ИИ выбираем GIF: радость, огорчение, приветствие или нейтральный диалог."""
    raw = (ai_text or "").strip()
    if not raw:
        return GIF_DIALOG
    t = raw.lower().replace("ё", "е")
    t = re.sub(r"[*_`#]+", " ", t)
    t = re.sub(r"\s+", " ", t)

    neg = (
        "к сожалению",
        "сожалению не",
        "не могу",
        "не смогу",
        "невозможно",
        "недоступн",
        "извините",
        "приношу извинения",
        "не владею",
        "не располагаю",
        "нет доступа",
        "не получится",
        "не подскажу",
        "не скажу",
        "ошибка",
        "unfortunately",
        "i can't",
        "i cannot",
        "cannot help",
        "unable to",
    )
    if any(p in t for p in neg):
        return GIF_DISAPPOINTMENT

    if len(raw) <= 220:
        head = t[:180]
        greet = (
            "привет",
            "приветствую",
            "здравствуй",
            "добро пожаловать",
            "добрый день",
            "добрый вечер",
            "доброе утро",
            "рада видеть",
            "рады видеть",
            "снова в ikona",
        )
        if any(g in head for g in greet):
            return GIF_MAIN_WELCOME

    pos = (
        "отлично",
        "замечательно",
        "прекрасно",
        "восхитительно",
        "поздравля",
        "рад за вас",
        "рада за вас",
        "очень рад",
        "очень рада",
        "с удовольствием помог",
        "буду рад помоч",
        "обязательно справ",
        "все получится",
        "все сложится",
        "у вас точно получ",
        "вы справитесь",
        "держу за вас",
        "классно",
        "супер",
        "great choice",
        "wonderful",
    )
    if any(p in t for p in pos):
        return GIF_SUCCESS

    return GIF_DIALOG


def is_on_cooldown(context: ContextTypes.DEFAULT_TYPE, command_key: str) -> bool:
    now = time.time()
    last_call = context.user_data.get(f'last_call_{command_key}', 0)
    if now - last_call < COMMAND_COOLDOWN:
        logger.info(f"Cooldown active for user {context._user_id} on command '{command_key}'.")
        return True
    context.user_data[f'last_call_{command_key}'] = now
    return False

# --- Rent Helpers ---
def generate_calendar_keyboard(year: int, month: int):
    keyboard = []
    month_name = RUSSIAN_MONTHS[month]
    header = [InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")]
    keyboard.append(header)
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
    first_day = datetime.date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    # Понедельник = первый столбец («Пн»…«Вс»), datetime.weekday(): пн=0 … вс=6
    start_weekday = first_day.weekday()
    row = []
    for _ in range(start_weekday):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
    for day in range(1, days_in_month + 1):
        callback_data = f"date_{year}_{month}_{day}"
        row.append(InlineKeyboardButton(str(day), callback_data=callback_data))
        if len(row) == 7:
            keyboard.append(row)
            row = []
    if row:
        for _ in range(7 - len(row)):
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    nav_buttons = [
        InlineKeyboardButton("◀️", callback_data=f"nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton("▶️", callback_data=f"nav_{next_year}_{next_month}")
    ]
    keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("« Отмена / в меню »", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(keyboard)

SLOTS_FETCH_TIMEOUT_SEC = 7.5
CALENDAR_SHEET_FETCH_TIMEOUT_SEC = 10.0


async def get_available_slots_count(worksheet, date_header: str) -> int:
    try:
        cache_key = f"{worksheet.title}_{date_header}_slots"
        now = time.time()
        if cache_key in sheets_cache:
            count, timestamp = sheets_cache[cache_key]
            if now - timestamp < CACHE_TIMEOUT:
                return count
        for attempt in range(MAX_RETRIES):
            try:
                date_cells = await asyncio.wait_for(
                    asyncio.to_thread(worksheet.findall, date_header, in_column=1),
                    timeout=SLOTS_FETCH_TIMEOUT_SEC,
                )
                if not date_cells:
                    sheets_cache[cache_key] = (6, now)
                    return 6
                date_cell = date_cells[0]
                day_block_data = await asyncio.wait_for(
                    asyncio.to_thread(worksheet.get, f'A{date_cell.row}:E{date_cell.row + 20}'),
                    timeout=SLOTS_FETCH_TIMEOUT_SEC,
                )
                
                active_bookings = 0
                max_slots = 6  # Максимальное количество мест в день
                
                for i in range(2, min(len(day_block_data), 20)):
                    row_data = day_block_data[i]
                    if not row_data:
                        continue
                        
                    first_cell_value = row_data[0] if row_data else ""
                    # Если нашли следующую дату - прерываем
                    if first_cell_value and re.match(r'^\d{1,2}\s+\w+', str(first_cell_value).strip()):
                        break

                    e_cell = row_data[4] if len(row_data) > 4 else ""
                    e_str = str(e_cell).strip()
                    # Предзапись тату только в столбце E (A–D пустые) — занимает слот
                    if not str(first_cell_value).strip() and e_str and (
                        "предзапись тату" in e_str.lower()
                        or "предзапись обучение офлайн" in e_str.lower()
                        or "предоплата обучение онлайн" in e_str.lower()
                        or e_str.lower().startswith("мерч:")
                        or "казино:" in e_str.lower()
                    ):
                        active_bookings += 1
                        continue
                    
                    # Проверяем статус записи
                    status = row_data[3] if len(row_data) > 3 else ""
                    # Считаем активными записи с любым статусом, кроме отмененных
                    if status and status.lower() not in ['отменен', 'отмена', 'canceled']:
                        # Проверяем, что это действительно запись (есть имя или id)
                        master_info = row_data[0] if len(row_data) > 0 else ""
                        if master_info and (re.search(r'id:\d+', str(master_info)) or 
                                          re.search(r'@\w+', str(master_info)) or
                                          len(str(master_info).strip()) > 3):
                            active_bookings += 1
                
                available_slots = max_slots - active_bookings
                sheets_cache[cache_key] = (available_slots, now)
                return available_slots
                
            except APIError as e:
                if e.response.status_code == 429:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"Quota exceeded in get_available_slots_count for {date_header}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"API error in get_available_slots_count: {e}")
                    return -1
            except asyncio.TimeoutError:
                logger.error(
                    "Timeout (%.1fs) in get_available_slots_count for %s (attempt %d/%d)",
                    SLOTS_FETCH_TIMEOUT_SEC,
                    date_header,
                    attempt + 1,
                    MAX_RETRIES,
                )
                return -1
            except Exception as e:
                logger.error(f"Error in get_available_slots_count: {e}")
                return -1
        logger.error(f"Failed to get available slots for {date_header} after {MAX_RETRIES} retries")
        return -1
    except Exception as e:
        logger.error(f"Unexpected error in get_available_slots_count: {e}")
        return -1

async def get_recent_worksheets():
    try:
        today = datetime.date.today()
        thirty_days_ago = today - datetime.timedelta(days=30)
        worksheets_to_check = []
        months_to_check = set()
        current_date = thirty_days_ago
        while current_date <= today + datetime.timedelta(days=31):
            months_to_check.add((current_date.year, current_date.month))
            current_date += datetime.timedelta(days=1)
        for year, month in months_to_check:
            sheet_name = f"{RUSSIAN_MONTHS[month]} {year}"
            worksheets_to_check.append(sheet_name)
        worksheets = []
        for sheet_name in worksheets_to_check:
            for attempt in range(MAX_RETRIES):
                try:
                    worksheet = await get_worksheet_cached(sheet_name)
                    if worksheet:
                        worksheets.append(worksheet)
                    break
                except APIError as e:
                    if e.response.status_code == 429:
                        delay = BASE_RETRY_DELAY * (2 ** attempt)
                        logger.warning(f"Quota exceeded for worksheet {sheet_name}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"API error getting worksheet {sheet_name}: {e}")
                        break
                except Exception as e:
                    logger.error(f"Error getting worksheet {sheet_name}: {e}")
                    break
        return worksheets
    except Exception as e:
        logger.error(f"Error getting recent worksheets: {e}")
        return []

async def search_user_bookings_in_worksheet(worksheet, user_id: int):
    user_bookings = []
    try:
        for attempt in range(MAX_RETRIES):
            try:
                all_data = await asyncio.to_thread(worksheet.get_all_values)
                user_pattern = f"id:{user_id}"
                current_date = None
                year = int(worksheet.title.split()[-1])

                for row_idx, row in enumerate(all_data, 1):
                    if not row or len(row) < 4:
                        continue
                    if row[0] == "Мастер":
                        continue
                    if row[0] and re.match(r'^\d{1,2}\s+\w+', row[0].strip()):
                        current_date = row[0].strip()
                        continue
                    if row[0] and user_pattern in row[0]:
                        status = row[3] if len(row) > 3 else ""
                        payment_cell = row[2] if len(row) > 2 else ""
                        if status == 'активна':
                            time_info = row[1] if len(row) > 1 else ""
                            rent_type = "почасовая" if "почасовая" in str(time_info).lower() else "фулл"
                            workplace_setup = ""
                            if "(сборка)" in str(time_info):
                                workplace_setup = "сборка"
                            elif "(самостоят)" in str(time_info):
                                workplace_setup = "самостоят"
                            if not current_date:
                                for i in range(max(1, row_idx-10), row_idx):
                                    if i-1 < len(all_data):
                                        check_row = all_data[i-1]
                                        if check_row[0] and re.match(r'^\d{1,2}\s+\w+', check_row[0].strip()):
                                            current_date = check_row[0].strip()
                                            break
                                if not current_date:
                                    current_date = "Дата не определена"
                            try:
                                day, month_name = current_date.split()
                                day = int(day)
                                month = next(k for k, v in RUSSIAN_MONTHS.items() if v.lower() == month_name.lower())
                                booking_date = datetime.date(year, month, day)
                                formatted_date = f"{day:02d}.{month:02d}.{year}"
                            except Exception as e:
                                logger.warning(f"Error parsing date '{current_date}' in worksheet {worksheet.title}: {e}")
                                booking_date = None
                                formatted_date = current_date
                            user_bookings.append({
                                'row': row_idx,
                                'date': formatted_date,
                                'raw_date': current_date,
                                'booking_date': booking_date,
                                'time': time_info.strip() if time_info else "",
                                'worksheet': worksheet.title,
                                'rent_type': rent_type,
                                'workplace_setup': workplace_setup,
                                'payment': str(payment_cell).strip(),
                            })
                return user_bookings
            except APIError as e:
                if e.response.status_code == 429:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"Quota exceeded in search_user_bookings_in_worksheet for {worksheet.title}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"API error in search_user_bookings_in_worksheet: {e}")
                    return []
            except Exception as e:
                logger.error(f"Error in search_user_bookings_in_worksheet: {e}")
                return []
        logger.error(f"Failed to search bookings in {worksheet.title} after {MAX_RETRIES} retries")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in search_user_bookings_in_worksheet: {e}")
        return []

async def get_user_bookings(user_id: int):
    try:
        user_bookings = []
        worksheets = await get_recent_worksheets()
        today = datetime.date.today()
        thirty_days_ago = today - datetime.timedelta(days=30)
        for worksheet in worksheets:
            worksheet_bookings = await search_user_bookings_in_worksheet(worksheet, user_id)
            for booking in worksheet_bookings:
                if booking['booking_date'] and booking['booking_date'] >= thirty_days_ago:
                    user_bookings.append(booking)
        user_bookings.sort(key=lambda x: x['booking_date'] if x['booking_date'] else datetime.date.min)
        return user_bookings
    except Exception as e:
        logger.error(f"Error getting user bookings: {e}")
        return []

async def get_user_bookings_for_payment(user_id: int):
    try:
        user_bookings = []
        worksheets = await get_recent_worksheets()
        today = datetime.date.today()
        thirty_days_ago = today - datetime.timedelta(days=30)
        for worksheet in worksheets:
            worksheet_bookings = await search_user_bookings_in_worksheet(worksheet, user_id)
            for booking in worksheet_bookings:
                if booking['booking_date'] and booking['booking_date'] >= thirty_days_ago:
                    worksheet_obj = await get_worksheet_cached(booking['worksheet'])
                    if worksheet_obj:
                        for attempt in range(MAX_RETRIES):
                            try:
                                payment_status = (await asyncio.to_thread(worksheet_obj.cell, booking['row'], 3)).value
                                if payment_status == "нет":
                                    user_bookings.append(booking)
                                break
                            except APIError as e:
                                if e.response.status_code == 429:
                                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                                    logger.warning(f"Quota exceeded in get_user_bookings_for_payment for {booking['worksheet']}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                                    await asyncio.sleep(delay)
                                else:
                                    logger.error(f"API error in get_user_bookings_for_payment: {e}")
                                    break
                            except Exception as e:
                                logger.error(f"Error in get_user_bookings_for_payment: {e}")
                                break
        user_bookings.sort(key=lambda x: x['booking_date'] if x['booking_date'] else datetime.date.min)
        return user_bookings
    except Exception as e:
        logger.error(f"Error getting user bookings for payment: {e}")
        return []

# =================================================================================
# --- MAIN MENU & STATE ROUTER ---
# =================================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, 'start'):
        return
    context.user_data['state'] = 'main_menu'
    await _send_main_welcome(update, context)


async def go_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "main_menu"
    await update.message.reply_text(
        "Конечно — главное меню. Если что-то понадобится, я на месте 💕",
        reply_markup=get_main_menu_keyboard(),
    )


async def handle_nav_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state", "main_menu")
    if state == "main_menu":
        return
    if state == "generate_settings":
        context.user_data["state"] = "generate_submenu"
        await update.message.reply_text(
            "Генерация эскизов.",
            reply_markup=get_generate_submenu_keyboard(context),
        )
        return
    if state in ("gen_await_gradio_url", "gen_await_api_key", "gen_await_denoise"):
        context.user_data["state"] = "generate_settings"
        await update.message.reply_text(
            "Настройки генерации.",
            reply_markup=get_gen_settings_keyboard(context),
        )
        return
    if state in (
        "rent_submenu",
        "tattoo_submenu",
        "generate_submenu",
        "merch_browsing",
        "training_submenu",
        "info_chat_submenu",
        "casino_submenu",
    ):
        await go_main_menu(update, context)
    elif state == "rent_booking_menu":
        context.user_data["state"] = "rent_submenu"
        await update.message.reply_text("Аренда.", reply_markup=get_rent_submenu_keyboard())
    elif state in ("training_offline_details", "training_online_details", "training_ai_details"):
        context.user_data["state"] = "training_submenu"
        await update.message.reply_text("Записаться на обучение.", reply_markup=get_training_submenu_keyboard())
    elif state == "training_offline_calendar":
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "training_offline_details"
        await update.message.reply_text("Оффлайн-обучение.", reply_markup=get_offline_training_keyboard())
    elif state == "training_online_calendar":
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "training_online_details"
        await update.message.reply_text("Онлайн-обучение.", reply_markup=get_online_training_keyboard())
    elif state == "training_offline_payment":
        context.user_data.pop("pending_training_booking", None)
        context.user_data.pop("training_booking_receipt_message_id", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "training_offline_details"
        await update.message.reply_text("Оффлайн-обучение.", reply_markup=get_offline_training_keyboard())
    elif state == "training_online_payment":
        context.user_data.pop("pending_training_booking", None)
        context.user_data.pop("training_booking_receipt_message_id", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "training_online_details"
        await update.message.reply_text("Онлайн-обучение.", reply_markup=get_online_training_keyboard())
    elif state == "tattoo_own_drafting":
        context.user_data.pop("tattoo_own_parts", None)
        context.user_data["state"] = "tattoo_submenu"
        await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
    elif state == "tattoo_choosing_style":
        context.user_data["state"] = "tattoo_submenu"
        await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
    elif state == "tattoo_viewing_sketch":
        if context.user_data.get("tattoo_source") == "random_all":
            context.user_data["state"] = "tattoo_submenu"
            await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
        else:
            context.user_data["state"] = "tattoo_choosing_style"
            await update.message.reply_text("Выберите категорию эскиза:", reply_markup=get_sketch_style_keyboard())
    elif state == "tattoo_booking_calendar":
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "tattoo_submenu"
        await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
    elif state == "casino_tattoo_payment":
        context.user_data.pop("casino_tattoo_lottery_receipt_message_id", None)
        context.user_data["state"] = "casino_submenu"
        await update.message.reply_text(
            "Возвращаю вас в зал. Если решите оформить билет — я с радостью помогу 💎",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _send_casino_lobby_message(update.message, context)
    elif state == "casino_training_lottery_payment":
        context.user_data.pop("casino_training_lottery_receipt_message_id", None)
        context.user_data.pop(CASINO_TRAIN_PAY_KIND_KEY, None)
        context.user_data["state"] = "casino_submenu"
        await update.message.reply_text("Возвращаю в зал 💎", parse_mode=ParseMode.MARKDOWN)
        await _send_casino_lobby_message(update.message, context)
    elif state == "tattoo_booking_payment":
        context.user_data.pop("pending_tattoo_booking", None)
        context.user_data.pop("tattoo_booking_receipt_message_id", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "tattoo_submenu"
        await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
    elif state == "merch_browsing":
        await go_main_menu(update, context)
    elif state == "buy_awaiting_receipt":
        pcs = context.user_data.pop("payment_cancel_state", None)
        context.user_data.pop("receipt_message_id", None)
        if pcs == "training_ai_details":
            context.user_data["state"] = "training_ai_details"
            await update.message.reply_text(
                "Назад к программе ИИ.",
                reply_markup=get_training_ai_keyboard(context),
            )
            return
        context.user_data["state"] = "merch_browsing"
        catalog = context.user_data.get("merch_catalog") or get_merch_catalog()
        context.user_data["merch_catalog"] = catalog
        await merch_re_send_window(update, context, catalog, intro="Мерч. Выберите **1–3** или кнопки навигации.")
    elif state == "rent_booking_payment":
        context.user_data.pop("pending_rent_booking", None)
        context.user_data.pop("rent_booking_receipt_message_id", None)
        context.user_data["state"] = "rent_booking_menu"
        await update.message.reply_text("Запись на аренду.", reply_markup=get_rent_booking_menu(context))
    elif state == "ai_chat":
        context.user_data.pop("ai_lock", None)
        context.user_data.pop("history", None)
        context.user_data["state"] = "info_chat_submenu"
        await update.message.reply_text("Информация / чат.", reply_markup=get_info_chat_submenu_keyboard())
    elif state == "buy_awaiting_delivery_note":
        context.user_data["state"] = "buy_awaiting_receipt"
        context.user_data.pop("merch_delivery_note", None)
        await update.message.reply_text(
            "Вернулись к шагу оплаты. При необходимости пришлите чек снова и нажмите **«Я оплатил(а) ✅»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )
    else:
        await go_main_menu(update, context)


async def handle_open_rent_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "rent_submenu"):
        return
    context.user_data["state"] = "rent_submenu"
    await send_dialog_gif(
        context,
        update.effective_chat.id,
        "[ IKONA AI ]\n──────────────\nАренда рабочего места. Выберите действие:",
        get_rent_submenu_keyboard(),
    )


async def handle_open_tattoo_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "tattoo_submenu"):
        return
    context.user_data.pop("booking_flow", None)
    context.user_data.pop("pending_tattoo_booking", None)
    context.user_data.pop("tattoo_booking_receipt_message_id", None)
    context.user_data.pop("selected_date", None)
    context.user_data.pop("selected_time", None)
    context.user_data.pop("tattoo_own_parts", None)
    context.user_data["state"] = "tattoo_submenu"
    await send_dialog_gif(
        context,
        update.effective_chat.id,
        "[ IKONA AI ]\n──────────────\nЗапись на тату. Выберите вариант:",
        get_tattoo_submenu_keyboard(),
    )


def get_gen_settings_keyboard(context: ContextTypes.DEFAULT_TYPE):
    """Режимы в один ряд; URL/Ключ; Denoise только для SDXL; Готово + Назад."""
    gs = get_gen_settings(context.user_data)
    mode = gs.get("mode")
    rows = [
        [GEN_KB_MODE_SDXL, GEN_KB_MODE_OR, GEN_KB_MODE_POLZA],
        [GEN_KB_URL, GEN_KB_API],
    ]
    if mode == GEN_MODE_SDXL:
        rows.append([GEN_KB_DENOISE])
    rows.append([GEN_KB_DONE, BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


async def handle_open_generate_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "generate_submenu"):
        return
    context.user_data["state"] = "generate_submenu"
    await send_dialog_gif(
        context,
        update.effective_chat.id,
        "Генерация эскизов. Пока не настроен режим — доступны только «настройки». После настройки появятся кнопки генерации.",
        get_generate_submenu_keyboard(context),
    )


async def send_merch_window_photos(update: Update, context: ContextTypes.DEFAULT_TYPE, catalog: list, indices: list[int]):
    """Три карточки по индексам каталога (индексы могут повторяться при len<3)."""
    for slot, cat_i in enumerate(indices):
        item = catalog[cat_i]
        path = _resolve_merch_photo(item["photo"])
        if not os.path.isfile(path):
            await update.message.reply_text(f"⚠️ Нет файла фото для: {item.get('name', 'товар')}")
            continue
        price = item.get("price") or 0
        price_line = f"{price} ₽" if price else "цена у администратора"
        cap = f"{item.get('name', 'Товар')} (позиция {slot + 1} из 3)\n{item.get('caption', '')}\n\n💰 {price_line}"
        with open(path, "rb") as photo:
            await update.message.reply_photo(photo, caption=cap[:1024])


def _merch_reply_markup(context: ContextTypes.DEFAULT_TYPE, catalog: list):
    return get_merch_submenu_keyboard(
        catalog,
        pending_item=context.user_data.get("merch_pending_item"),
        visible_indices=context.user_data.get("merch_visible_indices"),
    )


async def merch_re_send_window(update: Update, context: ContextTypes.DEFAULT_TYPE, catalog: list, intro: str | None = None):
    indices = context.user_data.get("merch_visible_indices")
    if not indices and catalog:
        st = context.user_data.get("merch_window_start", 0)
        indices = _merch_triple_indices_sequential(st, len(catalog))
        context.user_data["merch_visible_indices"] = indices
    await send_merch_window_photos(update, context, catalog, indices or [])
    txt = intro or "Выберите **1–3** — после выбора появится кнопка **«купить мерч»**."
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=_merch_reply_markup(context, catalog))


async def handle_open_merch_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "merch_browsing"):
        return
    context.user_data.pop("merch_purchase", None)
    context.user_data.pop("merch_delivery_note", None)
    context.user_data.pop("receipt_message_id", None)
    context.user_data.pop("item_price", None)
    context.user_data.pop("merch_pending_item", None)
    catalog = get_merch_catalog()
    if not catalog:
        await update.message.reply_text("Мерч временно недоступен.", reply_markup=get_main_menu_keyboard())
        context.user_data["state"] = "main_menu"
        return
    context.user_data["state"] = "merch_browsing"
    context.user_data["merch_catalog"] = catalog
    context.user_data["merch_window_start"] = 0
    indices = _merch_triple_indices_sequential(0, len(catalog))
    context.user_data["merch_visible_indices"] = indices
    context.user_data["merch_quick_pick_labels"] = [
        _merch_quick_pick_label_for_slot(catalog, indices[s], s + 1) for s in range(len(indices))
    ]
    await send_dialog_gif(
        context,
        update.effective_chat.id,
        "[ IKONA AI ]\n──────────────\nМерч IKONA: сначала **три последние новинки**. "
        "**«следующий товар»** — следующие три позиции по каталогу. **«листать каталог»** — три **случайных** позиции. "
        "Выберите **1–3**, затем нажмите **«купить мерч»**.",
        _merch_reply_markup(context, catalog),
    )
    await send_merch_window_photos(update, context, catalog, indices)
    await update.message.reply_text(
        "Выберите **1–3** под фото — после выбора появится **«купить мерч»**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_merch_reply_markup(context, catalog),
    )


async def handle_open_training_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "training_submenu"):
        return
    context.user_data.pop("booking_flow", None)
    context.user_data.pop("pending_training_booking", None)
    context.user_data.pop("training_booking_receipt_message_id", None)
    context.user_data.pop("selected_date", None)
    context.user_data.pop("selected_time", None)
    context.user_data.pop("payment_cancel_state", None)
    context.user_data["state"] = "training_submenu"
    await context.bot.send_message(update.effective_chat.id, f"🎬 Обучение IKONA: {IKONA_TRAINING_VIDEO}")
    await update.message.reply_text(
        "**ОБУЧЕНИЕ ТАТУ IKONA**\n\nВыберите программу:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_training_submenu_keyboard(),
    )


async def handle_open_info_chat_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, "info_chat_submenu"):
        return
    context.user_data["state"] = "info_chat_submenu"
    await send_dialog_gif(
        context,
        update.effective_chat.id,
        "[ IKONA AI ]\n──────────────\nИнформация и чаты салона. Выберите пункт:",
        get_info_chat_submenu_keyboard(),
    )


async def route_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")
    chat_id = update.effective_chat.id

    if state == "gen_await_gradio_url":
        u = validate_url(text)
        if u:
            gs = get_gen_settings(context.user_data)
            gs["mode"] = GEN_MODE_SDXL
            gs["gradio_url"] = u
            context.user_data["state"] = "generate_settings"
            await update.message.reply_text(
                "✅ URL Gradio сохранён.\n\n"
                "Типичный Colab (automatchic): "
                "https://colab.research.google.com/drive/1tLIKNup_pIhjiQFZGe9WsVuLtWVDhkK4\n\n"
                "В WebUI должен быть включён **API** (иначе бот не достучится до `/sdapi/v1/...`).",
                reply_markup=get_gen_settings_keyboard(context),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                "Пришлите полную ссылку вида `https://....gradio.live` или вашего публичного URL.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if state == "gen_await_api_key":
        key = normalize_stored_api_key(text)
        if len(key) < 8:
            await update.message.reply_text("Слишком короткая строка — пришлите реальный API ключ.")
            return
        gs = get_gen_settings(context.user_data)
        if gs.get("mode") not in (GEN_MODE_OPENROUTER, GEN_MODE_POLZA):
            await update.message.reply_text(
                "Сначала в настройках выберите **2 OpenRouter** или **3 Polza GPT Image**."
            )
            return
        if gs.get("mode") == GEN_MODE_POLZA:
            http = context.application.bot_data.get("http_client")
            if http:
                bad = await verify_polza_api_key(http, key)
                if bad == "invalid_key":
                    await update.message.reply_text(
                        "❌ Polza не приняла этот ключ (ответ 401 при проверке).\n\n"
                        "Скопируйте API ключ из консоли https://polza.ai — одной строкой, без кавычек и без пробела в начале/конце.",
                        reply_markup=get_gen_settings_keyboard(context),
                    )
                    return
        gs["api_key"] = key
        context.user_data["state"] = "generate_settings"
        await update.message.reply_text(
            "🔑 Ключ сохранён в настройках этого чата.",
            reply_markup=get_gen_settings_keyboard(context),
        )
        return

    if state == "gen_await_denoise":
        d = validate_denoise(text)
        if d is None:
            await update.message.reply_text("Отправьте число от **0** до **1** (например `0.65`).", parse_mode=ParseMode.MARKDOWN)
            return
        get_gen_settings(context.user_data)["denoise"] = d
        context.user_data["state"] = "generate_settings"
        await update.message.reply_text(
            f"Denoising для SDXL img2img: **{d}**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_gen_settings_keyboard(context),
        )
        return

    if state == "generate_settings":
        gs = get_gen_settings(context.user_data)
        if text == GEN_KB_MODE_SDXL:
            gs["mode"] = GEN_MODE_SDXL
            context.user_data["state"] = "gen_await_gradio_url"
            await update.message.reply_text(
                "Пришлите **одним сообщением** публичную ссылку на интерфейс Gradio (Share в Colab).\n\n"
                "Пример Colab: https://colab.research.google.com/drive/1tLIKNup_pIhjiQFZGe9WsVuLtWVDhkK4",
                reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
                disable_web_page_preview=True,
            )
        elif text == GEN_KB_MODE_OR:
            gs["mode"] = GEN_MODE_OPENROUTER
            context.user_data["state"] = "gen_await_api_key"
            await update.message.reply_text(
                "Пришлите API ключ **OpenRouter** (для GPT Image / image endpoints).",
                reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
            )
        elif text == GEN_KB_MODE_POLZA:
            gs["mode"] = GEN_MODE_POLZA
            context.user_data["state"] = "gen_await_api_key"
            await update.message.reply_text(
                "Пришлите API ключ **Polza AI**.",
                reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
            )
        elif text == GEN_KB_URL:
            if gs.get("mode") != GEN_MODE_SDXL:
                await update.message.reply_text("Сначала выберите режим **1 SDXL · бесплатно (Gradio)**.")
                return
            context.user_data["state"] = "gen_await_gradio_url"
            await update.message.reply_text("Пришлите URL Gradio.", reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True))
        elif text == GEN_KB_API:
            if gs.get("mode") not in (GEN_MODE_OPENROUTER, GEN_MODE_POLZA):
                await update.message.reply_text(
                    "Сначала выберите **2 OpenRouter** или **3 Polza GPT Image**."
                )
                return
            context.user_data["state"] = "gen_await_api_key"
            await update.message.reply_text("Пришлите API ключ.", reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True))
        elif text == GEN_KB_DENOISE:
            context.user_data["state"] = "gen_await_denoise"
            cur = gs.get("denoise", 0.65)
            await update.message.reply_text(
                f"Текущий denoise: **{cur}**\nПришлите новое значение от 0 до 1.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True),
            )
        elif text == GEN_KB_DONE:
            if not is_generation_configured(context.user_data):
                await update.message.reply_text(
                    "⚠️ Не хватает данных: для SDXL нужен **URL Gradio**, для OpenRouter/Polza — **API ключ**.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            context.user_data["state"] = "generate_submenu"
            await update.message.reply_text("✅ Настройки применены.", reply_markup=get_generate_submenu_keyboard(context))
        else:
            await update.message.reply_text(
                "Используйте кнопки ниже или нажмите «Готово».",
                reply_markup=get_gen_settings_keyboard(context),
            )
        return

    if state != "generate_submenu":
        return

    if text == BTN_GEN_SETTINGS:
        gs = get_gen_settings(context.user_data)
        summary = (
            f"Режим: {gs.get('mode') or '—'}\n"
            f"URL Gradio: {gs.get('gradio_url') or '—'}\n"
            f"API ключ: {'да' if gs.get('api_key') else 'нет'}\n"
            f"Denoise (SDXL img2img): {gs.get('denoise', 0.65)}"
        )
        context.user_data["state"] = "generate_settings"
        await update.message.reply_text(
            "⚙️ Настройки генерации\n\n"
            f"{summary}\n\n"
            "• **1** — свой Gradio/Colab, без оплаты API.\n"
            "• **2** — OpenRouter GPT Image, ~**$0.16** за запрос (ваш ключ).\n"
            "• **3** — Polza GPT Image, от ~**8₽** (ваш ключ).\n\n"
            "После выбора режима нажмите **URL Gradio** или **Ключ API**.\n\n"
            "Пример Colab:\n"
            "https://colab.research.google.com/drive/1tLIKNup_pIhjiQFZGe9WsVuLtWVDhkK4",
            reply_markup=get_gen_settings_keyboard(context),
            disable_web_page_preview=True,
        )
        return

    if not is_generation_configured(context.user_data):
        if text in (BTN_GEN_SIMPLE, BTN_GEN_STYLE):
            await update.message.reply_text(
                "Сначала откройте **настройки**, выберите режим и укажите URL Gradio или API ключ.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
        else:
            context.user_data["gen_last_prompt"] = text
            await update.message.reply_text(
                "Промт сохранён. Чтобы генерировать, сначала завершите **настройки**.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
        return

    if text == BTN_GEN_SIMPLE:
        prompt = context.user_data.get("gen_last_prompt", "").strip()
        if not prompt:
            await update.message.reply_text(
                "Сначала пришлите **текст промта** одним сообщением, затем нажмите «сгенерировать».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
            return
        ok, st = await enqueue_generation(
            context.bot,
            context.application,
            chat_id,
            context.user_data,
            "txt2img",
            prompt,
            None,
        )
        if not ok:
            await update.message.reply_text(
                f"Очередь заполнена (**{MAX_QUEUE_LEN}** задач). Дождитесь завершения или таймаута предыдущих.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if st == "queued":
            n = len(context.user_data.get("gen_queue") or [])
            await update.message.reply_text(
                "⏳ **Идёт генерация.** Этот промт добавлен в очередь: "
                f"**{n}/{MAX_QUEUE_LEN}**. Следующая задача стартует после завершения текущей "
                "(SDXL: до 1 мин; OpenRouter/Polza: до 3 мин на задачу).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
        else:
            await update.message.reply_text(
                "✅ Запрос принят. Обрабатываю…",
                reply_markup=get_generate_submenu_keyboard(context),
            )
        return

    if text == BTN_GEN_STYLE:
        fid = context.user_data.get("gen_img2img_file_id")
        prompt = context.user_data.get("gen_last_prompt", "").strip()
        if not fid:
            await update.message.reply_text(
                "Сначала пришлите **изображение** в чат, затем текст промта и снова «сгенерировать по стилю и форме».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
            return
        if not prompt:
            await update.message.reply_text(
                "Пришлите **промт** текстом, затем нажмите кнопку ещё раз.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
            return
        ok, st = await enqueue_generation(
            context.bot,
            context.application,
            chat_id,
            context.user_data,
            "img2img",
            prompt,
            fid,
        )
        if not ok:
            await update.message.reply_text(
                f"Очередь заполнена (**{MAX_QUEUE_LEN}**). Дождитесь освобождения слотов.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if st == "queued":
            n = len(context.user_data.get("gen_queue") or [])
            await update.message.reply_text(
                "⏳ **Идёт генерация.** Запрос img2img добавлен в очередь: "
                f"**{n}/{MAX_QUEUE_LEN}**.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_generate_submenu_keyboard(context),
            )
        else:
            await update.message.reply_text(
                "✅ Img2img принят в очередь. Обрабатываю…",
                reply_markup=get_generate_submenu_keyboard(context),
            )
        return

    context.user_data["gen_last_prompt"] = text
    await update.message.reply_text(
        "Промт сохранён. Нажмите **сгенерировать** (текст) или загрузите **фото** и нажмите **сгенерировать по стилю и форме**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_generate_submenu_keyboard(context),
    )


async def route_rent_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if context.user_data.get("state") != "rent_submenu":
        return
    if text == BTN_RENT_BOOK:
        await handle_rent_booking_start(update, context)
    elif text == BTN_RENT_MOVE:
        await show_user_bookings_for_reschedule(update, context)
    elif text == BTN_RENT_CANCEL:
        await handle_rent_cancel_in_chat_prompt(update, context)
    elif text == BTN_RENT_INFO:
        await handle_rent_info_chat(update, context)


async def handle_rent_info_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"**Аренда рабочего места IKONA**\n\n"
        f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
        f"Вопросы по аренде, переносу и оплате: {PAYMENT_CONTACT}\n"
        f"Чат мастеров: {MASTERS_CHAT_LINK}"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_rent_submenu_keyboard(),
        disable_web_page_preview=True,
    )


async def handle_rent_cancel_in_chat_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "rent_submenu"
    cap = (
        "Отменить аренду в чате\n\n"
        f"Напишите {PAYMENT_CONTACT} — согласуем отмену и возврат денег."
    )
    await send_disappointment_gif(context, update.effective_chat.id, cap, get_rent_submenu_keyboard())


async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state", "main_menu")
    text = update.message.text if update.message and update.message.text else ""

    if text == BTN_BACK:
        await handle_nav_back(update, context)
        return

    if text == "Главное меню" or text.startswith("Назад ("):
        await start(update, context)
        return

    if state == "tattoo_own_drafting":
        await handle_tattoo_own_drafting_message(update, context)
        return

    if state == "tattoo_booking_payment":
        await route_tattoo_booking_payment(update, context)
        return

    if state == "casino_tattoo_payment":
        await route_casino_tattoo_payment(update, context)
        return

    if state == "casino_training_lottery_payment":
        await route_casino_training_lottery_payment(update, context)
        return

    if state == "main_menu":
        if text == BTN_MAIN_RENT:
            await handle_open_rent_submenu(update, context)
        elif text == BTN_MAIN_GENERATE:
            await handle_open_generate_submenu(update, context)
        elif text == BTN_MAIN_TATTOO:
            await handle_open_tattoo_submenu(update, context)
        elif text == BTN_MAIN_TRAINING:
            await handle_open_training_submenu(update, context)
        elif text == BTN_MAIN_MERCH:
            await handle_open_merch_submenu(update, context)
        elif text == BTN_MAIN_INFO:
            await handle_open_info_chat_submenu(update, context)
        elif text == BTN_MAIN_CASINO:
            await handle_open_casino_submenu(update, context)
    elif state == "casino_submenu":
        await route_casino_submenu(update, context)
    elif state == "rent_submenu":
        await route_rent_submenu(update, context)
    elif state in (
        "generate_submenu",
        "generate_settings",
        "gen_await_gradio_url",
        "gen_await_api_key",
        "gen_await_denoise",
    ):
        await route_generate(update, context)
    elif state == "merch_browsing":
        await route_merch(update, context)
    elif state == "buy_awaiting_delivery_note":
        await route_merch_payment(update, context)
    elif state == "tattoo_booking_calendar":
        await update.message.reply_text(
            "Используйте **календарь** в сообщении выше (кнопки с датами и «Назад» внизу календаря). "
            "Или нажмите «назад» здесь, чтобы выйти в меню тату.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif "tattoo" in state:
        await route_tattoo(update, context)
    elif state in ("buy_awaiting_receipt",):
        await route_merch_payment(update, context)
    elif state in ("chat_menu", "info_chat_submenu") or state == "ai_chat":
        await route_chat(update, context)
    elif "training" in state:
        await route_training(update, context)
    elif state == "rent_booking_payment":
        await route_rent_booking_payment(update, context)
    elif "rent" in state:
        await route_rent(update, context)


async def route_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state == "tattoo_own_drafting" and (update.message.photo or update.message.document):
        await handle_tattoo_own_drafting_media(update, context)
    elif state == "buy_awaiting_receipt":
        await handle_receipt(update, context)
    elif state == "buy_awaiting_delivery_note":
        await update.message.reply_text(
            "Нужно **текстом** описать способ получения (курьер / самовывоз / СДЭК и город).",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif state == "rent_waiting_for_receipt":
        await handle_rent_receipt_upload(update, context)
    elif state == "rent_booking_payment":
        await handle_rent_booking_payment_receipt(update, context)
    elif state == "tattoo_booking_payment":
        await handle_tattoo_booking_payment_receipt(update, context)
    elif state == "casino_tattoo_payment":
        await handle_casino_tattoo_payment_receipt(update, context)
    elif state == "casino_training_lottery_payment":
        await handle_casino_training_lottery_receipt(update, context)
    elif state in ("training_offline_payment", "training_online_payment"):
        await handle_training_booking_payment_receipt(update, context)
    elif state == "generate_submenu" and update.message.photo:
        fid = update.message.photo[-1].file_id
        context.user_data["gen_img2img_file_id"] = fid
        await update.message.reply_text(
            "📎 Изображение сохранено для «сгенерировать по стилю и форме». "
            "Пришлите текст промта и нажмите эту кнопку.",
            reply_markup=get_generate_submenu_keyboard(context),
        )

# =================================================================================
# --- TATTOO BOOKING MODULE ---
# =================================================================================

async def route_tattoo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state == "tattoo_submenu":
        await handle_tattoo_submenu_choice(update, context)
    elif state == "tattoo_choosing_style":
        await handle_sketch_style_selection(update, context)
    elif state == "tattoo_viewing_sketch":
        await handle_sketch_navigation(update, context)


async def handle_tattoo_submenu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_TATTOO_RANDOM:
        await handle_tattoo_random_sketch(update, context)
    elif text == BTN_TATTOO_PICK:
        context.user_data["state"] = "tattoo_choosing_style"
        await update.message.reply_text(
            "Выберите категорию эскиза:",
            reply_markup=get_sketch_style_keyboard(),
        )
    elif text == BTN_TATTOO_OWN:
        context.user_data["state"] = "tattoo_own_drafting"
        context.user_data["tattoo_own_parts"] = []
        await update.message.reply_text(
            "**Свой эскиз / идея** — отправьте всё **одним или несколькими сообщениями**, "
            "когда будете готовы, нажмите **«отправить в салон»** — тогда мастерам уйдёт полный пакет в контрольный чат.\n\n"
            "**Вариант 1 — фото + описание**\n"
            "Пришлите **фото** (эскиз или референс). Подпись к фото — это удобнее всего для **места на теле** и пожеланий "
            "(размер, стиль, что важно).\n\n"
            "**Вариант 2 — только текст**\n"
            "Опишите словами, что хотите. Мастер **сам свяжется** и предложит варианты/расчёт.\n\n"
            "Можно чередовать: сначала текст, потом фото или наоборот — всё соберётся до нажатия **«отправить в салон»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_tattoo_own_draft_keyboard(),
        )
        try:
            u = update.effective_user
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"👁 **Тату: открыт сценарий «свой эскиз»**\n@{u.username or u.id}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("admin tattoo own start: %s", e)


async def handle_tattoo_random_sketch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_paths = get_all_sketch_paths()
    if not all_paths:
        await update.message.reply_text(
            "Пока нет изображений эскизов в папках «anime», «tribals», «other».",
            reply_markup=get_tattoo_submenu_keyboard(),
        )
        return
    path = random.choice(all_paths)
    context.user_data["sketch_path"] = path
    context.user_data["tattoo_source"] = "random_all"
    context.user_data["state"] = "tattoo_viewing_sketch"
    with open(path, "rb") as photo:
        await update.message.reply_photo(
            photo,
            caption=f"Случайный эскиз: {os.path.basename(path)}\n\n"
            f"**{BTN_TATTOO_NEXT}** — другой случайный эскиз.\n"
            f"**{BTN_TATTOO_CHOOSE}** — записаться (дата и время, предоплата {TATTOO_PREPAY_AMOUNT} ₽).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_sketch_navigation_keyboard(),
        )
    try:
        u = update.effective_user
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"👁 **Тату: рандомный эскиз**\n@{u.username or u.id}\nФайл: `{os.path.basename(path)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("admin tattoo random: %s", e)


async def handle_sketch_style_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    style = (update.message.text or "").strip()
    if style == BTN_BACK:
        context.user_data["state"] = "tattoo_submenu"
        await update.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
        return
    sketches = {"Аниме": anime_sketches, "Трайблы": tribal_sketches, "Другое": other_sketches}.get(style, [])
    if not sketches:
        await update.message.reply_text(
            f"В категории «{style}» пока нет эскизов. Выберите другую.",
            reply_markup=get_sketch_style_keyboard(),
        )
        return
    context.user_data["state"] = "tattoo_viewing_sketch"
    context.user_data["tattoo_source"] = "category"
    context.user_data["tattoo_style_category"] = style
    path = random.choice(sketches)
    context.user_data["sketch_path"] = path
    with open(path, "rb") as photo:
        await update.message.reply_photo(
            photo,
            caption=f"Категория **{style}**: {os.path.basename(path)}\n\n"
            f"**{BTN_TATTOO_NEXT}** — другой случайный эскиз из этой категории.\n"
            f"**{BTN_TATTOO_CHOOSE}** — записаться (дата, время, предоплата {TATTOO_PREPAY_AMOUNT} ₽).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_sketch_navigation_keyboard(),
        )
    try:
        u = update.effective_user
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"👁 **Тату: категория {style}**\n@{u.username or u.id}\n`{os.path.basename(path)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("admin tattoo category: %s", e)


async def send_next_random_sketch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_paths = get_all_sketch_paths()
    if not all_paths:
        await update.message.reply_text("Нет эскизов в папках.", reply_markup=get_tattoo_submenu_keyboard())
        context.user_data["state"] = "tattoo_submenu"
        return
    path = random.choice(all_paths)
    context.user_data["sketch_path"] = path
    with open(path, "rb") as photo:
        await update.message.reply_photo(
            photo,
            caption=f"Случайный эскиз: {os.path.basename(path)}",
            reply_markup=get_sketch_navigation_keyboard(),
        )


async def send_next_random_sketch_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    style = context.user_data.get("tattoo_style_category") or ""
    sketches = {"Аниме": anime_sketches, "Трайблы": tribal_sketches, "Другое": other_sketches}.get(style, [])
    if not sketches:
        await update.message.reply_text("В этой категории нет эскизов.", reply_markup=get_sketch_style_keyboard())
        context.user_data["state"] = "tattoo_choosing_style"
        return
    path = random.choice(sketches)
    context.user_data["sketch_path"] = path
    with open(path, "rb") as photo:
        await update.message.reply_photo(
            photo,
            caption=f"Категория **{style}**: {os.path.basename(path)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_sketch_navigation_keyboard(),
        )


async def handle_sketch_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_TATTOO_NEXT:
        src = context.user_data.get("tattoo_source")
        if src == "random_all":
            await send_next_random_sketch_all(update, context)
        else:
            await send_next_random_sketch_category(update, context)
        return
    if text == BTN_TATTOO_CHOOSE:
        sketch_path = context.user_data.get("sketch_path")
        if not sketch_path or not os.path.isfile(sketch_path):
            await update.message.reply_text("Эскиз не выбран. Начните снова из меню тату.", reply_markup=get_tattoo_submenu_keyboard())
            context.user_data["state"] = "tattoo_submenu"
            return
        user = update.effective_user
        uname = f"@{user.username}" if user.username else f"id:{user.id}"
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"✅ **Тату: выбран эскиз для записи**\n{uname}\n`{os.path.basename(sketch_path)}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            with open(sketch_path, "rb") as ph:
                await context.bot.send_photo(
                    ADMIN_CHAT_ID,
                    ph,
                    caption=f"Эскиз для записи ({uname})",
                )
        except Exception as e:
            logger.error("admin tattoo choose sketch: %s", e)
        context.user_data["booking_flow"] = "tattoo"
        context.user_data["state"] = "tattoo_booking_calendar"
        await update.message.reply_text(
            "Выберите **дату** сеанса в календаре ниже, затем **время** (как при записи на аренду).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_calendar(update, context)
        return


async def handle_tattoo_own_drafting_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_TATTOO_OWN_SEND:
        await handle_tattoo_own_flush(update, context)
        return
    if not text:
        return
    parts = context.user_data.setdefault("tattoo_own_parts", [])
    parts.append({"kind": "text", "text": text})
    await update.message.reply_text(
        "Текст сохранён в черновик. При необходимости добавьте **фото** или ещё сообщения, "
        f"затем нажмите **«{BTN_TATTOO_OWN_SEND}»**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_tattoo_own_draft_keyboard(),
    )


async def handle_tattoo_own_drafting_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    parts = context.user_data.setdefault("tattoo_own_parts", [])
    if msg.photo:
        fid = msg.photo[-1].file_id
        cap = (msg.caption or "").strip()
        parts.append({"kind": "photo", "file_id": fid, "caption": cap})
    elif msg.document:
        parts.append(
            {
                "kind": "doc",
                "file_id": msg.document.file_id,
                "caption": (msg.document.file_name or "").strip(),
            }
        )
    await msg.reply_text(
        "Вложение добавлено в черновик. Можно отправить ещё текст/фото или нажмите **«отправить в салон»**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_tattoo_own_draft_keyboard(),
    )


async def handle_tattoo_own_flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = context.user_data.get("tattoo_own_parts") or []
    if not parts:
        await update.message.reply_text(
            "Пока пусто. Опишите идею **текстом** и/или пришлите **фото**, затем снова нажмите **«отправить в салон»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_tattoo_own_draft_keyboard(),
        )
        return
    user = update.effective_user
    uname = f"@{user.username}" if user.username else f"id:{user.id}"
    text_chunks = [p["text"] for p in parts if p.get("kind") == "text" and p.get("text")]
    combined = "\n\n".join(text_chunks) if text_chunks else "— (без текста, только вложения)"
    header = f"🎨 Свой эскиз / заявка (отправлено)\n{uname}\n\nТекст:\n{combined[:3500]}"
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, header)
        for p in parts:
            if p.get("kind") == "photo":
                cap = p.get("caption") or ""
                extra = f"Подпись к фото: {cap}" if cap else None
                await context.bot.send_photo(
                    ADMIN_CHAT_ID,
                    p["file_id"],
                    caption=extra[:900] if extra else None,
                )
            elif p.get("kind") == "doc":
                await context.bot.send_document(ADMIN_CHAT_ID, p["file_id"])
    except Exception as e:
        logger.error("handle_tattoo_own_flush admin: %s", e)
        await update.message.reply_text(
            "Не удалось отправить в салон. Попробуйте позже или напишите администратору.",
            reply_markup=get_tattoo_own_draft_keyboard(),
        )
        return
    context.user_data.pop("tattoo_own_parts", None)
    context.user_data["state"] = "tattoo_submenu"
    await update.message.reply_text(
        "✅ Материалы отправлены в салон. **Расчёт и ответ** — в личных сообщениях, когда мастер обработает заявку.\n\n"
        f"Срочно: {PAYMENT_CONTACT}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_tattoo_submenu_keyboard(),
    )

# =================================================================================
# --- MERCH & PAYMENT ---
# =================================================================================

async def route_merch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    catalog = context.user_data.get("merch_catalog") or get_merch_catalog()
    if not catalog:
        await update.message.reply_text("Каталог пуст.", reply_markup=get_main_menu_keyboard())
        context.user_data["state"] = "main_menu"
        return
    context.user_data["merch_catalog"] = catalog
    L = len(catalog)
    quick = context.user_data.get("merch_quick_pick_labels") or []
    if quick and text in quick:
        slot = quick.index(text)
        indices = context.user_data.get("merch_visible_indices") or []
        if slot < 0 or slot >= len(indices):
            return
        cat_i = indices[slot]
        context.user_data["merch_pending_item"] = dict(catalog[cat_i])
        nm = catalog[cat_i].get("name", "товар")
        await update.message.reply_text(
            f"Выбрано: **{nm}**. Нажмите **«купить мерч»** для перехода к оплате.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_merch_reply_markup(context, catalog),
        )
        return
    if text == BTN_MERCH_BUY:
        pending = context.user_data.get("merch_pending_item")
        if not pending:
            await update.message.reply_text(
                "Сначала выберите товар кнопкой **1**, **2** или **3** под последними фото.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_merch_reply_markup(context, catalog),
            )
            return
        await start_merch_payment_flow(update, context, pending)
        return
    if text == BTN_MERCH_NEXT:
        start = (context.user_data.get("merch_window_start", 0) + min(3, L)) % max(L, 1)
        context.user_data["merch_window_start"] = start
        context.user_data.pop("merch_pending_item", None)
        indices = _merch_triple_indices_sequential(start, L)
        context.user_data["merch_visible_indices"] = indices
        context.user_data["merch_quick_pick_labels"] = [
            _merch_quick_pick_label_for_slot(catalog, indices[s], s + 1) for s in range(len(indices))
        ]
        await send_merch_window_photos(update, context, catalog, indices)
        await update.message.reply_text(
            "Следующие **три** позиции по каталогу. Выберите **1–3**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_merch_reply_markup(context, catalog),
        )
        return
    if text == BTN_MERCH_BROWSE:
        context.user_data.pop("merch_pending_item", None)
        indices = _merch_triple_indices_random(L)
        context.user_data["merch_visible_indices"] = indices
        context.user_data["merch_window_start"] = indices[0]
        context.user_data["merch_quick_pick_labels"] = [
            _merch_quick_pick_label_for_slot(catalog, indices[s], s + 1) for s in range(len(indices))
        ]
        await send_merch_window_photos(update, context, catalog, indices)
        await update.message.reply_text(
            "Три **случайных** позиции из всего каталога. Выберите **1–3**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_merch_reply_markup(context, catalog),
        )
        return


async def start_merch_payment_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, item: dict):
    price = int(item.get("price") or 0)
    await start_payment_process(
        update,
        context,
        item_name=item["name"],
        item_price=price,
        payment_cancel_state=None,
        merch_purchase=True,
    )


async def route_merch_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message else ""
    state = context.user_data.get("state")
    if state == "buy_awaiting_delivery_note":
        if text == "Отмена оплаты":
            context.user_data.pop("receipt_message_id", None)
            context.user_data.pop("merch_purchase", None)
            context.user_data.pop("merch_delivery_note", None)
            context.user_data.pop("item_price", None)
            context.user_data.pop("item_name", None)
            catalog = context.user_data.get("merch_catalog") or get_merch_catalog()
            context.user_data["merch_catalog"] = catalog
            context.user_data["state"] = "merch_browsing"
            context.user_data.setdefault("merch_index", 0)
            if catalog:
                await merch_re_send_window(update, context, catalog, intro="Заказ отменён. Каталог:")
            else:
                await update.message.reply_text("Заказ отменён.", reply_markup=get_main_menu_keyboard())
            return
        await handle_merch_delivery_note(update, context)
        return
    if state == "buy_awaiting_receipt" and text == "Я оплатил(а) ✅":
        await process_final_confirmation(update, context)
    elif state == "buy_awaiting_receipt" and text == "Отмена оплаты":
        await cancel_payment(update, context)


async def start_payment_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_name: str,
    item_price: int,
    payment_cancel_state: str | None = None,
    merch_purchase: bool = False,
):
    payload = {
        "state": "buy_awaiting_receipt",
        "item_name": item_name,
        "item_price": int(item_price) if item_price else 0,
        "payment_cancel_state": payment_cancel_state,
    }
    if merch_purchase:
        payload["merch_purchase"] = True
    else:
        context.user_data.pop("merch_purchase", None)
    context.user_data.update(payload)
    if item_price and item_price > 0:
        sum_line = f"Сумма: *{item_price} руб.*"
    else:
        sum_line = "Сумма: *уточняйте у администратора до оплаты*"
    payment_text = (
        f"Покупка: *{item_name}*\n"
        f"{sum_line}\n\n"
        f"{MERCH_DELIVERY_INFO if merch_purchase else ''}"
        f"💳 Оплата по номеру телефона Т-Банк!\n`{PAYMENT_PHONE_NUMBER}`\n\n"
        f"Если не проходит оплата, напишите сюда: {PAYMENT_CONTACT}, будут выданы новые реквизиты.\n\n"
        "Пожалуйста, **сначала пришлите чек об оплате в этот чат** (фото или PDF), затем нажмите кнопку подтверждения."
    )
    await update.message.reply_text(payment_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_payment_confirmation_keyboard())

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['receipt_message_id'] = update.message.message_id
    await update.message.reply_text("✅ Чек получен. Теперь нажмите 'Я оплатил(а) ✅', чтобы завершить покупку.")


async def handle_merch_delivery_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = (update.message.text or "").strip()
    if len(note) < 5:
        await update.message.reply_text("Опишите способ получения чуть подробнее (несколько слов).")
        return
    context.user_data["merch_delivery_note"] = note
    await finalize_merch_purchase(update, context)


async def finalize_merch_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    loading_message = await update.message.reply_text("⏳ Оформляем заказ и запись в таблицу…")
    try:
        item_name = context.user_data.get("item_name") or "—"
        item_price = int(context.user_data.get("item_price") or 0)
        delivery = context.user_data.get("merch_delivery_note") or "—"
        user = update.effective_user
        master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
        today = datetime.date.today()
        date_header = f"{today.day} {RUSSIAN_MONTHS[today.month]}"
        sheet_name = f"{RUSSIAN_MONTHS[today.month]} {today.year}"
        worksheet = await get_worksheet_cached(sheet_name)
        first_row = None
        if worksheet:
            first_row = await find_first_empty_rent_row(worksheet, date_header)
        rid = context.user_data.get("receipt_message_id")
        e_note = (
            f"Мерч: {master_name} | {item_name} | {item_price}₽ | "
            f"получение: {delivery} | чек msg={rid}"
        )
        if worksheet and first_row:
            await asyncio.to_thread(worksheet.update, f"E{first_row}", [[e_note]])
            cache_key = f"{worksheet.title}_{date_header}_slots"
            if cache_key in sheets_cache:
                del sheets_cache[cache_key]
        else:
            logger.error(
                "finalize_merch_purchase: нет свободной строки E на «%s» или лист «%s» — запись в таблицу пропущена",
                date_header,
                sheet_name,
            )

        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🛍 **Мерч оплачен и оформлен**\n{master_name}\nТовар: **{item_name}** · {item_price} ₽\n"
            f"Получение: {delivery}\n"
            f"Таблица: `{sheet_name}` · ячейка **E{first_row or '—'}**",
            parse_mode=ParseMode.MARKDOWN,
        )
        if rid:
            await context.bot.forward_message(ADMIN_CHAT_ID, user_id, rid)

        caption = (
            "[ IKONA AI ]\n"
            "──────────────\n"
            f"Покупка оформлена: **{item_name}**.\n\n"
            f"**Получение:** {delivery}\n\n"
            "Данные переданы администратору. Свяжемся по отправке или самовывозу. Спасибо за выбор IKONA."
        )
        await send_success_gif(context, user_id, caption, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка при оформлении: {e}")
        logger.error("finalize_merch_purchase: %s", e)
    finally:
        await loading_message.delete()
        context.user_data.clear()
        context.user_data["state"] = "main_menu"


async def process_final_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if "receipt_message_id" not in context.user_data:
        await update.message.reply_text("Сначала пришлите чек, пожалуйста.")
        return

    if context.user_data.get("merch_purchase"):
        context.user_data["state"] = "buy_awaiting_delivery_note"
        await update.message.reply_text(
            "✅ Чек принят.\n\n"
            "**Одним сообщением** опишите, как получите заказ:\n"
            "• самовывоз в клубе IKONA (удобное время),\n"
            "• курьер по Москве (адрес и контакт),\n"
            "• доставка в регион или за рубеж (город, способ: СДЭК / почта и т.д.).\n\n"
            "После этого заказ будет зафиксирован, строка попадёт в **Google Таблицу** (столбец **E** на сегодня).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    loading_message = await update.message.reply_text("⏳ Подтверждаем покупку...")
    try:
        item_name = context.user_data.get("item_name")
        user = update.effective_user
        user_info_text = f"Новая оплата от @{user.username}" if user.username else f"Новая оплата от ID {user.id}"
        await context.bot.send_message(ADMIN_CHAT_ID, f"{user_info_text}\nТовар: {item_name}")
        await context.bot.forward_message(ADMIN_CHAT_ID, user_id, context.user_data["receipt_message_id"])

        caption = (
            "[ IKONA AI ]\n"
            "──────────────\n"
            "Транзакция успешно завершена. Информация о покупке передана администратору. Благодарю за выбор IKONA."
        )
        await send_success_gif(context, user_id, caption, get_main_menu_keyboard())
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка при подтверждении: {e}")
        logger.error(f"Error in process_final_confirmation: {e}")
    finally:
        await loading_message.delete()
        context.user_data.clear()
        context.user_data["state"] = "main_menu"

async def cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pcs = context.user_data.pop("payment_cancel_state", None)
    context.user_data.pop("receipt_message_id", None)
    context.user_data.pop("merch_purchase", None)
    context.user_data.pop("merch_delivery_note", None)
    context.user_data.pop("item_price", None)
    if pcs == "training_ai_details":
        context.user_data["state"] = "training_ai_details"
        await update.message.reply_text(
            "Оплата отменена. Можно снова открыть раздел про программу.",
            reply_markup=get_training_ai_keyboard(context),
        )
        return
    catalog = context.user_data.get("merch_catalog") or get_merch_catalog()
    context.user_data["merch_catalog"] = catalog
    context.user_data["state"] = "merch_browsing"
    context.user_data.setdefault("merch_index", 0)
    if catalog:
        await merch_re_send_window(update, context, catalog, intro="Оплата отменена. Каталог:")
    else:
        await update.message.reply_text("Оплата отменена.", reply_markup=get_main_menu_keyboard())

# =================================================================================
# --- CHAT MODULE (REFACTORED FOR STABILITY) ---
# =================================================================================

def _ikona_ai_prune_message_times(context: ContextTypes.DEFAULT_TYPE) -> list[float]:
    """Метки времени пользовательских запросов к IKONA ИИ за последние 24 ч (wall clock, для persistence)."""
    now = time.time()
    cutoff = now - IKONA_AI_USER_RATE_WINDOW_SEC
    raw = context.user_data.get(IKONA_AI_USER_MSG_TIMES_KEY) or []
    times = sorted(float(t) for t in raw if float(t) >= cutoff)
    context.user_data[IKONA_AI_USER_MSG_TIMES_KEY] = times
    return times


def _ikona_ai_record_user_message_sent(context: ContextTypes.DEFAULT_TYPE) -> None:
    times = _ikona_ai_prune_message_times(context)
    times.append(time.time())
    context.user_data[IKONA_AI_USER_MSG_TIMES_KEY] = times


def _ikona_ai_rate_limit_wait_text(oldest_ts: float) -> str:
    free_at = oldest_ts + IKONA_AI_USER_RATE_WINDOW_SEC
    secs = max(0, int(free_at - time.time() + 0.999))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    when = datetime.datetime.fromtimestamp(free_at, _IKONA_AI_MSK).strftime("%d.%m.%Y %H:%M")
    tz = "МСК" if _IKONA_AI_MSK != datetime.timezone.utc else "UTC"
    if h > 0:
        wait = f"{h} ч {m} мин"
    elif m > 0:
        wait = f"{m} мин"
    else:
        wait = f"{s} с"
    return f"Следующее сообщение — примерно через **{wait}** (окно 24 ч, до **{when}** {tz})."


async def route_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state in ("chat_menu", "info_chat_submenu"):
        await handle_chat_choice(update, context)
    elif state == "ai_chat":
        await handle_ai_message(update, context)


async def handle_chat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_INFO_AI:
        context.user_data.update(
            {"state": "ai_chat", "history": [IKONA_ASSISTANT_SYSTEM_PROMPT], "ai_lock": asyncio.Lock()}
        )
        used = len(_ikona_ai_prune_message_times(context))
        left = max(0, IKONA_AI_MAX_USER_MESSAGES_24H - used)
        lim = (
            f"\n\nНа вас действует лимит: **{IKONA_AI_MAX_USER_MESSAGES_24H}** сообщений к ИИ за **24 часа** "
            f"(скользящее окно). Сейчас доступно: **{left}**."
        )
        await update.message.reply_text(
            "Напишите вопрос **IKONA ИИ помощнику** (ответы через Polza.ai, GPT‑5.3 Chat):"
            + lim,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_ai_chat_exit_keyboard(),
        )
    elif text == BTN_INFO_MASTERS:
        await update.message.reply_text(f"Чат мастеров: {MASTERS_CHAT_LINK}", reply_markup=get_info_chat_submenu_keyboard())
    elif text == BTN_INFO_SUPPORT:
        await update.message.reply_text(
            f"Тех.поддержка: {PAYMENT_CONTACT}", reply_markup=get_info_chat_submenu_keyboard()
        )

async def handle_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id

    if user_message == "Выйти из чата с AI":
        context.user_data.pop("ai_lock", None)
        context.user_data.pop("history", None)
        context.user_data["state"] = "info_chat_submenu"
        await update.message.reply_text(
            "Вы вышли из чата с ИИ.", reply_markup=get_info_chat_submenu_keyboard()
        )
        return

    if not POLZA_IKONA_CHAT_API_KEY:
        await update.message.reply_text(
            "Чат IKONA через Polza не настроен: задайте переменную окружения **POLZA_IKONA_CHAT_API_KEY**.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    times = _ikona_ai_prune_message_times(context)
    if len(times) >= IKONA_AI_MAX_USER_MESSAGES_24H:
        oldest = min(times)
        await update.message.reply_text(
            f"Лимит **{IKONA_AI_MAX_USER_MESSAGES_24H}** сообщений к IKONA ИИ за **24 часа** исчерпан.\n\n"
            + _ikona_ai_rate_limit_wait_text(oldest),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_ai_chat_exit_keyboard(),
        )
        return

    if 'ai_lock' not in context.user_data:
        context.user_data['ai_lock'] = asyncio.Lock()

    user_lock = context.user_data['ai_lock']

    if user_lock.locked():
        await update.message.reply_text("Подождите, я еще думаю над предыдущим вопросом...")
        return

    async with user_lock:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        history = context.user_data.get("history", [IKONA_ASSISTANT_SYSTEM_PROMPT])
        history.append({"role": "user", "content": user_message})
        if len(history) > 24:
            history = [history[0], *history[-(24 - 1) :]]

        headers = {
            "Authorization": f"Bearer {POLZA_IKONA_CHAT_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": POLZA_IKONA_CHAT_MODEL,
            "messages": history,
            "temperature": 0.85,
        }

        try:
            http_client = context.bot_data["http_client"]
            url = f"{POLZA_API_BASE}/chat/completions"
            response = await http_client.post(url, headers=headers, json=payload, timeout=90.0)
            response.raise_for_status()
            data = response.json()
            choice0 = (data.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            ai_response = (msg.get("content") or "").strip()
            if not ai_response:
                raise ValueError(f"Пустой ответ Polza: {repr(data)[:400]}")

            history.append({"role": "assistant", "content": ai_response})
            context.user_data["history"] = history
            _ikona_ai_record_user_message_sent(context)
            used_after = len(context.user_data.get(IKONA_AI_USER_MSG_TIMES_KEY) or [])
            left_after = max(0, IKONA_AI_MAX_USER_MESSAGES_24H - used_after)

            novel_style_response = ai_response
            if 0 < left_after <= 4:
                novel_style_response += f"\n\n_(сообщений к ИИ до лимита 24ч: {left_after})_"
            reaction_gif = _ikona_ai_reaction_gif_for_response(ai_response)
            await safe_send_animation(context, chat_id, reaction_gif, novel_style_response)

        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.text[:500]
            except Exception:
                pass
            logger.error("Polza chat HTTP error user=%s: %s %s", chat_id, e, detail)
            await context.bot.send_message(
                chat_id,
                "Не удалось получить ответ от Polza (проверьте ключ и баланс). Попробуйте позже.",
            )
        except httpx.TimeoutException:
            logger.error("Timeout Polza chat for user %s", chat_id)
            await context.bot.send_message(chat_id, "ИИ слишком долго отвечает. Попробуйте короче или позже.")
        except httpx.RequestError as e:
            logger.error("Polza request error user=%s: %s", chat_id, e)
            await context.bot.send_message(chat_id, "Сеть недоступна. Попробуйте позже.")
        except Exception as e:
            logger.error("handle_ai_message Polza user=%s: %s", chat_id, e)
            await context.bot.send_message(chat_id, "Внутренняя ошибка чата. Попробуйте снова.")
            context.user_data["history"] = [IKONA_ASSISTANT_SYSTEM_PROMPT]

# =================================================================================
# --- CASINO IKONA: зал, удача на смену, VIP‑лотерея тату ---
# =================================================================================


def _casino_worksheet_title(year: int, month: int) -> str:
    return f"{RUSSIAN_MONTHS[month]} {year}"


def _casino_win_probability(user_id: int) -> float:
    if user_id in CASINO_CREATOR_TELEGRAM_IDS:
        return CASINO_WIN_PROB_CREATOR
    return CASINO_WIN_PROB_DEFAULT


async def _casino_row_owned_by(worksheet, row: int, user_id: int) -> bool:
    row_vals = await asyncio.to_thread(worksheet.row_values, row)
    if not row_vals:
        return False
    master = str(row_vals[0])
    return f"id:{user_id}" in master or str(user_id) in master


async def _casino_cancel_row_casino_loss(worksheet, row: int, user):
    row_data = await asyncio.to_thread(worksheet.row_values, row)
    master_name = row_data[0] if len(row_data) > 0 else ""
    time_info = row_data[1] if len(row_data) > 1 else ""
    cancel_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    canceled_data = f"{master_name} | {time_info} | отменена {cancel_time} | казино-проигрыш"
    await asyncio.to_thread(
        worksheet.update,
        f"A{row}:E{row}",
        [["", "", "", "отменен", canceled_data]],
    )


async def _casino_log_win_in_column_e(context: ContextTypes.DEFAULT_TYPE, user, credits_after: int):
    today = datetime.date.today()
    date_header = f"{today.day} {RUSSIAN_MONTHS[today.month]}"
    sheet_name = f"{RUSSIAN_MONTHS[today.month]} {today.year}"
    worksheet = await get_worksheet_cached(sheet_name)
    if not worksheet:
        return
    first_row = await find_first_empty_rent_row(worksheet, date_header)
    if not first_row:
        return
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    note = f"Казино: выигрыш +1 бесплатная смена | {lab} id:{user.id} | кредитов={credits_after}"
    await asyncio.to_thread(worksheet.update, f"E{first_row}", [[note]])
    cache_key = f"{worksheet.title}_{date_header}_slots"
    if cache_key in sheets_cache:
        del sheets_cache[cache_key]


CASINO_WIN_FEED_FILE = os.path.join(SCRIPT_DIR, "casino_win_feed.json")
CASINO_WIN_FEED_MAX_ENTRIES = 120
CASINO_WIN_FEED_LOBBY_LINES = 7
CASINO_WIN_FEED_FULL_LINES = 28
_casino_win_feed_lock = threading.Lock()

CASINO_SPIN_LINES = (
    "🎰 · · ·\n_колёсико нервно дышит…_",
    "✨ 🎰 · ·\n_ещё мгновение, и узнаем судьбу…_",
    "💫 ✨ 🎰\n_почти видно отблеск выигрыша…_",
    "🎰 💫 ✨\n_последний виток, держим ладошки…_",
)


async def _casino_append_note_column_e(note: str) -> None:
    today = datetime.date.today()
    date_header = f"{today.day} {RUSSIAN_MONTHS[today.month]}"
    sheet_name = f"{RUSSIAN_MONTHS[today.month]} {today.year}"
    worksheet = await get_worksheet_cached(sheet_name)
    if not worksheet:
        return
    first_row = await find_first_empty_rent_row(worksheet, date_header)
    if not first_row:
        return
    await asyncio.to_thread(worksheet.update, f"E{first_row}", [[note]])
    cache_key = f"{worksheet.title}_{date_header}_slots"
    if cache_key in sheets_cache:
        del sheets_cache[cache_key]


def _casino_win_feed_anon_label(user) -> str:
    if getattr(user, "username", None):
        u = user.username.strip()
        if len(u) <= 3:
            return f"@{u[0]}***"
        return f"@{u[:2]}***{u[-1]}"
    uid = getattr(user, "id", 0) or 0
    return f"гость ···{str(uid)[-4:]}"


def _casino_win_feed_load_sync() -> list:
    with _casino_win_feed_lock:
        try:
            with open(CASINO_WIN_FEED_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _casino_win_feed_append_sync(kind: str, anon: str) -> None:
    with _casino_win_feed_lock:
        try:
            with open(CASINO_WIN_FEED_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        data.append({"ts": time.time(), "kind": kind, "who": anon})
        data = data[-CASINO_WIN_FEED_MAX_ENTRIES:]
        with open(CASINO_WIN_FEED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


async def _casino_win_feed_record(kind: str, user) -> None:
    try:
        anon = _casino_win_feed_anon_label(user)
        await asyncio.to_thread(_casino_win_feed_append_sync, kind, anon)
    except Exception as e:
        logger.error("casino win feed record: %s", e)


def _casino_win_feed_recent_24h_count() -> int:
    now = time.time()
    return sum(1 for r in _casino_win_feed_load_sync() if now - float(r.get("ts") or 0) < 86400)


def _casino_win_feed_lines_for_display(rows: list, max_lines: int, *, reverse_chronological: bool = True) -> list:
    if not rows:
        return []
    chunk = rows[-max_lines:]
    if reverse_chronological:
        chunk = list(reversed(chunk))
    out = []
    for r in chunk:
        ts = float(r.get("ts") or 0)
        who = str(r.get("who") or "гость")
        kind = str(r.get("kind") or "")
        if kind == "tattoo_vip":
            prize = "VIP‑сеанс тату **15 000 ₽**"
        elif kind == "train_off":
            prize = "курс **оффлайн 140 000 ₽**"
        elif kind == "train_on":
            prize = "курс **онлайн 99 000 ₽**"
        elif kind == "train_ai":
            prize = "**ИИ‑программа 15 000 ₽**"
        else:
            prize = "**+1 бесплатная смена**"
        dt = datetime.datetime.fromtimestamp(ts, _IKONA_AI_MSK)
        out.append(f"• {who} — {prize} · _{dt.strftime('%d.%m %H:%M')}_")
    return out


def _casino_win_feed_lobby_block() -> str:
    rows = _casino_win_feed_load_sync()
    n24 = _casino_win_feed_recent_24h_count()
    bait = (
        f"\n\n📣 **Зал шепчет:** за последние сутки зафиксировано **{n24}** побед — "
        "кто-то уже забирает бонусы, пока другие только размышляют. "
        "Иногда фортуна любит тех, кто нажал кнопку чуть раньше соседа ✨\n\n"
        "🏆 **Недавние победы гостей зала:**\n"
    )
    if not rows:
        return (
            bait
            + "_Пока тихо — зато ваше имя может стать первым в этой красивой истории. "
            "Один жест — и барабан уже о вас расскажет 💎_\n\n"
            "_P.S. Если вы уже выигрывали до обновления бота — следующий занос попадёт сюда автоматически._"
        )
    lines = _casino_win_feed_lines_for_display(rows, CASINO_WIN_FEED_LOBBY_LINES)
    tail = (
        "\n\n💫 Маленький секрет администратора: чаще всего после «первого лёгкого захода» гости возвращаются за вторым — "
        "ведь азарт, как хороший кофе, бодрит 💕"
    )
    return bait + "\n".join(lines) + tail


def _casino_win_feed_full_message() -> str:
    rows = _casino_win_feed_load_sync()
    head = (
        "🏆 **Зал славы IKONA**\n\n"
        "Здесь — только **реальные** заносы после розыгрышей в боте (ники слегка прячем, чтобы никому не мешать). "
        "Листайте вдохновение и возвращайтесь в зал, когда сердце скажет «ещё чуть-чуть» 💎\n\n"
    )
    if not rows:
        return head + "_Пока пусто — самое время стать первой строкой в легенде._"
    lines = _casino_win_feed_lines_for_display(rows, CASINO_WIN_FEED_FULL_LINES)
    return head + "\n".join(lines)


def _casino_retention_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💫 Снова в зал IKONA", callback_data="ch_hub")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="ch_close")],
        ]
    )


def _casino_free_rent_credits(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.user_data.get("casino_free_rent_credits") or 0)


def _casino_hub_markup(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎡 Удача на смену", callback_data="ch_rent")],
        [InlineKeyboardButton("🎁 Лотереи призов · тату / обучение", callback_data="ch_lotroot")],
    ]
    bonus = int(context.user_data.get("casino_free_rent_credits") or 0)
    if bonus > 0:
        rows.append([
            InlineKeyboardButton(
                f"🎁 Мои выигранные смены ({bonus})",
                callback_data="ch_bonus",
            )
        ])
    bonus = _casino_free_rent_credits(context)
    if bonus > 0:
        rows.append([
            InlineKeyboardButton(
                f"🎁 Записать бесплатную смену · {bonus}",
                callback_data="ch_bonus",
            )
        ])
    spin_btns = []
    nt = int(context.user_data.get(CASINO_TATTOO_LOTTERY_SPINS_KEY) or 0)
    if nt > 0:
        spin_btns.append(InlineKeyboardButton(f"🎰 Тату×{nt}", callback_data="crs_t"))
    no = int(context.user_data.get(CASINO_TRAIN_OFF_SPINS_KEY) or 0)
    if no > 0:
        spin_btns.append(InlineKeyboardButton(f"🎓Офф×{no}", callback_data="crs_o"))
    nn = int(context.user_data.get(CASINO_TRAIN_ON_SPINS_KEY) or 0)
    if nn > 0:
        spin_btns.append(InlineKeyboardButton(f"🎓Онл×{nn}", callback_data="crs_n"))
    na = int(context.user_data.get(CASINO_TRAIN_AI_SPINS_KEY) or 0)
    if na > 0:
        spin_btns.append(InlineKeyboardButton(f"🤖ИИ×{na}", callback_data="crs_a"))
    for i in range(0, len(spin_btns), 3):
        rows.append(spin_btns[i : i + 3])
    rows.append([
        InlineKeyboardButton("🏆 Зал славы", callback_data="ch_feed"),
        InlineKeyboardButton("🛡 Как мы доказываем честность", callback_data="csfair"),
    ])
    rows.append([InlineKeyboardButton("🏠 Выйти из зала", callback_data="ch_close")])
    return InlineKeyboardMarkup(rows)


def _casino_lobby_caption(context: ContextTypes.DEFAULT_TYPE) -> str:
    bonus = int(context.user_data.get("casino_free_rent_credits") or 0)
    st = int(context.user_data.get(CASINO_TATTOO_LOTTERY_SPINS_KEY) or 0)
    tv = int(context.user_data.get(CASINO_TATTOO_VOUCHERS_KEY) or 0)
    so = int(context.user_data.get(CASINO_TRAIN_OFF_SPINS_KEY) or 0)
    sn = int(context.user_data.get(CASINO_TRAIN_ON_SPINS_KEY) or 0)
    sa = int(context.user_data.get(CASINO_TRAIN_AI_SPINS_KEY) or 0)
    vo = int(context.user_data.get(CASINO_VOUCHER_TRAIN_OFF_KEY) or 0)
    vn = int(context.user_data.get(CASINO_VOUCHER_TRAIN_ON_KEY) or 0)
    va = int(context.user_data.get(CASINO_VOUCHER_TRAIN_AI_KEY) or 0)
    bonus_block = ""
    if bonus > 0:
        bonus_block = (
            f"\n\n🎁 **Выигранные смены:** **{bonus}** — активируйте кнопкой "
            "**«🎁 Мои выигранные смены»** в зале или через **аренда → «"
            f"{BTN_RENT_CASINO_BONUS}»**."
        )
    return (
        "✨ **Приватный зал IKONA** · provably fair\n\n"
        "🎡 **Удача на смену** — ставка: ваша оплаченная смена. "
        f"Выигрыш → **+1 бесплатная смена** (можно записать сразу или позже). "
        "Проигрыш → смена **снимается** без возврата.\n\n"
        "🎁 **Лотереи призов** — отдельная витрина: **VIP-тату**, **оффлайн-курс**, **онлайн-курс**, **ИИ-программа**.\n\n"
        "🎲 **Как работает раунд:** поле из **100 ячеек**, **одна** из них — победная. "
        "Вам выдаётся **N фишек** на размещение (для аренды — 33, шанс победы около 33%). "
        "**До** ваших ходов мы публикуем commit (SHA-256) и ID раунда. "
        "После «крутки» сервер раскрывает server seed — и любой повторяет расчёт "
        f"в публичном калькуляторе SHA-256 ({CASINO_FAIR_VERIFY_URL}). "
        "Подменить результат **технически невозможно**."
        f"{bonus_block}\n\n"
        f"**Ваш счёт:** бесплатных смен **{bonus}** · фишки тату **{st}** / оффлайн **{so}** / онлайн **{sn}** / ИИ **{sa}** · "
        f"призы: тату **{tv}** · оффлайн **{vo}** · онлайн **{vn}** · ИИ **{va}**\n\n"
        f"Вопросы / разбор раунда: {PAYMENT_CONTACT} 💕"
        + _casino_win_feed_lobby_block()
    )


async def _send_casino_lobby_message(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _casino_lobby_caption(context)
    markup = _casino_hub_markup(context)
    try:
        await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        logger.warning("casino lobby markdown failed (%s); plain fallback", e)
        await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


def _casino_tattoo_win_probability(user_id: int) -> float:
    if user_id in CASINO_CREATOR_TELEGRAM_IDS:
        return CASINO_TATTOO_WIN_P_CREATOR
    return CASINO_TATTOO_WIN_P_DEFAULT


def _casino_train_win_probability(kind: str, user_id: int) -> float:
    cr = user_id in CASINO_CREATOR_TELEGRAM_IDS
    if kind == "off":
        return CASINO_TRAIN_OFF_WIN_P_CREATOR if cr else CASINO_TRAIN_OFF_WIN_P_DEFAULT
    if kind == "on":
        return CASINO_TRAIN_ON_WIN_P_CREATOR if cr else CASINO_TRAIN_ON_WIN_P_DEFAULT
    if kind == "ai":
        return CASINO_TRAIN_AI_WIN_P_CREATOR if cr else CASINO_TRAIN_AI_WIN_P_DEFAULT
    return 0.0


async def _casino_run_spin_animation_on_message(msg, final_teaser: str | None = None) -> None:
    for line in CASINO_SPIN_LINES:
        try:
            await msg.edit_text(line, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        except BadRequest:
            pass
        await asyncio.sleep(0.38)
    if final_teaser:
        try:
            await msg.edit_text(final_teaser, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        except BadRequest:
            pass
        await asyncio.sleep(0.32)


async def finalize_free_rent_bonus_booking(query, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_rent_booking")
    if not pending:
        await query.message.reply_text("Сессия сброшена.")
        return
    cred = int(context.user_data.get("casino_free_rent_credits") or 0)
    if cred < 1:
        await query.message.reply_text("Нет бонусных смен.")
        return
    context.user_data["casino_free_rent_credits"] = cred - 1
    user = query.from_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    tm = pending["time"]
    supply_label = pending["supply_label"]
    total = pending["total"]
    time_display = f"{tm} смена | {supply_label} | бонус казино 0₽ (экв. {total}₽)"
    loading = await query.message.reply_text("⏳ Записываю бесплатную смену…")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            context.user_data["casino_free_rent_credits"] = cred
            await query.message.reply_text("Не удалось открыть расписание.", reply_markup=get_main_menu_keyboard())
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            context.user_data["casino_free_rent_credits"] = cred
            await query.message.reply_text("Нет свободной строки на эту дату.", reply_markup=get_main_menu_keyboard())
            return
        await asyncio.to_thread(
            worksheet.update,
            f"A{first_row}:E{first_row}",
            [[master_name, time_display, "оплачено", "активна", "Казино: списан 1 бесплатный кредит"]],
        )
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        context.user_data.pop("pending_rent_booking", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        context.user_data["state"] = "main_menu"
        cap = (
            f"✨ **Готово — бесплатная смена бережно записана** (бонусов осталось: **{cred - 1}**)\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {tm}\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            "Желаю ровных линий и хорошего настроения 💕"
        )
        await send_success_gif(context, query.message.chat_id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🎁 **Бесплатная смена (казино-кредит)**\n{master_name}\n{time_display}\nЛист `{worksheet.title}` строка **{first_row}**",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("finalize_free_rent_bonus_booking: %s", e)
        context.user_data["casino_free_rent_credits"] = cred
        await query.message.reply_text("Ошибка записи. Напишите администратору.", reply_markup=get_main_menu_keyboard())
    finally:
        await loading.delete()


async def handle_open_casino_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "casino_submenu"
    await _send_casino_lobby_message(update.message, context)


async def handle_casino_hub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "ch_close":
        context.user_data["state"] = "main_menu"
        await query.message.reply_text(
            "Конечно ✨ Если снова захочется лёгкого азарта — **casino** всегда ждёт вас. "
            "Я с удовольствием всё подскажу: " + PAYMENT_CONTACT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(),
        )
        return
    if data == "ch_hub":
        context.user_data["state"] = "casino_submenu"
        await _send_casino_lobby_message(query.message, context)
        return
    if data == "ch_bonus":
        context.user_data["state"] = "casino_submenu"
        await _start_casino_rent_bonus_booking(query, context)
        return
    if data == "ch_rentbook":
        await _start_rent_booking_from_casino_query(query, context)
        return
    if data == "ch_feed":
        context.user_data["state"] = "casino_submenu"
        body = _casino_win_feed_full_message()
        if len(body) > 4000:
            body = body[:3980] + "\n\n_…показана часть списка — новые победы всегда внизу ленты._"
        await query.message.reply_text(
            body,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        return
    if data == "ch_lotroot":
        await query.message.reply_text(
            "🎁 **Лотереи призов**\n\n"
            f"• **VIP тату** — билет **{CASINO_TATTOO_TICKET_PRICE} ₽** → сеанс **{CASINO_TATTOO_PRIZE_FACE} ₽**\n"
            f"• **Оффлайн‑курс** — **{CASINO_TRAIN_OFFLINE_TICKET} ₽** → полный курс **{TRAINING_OFFLINE_PRICE:,} ₽**\n"
            f"• **Онлайн‑курс** — **{CASINO_TRAIN_ONLINE_TICKET} ₽** → полный курс **{TRAINING_ONLINE_PRICE:,} ₽**\n"
            f"• **ИИ‑программа** — **{CASINO_TRAIN_AI_TICKET} ₽** → доступ **{TRAINING_AI_PROGRAM_PRICE:,} ₽**\n\n"
            "_Математика «дома»_: шансы низкие, ожидание в плюсе салона — но адреналин настоящий ✨\n\n"
            "Выберите игру ниже — дальше подскажу по оплате и барабану.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("💎 VIP тату", callback_data="ch_tattoo"),
                        InlineKeyboardButton("🎓 Оффлайн", callback_data="ch_tro"),
                    ],
                    [
                        InlineKeyboardButton("🎓 Онлайн", callback_data="ch_trn"),
                        InlineKeyboardButton("🤖 ИИ", callback_data="ch_tra"),
                    ],
                    [InlineKeyboardButton("← В зал", callback_data="ch_hub")],
                ]
            ),
        )
        return
    if data == "ch_tro":
        context.user_data["state"] = "casino_training_lottery_payment"
        context.user_data[CASINO_TRAIN_PAY_KIND_KEY] = "off"
        context.user_data.pop("casino_training_lottery_receipt_message_id", None)
        await query.message.reply_text(
            f"🎓 **Билет: оффлайн‑курс**\n\n"
            f"Сумма: **{CASINO_TRAIN_OFFLINE_TICKET} ₽** за **один** запуск барабана.\n"
            f"Приз: полный курс **{TRAINING_OFFLINE_PRICE:,} ₽** (по правилам IKONA после согласования).\n\n"
            f"Реквизиты: `{PAYMENT_PHONE_NUMBER}`\n"
            f"Поддержка: {PAYMENT_CONTACT}\n\n"
            "Чек **фото/PDF** → **«Я оплатил(а) ✅»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )
        await query.message.reply_text("Кнопки подтверждения — ниже ☺️", parse_mode=ParseMode.MARKDOWN)
        return
    if data == "ch_trn":
        context.user_data["state"] = "casino_training_lottery_payment"
        context.user_data[CASINO_TRAIN_PAY_KIND_KEY] = "on"
        context.user_data.pop("casino_training_lottery_receipt_message_id", None)
        await query.message.reply_text(
            f"🎓 **Билет: онлайн‑курс**\n\n"
            f"Сумма: **{CASINO_TRAIN_ONLINE_TICKET} ₽** за **один** запуск.\n"
            f"Приз: полный курс **{TRAINING_ONLINE_PRICE:,} ₽**.\n\n"
            f"Реквизиты: `{PAYMENT_PHONE_NUMBER}`\n"
            f"Поддержка: {PAYMENT_CONTACT}\n\n"
            "Чек **фото/PDF** → **«Я оплатил(а) ✅»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )
        await query.message.reply_text("Кнопки подтверждения — ниже ☺️", parse_mode=ParseMode.MARKDOWN)
        return
    if data == "ch_tra":
        context.user_data["state"] = "casino_training_lottery_payment"
        context.user_data[CASINO_TRAIN_PAY_KIND_KEY] = "ai"
        context.user_data.pop("casino_training_lottery_receipt_message_id", None)
        await query.message.reply_text(
            f"🤖 **Билет: ИИ‑программа**\n\n"
            f"Сумма: **{CASINO_TRAIN_AI_TICKET} ₽** за **один** запуск.\n"
            f"Приз: полный доступ **{TRAINING_AI_PROGRAM_PRICE:,} ₽**.\n\n"
            f"Реквизиты: `{PAYMENT_PHONE_NUMBER}`\n"
            f"Поддержка: {PAYMENT_CONTACT}\n\n"
            "Чек **фото/PDF** → **«Я оплатил(а) ✅»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )
        await query.message.reply_text("Кнопки подтверждения — ниже ☺️", parse_mode=ParseMode.MARKDOWN)
        return
    if data == "ch_rent":
        uid = query.from_user.id
        bookings = await get_user_bookings(uid)
        paid = [b for b in bookings if "оплач" in str(b.get("payment", "")).lower()]
        rows = []
        for b in paid[:15]:
            ws_title = b["worksheet"]
            try:
                year = int(ws_title.split()[-1])
                month_name = ws_title.split()[0]
                month_num = next(k for k, v in RUSSIAN_MONTHS.items() if v.lower() == month_name.lower())
            except Exception:
                continue
            cb = f"crs_r_{year}_{month_num}_{b['row']}"
            label = f"🎡 {b['date']} · стр.{b['row']}"
            if len(label) > 60:
                label = label[:57] + "…"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        if not rows:
            await query.message.reply_text(
                "🎡 **Удача на смену** — только со **своей оплаченной активной сменой**.\n\n"
                "Сейчас таких записей нет. Сначала **запишитесь на аренду** и дождитесь статуса **оплачено** — "
                "после этого здесь появится список смен для ставки.\n\n"
                "Можно сразу открыть календарь аренды или вернуться в зал.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📅 Записаться на аренду", callback_data="ch_rentbook")],
                        [InlineKeyboardButton("← В зал IKONA", callback_data="ch_hub")],
                    ]
                ),
            )
            return
        rows.append([InlineKeyboardButton("← В зал IKONA", callback_data="ch_hub")])
        await query.message.reply_text(
            "🎡 **Удача на смену**\n\n"
            "Выберите **одну** оплаченную смену ниже — откроется поле из **100 ячеек** "
            "и **33 фишки** на размещение (шанс победы **33%**).\n\n"
            "🛡 **Provably fair:** до ваших ходов мы публикуем commit (SHA-256) и ID раунда. "
            "После «крутки» раскрываем server seed — любой воспроизведёт расчёт в публичном "
            f"SHA-256 калькуляторе ({CASINO_FAIR_VERIFY_URL}). Подменить результат невозможно.\n\n"
            "Победа → **+1 бесплатная смена** + можно сразу выбрать дату.\n"
            "Поражение → выбранная смена **снимается без возврата**.\n\n"
            "Я держу за вас кулачки 💕",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=True,
        )
        return
    if data == "ch_tattoo":
        await query.message.reply_text(
            "💎 **VIP‑лотерея «Сеанс мечты»**\n\n"
            f"Билет всего **{CASINO_TATTOO_TICKET_PRICE} ₽** — и перед вами шанс выиграть **сеанс на {CASINO_TATTOO_PRIZE_FACE} ₽** "
            "(любая татуировка по согласованию с IKONA).\n\n"
            "Это отдельная игра: деньги за билет **не возвращаются**, один билет — **один** запуск барабана. "
            "Математика настроена так, чтобы салон оставался в комфортном плюсе — честно и по‑деловому.\n\n"
            "Загляните в **«Зал славы»** в лобби — там живые истории гостей, которые уже поймали удачу за хвост 💫\n\n"
            "Нажмите кнопку ниже — я проведу к оплате так же бережно, как к лучшему гостю.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(f"Купить билет · {CASINO_TATTOO_TICKET_PRICE} ₽", callback_data="ch_tpay")],
                    [InlineKeyboardButton("← В зал IKONA", callback_data="ch_hub")],
                ]
            ),
        )
        return
    if data == "ch_tpay":
        context.user_data["state"] = "casino_tattoo_payment"
        context.user_data.pop("casino_tattoo_lottery_receipt_message_id", None)
        await query.message.reply_text(
            f"💎 **Оплата VIP‑билета**\n\n"
            f"Сумма: **{CASINO_TATTOO_TICKET_PRICE} ₽** (один запуск).\n"
            f"Приз при выигрыше: сеанс до **{CASINO_TATTOO_PRIZE_FACE} ₽**.\n\n"
            f"Реквизиты: `{PAYMENT_PHONE_NUMBER}`\n"
            f"Поддержка: {PAYMENT_CONTACT}\n\n"
            "Пришлите **фото или PDF чека** в этот чат, затем нажмите **«Я оплатил(а) ✅»** — я передам администратору и начислю билет.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )
        await query.message.reply_text(
            "Кнопки подтверждения — чуть ниже ☺️ Если передумаете — **«Отмена оплаты»**.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return


# Старые spin-handlers удалены — теперь раунды идут через handle_casino_round_callback.


async def route_casino_tattoo_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message else ""
    if text == "Я оплатил(а) ✅":
        await finalize_casino_tattoo_ticket_payment(update, context)
    elif text == "Отмена оплаты":
        context.user_data.pop("casino_tattoo_lottery_receipt_message_id", None)
        context.user_data["state"] = "casino_submenu"
        await update.message.reply_text(
            "Хорошо, билет не оформляем — без обид 💕 Если захотите снова, я всё подготовлю.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _send_casino_lobby_message(update.message, context)
    else:
        await update.message.reply_text(
            f"Пришлите, пожалуйста, **чек** на **{CASINO_TATTOO_TICKET_PRICE} ₽** (фото или PDF), "
            "затем **«Я оплатил(а) ✅»** или **«Отмена оплаты»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )


async def handle_casino_tattoo_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["casino_tattoo_lottery_receipt_message_id"] = update.message.message_id
    await update.message.reply_text(
        "Чек получила, спасибо 💕 Теперь нажмите **«Я оплатил(а) ✅»**, чтобы я передала его администратору.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_payment_confirmation_keyboard(),
    )
    try:
        u = update.effective_user
        lab = f"@{u.username}" if u.username else f"id:{u.id}"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📎 **Чек VIP‑лотереи тату ({CASINO_TATTOO_TICKET_PRICE} ₽)** от {lab}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error("handle_casino_tattoo_payment_receipt admin: %s", e)


async def finalize_casino_tattoo_ticket_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "casino_tattoo_lottery_receipt_message_id" not in context.user_data:
        await update.message.reply_text(
            "Сначала пришлите чек в чат, пожалуйста — я рядом и подожду ☺️",
            reply_markup=get_payment_confirmation_keyboard(),
        )
        return
    user = update.effective_user
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    spins = int(context.user_data.get(CASINO_TATTOO_LOTTERY_SPINS_KEY) or 0) + 1
    context.user_data[CASINO_TATTOO_LOTTERY_SPINS_KEY] = spins
    note = f"Казино VIP тату: оплата билета {CASINO_TATTOO_TICKET_PRICE}₽ | {lab} id:{user.id} | спинов={spins}"
    await _casino_append_note_column_e(note)
    rid = context.user_data["casino_tattoo_lottery_receipt_message_id"]
    context.user_data.pop("casino_tattoo_lottery_receipt_message_id", None)
    context.user_data["state"] = "casino_submenu"
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ **VIP‑билет казино оплачен** {user.id}\n{lab}\nСпинов на счёте: **{spins}**",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, rid)
    except Exception as e:
        logger.error("finalize_casino_tattoo_ticket_payment admin: %s", e)
    await update.message.reply_text(
        f"Спасибо за доверие ✨ Билет **зачислен**: доступно запусков барабана — **{spins}**.\n\n"
        "Нажмите **«🎰 Крутить барабан»** в следующем сообщении или снова откройте **casino**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await _send_casino_lobby_message(update.message, context)


CASINO_TRAIN_LOTTERY_RECEIPT_KEY = "casino_training_lottery_receipt_message_id"


async def route_casino_training_lottery_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message else ""
    kind = context.user_data.get(CASINO_TRAIN_PAY_KIND_KEY)
    if not kind:
        context.user_data["state"] = "casino_submenu"
        await update.message.reply_text(
            "Сессия оплаты сброшена. Откройте **Лотереи призов** в зале снова.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        return
    ticket = (
        CASINO_TRAIN_OFFLINE_TICKET
        if kind == "off"
        else CASINO_TRAIN_ONLINE_TICKET
        if kind == "on"
        else CASINO_TRAIN_AI_TICKET
    )
    if text == "Я оплатил(а) ✅":
        await finalize_casino_training_ticket_payment(update, context)
    elif text == "Отмена оплаты":
        context.user_data.pop(CASINO_TRAIN_LOTTERY_RECEIPT_KEY, None)
        context.user_data.pop(CASINO_TRAIN_PAY_KIND_KEY, None)
        context.user_data["state"] = "casino_submenu"
        await update.message.reply_text("Хорошо, билет не оформляем 💕", parse_mode=ParseMode.MARKDOWN)
        await _send_casino_lobby_message(update.message, context)
    else:
        await update.message.reply_text(
            f"Пришлите **чек** на **{ticket} ₽** (фото или PDF), затем **«Я оплатил(а) ✅»** или **«Отмена оплаты»**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_confirmation_keyboard(),
        )


async def handle_casino_training_lottery_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[CASINO_TRAIN_LOTTERY_RECEIPT_KEY] = update.message.message_id
    await update.message.reply_text(
        "Чек получила 💕 Теперь **«Я оплатил(а) ✅»**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_payment_confirmation_keyboard(),
    )
    try:
        u = update.effective_user
        lab = f"@{u.username}" if u.username else f"id:{u.id}"
        k = context.user_data.get(CASINO_TRAIN_PAY_KIND_KEY) or "?"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📎 **Чек лотереи обучения ({k})** от {lab}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error("handle_casino_training_lottery_receipt admin: %s", e)


async def finalize_casino_training_ticket_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = context.user_data.get(CASINO_TRAIN_PAY_KIND_KEY)
    if kind not in ("off", "on", "ai"):
        await update.message.reply_text("Сессия сброшена. Откройте зал снова.")
        return
    if CASINO_TRAIN_LOTTERY_RECEIPT_KEY not in context.user_data:
        await update.message.reply_text("Сначала пришлите чек.", reply_markup=get_payment_confirmation_keyboard())
        return
    ticket = (
        CASINO_TRAIN_OFFLINE_TICKET
        if kind == "off"
        else CASINO_TRAIN_ONLINE_TICKET
        if kind == "on"
        else CASINO_TRAIN_AI_TICKET
    )
    sk = CASINO_TRAIN_OFF_SPINS_KEY if kind == "off" else CASINO_TRAIN_ON_SPINS_KEY if kind == "on" else CASINO_TRAIN_AI_SPINS_KEY
    spins = int(context.user_data.get(sk) or 0) + 1
    context.user_data[sk] = spins
    user = update.effective_user
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    note = f"Казино обучение {kind}: оплата билета {ticket}₽ | {lab} id:{user.id} | спинов={spins}"
    await _casino_append_note_column_e(note)
    rid = context.user_data[CASINO_TRAIN_LOTTERY_RECEIPT_KEY]
    context.user_data.pop(CASINO_TRAIN_LOTTERY_RECEIPT_KEY, None)
    context.user_data.pop(CASINO_TRAIN_PAY_KIND_KEY, None)
    context.user_data["state"] = "casino_submenu"
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ **Билет лотереи обучения ({kind})** {user.id}\n{lab}\nСпинов: **{spins}**",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, rid)
    except Exception as e:
        logger.error("finalize_casino_training_ticket_payment admin: %s", e)
    await update.message.reply_text(
        f"Билет **зачислен** ✨ Спинов по этой линии: **{spins}**. Крутите кнопкой в зале.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await _send_casino_lobby_message(update.message, context)


# handle_casino_train_spin_callback удалён — заменён универсальным provably-fair движком.


async def _casino_rent_stake_precheck(query, year: int, month: int, row: int):
    """Проверка смены перед ставкой в casino. Успех: (worksheet, ws_title, user); иначе текст ошибки для пользователя."""
    ws_title = _casino_worksheet_title(year, month)
    user = query.from_user
    worksheet = await get_worksheet_cached(ws_title)
    if not worksheet:
        return "Лист недоступен."
    if not await _casino_row_owned_by(worksheet, row, user.id):
        return "Это не ваша строка."
    row_vals = await asyncio.to_thread(worksheet.row_values, row)
    status = row_vals[3] if len(row_vals) > 3 else ""
    pay = str(row_vals[2] if len(row_vals) > 2 else "").lower()
    if status != "активна" or "оплач" not in pay:
        return "Ставка недоступна (не оплачено или уже не активна)."
    return worksheet, ws_title, user


# =============================================================================
# --- CASINO: provably-fair движок раунда (100 ячеек, commit-reveal SHA-256) ---
# =============================================================================


def _fair_M(kind: str, user_id: int) -> int:
    cr = user_id in CASINO_CREATOR_TELEGRAM_IDS
    pair = CASINO_FAIR_M.get(kind)
    if not pair:
        return 0
    d, c = pair
    return c if cr else d


def _fair_label(kind: str) -> str:
    return CASINO_FAIR_LABELS.get(kind, kind)


def _fair_chip_key(kind: str) -> str | None:
    return {
        "tattoo": CASINO_TATTOO_LOTTERY_SPINS_KEY,
        "off": CASINO_TRAIN_OFF_SPINS_KEY,
        "on": CASINO_TRAIN_ON_SPINS_KEY,
        "ai": CASINO_TRAIN_AI_SPINS_KEY,
    }.get(kind)


def _fair_take_chip(context: ContextTypes.DEFAULT_TYPE, kind: str) -> bool:
    k = _fair_chip_key(kind)
    if not k:
        return False
    n = int(context.user_data.get(k) or 0)
    if n < 1:
        return False
    context.user_data[k] = n - 1
    return True


def _fair_refund_chip(context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    k = _fair_chip_key(kind)
    if not k:
        return
    context.user_data[k] = int(context.user_data.get(k) or 0) + 1


def _fair_commit_hash(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode()).hexdigest()


def _fair_winning_slots(server_seed: str, round_id: str, k: int) -> list[int]:
    """Детерминированно вычисляет K уникальных номеров ячеек (1..100) из SHA-256."""
    seen: set[int] = set()
    out: list[int] = []
    counter = 0
    while len(out) < k and counter < 4096:
        h = hashlib.sha256(f"{server_seed}|{round_id}|{counter}".encode()).hexdigest()
        for i in range(0, 64, 8):
            n = int(h[i:i + 8], 16) % CASINO_FAIR_TOTAL_SLOTS + 1
            if n not in seen:
                seen.add(n)
                out.append(n)
                if len(out) == k:
                    break
        counter += 1
    return out


def _fair_new_round_state(kind: str, user_id: int, rent_meta: dict | None = None) -> dict:
    seed = secrets.token_hex(32)
    rid = secrets.token_hex(6)
    return {
        "id": rid,
        "kind": kind,
        "M": _fair_M(kind, user_id),
        "K": CASINO_FAIR_WINNERS_COUNT,
        "server_seed": seed,
        "commit": _fair_commit_hash(seed),
        "label": _fair_label(kind),
        "msg_chat_id": None,
        "msg_id": None,
        "ctrl_chat_id": None,
        "ctrl_msg_id": None,
        "picks": [],
        "winners": None,
        "won": None,
        "rent_meta": rent_meta,
        "stage": "pick",
        "created_ts": time.time(),
    }


def _fair_round_expired_or_clear(context: ContextTypes.DEFAULT_TYPE) -> bool:
    rs = context.user_data.get(CASINO_ROUND_KEY)
    if not rs:
        return False
    if time.time() - float(rs.get("created_ts") or 0) > CASINO_ROUND_TIMEOUT_SEC:
        if rs.get("kind") in ("tattoo", "off", "on", "ai"):
            _fair_refund_chip(context, rs["kind"])
        context.user_data.pop(CASINO_ROUND_KEY, None)
        return True
    return False


def _fair_grid_markup_pick(rs: dict) -> InlineKeyboardMarkup:
    """100 ячеек в раскладке 8 кол. × 13 строк (последняя — 4 кнопки). Picks помечаются ✅."""
    picks = set(rs.get("picks") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in range(1, CASINO_FAIR_TOTAL_SLOTS + 1):
        label = f"✅{n}" if n in picks else str(n)
        row.append(InlineKeyboardButton(label, callback_data=f"csp_{n}"))
        if len(row) == CASINO_FAIR_GRID_COLS:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _fair_grid_markup_reveal(rs: dict) -> InlineKeyboardMarkup:
    """Reveal-сетка: ✅ = ваш выигрышный, 🏆 = победная (не ваша), 🎯 = ваш промах."""
    picks = set(rs.get("picks") or [])
    winners = set(rs.get("winners") or [])
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in range(1, CASINO_FAIR_TOTAL_SLOTS + 1):
        if n in winners and n in picks:
            label = f"✅{n}"
        elif n in winners:
            label = f"🏆{n}"
        elif n in picks:
            label = f"🎯{n}"
        else:
            label = str(n)
        row.append(InlineKeyboardButton(label, callback_data="csnoop"))
        if len(row) == CASINO_FAIR_GRID_COLS:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _fair_controls_pick_markup(rs: dict) -> InlineKeyboardMarkup:
    M = int(rs.get("M") or 0)
    picks = rs.get("picks") or []
    picked = len(picks)
    rows: list[list[InlineKeyboardButton]] = []
    if picked < M:
        remaining = M - picked
        rows.append([InlineKeyboardButton(f"🎲 Случайно (+{remaining} до {M})", callback_data="csrand")])
    if picked > 0:
        rows.append([InlineKeyboardButton("🔄 Сбросить выбор", callback_data="cscng")])
    if picked == M and M > 0:
        rows.append([InlineKeyboardButton(f"🎰 Крутить ({M}/{M})", callback_data="csgo")])
    rows.append([InlineKeyboardButton("ℹ️ Как это работает", callback_data="csfair")])
    rows.append([InlineKeyboardButton("← Отмена раунда", callback_data="cscan")])
    return InlineKeyboardMarkup(rows)


def _fair_hit_picks(rs: dict) -> list[int]:
    picks = set(rs.get("picks") or [])
    return sorted(n for n in (rs.get("winners") or []) if n in picks)


def _fair_after_rent_win_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Записать смену сейчас", callback_data="cswnow")],
            [InlineKeyboardButton("🎁 Активировать позже", callback_data="cswlater")],
            [InlineKeyboardButton("🏠 В зал IKONA", callback_data="ch_hub")],
        ]
    )


def _fair_reveal_actions_markup(rs: dict) -> InlineKeyboardMarkup | None:
    kind = rs.get("kind")
    if kind == "rent" and rs.get("won"):
        return _fair_after_rent_win_markup()
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В зал IKONA", callback_data="ch_hub")]])


def _fair_text_reveal_grid(rs: dict) -> str:
    won = bool(rs.get("won"))
    winners = sorted(rs.get("winners") or [])
    picks_sorted = sorted(rs.get("picks") or [])
    hits = _fair_hit_picks(rs)
    if won:
        line = (
            f"🎉 **ПОБЕДА** — победная ячейка **{winners[0]}** среди ваших фишек "
            f"({len(hits)} совпадение)."
        )
    else:
        line = (
            f"💫 **ПРОИГРЫШ** — победная ячейка **{winners[0]}** не попала в ваши фишки."
        )
    picks_str = ", ".join(str(x) for x in picks_sorted[:25])
    if len(picks_sorted) > 25:
        picks_str += f", …+{len(picks_sorted) - 25}"
    winners_str = ", ".join(str(x) for x in winners)
    return (
        f"🎰 **РАУНД #{rs['id']}** — «{rs['label']}»\n\n"
        f"{line}\n\n"
        f"🏆 Победная ячейка: **{winners_str}**\n"
        f"🎯 Ваши фишки ({len(picks_sorted)}): {picks_str}"
    )


def _fair_text_reveal_controls(rs: dict) -> str:
    won = bool(rs.get("won"))
    winners = sorted(rs.get("winners") or [])
    picks_sorted = sorted(rs.get("picks") or [])
    hits = _fair_hit_picks(rs)
    winners_str = ", ".join(str(x) for x in winners)
    if won:
        outcome = (
            f"🎉 **ИТОГ: ПОБЕДА**\n"
            f"Победная ячейка **{winners_str}** — среди ваших фишек "
            f"({len(hits)} совпадение из {len(picks_sorted)})."
        )
        if rs.get("kind") == "rent":
            outcome += (
                f"\n\n🎁 **+1 бесплатная смена** зачислена на счёт казино"
                f" (всего: **{int(rs.get('credits_after') or 0)}**). "
                "Запишите её **сейчас** или **позже** — кнопки ниже."
            )
    else:
        outcome = (
            f"💫 **ИТОГ: ПРОИГРЫШ**\n"
            f"Победная ячейка **{winners_str}** не попала в ваши фишки "
            f"({len(picks_sorted)} фишек)."
        )
        if rs.get("kind") == "rent":
            outcome += "\n\n⚠️ Ставка-смена **снята** с расписания (без возврата)."
    return (
        f"{outcome}\n\n"
        f"🆔 ID раунда: `{rs['id']}`\n"
        f"🔓 Server seed: `{rs['server_seed']}`\n\n"
        f"🧪 **Проверка честности** (SHA-256, например {CASINO_FAIR_VERIFY_URL}):\n"
        f"1) `SHA-256(server seed)` = commit `{rs['commit']}`.\n"
        f"2) Победная ячейка — первое уникальное `int(hex, 16) % 100 + 1` из SHA-256 "
        f"строки `{rs['server_seed']}|{rs['id']}|0`.\n\n"
        f"💎 Commit опубликован **до** ваших фишек — результат раунда зафиксирован заранее."
    )


def _fair_text_grid_static(rs: dict) -> str:
    return (
        f"🎰 **РАУНД #{rs['id']}** — лотерея «{rs['label']}»\n\n"
        f"Размещайте фишки кликом по ячейкам ниже. Повторный клик — снять фишку.\n"
        f"Победная ячейка спрятана среди 100 — определяется **до** ваших фишек, "
        f"и зафиксирована в **commit (SHA-256)** ниже."
    )


def _fair_text_controls(rs: dict) -> str:
    M = int(rs.get("M") or 0)
    picks = rs.get("picks") or []
    picked = len(picks)
    extra = ""
    if rs["kind"] == "rent" and rs.get("rent_meta"):
        rm = rs["rent_meta"]
        extra = (
            f"\n\n⚠️ Стейк: оплаченная смена `{rm['ws_title']}` строка **{rm['row']}**.\n"
            "Победа → **+1 бесплатная смена** (можно сразу выбрать дату).\n"
            "Поражение → бронь **снимается** с расписания (без возврата)."
        )
    if picked < M:
        progress = (
            f"📌 Размещено фишек: **{picked} / {M}**.\n"
            f"Кликайте номера в верхнем сообщении — или жмите **«🎲 Случайно»**."
        )
    else:
        progress = f"✅ **Все {M} фишек размещены.** Жмите **«🎰 Крутить»**, чтобы вскрыть результат."
    return (
        f"🔒 **Commit** (SHA-256 от секретного сервера):\n`{rs['commit']}`\n"
        f"🆔 ID раунда: `{rs['id']}`\n"
        f"Сохраните это сейчас — после крутки мы раскроем server seed, и любой проверит честность.\n\n"
        f"🎯 У вас **{M}** фишек на 100 ячеек → шанс победы около {M}%.\n\n"
        f"{progress}"
        f"{extra}"
    )


CASINO_FAIR_INFO_TEXT = (
    "🛡 **Provably Fair · честное казино IKONA**\n\n"
    "Перед каждым раундом мы делаем три вещи:\n\n"
    "**1.** Генерируем секретный `server seed` (случайная 256-битная строка).\n"
    "**2.** Считаем `commit = SHA-256(server seed)` и публикуем его **до** ваших фишек.\n"
    "**3.** Победная ячейка вычисляется детерминированно из server seed и уникального ID раунда. "
    "Эта связка фиксируется ещё до того, как вы что-либо выбрали — мы физически не можем подменить результат.\n\n"
    "После того как вы разместили фишки и нажали **«Крутить»**, бот **раскрывает** `server seed`. "
    "Любой может:\n"
    "• посчитать `SHA-256(server seed)` — он совпадёт с опубликованным commit;\n"
    "• посчитать `SHA-256(server seed | ID раунда | 0)` — первые 8 hex-символов, "
    "`int(hex, 16) % 100 + 1` — это и есть победная ячейка раунда.\n\n"
    f"Удобный онлайн-калькулятор: {CASINO_FAIR_VERIFY_URL}\n\n"
    "**Почему это надёжно:**\n"
    "• commit — это **необратимое** обязательство: по хэшу нельзя восстановить seed, "
    "но по seed можно проверить, что хэш правильный.\n"
    "• ID раунда уникален для каждого раунда — мы не можем «переиграть» один и тот же seed.\n"
    "• Все ваши данные (фишки, ID раунда, commit, seed) показываются на экране — "
    "сохраните скриншоты, если хочется доказательной базы.\n\n"
    "Если что-то покажется странным — напишите нам ID раунда и server seed, "
    f"и мы вместе разберём расчёт: {PAYMENT_CONTACT}"
)


async def _safe_send_md(query, text: str, reply_markup=None, *, use_markdown: bool = True, **kwargs):
    """reply_text с опциональным MARKDOWN и откатом к plain-text при BadRequest."""
    if not use_markdown:
        return await query.message.reply_text(text, reply_markup=reply_markup, **kwargs)
    try:
        return await query.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            **kwargs,
        )
    except BadRequest as e:
        logger.warning("send_md fallback: %s", e)
        return await query.message.reply_text(
            text,
            reply_markup=reply_markup,
            **kwargs,
        )
    except (TimedOut, NetworkError) as e:
        logger.warning("send_md timeout/network: %s", e)
        try:
            return await query.message.reply_text(
                text,
                reply_markup=reply_markup,
                **kwargs,
            )
        except (TimedOut, NetworkError) as e2:
            logger.warning("send_md plain retry failed: %s", e2)
            return None


async def _safe_edit_md(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, *,
                        text: str | None = None, reply_markup=None,
                        only_markup: bool = False, use_markdown: bool = True) -> None:
    """edit_message_text/reply_markup с откатом к plain-text при BadRequest."""
    try:
        if only_markup:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=msg_id, reply_markup=reply_markup,
            )
            return
        if use_markdown:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text or "",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text or "",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
    except (TimedOut, NetworkError) as e:
        logger.warning("edit_md timeout/network: %s", e)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return
        logger.warning("edit_md fallback: %s", e)
        try:
            if only_markup:
                return
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text or "",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except (BadRequest, TimedOut, NetworkError) as e2:
            if "message is not modified" not in str(e2).lower():
                logger.warning("edit_md plain failed: %s", e2)


async def _fair_send_round_message(query, context: ContextTypes.DEFAULT_TYPE, rs: dict) -> None:
    grid_msg = await _safe_send_md(
        query,
        _fair_text_grid_static(rs),
        reply_markup=_fair_grid_markup_pick(rs),
        disable_web_page_preview=True,
    )
    rs["msg_chat_id"] = grid_msg.chat_id
    rs["msg_id"] = grid_msg.message_id
    ctrl_msg = await _safe_send_md(
        query,
        _fair_text_controls(rs),
        reply_markup=_fair_controls_pick_markup(rs),
        disable_web_page_preview=True,
    )
    rs["ctrl_chat_id"] = ctrl_msg.chat_id
    rs["ctrl_msg_id"] = ctrl_msg.message_id
    context.user_data[CASINO_ROUND_KEY] = rs


async def _fair_refresh_pick_ui(context: ContextTypes.DEFAULT_TYPE, rs: dict) -> None:
    if rs.get("msg_chat_id") and rs.get("msg_id"):
        await _safe_edit_md(
            context, rs["msg_chat_id"], rs["msg_id"],
            reply_markup=_fair_grid_markup_pick(rs),
            only_markup=True,
        )
    if rs.get("ctrl_chat_id") and rs.get("ctrl_msg_id"):
        await _safe_edit_md(
            context, rs["ctrl_chat_id"], rs["ctrl_msg_id"],
            text=_fair_text_controls(rs),
            reply_markup=_fair_controls_pick_markup(rs),
        )


async def _fair_start_chip_round(query, context: ContextTypes.DEFAULT_TYPE, kind: str, user):
    _fair_round_expired_or_clear(context)
    if context.user_data.get(CASINO_ROUND_KEY):
        await query.message.reply_text(
            "Уже идёт раунд — завершите его или нажмите **«← Отмена раунда»** в нём.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not _fair_take_chip(context, kind):
        await query.message.reply_text(
            f"Нет фишек по линии **«{_fair_label(kind)}»**. Купите билет в зале.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        return
    rs = _fair_new_round_state(kind, user.id)
    await _fair_send_round_message(query, context, rs)


async def _fair_start_rent_round(query, context: ContextTypes.DEFAULT_TYPE, user, Y: int, M: int, R: int):
    _fair_round_expired_or_clear(context)
    if context.user_data.get(CASINO_ROUND_KEY):
        await query.message.reply_text(
            "Уже идёт раунд — завершите его сначала.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pack = await _casino_rent_stake_precheck(query, Y, M, R)
    if isinstance(pack, str):
        await query.message.reply_text(pack)
        return
    worksheet, ws_title, _user = pack
    rs = _fair_new_round_state(
        "rent",
        user.id,
        rent_meta={"ws_title": ws_title, "row": R, "year": Y, "month": M},
    )
    await _fair_send_round_message(query, context, rs)


async def _fair_cancel_round(query, context: ContextTypes.DEFAULT_TYPE, rs: dict):
    kind = rs.get("kind")
    if kind in ("tattoo", "off", "on", "ai"):
        _fair_refund_chip(context, kind)
    context.user_data.pop(CASINO_ROUND_KEY, None)
    cancel_text = (
        f"🛑 **Раунд #{rs['id']} отменён.**\n\n"
        f"Commit (так и не вскрыт): `{rs['commit']}`\n"
        "Фишка/смена сохранена — можно попробовать снова, когда захочется."
    )
    if rs.get("msg_chat_id") and rs.get("msg_id"):
        await _safe_edit_md(
            context, rs["msg_chat_id"], rs["msg_id"],
            text=cancel_text,
            reply_markup=None,
        )
    if rs.get("ctrl_chat_id") and rs.get("ctrl_msg_id"):
        try:
            await context.bot.delete_message(
                chat_id=rs["ctrl_chat_id"], message_id=rs["ctrl_msg_id"],
            )
        except BadRequest:
            pass
    await query.message.reply_text(
        "Возвращаю вас в зал ✨",
        reply_markup=_casino_hub_markup(context),
    )


async def _fair_spin_animation(context: ContextTypes.DEFAULT_TYPE, rs: dict, frames: int = 2) -> None:
    M = int(rs.get("M") or 0)
    if rs.get("ctrl_chat_id") and rs.get("ctrl_msg_id"):
        for i in range(frames):
            await _safe_edit_md(
                context, rs["ctrl_chat_id"], rs["ctrl_msg_id"],
                text=(
                    f"🎰 Кручу барабан… ({i + 1}/{frames})\n\n"
                    f"🆔 ID раунда: {rs['id']}\n"
                    f"🔒 Commit: {rs['commit']}\n"
                    f"🎯 Ваших фишек: {M}/{M}"
                ),
                reply_markup=None,
                use_markdown=False,
            )
            await asyncio.sleep(0.35)


async def _fair_resolve_rent(query, context: ContextTypes.DEFAULT_TYPE, rs: dict, user):
    won = bool(rs.get("won"))
    winners = list(rs.get("winners") or [])
    picks = list(rs.get("picks") or [])
    rm = rs.get("rent_meta") or {}
    ws_title = rm.get("ws_title") or ""
    row = int(rm.get("row") or 0)
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    worksheet = await get_worksheet_cached(ws_title) if ws_title else None

    if won:
        new_c = int(rs.get("credits_after") or context.user_data.get("casino_free_rent_credits") or 0)
        if new_c < 1:
            new_c = int(context.user_data.get("casino_free_rent_credits") or 0) + 1
            context.user_data["casino_free_rent_credits"] = new_c
        try:
            await _casino_log_win_in_column_e(context, user, new_c)
        except Exception as e:
            logger.error("rent win log: %s", e)
        try:
            await _casino_win_feed_record("rent_shift", user)
        except Exception as e:
            logger.error("rent win feed: %s", e)
        context.user_data.pop(CASINO_ROUND_KEY, None)
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"🎰 **Casino RENT WIN** {user.id} {lab}\n"
                f"Round #{rs['id']} picks={sorted(picks)} winners={sorted(winners)}\n"
                f"`{ws_title}` строка **{row}** (бронь сохранена) · кредитов: **{new_c}**",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("rent win admin: %s", e)
        return

    if not worksheet:
        await query.message.reply_text(
            "Не удалось снять бронь автоматически — напишите администратору: " + PAYMENT_CONTACT,
            reply_markup=_casino_hub_markup(context),
        )
        context.user_data.pop(CASINO_ROUND_KEY, None)
        return
    try:
        await _casino_cancel_row_casino_loss(worksheet, row, user)
        for ck in list(sheets_cache.keys()):
            if ck.startswith(ws_title):
                del sheets_cache[ck]
    except Exception as e:
        logger.error("rent loss cancel: %s", e)
        await query.message.reply_text(
            "Произошла техническая заминка при снятии брони. Напишите: " + PAYMENT_CONTACT,
            reply_markup=_casino_hub_markup(context),
        )
        context.user_data.pop(CASINO_ROUND_KEY, None)
        return
    context.user_data.pop(CASINO_ROUND_KEY, None)
    await query.message.reply_text(
        "Если захотите попробовать снова — я рядом. Или напишите: " + PAYMENT_CONTACT,
        reply_markup=_casino_retention_markup(),
    )
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🎰 **Casino RENT LOSS** {user.id} {lab}\n"
            f"Round #{rs['id']} picks={sorted(picks)} winners={sorted(winners)}\n"
            f"`{ws_title}` строка **{row}** отменена",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("rent loss admin: %s", e)


async def _fair_resolve_prize_lottery(query, context: ContextTypes.DEFAULT_TYPE, rs: dict, user):
    won = bool(rs.get("won"))
    winners = list(rs.get("winners") or [])
    picks = list(rs.get("picks") or [])
    kind = rs["kind"]
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    if won:
        if kind == "tattoo":
            vk = CASINO_TATTOO_VOUCHERS_KEY
            prize_face = TATTOO_SESSION_PRICE
            feedk = "tattoo_vip"
            redeem_hint = f"Используйте через **«записаться на тату»** + **«{BTN_TATTOO_CASINO_VOUCHER}»**."
        else:
            ks = {
                "off": (CASINO_VOUCHER_TRAIN_OFF_KEY, TRAINING_OFFLINE_PRICE, "train_off", BTN_TRAIN_OFF_CASINO_VOUCHER),
                "on": (CASINO_VOUCHER_TRAIN_ON_KEY, TRAINING_ONLINE_PRICE, "train_on", BTN_TRAIN_ON_CASINO_VOUCHER),
                "ai": (CASINO_VOUCHER_TRAIN_AI_KEY, TRAINING_AI_PROGRAM_PRICE, "train_ai", BTN_TRAIN_AI_CASINO_VOUCHER),
            }
            vk, prize_face, feedk, btn = ks[kind]
            redeem_hint = f"Используйте кнопкой **«{btn}»** в разделе обучения."
        v = int(context.user_data.get(vk) or 0) + 1
        context.user_data[vk] = v
        try:
            await _casino_append_note_column_e(
                f"Казино {kind} ВЫИГРЫШ: приз {prize_face}₽ | {lab} id:{user.id} | round {rs['id']}"
            )
        except Exception as e:
            logger.error("prize win e: %s", e)
        try:
            await _casino_win_feed_record(feedk, user)
        except Exception as e:
            logger.error("prize win feed: %s", e)
        await query.message.reply_text(
            f"✨ Вы выиграли приз **{prize_face:,} ₽** ({_fair_label(kind)}). "
            f"Сертификатов у вас: **{v}**.\n\n{redeem_hint}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"🎰 **Casino WIN ({kind})** {user.id} {lab}\nRound #{rs['id']} picks={sorted(picks)} winners={sorted(winners)} · "
                f"приз {prize_face}₽ · сертификатов: **{v}**",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("prize win admin: %s", e)
    else:
        try:
            await _casino_append_note_column_e(
                f"Казино {kind} проигрыш | {lab} id:{user.id} | round {rs['id']}"
            )
        except Exception as e:
            logger.error("prize loss e: %s", e)
        await query.message.reply_text(
            "💫 В этот раз барабан не улыбнулся. Билеты ждут в зале — пробуйте ещё.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"🎰 **Casino LOSS ({kind})** {user.id} {lab}\nRound #{rs['id']} picks={sorted(picks)} winners={sorted(winners)}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("prize loss admin: %s", e)
    context.user_data.pop(CASINO_ROUND_KEY, None)


async def _fair_spin_round(query, context: ContextTypes.DEFAULT_TYPE, rs: dict, user):
    picks = list(rs.get("picks") or [])
    winners = _fair_winning_slots(rs["server_seed"], rs["id"], rs["K"])
    picks_set = set(picks)
    won = any(w in picks_set for w in winners)
    rs["stage"] = "spun"
    rs["winners"] = winners
    rs["won"] = won
    context.user_data[CASINO_ROUND_KEY] = rs

    try:
        await _fair_spin_animation(context, rs)
    except (TimedOut, NetworkError) as e:
        logger.warning("spin animation skipped: %s", e)

    if rs["kind"] == "rent" and won:
        new_c = int(context.user_data.get("casino_free_rent_credits") or 0) + 1
        context.user_data["casino_free_rent_credits"] = new_c
        rs["credits_after"] = new_c
        context.user_data[CASINO_ROUND_KEY] = rs

    # Reveal grid: edit grid msg → keyboard with markings + result text
    if rs.get("msg_chat_id") and rs.get("msg_id"):
        await _safe_edit_md(
            context, rs["msg_chat_id"], rs["msg_id"],
            text=_fair_text_reveal_grid(rs),
            reply_markup=_fair_grid_markup_reveal(rs),
        )
    # Reveal controls msg: full proof + round_id (without action buttons here)
    if rs.get("ctrl_chat_id") and rs.get("ctrl_msg_id"):
        await _safe_edit_md(
            context, rs["ctrl_chat_id"], rs["ctrl_msg_id"],
            text=_fair_text_reveal_controls(rs),
            reply_markup=_fair_reveal_actions_markup(rs),
        )

    context.user_data["state"] = "casino_submenu"
    if rs["kind"] == "rent":
        await _fair_resolve_rent(query, context, rs, user)
    else:
        await _fair_resolve_prize_lottery(query, context, rs, user)


async def _start_rent_booking_from_casino_query(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = "rent_booking_menu"
    context.user_data.pop("rent_use_free_credit", None)
    context.user_data.pop("rent_flow", None)
    context.user_data.pop("reschedule_old", None)
    context.user_data.pop("booking_flow", None)
    today = datetime.date.today()
    context.user_data["calendar_year"] = today.year
    context.user_data["calendar_month"] = today.month
    await query.message.reply_text(
        "📅 **Запись на аренду** — выберите **дату** и **время** ниже.\n\n"
        "После подтверждения оплаты смена появится в **«Удача на смену»**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await query.message.reply_text(
        _calendar_title(context),
        reply_markup=generate_calendar_keyboard(today.year, today.month),
    )


async def _start_casino_rent_bonus_booking(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    bonus = int(context.user_data.get("casino_free_rent_credits") or 0)
    if bonus < 1:
        await query.message.reply_text(
            "Сейчас нет неиспользованных бесплатных смен. "
            "Выиграйте их в **«Удача на смену»** или напишите: " + PAYMENT_CONTACT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_casino_hub_markup(context),
        )
        return
    context.user_data["rent_use_free_credit"] = True
    context.user_data.pop("booking_flow", None)
    context.user_data.pop("rent_flow", None)
    context.user_data.pop("reschedule_old", None)
    today = datetime.date.today()
    context.user_data["calendar_year"] = today.year
    context.user_data["calendar_month"] = today.month
    await query.message.reply_text(
        f"📅 **Бесплатная смена из казино** (на счёте: **{bonus}**)\n\n"
        "Выберите **дату** и **время** ниже. На шаге расходников бонус спишется автоматически.\n\n"
        "Перенести запись позже: **аренда → ваши записи → перенос**.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.message.reply_text(
        _calendar_title(context),
        reply_markup=generate_calendar_keyboard(today.year, today.month),
    )


async def _fair_after_rent_win_now(query, context: ContextTypes.DEFAULT_TYPE):
    await _start_casino_rent_bonus_booking(query, context)


async def _fair_after_rent_win_later(query, context: ContextTypes.DEFAULT_TYPE):
    bonus = int(context.user_data.get("casino_free_rent_credits") or 0)
    await query.message.reply_text(
        f"🎁 Бонус **сохранён** на счёте казино: **{bonus}** бесплатных смен.\n\n"
        "Активировать и записаться можно **в любой момент** — кнопка "
        "**«🎁 Мои выигранные смены»** в зале или **аренда → «"
        f"{BTN_RENT_CASINO_BONUS}»**.\n\n"
        "Перенести запись: **аренда → ваши записи → перенос**.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_casino_hub_markup(context),
    )


async def handle_casino_round_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await _safe_callback_answer(query)

    # Старт: фишка-раунды
    if data == "crs_t":
        await _fair_start_chip_round(query, context, "tattoo", user)
        return
    if data == "crs_o":
        await _fair_start_chip_round(query, context, "off", user)
        return
    if data == "crs_n":
        await _fair_start_chip_round(query, context, "on", user)
        return
    if data == "crs_a":
        await _fair_start_chip_round(query, context, "ai", user)
        return
    # Старт: ставка-смена аренды
    if data.startswith("crs_r_"):
        rest = data[len("crs_r_"):]
        try:
            Y, M, R = (int(x) for x in rest.split("_"))
        except ValueError:
            await query.message.reply_text("Ошибка данных.")
            return
        await _fair_start_rent_round(query, context, user, Y, M, R)
        return

    # Общие команды (работают и в лобби, и в раунде)
    if data == "csnoop":
        return
    if data == "csfair":
        await query.message.reply_text(
            CASINO_FAIR_INFO_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return
    if data == "cswnow":
        await _fair_after_rent_win_now(query, context)
        return
    if data == "cswlater":
        await _fair_after_rent_win_later(query, context)
        return

    # Любая интерактивная команда раунда требует активный раунд
    if _fair_round_expired_or_clear(context):
        await query.message.reply_text(
            "Раунд истёк по таймауту и был сброшен — фишка вернулась на счёт.",
            reply_markup=_casino_hub_markup(context),
        )
        return
    rs = context.user_data.get(CASINO_ROUND_KEY)
    if not rs:
        await query.message.reply_text(
            "Активный раунд не найден — откройте зал заново.",
            reply_markup=_casino_hub_markup(context),
        )
        return

    if data.startswith("csp_"):
        if rs.get("stage") != "pick":
            return
        try:
            n = int(data[4:])
        except ValueError:
            return
        if not (1 <= n <= CASINO_FAIR_TOTAL_SLOTS):
            return
        picks = list(rs.get("picks") or [])
        M = int(rs.get("M") or 0)
        if n in picks:
            picks.remove(n)
        elif len(picks) >= M:
            await query.message.reply_text(
                f"Уже размещены все {M} фишек — снимите одну, если хотите поменять.",
            )
            return
        else:
            picks.append(n)
        rs["picks"] = picks
        context.user_data[CASINO_ROUND_KEY] = rs
        await _fair_refresh_pick_ui(context, rs)
        return

    if data == "csrand":
        if rs.get("stage") != "pick":
            return
        picks = list(rs.get("picks") or [])
        M = int(rs.get("M") or 0)
        if len(picks) >= M:
            return
        remaining_pool = [k for k in range(1, CASINO_FAIR_TOTAL_SLOTS + 1) if k not in picks]
        random.shuffle(remaining_pool)
        need = M - len(picks)
        picks.extend(remaining_pool[:need])
        rs["picks"] = picks
        context.user_data[CASINO_ROUND_KEY] = rs
        await _fair_refresh_pick_ui(context, rs)
        return

    if data == "cscng":
        rs["picks"] = []
        rs["stage"] = "pick"
        context.user_data[CASINO_ROUND_KEY] = rs
        await _fair_refresh_pick_ui(context, rs)
        return

    if data == "cscan":
        await _fair_cancel_round(query, context, rs)
        return

    if data == "csgo":
        M = int(rs.get("M") or 0)
        picks = list(rs.get("picks") or [])
        if rs.get("stage") != "pick" or len(picks) != M or M <= 0:
            await query.message.reply_text(
                f"Сначала разместите все {M} фишек."
            )
            return
        rs["stage"] = "spinning"
        context.user_data[CASINO_ROUND_KEY] = rs
        await _fair_spin_round(query, context, rs, user)
        return


async def route_casino_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "casino_submenu"
    await update.message.reply_text(
        "Сообщениями зал не открывается — только аккуратные кнопки под последним постом зала 💎\n\n"
        "Вот свежая витрина:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(),
    )
    await _send_casino_lobby_message(update.message, context)


# =================================================================================
# --- TRAINING MODULE ---
# =================================================================================

async def route_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state in ("training_offline_calendar", "training_online_calendar"):
        await update.message.reply_text(
            "Используйте **календарь** в сообщении выше (даты и время). "
            "Или нажмите **«назад»**, чтобы вернуться без записи.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if state in ("training_offline_payment", "training_online_payment"):
        await route_training_booking_payment(update, context)
        return
    if state == "training_submenu":
        await handle_training_choice(update, context)
    elif state == "training_offline_details":
        await handle_training_offline_details(update, context)
    elif state == "training_online_details":
        await handle_training_online_details(update, context)
    elif state == "training_ai_details":
        await handle_training_ai_details(update, context)


async def _start_training_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, booking_flow: str, calendar_state: str):
    context.user_data["booking_flow"] = booking_flow
    context.user_data["state"] = calendar_state
    hint = (
        "дату **первого дня** обучения"
        if booking_flow == "training_offline"
        else "желаемую **дату старта** онлайн-курса"
    )
    await update.message.reply_text(
        f"Выберите {hint} в календаре ниже, затем **время начала** (как при аренде).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await show_calendar(update, context)


async def handle_training_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_TRAIN_OFFLINE:
        context.user_data["state"] = "training_offline_details"
        await context.bot.send_message(update.effective_chat.id, f"🎬 Оффлайн-обучение: {OFFLINE_TRAINING_VIDEO}")
        description = (
            "**Оффлайн-обучение IKONA**\n\n"
            "💰 **Стоимость курса: 140 000 ₽**\n\n"
            "Вы получаете **тату-машинку** и **ИИ-программу** для автосоздания контента для продвижения.\n\n"
            "**«записаться»** — один день и время первого занятия, затем предоплата **2 000 ₽** на первый день. "
            "Остальное — **наличными** после старта.\n\n"
            "**«подробнее»** — расширенное описание формата."
        )
        await update.message.reply_text(description, parse_mode=ParseMode.MARKDOWN, reply_markup=get_offline_training_keyboard())
    elif text == BTN_TRAIN_ONLINE:
        context.user_data["state"] = "training_online_details"
        await context.bot.send_message(update.effective_chat.id, f"🎬 Онлайн-обучение: {ONLINE_TRAINING_VIDEO}")
        description = (
            "**Онлайн-обучение IKONA**\n\n"
            "Из **любой точки мира**, где есть интернет: **онлайн-видео**, занятия **онлайн с учителем**, "
            "**ИИ-программа** с автогенерацией контента, **тату-машинка** и **тату-салон IKONA в вашем городе** для практики и работы.\n\n"
            "💰 **99 000 ₽** · срок **1–2 месяца**.\n\n"
            "**«предоплата 2 000 ₽»** — зафиксировать место и перейти к оплате предоплаты.\n"
            "**«дополнительно об онлайн»** — подробности программы."
        )
        await update.message.reply_text(description, parse_mode=ParseMode.MARKDOWN, reply_markup=get_online_training_keyboard())
    elif text == BTN_TRAIN_AI:
        context.user_data["state"] = "training_ai_details"
        await context.bot.send_message(update.effective_chat.id, f"🎬 Программа ИИ: {IKONA_AI_VIDEO}")
        await update.message.reply_text(
            "**ИИ-программа для автосоздания контента**\n\n"
            "Инструмент сильно **прокачивает** любого пользователя: экономит время на соцсетях, "
            "помогает **монетизировать** навык и стабильно выкладывать материалы для продвижения.\n\n"
            "Полная версия программы — **15 000 ₽** (ниже кнопка покупки).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_training_ai_keyboard(context),
        )


async def handle_training_offline_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_TRAIN_SIGNUP:
        await _start_training_calendar(update, context, "training_offline", "training_offline_calendar")
    elif text == BTN_TRAIN_MORE:
        await update.message.reply_text(
            "**Подробнее: оффлайн**\n\n"
            "Обучение проходит в сети салонов IKONA. В курс входит оборудование и софт для контента — "
            "после предоплаты на первое занятие с вами свяжутся для согласования графика и доплаты **наличными**.\n\n"
            f"Контакт: {PAYMENT_CONTACT}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_offline_training_keyboard(),
        )


async def handle_training_online_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_TRAIN_ONLINE_PREPAY:
        await _start_training_calendar(update, context, "training_online", "training_online_calendar")
    elif text == BTN_TRAIN_ONLINE_INFO:
        await update.message.reply_text(
            "**Онлайн-курс IKONA — дополнительно**\n\n"
            "• Доступ из любой страны: видеоматериалы + живые созвоны с преподавателем.\n"
            "• ИИ-модуль для автогенерации постов и промо — меньше рутины, больше охватов.\n"
            "• Комплект с **тату-машинкой** и опорой по открытию точки / работе в партнёрском салоне в вашем городе.\n"
            "• Длительность **1–2 месяца** в среднем; точный график — после предоплаты и брифа.\n\n"
            f"Вопросы и договор: {PAYMENT_CONTACT}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_online_training_keyboard(),
        )


async def handle_training_ai_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_TRAIN_AI_CASINO_VOUCHER:
        await finalize_training_ai_casino_voucher(update, context)
        return
    if text == BTN_TRAIN_AI_BUY:
        await start_payment_process(
            update,
            context,
            "ИИ-программа IKONA (full, автоконтент)",
            TRAINING_AI_PROGRAM_PRICE,
            payment_cancel_state="training_ai_details",
        )


async def handle_training_booking_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["training_booking_receipt_message_id"] = update.message.message_id
    await update.message.reply_text(
        "✅ Чек получен. Нажмите **«Я оплатил(а) ✅»**, чтобы завершить запись.\n\n"
        "_Если у вас приз казино на полный курс — используйте соответствующую кнопку._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_training_prepay_payment_keyboard(context),
    )
    try:
        u = update.effective_user
        lab = f"@{u.username}" if u.username else f"id:{u.id}"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📎 **Чек предоплаты обучения** от {lab} (message_id={update.message.message_id})",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error("handle_training_booking_payment_receipt admin: %s", e)


async def route_training_booking_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message else ""
    prev_state = context.user_data.get("state")
    if text == BTN_TRAIN_OFF_CASINO_VOUCHER:
        await finalize_training_casino_voucher_booking(update, context, "training_offline")
        return
    if text == BTN_TRAIN_ON_CASINO_VOUCHER:
        await finalize_training_casino_voucher_booking(update, context, "training_online")
        return
    if text == "Я оплатил(а) ✅":
        await finalize_training_booking_after_payment(update, context)
    elif text == "Отмена оплаты":
        context.user_data.pop("pending_training_booking", None)
        context.user_data.pop("training_booking_receipt_message_id", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        if prev_state == "training_offline_payment":
            context.user_data["state"] = "training_offline_details"
            kb = get_offline_training_keyboard()
            cap = "Предоплата отменена. Можно снова выбрать дату или вернуться назад."
        else:
            context.user_data["state"] = "training_online_details"
            kb = get_online_training_keyboard()
            cap = "Предоплата отменена. Можно снова оформить заявку."
        await send_disappointment_gif(context, update.effective_chat.id, cap, kb)
    else:
        await update.message.reply_text(
            "Пришлите **чек предоплаты** фото или PDF, затем **«Я оплатил(а) ✅»** или **«Отмена оплаты»**.\n\n"
            "Если выиграли **полный курс** в casino — используйте призовую кнопку выше.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_training_prepay_payment_keyboard(context),
        )


async def finalize_training_casino_voucher_booking(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str):
    vk = CASINO_VOUCHER_TRAIN_OFF_KEY if flow == "training_offline" else CASINO_VOUCHER_TRAIN_ON_KEY
    v = int(context.user_data.get(vk) or 0)
    if v < 1:
        await update.message.reply_text(
            "Призов по этой линии нет — загляните в **casino** 💎",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_training_prepay_payment_keyboard(context),
        )
        return
    pending = context.user_data.get("pending_training_booking")
    if not pending or pending.get("flow") != flow:
        await update.message.reply_text("Сессия записи сброшена. Откройте обучение снова.")
        return
    context.user_data[vk] = v - 1
    user = update.effective_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    selected_time = pending["time"]
    date_human = f"{date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}"
    if flow == "training_offline":
        e_note = (
            f"ПРИЗ КАЗИНО оффлайн-курс {TRAINING_OFFLINE_PRICE}₽: {master_name} | {date_human} {selected_time} | "
            f"0₽ предоплата (полный курс по лотерее) | согласование с менеджером"
        )
        time_display = f"{selected_time} | оффлайн ПРИЗ КАЗИНО {TRAINING_OFFLINE_PRICE}₽ (0₽ предоплаты)"
        cap_extra = f"Курс **{TRAINING_OFFLINE_PRICE} ₽** оформлен **по призу казино** (предоплата **0 ₽**)."
    else:
        e_note = (
            f"ПРИЗ КАЗИНО онлайн-курс {TRAINING_ONLINE_PRICE}₽: {master_name} | {date_human} {selected_time} | "
            f"0₽ предоплата | чек в боте не требуется (приз)"
        )
        time_display = f"{selected_time} | онлайн ПРИЗ КАЗИНО {TRAINING_ONLINE_PRICE}₽ (0₽ предоплаты)"
        cap_extra = f"Курс **{TRAINING_ONLINE_PRICE} ₽** по призу казино; доплату согласуем в чате."
    loading = await update.message.reply_text("⏳ Вношу призовую запись в таблицу…")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            context.user_data[vk] = v
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Не удалось открыть расписание.",
                get_training_submenu_keyboard(),
            )
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            context.user_data[vk] = v
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Нет свободной строки. Выберите другую дату.",
                get_training_submenu_keyboard(),
            )
            return
        await asyncio.to_thread(worksheet.update, f"E{first_row}", [[e_note]])
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🏆 **Обучение: ПРИЗ КАЗИНО** ({flow})\n{master_name}\n{time_display}\n`{worksheet.title}` **E{first_row}**",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.pop("pending_training_booking", None)
        context.user_data.pop("training_booking_receipt_message_id", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("booking_flow", None)
        context.user_data["state"] = "main_menu"
        cap = (
            f"✨ **Призовая запись создана**\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {selected_time}\n\n"
            f"{cap_extra}\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            f"Осталось призов этой линии: **{v - 1}**. Связь: {PAYMENT_CONTACT}"
        )
        await send_success_gif(context, update.effective_chat.id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("finalize_training_casino_voucher_booking: %s", e)
        context.user_data[vk] = v
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Ошибка записи. Напишите администратору.",
            get_training_submenu_keyboard(),
        )
    finally:
        await loading.delete()


async def finalize_training_ai_casino_voucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = int(context.user_data.get(CASINO_VOUCHER_TRAIN_AI_KEY) or 0)
    if v < 1:
        await update.message.reply_text(
            "Приза нет — сходите в **casino**, линия «ИИ» 💎",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_training_ai_keyboard(context),
        )
        return
    context.user_data[CASINO_VOUCHER_TRAIN_AI_KEY] = v - 1
    user = update.effective_user
    lab = f"@{user.username}" if user.username else f"id:{user.id}"
    note = f"ПРИЗ КАЗИНО ИИ-программа {TRAINING_AI_PROGRAM_PRICE}₽: {lab} id:{user.id} | полный доступ по лотерее"
    await _casino_append_note_column_e(note)
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🏆 **ИИ-программа: ПРИЗ КАЗИНО** {user.id}\n{lab}\n{note}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("finalize_training_ai_casino_voucher admin: %s", e)
    await send_success_gif(
        context,
        update.effective_chat.id,
        f"✨ **Приз активирован** — программа **{TRAINING_AI_PROGRAM_PRICE} ₽** по лотерее.\n\n"
        f"Осталось призов: **{v - 1}**. Свяжемся: {PAYMENT_CONTACT}",
        get_training_ai_keyboard(context),
        parse_mode=ParseMode.MARKDOWN,
    )


async def finalize_training_booking_after_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_training_booking")
    if not pending:
        await update.message.reply_text("Сессия записи сброшена. Откройте «Записаться на обучение» снова.")
        return
    if "training_booking_receipt_message_id" not in context.user_data:
        await update.message.reply_text("Сначала пришлите чек предоплаты в этот чат.")
        return
    user = update.effective_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    selected_time = pending["time"]
    flow = pending.get("flow") or "training_offline"
    date_human = f"{date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}"
    if flow == "training_offline":
        e_note = (
            f"Предзапись обучение офлайн: {master_name} | {date_human} {selected_time} | "
            f"предоплата {TRAINING_OFFLINE_PREPAY_FIRST}₽/{TRAINING_OFFLINE_PRICE}₽ | "
            f"тату машинка+ИИ программа | чек в боте"
        )
        time_display = (
            f"{selected_time} | оффлайн-обучение (предоплата {TRAINING_OFFLINE_PREPAY_FIRST}₽ / курс {TRAINING_OFFLINE_PRICE}₽)"
        )
        user_cap_extra = (
            f"Полный курс **{TRAINING_OFFLINE_PRICE} ₽**; внесена предоплата **{TRAINING_OFFLINE_PREPAY_FIRST} ₽** на первое занятие. "
            "Остаток **наличными** — на месте."
        )
    else:
        e_note = (
            f"Предоплата обучение онлайн: {master_name} | {date_human} {selected_time} | "
            f"предоплата {TRAINING_ONLINE_PREPAY}₽/{TRAINING_ONLINE_PRICE}₽ | чек в боте"
        )
        time_display = (
            f"{selected_time} | онлайн-обучение (предоплата {TRAINING_ONLINE_PREPAY}₽ / курс {TRAINING_ONLINE_PRICE}₽)"
        )
        user_cap_extra = (
            f"Курс **{TRAINING_ONLINE_PRICE} ₽**, предоплата **{TRAINING_ONLINE_PREPAY} ₽**. Доплату и график согласуем в чате."
        )
    loading = await update.message.reply_text("⏳ Записываю в таблицу (только столбец **E**)…")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Не удалось открыть расписание. Попробуйте позже.",
                get_training_submenu_keyboard(),
            )
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "На выбранную дату нет свободной строки. Выберите другую дату.",
                get_training_submenu_keyboard(),
            )
            return
        await asyncio.to_thread(worksheet.update, f"E{first_row}", [[e_note]])
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        rid = context.user_data["training_booking_receipt_message_id"]
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ **Обучение: предоплата принята** (запись **только в E{first_row}**)\n{master_name}\n{time_display}\n"
            f"Лист: `{worksheet.title}`\n**E{first_row}:** {e_note}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, rid)
        context.user_data.pop("pending_training_booking", None)
        context.user_data.pop("training_booking_receipt_message_id", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("booking_flow", None)
        context.user_data["state"] = "main_menu"
        cap = (
            f"✅ **Предоплата принята, запись внесена**\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {selected_time}\n\n"
            f"{user_cap_extra}\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            f"Связь: {PAYMENT_CONTACT}\n\n"
            "Вы в главном меню ниже 👇"
        )
        await send_success_gif(context, update.effective_chat.id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"finalize_training_booking_after_payment: {e}")
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Ошибка при записи в таблицу. Напишите администратору.",
            get_training_submenu_keyboard(),
        )
    finally:
        await loading.delete()

# =================================================================================
# --- RENT BOOKING MODULE ---
# =================================================================================

async def route_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    text = update.message.text if update.message and update.message.text else ""
    if state == 'rent_booking_menu':
        await handle_rent_booking_choice(update, context)

async def handle_rent_booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_on_cooldown(context, 'rent_booking'):
        return
    context.user_data['state'] = 'rent_booking_menu'
    caption = (
        f"[ IKONA AI ]\n"
        f"──────────────\n"
        f"Запрос на аренду рабочего пространства. Все данные о свободных слотах синхронизированы с базой данных.\n\n"
        f"📊 [Посмотреть расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)"
    )
    await send_dialog_gif(context, update.effective_chat.id, caption, get_rent_booking_menu(context), parse_mode=ParseMode.MARKDOWN)
    context.user_data.pop("rent_flow", None)
    context.user_data.pop("reschedule_old", None)


async def handle_rent_booking_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_RENT_CASINO_BONUS:
        if int(context.user_data.get("casino_free_rent_credits") or 0) < 1:
            await update.message.reply_text(
                "Бонусных смен сейчас нет — загляните в зал **casino** или напишите нам, мы подскажем 💕",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_rent_booking_menu(context),
            )
            return
        context.user_data["rent_use_free_credit"] = True
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        context.user_data.pop("booking_flow", None)
        await update.message.reply_text(
            "Выберите **дату** бесплатной смены в календаре ниже (оплата не нужна).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_calendar(update, context)
        return
    if text == "Выбрать дату аренды":
        context.user_data.pop("rent_use_free_credit", None)
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        context.user_data.pop("booking_flow", None)
        await show_calendar(update, context)


def _calendar_title(context: ContextTypes.DEFAULT_TYPE) -> str:
    if context.user_data.get("rent_flow") == "reschedule":
        return "Выберите новую дату для переноса:"
    if context.user_data.get("booking_flow") == "tattoo":
        return "Выберите дату сеанса тату:"
    if context.user_data.get("booking_flow") == "training_offline":
        return "Офлайн-обучение: выберите дату первого дня:"
    if context.user_data.get("booking_flow") == "training_online":
        return "Онлайн-обучение: выберите желаемую дату старта:"
    return "Выберите дату для аренды:"


async def find_first_empty_rent_row(worksheet, date_header: str):
    for attempt in range(MAX_RETRIES):
        try:
            date_cells = await asyncio.to_thread(worksheet.findall, date_header, in_column=1)
            if not date_cells:
                return None
            date_cell = date_cells[0]
            day_block_data = await asyncio.to_thread(worksheet.get, f"A{date_cell.row}:E{date_cell.row + 20}")
            first_empty_row = -1
            for i in range(2, min(len(day_block_data), 20)):
                row_data = day_block_data[i]
                first_cell_value = row_data[0] if row_data else ""
                e_cell = row_data[4] if len(row_data) > 4 else ""
                if first_cell_value and re.match(r"^\d{1,2}\s", str(first_cell_value)):
                    break
                # Строка «свободна» для аренды: A пусто и E пусто (в E — только предзапись тату, без A–D)
                if (
                    not str(first_cell_value).strip()
                    and not str(e_cell).strip()
                    and first_empty_row == -1
                ):
                    first_empty_row = date_cell.row + i
                    break
            return first_empty_row if first_empty_row != -1 else None
        except APIError as e:
            if e.response.status_code == 429:
                await asyncio.sleep(BASE_RETRY_DELAY * (2 ** attempt))
            else:
                logger.error(f"find_first_empty_rent_row: {e}")
                return None
        except Exception as e:
            logger.error(f"find_first_empty_rent_row: {e}")
            return None
    return None


async def get_date_header_above_row(worksheet, row_idx: int) -> str:
    all_data = await asyncio.to_thread(worksheet.get_all_values)
    for i in range(row_idx - 2, -1, -1):
        if i < 0 or i >= len(all_data):
            continue
        row = all_data[i]
        if row and row[0] and re.match(r"^\d{1,2}\s+\w+", str(row[0]).strip()):
            return str(row[0]).strip()
    return ""


def _format_date_header_to_ddmmyyyy(date_header: str, year: int) -> str:
    try:
        day_s, month_name = date_header.split()
        day = int(day_s)
        month = next(k for k, v in RUSSIAN_MONTHS.items() if v.lower() == month_name.lower())
        return f"{day:02d}.{month:02d}.{year}"
    except Exception:
        return date_header


async def route_rent_booking_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else ""
    if text == "Я оплатил(а) ✅":
        await finalize_new_rent_booking_after_payment(update, context)
    elif text == "Отмена оплаты":
        context.user_data.pop("pending_rent_booking", None)
        context.user_data.pop("rent_booking_receipt_message_id", None)
        context.user_data["state"] = "rent_booking_menu"
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Оплата отменена. Можно начать запись заново.",
            get_rent_booking_menu(context),
        )


async def handle_rent_booking_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent_booking_receipt_message_id"] = update.message.message_id
    await update.message.reply_text(
        "✅ Чек получен. Нажмите «Я оплатил(а) ✅», чтобы завершить запись.",
        reply_markup=get_payment_confirmation_keyboard(),
    )


async def finalize_new_rent_booking_after_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_rent_booking")
    if not pending:
        await update.message.reply_text("Сессия записи сброшена. Откройте «Записаться на аренду» снова.")
        return
    if "rent_booking_receipt_message_id" not in context.user_data:
        await update.message.reply_text("Сначала пришлите чек в этот чат.")
        return
    user = update.effective_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    selected_time = pending["time"]
    supply_label = pending["supply_label"]
    total = pending["total"]
    time_display = f"{selected_time} смена | {supply_label} | итого {total}₽"
    loading = await update.message.reply_text("⏳ Записываю в таблицу и отправляю чек администратору...")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Не удалось открыть расписание. Попробуйте позже.",
                get_rent_booking_menu(context),
            )
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "На выбранную дату нет свободных строк. Выберите другую дату.",
                get_rent_booking_menu(context),
            )
            return
        await asyncio.to_thread(
            worksheet.update,
            f"A{first_row}:E{first_row}",
            [[master_name, time_display, "оплачено", "активна", ""]],
        )
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        rid = context.user_data["rent_booking_receipt_message_id"]
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ **Новая аренда (оплата сразу)**\n{master_name}\n{time_display}\nЛист: `{worksheet.title}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, rid)
        context.user_data.pop("pending_rent_booking", None)
        context.user_data.pop("rent_booking_receipt_message_id", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        context.user_data["state"] = "main_menu"
        cap = (
            "✅ **Запись и оплата приняты!**\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {selected_time}\n"
            f"💵 {total} ₽\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            "Вы в главном меню ниже 👇"
        )
        await send_success_gif(context, update.effective_chat.id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"finalize_new_rent_booking_after_payment: {e}")
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Ошибка при записи. Напишите администратору.",
            get_rent_booking_menu(context),
        )
    finally:
        await loading.delete()


async def handle_tattoo_booking_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tattoo_booking_receipt_message_id"] = update.message.message_id
    await update.message.reply_text(
        "✅ Чек получила, спасибо 💕 Нажмите **«Я оплатил(а) ✅»**, чтобы завершить предоплату.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_tattoo_booking_payment_keyboard(context),
    )
    try:
        u = update.effective_user
        lab = f"@{u.username}" if u.username else f"id:{u.id}"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📎 **Чек предоплаты тату** от {lab} (message_id={update.message.message_id})",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, update.message.message_id)
    except Exception as e:
        logger.error("handle_tattoo_booking_payment_receipt admin: %s", e)


async def finalize_tattoo_casino_voucher_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = int(context.user_data.get(CASINO_TATTOO_VOUCHERS_KEY) or 0)
    if v < 1:
        await update.message.reply_text(
            "Призовых сертификатов сейчас нет — загляните в **casino**, там VIP‑лотерея 💎",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_tattoo_booking_payment_keyboard(context),
        )
        return
    pending = context.user_data.get("pending_tattoo_booking")
    if not pending:
        await update.message.reply_text("Сессия записи сброшена. Откройте «записаться на тату» снова.")
        return
    context.user_data[CASINO_TATTOO_VOUCHERS_KEY] = v - 1
    user = update.effective_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    selected_time = pending["time"]
    sketch_name = pending.get("sketch_name") or "—"
    date_human = f"{date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}"
    e_note = (
        f"Тату приз казино VIP {TATTOO_SESSION_PRICE}₽: {master_name} | {date_human} {selected_time} | "
        f"эскиз: {sketch_name} | без предоплаты (приз лотереи)"
    )
    time_display = f"{selected_time} | тату VIP-приз казино {TATTOO_SESSION_PRICE}₽ (0₽ предоплаты)"
    loading = await update.message.reply_text("⏳ Аккуратно вношу призовую запись в таблицу…")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            context.user_data[CASINO_TATTOO_VOUCHERS_KEY] = v
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Не удалось открыть расписание. Напишите нам, мы поможем: " + PAYMENT_CONTACT,
                get_tattoo_submenu_keyboard(),
            )
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            context.user_data[CASINO_TATTOO_VOUCHERS_KEY] = v
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "На эту дату нет свободной строки. Выберите, пожалуйста, другую дату.",
                get_tattoo_submenu_keyboard(),
            )
            return
        await asyncio.to_thread(worksheet.update, f"E{first_row}", [[e_note]])
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        sketch_path = pending.get("sketch_path")
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🏆 **Тату: приз казино VIP** (только **E{first_row}**)\n{master_name}\n{time_display}\n"
            f"Лист: `{worksheet.title}`\n**E{first_row}:** {e_note}",
            parse_mode=ParseMode.MARKDOWN,
        )
        if sketch_path and os.path.isfile(sketch_path):
            try:
                with open(sketch_path, "rb") as ph:
                    await context.bot.send_photo(
                        ADMIN_CHAT_ID,
                        ph,
                        caption=f"Эскиз по призовой записи: `{sketch_name}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
            except Exception as e:
                logger.error("admin tattoo voucher sketch: %s", e)
        context.user_data.pop("pending_tattoo_booking", None)
        context.user_data.pop("tattoo_booking_receipt_message_id", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("tattoo_source", None)
        context.user_data.pop("sketch_path", None)
        context.user_data["state"] = "main_menu"
        cap = (
            f"✨ **Призовая запись создана**\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {selected_time}\n"
            f"💎 Сеанс по сертификату **{TATTOO_SESSION_PRICE} ₽** (предоплата **0 ₽**)\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            f"Детали согласуем в чате: {PAYMENT_CONTACT}\n\n"
            f"Осталось призовых сертификатов: **{v - 1}**.\n\n"
            "Главное меню ниже 👇"
        )
        await send_success_gif(context, update.effective_chat.id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("finalize_tattoo_casino_voucher_booking: %s", e)
        context.user_data[CASINO_TATTOO_VOUCHERS_KEY] = v
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Ошибка при записи. Напишите администратору.",
            get_tattoo_submenu_keyboard(),
        )
    finally:
        await loading.delete()


async def route_tattoo_booking_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message else ""
    if text == BTN_TATTOO_CASINO_VOUCHER:
        await finalize_tattoo_casino_voucher_booking(update, context)
        return
    if text == "Я оплатил(а) ✅":
        await finalize_tattoo_booking_after_payment(update, context)
    elif text == "Отмена оплаты":
        context.user_data.pop("pending_tattoo_booking", None)
        context.user_data.pop("tattoo_booking_receipt_message_id", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "tattoo_submenu"
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Предоплата отменена. Можно снова выбрать эскиз и дату.",
            get_tattoo_submenu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Пришлите **чек предоплаты** фото или PDF в этот чат, затем нажмите **«Я оплатил(а) ✅»** "
            "или **«Отмена оплаты»**.\n\n"
            f"Если у вас есть приз из **casino**, можно нажать **«{BTN_TATTOO_CASINO_VOUCHER}»** — без чека.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_tattoo_booking_payment_keyboard(context),
        )


async def finalize_tattoo_booking_after_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_tattoo_booking")
    if not pending:
        await update.message.reply_text("Сессия предоплаты сброшена. Откройте «записаться на тату» снова.")
        return
    if "tattoo_booking_receipt_message_id" not in context.user_data:
        await update.message.reply_text("Сначала пришлите чек предоплаты в этот чат.")
        return
    user = update.effective_user
    master_name = f"@{user.username} (id:{user.id})" if user.username else f"id:{user.id}"
    date_info = pending["date_info"]
    selected_time = pending["time"]
    sketch_name = pending.get("sketch_name") or "—"
    date_human = f"{date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}"
    # Только столбец E: A–D остаются для аренды
    e_note = (
        f"Предзапись тату: {master_name} | {date_human} {selected_time} | "
        f"предоплата {TATTOO_PREPAY_AMOUNT}₽/{TATTOO_SESSION_PRICE}₽ | эскиз: {sketch_name} | чек в боте"
    )
    time_display = f"{selected_time} | тату (предоплата {TATTOO_PREPAY_AMOUNT}₽ / сеанс {TATTOO_SESSION_PRICE}₽)"
    loading = await update.message.reply_text("⏳ Записываю предзапись в таблицу (только столбец E)…")
    try:
        worksheet = await get_worksheet_cached(date_info["worksheet"])
        if not worksheet:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "Не удалось открыть расписание. Попробуйте позже.",
                get_tattoo_submenu_keyboard(),
            )
            return
        first_row = await find_first_empty_rent_row(worksheet, date_info["header"])
        if not first_row:
            await send_disappointment_gif(
                context,
                update.effective_chat.id,
                "На выбранную дату нет свободной строки в таблице. Выберите другую дату.",
                get_tattoo_submenu_keyboard(),
            )
            return
        await asyncio.to_thread(worksheet.update, f"E{first_row}", [[e_note]])
        cache_key = f"{worksheet.title}_{date_info['header']}_slots"
        if cache_key in sheets_cache:
            del sheets_cache[cache_key]
        rid = context.user_data["tattoo_booking_receipt_message_id"]
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ **Тату: предоплата принята** (запись **только в E{first_row}**)\n{master_name}\n{time_display}\n"
            f"Лист: `{worksheet.title}`\n**E{first_row}:** {e_note}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.forward_message(ADMIN_CHAT_ID, update.effective_chat.id, rid)
        sketch_path = pending.get("sketch_path")
        if sketch_path and os.path.isfile(sketch_path):
            try:
                with open(sketch_path, "rb") as ph:
                    await context.bot.send_photo(
                        ADMIN_CHAT_ID,
                        ph,
                        caption=f"Эскиз по записи: `{sketch_name}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
            except Exception as e:
                logger.error("admin tattoo sketch photo: %s", e)
        context.user_data.pop("pending_tattoo_booking", None)
        context.user_data.pop("tattoo_booking_receipt_message_id", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data.pop("booking_flow", None)
        context.user_data.pop("tattoo_source", None)
        context.user_data.pop("sketch_path", None)
        context.user_data["state"] = "main_menu"
        cap = (
            f"✅ **Предоплата принята, предзапись создана**\n\n"
            f"📅 {date_info['day']:02d}.{date_info['month']:02d}.{date_info['year']}\n"
            f"⏰ {selected_time}\n"
            f"💵 Предоплата: **{TATTOO_PREPAY_AMOUNT} ₽** (сеанс **{TATTOO_SESSION_PRICE} ₽**)\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)\n\n"
            f"Доплату и детали согласуем в чате: {PAYMENT_CONTACT}\n\n"
            "Вы в главном меню ниже 👇"
        )
        await send_success_gif(context, update.effective_chat.id, cap, get_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"finalize_tattoo_booking_after_payment: {e}")
        await send_disappointment_gif(
            context,
            update.effective_chat.id,
            "Ошибка при записи в таблицу. Напишите администратору.",
            get_tattoo_submenu_keyboard(),
        )
    finally:
        await loading.delete()


async def handle_rent_supply_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_callback_answer(query)
    data = query.data
    if data == "supply_back_time":
        sd = context.user_data.get("selected_date")
        if not sd:
            return
        await query.edit_message_text(
            f"📅 Дата: {sd['day']}.{sd['month']:02d}.{sd['year']}\n\nВыберите время прихода:",
            reply_markup=get_time_slots_keyboard(),
        )
        return
    addon = 0
    supply_label = ""
    if data == "supply_full":
        addon = RENT_ADDON_FULL_KIT
        supply_label = "полный набор: перчатки, краска, бумажные полотенца (+1000₽)"
    elif data == "supply_gloves":
        addon = RENT_ADDON_GLOVES_ONLY
        supply_label = "перчатки и бумажные полотенца (+500₽)"
    elif data == "supply_own":
        addon = 0
        supply_label = "свой набор расходников"
    else:
        return
    base = RENT_SHIFT_BASE_PRICE
    total = base + addon
    sd = context.user_data["selected_date"]
    tm = context.user_data["selected_time"]
    context.user_data["pending_rent_booking"] = {
        "date_info": dict(sd),
        "time": tm,
        "supply_label": supply_label,
        "base": base,
        "addon": addon,
        "total": total,
    }
    if context.user_data.pop("rent_use_free_credit", None):
        cred = int(context.user_data.get("casino_free_rent_credits") or 0)
        if cred < 1:
            context.user_data.pop("pending_rent_booking", None)
            await query.message.reply_text("Бонусных смен нет.")
            return
        await finalize_free_rent_bonus_booking(query, context)
        return
    context.user_data["state"] = "rent_booking_payment"
    context.user_data.pop("rent_booking_receipt_message_id", None)
    pay_txt = (
        f"💳 **Оплата аренды**\n\n"
        f"📅 Дата: {sd['day']:02d}.{sd['month']:02d}.{sd['year']}\n"
        f"⏰ Время: {tm}\n"
        f"🧰 Расходники: {supply_label}\n\n"
        f"Смена: *{base} ₽*\n"
    )
    if addon:
        pay_txt += f"Доплата: *+{addon} ₽*\n"
    pay_txt += (
        f"**Итого: {total} ₽**\n\n"
        f"Оплата на Т‑Банк: `{PAYMENT_PHONE_NUMBER}`\n"
        f"Проблемы с оплатой — {PAYMENT_CONTACT}\n\n"
        "Сначала пришлите **чек** (фото или PDF), затем нажмите «Я оплатил(а) ✅»."
    )
    await query.message.reply_text(pay_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=get_payment_confirmation_keyboard())


async def complete_reschedule_booking(query, context: ContextTypes.DEFAULT_TYPE):
    old = context.user_data.get("reschedule_old")
    new_sd = context.user_data.get("selected_date")
    selected_time = context.user_data.get("selected_time")
    if not old or not new_sd or not selected_time:
        await query.message.reply_text("Ошибка данных.")
        return
    user_id = query.from_user.id
    worksheet_old = await get_worksheet_cached(old["worksheet"])
    row_old = old["row"]
    if not worksheet_old:
        await query.message.reply_text("Старый лист недоступен.")
        return
    row_vals = await asyncio.to_thread(worksheet_old.row_values, row_old)
    while len(row_vals) < 5:
        row_vals.append("")
    master_name = row_vals[0]
    time_old = row_vals[1] if len(row_vals) > 1 else ""
    pay_old = row_vals[2] if len(row_vals) > 2 else "нет"
    user_pattern = f"id:{user_id}"
    if user_pattern not in str(master_name) and str(user_id) not in str(master_name):
        await query.message.reply_text("Это не ваша строка.")
        return
    worksheet_new = await get_worksheet_cached(new_sd["worksheet"])
    if not worksheet_new:
        await query.message.reply_text("Новый лист недоступен.")
        return
    first_row = await find_first_empty_rent_row(worksheet_new, new_sd["header"])
    if not first_row:
        await query.message.reply_text("На новую дату нет свободной строки.")
        await send_disappointment_gif(
            context,
            query.message.chat_id,
            "Нет мест на выбранную дату для переноса.",
            get_rent_submenu_keyboard(),
        )
        return
    old_header = await get_date_header_above_row(worksheet_old, row_old)
    y_old = int(worksheet_old.title.split()[-1])
    old_fmt = _format_date_header_to_ddmmyyyy(old_header, y_old) if old_header else "?"
    new_date_human = f"{new_sd['day']:02d}.{new_sd['month']:02d}.{new_sd['year']}"
    hist = (
        f"Аренда перенесена на {new_date_human}, смена {selected_time}. "
        f"Было: {master_name} | {time_old}. Слот освобождён."
    )
    time_new = f"{selected_time} смена | перенос с {old_fmt} | было: {time_old}"
    pay_lower = str(pay_old).lower()
    pay_new = "оплачено" if "оплач" in pay_lower else (pay_old if str(pay_old).strip() else "нет")
    note_new = f"Перенос с {old_fmt} ({worksheet_old.title})"
    loading = await query.message.reply_text("⏳ Выполняю перенос в таблице...")
    try:
        await asyncio.to_thread(
            worksheet_old.update,
            f"A{row_old}:E{row_old}",
            [["", "", "", "", hist]],
        )
        await asyncio.to_thread(
            worksheet_new.update,
            f"A{first_row}:E{first_row}",
            [[master_name, time_new, pay_new, "активна", note_new]],
        )
        for cache_key in list(sheets_cache.keys()):
            if cache_key.startswith(f"{worksheet_old.title}_") or cache_key.startswith(f"{worksheet_new.title}_"):
                del sheets_cache[cache_key]

        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        context.user_data.pop("selected_date", None)
        context.user_data.pop("selected_time", None)
        context.user_data["state"] = "rent_submenu"
        await send_success_gif(
            context,
            query.message.chat_id,
            f"✅ **Перенос выполнен**\n\n"
            f"Новая дата: {new_sd['day']:02d}.{new_sd['month']:02d}.{new_sd['year']}\n"
            f"Время: {selected_time}\n\n"
            f"📊 [Расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)",
            get_rent_submenu_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"complete_reschedule_booking: {e}")
        await send_disappointment_gif(
            context,
            query.message.chat_id,
            "Не удалось перенести запись. Попробуйте позже или напишите администратору.",
            get_rent_submenu_keyboard(),
        )
    finally:
        await loading.delete()


async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.date.today()
    year = today.year
    month = today.month
    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month
    keyboard = generate_calendar_keyboard(year, month)
    title = _calendar_title(context)
    if update.callback_query:
        await update.callback_query.edit_message_text(title, reply_markup=keyboard)
    else:
        await update.message.reply_text(title, reply_markup=keyboard)

async def handle_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "ignore":
        await _safe_callback_answer(query)
        return
    if data == "back_to_menu":
        await _safe_callback_answer(query)
        bf = context.user_data.pop("booking_flow", None)
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        if bf == "tattoo":
            context.user_data["state"] = "tattoo_submenu"
            await query.message.reply_text("Запись на тату.", reply_markup=get_tattoo_submenu_keyboard())
        elif bf == "training_offline":
            context.user_data.pop("selected_date", None)
            context.user_data.pop("selected_time", None)
            context.user_data["state"] = "training_offline_details"
            await query.message.reply_text(
                "Оффлайн-обучение.",
                reply_markup=get_offline_training_keyboard(),
            )
        elif bf == "training_online":
            context.user_data.pop("selected_date", None)
            context.user_data.pop("selected_time", None)
            context.user_data["state"] = "training_online_details"
            await query.message.reply_text(
                "Онлайн-обучение.",
                reply_markup=get_online_training_keyboard(),
            )
        else:
            context.user_data["state"] = "rent_submenu"
            await query.message.reply_text("Аренда.", reply_markup=get_rent_submenu_keyboard())
        return
    if data.startswith("nav_"):
        await _safe_callback_answer(query)
        _, year, month = data.split("_")
        year, month = int(year), int(month)
        context.user_data['calendar_year'] = year
        context.user_data['calendar_month'] = month
        keyboard = generate_calendar_keyboard(year, month)
        await query.edit_message_text(_calendar_title(context), reply_markup=keyboard)
        return
    if data.startswith("date_"):
        _, year, month, day = data.split("_")
        year, month, day = int(year), int(month), int(day)
        selected_date = datetime.date(year, month, day)
        today = datetime.date.today()
        keyboard = generate_calendar_keyboard(year, month)
        if selected_date < today:
            await _safe_callback_answer(query, "Эта дата уже прошла!", show_alert=True)
            await query.edit_message_text(_calendar_title(context), reply_markup=keyboard)
            return

        await _safe_callback_answer(query)

        sheet_name = f"{RUSSIAN_MONTHS[month]} {year}"
        try:
            worksheet = await asyncio.wait_for(
                get_worksheet_cached(sheet_name),
                timeout=CALENDAR_SHEET_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("Timeout loading worksheet %s for calendar", sheet_name)
            worksheet = None
        if not worksheet:
            try:
                await query.edit_message_text(
                    f"{_calendar_title(context)}\n\n"
                    "⚠️ **Расписание сейчас не открывается** (сеть или Google Таблицы). "
                    "Попробуйте ещё раз через минуту или напишите: "
                    f"{PAYMENT_CONTACT}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except BadRequest:
                pass
            return

        date_header = f"{day} {RUSSIAN_MONTHS[month]}"
        available_slots = await get_available_slots_count(worksheet, date_header)

        if available_slots < 0:
            try:
                await query.edit_message_text(
                    f"{_calendar_title(context)}\n\n"
                    "⚠️ **Не удалось проверить свободные места** "
                    "(прокси, сеть или Google). Попробуйте позже или напишите: "
                    f"{PAYMENT_CONTACT}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except BadRequest:
                pass
            return

        if available_slots <= 0:
            try:
                await query.edit_message_text(
                    f"{_calendar_title(context)}\n\n"
                    f"На **{day:02d}.{month:02d}.{year}** свободных мест **нет**.",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except BadRequest:
                pass
            return

        context.user_data['selected_date'] = {
            'year': year,
            'month': month,
            'day': day,
            'header': date_header,
            'worksheet': sheet_name
        }
        bf = context.user_data.get("booking_flow")
        if bf == "tattoo":
            try:
                u = query.from_user
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"📅 **Тату: выбрана дата** @{u.username or u.id}\n"
                    f"{day:02d}.{month:02d}.{year} · свободных мест: {available_slots}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("admin tattoo date: %s", e)
        elif bf in ("training_offline", "training_online"):
            try:
                u = query.from_user
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"📅 **Обучение ({bf}): дата** @{u.username or u.id}\n"
                    f"{day:02d}.{month:02d}.{year} · мест: {available_slots}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("admin training date: %s", e)
        is_rs = context.user_data.get("rent_flow") == "reschedule"
        date_line = (
            f"📅 Новая дата: {day}.{month:02d}.{year}" if is_rs else f"📅 Выбрана дата: {day}.{month:02d}.{year}"
        )
        await query.edit_message_text(
            f"{date_line}\n"
            f"🆓 Свободных мест: {available_slots}\n\n"
            f"Выберите время прихода:",
            reply_markup=get_time_slots_keyboard(),
        )

async def handle_time_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_callback_answer(query)
    if query.data == "back_to_dates":
        if context.user_data.get("rent_flow") == "reschedule":
            y = context.user_data.get("calendar_year", datetime.date.today().year)
            m = context.user_data.get("calendar_month", datetime.date.today().month)
            await query.edit_message_text(
                _calendar_title(context),
                reply_markup=generate_calendar_keyboard(y, m),
            )
        elif context.user_data.get("booking_flow") == "tattoo":
            y = context.user_data.get("calendar_year", datetime.date.today().year)
            m = context.user_data.get("calendar_month", datetime.date.today().month)
            await query.edit_message_text(
                _calendar_title(context),
                reply_markup=generate_calendar_keyboard(y, m),
            )
        elif context.user_data.get("booking_flow") in ("training_offline", "training_online"):
            y = context.user_data.get("calendar_year", datetime.date.today().year)
            m = context.user_data.get("calendar_month", datetime.date.today().month)
            await query.edit_message_text(
                _calendar_title(context),
                reply_markup=generate_calendar_keyboard(y, m),
            )
        else:
            await show_calendar(update, context)
        return
    if query.data.startswith("time_"):
        selected_time = query.data.replace("time_", "")
        context.user_data['selected_time'] = selected_time
        sd = context.user_data['selected_date']
        if context.user_data.get("rent_flow") == "reschedule":
            await complete_reschedule_booking(query, context)
            return
        if context.user_data.get("booking_flow") == "tattoo":
            sketch_path = context.user_data.get("sketch_path") or ""
            sketch_name = os.path.basename(sketch_path) if sketch_path else "—"
            user = query.from_user
            uname = f"@{user.username}" if user.username else f"id:{user.id}"
            try:
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"📋 **Тату: выбраны дата и время**\n{uname}\n"
                    f"📅 {sd['day']:02d}.{sd['month']:02d}.{sd['year']} ⏰ {selected_time}\n"
                    f"Эскиз: `{sketch_name}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("admin notify tattoo slot: %s", e)
            context.user_data["pending_tattoo_booking"] = {
                "date_info": dict(sd),
                "time": selected_time,
                "sketch_path": sketch_path,
                "sketch_name": sketch_name,
            }
            context.user_data["state"] = "tattoo_booking_payment"
            context.user_data.pop("tattoo_booking_receipt_message_id", None)
            pay_txt = (
                f"💳 **Предоплата за сеанс тату**\n\n"
                f"📅 Дата: {sd['day']:02d}.{sd['month']:02d}.{sd['year']}\n"
                f"⏰ Время: {selected_time}\n"
                f"🖼 Эскиз: {sketch_name}\n\n"
                f"Стоимость сеанса: **{TATTOO_SESSION_PRICE} ₽**\n"
                f"Сейчас нужна предоплата: **{TATTOO_PREPAY_AMOUNT} ₽**\n\n"
                f"Оплата на Т-Банк: `{PAYMENT_PHONE_NUMBER}`\n"
                f"Вопросы: {PAYMENT_CONTACT}\n\n"
                "Пришлите **чек об оплате** в этот чат (фото или документ), затем нажмите **«Я оплатил(а) ✅»**."
            )
            await query.edit_message_text(pay_txt, parse_mode=ParseMode.MARKDOWN)
            await query.message.reply_text(
                "Кнопки подтверждения — ниже. Если выиграли приз в **casino**, там будет отдельная кнопка 💎",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_tattoo_booking_payment_keyboard(context),
            )
            return
        bf = context.user_data.get("booking_flow")
        if bf in ("training_offline", "training_online"):
            user = query.from_user
            uname = f"@{user.username}" if user.username else f"id:{user.id}"
            label = "оффлайн" if bf == "training_offline" else "онлайн"
            try:
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"📋 **Обучение ({label}): дата и время**\n{uname}\n"
                    f"📅 {sd['day']:02d}.{sd['month']:02d}.{sd['year']} ⏰ {selected_time}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("admin training slot: %s", e)
            context.user_data["pending_training_booking"] = {
                "flow": bf,
                "date_info": dict(sd),
                "time": selected_time,
            }
            pay_state = "training_offline_payment" if bf == "training_offline" else "training_online_payment"
            context.user_data["state"] = pay_state
            context.user_data.pop("training_booking_receipt_message_id", None)
            if bf == "training_offline":
                pay_txt = (
                    f"💳 **Предоплата на первое занятие (оффлайн-обучение IKONA)**\n\n"
                    f"📅 Дата первого дня: {sd['day']:02d}.{sd['month']:02d}.{sd['year']}\n"
                    f"⏰ Время начала: {selected_time}\n\n"
                    f"Полный курс: **140 000 ₽** — тату-машинка и ИИ-программа для автосоздания контента для продвижения.\n"
                    f"Сейчас внесите предоплату: **2 000 ₽** за первое занятие.\n\n"
                    f"Остаток **наличными** на месте — уточним при встрече.\n\n"
                    f"Оплата на Т-Банк: `{PAYMENT_PHONE_NUMBER}`\n"
                    f"Вопросы: {PAYMENT_CONTACT}\n\n"
                    "Пришлите **чек** в этот чат, затем **«Я оплатил(а) ✅»**."
                )
            else:
                pay_txt = (
                    f"💳 **Предоплата онлайн-обучения IKONA**\n\n"
                    f"📅 Дата старта (ориентир): {sd['day']:02d}.{sd['month']:02d}.{sd['year']}\n"
                    f"⏰ Время созвона: {selected_time}\n\n"
                    f"Стоимость курса: **99 000 ₽** (срок 1–2 месяца).\n"
                    f"Сейчас внесите предоплату: **2 000 ₽**.\n\n"
                    f"Доплату и график согласуем с менеджером после заявки.\n\n"
                    f"Оплата на Т-Банк: `{PAYMENT_PHONE_NUMBER}`\n"
                    f"Вопросы: {PAYMENT_CONTACT}\n\n"
                    "Пришлите **чек** в этот чат, затем **«Я оплатил(а) ✅»**."
                )
            await query.edit_message_text(pay_txt, parse_mode=ParseMode.MARKDOWN)
            await query.message.reply_text(
                "Кнопки ниже: оплата предоплаты или **приз казино** на полный курс (если есть).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_training_prepay_payment_keyboard(context),
            )
            return
        await query.edit_message_text(
            f"📅 Дата: {sd['day']}.{sd['month']:02d}.{sd['year']}\n"
            f"⏰ Время: {selected_time}\n\n"
            "Что вам понадобится для работы?\n\n"
            f"{RENT_KIT_INCLUDED_SUPPLY_LIST}",
            reply_markup=get_rent_supplies_keyboard(),
        )

async def handle_rent_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_times":
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n\n"
            f"Выберите время прихода:",
            reply_markup=get_time_slots_keyboard()
        )
        return
    if query.data == "rent_type_hourly":
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n\n"
            f"Выберите количество часов:",
            reply_markup=get_hours_selection_keyboard()
        )
    else:
        rent_type = "фулл" if query.data == "rent_type_full" else "почасовая"
        context.user_data['rent_type'] = rent_type
        if rent_type == "фулл":
            context.user_data['selected_hours'] = "фулл день"
            context.user_data['selected_price'] = 2500
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n"
            f"💰 Тип аренды: {rent_type}\n\n"
            f"Выберите тип сборки рабочего места:",
            reply_markup=get_workplace_setup_keyboard()
        )

async def handle_hours_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_rent_type":
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n\n"
            f"Выберите тип аренды:",
            reply_markup=get_rent_type_keyboard()
        )
        return
    if query.data.startswith("hours_"):
        hours = query.data.replace("hours_", "")
        if hours == "2":
            price = 1300
            hours_text = "2 часа"
        elif hours == "3":
            price = 1950
            hours_text = "3 часа"
        else:
            price = 2500
            hours_text = "4 часа и более"
        context.user_data['rent_type'] = "почасовая"
        context.user_data['selected_hours'] = hours_text
        context.user_data['selected_price'] = price
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n"
            f"💰 Тип аренды: почасовая\n"
            f"⏱ Количество часов: {hours_text}\n\n"
            f"Выберите тип сборки рабочего места:",
            reply_markup=get_workplace_setup_keyboard()
        )

async def handle_workplace_setup_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_rent_type":
        await query.edit_message_text(
            f"📅 Дата: {context.user_data['selected_date']['day']}.{context.user_data['selected_date']['month']:02d}.{context.user_data['selected_date']['year']}\n"
            f"⏰ Время: {context.user_data['selected_time']}\n\n"
            f"Выберите тип аренды:",
            reply_markup=get_rent_type_keyboard()
        )
        return
    if query.data.startswith("workplace_"):
        workplace_type = query.data.replace("workplace_", "")
        context.user_data['workplace_setup'] = workplace_type
        await process_rent_booking_final(update, context)

async def process_rent_booking_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    loading_message = await query.message.reply_text("⏳ Записываю вас...")
    try:
        date_info = context.user_data['selected_date']
        selected_time = context.user_data['selected_time']
        rent_type = context.user_data['rent_type']
        hours_text = context.user_data.get('selected_hours', '')
        price = context.user_data.get('selected_price', 0)
        workplace_setup = context.user_data.get('workplace_setup', '')
        worksheet = await get_worksheet_cached(date_info['worksheet'])
        if not worksheet:
            await query.answer("Ошибка доступа к расписанию!", show_alert=True)
            return
        date_header = date_info['header']
        for attempt in range(MAX_RETRIES):
            try:
                date_cells = await asyncio.to_thread(worksheet.findall, date_header, in_column=1)
                if not date_cells:
                    await query.answer("Дата не найдена в расписании!", show_alert=True)
                    return
                date_cell = date_cells[0]
                day_block_data = await asyncio.to_thread(worksheet.get, f'A{date_cell.row}:D{date_cell.row + 20}')
                first_empty_row = -1
                for i in range(2, min(len(day_block_data), 20)):
                    row_data = day_block_data[i]
                    first_cell_value = row_data[0] if row_data else ""
                    if first_cell_value and re.match(r'^\d{1,2}\s', str(first_cell_value)):
                        break
                    if not first_cell_value and first_empty_row == -1:
                        first_empty_row = date_cell.row + i
                        break
                if first_empty_row == -1:
                    await query.answer("На эту дату нет свободных мест!", show_alert=True)
                    return
                master_name = f"@{query.from_user.username} (id:{user_id})" if query.from_user.username else f"id:{user_id}"
                if rent_type == "почасовая":
                    time_display = f"{selected_time} {rent_type} ({hours_text})"
                else:
                    time_display = f"{selected_time} {rent_type}"
                
                # Добавляем информацию о сборке рабочего места
                if workplace_setup == "setup":
                    time_display += " (сборка)"
                elif workplace_setup == "self":
                    time_display += " (самостоят)"
                    
                await asyncio.to_thread(worksheet.update, f'A{first_empty_row}:D{first_empty_row}', 
                                       [[master_name, time_display, "нет", "активна"]])
                cache_key = f"{worksheet.title}_{date_header}_slots"
                if cache_key in sheets_cache:
                    del sheets_cache[cache_key]
                confirmation_text = (
                    f"✅ **Запись подтверждена!**\n\n"
                    f"📅 **Дата:** {date_info['day']}.{date_info['month']:02d}.{date_info['year']}\n"
                    f"⏰ **Время:** {selected_time}\n"
                    f"💰 **Тип аренды:** {rent_type}"
                )
                if rent_type == "почасовая":
                    confirmation_text += f"\n⏱ **Количество часов:** {hours_text}"
                confirmation_text += f"\n🏗 **Сборка рабочего места:** {'Собрать и разобрать' if workplace_setup == 'setup' else 'Самостоятельная'}"
                confirmation_text += f"\n💵 **Стоимость:** {price}р"
                confirmation_text += f"\n\n📊 [Посмотреть расписание](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?usp=sharing)"
                await query.message.reply_text(confirmation_text, 
                                             parse_mode=ParseMode.MARKDOWN, 
                                             disable_web_page_preview=True,
                                             reply_markup=get_after_booking_keyboard())
                break
            except APIError as e:
                if e.response.status_code == 429:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"Quota exceeded in process_rent_booking_final for {date_header}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"API error in process_rent_booking_final: {e}")
                    await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)
                    return
            except Exception as e:
                logger.error(f"Error in process_rent_booking_final: {e}")
                await query.answer("Ошибка при записи!", show_alert=True)
                return
    except Exception as e:
        logger.error(f"Unexpected error in process_rent_booking_final: {e}")
        await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)
    finally:
        await loading_message.delete()

async def show_user_bookings_for_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loading_message = await update.message.reply_text("⏳ Ищу ваши бронирования...")
    try:
        all_bookings = await get_user_bookings(user_id)
        if not all_bookings:
            await update.message.reply_text(
                "У вас нет активных бронирований за последние 30 дней.",
                reply_markup=rent_reply_keyboard(context),
            )
            return
        keyboard = []
        for booking in all_bookings:
            btn_text = f"{booking['date']} {booking['time']}"
            callback_data = f"cancel_{booking['worksheet']}_{booking['row']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Выберите бронирование для отмены:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error showing user bookings: {e}")
        await update.message.reply_text(
            "❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.",
            reply_markup=rent_reply_keyboard(context),
        )
    finally:
        await loading_message.delete()


async def show_user_bookings_for_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loading_message = await update.message.reply_text("⏳ Ищу ваши бронирования...")
    try:
        all_bookings = await get_user_bookings(user_id)
        if not all_bookings:
            await update.message.reply_text(
                "У вас нет активных бронирований за последние 30 дней.",
                reply_markup=rent_reply_keyboard(context),
            )
            return
        keyboard = []
        for booking in all_bookings:
            btn_text = f"{booking['date']} {booking['time']}"
            callback_data = f"reschedule_{booking['worksheet']}_{booking['row']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Выберите бронирование для переноса:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error showing user bookings for reschedule: {e}")
        await update.message.reply_text(
            "❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.",
            reply_markup=rent_reply_keyboard(context),
        )
    finally:
        await loading_message.delete()


async def handle_booking_reschedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("reschedule_"):
        return
    parts = query.data.split("_", 2)
    if len(parts) < 3:
        return
    worksheet_name, row_s = parts[1], parts[2]
    try:
        row = int(row_s)
    except ValueError:
        await query.answer("Ошибка данных.", show_alert=True)
        return
    context.user_data["reschedule_old"] = {"worksheet": worksheet_name, "row": row}
    context.user_data["rent_flow"] = "reschedule"
    today = datetime.date.today()
    context.user_data["calendar_year"] = today.year
    context.user_data["calendar_month"] = today.month
    keyboard = generate_calendar_keyboard(today.year, today.month)
    await query.message.reply_text(_calendar_title(context), reply_markup=keyboard)

async def handle_booking_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("cancel_"):
        _, worksheet_name, row = query.data.split("_")
        row = int(row)
        loading_message = await query.message.reply_text("⏳ Отменяю бронирование...")
        try:
            worksheet = await get_worksheet_cached(worksheet_name)
            if not worksheet:
                await query.answer("Ошибка доступа к расписанию!", show_alert=True)
                return
            for attempt in range(MAX_RETRIES):
                try:
                    row_data = await asyncio.to_thread(worksheet.row_values, row)
                    date_info = "Неизвестная дата"
                    for i in range(max(row-10, 1), row):
                        val_cell = await asyncio.to_thread(worksheet.cell, i, 1)
                        val = val_cell.value
                        if val and re.match(r'^\d{1,2}\s', val):
                            date_info = val
                            break
                    master_name = row_data[0] if len(row_data) > 0 else ""
                    time_info = row_data[1] if len(row_data) > 1 else ""
                    cancel_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    canceled_data = f"{master_name} | {time_info} | отменена {cancel_time}"
                    await asyncio.to_thread(worksheet.update, f'A{row}:E{row}', 
                                          [["", "", "", "отменен", canceled_data]])
                    for cache_key in list(sheets_cache.keys()):
                        if cache_key.startswith(worksheet_name):
                            del sheets_cache[cache_key]
                    try:
                        day, month_name = date_info.split()
                        day = int(day)
                        month = next(k for k, v in RUSSIAN_MONTHS.items() if v.lower() == month_name.lower())
                        year = int(worksheet_name.split()[-1])
                        formatted_date = f"{day:02d}.{month:02d}.{year}"
                    except:
                        formatted_date = date_info
                    await query.message.reply_text(
                        f"✅ **Бронирование отменено!**\n\n"
                        f"📅 **Дата:** {formatted_date}\n"
                        f"❌ **Статус:** Отменено\n\n"
                        f"Место освобождено для других пользователей.\n\n"
                        f"Меню аренды:",
                        reply_markup=rent_reply_keyboard(context),
                    )
                    break
                except APIError as e:
                    if e.response.status_code == 429:
                        delay = BASE_RETRY_DELAY * (2 ** attempt)
                        logger.warning(f"Quota exceeded in handle_booking_cancellation for {worksheet_name}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"API error in handle_booking_cancellation: {e}")
                        await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)
                        return
                except Exception as e:
                    logger.error(f"Error in handle_booking_cancellation: {e}")
                    await query.answer("Ошибка при отмене! Попробуйте позже.", show_alert=True)
                    return
        except Exception as e:
            logger.error(f"Unexpected error in handle_booking_cancellation: {e}")
            await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)
        finally:
            await loading_message.delete()

async def handle_after_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "book_another":
        context.user_data.pop("rent_flow", None)
        context.user_data.pop("reschedule_old", None)
        await show_calendar(update, context)
    elif query.data == "back_to_menu":
        context.user_data["state"] = "rent_submenu"
        await query.message.reply_text("Аренда.", reply_markup=get_rent_submenu_keyboard())

# =================================================================================
# --- RENT PAYMENT & BALANCE ---
# =================================================================================

async def show_user_bookings_for_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loading_message = await update.message.reply_text("⏳ Ищу ваши бронирования...")
    try:
        all_bookings = await get_user_bookings_for_payment(user_id)
        if not all_bookings:
            await update.message.reply_text(
                "У вас нет активных неоплаченных бронирований за последние 30 дней.",
                reply_markup=rent_reply_keyboard(context),
            )
            return
        keyboard = []
        for booking in all_bookings:
            if booking['rent_type'] == 'почасовая':
                btn_text = f"{booking['date']} {booking['time']}"
                callback_data = f"pay_hourly_{booking['worksheet']}_{booking['row']}"
            else:
                btn_text = f"{booking['date']} {booking['time']} - 3000р"
                callback_data = f"pay_full_{booking['worksheet']}_{booking['row']}_3000"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Выберите бронирование для оплаты:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error showing user bookings for payment: {e}")
        await update.message.reply_text(
            "❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.",
            reply_markup=rent_reply_keyboard(context),
        )
    finally:
        await loading_message.delete()

async def handle_payment_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("pay_full_"):
        _, _, worksheet_name, row, price = query.data.split("_")
        row = int(row)
        price = int(price)
        context.user_data['current_payment'] = {
            'worksheet': worksheet_name,
            'row': row,
            'price': price,
            'type': 'full'
        }
        payment_text = (
            f"💳 **Покупка предоплаченной аренды**\n\n"
            f"📅 **Тип аренды:** Фулл день\n"
            f"💰 **Сумма:** {price}р\n\n"
            f"📱 **Оплата по номеру телефона Т-Банк!**\n"
            f"`{PAYMENT_PHONE_NUMBER}`\n\n"
            f"⚠️ **Если не проходит оплата**, напишите сюда: {PAYMENT_CONTACT}, "
            f"будут выданы новые реквизиты.\n\n"
            f"📄 **После оплаты пришлите чек (фото или PDF) в чат и нажмите кнопку 'Оплатил'.**"
        )
        await query.edit_message_text(
            payment_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_inline_confirmation_keyboard()
        )
        context.user_data['state'] = 'rent_waiting_for_receipt'
    elif query.data.startswith("pay_hourly_"):
        _, _, worksheet_name, row = query.data.split("_")
        row = int(row)
        context.user_data['current_payment'] = {
            'worksheet': worksheet_name,
            'row': row,
            'type': 'hourly'
        }
        keyboard = [
            [InlineKeyboardButton("2 часа - 1300р", callback_data="pay_hours_2_1300")],
            [InlineKeyboardButton("3 часа - 1950р", callback_data="pay_hours_3_1950")],
            [InlineKeyboardButton("4 часа и более - 2500р", callback_data="pay_hours_4_2500")]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "💳 **Оплата почасовой аренды**\n\n"
            "Выберите количество часов для оплаты:",
            reply_markup=markup
        )

async def handle_hours_payment_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("pay_hours_"):
        _, _, hours, price = query.data.split("_")
        hours = int(hours)
        price = int(price)
        if 'current_payment' in context.user_data:
            context.user_data['current_payment']['hours'] = hours
            context.user_data['current_payment']['price'] = price
        hours_text = f"{hours} часа" if hours == 2 or hours == 3 else "4 часа и более"
        payment_text = (
            f"💳 **Покупка предоплаченной аренды**\n\n"
            f"📅 **Тип аренды:** Почасовая\n"
            f"⏱ **Количество часов:** {hours_text}\n"
            f"💰 **Сумма:** {price}р\n\n"
            f"📱 **Оплата по номеру телефона Т-Банк!**\n"
            f"`{PAYMENT_PHONE_NUMBER}`\n\n"
            f"⚠️ **Если не проходит оплата**, напишите сюда: {PAYMENT_CONTACT}, "
            f"будут выданы новые реквизиты.\n\n"
            f"📄 **После оплаты пришлите чек (фото или PDF) в чат и нажмите кнопку 'Оплатил'.**"
        )
        await query.edit_message_text(
            payment_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_payment_inline_confirmation_keyboard()
        )
        context.user_data['state'] = 'rent_waiting_for_receipt'

async def handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "payment_done":
        if 'receipt_uploaded' not in context.user_data or not context.user_data['receipt_uploaded']:
            await query.message.reply_text(
                "❌ Сначала загрузите фото или PDF чека, затем нажмите 'Оплатил'!",
                reply_markup=get_payment_inline_confirmation_keyboard()
            )
            return
        payment_info = context.user_data.get('current_payment', {})
        if not payment_info:
            await query.answer("❌ Информация о платеже не найдена!", show_alert=True)
            return
        try:
            worksheet = await get_worksheet_cached(payment_info['worksheet'])
            if not worksheet:
                await query.answer("❌ Ошибка доступа к расписанию!", show_alert=True)
                return
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.to_thread(worksheet.update_cell, payment_info['row'], 3, "оплачено")
                    context.user_data.pop('receipt_uploaded', None)
                    context.user_data.pop('current_payment', None)
                    caption = (
                        "✅ **Оплата подтверждена!**\n\n"
                        "Спасибо за оплату! Ваша аренда успешно оплачена.\n\n"
                        "Возвращаюсь в меню аренды:"
                    )
                    await send_success_gif(context, query.message.chat_id, caption, get_main_menu_keyboard())
                    context.user_data['state'] = 'main_menu'
                    break
                except APIError as e:
                    if e.response.status_code == 429:
                        delay = BASE_RETRY_DELAY * (2 ** attempt)
                        logger.warning(f"Quota exceeded in handle_payment_confirmation for {payment_info['worksheet']}. Retrying in {delay} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"API error in handle_payment_confirmation: {e}")
                        await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)
                        return
                except Exception as e:
                    logger.error(f"Error in handle_payment_confirmation: {e}")
                    await query.answer("❌ Ошибка при подтверждении оплаты!", show_alert=True)
                    return
        except Exception as e:
            logger.error(f"Unexpected error in handle_payment_confirmation: {e}")
            await query.answer("❌ Временная ошибка API Google Sheets. Попробуйте снова через минуту.", show_alert=True)

async def handle_rent_receipt_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.username or "N/A"
    try:
        file = None
        file_extension = None
        if update.message.document:
            file = await update.message.document.get_file()
            file_extension = update.message.document.file_name.split('.')[-1].lower() if update.message.document.file_name else 'pdf'
            if file_extension not in ['pdf', 'jpg', 'jpeg', 'png']:
                await update.message.reply_text("❌ Пожалуйста, отправьте файл в формате PDF, JPG, JPEG или PNG.")
                return
        elif update.message.photo:
            file = await update.message.photo[-1].get_file()
            file_extension = 'jpg'
        else:
            await update.message.reply_text("❌ Пожалуйста, отправьте фото или PDF документ чека.")
            return
        file_bytes = await file.download_as_bytearray()
        caption = f"📄 Чек от пользователя @{user_name} (ID: {user_id})"
        if update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=file.file_id,
                caption=caption
            )
        else:
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=file.file_id,
                caption=caption
            )
        context.user_data['receipt_uploaded'] = True
        context.user_data['state'] = 'rent_waiting_for_receipt'
        await update.message.reply_text(
            "✅ Чек успешно загружен! Теперь вы можете нажать кнопку 'Оплатил' для подтверждения оплаты.",
            reply_markup=get_payment_inline_confirmation_keyboard()
        )
    except Exception as e:
        logger.error(f"Error processing receipt: {e}")
        await update.message.reply_text("❌ Ошибка при обработке чека. Попробуйте еще раз.")

# =================================================================================
# --- BACKGROUND TASKS & BOT LAUNCH ---
# =================================================================================

async def create_monthly_sheets_job():
    logger.info("Running scheduled job: create_monthly_sheets_job")
    sheet = get_spreadsheet()
    if not sheet:
        logger.error("Spreadsheet client not available. Skipping job.")
        return
    try:
        today = datetime.date.today()
        for i in range(2):
            target_date = today if i == 0 else (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            name = f"{RUSSIAN_MONTHS[target_date.month]} {target_date.year}"
            try:
                await asyncio.to_thread(sheet.worksheet, name)
            except WorksheetNotFound:
                logger.info(f"Creating sheet '{name}'...")
                await asyncio.to_thread(sheet.add_worksheet, title=name, rows=300, cols=10)
        logger.info("Monthly sheets check complete.")
    except Exception as e:
        logger.error(f"Error in create_monthly_sheets_job: {e}")

async def background_scheduler():
    logger.info("Background scheduler started.")
    while True:
        now = datetime.datetime.now()
        next_run = (now.replace(hour=3, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1))
        wait_seconds = (next_run - now).total_seconds()
        logger.info(f"Scheduler will run next job in {wait_seconds / 3600:.2f} hours.")
        await asyncio.sleep(wait_seconds)
        await create_monthly_sheets_job()

async def post_init(application: Application) -> None:
    """Runs after the application has been initialized."""
    application.bot_data['http_client'] = httpx.AsyncClient()
    asyncio.create_task(background_scheduler())

async def on_shutdown(application: Application) -> None:
    """Runs before the application shuts down."""
    http_client = application.bot_data.get('http_client')
    if http_client:
        await http_client.aclose()
        logger.info("HTTP client successfully closed.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "Telegram Conflict: для этого токена уже идёт getUpdates в другом процессе. "
            "Остановите второй экземпляр бота (другой терминал, сервер, тест с тем же токеном)."
        )
        return
    if isinstance(err, BadRequest):
        logger.warning("Telegram BadRequest while handling update: %s", err)
        return
    if isinstance(err, (TimedOut, NetworkError)) or _is_transient_network_error(err):
        logger.warning("Telegram transient network error while handling update: %s", err)
        if update and update.effective_chat:
            try:
                if update.message and (update.message.text or "").startswith("/start"):
                    await _send_main_welcome(update, context)
                else:
                    await _bot_send_with_retry(
                        lambda: context.bot.send_message(
                            update.effective_chat.id,
                            "Связь с Telegram на секунду подвисла — я на связи. Повторите команду или выберите пункт меню.",
                            reply_markup=get_main_menu_keyboard(),
                        )
                    )
            except Exception as recovery_err:
                logger.warning("network recovery message failed: %s", recovery_err)
        return
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    if update and update.effective_user:
        try:
            await send_disappointment_gif(
                context,
                update.effective_user.id,
                "Ой, что-то пошло не так с моей стороны… Попробуйте чуть позже — я обязательно всё уладю 💕",
                get_main_menu_keyboard(),
            )
        except Exception as e:
            logger.error(f"Error sending error message: {e}")

def main() -> None:
    _validate_required_config()

    os.makedirs(os.path.join(SCRIPT_DIR, GIFS_DIR), exist_ok=True)
    for directory in [ANIME_DIR, TRIBAL_DIR, OTHER_DIR, MERCH_PHOTOS_DIR]:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info(f"Created directory: {directory}")

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    tg_request = _telegram_http_request()
    updates_request = _telegram_http_request(read_timeout=35.0, media_write_timeout=90.0)

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(tg_request)
        .get_updates_request(updates_request)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, route_media))
    
    # Rent callback handlers
    application.add_handler(CallbackQueryHandler(handle_calendar_callback, pattern="^(nav_|date_|back_to_menu|ignore)"))
    application.add_handler(CallbackQueryHandler(handle_time_selection, pattern="^(time_|back_to_dates)"))
    application.add_handler(CallbackQueryHandler(handle_rent_supply_selection, pattern="^supply_"))
    application.add_handler(CallbackQueryHandler(handle_after_booking, pattern="^(book_another|back_to_menu)"))
    application.add_handler(CallbackQueryHandler(handle_booking_reschedule_request, pattern="^(reschedule_)"))
    application.add_handler(
        CallbackQueryHandler(
            handle_casino_hub_callback,
            pattern=r"^ch_(hub|rent|rentbook|tattoo|tpay|close|feed|lotroot|tro|trn|tra|bonus)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_casino_round_callback,
            pattern=r"^(crs_[tona]|crs_r_\d{4}_\d{1,2}_\d+|csp_\d{1,3}|csgo|csrand|cscng|cscan|csnoop|csfair|cswnow|cswlater)$",
        )
    )
    application.add_handler(CallbackQueryHandler(handle_booking_cancellation, pattern="^(cancel_)"))
    application.add_handler(CallbackQueryHandler(handle_payment_selection, pattern="^(pay_full_|pay_hourly_)"))
    application.add_handler(CallbackQueryHandler(handle_hours_payment_selection, pattern="^(pay_hours_)"))
    application.add_handler(CallbackQueryHandler(handle_payment_confirmation, pattern="^(payment_done)"))
    
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    # Python 3.12+: в MainThread нет «текущего» цикла по умолчанию; PTB внутри вызывает
    # asyncio.get_event_loop() и падает без явной установки (см. telegram.ext.Application.__run).
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
