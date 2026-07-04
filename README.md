# 3cxToGLPI Admin Panel

Pakiet zawiera:

- `cdr_to_glpi.py` - listener CDR + tworzenie ticketow GLPI
- `config.py` - konfiguracja wyciagnieta ze skryptu
- `admin_panel.py` - terminalowy panel sterowania

## Aktualny format CDR

W 3CX ustaw pola w tej kolejnosci:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```

`from-dn` jest dalej usuniete, ale `final-dn` zostalo przywrocone.

W bazie `it_cdr` kolumny CDR sa nazwane tak samo jak pola z 3CX:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```


`final-dn` znowu jest uzywane do rozpoznawania nieodebranych i przypisywania ownera. Jesli 3CX poda dziwny rekord bez `final-dn`, kod awaryjnie probuje uzyc `to-no`.

Technicznie SQLite wymaga cudzyslowow dla kolumn z myslnikiem, np.:

```sql
SELECT "from-no", "from-dispname", "to-no", "final-dn" FROM it_cdr;
```

## Numery IT

Do `IT_NUMBERS` dodane sa automatycznie wersje z prefixem `225435`, np.:

```text
517, 225435517, 699, 225435699
```

Prefix ustawisz w `.env`:

```env
IT_NUMBER_PREFIX=225435
```

## Debug CDR

Domyslnie wlaczony jest pelny debug CDR:

```env
CDR_DEBUG_FULL_PAYLOAD=1
```

W logach zobaczysz pelny payload po parsowaniu, z numerami pol i mapowaniem. Po testach mozna wylaczyc:

```env
CDR_DEBUG_FULL_PAYLOAD=0
```

## Baza GLPI users

Tabela `glpi_users` ma kolumny:

```text
glpi_user_id, email, full_name, first_name, last_name
```

Przy update bazy userow w logach wypisywany jest jeden surowy przykladowy payload usera z GLPI.

## Po wdrozeniu

Po podmianie plikow warto przebudowac tabele w panelu:

```text
Opcje bazy polaczen -> Wyzeruj baze polaczen
Opcje bazy userow GLPI -> Wyzeruj baze userow GLPI
Opcje bazy userow GLPI -> Updatuj baze userow z GLPI
```

Potem restart uslugi:

```bash
sudo systemctl restart cdr_to_glpi.service
```

## Panel

```bash
python3 admin_panel.py
```

Menu logow pokazuje caly plik i startuje od najnowszych wpisow.
