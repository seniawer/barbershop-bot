"""Хранение записей клиентов в базе SQLite (файл barbershop.db рядом с ботом)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("barbershop.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Открывает базу, сохраняет изменения и закрывает её после блока with."""
    conn = sqlite3.connect(DB_PATH)
    # Строки из базы можно читать по имени колонки: row["date"]
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # при успехе — сохранить (commit), при ошибке — откатить
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицу при первом запуске. Если она уже есть — ничего не делает."""
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


def add_booking(user_id: int, service: str, day: str, time: str) -> bool:
    """Сохраняет запись. Возвращает False, если это время уже кто-то занял."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO bookings (user_id, service, date, time) VALUES (?, ?, ?, ?)",
                (user_id, service, day, time),
            )
        return True
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
