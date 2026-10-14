import asyncio
import base64
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
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
# То же меню для владельца и мастеров — с кнопкой «Меню мастера»
admin_main_menu = ReplyKeyboardMarkup(
    keyboard=[*CLIENT_MENU_ROWS, [KeyboardButton(text="Меню мастера")]],
    resize_keyboard=True,
)

# Меню мастера (только для владельца и мастеров)
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
# Сколько минут длится услуга (показывается в мини-приложении). Запись всё равно идёт на час
SERVICE_MINUTES = {
    "haircut": 45,
    "beard": 30,
    "combo": 60,
    "kids": 30,
    "buzz": 20,
}
assert SERVICE_MINUTES.keys() == SERVICES.keys(), "у каждой услуги должна быть длительность"

# Название и адрес барбершопа для шапки мини-приложения (пока демо-данные)
SHOP_NAME = "Барбершоп «Бритва»"
SHOP_ADDRESS = "ул. Пушкина, 10"

# Сколько дней вперёд можно записаться и в какие часы работает барбершоп
DAYS_AHEAD = 7
WORK_HOURS = range(10, 20)  # с 10:00, последняя запись в 19:00
WORK_TIMES = [f"{hour:02d}:00" for hour in WORK_HOURS]
# На сколько дней вперёд мастер может закрыть время
CLOSE_DAYS_AHEAD = 14

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Адрес мини-приложения (Telegram Mini App) с онлайн-записью. Страница публичная
MINIAPP_URL = "https://seniawer.github.io/barbershop-miniapp/"
# Наш собственный запас по длине адреса страницы с данными (точного лимита Telegram не публикует)
MINIAPP_URL_LIMIT = 2048

# Telegram ID владельца из .env (задаётся в main). Владелец получает все уведомления
# и в меню мастера управляет записями и временем всех мастеров.
# None — владельца нет: меню мастера доступно только мастерам с telegram_id
ADMIN_ID: int | None = None

# ---------------- Мастера ----------------
# Список мастеров — в файле masters.json рядом с ботом (читается в main).
# Первый мастер в файле — тот, к кому при обновлении базы привязываются старые записи

MASTERS_FILE = Path(__file__).with_name("masters.json")
# id мастера уходит в callback_data кнопок и в адрес мини-приложения, поэтому он короткий
MASTER_ID_RE = re.compile(r"[a-z0-9_-]{1,16}")
# Особые значения вместо id мастера: «Любой мастер» при записи и «Все мастера» в меню мастера
ANY_MASTER = "any"
ALL_MASTERS = "all"


@dataclass(frozen=True)
class Master:
    id: str
    name: str
    description: str = ""
    # Telegram ID мастера: ему приходят уведомления о его записях и доступно меню мастера
    telegram_id: int | None = None


MASTERS: dict[str, Master] = {}


def load_masters(path: Path) -> dict[str, Master]:
    """Читает и проверяет masters.json. При ошибке — ValueError с понятным описанием."""
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"не удалось прочитать {path.name}: {error}") from error
    if not isinstance(items, list) or not items:
        raise ValueError(f"в {path.name} должен быть непустой список мастеров")
    masters: dict[str, Master] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"мастер в {path.name} должен быть объектом: {item!r}")
        master_id, name = item.get("id"), item.get("name")
        description, telegram_id = item.get("description", ""), item.get("telegram_id")
        if (
            not isinstance(master_id, str)
            or not MASTER_ID_RE.fullmatch(master_id)
            or master_id in (ANY_MASTER, ALL_MASTERS)
        ):
            raise ValueError(
                f"неверный id мастера {master_id!r}: нужны строчные латинские буквы, цифры, _ или -,"
                f" до 16 символов (кроме «{ANY_MASTER}» и «{ALL_MASTERS}»)"
            )
        if master_id in masters:
            raise ValueError(f"id мастера {master_id!r} повторяется")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"у мастера {master_id!r} нет имени")
        if not isinstance(description, str):
            raise ValueError(f"описание мастера {master_id!r} должно быть строкой")
        # bool в Python — тоже int, поэтому true/false отсекаем отдельно
        if telegram_id is not None and (
            not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0
        ):
            raise ValueError(f"telegram_id мастера {master_id!r} должен быть числом или null")
        masters[master_id] = Master(master_id, name.strip(), description.strip(), telegram_id)
    return masters


def master_name(master_id: str) -> str:
    """Имя мастера. Если мастера убрали из masters.json, а записи к нему остались, — его id."""
    master = MASTERS.get(master_id)
    return master.name if master else f"мастер {master_id}"


def master_label(master: str) -> str:
    """Имя мастера или «Любой мастер» (master — id или ANY_MASTER)."""
    return "Любой мастер" if master == ANY_MASTER else master_name(master)


def is_owner(user: User | None) -> bool:
    return ADMIN_ID is not None and user is not None and user.id == ADMIN_ID


def managed_masters(user: User | None) -> list[str]:
    """Мастера, чьими записями и временем может управлять пользователь:
    владелец (ADMIN_ID) — всеми, мастер — только собой (по telegram_id), клиент — никем."""
    if user is None:
        return []
    if is_owner(user):
        return list(MASTERS)
    return [master.id for master in MASTERS.values() if master.telegram_id == user.id]


def can_manage(user: User | None, master_id: str) -> bool:
    return master_id in managed_masters(user)


class IsStaff(Filter):
    """Пропускает сообщение или нажатие кнопки, только если его прислал владелец или мастер.
    Стоит на каждом обработчике меню мастера: скрытой кнопки мало, текст «Меню мастера»
    или callback_data «adm:...» может отправить кто угодно. Права на конкретного мастера
    (из кнопки или записи) каждый обработчик дополнительно проверяет через can_manage."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(managed_masters(event.from_user))


def menu_for(user: User | None) -> ReplyKeyboardMarkup:
    """Главное меню: у владельца и мастеров в нём есть кнопка «Меню мастера»."""
    return admin_main_menu if managed_masters(user) else main_menu


# Шаги записи. FSM запоминает, на каком из них сейчас клиент
class Booking(StatesGroup):
    service = State()
    master = State()
    date = State()
    time = State()


cancel_button = InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")


def closed_hours(master_id: str, day: date) -> set[str]:
    """Часы, которые мастер закрыл на этот день (если закрыт весь день — все рабочие часы)."""
    closed = db.closed_times(master_id, day.isoformat())
    if db.WHOLE_DAY in closed:
        return set(WORK_TIMES)
    return closed


def unavailable_times(master_id: str, day: date) -> set[str]:
    """Время, на которое к мастеру записаться нельзя: занято другим клиентом или закрыто мастером."""
    return db.busy_times(master_id, day.isoformat()) | closed_hours(master_id, day)


def master_free_times(master_id: str, day: date) -> list[str]:
    """Свободное время мастера на день: не занятое, не закрытое и (для сегодня) ещё не прошедшее."""
    now = datetime.now()
    busy = unavailable_times(master_id, day)
    return [
        time
        for hour in WORK_HOURS
        if (day != now.date() or hour > now.hour)
        and (time := f"{hour:02d}:00") not in busy
    ]


def master_choices(master: str) -> list[str]:
    """Из каких мастеров выбирать: для «Любого мастера» — из всех."""
    return list(MASTERS) if master == ANY_MASTER else [master]


def free_times(master: str, day: date) -> list[str]:
    """Свободное время на день у мастера, а для «Любого мастера» — время, когда свободен хоть кто-то."""
    times: set[str] = set()
    for master_id in master_choices(master):
        times.update(master_free_times(master_id, day))
    return sorted(times)


def booking_days() -> list[date]:
    """Дни, на которые можно записаться: сегодня и DAYS_AHEAD - 1 дней вперёд."""
    today = date.today()
    return [today + timedelta(days=i) for i in range(DAYS_AHEAD)]


def has_free_time(master: str) -> bool:
    return any(free_times(master, day) for day in booking_days())


def book(user: User, service: str, master: str, day: date, time: str) -> str | None:
    """Записывает клиента и возвращает id мастера, к которому он записан. None — время уже занято.
    Для «Любого мастера» бот сам выбирает свободного: сначала того, у кого в этот день меньше записей,
    при равенстве — по порядку в masters.json. Если кто-то в ту же секунду занял время
    у первого, пробуем следующего (add_booking вернёт False)."""
    candidates = [master_id for master_id in master_choices(master) if time in master_free_times(master_id, day)]
    if master == ANY_MASTER:
        candidates.sort(key=lambda master_id: db.bookings_count(master_id, day.isoformat()))
    for master_id in candidates:
        if db.add_booking(
            user.id,
            service,
            master_id,
            day.isoformat(),
            time,
            user.full_name,
            user.username,
            *reminders_to_skip(day, time),
        ):
            return master_id
    return None


def format_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def client_name(user: User) -> str:
    """Имя клиента для мастера: «Иван Петров (@ivan)»."""
    return format_client(user.full_name, user.username)


def format_client(name: str, username: str | None) -> str:
    if username:
        return f"{name} (@{username})"
    return name


async def notify_staff(bot: Bot, master_ids: Iterable[str], text: str, skip: int | None = None) -> None:
    """Отправляет сообщение владельцу (ADMIN_ID) и мастерам master_ids, у которых указан telegram_id.
    skip — Telegram ID того, кто сам сделал действие: ему уведомление не нужно."""
    recipients = [ADMIN_ID] + [MASTERS[m].telegram_id for m in master_ids if m in MASTERS]
    # dict.fromkeys убирает повторы (владелец может быть и мастером), сохраняя порядок
    for chat_id in dict.fromkeys(recipients):
        if chat_id is None or chat_id == skip:
            continue
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError as error:
            # Например, мастер ещё не нажал /start у бота. Клиенту это не мешает — только пишем в лог
            logging.warning("Не удалось отправить уведомление %s: %s", chat_id, error)


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


def hours_mask(times: Iterable[str]) -> int:
    """Набор рабочих часов одним числом: бит i — час WORK_TIMES[i] (10:00 — бит 0, 11:00 — бит 1...)."""
    return sum(1 << i for i, time in enumerate(WORK_TIMES) if time in set(times))


def mask_hours(mask: int) -> set[str]:
    """Обратное к hours_mask."""
    return {time for i, time in enumerate(WORK_TIMES) if mask >> i & 1}


def miniapp_data(with_descriptions: bool = True) -> dict:
    """Всё, что нужно странице записи: услуги, мастера и их занятые часы на DAYS_AHEAD дней.
    Ключи короткие, а занятость дня — одно число (hours_mask), чтобы адрес оставался коротким:
      v — версия формата, d — первый день, n — сколько дней, h — первый и последний час,
      s — [[код, название, цена, минуты], ...], m — [[id, имя, описание, [занятость по дням]], ...],
      b — [название барбершопа, адрес]
    Закрытое мастером время передаётся как занятое: для клиента разницы нет."""
    days = booking_days()
    return {
        "v": 1,
        "d": days[0].isoformat(),
        "n": len(days),
        "h": [WORK_HOURS.start, WORK_HOURS.stop - 1],
        "s": [[code, name, price, SERVICE_MINUTES[code]] for code, (name, price) in SERVICES.items()],
        "m": [
            [
                master.id,
                master.name,
                master.description if with_descriptions else "",
                [hours_mask(unavailable_times(master.id, day)) for day in days],
            ]
            for master in MASTERS.values()
        ],
        "b": [SHOP_NAME, SHOP_ADDRESS],
    }


def miniapp_url() -> str:
    """Адрес страницы записи с данными в параметре d: JSON → UTF-8 → base64url без «=».
    Если адрес выходит длиннее MINIAPP_URL_LIMIT, описания мастеров не передаются."""
    for with_descriptions in (True, False):
        raw = json.dumps(miniapp_data(with_descriptions), ensure_ascii=False, separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        url = f"{MINIAPP_URL}?d={encoded}"
        if len(url) <= MINIAPP_URL_LIMIT:
            return url
    logging.warning(
        "Адрес мини-приложения длиннее %s символов (%s): сократите названия услуг или число мастеров",
        MINIAPP_URL_LIMIT,
        len(url),
    )
    return url


def miniapp_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой мини-приложения. Адрес собирается заново при каждом вызове,
    чтобы страница получила актуальные услуги, мастеров и занятые слоты (см. miniapp_url).
    Кнопка именно в reply-клавиатуре: из inline-кнопки Telegram.WebApp.sendData не работает."""
    url = miniapp_url()
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


def masters_keyboard() -> InlineKeyboardMarkup:
    """«Любой мастер» и мастера, у которых на неделе есть свободное время."""
    rows = [[InlineKeyboardButton(text="👥 Любой мастер", callback_data=f"master:{ANY_MASTER}")]]
    for master in MASTERS.values():
        if has_free_time(master.id):
            text = f"{master.name} — {master.description}" if master.description else master.name
            rows.append([InlineKeyboardButton(text=text, callback_data=f"master:{master.id}")])
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard(master: str) -> InlineKeyboardMarkup:
    # Не показываем дни, где свободного времени не осталось (всё прошло или занято)
    buttons = [
        InlineKeyboardButton(text=format_date(day), callback_data=f"date:{day.isoformat()}")
        for day in booking_days()
        if free_times(master, day)
    ]
    # По 3 кнопки в ряд
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([cancel_button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(master: str, day: date) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=time, callback_data=f"time:{time}")
        for time in free_times(master, day)
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


# /id — показывает пользователю его Telegram ID (нужен для ADMIN_ID в .env и telegram_id в masters.json)
@dp.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(F.text == "Услуги и цены")
async def show_services(message: Message) -> None:
    lines = [f"• {name} — {price} ₽" for name, price in SERVICES.values()]
    masters = [
        f"• {master.name}" + (f" — {master.description}" if master.description else "")
        for master in MASTERS.values()
    ]
    await message.answer(
        "💈 Наши услуги и цены:\n\n" + "\n".join(lines)
        + "\n\n✂️ Мастера (каждый делает все услуги):\n\n" + "\n".join(masters)
    )


def booking_header(data: dict) -> str:
    """Уже выбранное клиентом: услуга, мастер и дата (что из этого есть)."""
    name, price = SERVICES[data["service"]]
    lines = [f"Услуга: {name} — {price} ₽"]
    if "master" in data:
        lines.append(f"Мастер: {master_label(data['master'])}")
    if "date" in data:
        lines.append(f"Дата: {format_date(date.fromisoformat(data['date']))}")
    return "\n".join(lines)


async def booking_done(message: Message, user: User, code: str, master_id: str, day: date, time: str, source: str) -> None:
    """Общий конец записи (кнопками или онлайн): уведомление мастеру и владельцу и вопрос о телефоне.
    Подтверждение клиенту каждый способ показывает сам."""
    name, price = SERVICES[code]
    await notify_staff(
        message.bot,
        [master_id],
        f"🆕 Новая запись{source}\n"
        f"Мастер: {master_name(master_id)}\n"
        f"Услуга: {name} — {price} ₽\n"
        f"Дата: {format_date(day)}\n"
        f"Время: {time}\n"
        f"Клиент: {client_name(user)}"
        + phone_line(db.client_phone(user.id)),
    )
    await ask_phone_once(message, user)


def booked_text(code: str, master_id: str, day: date, time: str) -> str:
    name, price = SERVICES[code]
    return (
        f"Вы записаны на {format_date(day)} в {time} ✅\n"
        f"Мастер: {master_name(master_id)}\n"
        f"Услуга: {name} — {price} ₽"
    )


# Шаг 1: клиент нажал «Записаться» — предлагаем выбрать услугу
@dp.message(F.text == "Записаться")
async def booking_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Booking.service)
    await message.answer("Выберите услугу:", reply_markup=services_keyboard())


# Шаг 2: услуга выбрана — предлагаем мастера
@dp.callback_query(Booking.service, F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.split(":", 1)[1]
    if code not in SERVICES:
        await callback.answer("Такой услуги нет", show_alert=True)
        return
    await state.update_data(service=code)
    await state.set_state(Booking.master)
    await callback.message.edit_text(
        f"{booking_header(await state.get_data())}\n\nВыберите мастера:",
        reply_markup=masters_keyboard(),
    )
    await callback.answer()


# Шаг 3: мастер выбран — предлагаем дату
@dp.callback_query(Booking.master, F.data.startswith("master:"))
async def choose_master(callback: CallbackQuery, state: FSMContext) -> None:
    master = callback.data.split(":", 1)[1]
    if master != ANY_MASTER and master not in MASTERS:
        await callback.answer("Такого мастера нет", show_alert=True)
        return
    if not has_free_time(master):
        await callback.answer("У этого мастера на неделе нет свободного времени", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=masters_keyboard())
        return
    await state.update_data(master=master)
    await state.set_state(Booking.date)
    await callback.message.edit_text(
        f"{booking_header(await state.get_data())}\n\nВыберите дату:",
        reply_markup=dates_keyboard(master),
    )
    await callback.answer()


# Шаг 4: дата выбрана — предлагаем время
@dp.callback_query(Booking.date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    day = date.fromisoformat(callback.data.split(":", 1)[1])
    master = (await state.get_data())["master"]
    if not free_times(master, day):
        await callback.answer("На этот день свободного времени нет, выберите другой", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=dates_keyboard(master))
        return
    await state.update_data(date=day.isoformat())
    await state.set_state(Booking.time)
    await callback.message.edit_text(
        f"{booking_header(await state.get_data())}\n\nВыберите время:",
        reply_markup=times_keyboard(master, day),
    )
    await callback.answer()


# Шаг 5: время выбрано — подтверждаем запись
@dp.callback_query(Booking.time, F.data.startswith("time:"))
async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
    time = callback.data.split(":", 1)[1]
    data = await state.get_data()
    day = date.fromisoformat(data["date"])
    # Пока клиент выбирал, это время могли занять — book проверяет ещё раз,
    # а для «Любого мастера» выбирает, к кому записать
    master_id = book(callback.from_user, data["service"], data["master"], day, time)
    if master_id is None:
        await callback.answer("Это время уже занято или прошло, выберите другое", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=times_keyboard(data["master"], day))
        return
    await state.clear()
    await callback.message.edit_text(booked_text(data["service"], master_id, day, time))
    await callback.answer()
    await booking_done(callback.message, callback.from_user, data["service"], master_id, day, time, "")


# Онлайн-запись: клиент нажал «Записаться онлайн» — даём кнопку, открывающую мини-приложение
@dp.message(F.text == "Записаться онлайн")
async def miniapp_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Нажмите кнопку ниже: откроется страница записи. "
        "Выберите услугу, мастера, день и время и подтвердите запись.",
        reply_markup=miniapp_keyboard(),
    )


@dp.message(F.text == "⬅️ Главное меню")
async def back_to_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню.", reply_markup=menu_for(message.from_user))


def parse_miniapp_booking(raw: str) -> tuple[str, str, date, str] | None:
    """Проверяет данные из мини-приложения и возвращает (код услуги, мастер, дата, время).
    Мастер — id из MASTERS или ANY_MASTER. None — если данные неверные.
    Странице не доверяем: её JSON может подделать кто угодно."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code, master = data.get("service"), data.get("master")
    day, time = data.get("date"), data.get("time")
    if not all(isinstance(value, str) for value in (code, master, day, time)):
        return None

    # Страница присылает код услуги и id мастера — те, что получила от бота в адресе.
    # Цену и имена из JSON не используем вовсе — они всегда берутся из SERVICES и MASTERS
    if code not in SERVICES:
        return None
    if master != ANY_MASTER and master not in MASTERS:
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
    return code, master, day, time


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
    code, master, day, time = booking
    # Как и в пошаговой записи: book проверяет, что время у мастера не прошло и не занято
    # (для «Любого мастера» — что свободен хоть кто-то), и сам выбирает мастера
    master_id = book(message.from_user, code, master, day, time)
    if master_id is None:
        await message.answer(
            "Это время уже занято или прошло 😔 Выберите другое — список свободного времени обновлён.",
            reply_markup=miniapp_keyboard(),
        )
        return
    await message.answer(
        booked_text(code, master_id, day, time), reply_markup=menu_for(message.from_user)
    )
    await booking_done(message, message.from_user, code, master_id, day, time, " (онлайн)")


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
    # Номер нужен владельцу и мастерам, к которым клиент записан
    upcoming = db.user_bookings(message.from_user.id, date.today().isoformat())
    await notify_staff(
        message.bot,
        {booking["master_id"] for booking in upcoming},
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
            f"{name} — {price} ₽\n{format_date(day)} в {booking['time']}\n"
            f"Мастер: {master_name(booking['master_id'])}",
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
        f"Запись отменена ❌\n{name}, {format_date(day)} в {booking['time']}\n"
        f"Мастер: {master_name(booking['master_id'])}"
    )
    await callback.answer()
    await notify_staff_cancelled(callback.bot, callback.from_user, booking)


async def notify_staff_cancelled(bot: Bot, user: User, booking, source: str = "") -> None:
    """Уведомление мастеру записи и владельцу: клиент отменил запись
    (source — откуда, например «из напоминания»)."""
    name, _ = SERVICES[booking["service"]]
    await notify_staff(
        bot,
        [booking["master_id"]],
        f"❌ Клиент отменил запись{source}\n"
        f"Мастер: {master_name(booking['master_id'])}\n"
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
        f"у вас запись в барбершоп.\nМастер: {master_name(booking['master_id'])}\n"
        f"Услуга: {name} — {price} ₽\n\n"
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
        await notify_staff(
            callback.bot,
            [booking["master_id"]],
            "✅ Клиент подтвердил визит\n"
            f"Мастер: {master_name(booking['master_id'])}\n"
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
        f"Отменить запись на {format_date(day)} в {booking['time']} "
        f"({name}, мастер {master_name(booking['master_id'])})?",
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
        f"Запись отменена ❌\n{name}, {format_date(day)} в {booking['time']}, "
        f"мастер {master_name(booking['master_id'])}\n"
        "Записаться снова можно кнопкой «Записаться»."
    )
    await callback.answer()
    await notify_staff_cancelled(callback.bot, callback.from_user, booking, " (из напоминания)")


# ---------------- Меню мастера ----------------
# Доступно владельцу (ADMIN_ID) и мастерам, у которых в masters.json указан telegram_id.
# Все обработчики ниже стоят за фильтром IsStaff(), а права на конкретного мастера или запись
# каждый проверяет сам (can_manage / staff_view): мастер видит и меняет только своё.
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


def callback_args(callback: CallbackQuery, count: int) -> list[str] | None:
    """Части callback_data после «adm:<действие>:». None — если их не столько, сколько ждём
    (например, кнопка из сообщения, отправленного старой версией бота)."""
    parts = callback.data.split(":")[2:]
    return parts if len(parts) == count else None


def parse_id(raw: str) -> int | None:
    return int(raw) if raw.isdigit() else None


@dataclass(frozen=True)
class StaffView:
    """Чьи записи показывать. flt — ALL_MASTERS или id мастера, уходит в callback_data кнопок,
    чтобы после отмены записи вернуться к тому же фильтру."""

    flt: str
    master_ids: list[str]
    # Кнопки фильтра нужны, если пользователь управляет несколькими мастерами (владелец)
    can_filter: bool


def staff_view(user: User | None, flt: str) -> StaffView | None:
    """Проверяет фильтр из кнопки. None — нет прав (мастер пытается смотреть чужие записи)."""
    managed = managed_masters(user)
    if flt == ALL_MASTERS:
        master_ids = managed
    elif flt in managed:
        master_ids = [flt]
    else:
        return None
    if not master_ids:
        return None
    return StaffView(flt, master_ids, len(managed) > 1)


def filter_row(view: StaffView, callback_prefix: str) -> list[InlineKeyboardButton]:
    """Кнопки «Все / Алексей / Борис ...», текущий фильтр отмечен «✓»."""
    options = [(ALL_MASTERS, "Все")] + [(master_id, master_name(master_id)) for master_id in MASTERS]
    return [
        InlineKeyboardButton(
            text=("✓ " if flt == view.flt else "") + text, callback_data=f"{callback_prefix}:{flt}"
        )
        for flt, text in options
    ]


def view_title(view: StaffView) -> str:
    """Подпись к отчёту: имя мастера, если показан один мастер, иначе «все мастера»."""
    return master_name(view.master_ids[0]) if len(view.master_ids) == 1 else "все мастера"


async def day_report(
    bot: Bot, day: date, view: StaffView, with_filter: bool = True
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Записи на день для мастера: текст и кнопки «Отменить» под каждой записью.
    with_filter — добавить кнопки фильтра по мастеру (только если их можно переключать)."""
    bookings = db.bookings_between(day.isoformat(), day.isoformat(), view.master_ids)
    header = f"📅 {format_date(day)} · {view_title(view)}"
    buttons = []
    if with_filter and view.can_filter:
        buttons.append(filter_row(view, f"adm:day:{day.isoformat()}"))
    if not bookings:
        return f"{header}\n\nЗаписей нет.", InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    lines = [f"{header}\n"]
    total = 0
    for booking in bookings:
        name, price = SERVICES[booking["service"]]
        total += price
        client = await booking_client(bot, booking)
        line = f"{booking['time']} — {name}, {price} ₽"
        if len(view.master_ids) > 1:
            line += f"\n      ✂️ {master_name(booking['master_id'])}"
        line += f"\n      {client}"
        if booking["phone"]:
            line += f"\n      📱 {booking['phone']}"
        if booking["confirmed"]:
            line += "\n      ✅ подтвердил визит"
        lines.append(line)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Отменить {booking['time']} — {client}",
                    callback_data=f"adm:cancel:{booking['id']}:{view.flt}",
                )
            ]
        )
    lines.append(f"\nИтого: {plural_bookings(len(bookings))} на {total} ₽")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("admin"), IsStaff())
@dp.message(F.text == "Меню мастера", IsStaff())
async def master_menu_open(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_owner(message.from_user):
        text = "Меню мастера. Вы владелец: видны записи и время всех мастеров."
    else:
        names = ", ".join(master_name(master_id) for master_id in managed_masters(message.from_user))
        text = f"Меню мастера: {names}."
    await message.answer(text, reply_markup=master_menu)


@dp.message(Command("admin"))
async def master_menu_denied(message: Message) -> None:
    await message.answer("Эта команда доступна только мастеру.")


# /test_reminder — сразу отправить напоминание по ближайшей записи (для проверки).
# Владелец — по ближайшей записи любого мастера, мастер — по своей.
# Отметку в базе не ставит: обычные напоминания по этой записи придут как положено
@dp.message(Command("test_reminder"), IsStaff())
async def test_reminder(message: Message) -> None:
    now = datetime.now()
    upcoming = [
        booking
        for booking in db.bookings_between(
            now.date().isoformat(), "9999-12-31", managed_masters(message.from_user)
        )
        if booking_start_time(booking["date"], booking["time"]) > now
    ]
    if not upcoming:
        await message.answer("Предстоящих записей нет — напоминание отправлять некому.")
        return
    booking = upcoming[0]
    client = await booking_client(message.bot, booking)
    when = (
        f"{format_date(date.fromisoformat(booking['date']))} в {booking['time']}"
        f" (мастер {master_name(booking['master_id'])})"
    )
    if await send_reminder(message.bot, booking):
        await message.answer(f"Напоминание по записи {when} отправлено: {client}.")
    else:
        await message.answer(f"Не удалось отправить напоминание по записи {when}, клиент {client}, см. лог.")


@dp.message(Command("test_reminder"))
async def test_reminder_denied(message: Message) -> None:
    await message.answer("Эта команда доступна только мастеру.")


@dp.message(F.text == "Клиентское меню", IsStaff())
async def master_to_client_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Клиентское меню.", reply_markup=menu_for(message.from_user))


@dp.message(F.text.in_({"Записи на сегодня", "На завтра"}), IsStaff())
async def master_day_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    day = date.today()
    if message.text == "На завтра":
        day += timedelta(days=1)
    # Владелец сначала видит всех мастеров, мастер — только себя
    view = staff_view(message.from_user, ALL_MASTERS)
    text, keyboard = await day_report(message.bot, day, view)
    await message.answer(text, reply_markup=keyboard)


# Кнопка фильтра под записями на день: перерисовываем то же сообщение
@dp.callback_query(F.data.startswith("adm:day:"), IsStaff())
async def master_day_filter(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    view = args and staff_view(callback.from_user, args[1])
    if view is None:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    try:
        day = date.fromisoformat(args[0])
    except ValueError:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    text, keyboard = await day_report(callback.bot, day, view)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # нажали на уже выбранный фильтр — сообщение не изменилось
    await callback.answer()


async def send_week(message: Message, view: StaffView) -> None:
    """Записи на неделю: каждый день с записями — отдельным сообщением со своими кнопками «Отменить»,
    в конце — итог и (для владельца) кнопки фильтра по мастеру."""
    days = booking_days()
    period = f"{format_date(days[0])} — {format_date(days[-1])}"
    bookings = db.bookings_between(days[0].isoformat(), days[-1].isoformat(), view.master_ids)
    keyboard = (
        InlineKeyboardMarkup(inline_keyboard=[filter_row(view, "adm:week")]) if view.can_filter else None
    )
    if not bookings:
        await message.answer(f"На неделю ({period}), {view_title(view)}: записей нет.", reply_markup=keyboard)
        return
    booked_days = {booking["date"] for booking in bookings}
    for day in days:
        if day.isoformat() in booked_days:
            text, day_keyboard = await day_report(message.bot, day, view, with_filter=False)
            await message.answer(text, reply_markup=day_keyboard)
    total = sum(SERVICES[booking["service"]][1] for booking in bookings)
    free_days = [format_date(day) for day in days if day.isoformat() not in booked_days]
    summary = (
        f"Итого за неделю ({period}), {view_title(view)}: "
        f"{plural_bookings(len(bookings))} на {total} ₽"
    )
    if free_days:
        summary += "\nБез записей: " + ", ".join(free_days)
    await message.answer(summary, reply_markup=keyboard)


@dp.message(F.text == "На неделю", IsStaff())
async def master_week_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_week(message, staff_view(message.from_user, ALL_MASTERS))


# Кнопка фильтра под итогом недели: присылаем неделю заново с другим фильтром
@dp.callback_query(F.data.startswith("adm:week:"), IsStaff())
async def master_week_filter(callback: CallbackQuery) -> None:
    args = callback_args(callback, 1)
    view = args and staff_view(callback.from_user, args[0])
    if view is None:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    await callback.answer()
    await send_week(callback.message, view)


# Отмена записи мастером. Шаг 1: спрашиваем подтверждение в том же сообщении
@dp.callback_query(F.data.startswith("adm:cancel:"), IsStaff())
async def master_cancel_ask(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    booking_id = args and parse_id(args[0])
    if not booking_id or staff_view(callback.from_user, args[1]) is None:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    booking = db.booking(booking_id)
    if booking is None or not can_manage(callback.from_user, booking["master_id"]):
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    flt = args[1]
    name, price = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    client = await booking_client(callback.bot, booking)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отменить запись", callback_data=f"adm:cancel_yes:{booking['id']}:{flt}")],
            [InlineKeyboardButton(text="Нет, оставить", callback_data=f"adm:cancel_no:{booking['date']}:{flt}")],
        ]
    )
    await callback.message.edit_text(
        "Отменить запись?\n\n"
        f"{format_date(day)} в {booking['time']}\n"
        f"Мастер: {master_name(booking['master_id'])}\n"
        f"{name} — {price} ₽\n"
        f"Клиент: {client}"
        + phone_line(booking["phone"])
        + "\n\nКлиенту придёт сообщение об отмене.",
        reply_markup=keyboard,
    )
    await callback.answer()


# Шаг 2а: мастер передумал — возвращаем список записей на этот день
@dp.callback_query(F.data.startswith("adm:cancel_no:"), IsStaff())
async def master_cancel_no(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    view = args and staff_view(callback.from_user, args[1])
    if view is None:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    text, keyboard = await day_report(callback.bot, date.fromisoformat(args[0]), view)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Шаг 2б: подтверждено — удаляем запись, пишем клиенту, мастеру/владельцу, обновляем список
@dp.callback_query(F.data.startswith("adm:cancel_yes:"), IsStaff())
async def master_cancel_yes(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    booking_id = args and parse_id(args[0])
    view = args and staff_view(callback.from_user, args[1])
    if not booking_id or view is None:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    # cancel_booking удалит запись, только если она к мастеру, которым управляет пользователь
    booking = db.cancel_booking(booking_id, managed_masters(callback.from_user))
    if booking is None:
        await callback.answer("Эта запись уже отменена", show_alert=True)
        return
    name, _ = SERVICES[booking["service"]]
    day = date.fromisoformat(booking["date"])
    try:
        await callback.bot.send_message(
            booking["user_id"],
            f"Мастер отменил вашу запись на {format_date(day)} в {booking['time']} "
            f"({name}, мастер {master_name(booking['master_id'])}). "
            "Приносим извинения! Выбрать другое время можно кнопкой «Записаться».",
        )
        result = "клиент уведомлён"
    except TelegramAPIError as error:
        # Например, клиент заблокировал бота. Запись всё равно отменена
        logging.warning("Не удалось уведомить клиента об отмене: %s", error)
        result = "но сообщение клиенту отправить не удалось"
    # Владелец отменил запись мастера — сообщаем мастеру, и наоборот
    await notify_staff(
        callback.bot,
        [booking["master_id"]],
        f"❌ Запись отменена в меню мастера ({callback.from_user.full_name})\n"
        f"Мастер: {master_name(booking['master_id'])}\n"
        f"Услуга: {name}\n"
        f"Дата: {format_date(day)}\n"
        f"Время: {booking['time']}",
        skip=callback.from_user.id,
    )
    text, keyboard = await day_report(callback.bot, day, view)
    await callback.message.edit_text(
        f"❌ Запись на {booking['time']} ({master_name(booking['master_id'])}) отменена, {result}.\n\n{text}",
        reply_markup=keyboard,
    )
    await callback.answer("Запись отменена")


# ---------------- Закрытие времени ----------------
# Время закрывается отдельно для каждого мастера. Владелец сначала выбирает мастера,
# мастер сразу попадает к своим дням. id мастера есть в каждой кнопке и проверяется через can_manage


def close_masters_keyboard(user: User | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=master_name(master_id), callback_data=f"adm:closem:{master_id}")]
        for master_id in managed_masters(user)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def close_days_keyboard(master_id: str, with_back: bool) -> InlineKeyboardMarkup:
    """Выбор дня для закрытия на CLOSE_DAYS_AHEAD дней вперёд.
    🔒 — день закрыт целиком, 🔸 — закрыты отдельные часы."""
    today = date.today()
    buttons = []
    for i in range(CLOSE_DAYS_AHEAD):
        day = today + timedelta(days=i)
        closed = db.closed_times(master_id, day.isoformat())
        mark = "🔒 " if db.WHOLE_DAY in closed else "🔸 " if closed else ""
        buttons.append(
            InlineKeyboardButton(
                text=mark + format_date(day), callback_data=f"adm:close:{master_id}:{day.isoformat()}"
            )
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ К выбору мастера", callback_data="adm:closepick")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def close_days_text(master_id: str) -> str:
    return f"Закрыть время: {master_name(master_id)}\n\nВыберите день:"


def parse_close_day(raw: str) -> date | None:
    """Дата из кнопки закрытия времени: ГГГГ-ММ-ДД в пределах CLOSE_DAYS_AHEAD дней."""
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    if not 0 <= (day - date.today()).days < CLOSE_DAYS_AHEAD:
        return None
    return day


# Отмеченные часы хранятся прямо в callback_data кнопок: hours_mask в шестнадцатеричном виде
# («403» — 10:00, 11:00 и 19:00). Так не нужен FSM, старые сообщения с кнопками продолжают работать,
# а callback_data с id мастера укладывается в лимит Telegram 64 байта
def encode_hours(times: set[str]) -> str:
    return format(hours_mask(times), "x")


def decode_hours(raw: str) -> set[str] | None:
    """Обратное к encode_hours. None — если в строке не набор рабочих часов."""
    if not re.fullmatch(r"[0-9a-f]{1,4}", raw):
        return None
    mask = int(raw, 16)
    if mask >> len(WORK_TIMES):
        return None
    return mask_hours(mask)


def close_hours_view(master_id: str, day: date, selected: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Экран выбора часов: «Весь день», часы-переключатели (✅ — будет закрыт) и «Сохранить»."""
    booked = db.busy_times(master_id, day.isoformat())
    key = f"{master_id}:{day.isoformat()}"
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
    rows.append([InlineKeyboardButton(text="⬅️ К выбору дня", callback_data=f"adm:closem:{master_id}")])
    text = (
        f"Закрыть время: {master_name(master_id)}, {format_date(day)}\n\n"
        "Нажмите «Весь день» или отметьте часы и нажмите «Сохранить».\n"
        "👤 — на это время уже есть запись."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def conflicts_warning(bot: Bot, master_id: str, day: date, times: set[str]) -> str:
    """Предупреждение о записях мастера, которые попали на закрытое время. Сами записи не отменяются."""
    conflicts = [
        booking
        for booking in db.bookings_between(day.isoformat(), day.isoformat(), [master_id])
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


def parse_master_day(user: User | None, raw_master: str, raw_day: str) -> tuple[str, date] | None:
    """Мастер и день из кнопки закрытия времени. None — нет прав на мастера или день недоступен."""
    day = parse_close_day(raw_day)
    if day is None or not can_manage(user, raw_master):
        return None
    return raw_master, day


@dp.message(F.text == "Закрыть время", IsStaff())
async def master_close_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    managed = managed_masters(message.from_user)
    if len(managed) == 1:
        await message.answer(close_days_text(managed[0]), reply_markup=close_days_keyboard(managed[0], False))
    else:
        await message.answer(
            "Кому закрыть время? Выберите мастера:", reply_markup=close_masters_keyboard(message.from_user)
        )


@dp.callback_query(F.data == "adm:closepick", IsStaff())
async def master_close_pick(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Кому закрыть время? Выберите мастера:", reply_markup=close_masters_keyboard(callback.from_user)
    )
    await callback.answer()


# Мастер выбран (или «К выбору дня») — показываем его дни
@dp.callback_query(F.data.startswith("adm:closem:"), IsStaff())
async def master_close_menu(callback: CallbackQuery) -> None:
    args = callback_args(callback, 1)
    if args is None or not can_manage(callback.from_user, args[0]):
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    with_back = len(managed_masters(callback.from_user)) > 1
    await callback.message.edit_text(
        close_days_text(args[0]), reply_markup=close_days_keyboard(args[0], with_back)
    )
    await callback.answer()


# День выбран — показываем часы. Уже закрытые часы сразу отмечены
@dp.callback_query(F.data.startswith("adm:close:"), IsStaff())
async def master_close_day(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    parsed = args and parse_master_day(callback.from_user, *args)
    if not parsed:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    master_id, day = parsed
    closed = db.closed_times(master_id, day.isoformat())
    if db.WHOLE_DAY in closed:
        await callback.answer(
            "Этот день уже закрыт целиком. Открыть его можно в «Закрытое время».", show_alert=True
        )
        return
    text, keyboard = close_hours_view(master_id, day, closed)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Нажат час-переключатель: перерисовываем кнопки с новым выбором
@dp.callback_query(F.data.startswith("adm:hours:"), IsStaff())
async def master_toggle_hour(callback: CallbackQuery) -> None:
    args = callback_args(callback, 3)
    parsed = args and parse_master_day(callback.from_user, args[0], args[1])
    selected = args and decode_hours(args[2])
    if not parsed or selected is None:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    text, keyboard = close_hours_view(*parsed, selected)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:save:"), IsStaff())
async def master_save_hours(callback: CallbackQuery) -> None:
    args = callback_args(callback, 3)
    parsed = args and parse_master_day(callback.from_user, args[0], args[1])
    selected = args and decode_hours(args[2])
    if not parsed or selected is None:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    master_id, day = parsed
    if not selected and not db.closed_times(master_id, day.isoformat()):
        await callback.answer("Отметьте хотя бы один час или нажмите «Весь день»", show_alert=True)
        return
    db.set_closed_hours(master_id, day.isoformat(), sorted(selected))
    who = master_name(master_id)
    if selected:
        text = f"🔒 {who}, {format_date(day)}: закрыто {', '.join(sorted(selected))}."
    else:
        text = f"🔓 {who}, {format_date(day)}: все часы открыты."
    text += await conflicts_warning(callback.bot, master_id, day, selected)
    await callback.message.edit_text(text)
    await callback.answer("Сохранено")


@dp.callback_query(F.data.startswith("adm:wholeday:"), IsStaff())
async def master_close_whole_day(callback: CallbackQuery) -> None:
    args = callback_args(callback, 2)
    parsed = args and parse_master_day(callback.from_user, *args)
    if not parsed:
        await callback.answer("Эта кнопка устарела или недоступна", show_alert=True)
        return
    master_id, day = parsed
    db.close_whole_day(master_id, day.isoformat())
    text = f"🔒 {master_name(master_id)}, {format_date(day)}: день закрыт целиком. Клиенты не смогут записаться к мастеру на этот день."
    text += await conflicts_warning(callback.bot, master_id, day, set(WORK_TIMES))
    await callback.message.edit_text(text)
    await callback.answer("Сохранено")


def closed_view(user: User | None) -> tuple[str, InlineKeyboardMarkup | None]:
    """Закрытое время мастеров пользователя начиная с сегодня, с кнопкой «Открыть» у каждого дня и часа."""
    managed = managed_masters(user)
    rows = db.closed_from(date.today().isoformat(), managed)
    if not rows:
        return "Закрытого времени нет.", None
    lines = ["🔒 Закрытое время:"]
    buttons = []
    # Группируем: мастер → день → закрытые часы. Строки уже отсортированы по мастеру и дате
    by_master: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        day = format_date(date.fromisoformat(row["date"]))
        what = "весь день" if row["time"] == db.WHOLE_DAY else row["time"]
        by_master.setdefault(row["master_id"], {}).setdefault(day, []).append(what)
        prefix = f"{master_name(row['master_id'])}, " if len(managed) > 1 else ""
        buttons.append(
            [InlineKeyboardButton(text=f"🔓 Открыть {prefix}{day}, {what}", callback_data=f"adm:open:{row['id']}")]
        )
    for master_id, days in by_master.items():
        lines.append(f"\n✂️ {master_name(master_id)}")
        lines += [f"• {day} — {', '.join(times)}" for day, times in days.items()]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text == "Закрытое время", IsStaff())
async def master_closed_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = closed_view(message.from_user)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("adm:open:"), IsStaff())
async def master_open_time(callback: CallbackQuery) -> None:
    args = callback_args(callback, 1)
    closed_id = args and parse_id(args[0])
    if not closed_id:
        await callback.answer("Эта кнопка устарела", show_alert=True)
        return
    # open_time откроет время, только если оно у мастера, которым управляет пользователь
    opened = db.open_time(closed_id, managed_masters(callback.from_user))
    text, keyboard = closed_view(callback.from_user)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # список не изменился (это время уже открыли из другого сообщения)
    await callback.answer("Открыто" if opened else "Это время уже открыто")


# Кнопка мастера из старого сообщения (до появления нескольких мастеров)
@dp.callback_query(F.data.startswith("adm:"), IsStaff())
async def master_button_outdated(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Откройте нужный раздел меню мастера заново.", show_alert=True)


# Кнопку мастера нажал не мастер и не владелец (например, подделал callback_data)
@dp.callback_query(F.data.startswith("adm:"))
async def master_button_denied(callback: CallbackQuery) -> None:
    await callback.answer("Это действие доступно только мастеру.", show_alert=True)


# Нажата кнопка из старого сообщения, когда клиент уже на другом шаге
@dp.callback_query()
async def outdated_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Нажмите «Записаться» ещё раз.", show_alert=True)


async def main() -> None:
    global ADMIN_ID, MASTERS
    # Читаем настройки из файла .env рядом с bot.py, чтобы не хранить токен в коде.
    # Переменные, уже заданные в окружении (например, в systemd), имеют приоритет
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не найден BOT_TOKEN. Создайте файл .env по образцу .env.example")

    # ADMIN_ID необязателен: если он пустой, уведомления получают только мастера с telegram_id
    admin_id = os.getenv("ADMIN_ID", "").strip()
    if admin_id:
        if not admin_id.isdigit():
            sys.exit("ADMIN_ID в .env должен быть числом. Узнать свой ID можно командой /id")
        ADMIN_ID = int(admin_id)

    try:
        MASTERS = load_masters(MASTERS_FILE)
    except ValueError as error:
        sys.exit(f"Ошибка в списке мастеров: {error}")
    logging.info("Мастера: %s", ", ".join(master.name for master in MASTERS.values()))

    # Записи и закрытое время из старой базы (до нескольких мастеров) достаются первому мастеру
    db.init_db(next(iter(MASTERS)))
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
