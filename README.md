# 3cxToGLPI Admin Panel v10

Zmiany v10:

- Usuniete stare kolumny z kodu bazy `glpi_users`:
  - `normalized_full_name`
  - `updated_at`
- Aktualna tabela `glpi_users` ma tylko:
  - `glpi_user_id`
  - `email`
  - `full_name`
  - `first_name`
  - `last_name`
- Dopasowanie usera dalej dziala, ale normalizacja nazwy odbywa sie w kodzie w czasie porownania, a nie przez osobna kolumne w bazie.
- Przy update cache userow GLPI tabela jest dropowana i tworzona od nowa.

Po podmianie plikow na serwerze odpal w panelu:

```text
Opcje bazy userow GLPI -> Wyzeruj baze userow GLPI
Opcje bazy userow GLPI -> Updatuj baze userow z GLPI
```

Aktualny format CDR:

```text
callid, duration, from-no, from-dispname, to-no, final-dn, final-dispname
```
