"""
cdr_to_glpi.py - GŁÓWNA USŁUGA (to ten plik uruchamia systemd).

Co robi:
  1. łączy się z centralą 3CX i odbiera rekordy rozmów (CDR),
  2. rozpoznaje, czy rozmowa dotyczy numeru IT i czy była odebrana, czy nie,
  3. tworzy ticket w GLPI (treść i przypisanie: tickets.py, komunikacja: glpi_client.py).

Reszta programu jest w osobnych plikach:
  config.py       - ustawienia            utils.py        - małe funkcje pomocnicze
  logger.py       - logi + rotacja        db.py           - baza SQLite
  glpi_client.py  - HTTP do GLPI          glpi_cache.py   - cache userów/sal/telefonów
  tickets.py      - logika ticketów       admin_panel.py  - panel TUI
"""

import csv
import errno
import json
import socket
import time
import traceback
from datetime import datetime
from io import StringIO

import config as cfg
from db import init_db, is_verbose_log_enabled, mark_as_done, mark_as_failed, save_it_cdr
from glpi_cache import refresh_glpi_phones_cache
from glpi_client import GLPIRequestError, create_glpi_ticket
from logger import log
from tickets import (
    build_answered_call_payload,
    build_missed_call_payload,
    resolve_owner_id,
    resolve_ticket_location_id,
)

# Zabezpieczenie: gdyby 3CX wysyłało dane bez znaku nowej linii, nie pozwalamy
# buforowi rosnąć w nieskończoność.
MAX_CDR_BUFFER_BYTES = 1_000_000


# =========================
# ROZPOZNAWANIE ROZMÓW
# =========================

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
        return True

    return seconds == 0


def is_missed_call_cdr(cdr):
    """
    Nieodebrane rozpoznajemy po:
    1. final-dn (lub awaryjnie to-no) jest jednym z naszych numerow IT
    2. duration = 0
    3. lub polaczenie zakonczylo sie bezposrednio na numerze kolejki (517 lub 699) #699 to kolejka z sal wykładowych
    """

    final_dn = str(cdr.get("final_dn") or "").strip()
    to_no = str(cdr.get("to_no") or "").strip()

    target_number = final_dn or to_no

    if target_number not in cfg.MISSED_CALL_FINAL_DNS:
        return False

    if is_zero_duration(cdr.get("duration")):
        return True

    if final_dn in ("517", "225435517", "699", "225435699"):
        return True

    return False


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


def safe_handle_cdr_line(line):
    """
    Obsługuje jedną linię CDR i NIGDY nie przepuszcza wyjątku dalej.

    Dlaczego: gdyby jedna "zła" linia (np. chwilowy błąd bazy) wyrzuciła wyjątek poza pętlę
    odbioru, program zerwałby połączenie z 3CX, połączył się od nowa i zgubił resztę
    bufora. Teraz błąd jest tylko zalogowany, a następne rozmowy lecą dalej.
    """

    try:
        handle_cdr_line(line)
    except Exception as e:
        log("ERROR", f"Nieoczekiwany blad przy obsludze CDR: {type(e).__name__}: {e}")
        log("ERROR", f"Linia CDR, ktorej dotyczy blad: {line.strip()}")
        log("ERROR", traceback.format_exc())


def split_complete_lines(buffer, data):
    """
    Dokłada nowe bajty do bufora i zwraca (lista_pełnych_linii, reszta_bufora).

    Pracujemy na BAJTACH i dekodujemy dopiero całą linię. Gdyby dekodować każdą porcję
    osobno, polska litera (2 bajty w UTF-8) rozcięta granicą porcji zamieniłaby się w "�".
    """

    buffer += data
    *complete, rest = buffer.split(b"\n")

    if len(rest) > MAX_CDR_BUFFER_BYTES:
        log("ERROR", f"Bufor CDR przekroczyl {MAX_CDR_BUFFER_BYTES} bajtow bez znaku nowej linii. Czyszcze bufor.")
        rest = b""

    lines = [part.decode("utf-8", errors="replace") for part in complete]

    return lines, rest


# =========================
# POŁĄCZENIE Z 3CX
# =========================

def get_connection_duration(start_time):
    if not start_time:
        return "brak danych"
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
    
    # Etykieta aparatu: numer + nazwa z GLPI (Zasoby -> Telefony), awaryjnie z configu
    final_label = resolve_phone_display_label(cdr.get("final_dn"))

    content = (
        "Automatyczne zgloszenie utworzone na podstawie rekordu CDR z 3CX.\n\n"

        f"From: {cdr['from_no']}\n"
        f"Final: {final_label}\n\n"

        f"Target: {cdr['to_no']}\n"
        f"Duration: {cdr['duration']}"
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
            "_groups_id_requester": cfg.GLPI_HELPDESK_GROUP_ID,
            "_groups_id_assign": cfg.GLPI_HELPDESK_GROUP_ID
        }
    }

    return apply_location_to_payload(payload, location_id, cdr.get("from_dispname"))


def build_answered_call_payload(cdr, owner_id, location_id=None):
    title = f"Polaczenie telefoniczne od {get_ticket_caller_label(cdr)}"

    # Etykieta aparatu: numer + nazwa z GLPI (Zasoby -> Telefony), awaryjnie z configu
    final_label = resolve_phone_display_label(cdr.get("final_dn"))

    content = (
        "Automatyczne zgloszenie utworzone na podstawie rekordu CDR z 3CX.\n\n"

        f"From: {cdr['from_no']}\n"
        f"Final: {final_label}\n\n"

        f"Target: {cdr['to_no']}\n"
        f"Duration: {cdr['duration']}"
    )

    payload = {
        "input": {
            "name": title,
            "content": content,
            "type": 1,
            "status": cfg.GLPI_STATUS_NEW, #Jest NEW, żeby technik odpowiedzialny mógł go sobie przypisać.
            "_disablenotif": True,
            "urgency": 3,
            "impact": 3,
            "priority": 3,
            "itilcategories_id": cfg.GLPI_CATEGORY_ID,
            "_groups_id_requester": cfg.GLPI_HELPDESK_GROUP_ID,
            "_groups_id_assign": cfg.GLPI_HELPDESK_GROUP_ID
        }
    }

    #Przypisz technika tylko wtedy, gdy jest zdefiniowany
    #Ustaw status na CLOSED tylko wtedy, gdy wykryło technika (owner_id)
    if owner_id:
        payload["input"]["_users_id_assign"] = owner_id
        payload["input"]["status"] = cfg.GLPI_STATUS_CLOSED

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

                buffer = b""

                while True:
                    data = sock.recv(4096)

                    if not data:
                        raise ConnectionError(
                            "3CX zamknal polaczenie albo firewall/proxy zamknal sesje. "
                            "recv() zwrocilo 0 bajtow, czyli druga strona zamknela socket bez danych."
                        )

                    lines, buffer = split_complete_lines(buffer, data)

                    for line in lines:
                        safe_handle_cdr_line(line)

        except Exception as e:
            log("ERROR", describe_connection_error(e, connection_started_at))
            log("INFO", f"Reconnecting in {cfg.RECONNECT_DELAY}s...")
            time.sleep(cfg.RECONNECT_DELAY)


# =========================
# START
# =========================

if __name__ == "__main__":
    init_db()

    # Rozgrzewka cache telefonow, zeby pierwsze polaczenie po starcie nie czekalo na GLPI.
    try:
        refresh_glpi_phones_cache()
    except Exception as e:
        log("GLPI PHONE", f"Start: nie udalo sie pobrac telefonow z GLPI: {type(e).__name__}: {e}. Uzywam cache/config.")

    connect_to_3cx()
