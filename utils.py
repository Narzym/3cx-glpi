"""
utils.py - małe, "czyste" funkcje pomocnicze.

"Czysta" funkcja bierze argumenty i zwraca wynik. Nie dotyka bazy, sieci ani plików,
więc łatwo ją przetestować i można jej użyć w dowolnym miejscu programu.
"""

import re
import unicodedata
from datetime import datetime

import config as cfg


def truncate_text(value, limit=3000):
    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + f"\n... [ucieto, calosc miala {len(value)} znakow]"


def get_now_string():
    return datetime.now(cfg.TIMEZONE).strftime("%d-%m-%Y %H:%M:%S")


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue

        value = str(value).strip()

        if value:
            return value

    return ""


def normalize_name(value):
    value = str(value or "").strip()
    # "l" z kreska nie rozklada sie w NFKD na litere + akcent, wiec zamieniamy je recznie.
    value = value.replace("ł", "l").replace("Ł", "L")
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
