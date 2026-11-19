"""
glpi_client.py - cała komunikacja HTTP z GLPI REST API v1.

Reszta programu nie musi wiedzieć, jak wygląda nagłówek Range, co znaczy kod 206
ani jak otwiera się sesję API. Woła tylko:
    fetch_glpi_collection("User", 200)   - pobranie listy (z automatyczną paginacją),
    create_glpi_ticket(payload)          - utworzenie ticketu.

Ponawianie żądań (retry):
  - GET (initSession, listy zasobów) ponawiamy przy chwilowych błędach: 429/500/502/503/504,
    zerwanym połączeniu itp.
  - POST (tworzenie ticketu) ponawiamy WYŁĄCZNIE wtedy, gdy nie udało się nawet nawiązać
    połączenia (żądanie nie dotarło do serwera). Po błędzie 5xx NIE ponawiamy, bo GLPI mogło
    ticket już utworzyć i ponowienie dałoby duplikat.
"""

from contextlib import contextmanager

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config as cfg
from logger import log
from utils import truncate_text


# =========================
# BŁĘDY
# =========================

class GLPIRequestError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class GLPIRightMissingError(GLPIRequestError):
    """403 ERROR_RIGHT_MISSING: profil konta API nie ma prawa odczytu danego typu zasobu."""


# =========================
# SESJA HTTP Z RETRY
# =========================

RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

_http_session = None


def build_retry(retries):
    options = dict(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=cfg.GLPI_HTTP_BACKOFF,
        status_forcelist=RETRY_STATUS_CODES,
        raise_on_status=False,  # po wyczerpaniu prób oddaj odpowiedź, my ją obsłużymy
        respect_retry_after_header=True,
    )

    try:
        return Retry(allowed_methods=frozenset({"GET"}), **options)
    except TypeError:
        # Stara biblioteka urllib3 (< 1.26) nazywa ten parametr inaczej.
        return Retry(method_whitelist=frozenset({"GET"}), **options)


def create_http_session():
    session = requests.Session()
    retries = max(0, cfg.GLPI_HTTP_RETRIES)

    adapter = HTTPAdapter(max_retries=build_retry(retries) if retries else 0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def get_http_session():
    """Jedna wspólna sesja HTTP: ponownie używa połączeń (szybciej niż nowe TCP/TLS za każdym razem)."""

    global _http_session

    if _http_session is None:
        _http_session = create_http_session()

    return _http_session


def reset_http_session():
    global _http_session

    if _http_session is not None:
        _http_session.close()

    _http_session = None


def request_timeout():
    # (limit na nawiązanie połączenia, limit na odpowiedź)
    return (cfg.GLPI_HTTP_CONNECT_TIMEOUT, cfg.GLPI_HTTP_TIMEOUT)


# =========================
# POMOCNICZE
# =========================

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
        "W GLPI nadaj profilowi tego usera prawo Odczyt/Read do tego typu zasobu "
        "(Telefony: Administracja -> Profile -> Zasoby; Lokalizacje: Listy rozwijane), "
        "albo uzyj tokena usera z takim prawem. "
        f"Body: {get_response_text(response)}"
    )


def glpi_headers(session_token, extra=None):
    headers = {
        "Content-Type": "application/json",
        "App-Token": cfg.GLPI_APP_TOKEN,
        "Session-Token": session_token
    }

    if extra:
        headers.update(extra)

    return headers


# =========================
# SESJA API GLPI
# =========================

def get_glpi_session_token():
    check_glpi_env()

    url = glpi_url("initSession")

    headers = {
        "Content-Type": "application/json",
        "App-Token": cfg.GLPI_APP_TOKEN,
        "Authorization": f"user_token {cfg.GLPI_USER_TOKEN}"
    }

    try:
        response = get_http_session().get(
            url,
            headers=headers,
            timeout=request_timeout()
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

    try:
        get_http_session().get(
            url,
            headers=glpi_headers(session_token),
            timeout=(cfg.GLPI_HTTP_CONNECT_TIMEOUT, 10)
        )
    except Exception as e:
        log("GLPI ERROR", f"Nie udalo sie zamknac sesji: {type(e).__name__}: {e}")


@contextmanager
def glpi_session():
    """
    Otwiera sesję API i GWARANTUJE jej zamknięcie, nawet gdy w środku poleci wyjątek.

    Użycie:
        with glpi_session() as token:
            ...
    """

    session_token = get_glpi_session_token()

    try:
        yield session_token
    finally:
        kill_glpi_session(session_token)


# =========================
# POBIERANIE LIST ZASOBÓW (User, Location, Phone ...)
# =========================

def fetch_glpi_collection(itemtype, page_size, extra_params=None):
    """
    Pobiera wszystkie rekordy danego typu (np. "User", "Location", "Phone").

    GLPI oddaje dane porcjami. Prosimy o zakres "0-199", potem "200-399" itd.
    Kod 206 oznacza "to nie wszystko, są dalsze strony", kod 200 "to już koniec".
    """

    records = []
    start = 0

    with glpi_session() as session_token:
        while True:
            end = start + page_size - 1

            headers = glpi_headers(session_token, {"Range": f"{start}-{end}"})

            params = {
                "range": f"{start}-{end}",
                "is_deleted": "0"
            }

            if extra_params:
                params.update(extra_params)

            try:
                response = get_http_session().get(
                    glpi_url(itemtype),
                    headers=headers,
                    params=params,
                    timeout=request_timeout()
                )
            except requests.exceptions.RequestException as e:
                raise GLPIRequestError(
                    f"Blad polaczenia przy pobieraniu {itemtype} z GLPI: {type(e).__name__}: {e}"
                )

            if response.status_code == 403 and response_has_glpi_error(response, "ERROR_RIGHT_MISSING"):
                raise GLPIRightMissingError(
                    build_glpi_right_missing_message(itemtype, response),
                    response.status_code
                )

            if response.status_code not in (200, 206):
                raise GLPIRequestError(
                    f"GLPI {itemtype} fetch error. HTTP {response.status_code} {response.reason}. Body: {get_response_text(response)}",
                    response.status_code
                )

            try:
                data = response.json()
            except Exception as e:
                raise GLPIRequestError(
                    f"GLPI {itemtype} zwrocilo niepoprawny JSON. HTTP {response.status_code}. Error: {e}. Body: {get_response_text(response)}",
                    response.status_code
                )

            if isinstance(data, dict):
                data = data.get("data") or data.get("items") or []

            if not isinstance(data, list):
                raise GLPIRequestError(f"GLPI {itemtype} zwrocilo dziwny format: {type(data).__name__}")

            records.extend(data)

            if len(data) < page_size or response.status_code == 200:
                break

            start += page_size

            if start > 100000:
                raise GLPIRequestError(f"Przerwano pobieranie {itemtype} z GLPI: przekroczono 100000 rekordow")

    return records


# =========================
# TWORZENIE TICKETU
# =========================

def create_glpi_ticket(payload):
    with glpi_session() as session_token:
        try:
            response = get_http_session().post(
                glpi_url("Ticket"),
                headers=glpi_headers(session_token),
                json=payload,
                timeout=request_timeout()
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
