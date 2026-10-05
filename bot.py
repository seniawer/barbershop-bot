import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
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
)
from dotenv import load_dotenv

import db

dp = Dispatcher()

# Главное меню: кнопки под полем ввода сообщения
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Мои записи"), KeyboardButton(text="Услуги и цены")],
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

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Telegram ID администратора из .env (задаётся в main). None — уведомления не отправляются
ADMIN_ID: int | None = None


# Шаги записи. FSM запоминает, на каком из них сейчас клиент
class Booking(StatesGroup):
    service = State()
    date = State()
    time = State()


cancel_button = InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")


def free_times(day: date) -> list[str]:
    """Свободное время на день: не занятое другими клиентами и (для сегодня) ещё не прошедшее."""
    now = datetime.now()
    busy = db.busy_times(day.isoformat())
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
    if user.username:
        return f"{user.full_name} (@{user.username})"
    return user.full_name


async def notify_admin(bot: Bot, text: str) -> None:
    """Отправляет сообщение администратору, если ADMIN_ID задан."""
    if ADMIN_ID is None:
        return
    try:
        await bot.send_message(ADMIN_ID, text)
    except TelegramAPIError as error:
        # Например, админ ещё не нажал /start у бота. Клиенту это не мешает — только пишем в лог
        logging.warning("Не удалось уведомить администратора: %s", error)


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
        reply_markup=main_menu,
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
        callback.from_user.id, data["service"], data["date"], time
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
        f"Клиент: {client_name(callback.from_user)}",
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
    await notify_admin(
        callback.bot,
        "❌ Клиент отменил запись\n"
        f"Услуга: {name}\n"
        f"Дата: {format_date(day)}\n"
        f"Время: {booking['time']}\n"
        f"Клиент: {client_name(callback.from_user)}",
    )


# Нажата кнопка из старого сообщения, когда клиент уже на другом шаге
@dp.callback_query()
async def outdated_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Нажмите «Записаться» ещё раз.", show_alert=True)


async def main() -> None:
    global ADMIN_ID
    # Читаем токен из файла .env, чтобы не хранить его в коде
    load_dotenv()
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
    bot = Bot(token=token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
