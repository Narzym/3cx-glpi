import socket
import time
import sqlite3
import csv
import os
import errno
import re
import json
import unicodedata
import requests
from io import StringIO
from datetime import datetime

import config as cfg


# Jesli API user nie ma prawa do Location, nie probujemy pobierac lokalizacji online
# przy kazdym polaczeniu. Panel nadal moze sprobowac update po poprawie uprawnien.
GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = False


# =========================
# POMOCNICZE
# =========================

class GLPIRequestError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_log_timestamp():
    return datetime.now(cfg.TIMEZONE).strftime("%y-%m-%d %H:%M:%S")


def write_log_to_file(log_line):
    try:
        log_dir = os.path.dirname(cfg.LOG_PATH)

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        with open(cfg.LOG_PATH, "a", encoding="utf-8") as file:
            file.write(log_line + "\n")

    except Exception:
        pass


def log(tag, message):
    message = str(message)

    if "\n" in message:
        for line in message.splitlines():
            log_line = f"{get_log_timestamp()} [{tag}] {line}"
            print(log_line, flush=True)
            write_log_to_file(log_line)
    else:
        log_line = f"{get_log_timestamp()} [{tag}] {message}"
        print(log_line, flush=True)
        write_log_to_file(log_line)


def truncate_text(value, limit=3000):
    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + f"\n... [ucieto, calosc miala {len(value)} znakow]"


def glpi_url(path):
    if not cfg.GLPI_API_URL:
        raise ValueError("Brakuje GLPI_API_URL w .env")

    return cfg.GLPI_API_URL.rstrip("/") + "/" + path.lstrip("/")


def check_glpi_env():
    required = {
        "GLPI_API_URL": cfg.GLPI_API_URL,
        "GLPI_APP_TOKEN": cfg.GLPI_APP_TOKEN,
        "GLPI_USER_TOKEN": cfg.GLPI_USER_TOKEN
    }

    for key, value in required.items():
        if not value:
            raise ValueError(f"Brakuje {key} w .env")


def get_response_text(response):
    if response is None:
        return "Brak odpowiedzi"

    return truncate_text(response.text, 1500)


def response_has_glpi_error(response, code):
    text = get_response_text(response)
    return str(code) in text


def build_glpi_right_missing_message(itemtype, response):
    return (
        f"GLPI {itemtype} fetch error. HTTP 403 Forbidden. "
        f"API user/token nie ma prawa odczytu do itemtype '{itemtype}'. "
        "To nie wyglada na zly endpoint, tylko na brak uprawnien profilu uzytkownika API. "
        "W GLPI nadaj profilowi tego usera prawo Odczyt/Read do Lokalizacji/List rozwijanych, "
        "albo uzyj tokena usera z takim prawem. "
        f"Body: {get_response_text(response)}"
    )


def get_now_string():
    return datetime.now(cfg.TIMEZONE).strftime("%d-%m-%Y %H:%M:%S")


def is_missed_call_ticket_time():
    now = datetime.now(cfg.TIMEZONE)
    return cfg.MISSED_CALL_START_HOUR <= now.hour < cfg.MISSED_CALL_END_HOUR


def duration_to_seconds(duration):
    duration = str(duration).strip()

    if not duration:
        return None

    try:
        if ":" not in duration:
            return int(float(duration))

        parts = [int(part.strip()) for part in duration.split(":")]

        if len(parts) == 3:
            hours, minutes, seconds = parts
            return hours * 3600 + minutes * 60 + seconds

        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds

        return None

    except Exception:
        return None


def is_zero_duration(duration):
    seconds = duration_to_seconds(duration)

    if seconds is None:
        return False

    return seconds == 0


def is_missed_call_cdr(cdr):
    """
    Nieodebrane rozpoznajemy po:
    1. final-dn jest jednym z naszych numerow IT, np. 517 albo 225435517
    2. duration = 0

    Awaryjnie, jesli final-dn bedzie puste, sprawdzamy tez to-no.
    """

    final_dn = cdr.get("final_dn")
    to_no = cdr.get("to_no")

    target_number = final_dn or to_no

    return target_number in cfg.MISSED_CALL_FINAL_DNS and is_zero_duration(cdr["duration"])


def get_connection_duration(start_time):
    if not start_time:
        return "brak danych"

    seconds = int((datetime.now(cfg.TIMEZONE) - start_time).total_seconds())
    return f"{seconds}s"


def describe_connection_error(error, connection_started_at=None):
    duration = get_connection_duration(connection_started_at)

    if isinstance(error, ConnectionError) and getattr(error, "errno", None) is None:
        return f"Polaczenie zamkniete normalnie przez druga strone albo urzadzenie po drodze. Czas polaczenia: {duration}. Error: {error}"

    if isinstance(error, ConnectionResetError):
        return f"Polaczenie zostalo zerwane resetem TCP. Czas polaczenia: {duration}. Error: {error}"

    if isinstance(error, ConnectionAbortedError):
        return f"Polaczenie zostalo przerwane lokalnie albo przez system. Czas polaczenia: {duration}. Error: {error}"

    if isinstance(error, TimeoutError):
        return f"Timeout polaczenia albo operacji socketu. Czas polaczenia: {duration}. Error: {error}"

    if isinstance(error, BrokenPipeError):
        return f"Broken pipe, druga strona zamknela polaczenie podczas uzywania socketu. Czas polaczenia: {duration}. Error: {error}"

    if isinstance(error, OSError):
        if error.errno == errno.ECONNRESET:
            return f"ECONNRESET, polaczenie zresetowane przez druga strone. Czas polaczenia: {duration}. Error: {error}"

        if error.errno == errno.ETIMEDOUT:
            return f"ETIMEDOUT, timeout TCP. Czas polaczenia: {duration}. Error: {error}"

        if error.errno == errno.EPIPE:
            return f"EPIPE, socket zamkniety po drugiej stronie. Czas polaczenia: {duration}. Error: {error}"

        if error.errno == errno.ENETUNREACH:
            return f"ENETUNREACH, siec nieosiagalna. Czas polaczenia: {duration}. Error: {error}"

        if error.errno == errno.EHOSTUNREACH:
            return f"EHOSTUNREACH, host nieosiagalny. Czas polaczenia: {duration}. Error: {error}"

        if error.errno == errno.ECONNREFUSED:
            return f"ECONNREFUSED, host odrzucil polaczenie. Czas polaczenia: {duration}. Error: {error}"

        return f"OSError errno={error.errno}. Czas polaczenia: {duration}. Error: {error}"

    return f"{type(error).__name__}. Czas polaczenia: {duration}. Error: {error}"


def configure_tcp_keepalive(sock):
    settings = []

    sock.settimeout(None)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    settings.append("SO_KEEPALIVE=1")

    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, cfg.TCP_KEEPALIVE_IDLE)
        settings.append(f"TCP_KEEPIDLE={cfg.TCP_KEEPALIVE_IDLE}")

    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, cfg.TCP_KEEPALIVE_INTERVAL)
        settings.append(f"TCP_KEEPINTVL={cfg.TCP_KEEPALIVE_INTERVAL}")

    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, cfg.TCP_KEEPALIVE_COUNT)
        settings.append(f"TCP_KEEPCNT={cfg.TCP_KEEPALIVE_COUNT}")

    if hasattr(socket, "TCP_USER_TIMEOUT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, cfg.TCP_USER_TIMEOUT_MS)
        settings.append(f"TCP_USER_TIMEOUT={cfg.TCP_USER_TIMEOUT_MS}ms")

    return ", ".join(settings)


# =========================
# BAZA DANYCH
# =========================

def db_connect():
    return sqlite3.connect(cfg.DB_PATH)


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
    conn = db_connect()
    cursor = conn.cursor()

    create_it_cdr_table(cursor)
    ensure_it_cdr_schema(cursor)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    create_glpi_users_table(cursor)
    ensure_glpi_users_schema(cursor)
    create_glpi_locations_table(cursor)

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

    conn.commit()
    conn.close()


def get_setting(key, default_value=None):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()

    conn.close()

    if not row:
        return default_value

    return row[0]


def set_setting(key, value):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    ''', (key, str(value)))

    conn.commit()
    conn.close()


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

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM it_cdr")
    before = cursor.fetchone()[0]

    cursor.execute('''
        DELETE FROM it_cdr
        WHERE id NOT IN (
            SELECT id
            FROM it_cdr
            ORDER BY id DESC
            LIMIT ?
        )
    ''', (limit,))

    deleted = cursor.rowcount if cursor.rowcount is not None else 0

    conn.commit()
    conn.close()

    if deleted > 0:
        log("DB", f"Limit CDR={limit}. Usunieto najstarsze rekordy: {deleted}. Bylo: {before}")

    return deleted


def reset_it_cdr():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS it_cdr")
    create_it_cdr_table(cursor)

    conn.commit()
    conn.close()


def reset_glpi_users():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS glpi_users")
    create_glpi_users_table(cursor)

    conn.commit()
    conn.close()


def reset_glpi_locations():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS glpi_locations")
    create_glpi_locations_table(cursor)

    conn.commit()
    conn.close()


def save_it_cdr(cdr: dict):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
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

    conn.commit()
    conn.close()

    cleanup_it_cdr_limit()

    return record_id


def mark_as_done(record_id, ticket_id, status_code, location_id=None):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
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

    conn.commit()
    conn.close()


def mark_as_failed(record_id, status_code, error):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
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

    conn.commit()
    conn.close()


def get_db_stats():
    conn = db_connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT COUNT(*) FROM it_cdr")
        cdr_count = cursor.fetchone()[0]
    except sqlite3.Error:
        cdr_count = "schema?"

    try:
        cursor.execute("SELECT COUNT(*) FROM glpi_users")
        glpi_users_count = cursor.fetchone()[0]
    except sqlite3.Error:
        glpi_users_count = "schema?"

    try:
        cursor.execute("SELECT COUNT(*) FROM glpi_locations")
        glpi_locations_count = cursor.fetchone()[0]
    except sqlite3.Error:
        glpi_locations_count = "schema?"

    conn.close()

    return {
        "cdr_count": cdr_count,
        "glpi_users_count": glpi_users_count,
        "glpi_locations_count": glpi_locations_count,
        "cdr_max_records": get_cdr_max_records_label()
    }


# =========================
# PARSOWANIE CDR
# =========================

def parse_cdr_line(line: str):
    """
    Aktualna struktura CDR po przywroceniu final-dn:
    callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname

    from-dn dalej pomijamy, bo nie jest nam potrzebne.
    final-dn wraca, bo jest najlepszy do ownera i missed calls.
    """

    reader = csv.reader(StringIO(line))
    values = next(reader)
    values = [v.strip() for v in values]

    if len(values) < 7:
        raise ValueError(f"Za malo wartosci w CDR: {len(values)}. Oczekiwano 7 pol.")

    if len(values) > 7 and is_verbose_log_enabled("log_cdr_payload"):
        log("CDR DEBUG", f"CDR ma {len(values)} pol, ale parser uzywa pierwszych 7. Sprawdz ustawienia 3CX.")

    callid = values[0]

    if callid.startswith("Call "):
        callid = callid[5:]

    fields = [
        (1, "callid", callid),
        (2, "duration", values[1]),
        (3, "from-no", values[2]),
        (4, "from-dispname", values[3]),
        (5, "to-no", values[4]),
        (6, "final-dn", values[5]),
        (7, "final-dispname", values[6]),
    ]

    cdr = {
        "raw_line": line,
        "raw_values_count": len(values),
        "numbered_fields": [
            {"nr": nr, "field": name, "value": value}
            for nr, name, value in fields
        ],
        "callid": callid,
        "duration": values[1],
        "from_no": values[2],
        "from_dispname": values[3],
        "to_no": values[4],
        "final_dn": values[5],
        "final_dispname": values[6],
    }

    # Aliasy dla reszty logiki, zeby nie robic piruetow w kazdej funkcji.
    cdr["caller_number"] = cdr["from_no"]
    cdr["caller_name"] = cdr["from_dispname"]
    cdr["dialed_number"] = cdr["to_no"]
    cdr["final_number"] = cdr["final_dn"]
    cdr["final_name"] = cdr["final_dispname"]

    return cdr

def log_cdr_debug(cdr):
    if not is_verbose_log_enabled("log_cdr_payload"):
        return

    debug_payload = {
        "raw_line": cdr.get("raw_line"),
        "raw_values_count": cdr.get("raw_values_count"),
        "numbered_fields": cdr.get("numbered_fields"),
        "mapped_for_app": {
            "caller_number/from-no": cdr.get("from_no"),
            "caller_name/from-dispname": cdr.get("from_dispname"),
            "dialed_number/to-no": cdr.get("to_no"),
            "final_number/final-dn": cdr.get("final_dn"),
            "final_name/final-dispname": cdr.get("final_dispname")
        }
    }

    log("CDR DEBUG", "Pelny payload CDR po parsowaniu:")
    log("CDR DEBUG", json.dumps(debug_payload, ensure_ascii=False, indent=2))


# =========================
# GLPI USERS CACHE
# =========================

def normalize_name(value):
    value = str(value or "").strip()
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = " ".join(value.split())
    return value


def token_set(value):
    normalized = normalize_name(value)
    return {token for token in normalized.split() if token}


def names_match(source_name, user_display_name):
    source_normalized = normalize_name(source_name)
    user_normalized = normalize_name(user_display_name)

    if not source_normalized or not user_normalized:
        return False

    if source_normalized == user_normalized:
        return True

    source_tokens = token_set(source_name)
    user_tokens = token_set(user_display_name)

    if len(source_tokens) >= 2 and source_tokens == user_tokens:
        return True

    if len(source_tokens) >= 2 and source_tokens.issubset(user_tokens):
        return True

    if len(user_tokens) >= 2 and user_tokens.issubset(source_tokens):
        return True

    return False


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue

        value = str(value).strip()

        if value:
            return value

    return ""


def looks_like_email(value):
    value = str(value or "").strip()
    return "@" in value and "." in value.split("@")[-1]


def extract_user_email(user):
    email = first_non_empty(
        user.get("email"),
        user.get("email_default"),
        user.get("default_email")
    )

    if email:
        return email

    # W Twoim GLPI login/name czesto wyglada jak email.
    name = first_non_empty(user.get("name"))

    if looks_like_email(name):
        return name

    return ""


def build_user_names(user):
    first_name = first_non_empty(user.get("firstname"))
    last_name = first_non_empty(user.get("realname"))
    name = first_non_empty(user.get("name"))

    full_name = " ".join(part for part in [first_name, last_name] if part).strip()

    if not full_name and not looks_like_email(name):
        full_name = name

    return first_name, last_name, full_name


def upsert_glpi_users(users, replace=False):
    conn = db_connect()
    cursor = conn.cursor()

    if replace:
        cursor.execute("DROP TABLE IF EXISTS glpi_users")

    create_glpi_users_table(cursor)

    inserted = 0

    for user in users:
        try:
            glpi_user_id = int(user.get("id"))
        except Exception:
            continue

        email = extract_user_email(user)
        first_name, last_name, full_name = build_user_names(user)

        if not full_name and not email:
            continue

        cursor.execute('''
            INSERT INTO glpi_users (
                glpi_user_id,
                email,
                full_name,
                first_name,
                last_name
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(glpi_user_id) DO UPDATE SET
                email = excluded.email,
                full_name = excluded.full_name,
                first_name = excluded.first_name,
                last_name = excluded.last_name
        ''', (
            glpi_user_id,
            email,
            full_name,
            first_name,
            last_name
        ))

        inserted += 1

    conn.commit()
    conn.close()

    return inserted


def find_glpi_user_in_cache(person_name, label="osoba"):
    person_name = str(person_name or "").strip()

    if not person_name:
        log("GLPI USER", f"Brak nazwy dla '{label}', nie da sie dopasowac usera.")
        return None

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT glpi_user_id, email, full_name, first_name, last_name
        FROM glpi_users
    ''')
    rows = cursor.fetchall()

    conn.close()

    matches = []

    for glpi_user_id, email, full_name, first_name, last_name in rows:
        candidates = [
            full_name,
            f"{first_name} {last_name}".strip(),
            f"{last_name} {first_name}".strip(),
            email,
        ]

        if any(names_match(person_name, candidate) for candidate in candidates if candidate):
            matches.append((glpi_user_id, email, full_name))

    if len(matches) == 1:
        glpi_user_id, email, full_name = matches[0]
        log("GLPI USER", f"Cache hit ({label}): '{person_name}' -> GLPI user id={glpi_user_id}, email='{email}', full_name='{full_name}'")
        return glpi_user_id

    if len(matches) > 1:
        log("GLPI USER", f"Cache ambiguous ({label}): '{person_name}' pasuje do {len(matches)} userow: {matches}. Zostawiam puste.")
        return None

    log("GLPI USER", f"Cache miss ({label}): '{person_name}'.")
    return None


def get_glpi_session_token():
    check_glpi_env()

    url = glpi_url("initSession")

    headers = {
        "Content-Type": "application/json",
        "App-Token": cfg.GLPI_APP_TOKEN,
        "Authorization": f"user_token {cfg.GLPI_USER_TOKEN}"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=cfg.GLPI_HTTP_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise GLPIRequestError(
            f"Blad polaczenia przy initSession: {type(e).__name__}: {e}"
        )

    if response.status_code != 200:
        raise GLPIRequestError(
            f"GLPI initSession error. HTTP {response.status_code} {response.reason}. Body: {get_response_text(response)}",
            response.status_code
        )

    try:
        data = response.json()
    except Exception as e:
        raise GLPIRequestError(
            f"GLPI initSession zwrocilo niepoprawny JSON. HTTP {response.status_code}. Error: {e}. Body: {get_response_text(response)}",
            response.status_code
        )

    session_token = data.get("session_token")

    if not session_token:
        raise GLPIRequestError(
            f"GLPI initSession nie zwrocilo session_token. Body: {get_response_text(response)}",
            response.status_code
        )

    return session_token


def kill_glpi_session(session_token):
    if not session_token:
        return

    url = glpi_url("killSession")

    headers = {
        "Content-Type": "application/json",
        "App-Token": cfg.GLPI_APP_TOKEN,
        "Session-Token": session_token
    }

    try:
        requests.get(
            url,
            headers=headers,
            timeout=10
        )
    except Exception as e:
        log("GLPI ERROR", f"Nie udalo sie zamknac sesji: {type(e).__name__}: {e}")


def fetch_glpi_users():
    session_token = None

    try:
        session_token = get_glpi_session_token()
        users = []
        start = 0
        page_size = cfg.GLPI_USER_PAGE_SIZE

        while True:
            end = start + page_size - 1
            url = glpi_url("User")

            headers = {
                "Content-Type": "application/json",
                "App-Token": cfg.GLPI_APP_TOKEN,
                "Session-Token": session_token,
                "Range": f"{start}-{end}"
            }

            params = {
                "range": f"{start}-{end}",
                "is_deleted": "0"
            }

            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=cfg.GLPI_HTTP_TIMEOUT,
                    
                )
            except requests.exceptions.RequestException as e:
                raise GLPIRequestError(
                    f"Blad polaczenia przy pobieraniu userow GLPI: {type(e).__name__}: {e}"
                )

            if response.status_code not in (200, 206):
                raise GLPIRequestError(
                    f"GLPI User fetch error. HTTP {response.status_code} {response.reason}. Body: {get_response_text(response)}",
                    response.status_code
                )

            try:
                data = response.json()
            except Exception as e:
                raise GLPIRequestError(
                    f"GLPI User zwrocilo niepoprawny JSON. HTTP {response.status_code}. Error: {e}. Body: {get_response_text(response)}",
                    response.status_code
                )

            if isinstance(data, dict):
                data = data.get("data") or data.get("items") or []

            if not isinstance(data, list):
                raise GLPIRequestError(f"GLPI User zwrocilo dziwny format: {type(data).__name__}")

            users.extend(data)

            if len(data) < page_size or response.status_code == 200:
                break

            start += page_size

            if start > 100000:
                raise GLPIRequestError("Przerwano pobieranie userow GLPI: przekroczono 100000 rekordow")

        return users

    finally:
        kill_glpi_session(session_token)


def refresh_glpi_users_cache():
    log("GLPI USER", "Pobieram userow z GLPI do cache...")
    users = fetch_glpi_users()

    if is_verbose_log_enabled("log_glpi_user_sample_payload"):
        if users:
            log("GLPI USER SAMPLE", "Przykladowy surowy payload jednego usera z GLPI:")
            log("GLPI USER SAMPLE", json.dumps(users[0], ensure_ascii=False, indent=2))
        else:
            log("GLPI USER SAMPLE", "GLPI nie zwrocilo zadnego usera.")

    count = upsert_glpi_users(users, replace=True)
    log("GLPI USER", f"Zaktualizowano cache userow GLPI. Zapisano: {count}")
    return count


def fetch_and_cache_users_silent():
    users = fetch_glpi_users()
    saved = upsert_glpi_users(users, replace=False)
    log("GLPI USER", f"Online GLPI fetch OK. Zapisano/odswiezono userow: {saved}")
    return saved


def resolve_ticket_location_id(cdr):
    """
    Ustawianie requestera po osobie dzwoniacej zostalo calkowicie wylaczone.
    Tutaj zostaje tylko lokalizacja, i tylko gdy from-dispname zawiera keyword "sala".
    """

    caller_name = cdr.get("caller_name") or cdr.get("from_dispname") or ""

    if not cfg.GLPI_SET_ROOM_AS_LOCATION:
        return None

    room_number = extract_room_number_from_caller_dispname(caller_name)

    if not room_number:
        if not from_dispname_has_sala_keyword(caller_name):
            log("GLPI LOCATION", f"Brak keywordu 'sala' w from-dispname='{caller_name}'. Nie dotykam lokalizacji.")
        else:
            log("GLPI LOCATION", f"from-dispname ma keyword 'sala', ale nie znalazlem numeru sali: '{caller_name}'. Ticket pojdzie bez lokalizacji.")
        return None

    log("GLPI LOCATION", f"from-dispname ma keyword 'sala'. from-dispname='{caller_name}', room_number='{room_number}'. Requester NIE bedzie ustawiany.")

    try:
        return resolve_location_id_by_room(room_number)
    except Exception as e:
        log("GLPI LOCATION", f"Dopasowanie lokalizacji FAIL: {type(e).__name__}: {e}. Ticket pojdzie bez lokalizacji.")
        return None


def resolve_owner_id(cdr):
    final_dn = cdr.get("final_dn")
    to_no = cdr.get("to_no")

    owner_id = cfg.OWNER_BY_FINAL_DN.get(final_dn)

    if owner_id:
        log("GLPI OWNER", f"Owner po final-dn: '{final_dn}' -> GLPI user id={owner_id}")
        return owner_id

    owner_id = cfg.OWNER_BY_TO_NO.get(to_no)

    if owner_id:
        log("GLPI OWNER", f"Owner awaryjnie po to-no: '{to_no}' -> GLPI user id={owner_id}")
        return owner_id

    final_name = cdr.get("final_dispname")

    if not final_name:
        log("GLPI OWNER", f"Brak ownera: final-dn='{final_dn}', to-no='{to_no}', final-dispname puste.")
        return None

    owner_id = find_glpi_user_in_cache(final_name, "owner/final-dispname")

    if owner_id:
        return owner_id

    try:
        fetch_and_cache_users_silent()
    except Exception as e:
        log("GLPI OWNER", f"Online GLPI fetch FAIL przy ownerze: {type(e).__name__}: {e}")
        return None

    owner_id = find_glpi_user_in_cache(final_name, "owner/final-dispname")

    if owner_id:
        log("GLPI OWNER", f"Owner po final-dispname: '{final_name}' -> GLPI user id={owner_id}")
        return owner_id

    log("GLPI OWNER", f"Brak ownera dla final-dn='{final_dn}', to-no='{to_no}', final-dispname='{final_name}'")
    return None


# =========================
# GLPI LOCATIONS CACHE
# =========================

def extract_room_number_from_text(value):
    value = str(value or "").strip()

    if not value:
        return ""

    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    normalized = normalize_name(value)

    # CDR: sala 443 / Sala 443 / sala 443a
    match = re.search(r"\bsala\s*([0-9]{1,5}[a-z]?)\b", normalized)
    if match:
        return match.group(1)

    # GLPI path/name: 443 (Sala wykladowa)
    match = re.search(r"\b([0-9]{1,5}[a-z]?)\s*\(\s*sala\b", ascii_value)
    if match:
        return match.group(1)

    # Po normalizacji: 443 sala wykladowa
    match = re.search(r"\b([0-9]{1,5}[a-z]?)\s+sala\b", normalized)
    if match:
        return match.group(1)

    return ""


def from_dispname_has_sala_keyword(value):
    normalized = normalize_name(value)
    return bool(re.search(r"\bsala\b", normalized))


def extract_room_number_from_caller_dispname(value):
    # Lokalizacje odpalamy WYLACZNIE dla from-dispname z keywordem "sala".
    # Numer telefonu albo zwykle imie usera nie ma prawa tu wejsc.
    if not from_dispname_has_sala_keyword(value):
        return ""

    room_number = extract_room_number_from_text(value)

    if room_number:
        return room_number

    normalized = normalize_name(value)
    match = re.search(r"\b([0-9]{1,5}[a-z]?)\b", normalized)

    if match:
        return match.group(1)

    return ""


def build_location_name(location):
    name = first_non_empty(location.get("name"))
    full_path = first_non_empty(
        location.get("completename"),
        location.get("complete_name"),
        location.get("full_name"),
        location.get("full_path")
    )

    if not full_path:
        full_path = name

    if not name:
        name = full_path

    return name, full_path


def upsert_glpi_locations(locations, replace=False):
    conn = db_connect()
    cursor = conn.cursor()

    if replace:
        cursor.execute("DROP TABLE IF EXISTS glpi_locations")

    create_glpi_locations_table(cursor)

    inserted = 0

    for location in locations:
        try:
            glpi_location_id = int(location.get("id"))
        except Exception:
            continue

        name, full_path = build_location_name(location)
        room_number = extract_room_number_from_text(f"{name} {full_path}")

        if not name and not full_path:
            continue

        cursor.execute('''
            INSERT INTO glpi_locations (
                glpi_location_id,
                room_number,
                name,
                full_path
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(glpi_location_id) DO UPDATE SET
                room_number = excluded.room_number,
                name = excluded.name,
                full_path = excluded.full_path
        ''', (
            glpi_location_id,
            room_number,
            name,
            full_path
        ))

        inserted += 1

    conn.commit()
    conn.close()

    return inserted


def find_glpi_location_in_cache(room_number):
    room_number = str(room_number or "").strip().lower()

    if not room_number:
        log("GLPI LOCATION", "Brak numeru sali, nie da sie dopasowac lokalizacji.")
        return None

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT glpi_location_id, room_number, name, full_path
        FROM glpi_locations
    ''')
    rows = cursor.fetchall()

    conn.close()

    matches = []

    for glpi_location_id, cached_room, name, full_path in rows:
        candidates = [cached_room, extract_room_number_from_text(name), extract_room_number_from_text(full_path)]
        candidate_values = [str(candidate or "").strip().lower() for candidate in candidates if candidate]

        if room_number in candidate_values:
            matches.append((glpi_location_id, cached_room, name, full_path))

    if len(matches) == 1:
        glpi_location_id, cached_room, name, full_path = matches[0]
        log("GLPI LOCATION", f"Cache hit: sala '{room_number}' -> location id={glpi_location_id}, name='{name}', path='{full_path}'")
        return glpi_location_id

    if len(matches) > 1:
        log("GLPI LOCATION", f"Cache ambiguous: sala '{room_number}' pasuje do {len(matches)} lokalizacji: {matches}. Zostawiam lokalizacje pusta.")
        return None

    log("GLPI LOCATION", f"Cache miss: sala '{room_number}'.")
    return None


def fetch_glpi_locations():
    session_token = None

    try:
        session_token = get_glpi_session_token()
        locations = []
        start = 0
        page_size = cfg.GLPI_LOCATION_PAGE_SIZE

        while True:
            end = start + page_size - 1
            url = glpi_url("Location")

            headers = {
                "Content-Type": "application/json",
                "App-Token": cfg.GLPI_APP_TOKEN,
                "Session-Token": session_token,
                "Range": f"{start}-{end}"
            }

            params = {
                "range": f"{start}-{end}",
                "is_deleted": "0"
            }

            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=cfg.GLPI_HTTP_TIMEOUT,
                    
                )
            except requests.exceptions.RequestException as e:
                raise GLPIRequestError(
                    f"Blad polaczenia przy pobieraniu lokalizacji GLPI: {type(e).__name__}: {e}"
                )

            if response.status_code == 403 and response_has_glpi_error(response, "ERROR_RIGHT_MISSING"):
                global GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME
                GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = True
                raise GLPIRequestError(
                    build_glpi_right_missing_message("Location", response),
                    response.status_code
                )

            if response.status_code not in (200, 206):
                raise GLPIRequestError(
                    f"GLPI Location fetch error. HTTP {response.status_code} {response.reason}. Body: {get_response_text(response)}",
                    response.status_code
                )

            try:
                data = response.json()
            except Exception as e:
                raise GLPIRequestError(
                    f"GLPI Location zwrocilo niepoprawny JSON. HTTP {response.status_code}. Error: {e}. Body: {get_response_text(response)}",
                    response.status_code
                )

            if isinstance(data, dict):
                data = data.get("data") or data.get("items") or []

            if not isinstance(data, list):
                raise GLPIRequestError(f"GLPI Location zwrocilo dziwny format: {type(data).__name__}")

            locations.extend(data)

            if len(data) < page_size or response.status_code == 200:
                break

            start += page_size

            if start > 100000:
                raise GLPIRequestError("Przerwano pobieranie lokalizacji GLPI: przekroczono 100000 rekordow")

        return locations

    finally:
        kill_glpi_session(session_token)


def refresh_glpi_locations_cache():
    global GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME
    GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = False

    log("GLPI LOCATION", "Pobieram lokalizacje z GLPI do cache...")
    locations = fetch_glpi_locations()

    if is_verbose_log_enabled("log_glpi_location_sample_payload"):
        if locations:
            log("GLPI LOCATION SAMPLE", "Przykladowy surowy payload jednej lokalizacji z GLPI:")
            log("GLPI LOCATION SAMPLE", json.dumps(locations[0], ensure_ascii=False, indent=2))
        else:
            log("GLPI LOCATION SAMPLE", "GLPI nie zwrocilo zadnej lokalizacji.")

    count = upsert_glpi_locations(locations, replace=True)
    log("GLPI LOCATION", f"Zaktualizowano cache lokalizacji GLPI. Zapisano: {count}")
    return count


def fetch_and_cache_locations_silent():
    if GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME:
        log("GLPI LOCATION", "Pomijam online fetch lokalizacji: w tej sesji API juz zwrocilo 403/ERROR_RIGHT_MISSING dla Location.")
        return 0

    locations = fetch_glpi_locations()
    saved = upsert_glpi_locations(locations, replace=False)
    log("GLPI LOCATION", f"Online GLPI fetch OK. Zapisano/odswiezono lokalizacje: {saved}")
    return saved


def resolve_location_id_by_room(room_number):
    try:
        location_id = find_glpi_location_in_cache(room_number)
    except Exception as e:
        log("GLPI LOCATION", f"Cache lookup FAIL: {type(e).__name__}: {e}. Probuje pobrac lokalizacje z GLPI.")
        location_id = None

    if location_id:
        return location_id

    if not cfg.GLPI_AUTO_FETCH_LOCATIONS_ON_MISS:
        log("GLPI LOCATION", "Nie robie online fetch lokalizacji przy polaczeniu. Odswiez cache z panelu albo ustaw GLPI_AUTO_FETCH_LOCATIONS_ON_MISS=1 w .env.")
        return None

    try:
        fetch_and_cache_locations_silent()
    except Exception as e:
        log("GLPI LOCATION", f"Online GLPI location fetch FAIL: {type(e).__name__}: {e}. Zostawiam lokalizacje pusta.")
        return None

    try:
        location_id = find_glpi_location_in_cache(room_number)
    except Exception as e:
        log("GLPI LOCATION", f"Cache lookup po fetchu FAIL: {type(e).__name__}: {e}. Zostawiam lokalizacje pusta.")
        return None

    if location_id:
        log("GLPI LOCATION", f"Online match OK: sala '{room_number}' -> location id={location_id}")
        return location_id

    log("GLPI LOCATION", f"Nie znaleziono lokalizacji dla sali '{room_number}'. Ticket pojdzie bez lokalizacji.")
    return None


# =========================
# GLPI TICKETY
# =========================

def apply_location_to_payload(payload, location_id, caller_name):
    if not location_id:
        return payload

    input_data = payload.setdefault("input", {})
    input_data["locations_id"] = location_id
    log("GLPI LOCATION", f"Dodaje lokalizacje do ticketu: locations_id={location_id}, from-dispname='{caller_name}'")
    return payload


def get_ticket_caller_label(cdr):
    return first_non_empty(cdr.get("from_dispname"), cdr.get("from_no"), "nieznany")


def build_missed_call_payload(cdr, location_id=None):
    title = f"Nieodebrane Polaczenie od {get_ticket_caller_label(cdr)}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie nieodebranego polaczenia z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"duration: {cdr['duration']}\n"
        f"from-no: {cdr['from_no']}\n"
        f"from-dispname: {cdr['from_dispname']}\n"
        f"to-no: {cdr['to_no']}\n"
        f"final-dn: {cdr['final_dn']}\n"
        f"final-dispname: {cdr['final_dispname']}\n"
    )

    payload = {
        "input": {
            "name": title,
            "content": content,
            "type": 1,
            "status": cfg.GLPI_STATUS_NEW,
            "urgency": 3,
            "impact": 3,
            "priority": 3,
            "itilcategories_id": cfg.GLPI_CATEGORY_ID,
            "_groups_id_assign": cfg.GLPI_HELPDESK_GROUP_ID
        }
    }

    return apply_location_to_payload(payload, location_id, cdr.get("from_dispname"))


def build_answered_call_payload(cdr, owner_id, location_id=None):
    title = f"Polaczenie telefoniczne od {get_ticket_caller_label(cdr)}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie rekordu CDR z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"duration: {cdr['duration']}\n"
        f"from-no: {cdr['from_no']}\n"
        f"from-dispname: {cdr['from_dispname']}\n"
        f"to-no: {cdr['to_no']}\n"
        f"final-dn: {cdr['final_dn']}\n"
        f"final-dispname: {cdr['final_dispname']}\n"
    )

    payload = {
        "input": {
            "name": title,
            "content": content,
            "type": 1,
            "status": cfg.GLPI_STATUS_CLOSED,
            "urgency": 3,
            "impact": 3,
            "priority": 3,
            "itilcategories_id": cfg.GLPI_CATEGORY_ID,
            "_users_id_assign": owner_id,
            "_groups_id_assign": cfg.GLPI_HELPDESK_GROUP_ID
        }
    }

    return apply_location_to_payload(payload, location_id, cdr.get("from_dispname"))


def post_glpi_ticket(payload):
    session_token = None

    try:
        session_token = get_glpi_session_token()

        url = glpi_url("Ticket")

        headers = {
            "Content-Type": "application/json",
            "App-Token": cfg.GLPI_APP_TOKEN,
            "Session-Token": session_token
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=cfg.GLPI_HTTP_TIMEOUT
            )
        except requests.exceptions.RequestException as e:
            raise GLPIRequestError(
                f"Blad polaczenia przy tworzeniu ticketu: {type(e).__name__}: {e}"
            )

        if response.status_code not in (200, 201):
            raise GLPIRequestError(
                f"GLPI create ticket error. HTTP {response.status_code} {response.reason}. Body: {get_response_text(response)}",
                response.status_code
            )

        try:
            data = response.json()
        except Exception as e:
            raise GLPIRequestError(
                f"GLPI create ticket zwrocilo niepoprawny JSON. HTTP {response.status_code}. Error: {e}. Body: {get_response_text(response)}",
                response.status_code
            )

        ticket_id = (
            data.get("id")
            or data.get("items_id")
            or data.get("ticket_id")
            or "unknown"
        )

        return response.status_code, ticket_id

    finally:
        kill_glpi_session(session_token)




def create_glpi_ticket(payload):
    return post_glpi_ticket(payload)


# =========================
# LOGIKA CDR
# =========================

def handle_cdr_line(line: str):
    line = line.strip()

    if not line:
        return

    try:
        cdr = parse_cdr_line(line)
        log_cdr_debug(cdr)
    except Exception as e:
        log("BLEDNY CDR", line)
        log("ERROR", e)
        return

    to_no = cdr["to_no"]
    final_dn = cdr.get("final_dn")

    if to_no not in cfg.IT_NUMBERS and final_dn not in cfg.IT_NUMBERS:
        log("SPOZA IT", line)
        return

    is_missed_call = is_missed_call_cdr(cdr)
    location_id = resolve_ticket_location_id(cdr)

    if is_missed_call:
        log("CDR TYPE", f"Nieodebrane. duration={cdr['duration']}, to-no={cdr['to_no']}, final-dn={cdr['final_dn']}, final-dispname={cdr['final_dispname']}, from-dispname={cdr['from_dispname']}")

        if not is_missed_call_ticket_time():
            log("POMINIETO GLPI", "Nieodebrane polaczenie poza godzinami 08:00-20:00. Nie tworze ticketa i nie zapisuje do bazy.")
            return

        payload = build_missed_call_payload(cdr, location_id)

        try:
            status_code, ticket_id = create_glpi_ticket(payload)
            log("GLPI SUCCESS", f"Utworzono ticket z nieodebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}. location_id={location_id}")

        except GLPIRequestError as e:
            log("GLPI ERROR", e)

        except Exception as e:
            log("GLPI ERROR", f"Nieoczekiwany blad przy nieodebranym polaczeniu: {type(e).__name__}: {e}")

        return

    log("CDR TYPE", f"Odebrane. duration={cdr['duration']}, to-no={cdr['to_no']}, final-dn={cdr['final_dn']}, final-dispname={cdr['final_dispname']}, from-dispname={cdr['from_dispname']}")

    record_id = save_it_cdr(cdr)

    log("IT", f"Zapisano odebrany CDR do bazy. ID rekordu: {record_id}")
    log("IT", f"CDR: {line}")

    owner_id = resolve_owner_id(cdr)

    if not owner_id:
        error = f"Brak przypisanego ownera dla final-dn='{cdr['final_dn']}', to-no='{cdr['to_no']}', final-dispname='{cdr['final_dispname']}'"
        log("POMINIETO GLPI", error)
        mark_as_failed(record_id, None, error)
        return

    payload = build_answered_call_payload(cdr, owner_id, location_id)

    try:
        status_code, ticket_id = create_glpi_ticket(payload)
        mark_as_done(record_id, ticket_id, status_code, location_id)

        log("GLPI SUCCESS", f"Utworzono ticket z odebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}. owner_id={owner_id}. location_id={location_id}")

    except GLPIRequestError as e:
        error_text = str(e)
        log("GLPI ERROR", error_text)
        mark_as_failed(record_id, e.status_code, error_text)

    except Exception as e:
        error_text = f"Nieoczekiwany blad GLPI: {type(e).__name__}: {e}"
        log("GLPI ERROR", error_text)
        mark_as_failed(record_id, None, error_text)


def connect_to_3cx():
    while True:
        connection_started_at = None

        try:
            log("INFO", f"Connecting to 3CX CDR passive socket {cfg.HOST}:{cfg.PORT}...")

            with socket.create_connection((cfg.HOST, cfg.PORT), timeout=10) as sock:
                connection_started_at = datetime.now(cfg.TIMEZONE)

                keepalive_info = configure_tcp_keepalive(sock)

                local_addr = sock.getsockname()
                remote_addr = sock.getpeername()

                log("OK", f"Connected to 3CX. Local: {local_addr}. Remote: {remote_addr}")
                log("TCP", f"Keepalive wlaczony: {keepalive_info}")
                log("INFO", "Waiting for CDR data...")

                buffer = ""

                while True:
                    data = sock.recv(4096)

                    if not data:
                        raise ConnectionError(
                            "3CX zamknal polaczenie albo firewall/proxy zamknal sesje. "
                            "recv() zwrocilo 0 bajtow, czyli druga strona zamknela socket bez danych."
                        )

                    text = data.decode("utf-8", errors="replace")
                    buffer += text

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        handle_cdr_line(line)

        except Exception as e:
            log("ERROR", describe_connection_error(e, connection_started_at))
            log("INFO", f"Reconnecting in {cfg.RECONNECT_DELAY}s...")
            time.sleep(cfg.RECONNECT_DELAY)


# =========================
# START
# =========================

if __name__ == "__main__":
    init_db()
    connect_to_3cx()
