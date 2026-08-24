# 3cxToGLPI Admin Panel v1.16

Terminalowy panel zarzadzania integracja 3CX -> GLPI.

## Zmiany v1.16

- calkowicie usunieto automatyczne ustawianie zglaszajacego/requestera na podstawie osoby dzwoniacej,
- tickety nie dostaja juz pol `_users_id_requester` ani `users_id_recipient`,
- lokalizacja po `sala XXX` dalej dziala normalnie,
- owner/przypisany technik dalej jest ustawiany po `final-dn`, `to-no` albo awaryjnie po `final-dispname`,
- widok bazy polaczen nie pokazuje juz kolumny requestera.

## Uruchomienie

```bash
cd /opt/3cx-glpi
python3 admin_panel.py
```

## Po aktualizacji przez git

```bash
cd /opt/3cx-glpi
git pull
sudo systemctl restart cdr_to_glpi.service
python3 admin_panel.py
```

Restart service jest wymagany, bo zmienil sie listener `cdr_to_glpi.py`.
