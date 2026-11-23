# Aplikacje Home Assistant

Repozytorium aplikacji (dawniej „dodatków”) dla Home Assistant OS.

[![Dodaj repozytorium do Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fkrzysztof-cislo%2Fha-apps)

Ręcznie: **Settings → Apps → Install app → ⋮ → Repositories** i wklej
`https://github.com/krzysztof-cislo/ha-apps`.

## Aplikacje

| Aplikacja | Opis |
|---|---|
| [Netflix Household Auto-Confirm](netflix_household/) | Automatycznie potwierdza maile Netflixa „Zaktualizuj gospodarstwo domowe”. |

Obrazy są budowane przez GitHub Actions (aarch64, amd64) i publikowane w
`ghcr.io/krzysztof-cislo/`, więc Home Assistant tylko je pobiera.
