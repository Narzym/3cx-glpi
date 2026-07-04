import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# =========================
# KONFIGURACJA APLIKACJI
# =========================

APP_NAME = "3cxToGLPI"
SERVICE_NAME = os.getenv("SERVICE_NAME", "cdr_to_glpi.service")
DB_PATH = os.getenv("DB_PATH", "cdrdatabase.db")
LOG_PATH = os.getenv("LOG_PATH", "logs/cdr_to_glpi.log")

# Jezeli w bazie nie ma ustawienia cdr_max_records, ten limit bedzie uzyty jako domyslny.
# Wartosc moze byc liczba >= 10 albo "unlimited".
CDR_MAX_RECORDS_DEFAULT = os.getenv("CDR_MAX_RECORDS_DEFAULT", "500")

# =========================
# KONFIGURACJA 3CX
# =========================

HOST = os.getenv("THREECX_HOST", "212.180.155.28")
PORT = int(os.getenv("THREECX_PORT", "5001"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))

IT_NUMBERS = {"517", "699", "717", "519", "524", "526", "528", "529"}

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

# Przy tworzeniu ticketu ustawiamy osobe dzwoniaca jako requester.
# Dodatkowo probujemy ustawic users_id_recipient, czyli pole najblizsze "tworcy"/"otwierajacemu" ticket.
# Jezeli GLPI tego nie przyjmie przez uprawnienia, ustaw na False.
GLPI_SET_CALLER_AS_REQUESTER = os.getenv("GLPI_SET_CALLER_AS_REQUESTER", "1") == "1"
GLPI_SET_CALLER_AS_RECIPIENT = os.getenv("GLPI_SET_CALLER_AS_RECIPIENT", "1") == "1"

# =========================
# MAPOWANIE GLPI
# =========================

GLPI_HELPDESK_GROUP_ID = int(os.getenv("GLPI_HELPDESK_GROUP_ID", "27"))
GLPI_CATEGORY_ID = int(os.getenv("GLPI_CATEGORY_ID", "16"))
GLPI_STATUS_NEW = int(os.getenv("GLPI_STATUS_NEW", "1"))
GLPI_STATUS_CLOSED = int(os.getenv("GLPI_STATUS_CLOSED", "6"))

MISSED_CALL_FINAL_DNS = {"517", "699", "717", "519", "524", "526", "528", "529"}

OWNER_BY_FINAL_DN = {
    "519": 193,     # Grzegorz
    "524": 172,     # Mateusz
    "526": 154,     # Wojciech
    "528": 213,     # Michal
    "529": 187      # Jan
}
