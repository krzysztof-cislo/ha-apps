# Changelog

## 1.2.1
- Czyste zatrzymywanie aplikacji (obsługa SIGTERM) – koniec z błędem „exit code 137” przy restarcie i aktualizacji.
- Obrazy budowane przez GitHub Actions i pobierane z ghcr.io.

## 1.2.0
- IMAP IDLE: reakcja na nowy mail w ~1–2 s zamiast sprawdzania co 30 s.
- Opcja `after_confirm` (`trash` / `read` / `keep`) – domyślnie mail trafia do kosza (zastępuje `mark_as_read`).
- Weryfikacja certyfikatu TLS serwera IMAP.

## 1.1.1
- Zamykanie banera cookies przed kliknięciem.

## 1.1.0
- Awaryjne logowanie do Netflixa, zapamiętywanie sesji, wykrywanie sukcesu po `upl-success`.

## 1.0.0
- Pierwsza wersja.
