"""Хранение записей клиентов и закрытого времени в базе SQLite (по умолчанию файл barbershop.db рядом с ботом)."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BOT_DIR = Path(__file__).parent

# Значение колонки time в таблице closed_time, которое означает «закрыт весь день»
WHOLE_DAY = "*"


def db_path() -> Path:
    """Путь к файлу базы: из переменной DB_PATH, иначе barbershop.db рядом с ботом.

    Читаем при каждом вызове, а не при импорте: .env загружается позже, в main()."""
    value = os.getenv("DB_PATH", "").strip() or "barbershop.db"
    # Относительный путь считаем от папки бота, а не от текущей папки терминала.
    # Абсолютный путь (например /var/lib/...) при таком сложении остаётся как есть
    return BOT_DIR / Path(value).expanduser()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Открывает базу, сохраняет изменения и закрывает её после блока with."""
    conn = sqlite3.connect(db_path())
    # Строки из базы можно читать по имени колонки: row["date"]
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # при успехе — сохранить (commit), при ошибке — откатить
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицу при первом запуске. Если она уже есть — ничего не делает."""
    # Папку для базы создаём заранее, если в DB_PATH указана ещё несуществующая
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        # UNIQUE (date, time): база сама не даст записать двух клиентов на одно время
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                service    TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                time       TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE (date, time)
            )
            """
        )
        # Имя и ник клиента появились позже, чем таблица. В старой базе добавляем колонки,
        # уже сохранённые записи при этом не меняются (у них в новых колонках будет NULL)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)")}
        for column in ("client_name", "client_username"):
            if column not in columns:
                conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} TEXT")
        # Время, которое мастер закрыл для записи. time — «ЧЧ:00» или WHOLE_DAY (весь день)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS closed_time (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT    NOT NULL,
                time TEXT    NOT NULL,
                UNIQUE (date, time)
            )
            """
        )


def add_booking(
    user_id: int,
    service: str,
    day: str,
    time: str,
    client_name: str | None = None,
    client_username: str | None = None,
) -> bool:
    """Сохраняет запись. Возвращает False, если это время уже кто-то занял или мастер его закрыл."""
    try:
        with connect() as conn:
            # Проверка закрытого времени — в том же запросе, что и вставка:
            # если мастер закроет час в ту же секунду, запись всё равно не пройдёт
            cursor = conn.execute(
                "INSERT INTO bookings (user_id, service, date, time, client_name, client_username)"
                " SELECT ?, ?, ?, ?, ?, ?"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM closed_time WHERE date = ? AND time IN (?, ?)"
                " )",
                (user_id, service, day, time, client_name, client_username, day, time, WHOLE_DAY),
            )
        return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        return False


def busy_times(day: str) -> set[str]:
    """Время, которое на этот день уже занято."""
    with connect() as conn:
        rows = conn.execute("SELECT time FROM bookings WHERE date = ?", (day,)).fetchall()
    return {row["time"] for row in rows}


def user_bookings(user_id: int, from_day: str) -> list[sqlite3.Row]:
    """Записи клиента начиная с указанного дня, по порядку."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, service, date, time FROM bookings"
            " WHERE user_id = ? AND date >= ? ORDER BY date, time",
            (user_id, from_day),
        ).fetchall()


def delete_booking(booking_id: int, user_id: int) -> sqlite3.Row | None:
    """Удаляет запись клиента и возвращает её. None — если такой записи уже нет."""
    with connect() as conn:
        booking = conn.execute(
            "SELECT id, service, date, time FROM bookings WHERE id = ? AND user_id = ?",
            (booking_id, user_id),
        ).fetchone()
        if booking is not None:
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return booking


def bookings_between(from_day: str, to_day: str) -> list[sqlite3.Row]:
    """Все записи с from_day по to_day включительно, по порядку. Для меню мастера."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, user_id, service, date, time, client_name, client_username FROM bookings"
            " WHERE date BETWEEN ? AND ? ORDER BY date, time",
            (from_day, to_day),
        ).fetchall()


def booking(booking_id: int) -> sqlite3.Row | None:
    """Одна запись по id (для меню мастера). None — если её нет."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, user_id, service, date, time, client_name, client_username FROM bookings"
            " WHERE id = ?",
            (booking_id,),
        ).fetchone()


def cancel_booking(booking_id: int) -> sqlite3.Row | None:
    """Мастер отменяет запись: удаляет её и возвращает (вместе с user_id клиента).
    None — если такой записи уже нет."""
    with connect() as conn:
        booking = conn.execute(
            "SELECT id, user_id, service, date, time FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()
        if booking is not None:
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return booking


def closed_times(day: str) -> set[str]:
    """Закрытое время на день: набор «ЧЧ:00», а если закрыт весь день — в наборе есть WHOLE_DAY."""
    with connect() as conn:
        rows = conn.execute("SELECT time FROM closed_time WHERE date = ?", (day,)).fetchall()
    return {row["time"] for row in rows}


def close_whole_day(day: str) -> None:
    """Закрывает весь день. Отдельно закрытые часы этого дня больше не нужны — удаляем их."""
    with connect() as conn:
        conn.execute("DELETE FROM closed_time WHERE date = ?", (day,))
        conn.execute("INSERT INTO closed_time (date, time) VALUES (?, ?)", (day, WHOLE_DAY))


def set_closed_hours(day: str, times: list[str]) -> None:
    """Делает закрытыми на этот день ровно эти часы (ранее закрытые, но не отмеченные — открываются)."""
    with connect() as conn:
        conn.execute("DELETE FROM closed_time WHERE date = ?", (day,))
        conn.executemany(
            "INSERT INTO closed_time (date, time) VALUES (?, ?)",
            [(day, time) for time in times],
        )


def closed_from(from_day: str) -> list[sqlite3.Row]:
    """Всё закрытое время начиная с указанного дня, по порядку (WHOLE_DAY идёт первым в дне)."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, date, time FROM closed_time WHERE date >= ? ORDER BY date, time",
            (from_day,),
        ).fetchall()


def open_time(closed_id: int) -> bool:
    """Открывает закрытый день или час. False — если его уже открыли."""
    with connect() as conn:
        cursor = conn.execute("DELETE FROM closed_time WHERE id = ?", (closed_id,))
    return cursor.rowcount == 1
