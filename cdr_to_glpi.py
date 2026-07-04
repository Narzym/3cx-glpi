import socket
import time
import sqlite3
import csv
import os
import errno
import re
import unicodedata
import json
import requests
from io import StringIO
from datetime import datetime

import config as cfg


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


def is_missed_call_ticket_time():
    now = datetime.now(cfg.TIMEZONE)
    return cfg.MISSED_CALL_START_HOUR <= now.hour < cfg.MISSED_CALL_END_HOUR


def get_now_string():
    return datetime.now(cfg.TIMEZONE).strftime("%d-%m-%Y %H:%M:%S")


def duration_to_seconds(duration):
    """
    Obsluguje formaty:
    00:00:00
    0:00:00
    00:00
    15
    """

    duration = str(duration).strip()

    if not duration:
        return None

    try:
        if ":" not in duration:
            return int(float(duration))

        parts = duration.split(":")
        parts = [int(part.strip()) for part in parts]

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
    1. final_number jest jednym z naszych numerow
    2. duration = 0

    Sam final_number NIE wystarcza, bo przy odebranym polaczeniu direct
    final_number tez jest np. 526 i wtedy robilo falszywe missed calle.
    """

    final_number = cdr["final_number"]
    duration = cdr["duration"]

    return final_number in cfg.MISSED_CALL_FINAL_DNS and is_zero_duration(duration)


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


def column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def add_column_if_missing(cursor, table_name, column_name, column_definition):
    if not column_exists(cursor, table_name, column_name):
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def normalize_cdr_limit_value(value):
    value = str(value).strip().lower()

    if value in ("unlimited", "bez limitu", "none", "0"):
        return "unlimited"

    limit = int(value)

    if limit < 10:
        raise ValueError("Limit rekordow CDR musi byc >= 10 albo unlimited")

    return str(limit)


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def ensure_glpi_users_table(cursor):
    """
    Cache baza GLPI.
    Stara tabela jest kasowana bez migracji, bo wolimy czysta strukture niz SQL-owy bigos.
    """

    expected_columns = [
        "glpi_user_id",
        "email",
        "full_name",
        "first_name",
        "last_name"
    ]
    current_columns = get_table_columns(cursor, "glpi_users")

    if current_columns and current_columns != expected_columns:
        cursor.execute("DROP TABLE IF EXISTS glpi_users")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glpi_users (
            glpi_user_id INTEGER PRIMARY KEY,
            email TEXT,
            full_name TEXT,
            first_name TEXT,
            last_name TEXT
        )
    """)


def init_db():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS it_cdr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            callid TEXT,
            duration TEXT,
            caller_number TEXT,
            caller_name TEXT,
            dialed_number TEXT,
            final_number TEXT,
            final_name TEXT,
            zrobiony INTEGER NOT NULL DEFAULT 0,
            glpi_ticket_id TEXT,
            glpi_status_code INTEGER,
            glpi_error TEXT,
            glpi_requester_user_id TEXT
        )
    """)

    add_column_if_missing(cursor, "it_cdr", "caller_name", "TEXT")
    add_column_if_missing(cursor, "it_cdr", "glpi_requester_user_id", "TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    ensure_glpi_users_table(cursor)

    default_limit = normalize_cdr_limit_value(cfg.CDR_MAX_RECORDS_DEFAULT)
    cursor.execute("""
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('cdr_max_records', ?)
    """, (default_limit,))

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

    cursor.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


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

    cursor.execute("""
        DELETE FROM it_cdr
        WHERE id NOT IN (
            SELECT id
            FROM it_cdr
            ORDER BY id DESC
            LIMIT ?
        )
    """, (limit,))

    deleted = cursor.rowcount if cursor.rowcount is not None else 0

    conn.commit()
    conn.close()

    if deleted > 0:
        log("DB", f"Limit CDR={limit}. Usunieto najstarsze rekordy: {deleted}. Bylo: {before}")

    return deleted


def reset_it_cdr():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM it_cdr")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'it_cdr'")

    conn.commit()
    conn.close()


def reset_glpi_users():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM glpi_users")

    conn.commit()
    conn.close()


def save_it_cdr(cdr: dict):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO it_cdr (
            created_at,
            callid,
            duration,
            caller_number,
            caller_name,
            dialed_number,
            final_number,
            final_name,
            zrobiony
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        get_now_string(),
        cdr["callid"],
        cdr["duration"],
        cdr["caller_number"],
        cdr["caller_name"],
        cdr["dialed_number"],
        cdr["final_number"],
        cdr["final_name"],
        0
    ))

    record_id = cursor.lastrowid

    conn.commit()
    conn.close()

    cleanup_it_cdr_limit()

    return record_id


def mark_as_done(record_id, ticket_id, status_code, requester_user_id=None):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE it_cdr
        SET zrobiony = 1,
            glpi_ticket_id = ?,
            glpi_status_code = ?,
            glpi_error = NULL,
            glpi_requester_user_id = ?
        WHERE id = ?
    """, (
        str(ticket_id),
        status_code,
        str(requester_user_id) if requester_user_id else None,
        record_id
    ))

    conn.commit()
    conn.close()


def mark_as_failed(record_id, status_code, error):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE it_cdr
        SET zrobiony = 0,
            glpi_status_code = ?,
            glpi_error = ?
        WHERE id = ?
    """, (
        status_code,
        truncate_text(error, 3500),
        record_id
    ))

    conn.commit()
    conn.close()


def get_db_stats():
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM it_cdr")
    cdr_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM glpi_users")
    glpi_users_count = cursor.fetchone()[0]

    conn.close()

    return {
        "cdr_count": cdr_count,
        "glpi_users_count": glpi_users_count,
        "cdr_max_records": get_cdr_max_records_label()
    }


# =========================
# PARSOWANIE CDR
# =========================

def parse_cdr_line(line: str):
    """
    Nowa struktura CDR:
    callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname

    Wewnetrznie zostawiamy stare nazwy kluczy, zeby reszta logiki byla czytelna:
    caller_number = from-no
    caller_name = from-dispname
    dialed_number = to-no
    final_number = final-dn
    final_name = final-dispname
    """

    reader = csv.reader(StringIO(line))
    values = next(reader)

    values = [v.strip() for v in values]

    if len(values) < 7:
        raise ValueError(f"Za malo wartosci w CDR: {len(values)}. Oczekiwano 7 pol.")

    callid = values[0]

    if callid.startswith("Call "):
        callid = callid[5:]

    return {
        "callid": callid,
        "duration": values[1],
        "caller_number": values[2],
        "caller_name": values[3],
        "dialed_number": values[4],
        "final_number": values[5],
        "final_name": values[6],
    }


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


def names_match(caller_name, user_display_name):
    caller_normalized = normalize_name(caller_name)
    user_normalized = normalize_name(user_display_name)

    if not caller_normalized or not user_normalized:
        return False

    if caller_normalized == user_normalized:
        return True

    caller_tokens = token_set(caller_name)
    user_tokens = token_set(user_display_name)

    if len(caller_tokens) >= 2 and caller_tokens == user_tokens:
        return True

    # Dopuszczamy sytuacje typu "Jan Kowalski ext" albo "Kowalski Jan".
    if len(caller_tokens) >= 2 and caller_tokens.issubset(user_tokens):
        return True

    if len(user_tokens) >= 2 and user_tokens.issubset(caller_tokens):
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


def extract_user_first_name(user):
    return first_non_empty(
        user.get("firstname"),
        user.get("first_name"),
        user.get("given_name")
    )


def extract_user_last_name(user):
    return first_non_empty(
        user.get("realname"),
        user.get("lastname"),
        user.get("last_name"),
        user.get("surname")
    )


def build_user_full_name(user):
    first_name = extract_user_first_name(user)
    last_name = extract_user_last_name(user)
    name = first_non_empty(user.get("name"))

    full_from_names = " ".join(part for part in [first_name, last_name] if part).strip()

    # W GLPI pole name czesto jest loginem albo mailem. Pelne imie bierzemy najpierw
    # z firstname + realname, a name tylko awaryjnie, jesli nie ma nic lepszego.
    return first_non_empty(full_from_names, name)


def looks_like_email(value):
    value = str(value or "").strip()
    return "@" in value and "." in value.split("@")[-1]


def extract_user_email(user):
    email = first_non_empty(
        user.get("email"),
        user.get("email_default"),
        user.get("default_email")
    )

    # U Ciebie GLPI wyglada tak, ze pole name czesto jest loginem-mailem.
    # Dlatego jesli klasyczne pola mailowe sa puste, bierzemy name jako email.
    if not email and looks_like_email(user.get("name")):
        email = first_non_empty(user.get("name"))

    return email


def format_sample_glpi_user_payload(user):
    try:
        return json.dumps(user, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return repr(user)


def upsert_glpi_users(users, replace=False):
    conn = db_connect()
    cursor = conn.cursor()
    ensure_glpi_users_table(cursor)

    if replace:
        cursor.execute("DELETE FROM glpi_users")

    inserted = 0

    for user in users:
        try:
            glpi_user_id = int(user.get("id"))
        except Exception:
            continue

        email = extract_user_email(user)
        first_name = extract_user_first_name(user)
        last_name = extract_user_last_name(user)
        full_name = build_user_full_name(user)

        if not full_name:
            continue

        cursor.execute("""
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
        """, (
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


def find_glpi_user_in_cache(caller_name):
    caller_name = str(caller_name or "").strip()

    if not caller_name:
        log("GLPI USER", "Brak from-dispname w CDR, nie da sie dopasowac usera.")
        return None

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT glpi_user_id, full_name
        FROM glpi_users
    """)
    rows = cursor.fetchall()

    conn.close()

    matches = []

    for glpi_user_id, full_name in rows:
        if names_match(caller_name, full_name):
            matches.append((glpi_user_id, full_name))

    if len(matches) == 1:
        glpi_user_id, full_name = matches[0]
        log("GLPI USER", f"Cache hit: '{caller_name}' -> GLPI user id={glpi_user_id}, full_name='{full_name}'")
        return glpi_user_id

    if len(matches) > 1:
        log("GLPI USER", f"Cache ambiguous: '{caller_name}' pasuje do {len(matches)} userow: {matches}. Zostawiam puste.")
        return None

    log("GLPI USER", f"Cache miss: '{caller_name}'. Sprawdzam GLPI online.")
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
                    timeout=cfg.GLPI_HTTP_TIMEOUT
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

    if users:
        log("GLPI USER SAMPLE", "Przykladowy surowy payload jednego usera z GLPI:")
        log("GLPI USER SAMPLE", format_sample_glpi_user_payload(users[0]))
    else:
        log("GLPI USER SAMPLE", "GLPI zwrocilo pusta liste userow, nie ma przykladowego payloadu.")

    count = upsert_glpi_users(users, replace=True)
    log("GLPI USER", f"Zaktualizowano cache userow GLPI. Zapisano: {count}")
    return count


def resolve_caller_user_id(cdr):
    caller_name = cdr.get("caller_name")

    log("GLPI USER", f"Dopasowanie osoby dzwoniacej. from-no='{cdr.get('caller_number')}', from-dispname='{caller_name}'")

    user_id = find_glpi_user_in_cache(caller_name)

    if user_id:
        return user_id

    try:
        users = fetch_glpi_users()

        if users:
            log("GLPI USER SAMPLE", "Przykladowy surowy payload jednego usera z GLPI przy online fetch:")
            log("GLPI USER SAMPLE", format_sample_glpi_user_payload(users[0]))

        saved = upsert_glpi_users(users, replace=False)
        log("GLPI USER", f"Online GLPI fetch OK. Zapisano/odswiezono userow: {saved}")
    except Exception as e:
        log("GLPI USER", f"Online GLPI fetch FAIL: {type(e).__name__}: {e}. Zostawiam requester pusty.")
        return None

    user_id = find_glpi_user_in_cache(caller_name)

    if user_id:
        log("GLPI USER", f"Online match OK: '{caller_name}' -> GLPI user id={user_id}")
        return user_id

    log("GLPI USER", f"Nie znaleziono usera dla '{caller_name}' ani w cache, ani w GLPI. Zostawiam puste.")
    return None


# =========================
# GLPI TICKETY
# =========================

def apply_caller_to_payload(payload, caller_user_id, caller_name):
    if not caller_user_id:
        log("GLPI USER", f"Ticket bez osoby dzwoniacej jako requester. caller_name='{caller_name}'")
        return payload

    input_data = payload.setdefault("input", {})

    if cfg.GLPI_SET_CALLER_AS_REQUESTER:
        input_data["_users_id_requester"] = caller_user_id
        log("GLPI USER", f"Dodaje osobe dzwoniaca jako requester: user_id={caller_user_id}")

    if cfg.GLPI_SET_CALLER_AS_RECIPIENT:
        input_data["users_id_recipient"] = caller_user_id
        log("GLPI USER", f"Probuje ustawic osobe dzwoniaca jako recipient/tworca: user_id={caller_user_id}")

    return payload


def build_missed_call_payload(cdr, caller_user_id=None):
    title = f"Nieodebrane Polaczenie od {cdr['caller_number']}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie nieodebranego polaczenia z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"Czas trwania: {cdr['duration']}\n"
        f"Numer dzwoniacy: {cdr['caller_number']}\n"
        f"Nazwa dzwoniacego: {cdr['caller_name']}\n"
        f"Numer wybrany: {cdr['dialed_number']}\n"
        f"Extension nr: {cdr['final_number']}\n"
        f"Nazwa odbierajacego: {cdr['final_name']}\n"
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

    return apply_caller_to_payload(payload, caller_user_id, cdr.get("caller_name"))


def build_answered_call_payload(cdr, owner_id, caller_user_id=None):
    title = f"Polaczenie telefoniczne od {cdr['caller_number']}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie rekordu CDR z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"Czas trwania: {cdr['duration']}\n"
        f"Numer dzwoniacy: {cdr['caller_number']}\n"
        f"Nazwa dzwoniacego: {cdr['caller_name']}\n"
        f"Numer wybrany: {cdr['dialed_number']}\n"
        f"Extension nr: {cdr['final_number']}\n"
        f"Nazwa odbierajacego: {cdr['final_name']}\n"
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

    return apply_caller_to_payload(payload, caller_user_id, cdr.get("caller_name"))


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
    """
    Tworzy ticket. Jezeli GLPI nie przyjmie pola users_id_recipient albo requestera,
    probuje jeszcze raz bez problematycznych pol, zeby samo tworzenie ticketu nie padlo.
    """

    attempts = []

    original = {"input": dict(payload.get("input", {}))}
    attempts.append(("pelny payload", original))

    if "users_id_recipient" in original["input"]:
        no_recipient = {"input": dict(original["input"])}
        no_recipient["input"].pop("users_id_recipient", None)
        attempts.append(("bez users_id_recipient", no_recipient))

    if "_users_id_requester" in original["input"] or "users_id_recipient" in original["input"]:
        no_caller = {"input": dict(original["input"])}
        no_caller["input"].pop("users_id_recipient", None)
        no_caller["input"].pop("_users_id_requester", None)
        attempts.append(("bez caller user fields", no_caller))

    last_error = None

    for label, attempt_payload in attempts:
        try:
            if label != "pelny payload":
                log("GLPI RETRY", f"Ponawiam tworzenie ticketu: {label}")

            return post_glpi_ticket(attempt_payload)

        except GLPIRequestError as e:
            last_error = e
            log("GLPI ERROR", f"Proba '{label}' nieudana: {e}")

    raise last_error


# =========================
# LOGIKA CDR
# =========================

def handle_cdr_line(line: str):
    line = line.strip()

    if not line:
        return

    try:
        cdr = parse_cdr_line(line)
    except Exception as e:
        log("BLEDNY CDR", line)
        log("ERROR", e)
        return

    dialed_number = cdr["dialed_number"]

    if dialed_number not in cfg.IT_NUMBERS:
        log("SPOZA IT", line)
        return

    is_missed_call = is_missed_call_cdr(cdr)
    caller_user_id = resolve_caller_user_id(cdr)

    if is_missed_call:
        log("CDR TYPE", f"Nieodebrane. duration={cdr['duration']}, final_number={cdr['final_number']}, final_name={cdr['final_name']}, caller_name={cdr['caller_name']}")

        if not is_missed_call_ticket_time():
            log("POMINIETO GLPI", "Nieodebrane polaczenie poza godzinami 08:00-20:00. Nie tworze ticketa i nie zapisuje do bazy.")
            return

        payload = build_missed_call_payload(cdr, caller_user_id)

        try:
            status_code, ticket_id = create_glpi_ticket(payload)
            log("GLPI SUCCESS", f"Utworzono ticket z nieodebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}. caller_user_id={caller_user_id}")

        except GLPIRequestError as e:
            log("GLPI ERROR", e)

        except Exception as e:
            log("GLPI ERROR", f"Nieoczekiwany blad przy nieodebranym polaczeniu: {type(e).__name__}: {e}")

        return

    log("CDR TYPE", f"Odebrane. duration={cdr['duration']}, final_number={cdr['final_number']}, final_name={cdr['final_name']}, caller_name={cdr['caller_name']}")

    record_id = save_it_cdr(cdr)

    log("IT", f"Zapisano odebrany CDR do bazy. ID rekordu: {record_id}")
    log("IT", f"CDR: {line}")

    owner_id = cfg.OWNER_BY_FINAL_DN.get(cdr["final_number"])

    if not owner_id:
        error = f"Brak przypisanego ownera dla final-dn: {cdr['final_number']}"
        log("POMINIETO GLPI", error)
        mark_as_failed(record_id, None, error)
        return

    payload = build_answered_call_payload(cdr, owner_id, caller_user_id)

    try:
        status_code, ticket_id = create_glpi_ticket(payload)
        mark_as_done(record_id, ticket_id, status_code, caller_user_id)

        log("GLPI SUCCESS", f"Utworzono ticket z odebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}. owner_id={owner_id}. caller_user_id={caller_user_id}")

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
