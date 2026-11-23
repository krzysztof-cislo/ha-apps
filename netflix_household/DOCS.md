# Netflix Household Auto-Confirm

Aplikacja trzyma stałe połączenie ze skrzynką pocztową i czeka na nowe maile
(IMAP IDLE – serwer sam powiadamia o nowej wiadomości, zwykle w 1–2 s). Kiedy przyjdzie
mail od Netflixa z linkiem „Zaktualizuj gospodarstwo domowe”, otwiera go w
przeglądarce Chromium działającej bez okna i klika **Potwierdź aktualizację**.

## Instalacja

1. **Settings → Apps → Install app → ⋮ → Repositories** → dodaj
   `https://github.com/krzysztof-cislo/ha-apps`.
2. W sklepie pojawi się *Netflix Household Auto-Confirm* → **Install**
   (gotowy obraz z ghcr.io – bez budowania na urządzeniu).
3. Uzupełnij konfigurację i uruchom aplikację.

Nowe wersje pojawiają się w HA jako zwykła aktualizacja (**Update**).

## Gmail

IMAP wymaga **hasła aplikacji**: <https://myaccount.google.com/apppasswords>
(konto musi mieć włączoną weryfikację dwuetapową). Wpisz 16 znaków bez spacji
jako `imap_password`.

## Logowanie do Netflixa

Link z maila zawiera jednorazowy token, więc zwykle **nie trzeba być zalogowanym** –
dodatek otwiera link i klika przycisk. Jeśli jednak Netflix pokaże stronę logowania:

- z wypełnionymi `netflix_email` i `netflix_password` dodatek się zaloguje
  i ponownie otworzy link,
- bez nich zgłosi status `login_failed` i powiadomienie w HA.

Po udanym potwierdzeniu lub logowaniu ciasteczka sesji są zapisywane w
`/data/netflix_session.json` (prywatny katalog dodatku), więc kolejne razy
przeglądarka jest już „znanym urządzeniem”. Żeby wymusić nową sesję, odinstaluj
dodatek i zainstaluj go ponownie.

Netflix może przy logowaniu z nowego urządzenia poprosić o kod lub captchę –
tego dodatek nie obsłuży; zobaczysz to na zrzucie ekranu.

## Pierwsze uruchomienie – test

1. Włącz `dry_run: true`.
2. Wywołaj na telewizorze „Zaktualizuj gospodarstwo domowe” → Netflix wyśle maila.
3. W logu dodatku powinno być `[dry_run] Tryb testowy: znalazłem przycisk …`, a zrzut
   ekranu strony pojawi się w `/share/netflix_household/last.png` (Samba: `\\homeassistant\share`).
4. Jeśli wszystko gra, wyłącz `dry_run` i powtórz – tym razem dodatek kliknie przycisk.

## Co się dzieje z mailem

Opcja `after_confirm` (tylko po udanym potwierdzeniu, status `confirmed`):

- `trash` (domyślnie) – mail trafia do kosza. Folder kosza jest wykrywany automatycznie
  (w Gmailu `[Gmail]/Kosz` lub `[Gmail]/Trash`); Gmail sam opróżnia kosz po 30 dniach.
- `read` – mail zostaje w skrzynce, oznaczony jako przeczytany.
- `keep` – nic nie jest zmieniane.

Przy każdym innym wyniku (`uncertain`, `expired`, `failed`…) mail zostaje nietknięty,
żeby można było kliknąć link ręcznie.

## Połączenie z pocztą

- Serwer z obsługą IMAP IDLE (Gmail, większość dostawców): reakcja w ~1–2 s,
  połączenie odnawiane co 5 min, ponowne logowanie tylko po zerwaniu połączenia.
- Serwer bez IDLE: skrzynka sprawdzana co `poll_interval` sekund.
- Certyfikat TLS serwera jest weryfikowany.

## Wynik w Home Assistant

Każda próba wysyła zdarzenie `netflix_household` z polami `status`
(`confirmed`, `uncertain`, `expired`, `failed`, `login_failed`, `dry_run`), `message`, `subject`.
Przykładowa automatyzacja:

```yaml
triggers:
  - trigger: event
    event_type: netflix_household
actions:
  - action: notify.mobile_app_telefon
    data:
      message: "{{ trigger.event.data.message }}"
```

Zamiast automatyzacji możesz po prostu ustawić `notify_service`. Przy błędzie
dodatek tworzy też powiadomienie w panelu HA.

## Gdy coś nie działa

- `failed` / brak przycisku: Netflix mógł zmienić stronę. Zobacz zrzut w
  `/share/netflix_household/` i włącz `debug: true` – w logu będzie tekst strony.
- `expired`: link żyje ok. 15 minut. Dodatek musi działać, gdy przychodzi mail.
- Mail nie jest wykrywany: sprawdź `imap_folder` (np. filtr Gmaila przenosi
  maile do etykiety) i `sender_filter`.
