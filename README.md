# 3cxToGLPI Admin Panel v1.18

Wersja v1.18:

- przywrocono proste przypisywanie ticketow do grupy IT_Helpdesk przez pole `_groups_id_assign`
- usunieto dodatkowe dopisywanie relacji `Group_Ticket`
- usunieto opcje/zmiany zwiazane z `GLPI_VERIFY_SSL`
- brak dodatkowych komunikatow `GLPI GROUP`

Nowe tickety nadal ida do grupy ustawionej w `.env`/configu:

```env
GLPI_HELPDESK_GROUP_ID=27
```

Po wrzuceniu zmian na serwer:

```bash
cd /opt/3cx-glpi
git pull
sudo systemctl restart cdr_to_glpi.service
python3 admin_panel.py
```
