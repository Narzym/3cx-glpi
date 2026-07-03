# cdr_to_glpi.py

import socket
import time
import sqlite3
import csv
import os
import errno
import requests
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# =========================
# KONFIGURACJA 3CX
# =========================

HOST = "212.180.155.28"
PORT = 5001

RECONNECT_DELAY = 5
DB_PATH = "cdrdatabase.db"

LOG_PATH = os.getenv("LOG_PATH", "logs/cdr_to_glpi.log")

IT_NUMBERS = {"517", "699", "717", "519", "524", "526", "528", "529"}  # pierwsze 3 - kolejki, kolejne 5 - extensions pracownikow


# =========================
# KONFIGURACJA TCP KEEPALIVE
# =========================

TCP_KEEPALIVE_IDLE = 60          # po ilu sekundach bez ruchu zaczac sprawdzac polaczenie
TCP_KEEPALIVE_INTERVAL = 10      # co ile sekund wysylac probe keepalive
TCP_KEEPALIVE_COUNT = 3          # ile nieudanych prob zanim system uzna polaczenie za martwe
TCP_USER_TIMEOUT_MS = 30000      # limit oczekiwania na potwierdzenie danych, jesli system wspiera


# =========================
# KONFIGURACJA CZASU
# =========================

TIMEZONE = ZoneInfo("Europe/Warsaw")

MISSED_CALL_START_HOUR = 8
MISSED_CALL_END_HOUR = 20


# =========================
# KONFIGURACJA GLPI REST API V1
# =========================

GLPI_API_URL = os.getenv("GLPI_API_URL")

GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = os.getenv("GLPI_USER_TOKEN")


# =========================
# MAPOWANIE GLPI
# =========================

GLPI_HELPDESK_GROUP_ID = 27
GLPI_CATEGORY_ID = 16

GLPI_STATUS_NEW = 1
GLPI_STATUS_CLOSED = 6

MISSED_CALL_FINAL_DNS = {"517", "699", "717", "519", "524", "526", "528", "529"}

OWNER_BY_FINAL_DN = {
    "519": 193,     # Grzegorz
    "524": 172,     # Mateusz
    "526": 154,     # Wojciech
    "528": 213,     # Michal
    "529": 187      # Jan
}


# =========================
# POMOCNICZE
# =========================

class GLPIRequestError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_log_timestamp():
    return datetime.now(TIMEZONE).strftime("%y-%m-%d %H:%M:%S")


def write_log_to_file(log_line):
    try:
        log_dir = os.path.dirname(LOG_PATH)

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        with open(LOG_PATH, "a", encoding="utf-8") as file:
            file.write(log_line + "\n")

    except Exception:
        pass


def log(tag, message):
    message = str(message)

    if "\n" in message:
        for line in message.splitlines():
            log_line = f"{get_log_timestamp()} [{tag}] {line}"
            print(log_line)
            write_log_to_file(log_line)
    else:
        log_line = f"{get_log_timestamp()} [{tag}] {message}"
        print(log_line)
        write_log_to_file(log_line)


def truncate_text(value, limit=3000):
    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + f"\n... [ucieto, calosc miala {len(value)} znakow]"


def glpi_url(path):
    if not GLPI_API_URL:
        raise ValueError("Brakuje GLPI_API_URL w .env")

    return GLPI_API_URL.rstrip("/") + "/" + path.lstrip("/")


def check_glpi_env():
    required = {
        "GLPI_API_URL": GLPI_API_URL,
        "GLPI_APP_TOKEN": GLPI_APP_TOKEN,
        "GLPI_USER_TOKEN": GLPI_USER_TOKEN
    }

    for key, value in required.items():
        if not value:
            raise ValueError(f"Brakuje {key} w .env")


def get_response_text(response):
    if response is None:
        return "Brak odpowiedzi"

    return truncate_text(response.text, 1500)


def is_missed_call_ticket_time():
    now = datetime.now(TIMEZONE)
    return MISSED_CALL_START_HOUR <= now.hour < MISSED_CALL_END_HOUR


def get_now_string():
    return datetime.now(TIMEZONE).strftime("%d-%m-%Y %H:%M:%S")


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

    return final_number in MISSED_CALL_FINAL_DNS and is_zero_duration(duration)


def get_connection_duration(start_time):
    if not start_time:
        return "brak danych"

    seconds = int((datetime.now(TIMEZONE) - start_time).total_seconds())
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
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE)
        settings.append(f"TCP_KEEPIDLE={TCP_KEEPALIVE_IDLE}")

    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL)
        settings.append(f"TCP_KEEPINTVL={TCP_KEEPALIVE_INTERVAL}")

    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT)
        settings.append(f"TCP_KEEPCNT={TCP_KEEPALIVE_COUNT}")

    if hasattr(socket, "TCP_USER_TIMEOUT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, TCP_USER_TIMEOUT_MS)
        settings.append(f"TCP_USER_TIMEOUT={TCP_USER_TIMEOUT_MS}ms")

    return ", ".join(settings)


# =========================
# BAZA DANYCH
# =========================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS it_cdr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            callid TEXT,
            duration TEXT,
            caller_number TEXT,
            dialed_number TEXT,
            final_number TEXT,
            final_name TEXT,
            zrobiony INTEGER NOT NULL DEFAULT 0,
            glpi_ticket_id TEXT,
            glpi_status_code INTEGER,
            glpi_error TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_it_cdr(cdr: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO it_cdr (
            created_at,
            callid,
            duration,
            caller_number,
            dialed_number,
            final_number,
            final_name,
            zrobiony
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        get_now_string(),
        cdr["callid"],
        cdr["duration"],
        cdr["caller_number"],
        cdr["dialed_number"],
        cdr["final_number"],
        cdr["final_name"],
        0
    ))

    record_id = cursor.lastrowid

    cursor.execute("""
        DELETE FROM it_cdr
        WHERE id NOT IN (
            SELECT id
            FROM it_cdr
            ORDER BY id DESC
            LIMIT 500
        )
    """)

    conn.commit()
    conn.close()

    return record_id


def mark_as_done(record_id, ticket_id, status_code):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE it_cdr
        SET zrobiony = 1,
            glpi_ticket_id = ?,
            glpi_status_code = ?,
            glpi_error = NULL
        WHERE id = ?
    """, (
        str(ticket_id),
        status_code,
        record_id
    ))

    conn.commit()
    conn.close()


def mark_as_failed(record_id, status_code, error):
    conn = sqlite3.connect(DB_PATH)
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


# =========================
# PARSOWANIE CDR
# =========================

def parse_cdr_line(line: str):
    reader = csv.reader(StringIO(line))
    values = next(reader)

    values = [v.strip() for v in values]

    if len(values) < 6:
        raise ValueError(f"Za malo wartosci w CDR: {len(values)}")

    callid = values[0]

    if callid.startswith("Call "):
        callid = callid[5:]

    return {
        "callid": callid,
        "duration": values[1],
        "caller_number": values[2],
        "dialed_number": values[3],
        "final_number": values[4],
        "final_name": values[5],
    }


# =========================
# GLPI REST API V1
# =========================

def get_glpi_session_token():
    check_glpi_env()

    url = glpi_url("initSession")

    headers = {
        "Content-Type": "application/json",
        "App-Token": GLPI_APP_TOKEN,
        "Authorization": f"user_token {GLPI_USER_TOKEN}"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=15
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
        "App-Token": GLPI_APP_TOKEN,
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


def build_missed_call_payload(cdr):
    title = f"Nieodebrane Polaczenie od {cdr['caller_number']}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie nieodebranego polaczenia z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"Czas trwania: {cdr['duration']}\n"
        f"Numer dzwoniacy: {cdr['caller_number']}\n"
        f"Numer wybrany: {cdr['dialed_number']}\n"
        f"Extension nr: {cdr['final_number']}\n"
        f"Nazwa odbierajacego: {cdr['final_name']}\n"
    )

    payload = {
        "input": {
            "name": title,
            "content": content,
            "type": 1,
            "status": GLPI_STATUS_NEW,
            "urgency": 3,
            "impact": 3,
            "priority": 3,
            "itilcategories_id": GLPI_CATEGORY_ID,
            "_groups_id_assign": GLPI_HELPDESK_GROUP_ID
        }
    }

    return payload


def build_answered_call_payload(cdr, owner_id):
    title = f"Polaczenie telefoniczne od {cdr['caller_number']}"

    content = (
        "Automatyczne zgloszenie utworzone na podstawie rekordu CDR z 3CX.\n\n"
        f"Call ID: {cdr['callid']}\n"
        f"Czas trwania: {cdr['duration']}\n"
        f"Numer dzwoniacy: {cdr['caller_number']}\n"
        f"Numer wybrany: {cdr['dialed_number']}\n"
        f"Extension nr: {cdr['final_number']}\n"
        f"Nazwa odbierajacego: {cdr['final_name']}\n"
    )

    payload = {
        "input": {
            "name": title,
            "content": content,
            "type": 1,
            "status": GLPI_STATUS_CLOSED,
            "urgency": 3,
            "impact": 3,
            "priority": 3,
            "itilcategories_id": GLPI_CATEGORY_ID,
            "_users_id_assign": owner_id,
            "_groups_id_assign": GLPI_HELPDESK_GROUP_ID
        }
    }

    return payload


def create_glpi_ticket(payload):
    session_token = None

    try:
        session_token = get_glpi_session_token()

        url = glpi_url("Ticket")

        headers = {
            "Content-Type": "application/json",
            "App-Token": GLPI_APP_TOKEN,
            "Session-Token": session_token
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=15
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


# =========================
# DUMMY GLPI
# =========================

# def create_glpi_ticket(payload):
#     log("DUMMY GLPI", "Ticket NIE zostal wyslany do GLPI.")
#     log("DUMMY GLPI", f"Payload: {payload}")
#
#     fake_status_code = 201
#     fake_ticket_id = "dummy-ticket-id"
#
#     return fake_status_code, fake_ticket_id


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

    if dialed_number not in IT_NUMBERS:
        log("SPOZA IT", line)
        return

    is_missed_call = is_missed_call_cdr(cdr)

    if is_missed_call:
        log("CDR TYPE", f"Nieodebrane. duration={cdr['duration']}, final_number={cdr['final_number']}, final_name={cdr['final_name']}")

        if not is_missed_call_ticket_time():
            log("POMINIETO GLPI", "Nieodebrane polaczenie poza godzinami 08:00-20:00. Nie tworze ticketa i nie zapisuje do bazy.")
            return

        payload = build_missed_call_payload(cdr)

        try:
            status_code, ticket_id = create_glpi_ticket(payload)
            log("GLPI SUCCESS", f"Utworzono ticket z nieodebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}")

        except GLPIRequestError as e:
            log("GLPI ERROR", e)

        except Exception as e:
            log("GLPI ERROR", f"Nieoczekiwany blad przy nieodebranym polaczeniu: {type(e).__name__}: {e}")

        return

    log("CDR TYPE", f"Odebrane. duration={cdr['duration']}, final_number={cdr['final_number']}, final_name={cdr['final_name']}")

    record_id = save_it_cdr(cdr)

    log("IT", f"Zapisano odebrany CDR do bazy. ID rekordu: {record_id}")
    log("IT", f"CDR: {line}")

    owner_id = OWNER_BY_FINAL_DN.get(cdr["final_number"])

    if not owner_id:
        error = f"Brak przypisanego ownera dla final-dn: {cdr['final_number']}"
        log("POMINIETO GLPI", error)
        mark_as_failed(record_id, None, error)
        return

    payload = build_answered_call_payload(cdr, owner_id)

    try:
        status_code, ticket_id = create_glpi_ticket(payload)
        mark_as_done(record_id, ticket_id, status_code)

        log("GLPI SUCCESS", f"Utworzono ticket z odebranego polaczenia. Ticket ID: {ticket_id}. HTTP {status_code}")

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
            log("INFO", f"Connecting to 3CX CDR passive socket {HOST}:{PORT}...")

            with socket.create_connection((HOST, PORT), timeout=10) as sock:
                connection_started_at = datetime.now(TIMEZONE)

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
            log("INFO", f"Reconnecting in {RECONNECT_DELAY}s...")
            time.sleep(RECONNECT_DELAY)


# =========================
# START
# =========================

if __name__ == "__main__":
    init_db()
    connect_to_3cx()