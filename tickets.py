"""
tickets.py - logika biznesowa: "komu przypisać ticket, jaką dać lokalizację i co w nim napisać".

Tu NIE ma gniazd, SQL-a ani HTTP. Ten plik tylko składa decyzje z danych, które dostarczają
glpi_cache (dopasowania) i cdr_to_glpi (rekord rozmowy), i buduje gotowy payload ticketu.
"""

import config as cfg
from glpi_cache import (
    extract_room_number_from_caller_dispname,
    fetch_and_cache_users_silent,
    find_glpi_user_in_cache,
    from_dispname_has_sala_keyword,
    get_glpi_phone_by_dn,
    resolve_location_id_by_room,
    resolve_phone_display_label,
)
from logger import log
from utils import first_non_empty


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

    # 1. Priorytet: GLPI -> Zasoby -> Telefony (pole "Uzytkownik" = kto ma aparat teraz).
    # Jesli aparat jest w GLPI, to GLPI rozstrzyga - rowniez gdy nikt go nie ma (wtedy ticket bez realizatora).
    phone_entry = get_glpi_phone_by_dn(final_dn or to_no)

    if phone_entry is not None:
        glpi_user_id = phone_entry.get("glpi_user_id")
        phone_name = phone_entry.get("phone_name") or ""

        if glpi_user_id:
            log("GLPI OWNER", f"Owner z zasobow GLPI: '{phone_entry['dn']}' ({phone_name}) -> GLPI user id={glpi_user_id}")
            return glpi_user_id

        log("GLPI OWNER", f"Telefon '{phone_entry['dn']}' ({phone_name}) nie ma w GLPI przypisanego uzytkownika. Ticket pojdzie bez realizatora.")
        return None

    # 2. Fallback z config.py (kolejki 517/699, telefon spoza GLPI albo cache pusty i GLPI niedostepne)
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
