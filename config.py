import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# =========================
# KONFIGURACJA APLIKACJI
# =========================

APP_NAME = "3cxToGLPI"
APP_VERSION = "1.13"
SERVICE_NAME = os.getenv("SERVICE_NAME", "cdr_to_glpi.service")
DB_PATH = os.getenv("DB_PATH", "cdrdatabase.db")
LOG_PATH = os.getenv("LOG_PATH", "logs/cdr_to_glpi.log")

# Wartosc moze byc liczba >= 10 albo "unlimited".
CDR_MAX_RECORDS_DEFAULT = os.getenv("CDR_MAX_RECORDS_DEFAULT", "500")

# Domyslne ustawienia verbose logow. Panel zapisuje prawdziwy stan w SQLite/settings.
# Domyslnie payloady sa wylaczone, zeby log nie rosl jak mutant po kawie.
LOG_CDR_PAYLOAD_DEFAULT = os.getenv("LOG_CDR_PAYLOAD_DEFAULT", "0") == "1"
LOG_GLPI_USER_SAMPLE_PAYLOAD_DEFAULT = os.getenv("LOG_GLPI_USER_SAMPLE_PAYLOAD_DEFAULT", "0") == "1"
LOG_GLPI_LOCATION_SAMPLE_PAYLOAD_DEFAULT = os.getenv("LOG_GLPI_LOCATION_SAMPLE_PAYLOAD_DEFAULT", "0") == "1"
# Stara zmienna zostaje jako alias awaryjny, ale nie wlacza juz nic sama z siebie.
CDR_DEBUG_FULL_PAYLOAD = LOG_CDR_PAYLOAD_DEFAULT

# =========================
# KONFIGURACJA 3CX
# =========================

HOST = os.getenv("THREECX_HOST", "212.180.155.28")
PORT = int(os.getenv("THREECX_PORT", "5001"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))

IT_NUMBER_PREFIX = os.getenv("IT_NUMBER_PREFIX", "225435")

IT_NUMBERS_BASE = {"517", "699", "717", "519", "524", "526", "528", "529"}


def expand_with_prefix(numbers, prefix=IT_NUMBER_PREFIX):
    expanded = set()

    for number in numbers:
        number = str(number).strip()

        if not number:
            continue

        expanded.add(number)

        if prefix and not number.startswith(prefix):
            expanded.add(prefix + number)

    return expanded


# Np. 517 oraz 225435517.
IT_NUMBERS = expand_with_prefix(IT_NUMBERS_BASE)

# =========================
# KONFIGURACJA TCP KEEPALIVE
# =========================

TCP_KEEPALIVE_IDLE = int(os.getenv("TCP_KEEPALIVE_IDLE", "60"))
TCP_KEEPALIVE_INTERVAL = int(os.getenv("TCP_KEEPALIVE_INTERVAL", "10"))
TCP_KEEPALIVE_COUNT = int(os.getenv("TCP_KEEPALIVE_COUNT", "3"))
TCP_USER_TIMEOUT_MS = int(os.getenv("TCP_USER_TIMEOUT_MS", "30000"))

# =========================
# KONFIGURACJA CZASU
# =========================

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
MISSED_CALL_START_HOUR = int(os.getenv("MISSED_CALL_START_HOUR", "8"))
MISSED_CALL_END_HOUR = int(os.getenv("MISSED_CALL_END_HOUR", "20"))

# =========================
# KONFIGURACJA GLPI REST API V1
# =========================

GLPI_API_URL = os.getenv("GLPI_API_URL")
GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = os.getenv("GLPI_USER_TOKEN")
GLPI_HTTP_TIMEOUT = int(os.getenv("GLPI_HTTP_TIMEOUT", "15"))
GLPI_USER_PAGE_SIZE = int(os.getenv("GLPI_USER_PAGE_SIZE", "200"))
GLPI_LOCATION_PAGE_SIZE = int(os.getenv("GLPI_LOCATION_PAGE_SIZE", "200"))

GLPI_SET_CALLER_AS_REQUESTER = os.getenv("GLPI_SET_CALLER_AS_REQUESTER", "1") == "1"
GLPI_SET_CALLER_AS_RECIPIENT = os.getenv("GLPI_SET_CALLER_AS_RECIPIENT", "1") == "1"
GLPI_SET_ROOM_AS_LOCATION = os.getenv("GLPI_SET_ROOM_AS_LOCATION", "1") == "1"

# Przy polaczeniu z sali najpierw szukamy lokalizacji w cache.
# Domyslnie NIE odpalamy fetch z GLPI przy kazdym cache miss, zeby brak uprawnien 403
# nie spamowal logow i nie spowalnial ticketow. Lokalizacje odswiezaj z panelu.
GLPI_AUTO_FETCH_LOCATIONS_ON_MISS = os.getenv("GLPI_AUTO_FETCH_LOCATIONS_ON_MISS", "0") == "1"

# =========================
# MAPOWANIE GLPI
# =========================

GLPI_HELPDESK_GROUP_ID = int(os.getenv("GLPI_HELPDESK_GROUP_ID", "27"))
GLPI_CATEGORY_ID = int(os.getenv("GLPI_CATEGORY_ID", "16"))
GLPI_STATUS_NEW = int(os.getenv("GLPI_STATUS_NEW", "1"))
GLPI_STATUS_CLOSED = int(os.getenv("GLPI_STATUS_CLOSED", "6"))

# Nieodebrane rozpoznajemy po final-dn + duration=0.
# Awaryjnie kod moze sprawdzic to-no, jesli final-dn bedzie puste.
MISSED_CALL_FINAL_DNS = expand_with_prefix(IT_NUMBERS_BASE)
MISSED_CALL_TO_NUMBERS = MISSED_CALL_FINAL_DNS

OWNER_BY_FINAL_DN_BASE = {
    "519": 193,     # Grzegorz
    "524": 172,     # Mateusz
    "526": 154,     # Wojciech
    "528": 213,     # Michal
    "529": 187      # Jan
}

OWNER_BY_FINAL_DN = {}
for number, user_id in OWNER_BY_FINAL_DN_BASE.items():
    OWNER_BY_FINAL_DN[number] = user_id
    if IT_NUMBER_PREFIX and not number.startswith(IT_NUMBER_PREFIX):
        OWNER_BY_FINAL_DN[IT_NUMBER_PREFIX + number] = user_id

# Fallback dla sytuacji, gdy final-dn bedzie puste albo 3CX dziwnie poda numer tylko w to-no.
OWNER_BY_TO_NO_BASE = OWNER_BY_FINAL_DN_BASE
OWNER_BY_TO_NO = dict(OWNER_BY_FINAL_DN)
