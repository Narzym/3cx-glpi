# 3cxToGLPI Admin Panel v1.12

Zmiany w tej wersji:

- Panel pokazuje wersje `v1.12`.
- Menu glowne ma live feed z podstawowymi logami.
- Live feed odswieza sie na zywo razem z licznikiem CDR w naglowku.
- `Pokaz logi` zmienione na `Rozwin logi`.
- Dodane submenu `Opcje logow`.
- W `Opcje logow` mozna przelaczac:
  - payload z CDR,
  - payload z userow GLPI,
  - payload z lokalizacji GLPI.
- Payloady verbose sa domyslnie wylaczone i sterowane z SQLite/settings.
- Sukcesy tworzenia ticketow sa kolorowane na zielono.
- Errory sa kolorowane na czerwono.
- Widok rekordow bazy polaczen jest skrocony:
  - callid pokazuje ostatnie 8 znakow,
  - dlugie pola sa ucinane,
  - tabela nie zawija juz kazdego wiersza w makaron.

Po wrzuceniu na Linuxa:

```bash
cd /opt/3cx-glpi
git pull
sudo systemctl restart cdr_to_glpi.service
python3 admin_panel.py
```

Zmiana `Opcje logow` dziala przez baze SQLite, wiec listener moze odczytac przelaczniki bez kolejnego restartu po samym klikaniu w panelu.


## v1.13

- Kolor przewodni panelu zmieniony z cyan na magenta.
- Live feed pokazuje najnowsze logi od dolu, bez scrollowania.
- Live feed czyta wiekszy ogon logow, zeby verbose payloady nie zaslanialy podstawowych eventow.
- Usunieto opcje „Statystyki bazy” z submenu baz.
