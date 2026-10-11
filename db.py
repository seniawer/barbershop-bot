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
        # Отметки напоминаний (1 — отправлено или не нужно) и подтверждение визита клиентом.
        # Хранятся в базе, чтобы после перезапуска бот не прислал напоминание повторно
        for column in ("reminded_24h", "reminded_2h", "confirmed"):
            if column not in columns:
                conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        # Телефоны клиентов. phone_asked = 1 — номер уже спрашивали (повторно не спрашиваем)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                user_id     INTEGER PRIMARY KEY,
                phone       TEXT,
                phone_asked INTEGER NOT NULL DEFAULT 0
            )
            """
        )
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
    skip_24h: bool = False,
    skip_2h: bool = False,
) -> bool:
    """Сохраняет запись. Возвращает False, если это время уже кто-то занял или мастер его закрыл.
    skip_24h / skip_2h — сразу отметить напоминание как ненужное (до записи осталось мало времени)."""
    try:
        with connect() as conn:
            # Проверка закрытого времени — в том же запросе, что и вставка:
            # если мастер закроет час в ту же секунду, запись всё равно не пройдёт
            cursor = conn.execute(
                "INSERT INTO bookings"
                " (user_id, service, date, time, client_name, client_username, reminded_24h, reminded_2h)"
                " SELECT ?, ?, ?, ?, ?, ?, ?, ?"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM closed_time WHERE date = ? AND time IN (?, ?)"
                " )",
                (
                    user_id, service, day, time, client_name, client_username,
                    int(skip_24h), int(skip_2h), day, time, WHOLE_DAY,
                ),
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


# Запись вместе с телефоном клиента (если он им поделился) — для мастера и напоминаний
BOOKING_COLUMNS = (
    "b.id, b.user_id, b.service, b.date, b.time, b.client_name, b.client_username,"
    " b.reminded_24h, b.reminded_2h, b.confirmed, c.phone"
)
BOOKINGS_WITH_PHONE = "bookings b LEFT JOIN clients c ON c.user_id = b.user_id"


def bookings_between(from_day: str, to_day: str) -> list[sqlite3.Row]:
    """Все записи с from_day по to_day включительно, по порядку. Для меню мастера."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE}"
            " WHERE date BETWEEN ? AND ? ORDER BY date, time",
            (from_day, to_day),
        ).fetchall()


def booking(booking_id: int) -> sqlite3.Row | None:
    """Одна запись по id (для меню мастера). None — если её нет."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE} WHERE b.id = ?",
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


def bookings_to_remind(from_day: str, to_day: str) -> list[sqlite3.Row]:
    """Записи с from_day по to_day, по которым отправлены ещё не все напоминания."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE}"
            " WHERE date BETWEEN ? AND ? AND (reminded_24h = 0 OR reminded_2h = 0)"
            " ORDER BY date, time",
            (from_day, to_day),
        ).fetchall()


def mark_reminded(booking_id: int, column: str) -> bool:
    """Отмечает напоминание (column — reminded_24h или reminded_2h) как отправленное.
    False — если оно уже было отмечено: значит, отправлять его не нужно."""
    if column not in ("reminded_24h", "reminded_2h"):
        raise ValueError(column)
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE bookings SET {column} = 1 WHERE id = ? AND {column} = 0", (booking_id,)
        )
    return cursor.rowcount == 1


def user_booking(booking_id: int, user_id: int) -> sqlite3.Row | None:
    """Запись по id, если она принадлежит этому клиенту. None — если её нет."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE} WHERE b.id = ? AND b.user_id = ?",
            (booking_id, user_id),
        ).fetchone()


def confirm_booking(booking_id: int, user_id: int) -> bool:
    """Клиент подтвердил визит. False — если подтвердил уже раньше (или записи нет)."""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE bookings SET confirmed = 1 WHERE id = ? AND user_id = ? AND confirmed = 0",
            (booking_id, user_id),
        )
    return cursor.rowcount == 1


def client_phone(user_id: int) -> str | None:
    """Телефон клиента. None — если он им не поделился."""
    with connect() as conn:
        row = conn.execute("SELECT phone FROM clients WHERE user_id = ?", (user_id,)).fetchone()
    return row["phone"] if row else None


def should_ask_phone(user_id: int) -> bool:
    """Нужно ли спросить номер: его ещё нет и раньше не спрашивали.
    Сразу отмечает, что спросили, — второй раз бот номер не попросит."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO clients (user_id) VALUES (?)", (user_id,))
        cursor = conn.execute(
            "UPDATE clients SET phone_asked = 1"
            " WHERE user_id = ? AND phone_asked = 0 AND phone IS NULL",
            (user_id,),
        )
    return cursor.rowcount == 1


def save_phone(user_id: int, phone: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO clients (user_id, phone, phone_asked) VALUES (?, ?, 1)"
            " ON CONFLICT (user_id) DO UPDATE SET phone = excluded.phone, phone_asked = 1",
            (user_id, phone),
        )
