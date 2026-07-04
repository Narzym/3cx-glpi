# 3cxToGLPI Admin Panel

Ten pakiet NIE tworzy nowego systemd service. Panel podpina sie pod Twoj istniejacy service.

Pliki:

- `config.py` - centralny config aplikacji.
- `cdr_to_glpi.py` - listener CDR + logika GLPI.
- `admin_panel.py` - terminalowy panel administracyjny.
- `requirements.txt` - zaleznosci Pythona.

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
- opcje bazy userow GLPI: updatuj cache z GLPI, wyzeruj cache, pokaz rekordy `SELECT * FROM glpi_users`, statystyki

Limit bazy CDR jest w tabeli `settings` jako `cdr_max_records`. Wartosc: liczba >= 10 albo `unlimited`.

Do cache userow GLPI panel uzywa tabeli `glpi_users`. Mozesz ja odswiezyc, podejrzec albo wyzerowac z poziomu panelu.
