# 3cxToGLPI v2.0

Usługa odbiera rekordy rozmów (CDR) z centrali 3CX i tworzy na ich podstawie tickety w GLPI.
Ticket dostaje realizatora wg tego, kto w GLPI (Zasoby → Telefony → pole „Użytkownik”) ma w tej chwili dany aparat IT.
Do obsługi służy panel TUI (`admin_panel.py`).

## Struktura projektu

| Plik | Za co odpowiada |
|---|---|
| `cdr_to_glpi.py` | **Usługa** (uruchamia ją systemd): odbiór CDR z 3CX, pętla główna |
| `tickets.py` | Logika ticketów: komu przypisać, jaka lokalizacja, co wpisać w treści |
| `glpi_cache.py` | Lokalny cache użytkowników, lokalizacji i telefonów z GLPI + dopasowania |
| `glpi_client.py` | Cała komunikacja HTTP z GLPI (sesja API, paginacja, retry, tworzenie ticketu) |
| `db.py` | Baza SQLite: połączenie (WAL), tabele, ustawienia, rekordy CDR, statystyki |
| `logger.py` | Logi do konsoli i pliku z automatyczną rotacją |
| `utils.py` | Małe funkcje pomocnicze bez efektów ubocznych |
| `config.py` | Wszystkie ustawienia (wartości z `.env`, mapowania kolejek) |
| `admin_panel.py` | Panel administratora (curses) |
| `tests/` | Testy automatyczne z atrapą GLPI |

Zasada zależności (w jedną stronę, bez kółek):
`config` ← `utils`, `logger` ← `db` ← `glpi_cache` ← `tickets` ← `cdr_to_glpi`; `glpi_client` jest używany przez `glpi_cache` i `cdr_to_glpi`.

## Testy

```bash
python3 -m unittest discover -s tests -b -v
```

Testy używają katalogu tymczasowego i atrapy GLPI na `localhost`. Nie dotykają produkcyjnej bazy ani prawdziwego GLPI,
więc można je bezpiecznie uruchomić także na serwerze. **Uruchamiaj je przed każdym wdrożeniem.**

## Aktualizacja na serwerze

```bash
cd /opt/3cx-glpi
git pull origin main
python3 -m unittest discover -s tests -b          # powinno zakończyć się "OK"
/usr/bin/python3 -m py_compile config.py utils.py logger.py db.py glpi_client.py glpi_cache.py tickets.py cdr_to_glpi.py admin_panel.py
sudo systemctl restart cdr_to_glpi.service
tail -n 30 logs/cdr_to_glpi.log
```

W logu powinno pojawić się `[GLPI PHONE] Zaktualizowano cache telefonow GLPI. Zapisano: N`.

Uwagi:
- Od v2.0 baza działa w trybie **WAL**: obok `cdrdatabase.db` pojawią się pliki `cdrdatabase.db-wal` i `-shm`. To normalne.
  Przy kopii zapasowej kopiuj wszystkie trzy albo użyj `sqlite3 cdrdatabase.db ".backup kopia.db"`.
- Usługa i panel powinny działać jako ten sam użytkownik systemowy (pliki WAL muszą być zapisywalne dla obu).
- Awaryjny powrót do klasycznego trybu bazy: `DB_JOURNAL_MODE=DELETE` w `.env` i restart usługi.

## Diagnostyka z linii poleceń

```bash
cd /opt/3cx-glpi
# kto ma teraz telefon 519 wg GLPI (odświeża cache, jeśli jest starszy niż TTL)
/usr/bin/python3 -c 'import db, glpi_cache; db.init_db(); print(glpi_cache.get_glpi_phone_by_dn("519")); print(glpi_cache.resolve_phone_display_label("519"))'
```

## Historia zmian

### v2.0
- Podział monolitu `cdr_to_glpi.py` (~2150 linii) na moduły (patrz tabela wyżej). Zachowanie aplikacji bez zmian — wynik porównano
  na identycznych danych ze starą wersją (te same tickety, przypisania i lokalizacje).
- Przypisywanie ticketów wg użytkownika telefonu w GLPI (Zasoby → Telefony), cache z konfigurowalnym TTL (`GLPI_PHONES_CACHE_TTL`).
- Stabilność: błąd przy jednej linii CDR nie zrywa już połączenia z 3CX; poprawne dekodowanie polskich liter na granicy porcji danych;
  SQLite w trybie WAL z timeoutem; `db_session()` gwarantuje commit/rollback/zamknięcie połączenia; atomowa podmiana cache
  (pusta odpowiedź GLPI nie kasuje cache); ponawianie żądań GET do GLPI (tworzenie ticketu nie jest ponawiane po błędzie serwera,
  żeby nie dublować ticketów); rotacja logów; `systemctl --no-ask-password` w panelu.
- Wydajność panelu: cache statusu usługi i statystyk (2 s), odczyt logów od końca pliku, live feed nie czyta dysku, gdy log się nie zmienił.
- Poprawka dopasowania nazwisk: litera „ł” (np. „Łukasz” = „Lukasz”).
- Testy automatyczne (`tests/`), `.env.example`.

### v1.19
- Grupa IT_Helpdesk id=27 dodawana w payloadzie ticketa jako grupa zgłaszająca (`_groups_id_requester`) i przypisana (`_groups_id_assign`).
