"""
Testy automatyczne aplikacji 3cxToGLPI.

Uruchomienie (z katalogu głównego projektu):
    python3 -m unittest discover -s tests -b -v

Testy NIE dotykają produkcyjnej bazy ani prawdziwego GLPI:
  - baza i logi lądują w katalogu tymczasowym,
  - GLPI jest zastąpione atrapą (tests/fake_glpi.py) działającą na localhost.
"""

import os
import random
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fake_glpi import FakeGLPI  # noqa: E402

# Atrapa GLPI musi ruszyć PRZED importem config.py, bo config czyta adres z zmiennych środowiskowych.
FAKE = FakeGLPI()
FAKE.start()

_TMP_ROOT = tempfile.mkdtemp(prefix="3cx_tests_")

os.environ.update({
    "GLPI_API_URL": FAKE.base_url,
    "GLPI_APP_TOKEN": "app-token",
    "GLPI_USER_TOKEN": "user-token",
    "DB_PATH": os.path.join(_TMP_ROOT, "import.db"),
    "LOG_PATH": os.path.join(_TMP_ROOT, "import.log"),
    "IT_NUMBER_PREFIX": "225435",
    "GLPI_HTTP_RETRIES": "2",
    "GLPI_HTTP_BACKOFF": "0.01",
    "GLPI_HTTP_CONNECT_TIMEOUT": "2",
    "GLPI_HTTP_TIMEOUT": "5",
})

import admin_panel  # noqa: E402
import cdr_to_glpi  # noqa: E402
import config as cfg  # noqa: E402
import db  # noqa: E402
import glpi_cache  # noqa: E402
import glpi_client  # noqa: E402
import logger  # noqa: E402
import tickets  # noqa: E402
import utils  # noqa: E402


def phone(phone_id, name, other="", serial="", user=0, **extra):
    data = {"id": phone_id, "name": name, "otherserial": other, "serial": serial,
            "users_id": user, "is_deleted": 0, "is_template": 0}
    data.update(extra)
    return data


def cdr_line(callid, duration, from_no, from_name, to_no, final_dn, final_name=""):
    return f"Call {callid},{duration},{from_no},{from_name},{to_no},{final_dn},{final_name}"


class AppTestCase(unittest.TestCase):
    """Wspólny przygotowawca: świeża baza, świeża atrapa GLPI, czysty stan modułów."""

    def setUp(self):
        FAKE.reset()
        self.tmp = tempfile.mkdtemp(dir=_TMP_ROOT)

        for name, value in {
            "DB_PATH": os.path.join(self.tmp, "t.db"),
            "LOG_PATH": os.path.join(self.tmp, "logs", "t.log"),
            "GLPI_API_URL": FAKE.base_url,
            "GLPI_PHONES_CACHE_TTL": 30,
            "LOG_MAX_BYTES": 5 * 1024 * 1024,
            "LOG_BACKUP_COUNT": 5,
        }.items():
            patcher = mock.patch.object(cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Kontrolowany fallback z configu (zamiast prawdziwych ID techników).
        for name in ("OWNER_BY_FINAL_DN", "OWNER_BY_TO_NO"):
            patcher = mock.patch.object(cfg, name, {"517": None, "699": None, "529": 17})
            patcher.start()
            self.addCleanup(patcher.stop)

        glpi_client.reset_http_session()
        glpi_cache.GLPI_PHONES_RETRY_AFTER = 0.0
        glpi_cache.GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME = False
        db.init_db()

    def read_log(self):
        try:
            with open(cfg.LOG_PATH, encoding="utf-8") as file:
                return file.read()
        except FileNotFoundError:
            return ""

    def cdr_rows(self):
        with db.db_session() as conn:
            return conn.execute(
                "SELECT callid, zrobiony, glpi_ticket_id, glpi_error FROM it_cdr ORDER BY id"
            ).fetchall()


# =====================================================================
# UTILS
# =====================================================================

class TestUtils(unittest.TestCase):
    def test_polish_l_with_stroke_is_normalized(self):
        self.assertEqual(utils.normalize_name("Łukasz Wójcik"), "lukasz wojcik")
        self.assertTrue(utils.names_match("Łukasz Wójcik", "Lukasz Wojcik"))

    def test_names_match_is_order_independent(self):
        self.assertTrue(utils.names_match("Jan Kowalski", "kowalski jan"))
        self.assertFalse(utils.names_match("Jan Kowalski", "Jan Nowak"))


# =====================================================================
# BAZA DANYCH
# =====================================================================

class TestDatabase(AppTestCase):
    def test_wal_mode_is_enabled(self):
        conn = sqlite3.connect(cfg.DB_PATH)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_session_commits_and_closes_connection(self):
        with db.db_session() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # polaczenie jest zamkniete

        self.assertEqual(db.get_setting("k"), "v")

    def test_session_rolls_back_on_error_and_closes(self):
        with self.assertRaises(RuntimeError):
            with db.db_session() as conn:
                conn.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
                raise RuntimeError("boom")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

        self.assertIsNone(db.get_setting("x"))

    def test_cdr_limit_keeps_only_newest_records(self):
        db.set_cdr_max_records(10)

        for number in range(15):
            db.save_it_cdr({"callid": f"c{number}", "duration": "1", "from_no": "1", "from_dispname": "a",
                            "to_no": "517", "final_dn": "519", "final_dispname": "x"})

        self.assertEqual([row[0] for row in self.cdr_rows()], [f"c{n}" for n in range(5, 15)])

    def test_stats_contain_all_counters(self):
        stats = db.get_db_stats()
        self.assertEqual(set(stats), {"cdr_count", "glpi_users_count", "glpi_locations_count",
                                      "glpi_phones_count", "cdr_max_records"})

    def test_log_option_toggle(self):
        key = "log_cdr_payload"
        before = db.is_verbose_log_enabled(key)
        db.toggle_log_option(key)
        self.assertNotEqual(before, db.is_verbose_log_enabled(key))


# =====================================================================
# ROTACJA LOGÓW
# =====================================================================

class TestLogRotation(AppTestCase):
    def test_log_file_is_rotated_and_bounded(self):
        with mock.patch.object(cfg, "LOG_MAX_BYTES", 1000), mock.patch.object(cfg, "LOG_BACKUP_COUNT", 3):
            for number in range(300):
                logger.write_log_to_file("x" * 50 + f" {number}")

        log_dir = os.path.dirname(cfg.LOG_PATH)
        files = sorted(os.listdir(log_dir))
        self.assertEqual(files, ["t.log", "t.log.1", "t.log.2", "t.log.3"])

        total = sum(os.path.getsize(os.path.join(log_dir, name)) for name in files)
        self.assertLess(total, 4 * 1100)
        self.assertIn(" 299", self.read_log())  # najnowszy wpis jest w biezacym pliku

    def test_rotation_can_be_disabled(self):
        with mock.patch.object(cfg, "LOG_MAX_BYTES", 0):
            for number in range(100):
                logger.write_log_to_file("x" * 50)

        self.assertEqual(os.listdir(os.path.dirname(cfg.LOG_PATH)), ["t.log"])


# =====================================================================
# KLIENT HTTP (retry, paginacja, błędy)
# =====================================================================

class TestGlpiClient(AppTestCase):
    def test_get_is_retried_after_temporary_5xx(self):
        FAKE.data["Phone"] = [{"id": 1}]
        FAKE.fail_first[("GET", "/Phone")] = [503, 502]

        self.assertEqual(glpi_client.fetch_glpi_collection("Phone", 200), [{"id": 1}])
        self.assertEqual(FAKE.count("GET", "/Phone"), 3)

    def test_get_gives_up_after_configured_retries_and_closes_session(self):
        FAKE.fail_first[("GET", "/Phone")] = [503] * 10

        with self.assertRaises(glpi_client.GLPIRequestError) as ctx:
            glpi_client.fetch_glpi_collection("Phone", 200)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(FAKE.count("GET", "/Phone"), 3)  # 1 proba + 2 ponowienia
        self.assertEqual(FAKE.count("GET", "/killSession"), FAKE.count("GET", "/initSession"))

    def test_post_is_never_retried_after_5xx(self):
        """Ponowienie POST po 5xx moglo by utworzyc DUPLIKAT ticketu."""
        FAKE.fail_first[("POST", "/Ticket")] = [503]

        with self.assertRaises(glpi_client.GLPIRequestError):
            glpi_client.create_glpi_ticket({"input": {"name": "x"}})

        self.assertEqual(FAKE.count("POST", "/Ticket"), 1)
        self.assertEqual(FAKE.tickets, [])

    def test_create_ticket_ok(self):
        status, ticket_id = glpi_client.create_glpi_ticket({"input": {"name": "x"}})
        self.assertEqual((status, ticket_id), (201, 101))
        self.assertEqual(FAKE.tickets, [{"input": {"name": "x"}}])
        self.assertEqual(FAKE.count("GET", "/killSession"), 1)

    def test_pagination_collects_all_pages(self):
        FAKE.data["User"] = [{"id": number} for number in range(5)]

        users = glpi_client.fetch_glpi_collection("User", 2)

        self.assertEqual([user["id"] for user in users], [0, 1, 2, 3, 4])
        self.assertEqual(FAKE.count("GET", "/User"), 3)

    def test_missing_right_raises_dedicated_error_without_retry(self):
        FAKE.always[("GET", "/Location")] = (403, ["ERROR_RIGHT_MISSING", "Brak uprawnien"])

        with self.assertRaises(glpi_client.GLPIRightMissingError) as ctx:
            glpi_client.fetch_glpi_collection("Location", 200)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(FAKE.count("GET", "/Location"), 1)

    def test_unreachable_server_gives_clear_error(self):
        with mock.patch.object(cfg, "GLPI_API_URL", "http://127.0.0.1:1/api.php/v1"):
            with self.assertRaises(glpi_client.GLPIRequestError) as ctx:
                glpi_client.create_glpi_ticket({"input": {}})

        self.assertIn("Blad polaczenia", str(ctx.exception))


# =====================================================================
# CACHE: UŻYTKOWNICY I LOKALIZACJE
# =====================================================================

class TestUsersAndLocationsCache(AppTestCase):
    def test_users_refresh_and_empty_response_keeps_old_cache(self):
        FAKE.data["User"] = [{"id": 5, "name": "jan@firma.pl", "firstname": "Jan", "realname": "Kowalski"}]

        self.assertEqual(glpi_cache.refresh_glpi_users_cache(), 1)
        self.assertEqual(glpi_cache.find_glpi_user_in_cache("Jan Kowalski"), 5)

        FAKE.data["User"] = []
        self.assertEqual(glpi_cache.refresh_glpi_users_cache(), 0)
        self.assertEqual(glpi_cache.find_glpi_user_in_cache("Jan Kowalski"), 5)  # cache przetrwal

    def test_locations_refresh_and_room_lookup(self):
        FAKE.data["Location"] = [
            {"id": 163, "name": "443 (Sala wykladowa)", "completename": "ALA > Budynek > 443 (Sala wykladowa)"},
            {"id": 164, "name": "444 (Sala wykladowa)", "completename": "ALA > Budynek > 444 (Sala wykladowa)"},
        ]

        self.assertEqual(glpi_cache.refresh_glpi_locations_cache(), 2)
        self.assertEqual(glpi_cache.resolve_location_id_by_room("443"), 163)
        self.assertIsNone(glpi_cache.resolve_location_id_by_room("999"))

    def test_location_403_sets_flag_and_skips_further_online_fetches(self):
        FAKE.always[("GET", "/Location")] = (403, ["ERROR_RIGHT_MISSING", "x"])

        with self.assertRaises(glpi_client.GLPIRightMissingError):
            glpi_cache.fetch_glpi_locations()

        self.assertTrue(glpi_cache.GLPI_LOCATION_FETCH_FORBIDDEN_RUNTIME)

        before = FAKE.count("GET", "/Location")
        self.assertEqual(glpi_cache.fetch_and_cache_locations_silent(), 0)
        self.assertEqual(FAKE.count("GET", "/Location"), before)


# =====================================================================
# CACHE: TELEFONY
# =====================================================================

class TestPhonesCache(AppTestCase):
    def setUp(self):
        super().setUp()
        FAKE.data["Phone"] = [
            phone(2, "519 SAMSUNG", other="519", user=928),
            phone(3, "NOKIA", other="520", user=0),
            phone(4, "521 SONY ERICSSON", other="225435521", user=1043),
            phone(5, "XIAOMI", serial="526", user=32),
            phone(6, "528 IPHONE", other="INV-0042", user=1040),
            phone(7, "Telefon recepcji", other="9999", user=5),
            phone(8, "Szablon", other="529", user=17, is_template=1),
            phone(9, "Usuniety", other="524", user=17, is_deleted=1),
        ]

    def cached(self):
        with db.db_session() as conn:
            return {row[1]: row for row in conn.execute("SELECT * FROM glpi_phones")}

    def test_only_it_numbers_are_cached_and_templates_are_skipped(self):
        self.assertEqual(glpi_cache.refresh_glpi_phones_cache(), 5)
        cached = self.cached()
        self.assertEqual(set(cached), {"519", "520", "521", "526", "528"})
        self.assertIsNone(cached["520"][3])                 # users_id=0 -> brak technika
        self.assertEqual(cached["521"][2], "SONY ERICSSON")  # prefiks i numer obcięte z nazwy
        self.assertEqual(cached["528"][3], 1040)             # numer z nazwy, gdy inwentarz obcy

    def test_duplicate_number_prefers_newer_phone_and_warns(self):
        FAKE.data["Phone"].append(phone(10, "Cudzy 519", other="519", user=77))
        glpi_cache.refresh_glpi_phones_cache()
        self.assertEqual(self.cached()["519"][3], 77)
        self.assertIn("kilku telefonach", self.read_log())

    def test_owner_resolution_prefers_glpi_over_config(self):
        glpi_cache.refresh_glpi_phones_cache()
        cdr = lambda final, to="517": {"final_dn": final, "to_no": to, "final_dispname": ""}  # noqa: E731

        self.assertEqual(tickets.resolve_owner_id(cdr("519")), 928)
        self.assertEqual(tickets.resolve_owner_id(cdr("225435521")), 1043)
        self.assertIsNone(tickets.resolve_owner_id(cdr("520")))                  # w GLPI, ale nikt go nie ma
        self.assertEqual(tickets.resolve_owner_id(cdr("", "526")), 32)           # final_dn puste -> to_no
        self.assertIsNone(tickets.resolve_owner_id(cdr("517")))                  # kolejka: config
        self.assertEqual(tickets.resolve_owner_id(cdr("529")), 17)               # poza GLPI: config

    def test_labels(self):
        glpi_cache.refresh_glpi_phones_cache()
        self.assertEqual(glpi_cache.resolve_phone_display_label("519"), "519 (SAMSUNG)")
        self.assertEqual(glpi_cache.resolve_phone_display_label("225435521"), "225435521 (SONY ERICSSON)")
        self.assertEqual(glpi_cache.resolve_phone_display_label("517"), "517 (Kolejka IT)")
        self.assertEqual(glpi_cache.resolve_phone_display_label(""), "")

    def test_fresh_cache_does_not_call_glpi(self):
        glpi_cache.refresh_glpi_phones_cache()
        before = FAKE.count("GET", "/Phone")

        for _ in range(5):
            glpi_cache.get_glpi_phone_by_dn("519")

        self.assertEqual(FAKE.count("GET", "/Phone"), before)

    def test_stale_cache_is_refreshed_and_sees_new_technician(self):
        glpi_cache.refresh_glpi_phones_cache()
        db.set_setting("glpi_phones_last_sync", str(time.time() - 31))
        FAKE.data["Phone"][0]["users_id"] = 959

        self.assertEqual(glpi_cache.get_glpi_phone_by_dn("519")["glpi_user_id"], 959)

    def test_api_failure_keeps_old_cache_and_backs_off(self):
        glpi_cache.refresh_glpi_phones_cache()
        db.set_setting("glpi_phones_last_sync", str(time.time() - 31))
        stale_sync = db.get_setting("glpi_phones_last_sync")

        FAKE.always[("GET", "/Phone")] = (500, {"error": "down"})
        before = FAKE.count("GET", "/Phone")

        for _ in range(5):
            self.assertEqual(glpi_cache.get_glpi_phone_by_dn("519")["glpi_user_id"], 928)  # stary cache

        self.assertEqual(FAKE.count("GET", "/Phone") - before, 3)  # jedna proba (1 + 2 retry), potem backoff
        self.assertEqual(db.get_setting("glpi_phones_last_sync"), stale_sync)  # last_sync nie sfalszowany

    def test_missing_right_gives_readable_error_and_keeps_cache(self):
        glpi_cache.refresh_glpi_phones_cache()
        FAKE.always[("GET", "/Phone")] = (403, ["ERROR_RIGHT_MISSING", "Brak"])

        with self.assertRaises(glpi_client.GLPIRightMissingError) as ctx:
            glpi_cache.refresh_glpi_phones_cache()

        self.assertIn("Profile", str(ctx.exception))
        self.assertEqual(glpi_cache.get_glpi_phone_by_dn("519")["glpi_user_id"], 928)

    def test_empty_glpi_response_does_not_wipe_cache(self):
        glpi_cache.refresh_glpi_phones_cache()
        FAKE.data["Phone"] = []
        self.assertEqual(glpi_cache.refresh_glpi_phones_cache(), 0)
        self.assertEqual(db.get_db_stats()["glpi_phones_count"], 5)

    def test_ttl_zero_always_asks_glpi(self):
        glpi_cache.refresh_glpi_phones_cache()
        before = FAKE.count("GET", "/Phone")

        with mock.patch.object(cfg, "GLPI_PHONES_CACHE_TTL", 0):
            for _ in range(3):
                glpi_cache.get_glpi_phone_by_dn("519")

        self.assertEqual(FAKE.count("GET", "/Phone") - before, 3)

    def test_reset_clears_table_and_sync_marker(self):
        glpi_cache.refresh_glpi_phones_cache()
        db.reset_glpi_phones()
        self.assertEqual(db.get_db_stats()["glpi_phones_count"], 0)
        self.assertIsNone(db.get_setting("glpi_phones_last_sync"))


# =====================================================================
# CAŁY PRZEPŁYW: LINIA CDR -> TICKET
# =====================================================================

class TestEndToEnd(AppTestCase):
    def setUp(self):
        super().setUp()
        FAKE.data["Phone"] = [phone(2, "519 SAMSUNG", other="519", user=928), phone(3, "NOKIA", other="520")]
        FAKE.data["Location"] = [{"id": 163, "name": "443 (Sala wykladowa)",
                                  "completename": "ALA > 443 (Sala wykladowa)"}]
        FAKE.data["User"] = [{"id": 5, "name": "jan@firma.pl", "firstname": "Jan", "realname": "Kowalski"}]
        glpi_cache.refresh_glpi_locations_cache()

    def test_answered_call_is_assigned_to_phone_holder_and_closed(self):
        cdr_to_glpi.handle_cdr_line(cdr_line("c1", "00:01:10", "48123", "Jan Kowalski", "517", "519", "SAMSUNG"))

        self.assertEqual(len(FAKE.tickets), 1)
        payload = FAKE.tickets[0]["input"]
        self.assertEqual(payload["_users_id_assign"], 928)
        self.assertEqual(payload["status"], cfg.GLPI_STATUS_CLOSED)
        self.assertIn("Final: 519 (SAMSUNG)", payload["content"])
        self.assertEqual(payload["_groups_id_assign"], cfg.GLPI_HELPDESK_GROUP_ID)
        self.assertEqual(self.cdr_rows(), [("c1", 1, "101", None)])

    def test_call_to_unassigned_phone_stays_new_without_technician(self):
        cdr_to_glpi.handle_cdr_line(cdr_line("c2", "00:00:30", "48123", "Ktos", "517", "520", "NOKIA"))

        payload = FAKE.tickets[0]["input"]
        self.assertNotIn("_users_id_assign", payload)
        self.assertEqual(payload["status"], cfg.GLPI_STATUS_NEW)

    def test_room_caller_gets_location(self):
        cdr_to_glpi.handle_cdr_line(cdr_line("c3", "00:00:30", "101", "Sala 443", "699", "519", "SAMSUNG"))
        self.assertEqual(FAKE.tickets[0]["input"]["locations_id"], 163)

    def test_owner_fallback_by_final_dispname_for_phone_outside_glpi(self):
        cdr_to_glpi.handle_cdr_line(cdr_line("c4", "00:00:30", "48123", "Ktos", "517", "526", "Jan Kowalski"))
        self.assertEqual(FAKE.tickets[0]["input"]["_users_id_assign"], 5)

    def test_call_outside_it_numbers_is_ignored(self):
        cdr_to_glpi.handle_cdr_line(cdr_line("c5", "00:00:30", "48123", "Ktos", "200", "201", "Ktos Inny"))
        self.assertEqual(FAKE.tickets, [])
        self.assertEqual(self.cdr_rows(), [])

    def test_glpi_error_is_recorded_not_raised(self):
        FAKE.always[("POST", "/Ticket")] = (500, {"error": "boom"})

        cdr_to_glpi.handle_cdr_line(cdr_line("c6", "00:01:00", "48123", "Ktos", "517", "519", "SAMSUNG"))

        rows = self.cdr_rows()
        self.assertEqual((rows[0][0], rows[0][1]), ("c6", 0))
        self.assertIn("HTTP 500", rows[0][3])
        self.assertEqual(FAKE.count("POST", "/Ticket"), 1)  # bez duplikatow

    def test_missed_call_in_working_hours_creates_unassigned_ticket(self):
        with mock.patch.object(cdr_to_glpi, "is_missed_call_ticket_time", return_value=True):
            cdr_to_glpi.handle_cdr_line(cdr_line("c7", "00:00:00", "48123", "Ktos", "517", "517", ""))

        payload = FAKE.tickets[0]["input"]
        self.assertTrue(payload["name"].startswith("Nieodebrane"))
        self.assertNotIn("_users_id_assign", payload)
        self.assertIn("517 (Kolejka IT)", payload["content"])

    def test_missed_call_outside_working_hours_is_skipped(self):
        with mock.patch.object(cdr_to_glpi, "is_missed_call_ticket_time", return_value=False):
            cdr_to_glpi.handle_cdr_line(cdr_line("c8", "00:00:00", "48123", "Ktos", "517", "517", ""))

        self.assertEqual(FAKE.tickets, [])

    def test_unexpected_exception_is_contained_and_logged(self):
        with mock.patch.object(cdr_to_glpi, "save_it_cdr", side_effect=sqlite3.OperationalError("database is locked")):
            cdr_to_glpi.safe_handle_cdr_line(cdr_line("c9", "00:01:00", "1", "Ktos", "517", "519", "SAMSUNG"))

        log = self.read_log()
        self.assertIn("Nieoczekiwany blad przy obsludze CDR", log)
        self.assertIn("database is locked", log)
        self.assertIn("c9", log)


# =====================================================================
# ODBIÓR Z GNIAZDA 3CX
# =====================================================================

class TestListener(AppTestCase):
    def test_polish_letters_split_between_chunks_survive(self):
        line = "Call 1,00:00:10,1,Łódź Żółć,517,519,x\n".encode("utf-8")
        cut = line.index("ó".encode("utf-8")) + 1  # tniemy w srodku dwubajtowej litery

        lines1, rest = cdr_to_glpi.split_complete_lines(b"", line[:cut])
        self.assertEqual(lines1, [])

        lines2, rest = cdr_to_glpi.split_complete_lines(rest, line[cut:])
        self.assertEqual(lines2, ["Call 1,00:00:10,1,Łódź Żółć,517,519,x"])
        self.assertEqual(rest, b"")

    def test_partial_line_is_kept_until_complete(self):
        lines, rest = cdr_to_glpi.split_complete_lines(b"", b"aaa\nbbb\nccc")
        self.assertEqual((lines, rest), (["aaa", "bbb"], b"ccc"))

    def test_runaway_buffer_is_dropped(self):
        with mock.patch.object(cdr_to_glpi, "MAX_CDR_BUFFER_BYTES", 10):
            lines, rest = cdr_to_glpi.split_complete_lines(b"", b"x" * 50)

        self.assertEqual((lines, rest), ([], b""))

    def test_connection_survives_a_line_that_crashes_the_handler(self):
        """
        Regresja: kiedys wyjatek przy jednej linii zrywal polaczenie z 3CX i gubil reszte bufora.
        Teraz pierwsza linia wywala handler, a druga MUSI dalej utworzyc ticket - na tym samym polaczeniu.
        """

        FAKE.data["Phone"] = [phone(2, "519 SAMSUNG", other="519", user=928)]
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        connections = []

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                connections.append(conn)
                conn.sendall(
                    (cdr_line("bad", "00:01:00", "1", "Ktos", "517", "519", "x") + "\n"
                     + cdr_line("good", "00:01:00", "1", "Ktos", "517", "519", "x") + "\n").encode("utf-8")
                )

        threading.Thread(target=serve, daemon=True).start()

        real_save = db.save_it_cdr
        calls = {"n": 0}

        def flaky_save(cdr):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_save(cdr)

        with mock.patch.object(cfg, "HOST", "127.0.0.1"), mock.patch.object(cfg, "PORT", server.getsockname()[1]), \
                mock.patch.object(cfg, "RECONNECT_DELAY", 3600), \
                mock.patch.object(cdr_to_glpi, "save_it_cdr", flaky_save):
            threading.Thread(target=cdr_to_glpi.connect_to_3cx, daemon=True).start()

            deadline = time.time() + 8
            while time.time() < deadline and not FAKE.tickets:
                time.sleep(0.05)

            self.assertEqual(len(FAKE.tickets), 1, "druga linia nie zostala obsluzona")
            self.assertEqual(len(connections), 1, "polaczenie z 3CX zostalo zerwane i nawiazane od nowa")
            self.assertEqual([row[0] for row in self.cdr_rows()], ["good"])

        server.close()
        for conn in connections:
            conn.close()


# =====================================================================
# PANEL ADMINA (część bez curses)
# =====================================================================

class TestAdminPanelHelpers(AppTestCase):
    def setUp(self):
        super().setUp()
        admin_panel.invalidate_cache()
        admin_panel._LIVE_FEED_CACHE.update({"signature": None, "lines": []})

    def write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_read_last_lines_matches_naive_implementation(self):
        rng = random.Random(7)
        alphabet = "abcdefghij ĄąĆćĘęŁłŃńÓóŚśŹźŻż0123456789"

        for trial in range(60):
            lines = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(rng.randint(0, 60))]
            text = "\n".join(lines)
            if rng.random() < 0.7 and lines:
                text += "\n"

            path = self.write(f"f{trial}.log", text.encode("utf-8"))

            with open(path, encoding="utf-8", newline="") as file:
                expected_all = file.readlines()

            for max_lines in (1, 2, 5, 50, 1000):
                for block_size in (1, 3, 7, 64, 65536):
                    got = admin_panel.read_last_lines(path, max_lines=max_lines, block_size=block_size)
                    self.assertEqual(got, expected_all[-max_lines:],
                                     f"trial={trial} max={max_lines} block={block_size}")

    def test_read_last_lines_missing_and_empty_file(self):
        self.assertEqual(admin_panel.read_last_lines(os.path.join(self.tmp, "nie-ma.log")), [])
        self.assertEqual(admin_panel.read_last_lines(self.write("empty.log", b"")), [])

    def test_live_feed_does_not_reread_unchanged_log(self):
        path = self.write("live.log", b"[GLPI SUCCESS] a\n[SPOZA IT] b\n")

        with mock.patch.object(cfg, "LOG_PATH", path), \
                mock.patch.object(admin_panel, "read_last_lines", wraps=admin_panel.read_last_lines) as spy:
            first = admin_panel.get_basic_live_feed_lines(50)
            second = admin_panel.get_basic_live_feed_lines(50)
            self.assertEqual(first, second)
            self.assertEqual(spy.call_count, 1)

            with open(path, "ab") as file:
                file.write(b"[ERROR] c\n")

            third = admin_panel.get_basic_live_feed_lines(50)
            self.assertEqual(spy.call_count, 2)
            self.assertEqual(third[-1], "[ERROR] c")

    def test_cached_call_and_invalidate(self):
        counter = {"n": 0}

        def compute():
            counter["n"] += 1
            return counter["n"]

        self.assertEqual(admin_panel.cached_call("k", 60, compute), 1)
        self.assertEqual(admin_panel.cached_call("k", 60, compute), 1)
        admin_panel.invalidate_cache()
        self.assertEqual(admin_panel.cached_call("k", 60, compute), 2)

    def test_systemctl_never_waits_for_a_password(self):
        fake_result = SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(admin_panel.subprocess, "run", return_value=fake_result) as run:
            admin_panel.run_systemctl("restart")

        args, kwargs = run.call_args
        self.assertEqual(args[0], ["systemctl", "--no-ask-password", "restart", cfg.SERVICE_NAME])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_service_status_is_cached_between_redraws(self):
        fake_result = SimpleNamespace(returncode=0)

        with mock.patch.object(admin_panel.subprocess, "run", return_value=fake_result) as run:
            self.assertTrue(admin_panel.is_service_running())
            self.assertTrue(admin_panel.is_service_running())
            self.assertEqual(run.call_count, 1)

    def test_phone_assignments_view_joins_user_names(self):
        FAKE.data["Phone"] = [phone(2, "519 SAMSUNG", other="519", user=928)]
        FAKE.data["User"] = [{"id": 928, "name": "m@x.pl", "firstname": "Michal", "realname": "Nowak"}]
        glpi_cache.refresh_glpi_users_cache()
        glpi_cache.refresh_glpi_phones_cache()

        query = ("SELECT p.dn, p.phone_name, COALESCE(u.full_name, '') AS user_name FROM glpi_phones p "
                 "LEFT JOIN glpi_users u ON u.glpi_user_id = p.glpi_user_id")
        text = admin_panel.query_sqlite(query, show_query=False)

        self.assertIn("519", text)
        self.assertIn("Michal Nowak", text)

    def test_show_logs_reads_only_the_tail_of_a_big_log(self):
        content = "".join(f"linia {number}\n" for number in range(10000)).encode("utf-8")
        path = self.write("big.log", content)

        with mock.patch.object(cfg, "LOG_PATH", path), \
                mock.patch.object(admin_panel, "show_scrollable_text") as viewer:
            admin_panel.show_logs(screen=None)

        shown = viewer.call_args.args[2]
        self.assertIn("limit 3000", shown)
        self.assertIn("linia 9999", shown)          # najnowsze wpisy sa
        self.assertNotIn("linia 6999\n", shown)     # starsze niz limit - nie
        self.assertEqual(shown.count("linia "), 3000)

    def test_query_sqlite_reports_errors_instead_of_crashing(self):
        self.assertIn("BLAD SQLite", admin_panel.query_sqlite("SELECT * FROM nie_ma_takiej_tabeli"))


if __name__ == "__main__":
    unittest.main()
