import curses
import os
import sqlite3
import subprocess
import textwrap
import io
import contextlib

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


def clear_terminal_after_exit():
    os.system("clear")


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


def draw_header(screen, subtitle=None, footer=True):
    if not draw_box(screen):
        return

    height, width = screen.getmaxyx()

    title = f"3cxToGLPI Admin Panel v{getattr(cfg, 'APP_VERSION', '1.11')}"
    safe_addstr(screen, 1, max(2, (width - len(title)) // 2), title, curses.A_BOLD | curses.color_pair(COLOR_CYAN))

    status, attr = get_service_status_label()
    safe_addstr(screen, 3, 3, "Status: ", curses.A_BOLD)
    safe_addstr(screen, 3, 11, status, attr)

    stats = core.get_db_stats()
    stats_text = f"CDR: {stats['cdr_count']} | CDR limit: {stats['cdr_max_records']} | GLPI users cache: {stats['glpi_users_count']} | GLPI locations cache: {stats['glpi_locations_count']} | Service: {cfg.SERVICE_NAME}"
    safe_addstr(screen, 4, 3, stats_text, curses.color_pair(COLOR_YELLOW))

    if subtitle:
        safe_addstr(screen, 6, 3, subtitle, curses.A_BOLD)

    if footer:
        safe_addstr(screen, height - 2, 3, "↑/↓ wybierz | Enter klik | q/Backspace wstecz", curses.A_DIM)


def wrap_lines_for_screen(raw_lines, content_width):
    wrapped_lines = []

    for line in raw_lines:
        wrapped = textwrap.wrap(
            line,
            max(20, content_width),
            replace_whitespace=False,
            drop_whitespace=False
        ) or [""]
        wrapped_lines.extend(wrapped)

    if not wrapped_lines:
        wrapped_lines = [""]

    return wrapped_lines


def drain_input_queue(screen):
    """
    Czysci zakolejkowane klawisze.
    Jak przytrzymasz strzalke, terminal potrafi wyslac setki KEY_UP/KEY_DOWN.
    Bez tego panel potem jeszcze minute mieli stare klawisze jak chomik w kolowrotku.
    """

    old_nodelay = False

    try:
        screen.nodelay(True)
        old_nodelay = True

        while screen.getch() != -1:
            pass

    except Exception:
        try:
            curses.flushinp()
        except Exception:
            pass

    finally:
        if old_nodelay:
            screen.nodelay(False)


def show_scrollable_text(screen, title, text, start_bottom=False, scroll_step=5):
    raw_lines = str(text).splitlines()
    scroll = 0
    wrapped_lines = None
    last_width = None
    initialized_bottom = False

    while True:
        screen.clear()
        draw_header(screen, title, footer=False)

        height, width = screen.getmaxyx()
        content_width = max(20, width - 8)
        visible_height = max(1, height - 12)

        if wrapped_lines is None or last_width != content_width:
            wrapped_lines = wrap_lines_for_screen(raw_lines, content_width)
            last_width = content_width

            if start_bottom and not initialized_bottom:
                scroll = max(0, len(wrapped_lines) - visible_height)
                initialized_bottom = True

        max_scroll = max(0, len(wrapped_lines) - visible_height)
        scroll = min(max(scroll, 0), max_scroll)
        visible = wrapped_lines[scroll:scroll + visible_height]

        y = 8
        for line in visible:
            if y >= height - 4:
                break
            safe_addstr(screen, y, 4, line)
            y += 1

        footer = f"Linie {scroll + 1}-{min(scroll + visible_height, len(wrapped_lines))}/{len(wrapped_lines)} | ↑/↓ scroll | Enter wstecz"
        safe_addstr(screen, height - 2, 3, footer, curses.A_DIM)
        screen.refresh()

        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return

        if key in (10, 13, curses.KEY_ENTER):
            drain_input_queue(screen)
            return

        if key == curses.KEY_UP:
            scroll = max(0, scroll - scroll_step)
            drain_input_queue(screen)

        elif key == curses.KEY_DOWN:
            scroll = min(max_scroll, scroll + scroll_step)
            drain_input_queue(screen)

def show_text(screen, title, text):
    show_scrollable_text(screen, title, text, start_bottom=False)


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
    if confirm(screen, "Na pewno wyzerowac baze polaczen it_cdr? To tez przebuduje tabele na nowy schemat CDR."):
        run_action_and_show(screen, "Wyzeruj baze polaczen", core.reset_it_cdr)


def reset_glpi_users_db(screen):
    if confirm(screen, "Na pewno wyzerowac cache userow GLPI? To tez przebuduje tabele."):
        run_action_and_show(screen, "Wyzeruj baze userow GLPI", core.reset_glpi_users)


def reset_glpi_locations_db(screen):
    if confirm(screen, "Na pewno wyzerowac cache lokalizacji GLPI? To tez przebuduje tabele."):
        run_action_and_show(screen, "Wyzeruj baze lokalizacji GLPI", core.reset_glpi_locations)


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


def refresh_glpi_locations(screen):
    run_action_and_show(screen, "Update bazy lokalizacji GLPI", core.refresh_glpi_locations_cache)


def show_db_stats(screen):
    stats = core.get_db_stats()
    text = (
        f"Baza SQLite: {cfg.DB_PATH}\n"
        f"Rekordy CDR: {stats['cdr_count']}\n"
        f"Limit CDR: {stats['cdr_max_records']}\n"
        f"Userzy GLPI w cache: {stats['glpi_users_count']}\n"
        f"Lokalizacje GLPI w cache: {stats['glpi_locations_count']}\n"
    )
    show_text(screen, "Statystyki bazy", text)


def show_systemd_status(screen):
    show_text(screen, "Status systemd", get_systemd_status_text())


def format_sql_table(headers, rows):
    headers = [str(header) for header in headers]
    string_rows = [["" if value is None else str(value) for value in row] for row in rows]
    widths = []

    for index, header in enumerate(headers):
        values = [row[index] for row in string_rows]
        width = max([len(header)] + [len(value) for value in values])
        widths.append(min(width, 50))

    def cut(value, width):
        value = str(value)
        if len(value) <= width:
            return value
        return value[:width - 3] + "..."

    lines = []
    lines.append(" | ".join(cut(header, widths[i]).ljust(widths[i]) for i, header in enumerate(headers)))
    lines.append("-+-".join("-" * width for width in widths))

    for row in string_rows:
        lines.append(" | ".join(cut(value, widths[i]).ljust(widths[i]) for i, value in enumerate(row)))

    return "\n".join(lines)


def query_sqlite(query):
    try:
        conn = sqlite3.connect(cfg.DB_PATH)
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        headers = [description[0] for description in cursor.description]
        conn.close()

        return f"Query: {query}\nLiczba rekordow: {len(rows)}\n\n" + format_sql_table(headers, rows)

    except sqlite3.Error as e:
        try:
            conn.close()
        except Exception:
            pass

        return f"Query: {query}\nBLAD SQLite: {type(e).__name__}: {e}\n\nNajpewniej tabela ma stary schemat albo jeszcze nie istnieje. Wyzeruj/odswiez odpowiednia baze z menu."


def show_cdr_records(screen):
    show_text(screen, "Rekordy bazy polaczen", query_sqlite("SELECT * FROM it_cdr"))


def show_glpi_user_records(screen):
    query = "SELECT glpi_user_id, email, full_name, first_name, last_name FROM glpi_users ORDER BY glpi_user_id"
    show_text(screen, "Rekordy bazy userow GLPI", query_sqlite(query))


def show_glpi_location_records(screen):
    query = "SELECT glpi_location_id, room_number, name, full_path FROM glpi_locations ORDER BY room_number, glpi_location_id"
    show_text(screen, "Rekordy bazy lokalizacji GLPI", query_sqlite(query))


def show_logs(screen):
    path = cfg.LOG_PATH

    if not os.path.exists(path):
        text = f"Plik logow nie istnieje: {path}"
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            content = file.read()

        line_count = len(content.splitlines())
        text = f"Plik logow: {path}\nPokazuje caly plik: {line_count} linii.\n\n" + content

    show_scrollable_text(screen, "Logi", text, start_bottom=True)


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
            "label": "Opcje bazy lokalizacji GLPI",
            "handler": lambda screen: menu_loop(screen, "Opcje bazy lokalizacji GLPI", glpi_locations_menu_items)
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
            "label": "Pokaz rekordy",
            "handler": show_cdr_records
        },
        {
            "label": "Wyzeruj baze polaczen",
            "handler": reset_cdr_db
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
            "label": "Pokaz rekordy",
            "handler": show_glpi_user_records
        },
        {
            "label": "Updatuj baze userow z GLPI",
            "handler": refresh_glpi_users
        },
        {
            "label": "Wyzeruj baze userow GLPI",
            "handler": reset_glpi_users_db
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


def glpi_locations_menu_items():
    return [
        {
            "label": "Pokaz rekordy",
            "handler": show_glpi_location_records
        },
        {
            "label": "Updatuj baze lokalizacji z GLPI",
            "handler": refresh_glpi_locations
        },
        {
            "label": "Wyzeruj baze lokalizacji GLPI",
            "handler": reset_glpi_locations_db
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
    except KeyboardInterrupt:
        pass
    finally:
        clear_terminal_after_exit()


if __name__ == "__main__":
    main()
