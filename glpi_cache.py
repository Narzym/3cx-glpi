"""
glpi_cache.py - lokalna kopia (cache) danych z GLPI w SQLite oraz dopasowania na jej podstawie.

Trzy cache:
  - użytkownicy (glpi_users)  -> do dopasowania osoby po imieniu i nazwisku,
  - lokalizacje (glpi_locations) -> do dopasowania "sala 443" na lokalizację w GLPI,
  - telefony (glpi_phones)    -> kto aktualnie ma dany aparat IT (Zasoby -> Telefony).

Dlaczego cache? Zapytanie do GLPI trwa setki milisekund, a odczyt z lokalnej bazy ułamek
milisekundy. Dane zmieniają się rzadko, więc pobieramy je z wyprzedzeniem.

Przy odświeżaniu cache zawsze podmieniamy dane w JEDNEJ transakcji (DELETE + INSERT),
a pusta odpowiedź z GLPI nie kasuje poprzedniego cache.
"""

import json
import re
import sqlite3
import time
import unicodedata

import config as cfg
from db import (
    create_glpi_locations_table,
    create_glpi_phones_table,
    create_glpi_users_table,
    db_session,
    get_setting,
    is_verbose_log_enabled,
    set_setting,
)
from glpi_client import GLPIRightMissingError, fetch_glpi_collection
from logger import log
from utils import first_non_empty, get_now_string, names_match, normalize_name


# =========================
# UŻYTKOWNICY
# =========================

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
    rows = []

    for user in users:
        try:
            glpi_user_id = int(user.get("id"))
        except Exception:
            continue

        email = extract_user_email(user)
        first_name, last_name, full_name = build_user_names(user)

        if not full_name and not email:
            continue

        rows.append((glpi_user_id, email, full_name, first_name, last_name))

    if replace and not rows:
        log("GLPI USER", "GLPI nie zwrocilo zadnego uzytkownika do zapisania. Zostawiam poprzedni cache.")
        return 0

    with db_session() as conn:
        cursor = conn.cursor()
        create_glpi_users_table(cursor)

        if replace:
            cursor.execute("DELETE FROM glpi_users")

        cursor.executemany('''
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
        ''', rows)

    return len(rows)


def find_glpi_user_in_cache(person_name, label="osoba"):
    person_name = str(person_name or "").strip()

    if not person_name:
        log("GLPI USER", f"Brak nazwy dla '{label}', nie da sie dopasowac usera.")
        return None

    with db_session() as conn:
        rows = conn.execute('''
            SELECT glpi_user_id, email, full_name, first_name, last_name
            FROM glpi_users
        ''').fetchall()

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


def fetch_glpi_users():
    return fetch_glpi_collection("User", cfg.GLPI_USER_PAGE_SIZE)


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


# =========================
# LOKALIZACJE (SALE)
# =========================

# Jesli API user nie ma prawa do Location, nie probujemy pobierac lokalizacji online
# przy kazdym polaczeniu. Panel nadal moze sprobowac update po poprawie uprawnien.
GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = False


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
    rows = []

    for location in locations:
        try:
            glpi_location_id = int(location.get("id"))
        except Exception:
            continue

        name, full_path = build_location_name(location)
        room_number = extract_room_number_from_text(f"{name} {full_path}")

        if not name and not full_path:
            continue

        rows.append((glpi_location_id, room_number, name, full_path))

    if replace and not rows:
        log("GLPI LOCATION", "GLPI nie zwrocilo zadnej lokalizacji do zapisania. Zostawiam poprzedni cache.")
        return 0

    with db_session() as conn:
        cursor = conn.cursor()
        create_glpi_locations_table(cursor)

        if replace:
            cursor.execute("DELETE FROM glpi_locations")

        cursor.executemany('''
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
        ''', rows)

    return len(rows)


def find_glpi_location_in_cache(room_number):
    room_number = str(room_number or "").strip().lower()

    if not room_number:
        log("GLPI LOCATION", "Brak numeru sali, nie da sie dopasowac lokalizacji.")
        return None

    with db_session() as conn:
        rows = conn.execute('''
            SELECT glpi_location_id, room_number, name, full_path
            FROM glpi_locations
        ''').fetchall()

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
    global GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME

    try:
        return fetch_glpi_collection("Location", cfg.GLPI_LOCATION_PAGE_SIZE)
    except GLPIRightMissingError:
        GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = True
        raise


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
# TELEFONY (Zasoby -> Telefony)
# =========================

# Po nieudanym odswiezeniu telefonow nie probujemy ponownie przed tym czasem (time.time()),
# zeby niedostepne GLPI nie blokowalo kazdego kolejnego polaczenia.
GLPI_PHONES_RETRY_AFTER = 0.0


def normalize_phone_dn(value):
    dn = str(value or "").strip()
    prefix = cfg.IT_NUMBER_PREFIX

    if prefix and dn.startswith(prefix) and len(dn) > len(prefix):
        dn = dn[len(prefix):]

    return dn


def extract_dn_from_glpi_phone(phone):
    """
    Numer wewnetrzny 3CX bierzemy kolejno z: Numer inwentarzowy (otherserial),
    Numer seryjny (serial) i poczatku nazwy (np. "519 SAMSUNG").
    Przyjmujemy WYLACZNIE numery z IT_NUMBERS_BASE, zeby inny sprzet z GLPI
    (np. z przypadkowo takim samym numerem inwentarzowym) nie podszyl sie pod aparat IT.
    """

    candidates = [phone.get("otherserial"), phone.get("serial")]

    name_match = re.match(r"^\s*(\d{3,12})\b", str(phone.get("name") or ""))

    if name_match:
        candidates.append(name_match.group(1))

    for candidate in candidates:
        dn = normalize_phone_dn(candidate)

        if dn in cfg.IT_NUMBERS_BASE:
            return dn

    return ""


def clean_glpi_phone_label(raw_name, dn):
    # "519 SAMSUNG" -> "SAMSUNG", zeby w tickecie nie bylo "519 (519 SAMSUNG)".
    name = str(raw_name or "").strip()
    prefix = re.escape(cfg.IT_NUMBER_PREFIX or "")

    return re.sub(rf"^(?:{prefix})?{re.escape(dn)}\s*[-:|]?\s*", "", name).strip()


def replace_glpi_phones_cache(phones):
    """
    Podmienia cache telefonow w jednej transakcji (bez DROP TABLE), wiec rownolegly odczyt
    nigdy nie zobaczy pustej tabeli, a blad w polowie nie zostawi po sobie zepsutego cache.
    Zwraca liczbe zapisanych telefonow.
    """

    by_dn = {}

    for phone in phones:
        if not isinstance(phone, dict):
            continue

        if phone.get("is_deleted") or phone.get("is_template"):
            continue

        try:
            glpi_phone_id = int(phone.get("id"))
        except (TypeError, ValueError):
            continue

        dn = extract_dn_from_glpi_phone(phone)

        if not dn:
            continue

        try:
            glpi_user_id = int(phone.get("users_id") or 0)
        except (TypeError, ValueError):
            glpi_user_id = 0

        entry = (
            glpi_phone_id,
            dn,
            clean_glpi_phone_label(phone.get("name"), dn),
            glpi_user_id if glpi_user_id > 0 else None
        )

        if dn in by_dn:
            kept = max(by_dn[dn][0], glpi_phone_id)
            log("GLPI PHONE", f"UWAGA: numer {dn} jest w GLPI w kilku telefonach (id={by_dn[dn][0]} i id={glpi_phone_id}). Uzywam id={kept}. Usun duplikat w GLPI.")

            if glpi_phone_id < by_dn[dn][0]:
                continue

        by_dn[dn] = entry

    if not by_dn:
        log("GLPI PHONE", "GLPI nie zwrocilo zadnego telefonu z numerem IT (otherserial/serial/nazwa z IT_NUMBERS_BASE). Zostawiam poprzedni cache.")
        return 0

    now_str = get_now_string()

    with db_session() as conn:
        cursor = conn.cursor()
        create_glpi_phones_table(cursor)
        cursor.execute("DELETE FROM glpi_phones")
        cursor.executemany('''
            INSERT INTO glpi_phones (glpi_phone_id, dn, phone_name, glpi_user_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', [entry + (now_str,) for entry in by_dn.values()])

    return len(by_dn)


def fetch_glpi_phones():
    return fetch_glpi_collection("Phone", cfg.GLPI_PHONE_PAGE_SIZE)


def sync_glpi_phones():
    phones = fetch_glpi_phones()
    count = replace_glpi_phones_cache(phones)
    set_setting("glpi_phones_last_sync", str(time.time()))
    return count


def refresh_glpi_phones_cache():
    # Reczne odswiezenie z panelu admina. Bledy leca wyzej, zeby panel je pokazal.
    log("GLPI PHONE", "Pobieram telefony z GLPI do cache...")
    count = sync_glpi_phones()
    log("GLPI PHONE", f"Zaktualizowano cache telefonow GLPI. Zapisano: {count}")
    return count


def ensure_glpi_phones_fresh():
    global GLPI_PHONES_RETRY_AFTER

    now = time.time()

    if now < GLPI_PHONES_RETRY_AFTER:
        return

    try:
        last_sync = float(get_setting("glpi_phones_last_sync", "0") or 0)
    except ValueError:
        last_sync = 0.0

    if now - last_sync < cfg.GLPI_PHONES_CACHE_TTL:
        return

    try:
        count = sync_glpi_phones()
        log("GLPI PHONE", f"Auto-sync telefonow OK (TTL={cfg.GLPI_PHONES_CACHE_TTL}s). W cache: {count}")
    except Exception as e:
        GLPI_PHONES_RETRY_AFTER = time.time() + 15
        log("GLPI PHONE", f"Auto-sync telefonow FAIL: {type(e).__name__}: {e}. Uzywam ostatniego cache/config. Kolejna proba za 15s.")


def get_glpi_phone_by_dn(dn):
    short_dn = normalize_phone_dn(dn)

    if not short_dn:
        return None

    ensure_glpi_phones_fresh()

    try:
        with db_session() as conn:
            row = conn.execute('''
                SELECT glpi_phone_id, dn, phone_name, glpi_user_id
                FROM glpi_phones
                WHERE dn = ?
                LIMIT 1
            ''', (short_dn,)).fetchone()

    except sqlite3.Error as e:
        log("GLPI PHONE", f"Blad odczytu glpi_phones z SQLite: {type(e).__name__}: {e}")
        return None

    if not row:
        return None

    return {
        "glpi_phone_id": row[0],
        "dn": row[1],
        "phone_name": row[2],
        "glpi_user_id": row[3],
    }


def resolve_phone_display_label(dn):
    dn_clean = str(dn or "").strip()

    if not dn_clean:
        return ""

    phone_entry = get_glpi_phone_by_dn(dn_clean)

    if phone_entry and phone_entry.get("phone_name"):
        return f"{dn_clean} ({phone_entry['phone_name']})"

    fallback_name = cfg.PHONE_NAME_BY_FINAL_DN.get(dn_clean)
    return f"{dn_clean} ({fallback_name})" if fallback_name else dn_clean
