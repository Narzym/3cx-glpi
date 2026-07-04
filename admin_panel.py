import curses
import os
import sqlite3
import subprocess
import textwrap
import io
import contextlib
import sys

import config as cfg
import cdr_to_glpi as core


COLOR_GREEN = 1
COLOR_RED = 2
COLOR_YELLOW = 3
COLOR_CYAN = 4
COLOR_REVERSE = 5


class ScreenExit(Exception):
    pass


def safe_addstr(screen, y, x, text, attr=0):
    height, width = screen.getmaxyx()

    if y < 0 or y >= height:
        return

    if x < 0 or x >= width:
        return

    text = str(text)
    max_len = max(0, width - x - 1)

    if max_len <= 0:
        return

    screen.addstr(y, x, text[:max_len], attr)


def draw_box(screen):
    height, width = screen.getmaxyx()

    if height < 5 or width < 30:
        screen.clear()
        safe_addstr(screen, 0, 0, "Terminal za maly. Powieksz okno.")
        screen.refresh()
        return False

    screen.border()
    return True


def is_service_running():
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", cfg.SERVICE_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return result.returncode == 0


def get_service_status_label():
    if is_service_running():
        return "Running", curses.color_pair(COLOR_GREEN) | curses.A_BOLD

    return "Stopped", curses.color_pair(COLOR_RED) | curses.A_BOLD


def run_systemctl(action):
    result = subprocess.run(
        ["systemctl", action, cfg.SERVICE_NAME],
        capture_output=True,
        text=True,
        timeout=30
    )

    output = ""

    if result.stdout.strip():
        output += result.stdout.strip()

    if result.stderr.strip():
        if output:
            output += "\n"
        output += result.stderr.strip()

    if not output:
        output = f"systemctl {action} {cfg.SERVICE_NAME}: OK"

    return result.returncode, output


def get_systemd_status_text():
    result = subprocess.run(
        ["systemctl", "status", cfg.SERVICE_NAME, "--no-pager", "--lines=12"],
        capture_output=True,
        text=True,
        timeout=30
    )

    output = ""

    if result.stdout.strip():
        output += result.stdout.strip()

    if result.stderr.strip():
        if output:
            output += "\n"
        output += result.stderr.strip()

    if not output:
        output = "Brak outputu z systemctl status."

    return output


def draw_header(screen, subtitle=None, footer_text="↑/↓ wybierz  Enter klik  q/Backspace wstecz"):
    if not draw_box(screen):
        return

    width = screen.getmaxyx()[1]

    title = "3cxToGLPI Admin Panel"
    safe_addstr(screen, 1, max(2, (width - len(title)) // 2), title, curses.A_BOLD | curses.color_pair(COLOR_CYAN))

    status, attr = get_service_status_label()
    safe_addstr(screen, 3, 3, "Status: ", curses.A_BOLD)
    safe_addstr(screen, 3, 11, status, attr)

    stats = core.get_db_stats()
    stats_text = f"CDR: {stats['cdr_count']} | CDR limit: {stats['cdr_max_records']} | GLPI users cache: {stats['glpi_users_count']} | Service: {cfg.SERVICE_NAME}"
    safe_addstr(screen, 4, 3, stats_text, curses.color_pair(COLOR_YELLOW))

    if subtitle:
        safe_addstr(screen, 6, 3, subtitle, curses.A_BOLD)

    if footer_text:
        safe_addstr(screen, screen.getmaxyx()[0] - 2, 3, footer_text, curses.A_DIM)


def wait_key(screen):
    safe_addstr(screen, screen.getmaxyx()[0] - 2, 3, "Nacisnij dowolny klawisz, zeby wrocic...", curses.A_DIM)
    screen.refresh()
    screen.getch()


def build_wrapped_lines(text, width):
    lines = []
    wrap_width = max(20, width - 8)

    for line in str(text).splitlines():
        if not line:
            lines.append("")
            continue

        wrapped = textwrap.wrap(
            line,
            wrap_width,
            replace_whitespace=False,
            drop_whitespace=False
        ) or [""]
        lines.extend(wrapped)

    if not lines:
        lines = [""]

    return lines


def show_text(screen, title, text, start_at_bottom=False):
    scroll = 0
    first_draw = True
    lines_cache = None
    cache_width = None

    while True:
        screen.clear()
        draw_header(screen, title, footer_text=None)

        height, width = screen.getmaxyx()

        if lines_cache is None or cache_width != width:
            lines_cache = build_wrapped_lines(text, width)
            cache_width = width

        lines = lines_cache
        visible_height = max(1, height - 11)
        max_scroll = max(0, len(lines) - visible_height)

        if first_draw and start_at_bottom:
            scroll = max_scroll
            first_draw = False
        else:
            first_draw = False
            scroll = min(max(0, scroll), max_scroll)

        visible = lines[scroll:scroll + visible_height]

        y = 8
        for line in visible:
            safe_addstr(screen, y, 4, line)
            y += 1

        start_line = min(scroll + 1, len(lines))
        end_line = min(scroll + visible_height, len(lines))
        safe_addstr(
            screen,
            height - 2,
            3,
            f"Linie {start_line}-{end_line}/{len(lines)} | ↑/↓ scroll | Enter wstecz",
            curses.A_DIM
        )
        screen.refresh()

        key = screen.getch()

        if key in (10, 13, curses.KEY_ENTER):
            return

        if key == curses.KEY_UP and scroll > 0:
            scroll -= 1
        elif key == curses.KEY_DOWN and scroll < max_scroll:
            scroll += 1


def confirm(screen, question):
    screen.clear()
    draw_header(screen, "Potwierdzenie")
    safe_addstr(screen, 8, 4, question, curses.A_BOLD | curses.color_pair(COLOR_YELLOW))
    safe_addstr(screen, 10, 4, "T = tak, N = nie")
    screen.refresh()

    while True:
        key = screen.getch()

        if key in (ord("t"), ord("T")):
            return True

        if key in (ord("n"), ord("N"), ord("q"), ord("Q"), 27):
            return False


def prompt_text(screen, title, question, default=""):
    curses.echo()
    curses.curs_set(1)

    try:
        screen.clear()
        draw_header(screen, title)
        safe_addstr(screen, 8, 4, question)
        safe_addstr(screen, 10, 4, f"Aktualnie/domyslnie: {default}", curses.A_DIM)
        safe_addstr(screen, 12, 4, "> ", curses.A_BOLD)
        screen.refresh()

        value = screen.getstr(12, 6, 80).decode("utf-8", errors="replace").strip()

        if not value:
            value = default

        return value

    finally:
        curses.noecho()
        curses.curs_set(0)


def run_action_and_show(screen, title, func):
    screen.clear()
    draw_header(screen, title)
    safe_addstr(screen, 8, 4, "Wykonuje akcje...", curses.color_pair(COLOR_YELLOW) | curses.A_BOLD)
    screen.refresh()

    captured = io.StringIO()

    try:
        with contextlib.redirect_stdout(captured):
            result = func()

        log_text = captured.getvalue().strip()
        message = "OK"

        if result is not None:
            message += f"\nWynik: {result}"

        if log_text:
            message += "\n\nLogi z akcji:\n" + log_text

        show_text(screen, title, message)

    except Exception as e:
        log_text = captured.getvalue().strip()
        message = f"BLAD: {type(e).__name__}: {e}"

        if log_text:
            message += "\n\nLogi przed bledem:\n" + log_text

        show_text(screen, title, message)


def run_systemctl_and_show(screen, action):
    def do_action():
        code, output = run_systemctl(action)
        return f"Kod wyjscia: {code}\n{output}"

    run_action_and_show(screen, f"systemctl {action}", do_action)


def toggle_service(screen):
    action = "stop" if is_service_running() else "start"
    run_systemctl_and_show(screen, action)


def reset_cdr_db(screen):
    if confirm(screen, "Na pewno wyzerowac baze polaczen it_cdr?"):
        run_action_and_show(screen, "Wyzeruj baze polaczen", core.reset_it_cdr)


def reset_glpi_users_db(screen):
    if confirm(screen, "Na pewno wyzerowac cache userow GLPI?"):
        run_action_and_show(screen, "Wyzeruj baze userow GLPI", core.reset_glpi_users)


def set_cdr_limit(screen):
    current = core.get_cdr_max_records_label()
    value = prompt_text(
        screen,
        "Limit bazy polaczen",
        "Podaj limit rekordow CDR: liczba >= 10 albo unlimited",
        current
    )

    def do_action():
        new_value = core.set_cdr_max_records(value)
        return f"Nowy limit: {new_value}"

    run_action_and_show(screen, "Ustaw limit CDR", do_action)


def refresh_glpi_users(screen):
    run_action_and_show(screen, "Update bazy userow GLPI", core.refresh_glpi_users_cache)


def show_db_stats(screen):
    stats = core.get_db_stats()
    text = (
        f"Baza SQLite: {cfg.DB_PATH}\n"
        f"Rekordy CDR: {stats['cdr_count']}\n"
        f"Limit CDR: {stats['cdr_max_records']}\n"
        f"Userzy GLPI w cache: {stats['glpi_users_count']}\n"
    )
    show_text(screen, "Statystyki bazy", text)


def show_systemd_status(screen):
    show_text(screen, "Status systemd", get_systemd_status_text())


def read_full_log_file():
    log_path = cfg.LOG_PATH

    if not log_path:
        return "LOG_PATH nie jest ustawiony w configu."

    if not os.path.exists(log_path):
        return f"Plik logow nie istnieje jeszcze: {log_path}"

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as file:
            content = file.read()
    except Exception as e:
        return f"Nie udalo sie odczytac logow: {type(e).__name__}: {e}"

    if not content.strip():
        return f"Plik logow jest pusty: {log_path}"

    line_count = len(content.splitlines())
    header = [
        f"Plik logow: {log_path}",
        f"Pokazuje caly plik: {line_count} linii.",
        ""
    ]

    return "\n".join(header) + "\n" + content.rstrip()


def show_logs(screen):
    log_path = cfg.LOG_PATH or "brak LOG_PATH"
    show_text(screen, f"Logi: {log_path}", read_full_log_file(), start_at_bottom=True)


def format_sqlite_query_result(query):
    conn = sqlite3.connect(cfg.DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
    finally:
        conn.close()

    output = [
        f"SQLite DB: {cfg.DB_PATH}",
        f"Query: {query}",
        f"Liczba rekordow: {len(rows)}",
        ""
    ]

    if not rows:
        output.append("Brak rekordow. Baza czysta jak stol po audycie.")
        return "\n".join(output)

    output.append(" | ".join(columns))
    output.append("-" * 120)

    for row in rows:
        cells = []

        for value in row:
            cell = "" if value is None else str(value)
            cell = cell.replace("\r", " ").replace("\n", " ")

            if len(cell) > 160:
                cell = cell[:160] + "... [ucieto]"

            cells.append(cell)

        output.append(" | ".join(cells))

    return "\n".join(output)


def show_sqlite_query(screen, title, query):
    def do_query():
        return format_sqlite_query_result(query)

    run_action_and_show(screen, title, do_query)


def show_it_cdr_records(screen):
    show_sqlite_query(screen, "Rekordy bazy polaczen", "SELECT * FROM it_cdr")


def show_glpi_users_records(screen):
    show_sqlite_query(
        screen,
        "Rekordy bazy userow GLPI",
        "SELECT glpi_user_id, email, full_name, first_name, last_name FROM glpi_users ORDER BY glpi_user_id"
    )


def draw_menu(screen, title, items, selected_index):
    screen.clear()
    draw_header(screen, title)

    height, width = screen.getmaxyx()
    start_y = 8
    item_rows = []

    for index, item in enumerate(items):
        y = start_y + index * 2

        if y >= height - 3:
            break

        label = item["label"]() if callable(item["label"]) else item["label"]
        prefix = "➜ " if index == selected_index else "  "
        attr = curses.A_NORMAL

        if index == selected_index:
            attr = curses.color_pair(COLOR_REVERSE) | curses.A_BOLD

        safe_addstr(screen, y, 4, (prefix + label).ljust(max(10, width - 9)), attr)
        item_rows.append((y, index))

    return item_rows


def menu_loop(screen, title, build_items):
    selected = 0

    while True:
        items = build_items()

        if selected >= len(items):
            selected = max(0, len(items) - 1)

        item_rows = draw_menu(screen, title, items, selected)
        screen.refresh()

        key = screen.getch()

        if key in (ord("q"), ord("Q"), 27, curses.KEY_BACKSPACE, 127, 8):
            return

        if key == curses.KEY_UP:
            selected = (selected - 1) % len(items)
            continue

        if key == curses.KEY_DOWN:
            selected = (selected + 1) % len(items)
            continue

        if key == curses.KEY_MOUSE:
            try:
                _, mouse_x, mouse_y, _, _ = curses.getmouse()
            except Exception:
                continue

            for row_y, index in item_rows:
                if mouse_y == row_y and mouse_x >= 4:
                    selected = index
                    key = 10
                    break

        if key in (10, 13, curses.KEY_ENTER):
            handler = items[selected]["handler"]
            result = handler(screen)

            if result == "EXIT_APP":
                raise ScreenExit()

            if result == "BACK":
                return


def main_menu_items():
    return [
        {
            "label": lambda: "Zatrzymaj usluge" if is_service_running() else "Uruchom usluge",
            "handler": toggle_service
        },
        {
            "label": "Systemd service",
            "handler": lambda screen: menu_loop(screen, "Systemd service", systemd_menu_items)
        },
        {
            "label": "Opcje bazy polaczen",
            "handler": lambda screen: menu_loop(screen, "Opcje bazy polaczen", cdr_db_menu_items)
        },
        {
            "label": "Opcje bazy userow GLPI",
            "handler": lambda screen: menu_loop(screen, "Opcje bazy userow GLPI", glpi_users_menu_items)
        },
        {
            "label": "Pokaz logi",
            "handler": show_logs
        },
        {
            "label": "Wyjscie",
            "handler": lambda screen: "EXIT_APP"
        }
    ]


def systemd_menu_items():
    return [
        {
            "label": "Restart service",
            "handler": lambda screen: run_systemctl_and_show(screen, "restart")
        },
        {
            "label": lambda: "Stop service" if is_service_running() else "Start service",
            "handler": toggle_service
        },
        {
            "label": "Status service",
            "handler": show_systemd_status
        },
        {
            "label": "Wstecz",
            "handler": lambda screen: "BACK"
        }
    ]


def cdr_db_menu_items():
    return [
        {
            "label": "Wyzeruj baze polaczen",
            "handler": reset_cdr_db
        },
        {
            "label": "Pokaz rekordy",
            "handler": show_it_cdr_records
        },
        {
            "label": lambda: f"Ile rekordow max: {core.get_cdr_max_records_label()}",
            "handler": set_cdr_limit
        },
        {
            "label": "Statystyki bazy",
            "handler": show_db_stats
        },
        {
            "label": "Wstecz",
            "handler": lambda screen: "BACK"
        }
    ]


def glpi_users_menu_items():
    return [
        {
            "label": "Updatuj baze userow z GLPI",
            "handler": refresh_glpi_users
        },
        {
            "label": "Wyzeruj baze userow GLPI",
            "handler": reset_glpi_users_db
        },
        {
            "label": "Pokaz rekordy",
            "handler": show_glpi_users_records
        },
        {
            "label": "Statystyki bazy",
            "handler": show_db_stats
        },
        {
            "label": "Wstecz",
            "handler": lambda screen: "BACK"
        }
    ]


def curses_main(screen):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    screen.keypad(True)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_RED, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_REVERSE, curses.COLOR_BLACK, curses.COLOR_CYAN)

    core.init_db()

    try:
        menu_loop(screen, "Menu glowne", main_menu_items)
    except ScreenExit:
        return


def main():
    try:
        curses.wrapper(curses_main)
    finally:
        os.system("clear")
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
