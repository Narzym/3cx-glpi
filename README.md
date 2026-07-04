# 3cxToGLPI Admin Panel

Ten pakiet NIE tworzy nowego systemd service. Panel podpina sie pod Twoj istniejacy service.

Pliki:

- `config.py` - centralny config aplikacji.
- `cdr_to_glpi.py` - listener CDR + logika GLPI.
- `admin_panel.py` - terminalowy panel administracyjny.

Nowy format CDR:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```

Wewnetrzne mapowanie w kodzie:

- `from-no` -> `caller_number`
- `from-dispname` -> `caller_name`
- `to-no` -> `dialed_number`
- `final-dn` -> `final_number`
- `final-dispname` -> `final_name`

Panel uruchamiasz z katalogu projektu:

```bash
sudo python3 admin_panel.py
```

`sudo` odpala panel z uprawnieniami administratora, zeby mogl robic `systemctl start/stop/restart`.
`python3` odpala skrypt Pythonem 3.
`admin_panel.py` to plik panelu.

Panel uzywa istniejacej uslugi z `.env`:

```env
SERVICE_NAME=cdr_to_glpi.service
```

Jesli Twoj service ma inna nazwe, wpisz dokladnie ta nazwe. Sprawdzisz ja tak:

```bash
systemctl list-units --type=service | grep cdr
```

`systemctl` zarzadza uslugami systemd.
`list-units` pokazuje aktywne unity/uslugi.
`--type=service` filtruje tylko uslugi.
`grep cdr` pokazuje tylko linie zawierajace `cdr`.

Najwazniejsze zmienne `.env`:

```env
GLPI_API_URL=https://twoj-glpi.example.pl/apirest.php
GLPI_APP_TOKEN=...
GLPI_USER_TOKEN=...
LOG_PATH=logs/cdr_to_glpi.log
DB_PATH=cdrdatabase.db
SERVICE_NAME=cdr_to_glpi.service
```

Opcje w panelu:

- menu glowne: uruchom/zatrzymaj usluge, systemd service, opcje bazy polaczen, opcje bazy userow GLPI, pokaz logi
- opcje bazy polaczen: wyzeruj baze, pokaz rekordy `SELECT * FROM it_cdr`, ustaw limit, statystyki
- opcje bazy userow GLPI: updatuj cache z GLPI, wyzeruj cache, pokaz rekordy `SELECT glpi_user_id, email, full_name, first_name, last_name FROM glpi_users ORDER BY glpi_user_id`, statystyki

Limit bazy CDR jest w tabeli `settings` jako `cdr_max_records`. Wartosc: liczba >= 10 albo `unlimited`.

Tabela cache userow GLPI ma teraz tylko:

```text
glpi_user_id, email, full_name, first_name, last_name
```

Email jest brany najpierw z klasycznych pol mailowych GLPI (`email`, `email_default`, `default_email`). Jezeli sa puste, a `name` wyglada jak adres mailowy, wtedy email jest brany z `name`.

Przy kazdym update cache userow aplikacja zapisuje do logow jeden przykladowy surowy payload usera z GLPI, zeby bylo widac, jakie pola faktycznie oddaje API.

Przy starcie aplikacji stara wersja tabeli `glpi_users` zostanie usunieta i utworzona od nowa bez migracji.
