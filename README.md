# 3cxToGLPI Admin Panel v1.19

Zmiana v1.19:
- grupa IT_Helpdesk id=27 jest dodawana w payloadzie ticketa jako grupa zgłaszająca (`_groups_id_requester`) i grupa przypisana (`_groups_id_assign`).
- bez dodatkowych requestów Group_Ticket, bez certyfikatowych kombinacji, bez nowych logów specjalnych.

Najważniejsze pliki:
- `cdr_to_glpi.py`
- `admin_panel.py`
- `config.py`

Po aktualizacji zrestartuj usługę:

```bash
sudo systemctl restart cdr_to_glpi.service
```
