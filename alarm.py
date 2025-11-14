#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, Optional, List

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# ================== НАСТРОЙКИ ==================

TELEGRAM_BOT_TOKEN = "8584144757:AAGPx65JAtgudJe6bQHFlP1w8Drwqou4Bh4"
ALERTS_API_TOKEN = "5c0c5851392c79033d1a99993d45063d60b22506ab2203"

DB_PATH = "users.db"
CHECK_INTERVAL_SECONDS = 10  # интервал проверки API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

# ================== МОДЕЛИ / КОНФИГ ==================


@dataclass(frozen=True)
class Oblast:
    key: str         # внутренний ключ для callback_data
    title: str       # отображаемое название
    index: int       # индекс в строке /v1/iot/active_air_raid_alerts_by_oblast.json


# Порядок и индексы ОБЯЗАТЕЛЬНО совпадают с документацией alerts.in.ua
# Мы исключаем Крым (index 0) и м. Севастополь (index 18), чтобы оставить 25 регіонів.
OBLASTS: List[Oblast] = [
    Oblast("volyn", "Волинська область", 1),
    Oblast("vinnytsia", "Вінницька область", 2),
    Oblast("dnipro", "Дніпропетровська область", 3),
    Oblast("donetsk", "Донецька область", 4),
    Oblast("zhytomyr", "Житомирська область", 5),
    Oblast("zakarpattia", "Закарпатська область", 6),
    Oblast("zaporizhzhia", "Запорізька область", 7),
    Oblast("ivano_frankivsk", "Івано-Франківська область", 8),
    Oblast("kyiv_city", "м. Київ", 9),
    Oblast("kyiv", "Київська область", 10),
    Oblast("kirovohrad", "Кіровоградська область", 11),
    Oblast("luhansk", "Луганська область", 12),
    Oblast("lviv", "Львівська область", 13),
    Oblast("mykolaiv", "Миколаївська область", 14),
    Oblast("odesa", "Одеська область", 15),
    Oblast("poltava", "Полтавська область", 16),
    Oblast("rivne", "Рівненська область", 17),
    # index 18 = м. Севастополь (пропускаем)
    Oblast("sumy", "Сумська область", 19),
    Oblast("ternopil", "Тернопільська область", 20),
    Oblast("kharkiv", "Харківська область", 21),
    Oblast("kherson", "Херсонська область", 22),
    Oblast("khmelnytskyi", "Хмельницька область", 23),
    Oblast("cherkasy", "Черкаська область", 24),
    Oblast("chernivtsi", "Чернівецька область", 25),
    Oblast("chernihiv", "Чернігівська область", 26),
]

KEY_TO_OBLAST: Dict[str, Oblast] = {o.key: o for o in OBLASTS}
INDEX_TO_OBLAST: Dict[int, Oblast] = {o.index: o for o in OBLASTS}

# last known statuses для каждого index (символ 'A','P','N')
last_oblast_statuses: Dict[int, str] = {}


# ================== РАБОТА С БАЗОЙ ==================


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id             INTEGER PRIMARY KEY,
                region_index        INTEGER,
                notifications_enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.commit()


async def get_or_create_user(user_id: int) -> Dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, region_index, notifications_enabled FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row:
            return {
                "user_id": row["user_id"],
                "region_index": row["region_index"],
                "notifications_enabled": bool(row["notifications_enabled"]),
            }

        await db.execute(
            "INSERT INTO users (user_id, region_index, notifications_enabled) VALUES (?, NULL, 1)",
            (user_id,),
        )
        await db.commit()
        return {"user_id": user_id, "region_index": None, "notifications_enabled": True}


async def set_user_region(user_id: int, region_index: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET region_index = ? WHERE user_id = ?",
            (region_index, user_id),
        )
        await db.commit()


async def toggle_user_notifications(user_id: int) -> bool:
    """
    Переключает уведомления и возвращает новое состояние (True = Включеннi).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT notifications_enabled FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            # если вдруг пользователя нет — создаём с дефолтами
            await db.execute(
                "INSERT INTO users (user_id, region_index, notifications_enabled) VALUES (?, NULL, 1)",
                (user_id,),
            )
            await db.commit()
            return True

        current = bool(row["notifications_enabled"])
        new_value = 0 if current else 1
        await db.execute(
            "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
            (new_value, user_id),
        )
        await db.commit()
        return not current


async def get_users_for_region(region_index: int) -> List[int]:
    """
    Возвращает user_id всех, кто подписан на область и у кого включены уведомления.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id FROM users WHERE region_index = ? AND notifications_enabled = 1",
            (region_index,),
        )
        users = [row["user_id"] async for row in cur]
        return users


# ================== КЛАВИАТУРЫ ==================


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="📍Перевiрити тривогу")],
            [KeyboardButton(text="⚙️Налаштування")],
        ],
    )


def build_oblasts_inline_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """
    prefix нужен, чтобы различать выбор региона из /start и из настроек,
    например: 'start_region:' или 'settings_region:'.
    """
    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []

    for oblast in OBLASTS:
        btn = InlineKeyboardButton(
            text=oblast.title,
            callback_data=f"{prefix}{oblast.key}",
        )
        row.append(btn)
        # делаем по 2-3 кнопки в ряд, чтобы не было совсем кишки
        if len(row) == 3:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_start_status_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛎Перевiрити статус", callback_data="check_status")]
        ]
    )


def build_settings_inline_keyboard(
    notifications_enabled: bool,
) -> InlineKeyboardMarkup:
    notif_status = "Включеннi" if notifications_enabled else "Вимкнено"
    notif_button_text = f"🔔Повiдомлення: {notif_status}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍Змiнити область", callback_data="change_region")],
            [
                InlineKeyboardButton(
                    text=notif_button_text,
                    callback_data="toggle_notifications",
                )
            ],
        ]
    )


# ================== РАБОТА С API alerts.in.ua ==================


async def fetch_oblast_statuses_string() -> Optional[str]:
    """
    Возвращает строку вида "ANNNNNNN..." длиной 27 символов,
    где каждый символ — статус области (A/P/N).
    docs: /v1/iot/active_air_raid_alerts_by_oblast.json
    """
    url = "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json"

    headers = {
        "Authorization": f"Bearer {ALERTS_API_TOKEN}",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logging.error(f"API error {resp.status}: {text}")
                    return None

                raw = await resp.text()
                # это JSON-строка, а не просто текст → парсим
                statuses = json.loads(raw)
                if not isinstance(statuses, str):
                    logging.error("Unexpected API response format (not a string)")
                    return None

                return statuses
    except Exception as e:
        logging.error(f"Exception while fetching alerts: {e}")
        return None


async def get_region_status_char(region_index: int) -> Optional[str]:
    """
    Возвращает символ 'A', 'P' или 'N' для конкретного region_index.
    """
    statuses = await fetch_oblast_statuses_string()
    if not statuses:
        return None

    if region_index < 0 or region_index >= len(statuses):
        logging.error(f"Region index {region_index} out of range for statuses string")
        return None

    return statuses[region_index]


def build_manual_status_message(region_index: int, code: Optional[str]) -> str:
    oblast = INDEX_TO_OBLAST.get(region_index)
    region_name = oblast.title if oblast else "область"

    if code is None:
        return (
            "⚠️ Не вдалось отримати статус тривоги.\n"
            "Спробуйте ще раз трохи пізніше."
        )

    # Только A = тревога
    if code == "A":
        return (
            f"🚨 Повiтряна тривога в областi {region_name}.\n"
            f"‼️ Негайно прямуйте до укриття!"
        )

    # P и N оба считаются как NO ALERT
    if code in ("P", "N"):
        return (
            f"✅ Зараз у областi {region_name} немає повiтряної тривоги.\n"
            f"Залишайтеся пильними."
        )

    return (
        f"⚠️ Незнаний статус тривоги ({code}) для областi {region_name}.\n"
        f"Можливо, тимчасова помилка сервісу."
    )


# ================== ХЕНДЛЕРЫ БОТА ==================


@dp.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    user = await get_or_create_user(message.from_user.id)

    text = "📡Оберiть область, яку будете вiдстежувати:"
    await message.answer(
        text,
        reply_markup=main_reply_keyboard(),
    )

    await message.answer(
        text,
        reply_markup=build_oblasts_inline_keyboard(prefix="start_region:"),
    )


@dp.callback_query(F.data.startswith("start_region:"))
async def callback_start_region(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    oblast = KEY_TO_OBLAST.get(key)
    if not oblast:
        await callback.answer("Невiдома область.", show_alert=True)
        return

    await set_user_region(callback.from_user.id, oblast.index)

    text = (
        f"🚧Бот вiдстежуэ тривоги в областi {oblast.title}.\n"
        f"Ви пiдписанi на повiдомлення"
    )

    await callback.message.edit_text(
        text,
        reply_markup=build_start_status_button(),
    )
    await callback.answer()  # просто закрыть "часики"


@dp.callback_query(F.data == "check_status")
async def callback_check_status(callback: CallbackQuery) -> None:
    user = await get_or_create_user(callback.from_user.id)
    region_index = user["region_index"]

    if region_index is None:
        await callback.answer(
            "Спочатку оберiть область у налаштуваннях або через /start.",
            show_alert=True,
        )
        return

    code = await get_region_status_char(region_index)
    text = build_manual_status_message(region_index, code)

    # Меняем текст сообщения, кнопка после этого исчезнет (как ты и писал)
    await callback.message.edit_text(text)
    await callback.answer()


@dp.message(F.text == "📍Перевiрити тривогу")
async def message_check_alert(message: types.Message) -> None:
    user = await get_or_create_user(message.from_user.id)
    region_index = user["region_index"]

    if region_index is None:
        await message.answer(
            "Спочатку оберiть область у налаштуваннях (кнопка ⚙️Налаштування) або через /start.",
            reply_markup=main_reply_keyboard(),
        )
        return

    code = await get_region_status_char(region_index)
    text = build_manual_status_message(region_index, code)
    await message.answer(text, reply_markup=main_reply_keyboard())


async def send_settings(chat_id: int) -> None:
    user = await get_or_create_user(chat_id)
    region_index = user["region_index"]
    notifications_enabled = user["notifications_enabled"]

    if region_index is None:
        region_text = "не вибрана"
    else:
        oblast = INDEX_TO_OBLAST.get(region_index)
        region_text = oblast.title if oblast else f"#{region_index}"

    notif_status = "Включеннi" if notifications_enabled else "Вимкнено"

    text = (
        "🎈Налаштування\n"
        f"🌐Область: {region_text}\n"
        f"🔔Повiдомлення: {notif_status}"
    )

    await bot.send_message(
        chat_id,
        text,
        reply_markup=build_settings_inline_keyboard(notifications_enabled),
    )


@dp.message(F.text == "⚙️Налаштування")
async def message_settings(message: types.Message) -> None:
    await send_settings(message.chat.id)


@dp.callback_query(F.data == "change_region")
async def callback_change_region(callback: CallbackQuery) -> None:
    text = "💙💛Оберiть область яку будете вiдстежувати:"

    await callback.message.edit_text(
        text,
        reply_markup=build_oblasts_inline_keyboard(prefix="settings_region:"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("settings_region:"))
async def callback_settings_region(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    oblast = KEY_TO_OBLAST.get(key)

    if not oblast:
        await callback.answer("Невiдома область.", show_alert=True)
        return

    await set_user_region(callback.from_user.id, oblast.index)

    # 🔔 мини уведомление
    await callback.answer("Область змiнено!", show_alert=False)

    # ❗️Здесь раньше было edit_text — убираем полностью

    # ✔️ удаляем старое сообщение (где были кнопки областей)
    try:
        await callback.message.delete()
    except:
        pass

    # ✔️ отправляем НОВОЕ сообщение "Налаштування"
    await send_settings(callback.from_user.id)


@dp.callback_query(F.data == "toggle_notifications")
async def callback_toggle_notifications(callback: CallbackQuery) -> None:
    new_state = await toggle_user_notifications(callback.from_user.id)
    # new_state == True → включены
    status_text = "Включеннi" if new_state else "Вимкнено"

    # Обновляем клавиатуру у того же сообщения
    user = await get_or_create_user(callback.from_user.id)
    new_kb = build_settings_inline_keyboard(new_state)

    # Меняем только клавиатуру, текст оставляем
    await callback.message.edit_reply_markup(reply_markup=new_kb)

    await callback.answer(
        "Повiдомлення увiмкнено ✅" if new_state else "Повiдомлення вимкнено 🔕",
        show_alert=False,
    )


# ================== ФОНОВЫЙ МОНИТОРИНГ ТРИВОГ ==================


async def alerts_monitor():
    global last_oblast_statuses

    # инициализация: чтобы не слать уведомления при первом запуске
    statuses = await fetch_oblast_statuses_string()
    if statuses:
        for idx, ch in enumerate(statuses):
            last_oblast_statuses[idx] = ch

    logging.info("Alerts monitor started")

    while True:
        try:
            statuses = await fetch_oblast_statuses_string()
            if not statuses:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # проходим по всем индексам, которые нас интересуют (те, что есть в INDEX_TO_OBLAST)
            for region_index, oblast in INDEX_TO_OBLAST.items():
                code = statuses[region_index] if region_index < len(statuses) else "N"
                prev_code = last_oblast_statuses.get(region_index)

                # Обновляем last_oblast_statuses, но сначала проверяем переходы
                if prev_code is None:
                    last_oblast_statuses[region_index] = code
                    continue

                # Переход "нет тревоги → тревога" (A или P)
                if prev_code in ("N",) and code in ("A", "P"):
                    users = await get_users_for_region(region_index)
                    if users:
                        text = (
                            f"🚨😔Тривога в областi {oblast.title}\n"
                            f"‼️В УКРИТТЯ!"
                        )
                        for uid in users:
                            try:
                                await bot.send_message(uid, text)
                            except Exception as e:
                                logging.warning(
                                    f"Failed to send alert start to {uid}: {e}"
                                )

                # Переход "тревога → нет тревоги"
                if prev_code in ("A", "P") and code == "N":
                    users = await get_users_for_region(region_index)
                    if users:
                        text = (
                            "✅😁Вiдбiй тривоги\n"
                            "🇺🇦 Слава Україні!"
                        )
                        for uid in users:
                            try:
                                await bot.send_message(uid, text)
                            except Exception as e:
                                logging.warning(
                                    f"Failed to send alert end to {uid}: {e}"
                                )

                # Обновляем последнее значение
                last_oblast_statuses[region_index] = code

        except Exception as e:
            logging.error(f"Error in alerts_monitor: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ================== MAIN ==================


async def main():
    await init_db()
    # запускаем фоновый монитор
    asyncio.create_task(alerts_monitor())
    # запускаем поллинг бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")