# 3cxToGLPI Admin Panel v1.11


Zmiany v12:

- poprawione obslugiwanie bledu `ERROR_RIGHT_MISSING` przy pobieraniu lokalizacji GLPI,
- gdy API user nie ma prawa do `Location`, kod pokazuje jasny komunikat zamiast enigmatycznego 403,
- ticket z telefonu sali dalej dziala nawet bez lokalizacji: requester pusty, lokalizacja pusta, ticket idzie dalej,
- przy cache miss lokalizacji kod domyslnie nie probuje pobierac lokalizacji online przy kazdym polaczeniu, zeby nie spamowac logow 403 i nie spowalniac ticketow,
- lokalizacje najlepiej odswiezac z panelu po nadaniu uprawnien w GLPI,
- `glpi_users` zostaje bez starych kolumn `normalized_full_name` i `updated_at`.

## Wazne

Blad:

```text
["ERROR_RIGHT_MISSING", "Brak uprawnien do wykonania tej akcji."]
```

oznacza, ze token/user API nie ma prawa odczytu do lokalizacji GLPI. Endpoint `/Location` jest OK, ale profil usera API musi miec prawo odczytu lokalizacji/list rozwijanych.

Po poprawie uprawnien w GLPI wejdz w panel:

```text
Opcje bazy lokalizacji GLPI -> Updatuj baze lokalizacji z GLPI
```

## Opcjonalnie

Domyslnie aplikacja nie robi online fetch lokalizacji przy kazdym cache miss. Jezeli jednak chcesz to wlaczyc:

```env
GLPI_AUTO_FETCH_LOCATIONS_ON_MISS=1
```

## v1.11

- Tytul ticketu uzywa `from-dispname`, a nie `from-no`.
- Lokalizacja GLPI odpala sie tylko, gdy `from-dispname` zawiera keyword `sala`.
- Panel pokazuje wersje aplikacji w naglowku.
- `it_cdr` dostaje brakujace kolumny automatycznie, zeby update nie wywalal sie na starej tabeli.
