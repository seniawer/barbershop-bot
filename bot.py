import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
    WebAppInfo,
)
from dotenv import load_dotenv

import db

dp = Dispatcher()

# Главное меню: кнопки под полем ввода сообщения
CLIENT_MENU_ROWS = [
    [KeyboardButton(text="Записаться"), KeyboardButton(text="Записаться онлайн")],
    [KeyboardButton(text="Мои записи"), KeyboardButton(text="Услуги и цены")],
]
main_menu = ReplyKeyboardMarkup(keyboard=CLIENT_MENU_ROWS, resize_keyboard=True)
# То же меню для администратора — с кнопкой «Меню мастера»
admin_main_menu = ReplyKeyboardMarkup(
    keyboard=[*CLIENT_MENU_ROWS, [KeyboardButton(text="Меню мастера")]],
    resize_keyboard=True,
)

# Меню мастера (только для администратора)
master_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Записи на сегодня"), KeyboardButton(text="На завтра")],
        [KeyboardButton(text="На неделю")],
        [KeyboardButton(text="Закрыть время"), KeyboardButton(text="Закрытое время")],
        [KeyboardButton(text="Клиентское меню")],
    ],
    resize_keyboard=True,
)

# Услуги и цены (пока прямо в коде): код услуги -> (название, цена в рублях)
SERVICES = {
    "haircut": ("Мужская стрижка", 1500),
    "beard": ("Стрижка бороды", 800),
    "combo": ("Стрижка + борода", 2000),
    "kids": ("Детская стрижка", 1000),
    "buzz": ("Стрижка машинкой", 700),
}

# Сколько дней вперёд можно записаться и в какие часы работает барбершоп
DAYS_AHEAD = 7
WORK_HOURS = range(10, 20)  # с 10:00, последняя запись в 19:00
WORK_TIMES = [f"{hour:02d}:00" for hour in WORK_HOURS]
# На сколько дней вперёд мастер может закрыть время
CLOSE_DAYS_AHEAD = 14

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Адрес мини-приложения (Telegram Mini App) с онлайн-записью. Страница публичная
MINIAPP_URL = "https://seniawer.github.io/barbershop-miniapp/"

# Telegram ID администратора из .env (задаётся в main).
# None — уведомления не отправляются и меню мастера недоступно
ADMIN_ID: int | None = None


def is_admin(user: User | None) -> bool:
    return ADMIN_ID is not None and user is not None and user.id == ADMIN_ID


class IsAdmin(Filter):
    """Пропускает сообщение или нажатие кнопки, только если его прислал администратор.
    Стоит на каждом обработчике меню мастера: скрытой кнопки мало, текст «Меню мастера»
    или callback_data «adm:...» может отправить кто угодно."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return is_admin(event.from_user)


def menu_for(user: User | None) -> ReplyKeyboardMarkup:
    """Главное меню: у администратора в нём есть кнопка «Меню мастера»."""
    return admin_main_menu if is_admin(user) else main_menu


# Шаги записи. FSM запоминает, на каком из них сейчас клиент
class Booking(StatesGroup):
    service = State()
    date = State()
    time = State()


cancel_button = InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")


def closed_hours(day: date) -> set[str]:
    """Часы, которые мастер закрыл на этот день (если закрыт весь день — все рабочие часы)."""
    closed = db.closed_times(day.isoformat())
    if db.WHOLE_DAY in closed:
        return set(WORK_TIMES)
    return closed


def unavailable_times(day: date) -> set[str]:
    """Время, на которое записаться нельзя: занято другим клиентом или закрыто мастером."""
    return db.busy_times(day.isoformat()) | closed_hours(day)


def free_times(day: date) -> list[str]:
    """Свободное время на день: не занятое, не закрытое мастером и (для сегодня) ещё не прошедшее."""
    now = datetime.now()
    busy = unavailable_times(day)
    return [
        time
        for hour in WORK_HOURS
        if (day != now.date() or hour > now.hour)
        and (time := f"{hour:02d}:00") not in busy
    ]


def format_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def client_name(user: User) -> str:
    """Имя клиента для администратора: «Иван Петров (@ivan)»."""
    return format_client(user.full_name, user.username)


def format_client(name: str, username: str | None) -> str:
    if username:
        return f"{name} (@{username})"
    return name


async def notify_admin(bot: Bot, text: str) -> None:
    """Отправляет сообщение администратору, если ADMIN_ID задан."""
    if ADMIN_ID is None:
        return
    try:
        await bot.send_message(ADMIN_ID, text)
    except TelegramAPIError as error:
        # Например, админ ещё не нажал /start у бота. Клиенту это не мешает — только пишем в лог
        logging.warning("Не удалось уведомить администратора: %s", error)


def phone_line(phone: str | None) -> str:
    """Строка с телефоном клиента для мастера. Пустая, если клиент номером не поделился."""
    return f"\nТелефон: {phone}" if phone else ""


def booking_start_time(day: str | date, time: str) -> datetime:
    """Начало записи. Время — как и везде в боте, по часам компьютера (datetime.now())."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return datetime.combine(day, datetime.strptime(time, "%H:%M").time())


def reminders_to_skip(day: date, time: str) -> tuple[bool, bool]:
    """Какие напоминания не нужны новой записи: (за сутки, за 2 часа).
    До записи меньше 24 часов — за сутки не шлём, меньше 2 часов — не шлём никаких."""
    left = booking_start_time(day, time) - datetime.now()
    return left < timedelta(hours=24), left < timedelta(hours=2)


# Клавиатура с просьбой поделиться номером. request_contact отправляет только свой контакт
phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        [KeyboardButton(text="Пропустить")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


async def ask_phone_once(message: Message, user: User) -> None:
    """После первой записи просим номер телефона. Если уже спрашивали — ничего не делаем."""
    if not db.should_ask_phone(user.id):
        return
    await message.answer(
        "Оставьте, пожалуйста, номер телефона — мастер сможет связаться с вами, "
        "если что-то изменится. Номер спрашиваем один раз.",
        reply_markup=phone_keyboard,
    )


def miniapp_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой мини-приложения. Адрес собирается заново при каждом вызове,
    чтобы страница получила актуальный список занятых слотов: ?busy=2026-10-07_15:00,...
    Закрытое мастером время передаётся как занятое: для клиента разницы нет.
    Кнопка именно в reply-клавиатуре: из inline-кнопки Telegram.WebApp.sendData не работает."""
    today = date.today()
    busy = [
        f"{day.isoformat()}_{time}"
        for day in (today + timedelta(days=i) for i in range(DAYS_AHEAD))
        for time in sorted(unavailable_times(day))
    ]
    url = MINIAPP_URL + ("?busy=" + ",".join(busy) if busy else "")
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Открыть онлайн-запись", web_app=WebAppInfo(url=url))],
            [KeyboardButton(text="⬅️ Главное меню")],
        ],
        resize_keyboard=True,
    )


def services_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{name} — {price} ₽", callback_data=f"service:{code}")]
        for code, (name, price) in SERVICES.items()
    ]
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard() -> InlineKeyboardMarkup:
    today = date.today()
    days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    # Не показываем дни, где свободного времени не осталось (всё прошло или занято)
    buttons = [
        InlineKeyboardButton(text=format_date(day), callback_data=f"date:{day.isoformat()}")
        for day in days
        if free_times(day)
    ]
    # По 3 кнопки в ряд
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(day: date) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=time, callback_data=f"time:{time}")
        for time in free_times(day)
    ]
    # По 4 кнопки в ряд
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = message.from_user.first_name if message.from_user else "гость"
    await message.answer(
        f"Здравствуйте, {name}! 👋\n"
        "Я бот барбершопа. Помогу записаться на стрижку.\n"
        "Выберите действие в меню ниже.",
        reply_markup=menu_for(message.from_user),
    )


# /id — показывает пользователю его Telegram ID (нужен для ADMIN_ID в .env)
@dp.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(F.text == "Услуги и цены")
async def show_services(message: Message) -> None:
    lines = [f"• {name} — {price} ₽" for name, price in SERVICES.values()]
    await message.answer("💈 Наши услуги и цены:\n\n" + "\n".join(lines))


# Шаг 1: клиент нажал «Записаться» — предлагаем выбрать услугу
@dp.message(F.text == "Записаться")
async def booking_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=services_keyboard())


# Шаг 2: услуга выбрана — предлагаем дату
@dp.callback_query(Booking.service, F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.split(":", 1)[1]
    if code not in SERVICES:
        await callback.answer("Такой услуги нет", show_alert=True)
        return
    await state.update_data(service=code)
    await state.set_state(Booking.date)
    name, price = SERVICES[code]
    await callback.message.edit_text(
        f"Услуга: {name} — {price} ₽\n\nВыберите дату:",
        reply_markup=dates_keyboard(),
    )
    await callback.answer()


# Шаг 3: дата выбрана — предлагаем время
@dp.callback_query(Booking.date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    day = date.fromisoformat(callback.data.split(":", 1)[1])
    if not free_times(day):
        await callback.answer("На этот день свободного времени нет, выберите другой", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=dates_keyboard())
        return
    await state.update_data(date=day.isoformat())
    await state.set_state(Booking.time)
    data = await state.get_data()
    name, price = SERVICES[data["service"]]
    await callback.message.edit_text(
        f"Услуга: {name} — {price} ₽\n"
        f"Дата: {format_date(day)}\n\n"
        "Выберите время:",
        reply_markup=times_keyboard(day),
    )
    await callback.answer()


# Шаг 4: время выбрано — подтверждаем запись
@dp.callback_query(Booking.time, F.data.startswith("time:"))
async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
    time = callback.data.split(":", 1)[1]
    data = await state.get_data()
    day = date.fromisoformat(data["date"])
    # Пока клиент выбирал, это время мог занять кто-то другой — проверяем ещё раз.
    # add_booking тоже вернёт False, если кто-то успел записаться в ту же секунду.
    if time not in free_times(day) or not db.add_booking(
        callback.from_user.id,
        data["service"],
        data["date"],
        time,
        callback.from_user.full_name,
        callback.from_user.username,
        *reminders_to_skip(day, time),
    ):
        await callback.answer("Это время уже занято или прошло, выберите другое", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=times_keyboard(day))
        return
    name, price = SERVICES[data["service"]]
    await state.clear()
    await callback.message.edit_text(
        f"Вы записаны на {format_date(day)} в {time} ✅\n"
        f"Услуга: {name} — {price} ₽"
    )
    await callback.answer()
    await notify_admin(
        callback.bot,
        "🆕 Новая запись\n"
        f"Услуга: {name} — {price} ₽\n"
        f"Дата: {format_date(day)}\n"
        f"Время: {time}\n"
        f"Клиент: {client_name(callback.from_user)}"
        + phone_line(db.client_phone(callback.from_user.id)),
    )
    await ask_phone_once(callback.message, callback.from_user)


# Онлайн-запись: клиент нажал «Записаться онлайн» — даём кнопку, открывающую мини-приложение
@dp.message(F.text == "Записаться онлайн")
async def miniapp_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Нажмите кнопку ниже: откроется страница записи. "
        "Выберите услугу, день и время и подтвердите запись.",
        reply_markup=miniapp_keyboard(),
    )


@dp.message(F.text == "⬅️ Главное меню")
async def back_to_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню.", reply_markup=menu_for(message.from_user))


def parse_miniapp_booking(raw: str) -> tuple[str, date, str] | None:
    """Проверяет данные из мини-приложения и возвращает (код услуги, дата, время).
    None — если данные неверные. Странице не доверяем: её JSON может подделать кто угодно."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    service, day, time = data.get("service"), data.get("date"), data.get("time")
    if not all(isinstance(value, str) for value in (service, day, time)):
        return None

    # Страница присылает название услуги («Мужская стрижка»). Ищем его в SERVICES.
    # Цену из JSON не используем вовсе — она всегда берётся из SERVICES
    code = next((code for code, (name, _) in SERVICES.items() if name == service), None)
    if code is None:
        return None

    # Дата строго ГГГГ-ММ-ДД и в пределах ближайших DAYS_AHEAD дней (начиная с сегодня)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    try:
        day = date.fromisoformat(day)
    except ValueError:
        return None
    if not 0 <= (day - date.today()).days < DAYS_AHEAD:
        return None

    # Время строго ЧЧ:00 и в рабочие часы
    match = re.fullmatch(r"(\d{2}):00", time)
    if match is None or int(match.group(1)) not in WORK_HOURS:
        return None
    return code, day, time


# Мини-приложение прислало выбор клиента (Telegram.WebApp.sendData)
@dp.message(F.web_app_data)
async def miniapp_booking(message: Message, state: FSMContext) -> None:
    await state.clear()
    booking = parse_miniapp_booking(message.web_app_data.data)
    if booking is None:
        await message.answer(
            "Не получилось разобрать запись. Попробуйте ещё раз.",
            reply_markup=miniapp_keyboard(),
        )
        return
    code, day, time = booking
    # Как и в пошаговой записи: время не должно пройти или быть занятым,
    # а add_booking вернёт False, если кто-то успел записаться в ту же секунду
    if time not in free_times(day) or not db.add_booking(
        message.from_user.id,
        code,
        day.isoformat(),
        time,
        message.from_user.full_name,
        message.from_user.username,
        *reminders_to_skip(day, time),
    ):
        await message.answer(
            "Это время уже занято или прошло 😔 Выберите другое — список свободного времени обновлён.",
            reply_markup=miniapp_keyboard(),
        )
        return
    name, price = SERVICES[code]
    await message.answer(
        f"Вы записаны на {format_date(day)} в {time} ✅\n"
        f"Услуга: {name} — {price} ₽",
        reply_markup=menu_for(message.from_user),
    )
    await notify_admin(
        message.bot,
        "🆕 Новая запись (онлайн)\n"
        f"Услуга: {name} — {price} ₽\n"
        f"Дата: {format_date(day)}\n"
        f"Время: {time}\n"
        f"Клиент: {client_name(message.from_user)}"
        + phone_line(db.client_phone(message.from_user.id)),
    )
    await ask_phone_once(message, message.from_user)


# Клиент нажал «Поделиться номером». Принимаем только его собственный контакт:
# переслать чужой контакт кнопкой нельзя, но можно вручную через «Прикрепить»
@dp.message(F.contact)
async def save_contact(message: Message) -> None:
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Пожалуйста, отправьте свой номер кнопкой «📱 Поделиться номером».",
            reply_markup=phone_keyboard,
        )
        return
    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    db.save_phone(message.from_user.id, phone)
    await message.answer("Спасибо, номер сохранён ✅", reply_markup=menu_for(message.from_user))
    await notify_admin(
        message.bot,
        f"📱 Клиент {client_name(message.from_user)} оставил номер: {phone}",
    )


@dp.message(F.text == "Пропустить")
async def skip_phone(message: Message) -> None:
    await message.answer(
        "Хорошо, без номера. Главное меню.", reply_markup=menu_for(message.from_user)
    )


@dp.callback_query(F.data == "cancel")
async def cancel_booking(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Запись отменена.")
    await callback.answer()


@dp.message(F.text == "Мои записи")
async def my_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Прошедшие дни не показываем — только сегодняшние и будущие записи
    user_bookings = db.user_bookings(message.from_user.id, date.today().isoformat())
    if not user_bookings:
        await message.answer("У вас пока нет записей. Нажмите «Записаться», чтобы выбрать время.")
        return
    await message.answer("📋 Ваши записи:")
    # Каждую запись отдельным сообщением, чтобы кнопка отмены была прямо под ней
    for booking in user_bookings:
        name, price = SERVICES[booking["service"]]
        day = date.fromisoformat(booking["date"])
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отменить запись", callback_data=f"unbook:{booking['id']}")]
            ]
        )
        await message.answer(
            f"{name} — {price} ₽\n{format_date(day)} в {booking['time']}",
            reply_markup=keyboard,
        )


@dp.callback_query(F.data.startswith("unbook:"))
async def unbook(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":", 1)[1])
    booking = db.delete_booking(booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, _ = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    await callback.message.edit_text(
        f"Запись отменена ❌\n{name}, {format_date(day)} в {booking['time']}"
    )
    await callback.answer()
    await notify_admin_cancelled(callback.bot, callback.from_user, booking)


async def notify_admin_cancelled(bot: Bot, user: User, booking, source: str = "") -> None:
    """Уведомление мастеру: клиент отменил запись (source — откуда, например «из напоминания»)."""
    name, _ = SERVICES[booking["service"]]
    await notify_admin(
        bot,
        f"❌ Клиент отменил запись{source}\n"
        f"Услуга: {name}\n"
        f"Дата: {format_date(date.fromisoformat(booking['date']))}\n"
        f"Время: {booking['time']}\n"
        f"Клиент: {client_name(user)}"
        + phone_line(db.client_phone(user.id)),
    )


# ---------------- Напоминания клиенту ----------------
# За 24 часа и за 2 часа до записи. Отметки об отправке — в колонках reminded_24h / reminded_2h.
# callback_data кнопок напоминания начинается с «rem:»

REMINDERS = [
    # (колонка-отметка, за сколько до записи)
    ("reminded_24h", timedelta(hours=24)),
    ("reminded_2h", timedelta(hours=2)),
]


def when_text(day: date) -> str:
    """«сегодня», «завтра» или дата."""
    days = (day - date.today()).days
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    return format_date(day)


def reminder_view(booking) -> tuple[str, InlineKeyboardMarkup]:
    """Текст напоминания и кнопки «Приду ✅» / «Отменить запись»."""
    name, price = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    text = (
        f"⏰ Напоминаем: {when_text(day)}, {format_date(day)}, в {booking['time']} "
        f"у вас запись в барбершоп.\nУслуга: {name} — {price} ₽\n\n"
        "Пожалуйста, подтвердите визит."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Приду ✅", callback_data=f"rem:yes:{booking['id']}")],
            [InlineKeyboardButton(text="Отменить запись", callback_data=f"rem:cancel:{booking['id']}")],
        ]
    )
    return text, keyboard


async def send_reminder(bot: Bot, booking) -> bool:
    """Отправляет напоминание клиенту. False — если не получилось (например, бот заблокирован)."""
    text, keyboard = reminder_view(booking)
    try:
        await bot.send_message(booking["user_id"], text, reply_markup=keyboard)
        return True
    except TelegramAPIError as error:
        logging.warning("Не удалось отправить напоминание по записи %s: %s", booking["id"], error)
        return False


async def check_reminders(bot: Bot) -> None:
    """Отправляет напоминания, срок которых наступил. Запись, до которой меньше 2 часов,
    получает только напоминание за 2 часа: «суточное» для неё уже не актуально."""
    now = datetime.now()
    today = now.date()
    bookings = db.bookings_to_remind(today.isoformat(), (today + timedelta(days=1)).isoformat())
    for booking in bookings:
        start = booking_start_time(booking["date"], booking["time"])
        if start <= now:
            continue
        due = [column for column, before in REMINDERS if now >= start - before]
        if not due:
            continue  # до записи больше 24 часов
        # Шлём только самое позднее из наступивших напоминаний, а более ранние
        # отмечаем без отправки, чтобы не прислать два подряд
        *earlier, column = due
        for skipped in earlier:
            db.mark_reminded(booking["id"], skipped)
        # Сначала отметка, потом отправка: если бот упадёт между ними,
        # напоминание пропадёт, но не придёт дважды
        if db.mark_reminded(booking["id"], column):
            await send_reminder(bot, booking)


async def reminders_loop(bot: Bot) -> None:
    """Фоновая проверка напоминаний раз в минуту."""
    while True:
        try:
            await check_reminders(bot)
        except Exception:
            # Ошибка в одной проверке не должна остановить напоминания совсем
            logging.exception("Ошибка при проверке напоминаний")
        await asyncio.sleep(60)


@dp.callback_query(F.data.startswith("rem:yes:"))
async def reminder_confirm(callback: CallbackQuery) -> None:
    booking = db.user_booking(int(callback.data.split(":")[2]), callback.from_user.id)
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    newly_confirmed = db.confirm_booking(booking["id"], callback.from_user.id)
    text, _ = reminder_view(booking)
    await callback.message.edit_text(text.rsplit("\n\n", 1)[0] + "\n\nЖдём вас! ✅")
    await callback.answer("Ждём вас!")
    if newly_confirmed:
        name, _ = SERVICES[booking["service"]]
        await notify_admin(
            callback.bot,
            "✅ Клиент подтвердил визит\n"
            f"Услуга: {name}\n"
            f"Дата: {format_date(date.fromisoformat(booking['date']))}\n"
            f"Время: {booking['time']}\n"
            f"Клиент: {client_name(callback.from_user)}"
            + phone_line(booking["phone"]),
        )


# Отмена из напоминания. Шаг 1: спрашиваем подтверждение
@dp.callback_query(F.data.startswith("rem:cancel:"))
async def reminder_cancel_ask(callback: CallbackQuery) -> None:
    booking = db.user_booking(int(callback.data.split(":")[2]), callback.from_user.id)
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, _ = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отменить", callback_data=f"rem:cancel_yes:{booking['id']}")],
            [InlineKeyboardButton(text="Нет, оставить", callback_data=f"rem:cancel_no:{booking['id']}")],
        ]
    )
    await callback.message.edit_text(
        f"Отменить запись на {format_date(day)} в {booking['time']} ({name})?",
        reply_markup=keyboard,
    )
    await callback.answer()


# Шаг 2а: клиент передумал — возвращаем напоминание с кнопками
@dp.callback_query(F.data.startswith("rem:cancel_no:"))
async def reminder_cancel_no(callback: CallbackQuery) -> None:
    booking = db.user_booking(int(callback.data.split(":")[2]), callback.from_user.id)
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    text, keyboard = reminder_view(booking)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Шаг 2б: подтверждено — удаляем запись и сообщаем мастеру
@dp.callback_query(F.data.startswith("rem:cancel_yes:"))
async def reminder_cancel_yes(callback: CallbackQuery) -> None:
    booking = db.delete_booking(int(callback.data.split(":")[2]), callback.from_user.id)
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, _ = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    await callback.message.edit_text(
        f"Запись отменена ❌\n{name}, {format_date(day)} в {booking['time']}\n"
        "Записаться снова можно кнопкой «Записаться»."
    )
    await callback.answer()
    await notify_admin_cancelled(callback.bot, callback.from_user, booking, " (из напоминания)")


# ---------------- Меню мастера ----------------
# Все обработчики ниже работают только для администратора (фильтр IsAdmin()).
# callback_data кнопок мастера начинается с «adm:».


def plural_bookings(count: int) -> str:
    """1 запись, 2 записи, 5 записей."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} запись"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} записи"
    return f"{count} записей"


async def booking_client(bot: Bot, booking) -> str:
    """Имя и ник клиента записи. У записей, сделанных до появления меню мастера,
    имени в базе нет — спрашиваем его у Telegram, а если не вышло, показываем ID."""
    if booking["client_name"]:
        return format_client(booking["client_name"], booking["client_username"])
    try:
        chat = await bot.get_chat(booking["user_id"])
        return format_client(chat.full_name, chat.username)
    except TelegramAPIError:
        return f"клиент ID {booking['user_id']}"


async def day_report(bot: Bot, day: date) -> tuple[str, InlineKeyboardMarkup | None]:
    """Записи на день для мастера: текст и кнопки «Отменить» под каждой записью."""
    bookings = db.bookings_between(day.isoformat(), day.isoformat())
    if not bookings:
        return f"📅 {format_date(day)}\n\nЗаписей нет.", None
    lines = [f"📅 {format_date(day)}\n"]
    buttons = []
    total = 0
    for booking in bookings:
        name, price = SERVICES[booking["service"]]
        total += price
        client = await booking_client(bot, booking)
        line = f"{booking['time']} — {name}, {price} ₽\n      {client}"
        if booking["phone"]:
            line += f"\n      📱 {booking['phone']}"
        if booking["confirmed"]:
            line += "\n      ✅ подтвердил визит"
        lines.append(line)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Отменить {booking['time']} — {client}",
                    callback_data=f"adm:cancel:{booking['id']}",
                )
            ]
        )
    lines.append(f"\nИтого: {plural_bookings(len(bookings))} на {total} ₽")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("admin"), IsAdmin())
@dp.message(F.text == "Меню мастера", IsAdmin())
async def master_menu_open(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню мастера.", reply_markup=master_menu)


@dp.message(Command("admin"))
async def master_menu_denied(message: Message) -> None:
    await message.answer("Эта команда доступна только мастеру.")


# /test_reminder — сразу отправить напоминание по ближайшей записи (для проверки).
# Отметку в базе не ставит: обычные напоминания по этой записи придут как положено
@dp.message(Command("test_reminder"), IsAdmin())
async def test_reminder(message: Message) -> None:
    now = datetime.now()
    upcoming = [
        booking
        for booking in db.bookings_between(now.date().isoformat(), "9999-12-31")
        if booking_start_time(booking["date"], booking["time"]) > now
    ]
    if not upcoming:
        await message.answer("Предстоящих записей нет — напоминание отправлять некому.")
        return
    booking = upcoming[0]
    client = await booking_client(message.bot, booking)
    when = f"{format_date(date.fromisoformat(booking['date']))} в {booking['time']}"
    if await send_reminder(message.bot, booking):
        await message.answer(f"Напоминание по записи {when} отправлено: {client}.")
    else:
        await message.answer(f"Не удалось отправить напоминание по записи {when} ({client}), см. лог.")


@dp.message(Command("test_reminder"))
async def test_reminder_denied(message: Message) -> None:
    await message.answer("Эта команда доступна только мастеру.")


@dp.message(F.text == "Клиентское меню", IsAdmin())
async def master_to_client_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Клиентское меню.", reply_markup=menu_for(message.from_user))


@dp.message(F.text.in_({"Записи на сегодня", "На завтра"}), IsAdmin())
async def master_day_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    day = date.today()
    if message.text == "На завтра":
        day += timedelta(days=1)
    text, keyboard = await day_report(message.bot, day)
    await message.answer(text, reply_markup=keyboard)


@dp.message(F.text == "На неделю", IsAdmin())
async def master_week_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    today = date.today()
    days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    bookings = db.bookings_between(days[0].isoformat(), days[-1].isoformat())
    if not bookings:
        await message.answer(
            f"На неделю ({format_date(days[0])} — {format_date(days[-1])}) записей нет."
        )
        return
    # Каждый день с записями — отдельным сообщением со своими кнопками «Отменить»
    booked_days = {booking["date"] for booking in bookings}
    for day in days:
        if day.isoformat() in booked_days:
            text, keyboard = await day_report(message.bot, day)
            await message.answer(text, reply_markup=keyboard)
    total = sum(SERVICES[booking["service"]][1] for booking in bookings)
    free_days = [format_date(day) for day in days if day.isoformat() not in booked_days]
    summary = (
        f"Итого за неделю ({format_date(days[0])} — {format_date(days[-1])}): "
        f"{plural_bookings(len(bookings))} на {total} ₽"
    )
    if free_days:
        summary += "\nБез записей: " + ", ".join(free_days)
    await message.answer(summary)


# Отмена записи мастером. Шаг 1: спрашиваем подтверждение в том же сообщении
@dp.callback_query(F.data.startswith("adm:cancel:"), IsAdmin())
async def master_cancel_ask(callback: CallbackQuery) -> None:
    booking = db.booking(int(callback.data.split(":")[2]))
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, price = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    client = await booking_client(callback.bot, booking)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отменить запись", callback_data=f"adm:cancel_yes:{booking['id']}")],
            [InlineKeyboardButton(text="Нет, оставить", callback_data=f"adm:cancel_no:{booking['date']}")],
        ]
    )
    await callback.message.edit_text(
        "Отменить запись?\n\n"
        f"{format_date(day)} в {booking['time']}\n"
        f"{name} — {price} ₽\n"
        f"Клиент: {client}"
        + phone_line(booking["phone"])
        + "\n\nКлиенту придёт сообщение об отмене.",
        reply_markup=keyboard,
    )
    await callback.answer()


# Шаг 2а: мастер передумал — возвращаем список записей на этот день
@dp.callback_query(F.data.startswith("adm:cancel_no:"), IsAdmin())
async def master_cancel_no(callback: CallbackQuery) -> None:
    day = date.fromisoformat(callback.data.split(":")[2])
    text, keyboard = await day_report(callback.bot, day)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Шаг 2б: подтверждено — удаляем запись, пишем клиенту, обновляем список
@dp.callback_query(F.data.startswith("adm:cancel_yes:"), IsAdmin())
async def master_cancel_yes(callback: CallbackQuery) -> None:
    booking = db.cancel_booking(int(callback.data.split(":")[2]))
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, _ = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    try:
        await callback.bot.send_message(
            booking["user_id"],
            f"Мастер отменил вашу запись на {format_date(day)} в {booking['time']} ({name}). "
            "Приносим извинения! Выбрать другое время можно кнопкой «Записаться».",
        )
        result = "клиент уведомлён"
    except TelegramAPIError as error:
        # Например, клиент заблокировал бота. Запись всё равно отменена
        logging.warning("Не удалось уведомить клиента об отмене: %s", error)
        result = "но сообщение клиенту отправить не удалось"
    text, keyboard = await day_report(callback.bot, day)
    await callback.message.edit_text(
        f"❌ Запись на {booking['time']} отменена, {result}.\n\n{text}",
        reply_markup=keyboard,
    )
    await callback.answer("Запись отменена")


# ---------------- Закрытие времени ----------------


def close_days_keyboard() -> InlineKeyboardMarkup:
    """Выбор дня для закрытия на CLOSE_DAYS_AHEAD дней вперёд.
    🔒 — день закрыт целиком, 🔸 — закрыты отдельные часы."""
    today = date.today()
    buttons = []
    for i in range(CLOSE_DAYS_AHEAD):
        day = today + timedelta(days=i)
        closed = db.closed_times(day.isoformat())
        mark = "🔒 " if db.WHOLE_DAY in closed else "🔸 " if closed else ""
        buttons.append(
            InlineKeyboardButton(text=mark + format_date(day), callback_data=f"adm:close:{day.isoformat()}")
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_close_day(raw: str) -> date | None:
    """Дата из кнопки закрытия времени: ГГГГ-ММ-ДД в пределах CLOSE_DAYS_AHEAD дней."""
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    if not 0 <= (day - date.today()).days < CLOSE_DAYS_AHEAD:
        return None
    return day


# Отмеченные часы хранятся прямо в callback_data кнопок: «10.11.15» — 10:00, 11:00 и 15:00.
# Так не нужен FSM, и старые сообщения с кнопками продолжают работать
def encode_hours(times: set[str]) -> str:
    return ".".join(str(int(time[:2])) for time in sorted(times))


def decode_hours(raw: str) -> set[str] | None:
    """Обратное к encode_hours. None — если в строке что-то кроме рабочих часов."""
    if not raw:
        return set()
    times = set()
    for part in raw.split("."):
        if not part.isdigit() or int(part) not in WORK_HOURS:
            return None
        times.add(f"{int(part):02d}:00")
    return times


def close_hours_view(day: date, selected: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Экран выбора часов: «Весь день», часы-переключатели (✅ — будет закрыт) и «Сохранить»."""
    booked = db.busy_times(day.isoformat())
    key = day.isoformat()
    hour_buttons = []
    for time in WORK_TIMES:
        toggled = selected ^ {time}  # выбор после нажатия на эту кнопку
        text = ("✅ " if time in selected else "") + time + (" 👤" if time in booked else "")
        hour_buttons.append(
            InlineKeyboardButton(text=text, callback_data=f"adm:hours:{key}:{encode_hours(toggled)}")
        )
    rows = [[InlineKeyboardButton(text="🔒 Весь день", callback_data=f"adm:wholeday:{key}")]]
    rows += [hour_buttons[i : i + 4] for i in range(0, len(hour_buttons), 4)]
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"adm:save:{key}:{encode_hours(selected)}")])
    rows.append([InlineKeyboardButton(text="⬅️ К выбору дня", callback_data="adm:closemenu")])
    text = (
        f"Закрыть время: {format_date(day)}\n\n"
        "Нажмите «Весь день» или отметьте часы и нажмите «Сохранить».\n"
        "👤 — на это время уже есть запись."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def conflicts_warning(bot: Bot, day: date, times: set[str]) -> str:
    """Предупреждение о записях, которые попали на закрытое время. Сами записи не отменяются."""
    conflicts = [
        booking
        for booking in db.bookings_between(day.isoformat(), day.isoformat())
        if booking["time"] in times
    ]
    if not conflicts:
        return ""
    lines = ["\n\n⚠️ На закрытое время уже есть записи. Они НЕ отменены:"]
    for booking in conflicts:
        name, price = SERVICES[booking["service"]]
        client = await booking_client(bot, booking)
        lines.append(f"• {booking['time']} — {name}, {price} ₽, {client}")
    lines.append("Отменить их можно в списке записей («Записи на сегодня», «На завтра», «На неделю»).")
    return "\n".join(lines)


@dp.message(F.text == "Закрыть время", IsAdmin())
async def master_close_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите день, в котором нужно закрыть время:", reply_markup=close_days_keyboard())


@dp.callback_query(F.data == "adm:closemenu", IsAdmin())
async def master_close_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Выберите день, в котором нужно закрыть время:", reply_markup=close_days_keyboard()
    )
    await callback.answer()


# День выбран — показываем часы. Уже закрытые часы сразу отмечены
@dp.callback_query(F.data.startswith("adm:close:"), IsAdmin())
async def master_close_day(callback: CallbackQuery) -> None:
    day = parse_close_day(callback.data.split(":")[2])
    if day is None:
        await callback.answer("Этот день уже недоступен", show_alert=True)
        return
    closed = db.closed_times(day.isoformat())
    if db.WHOLE_DAY in closed:
        await callback.answer(
            "Этот день уже закрыт целиком. Открыть его можно в «Закрытое время».", show_alert=True
        )
        return
    text, keyboard = close_hours_view(day, closed)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Нажат час-переключатель: перерисовываем кнопки с новым выбором
@dp.callback_query(F.data.startswith("adm:hours:"), IsAdmin())
async def master_toggle_hour(callback: CallbackQuery) -> None:
    _, _, raw_day, raw_hours = callback.data.split(":", 3)
    day, selected = parse_close_day(raw_day), decode_hours(raw_hours)
    if day is None or selected is None:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    text, keyboard = close_hours_view(day, selected)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:save:"), IsAdmin())
async def master_save_hours(callback: CallbackQuery) -> None:
    _, _, raw_day, raw_hours = callback.data.split(":", 3)
    day, selected = parse_close_day(raw_day), decode_hours(raw_hours)
    if day is None or selected is None:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    if not selected and not db.closed_times(day.isoformat()):
        await callback.answer("Отметьте хотя бы один час или нажмите «Весь день»", show_alert=True)
        return
    db.set_closed_hours(day.isoformat(), sorted(selected))
    if selected:
        text = f"🔒 {format_date(day)}: закрыто {', '.join(sorted(selected))}."
    else:
        text = f"🔓 {format_date(day)}: все часы открыты."
    text += await conflicts_warning(callback.bot, day, selected)
    await callback.message.edit_text(text)
    await callback.answer("Сохранено")


@dp.callback_query(F.data.startswith("adm:wholeday:"), IsAdmin())
async def master_close_whole_day(callback: CallbackQuery) -> None:
    day = parse_close_day(callback.data.split(":")[2])
    if day is None:
        await callback.answer("Этот день уже недоступен", show_alert=True)
        return
    db.close_whole_day(day.isoformat())
    text = f"🔒 {format_date(day)} закрыт целиком. Клиенты этот день не увидят."
    text += await conflicts_warning(callback.bot, day, set(WORK_TIMES))
    await callback.message.edit_text(text)
    await callback.answer("Сохранено")


def closed_view() -> tuple[str, InlineKeyboardMarkup | None]:
    """Список закрытого времени начиная с сегодня, с кнопкой «Открыть» у каждого дня и часа."""
    rows = db.closed_from(date.today().isoformat())
    if not rows:
        return "Закрытого времени нет.", None
    lines = ["🔒 Закрытое время:\n"]
    buttons = []
    by_day: dict[str, list[str]] = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        what = "весь день" if row["time"] == db.WHOLE_DAY else row["time"]
        by_day.setdefault(format_date(day), []).append(what)
        buttons.append(
            [InlineKeyboardButton(text=f"🔓 Открыть {format_date(day)}, {what}", callback_data=f"adm:open:{row['id']}")]
        )
    lines += [f"• {day} — {', '.join(times)}" for day, times in by_day.items()]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text == "Закрытое время", IsAdmin())
async def master_closed_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = closed_view()
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("adm:open:"), IsAdmin())
async def master_open_time(callback: CallbackQuery) -> None:
    opened = db.open_time(int(callback.data.split(":")[2]))
    text, keyboard = closed_view()
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # список не изменился (это время уже открыли из другого сообщения)
    await callback.answer("Открыто" if opened else "Это время уже открыто")


# Кнопку мастера нажал не администратор (например, подделал callback_data)
@dp.callback_query(F.data.startswith("adm:"))
async def master_button_denied(callback: CallbackQuery) -> None:
    await callback.answer("Это действие доступно только мастеру.", show_alert=True)


# Нажата кнопка из старого сообщения, когда клиент уже на другом шаге
@dp.callback_query()
async def outdated_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Нажмите «Записаться» ещё раз.", show_alert=True)


async def main() -> None:
    global ADMIN_ID
    # Читаем настройки из файла .env рядом с bot.py, чтобы не хранить токен в коде.
    # Переменные, уже заданные в окружении (например, в systemd), имеют приоритет
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не найден BOT_TOKEN. Создайте файл .env по образцу .env.example")

    # ADMIN_ID необязателен: если он пустой, бот просто не шлёт уведомления
    admin_id = os.getenv("ADMIN_ID", "").strip()
    if admin_id:
        if not admin_id.isdigit():
            sys.exit("ADMIN_ID в .env должен быть числом. Узнать свой ID можно командой /id")
        ADMIN_ID = int(admin_id)

    db.init_db()
    logging.info("База данных: %s", db.db_path())
    bot = Bot(token=token)
    # Напоминания проверяются в фоне, пока бот принимает сообщения.
    # Ссылку на задачу храним, чтобы её не удалил сборщик мусора
    reminders = asyncio.create_task(reminders_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
