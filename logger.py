"""
logger.py - logowanie do konsoli (journald) i do pliku, z automatyczną rotacją pliku.

Plik logów nie rośnie w nieskończoność: gdy przekroczy LOG_MAX_BYTES, jest przesuwany
do cdr_to_glpi.log.1, a starsze kopie jadą dalej (.2, .3 ...). Zostaje maksymalnie
LOG_BACKUP_COUNT kopii, najstarsza jest usuwana.
"""

import os
from datetime import datetime

import config as cfg


def get_log_timestamp():
    return datetime.now(cfg.TIMEZONE).strftime("%y-%m-%d %H:%M:%S")


def rotate_log_if_needed(path):
    """Jeśli plik jest za duży, przesuwa go na .1 (a stare kopie na .2, .3 ...)."""

    max_bytes = cfg.LOG_MAX_BYTES
    backups = cfg.LOG_BACKUP_COUNT

    if max_bytes <= 0:
        return

    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        # Pliku jeszcze nie ma - nie ma czego rotować.
        return

    try:
        if backups <= 0:
            os.remove(path)
            return

        # Od najstarszej kopii do najnowszej: .4 -> .5, .3 -> .4, ... (najstarsza ginie).
        for index in range(backups - 1, 0, -1):
            source = f"{path}.{index}"

            if os.path.exists(source):
                os.replace(source, f"{path}.{index + 1}")

        os.replace(path, f"{path}.1")

    except OSError:
        # Logowanie nigdy nie może wywrócić programu.
        pass


def write_log_to_file(log_line):
    try:
        log_dir = os.path.dirname(cfg.LOG_PATH)

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        rotate_log_if_needed(cfg.LOG_PATH)

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
