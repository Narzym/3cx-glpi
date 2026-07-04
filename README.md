# 3cxToGLPI Admin Panel

Pakiet zawiera panel terminalowy do zarzadzania istniejaca usluga `cdr_to_glpi.service` oraz kod listenera 3CX CDR.

## Pliki

- `admin_panel.py` - terminalowy panel sterowania
- `cdr_to_glpi.py` - listener CDR + logika GLPI
- `config.py` - konfiguracja wyciagnieta z glownego skryptu
- `README.md` - ten plik

## Zmiany v5

- widok logow pokazuje caly plik logow, nie tylko ostatnie 300 linii
- widok logow startuje od dolu, czyli przy najnowszych wpisach
- dolna belka w widoku tekstowym/logow jest jedna linijka
- ostatnia linia logow nie jest juz zaslaniana przez menu
- w widoku tekstowym dzialaja tylko:
  - strzalka w gore
  - strzalka w dol
  - Enter jako powrot
- wyjscie z panelu czysci terminal
- tabela `glpi_users` ma pola:
  - `glpi_user_id`
  - `email`
  - `full_name`
  - `first_name`
  - `last_name`
- update userow GLPI loguje jeden przykladowy surowy payload z GLPI

## Uruchomienie

```bash
cd /opt/3cx-glpi
python3 admin_panel.py
```

## Po podmianie kodu

```bash
sudo systemctl restart cdr_to_glpi.service
```


## v6 - debug payload CDR

Parser CDR obsluguje teraz format 8-polowy:

```text
callid, duration, from-no, from-dn, from-dispname, to-no, final-dn, final-dispname
```

Dla kazdego odebranego CDR do logow trafia tag `[CDR DEBUG]` z:

- surowa linia CDR,
- liczba pol,
- lista pol 1-8 z nazwami i wartosciami,
- mapowanie uzywane przez aplikacje.

Debug jest domyslnie wlaczony. Zeby go wylaczyc po testach, dodaj do `.env`:

```env
CDR_DEBUG_FULL_PAYLOAD=0
```
