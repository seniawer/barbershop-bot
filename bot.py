import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

dp = Dispatcher()

# Главное меню: кнопки под полем ввода сообщения
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Мои записи"), KeyboardButton(text="Услуги и цены")],
    ],
    resize_keyboard=True,
)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    name = message.from_user.first_name if message.from_user else "гость"
    await message.answer(
        f"Здравствуйте, {name}! 👋\n"
        "Я бот барбершопа. Помогу записаться на стрижку.\n"
        "Выберите действие в меню ниже.",
        reply_markup=main_menu,
    )


async def main() -> None:
    # Читаем токен из файла .env, чтобы не хранить его в коде
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не найден BOT_TOKEN. Создайте файл .env по образцу .env.example")

    bot = Bot(token=token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
