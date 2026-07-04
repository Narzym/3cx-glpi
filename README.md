# 3cxToGLPI Admin Panel

Terminalowy panel administracyjny dla projektu 3CX -> GLPI.

## Aktualny format CDR

Ustaw w 3CX pola CDR w tej kolejnosci:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```

## Najwazniejsze pliki

- `config.py` - konfiguracja projektu
- `cdr_to_glpi.py` - listener CDR + integracja GLPI
- `admin_panel.py` - panel terminalowy

## Panel

Uruchom:

```bash
python3 admin_panel.py
```

Panel korzysta z istniejacej uslugi systemd podanej w `.env`:

```env
SERVICE_NAME=cdr_to_glpi.service
```

## Logi

Widok logow pokazuje caly plik z `LOG_PATH` i startuje od dolu, czyli od najnowszych wpisow.

Sterowanie w widoku logow i rekordow:

```text
strzalka gora/dol - scroll
Enter - powrot
```

Scroll logow przewija kilka linii naraz i czysci zakolejkowane klawisze, zeby przytrzymana strzalka nie mielila jeszcze dlugo po puszczeniu.

## Debug CDR

Debug pelnego CDR payloadu sterowany jest przez `.env`:

```env
CDR_DEBUG_FULL_PAYLOAD=1
```

Wylaczenie:

```env
CDR_DEBUG_FULL_PAYLOAD=0
```
