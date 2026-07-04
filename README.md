# 3cxToGLPI Admin Panel v11

Zmiany w v11:

- requester jest dopasowywany z `from-dispname` do cache `glpi_users`
- jesli requester nie znajdzie sie w cache ani w GLPI, ticket dalej tworzy sie normalnie z pustym requesterem
- jezeli `from-dispname` wyglada jak sala, np. `sala 443`, requester zostaje pusty, a kod probuje ustawic `locations_id` w tickecie
- dodana baza/cache lokalizacji GLPI: `glpi_locations`
- dodane submenu panelu: `Opcje bazy lokalizacji GLPI`
- przy update lokalizacji loguje jeden przykladowy surowy payload z GLPI
- usuniete stare kolumny userow z kodu: `normalized_full_name`, `updated_at`
- aktualna baza userow GLPI: `glpi_user_id`, `email`, `full_name`, `first_name`, `last_name`
- aktualna baza lokalizacji GLPI: `glpi_location_id`, `room_number`, `name`, `full_path`

Aktualna kolejnosc CDR:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```

Po podmianie plikow zalecane akcje w panelu:

```text
Opcje bazy userow GLPI -> Wyzeruj baze userow GLPI
Opcje bazy userow GLPI -> Updatuj baze userow z GLPI
Opcje bazy lokalizacji GLPI -> Wyzeruj baze lokalizacji GLPI
Opcje bazy lokalizacji GLPI -> Updatuj baze lokalizacji z GLPI
Opcje bazy polaczen -> Wyzeruj baze polaczen
```

Zmienne `.env`, opcjonalne:

```env
GLPI_LOCATION_PAGE_SIZE=200
GLPI_SET_ROOM_AS_LOCATION=1
```
