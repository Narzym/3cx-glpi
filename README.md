# 3cxToGLPI Admin Panel v1.14

Terminalowy panel zarzadzania integracja 3CX -> GLPI.

## Zmiany v1.14

- kolor przewodni ustawiony jako light magenta,
- zaznaczona opcja ma czarny tekst na light magenta,
- w widoku rekordow bazy polaczen usunieto linie `Query`,
- `callid` w widoku rekordow bazy polaczen jest skrocony do ostatnich 4 znakow.

## Uruchomienie

```bash
cd /opt/3cx-glpi
python3 admin_panel.py
```

## Po aktualizacji przez git

```bash
cd /opt/3cx-glpi
git pull
python3 admin_panel.py
```

Restart service nie jest wymagany dla samych zmian w panelu.
