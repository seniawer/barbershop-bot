"""Хранение записей клиентов и закрытого времени в базе SQLite (по умолчанию файл barbershop.db рядом с ботом)."""

import logging
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


# Схемы таблиц с мастером. {name} — имя таблицы: при миграции сначала создаётся копия.
# UNIQUE (master_id, date, time): база сама не даст записать двух клиентов к одному мастеру на одно время
BOOKINGS_SCHEMA = """
    CREATE TABLE {name} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        service         TEXT    NOT NULL,
        master_id       TEXT    NOT NULL,
        date            TEXT    NOT NULL,
        time            TEXT    NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        client_name     TEXT,
        client_username TEXT,
        reminded_24h    INTEGER NOT NULL DEFAULT 0,
        reminded_2h     INTEGER NOT NULL DEFAULT 0,
        confirmed       INTEGER NOT NULL DEFAULT 0,
        UNIQUE (master_id, date, time)
    )
"""
# Время, которое мастер закрыл для записи. time — «ЧЧ:00» или WHOLE_DAY (весь день)
CLOSED_TIME_SCHEMA = """
    CREATE TABLE {name} (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        master_id TEXT    NOT NULL,
        date      TEXT    NOT NULL,
        time      TEXT    NOT NULL,
        UNIQUE (master_id, date, time)
    )
"""


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db(default_master: str) -> None:
    """Создаёт таблицы при первом запуске и обновляет старую базу.
    default_master — мастер, к которому привязываются записи и закрытое время из базы,
    созданной до появления нескольких мастеров."""
    # Папку для базы создаём заранее, если в DB_PATH указана ещё несуществующая
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(BOOKINGS_SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS").format(name="bookings"))
        conn.execute(
            CLOSED_TIME_SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS").format(name="closed_time")
        )
        # Имя и ник клиента, отметки напоминаний (1 — отправлено или не нужно) и подтверждение
        # визита появились позже, чем таблица. В старой базе добавляем колонки,
        # уже сохранённые записи при этом не меняются
        columns = table_columns(conn, "bookings")
        # comment — необязательный комментарий клиента к записи (из мини-приложения)
        for column in ("client_name", "client_username", "comment"):
            if column not in columns:
                conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} TEXT")
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
    migrate_to_masters(default_master)


def migrate_to_masters(default_master: str) -> None:
    """Добавляет мастера в старые таблицы bookings и closed_time.

    Нужно поменять UNIQUE (date, time) на UNIQUE (master_id, date, time), а SQLite не умеет менять
    ограничения у существующей таблицы. Поэтому таблица пересоздаётся: новая копия → перенос строк
    (с теми же id) → удаление старой → переименование. Всё в одной транзакции: если что-то пойдёт
    не так, база останется как была."""
    # isolation_level=None — модуль sqlite3 сам не открывает и не закрывает транзакции,
    # BEGIN и COMMIT пишем явно, чтобы CREATE/DROP/ALTER точно попали в одну транзакцию
    conn = sqlite3.connect(db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, schema in (("bookings", BOOKINGS_SCHEMA), ("closed_time", CLOSED_TIME_SCHEMA)):
                old_columns = table_columns(conn, table)
                if "master_id" in old_columns:
                    continue
                copied = ", ".join(sorted(old_columns))
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
                conn.execute(schema.format(name=f"{table}_new"))
                conn.execute(
                    f"INSERT INTO {table}_new ({copied}, master_id) SELECT {copied}, ? FROM {table}",
                    (default_master,),
                )
                if conn.execute(f"SELECT COUNT(*) FROM {table}_new").fetchone()[0] != count:
                    raise RuntimeError(f"Миграция {table}: перенесены не все строки")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
                # Счётчик id оставляем прежним: id удалённых записей не должны достаться новым
                # (на них могут ссылаться кнопки в старых сообщениях)
                if seq is not None:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
                    conn.execute(
                        "INSERT INTO sqlite_sequence (name, seq)"
                        f" SELECT ?, MAX(?, IFNULL((SELECT MAX(id) FROM {table}), 0))",
                        (table, seq["seq"]),
                    )
                logging.info("База: %s строк в %s привязано к мастеру %s", count, table, default_master)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def add_booking(
    user_id: int,
    service: str,
    master_id: str,
    day: str,
    time: str,
    client_name: str | None = None,
    client_username: str | None = None,
    skip_24h: bool = False,
    skip_2h: bool = False,
    comment: str | None = None,
) -> bool:
    """Сохраняет запись к мастеру. Возвращает False, если это время у мастера уже занято или закрыто.
    skip_24h / skip_2h — сразу отметить напоминание как ненужное (до записи осталось мало времени)."""
    try:
        with connect() as conn:
            # Проверка закрытого времени — в том же запросе, что и вставка:
            # если мастер закроет час в ту же секунду, запись всё равно не пройдёт
            cursor = conn.execute(
                "INSERT INTO bookings"
                " (user_id, service, master_id, date, time, client_name, client_username, reminded_24h, reminded_2h,"
                " comment)"
                " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM closed_time WHERE master_id = ? AND date = ? AND time IN (?, ?)"
                " )",
                (
                    user_id, service, master_id, day, time, client_name, client_username,
                    int(skip_24h), int(skip_2h), comment, master_id, day, time, WHOLE_DAY,
                ),
            )
        return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        return False


def busy_times(master_id: str, day: str) -> set[str]:
    """Время, которое у мастера на этот день уже занято."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT time FROM bookings WHERE master_id = ? AND date = ?", (master_id, day)
        ).fetchall()
    return {row["time"] for row in rows}


def user_bookings(user_id: int, from_day: str) -> list[sqlite3.Row]:
    """Записи клиента начиная с указанного дня, по порядку."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, service, master_id, date, time FROM bookings"
            " WHERE user_id = ? AND date >= ? ORDER BY date, time",
            (user_id, from_day),
        ).fetchall()


def delete_booking(booking_id: int, user_id: int) -> sqlite3.Row | None:
    """Удаляет запись клиента и возвращает её. None — если такой записи уже нет."""
    with connect() as conn:
        booking = conn.execute(
            "SELECT id, service, master_id, date, time FROM bookings WHERE id = ? AND user_id = ?",
            (booking_id, user_id),
        ).fetchone()
        if booking is not None:
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return booking


# Запись вместе с телефоном клиента (если он им поделился) — для мастера и напоминаний
BOOKING_COLUMNS = (
    "b.id, b.user_id, b.service, b.master_id, b.date, b.time, b.client_name, b.client_username,"
    " b.reminded_24h, b.reminded_2h, b.confirmed, b.comment, c.phone"
)
BOOKINGS_WITH_PHONE = "bookings b LEFT JOIN clients c ON c.user_id = b.user_id"


def in_list(column: str, values: list[str]) -> str:
    """Условие «column IN (?, ?, ...)» для списка значений (пустой список — ни одной строки)."""
    return f"{column} IN ({', '.join('?' * len(values))})"


def bookings_between(from_day: str, to_day: str, master_ids: list[str]) -> list[sqlite3.Row]:
    """Записи к этим мастерам с from_day по to_day включительно, по порядку. Для меню мастера."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE}"
            f" WHERE b.date BETWEEN ? AND ? AND {in_list('b.master_id', master_ids)}"
            " ORDER BY b.date, b.time, b.master_id",
            (from_day, to_day, *master_ids),
        ).fetchall()


def bookings_count(master_id: str, day: str) -> int:
    """Сколько записей у мастера на день (чтобы «Любой мастер» выбирал менее занятого)."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE master_id = ? AND date = ?", (master_id, day)
        ).fetchone()[0]


def booking(booking_id: int) -> sqlite3.Row | None:
    """Одна запись по id (для меню мастера). None — если её нет."""
    with connect() as conn:
        return conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM {BOOKINGS_WITH_PHONE} WHERE b.id = ?",
            (booking_id,),
        ).fetchone()


def cancel_booking(booking_id: int, master_ids: list[str]) -> sqlite3.Row | None:
    """Мастер отменяет запись: удаляет её и возвращает (вместе с user_id клиента).
    Удаляется, только если запись к одному из master_ids. None — если такой записи нет."""
    with connect() as conn:
        booking = conn.execute(
            "SELECT id, user_id, service, master_id, date, time FROM bookings"
            f" WHERE id = ? AND {in_list('master_id', master_ids)}",
            (booking_id, *master_ids),
        ).fetchone()
        if booking is not None:
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return booking


def closed_times(master_id: str, day: str) -> set[str]:
    """Закрытое время мастера на день: набор «ЧЧ:00», а если закрыт весь день — в наборе есть WHOLE_DAY."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT time FROM closed_time WHERE master_id = ? AND date = ?", (master_id, day)
        ).fetchall()
    return {row["time"] for row in rows}


def close_whole_day(master_id: str, day: str) -> None:
    """Закрывает мастеру весь день. Отдельно закрытые часы этого дня больше не нужны — удаляем их."""
    with connect() as conn:
        conn.execute("DELETE FROM closed_time WHERE master_id = ? AND date = ?", (master_id, day))
        conn.execute(
            "INSERT INTO closed_time (master_id, date, time) VALUES (?, ?, ?)", (master_id, day, WHOLE_DAY)
        )


def set_closed_hours(master_id: str, day: str, times: list[str]) -> None:
    """Делает закрытыми у мастера на этот день ровно эти часы (ранее закрытые, но не отмеченные — открываются)."""
    with connect() as conn:
        conn.execute("DELETE FROM closed_time WHERE master_id = ? AND date = ?", (master_id, day))
        conn.executemany(
            "INSERT INTO closed_time (master_id, date, time) VALUES (?, ?, ?)",
            [(master_id, day, time) for time in times],
        )


def closed_from(from_day: str, master_ids: list[str]) -> list[sqlite3.Row]:
    """Закрытое время этих мастеров начиная с указанного дня, по порядку (WHOLE_DAY идёт первым в дне)."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, master_id, date, time FROM closed_time"
            f" WHERE date >= ? AND {in_list('master_id', master_ids)} ORDER BY master_id, date, time",
            (from_day, *master_ids),
        ).fetchall()


def open_time(closed_id: int, master_ids: list[str]) -> bool:
    """Открывает закрытый день или час, если он у одного из master_ids. False — если его нет (уже открыли)."""
    with connect() as conn:
        cursor = conn.execute(
            f"DELETE FROM closed_time WHERE id = ? AND {in_list('master_id', master_ids)}",
            (closed_id, *master_ids),
        )
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
