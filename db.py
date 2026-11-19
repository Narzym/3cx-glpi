"""
db.py - cała praca z bazą SQLite w jednym miejscu.

Co tu jest:
  - połączenie z bazą (tryb WAL, timeout na zajętą bazę) i bezpieczny "db_session",
  - definicje tabel i ich migracje (dodawanie brakujących kolumn),
  - ustawienia (tabela settings), rekordy CDR, statystyki.

Zasada: żeby coś zrobić w bazie, otwieramy "with db_session() as conn:".
Po wyjściu z bloku "with" zmiany same się zapisują (commit), a przy błędzie same
się cofają (rollback). Połączenie zawsze jest zamykane.
"""

import sqlite3
from contextlib import contextmanager

import config as cfg
from logger import log
from utils import get_now_string, truncate_text


# =========================
# POŁĄCZENIE
# =========================

def is_wal_enabled():
    return str(cfg.DB_JOURNAL_MODE).strip().upper() == "WAL"


def db_connect():
    """
    Otwiera nowe połączenie z bazą.

    timeout: jak długo (w sekundach) czekać, gdy ktoś inny akurat zapisuje do bazy,
    zamiast od razu rzucać błędem "database is locked". Ta wartość ustawia
    w SQLite tzw. busy_timeout.
    """

    conn = sqlite3.connect(cfg.DB_PATH, timeout=cfg.DB_BUSY_TIMEOUT_SECONDS)

    if is_wal_enabled():
        # W trybie WAL to zalecane ustawienie: dużo szybsze zapisy, nadal bezpieczne.
        conn.execute("PRAGMA synchronous=NORMAL")

    return conn


@contextmanager
def db_session():
    """
    Bezpieczna sesja bazy: commit po sukcesie, rollback po błędzie, zawsze close.

    Użycie:
        with db_session() as conn:
            conn.execute("INSERT ...")
    """

    conn = db_connect()

    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_journal_mode(conn):
    """
    WAL (Write-Ahead Logging): czytanie i zapisywanie nie blokują się nawzajem.
    To ustawienie zapisuje się w pliku bazy na stałe, więc wystarczy raz przy starcie.
    """

    mode = "WAL" if is_wal_enabled() else "DELETE"
    row = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
    return row[0] if row else None


# =========================
# TABELE
# =========================

def create_it_cdr_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS it_cdr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            callid TEXT,
            duration TEXT,
            "from-no" TEXT,
            "from-dispname" TEXT,
            "to-no" TEXT,
            "final-dn" TEXT,
            "final-dispname" TEXT,
            zrobiony INTEGER NOT NULL DEFAULT 0,
            glpi_ticket_id TEXT,
            glpi_status_code INTEGER,
            glpi_error TEXT,
            glpi_location_id TEXT
        )
    ''')


def create_glpi_users_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS glpi_users (
            glpi_user_id INTEGER PRIMARY KEY,
            email TEXT,
            full_name TEXT,
            first_name TEXT,
            last_name TEXT
        )
    ''')


def create_glpi_locations_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS glpi_locations (
            glpi_location_id INTEGER PRIMARY KEY,
            room_number TEXT,
            name TEXT,
            full_path TEXT
        )
    ''')


def create_glpi_phones_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS glpi_phones (
            glpi_phone_id INTEGER PRIMARY KEY,
            dn TEXT NOT NULL,
            phone_name TEXT,
            glpi_user_id INTEGER,
            updated_at TEXT
        )
    ''')


def create_settings_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')


def get_table_columns(cursor, table_name):
    try:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}
    except sqlite3.Error:
        return set()


def add_column_if_missing(cursor, table_name, column_name, column_definition):
    columns = get_table_columns(cursor, table_name)

    if column_name in columns:
        return False

    cursor.execute(f'ALTER TABLE {table_name} ADD COLUMN "{column_name}" {column_definition}')
    return True


def ensure_it_cdr_schema(cursor):
    # Nie migrujemy danych, ale dodajemy brakujace kolumny, zeby stara tabela
    # nie wysypala listenera po update kodu. Stare kolumny moga sobie zostac.
    expected_columns = {
        "created_at": "TEXT",
        "callid": "TEXT",
        "duration": "TEXT",
        "from-no": "TEXT",
        "from-dispname": "TEXT",
        "to-no": "TEXT",
        "final-dn": "TEXT",
        "final-dispname": "TEXT",
        "zrobiony": "INTEGER NOT NULL DEFAULT 0",
        "glpi_ticket_id": "TEXT",
        "glpi_status_code": "INTEGER",
        "glpi_error": "TEXT",
        "glpi_location_id": "TEXT",
    }

    for column_name, column_definition in expected_columns.items():
        try:
            add_column_if_missing(cursor, "it_cdr", column_name, column_definition)
        except sqlite3.Error as e:
            log("DB", f"Nie udalo sie dodac kolumny it_cdr.{column_name}: {type(e).__name__}: {e}")


def ensure_glpi_users_schema(cursor):
    expected_columns = {
        "email": "TEXT",
        "full_name": "TEXT",
        "first_name": "TEXT",
        "last_name": "TEXT",
    }

    for column_name, column_definition in expected_columns.items():
        try:
            add_column_if_missing(cursor, "glpi_users", column_name, column_definition)
        except sqlite3.Error as e:
            log("DB", f"Nie udalo sie dodac kolumny glpi_users.{column_name}: {type(e).__name__}: {e}")


def normalize_cdr_limit_value(value):
    value = str(value).strip().lower()

    if value in ("unlimited", "bez limitu", "none", "0"):
        return "unlimited"

    limit = int(value)

    if limit < 10:
        raise ValueError("Limit rekordow CDR musi byc >= 10 albo unlimited")

    return str(limit)


def bool_to_setting(value):
    return "1" if bool(value) else "0"


LOG_OPTION_DEFAULTS = {
    "log_cdr_payload": bool_to_setting(getattr(cfg, "LOG_CDR_PAYLOAD_DEFAULT", False)),
    "log_glpi_user_sample_payload": bool_to_setting(getattr(cfg, "LOG_GLPI_USER_SAMPLE_PAYLOAD_DEFAULT", False)),
    "log_glpi_location_sample_payload": bool_to_setting(getattr(cfg, "LOG_GLPI_LOCATION_SAMPLE_PAYLOAD_DEFAULT", False)),
}


def init_db():
    with db_session() as conn:
        apply_journal_mode(conn)

        cursor = conn.cursor()

        create_it_cdr_table(cursor)
        ensure_it_cdr_schema(cursor)
        create_settings_table(cursor)
        create_glpi_users_table(cursor)
        ensure_glpi_users_schema(cursor)
        create_glpi_locations_table(cursor)
        create_glpi_phones_table(cursor)

        default_limit = normalize_cdr_limit_value(cfg.CDR_MAX_RECORDS_DEFAULT)
        cursor.execute('''
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('cdr_max_records', ?)
        ''', (default_limit,))

        for key, default_value in LOG_OPTION_DEFAULTS.items():
            cursor.execute('''
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?)
            ''', (key, default_value))


# =========================
# USTAWIENIA
# =========================

def get_setting(key, default_value=None):
    with db_session() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()

    if not row:
        return default_value

    return row[0]


def set_setting(key, value):
    with db_session() as conn:
        conn.execute('''
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''', (key, str(value)))


def setting_to_bool(value, default=False):
    if value is None:
        return default

    return str(value).strip().lower() in ("1", "true", "yes", "tak", "on", "wlaczone")


def get_bool_setting(key, default=False):
    return setting_to_bool(get_setting(key, bool_to_setting(default)), default)


def set_bool_setting(key, value):
    set_setting(key, bool_to_setting(value))
    return get_bool_setting(key, False)


def toggle_bool_setting(key):
    current = get_bool_setting(key, False)
    return set_bool_setting(key, not current)


def is_verbose_log_enabled(key):
    default = setting_to_bool(LOG_OPTION_DEFAULTS.get(key), False)
    return get_bool_setting(key, default)


def get_log_options_state():
    return {key: is_verbose_log_enabled(key) for key in LOG_OPTION_DEFAULTS}


def toggle_log_option(key):
    if key not in LOG_OPTION_DEFAULTS:
        raise ValueError(f"Nieznana opcja logow: {key}")

    return toggle_bool_setting(key)


# =========================
# REKORDY CDR
# =========================

def get_cdr_max_records():
    value = get_setting("cdr_max_records", cfg.CDR_MAX_RECORDS_DEFAULT)
    value = normalize_cdr_limit_value(value)

    if value == "unlimited":
        return None

    return int(value)


def get_cdr_max_records_label():
    limit = get_cdr_max_records()

    if limit is None:
        return "unlimited"

    return str(limit)


def set_cdr_max_records(value):
    normalized = normalize_cdr_limit_value(value)
    set_setting("cdr_max_records", normalized)
    cleanup_it_cdr_limit()
    return normalized


def cleanup_it_cdr_limit():
    limit = get_cdr_max_records()

    if limit is None:
        return 0

    with db_session() as conn:
        before = conn.execute("SELECT COUNT(*) FROM it_cdr").fetchone()[0]

        cursor = conn.execute('''
            DELETE FROM it_cdr
            WHERE id NOT IN (
                SELECT id
                FROM it_cdr
                ORDER BY id DESC
                LIMIT ?
            )
        ''', (limit,))

        deleted = cursor.rowcount if cursor.rowcount is not None else 0

    if deleted > 0:
        log("DB", f"Limit CDR={limit}. Usunieto najstarsze rekordy: {deleted}. Bylo: {before}")

    return deleted


def save_it_cdr(cdr: dict):
    with db_session() as conn:
        cursor = conn.execute('''
            INSERT INTO it_cdr (
                created_at,
                callid,
                duration,
                "from-no",
                "from-dispname",
                "to-no",
                "final-dn",
                "final-dispname",
                zrobiony
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            get_now_string(),
            cdr["callid"],
            cdr["duration"],
            cdr["from_no"],
            cdr["from_dispname"],
            cdr["to_no"],
            cdr["final_dn"],
            cdr["final_dispname"],
            0
        ))

        record_id = cursor.lastrowid

    cleanup_it_cdr_limit()

    return record_id


def mark_as_done(record_id, ticket_id, status_code, location_id=None):
    with db_session() as conn:
        conn.execute('''
            UPDATE it_cdr
            SET zrobiony = 1,
                glpi_ticket_id = ?,
                glpi_status_code = ?,
                glpi_error = NULL,
                glpi_location_id = ?
            WHERE id = ?
        ''', (
            str(ticket_id),
            status_code,
            str(location_id) if location_id else None,
            record_id
        ))


def mark_as_failed(record_id, status_code, error):
    with db_session() as conn:
        conn.execute('''
            UPDATE it_cdr
            SET zrobiony = 0,
                glpi_status_code = ?,
                glpi_error = ?
            WHERE id = ?
        ''', (
            status_code,
            truncate_text(error, 3500),
            record_id
        ))


# =========================
# RESETY (zerowanie tabel z panelu admina)
# =========================

def reset_it_cdr():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS it_cdr")
        create_it_cdr_table(cursor)


def reset_glpi_users():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS glpi_users")
        create_glpi_users_table(cursor)


def reset_glpi_locations():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS glpi_locations")
        create_glpi_locations_table(cursor)


def reset_glpi_phones():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS glpi_phones")
        create_glpi_phones_table(cursor)
        cursor.execute("DELETE FROM settings WHERE key = 'glpi_phones_last_sync'")


# =========================
# STATYSTYKI
# =========================

STATS_TABLES = {
    "cdr_count": "it_cdr",
    "glpi_users_count": "glpi_users",
    "glpi_locations_count": "glpi_locations",
    "glpi_phones_count": "glpi_phones",
}


def get_db_stats():
    stats = {}

    with db_session() as conn:
        for key, table in STATS_TABLES.items():
            try:
                stats[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                stats[key] = "schema?"

    stats["cdr_max_records"] = get_cdr_max_records_label()

    return stats
