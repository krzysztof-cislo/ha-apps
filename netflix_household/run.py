#!/usr/bin/env python3
"""Netflix Household Auto-Confirm.

Trzyma połączenie IMAP i czeka na nowe maile (IMAP IDLE). Gdy przyjdzie świeży mail od
Netflixa z linkiem „Zaktualizuj gospodarstwo domowe”, otwiera link w headless
Chromium i klika przycisk potwierdzenia. Wynik trafia do Home Assistanta jako
zdarzenie `netflix_household` (+ opcjonalnie powiadomienie).
"""

from __future__ import annotations

import email
import html
import imaplib
import json
import logging
import os
import re
import signal
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")
STATE_FILE = Path("/data/processed.json")
SESSION_FILE = Path("/data/netflix_session.json")
SHOT_DIR = Path("/share/netflix_household")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
STATE_LIMIT = 500
MAX_ATTEMPTS = 3
IDLE_RENEW = 5 * 60  # odnawiaj IDLE co 5 min (RFC 2177: < 29 min; chroni też przed zerwaniem przez NAT)
RECONNECT_MIN = 5
RECONNECT_MAX = 300
# imaplib domyślnie NIE weryfikuje certyfikatu serwera – wymuszamy weryfikację.
SSL_CONTEXT = ssl.create_default_context()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
LINK_RE = re.compile(r"https://www\.netflix\.com/[^\s\"'<>]+")
# Kolejność ma znaczenie: od najbardziej konkretnych.
BUTTON_SELECTORS = [
    'button[data-uia="set-primary-location-action"]',
    '[data-uia="set-primary-location-action"]',
]
BUTTON_TEXTS = [
    re.compile(r"potwierdź aktualizację|confirm update", re.I),
    re.compile(r"^\s*(potwierdź|confirm)\s*$", re.I),
]
SUCCESS_SELECTOR = '[data-uia="upl-success"]'
SUCCESS_RE = re.compile(
    r"zaktualizowan|updated|gotowe|all set|return to streaming|wróci do (oglądania|odtwarzania)", re.I
)
# Netflix używa banera OneTrust; potrafi zasłonić przycisk i przechwycić kliknięcie.
COOKIE_REJECT_SELECTORS = [
    "#onetrust-reject-all-handler",
    "#onetrust-banner-sdk button:text-matches('^(Reject|Odrzuć)', 'i')",
    "#onetrust-close-btn-container button",
]
EXPIRED_RE = re.compile(r"wygas|expired|no longer valid|nieważn|nie jest już aktywn", re.I)
LOGIN_ID_SELECTOR = 'input[name="userLoginId"]'
LOGIN_PASSWORD_SELECTOR = 'input[name="password"]'
LOGIN_SUBMIT_SELECTOR = 'button[data-uia="login-submit-button"]'
USE_PASSWORD_RE = re.compile(r"użyj hasła|use password|zaloguj się hasłem|sign in with password", re.I)

opts: dict = json.loads(OPTIONS_FILE.read_text())
logging.basicConfig(
    level=logging.DEBUG if opts.get("debug") else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("netflix_household")


# ---------------------------------------------------------------- state ----

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        return {"done": list(data.get("done", [])), "attempts": dict(data.get("attempts", {}))}
    except (FileNotFoundError, ValueError):
        return {"done": [], "attempts": {}}


def save_state(state: dict) -> None:
    state["done"] = state["done"][-STATE_LIMIT:]
    STATE_FILE.write_text(json.dumps(state))


def mark_done(state: dict, key: str) -> None:
    if key not in state["done"]:
        state["done"].append(key)
    state["attempts"].pop(key, None)
    save_state(state)


# ------------------------------------------------------- home assistant ----

def ha_post(path: str, payload: dict) -> None:
    if not SUPERVISOR_TOKEN:
        log.debug("Brak SUPERVISOR_TOKEN – pomijam %s", path)
        return
    req = urllib.request.Request(
        f"http://supervisor/core/api/{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        log.warning("Wywołanie HA %s nie powiodło się: %s", path, exc)


def report(status: str, message: str, subject: str) -> None:
    log.info("[%s] %s", status, message)
    ha_post("events/netflix_household", {"status": status, "message": message, "subject": subject})

    service = (opts.get("notify_service") or "").strip()
    if service:
        domain, _, name = service.partition(".")
        if not name:
            domain, name = "notify", service
        ha_post(f"services/{domain}/{name}", {"title": "Netflix", "message": message})

    if status not in ("confirmed", "dry_run"):
        ha_post(
            "services/persistent_notification/create",
            {
                "title": "Netflix – gospodarstwo domowe",
                "message": message,
                "notification_id": "netflix_household",
            },
        )


# ---------------------------------------------------------------- email ----

def decode_subject(msg: Message) -> str:
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:  # noqa: BLE001
        return msg.get("Subject", "")


def message_text(msg: Message) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() not in ("text/html", "text/plain"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def extract_links(msg: Message) -> list[str]:
    text = html.unescape(message_text(msg))
    patterns = [p.strip() for p in opts["link_pattern"].split(",") if p.strip()]
    links: list[str] = []
    for link in LINK_RE.findall(text):
        link = link.rstrip(").,;]")
        if any(p in link for p in patterns) and link not in links:
            links.append(link)
    return links


def message_date(msg: Message) -> datetime | None:
    try:
        date = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date


# -------------------------------------------------------------- browser ----

def screenshot(page, name: str) -> str:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        (SHOT_DIR / "last.png").write_bytes(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log.warning("Nie udało się zrobić zrzutu ekranu: %s", exc)
    return str(path)


def visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector).filter(visible=True).first
        return bool(loc.count()) and loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


def settle(page, timeout_ms: int = 20_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 – strony Netflixa potrafią nigdy nie być „idle”
        pass


def wait_for(page, check, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(500)


def find_button(page, timeout_s: float):
    found = []

    def check():
        candidates = [page.locator(sel).filter(visible=True).first for sel in BUTTON_SELECTORS]
        candidates += [page.get_by_role("button", name=rx).filter(visible=True).first for rx in BUTTON_TEXTS]
        for loc in candidates:
            if loc.count() and loc.is_visible():
                found.append(loc)
                return True
        return False

    return found[0] if wait_for(page, check, timeout_s) else None


def is_login_page(page) -> bool:
    return (
        visible(page, LOGIN_ID_SELECTOR)
        or visible(page, LOGIN_PASSWORD_SELECTOR)
        or "/login" in page.url
    )


def save_session(ctx) -> None:
    try:
        ctx.storage_state(path=str(SESSION_FILE))
        SESSION_FILE.chmod(0o600)
        log.debug("Zapisano sesję Netflix")
    except Exception as exc:  # noqa: BLE001
        log.warning("Nie udało się zapisać sesji: %s", exc)


def submit_login(page, field_selector: str) -> None:
    if visible(page, LOGIN_SUBMIT_SELECTOR):
        page.locator(LOGIN_SUBMIT_SELECTOR).filter(visible=True).first.click()
    else:
        page.locator(field_selector).filter(visible=True).first.press("Enter")
    settle(page, 15_000)


def login(page) -> tuple[bool, str]:
    user = (opts.get("netflix_email") or "").strip()
    password = opts.get("netflix_password") or ""
    if not user or not password:
        return False, "Netflix wymaga zalogowania, a w konfiguracji brak netflix_email / netflix_password."

    log.info("Strona wymaga logowania – loguję się jako %s", user)
    if not wait_for(page, lambda: visible(page, LOGIN_ID_SELECTOR), 10):
        return False, "Netflix przekierował na logowanie, ale nie znalazłem pola e-mail."
    page.locator(LOGIN_ID_SELECTOR).filter(visible=True).first.fill(user)

    if not visible(page, LOGIN_PASSWORD_SELECTOR):
        # Nowszy formularz bywa dwuetapowy: najpierw e-mail, potem wybór metody.
        use_password = page.get_by_role("button", name=USE_PASSWORD_RE).filter(visible=True).first
        if not (use_password.count() and use_password.is_visible()):
            submit_login(page, LOGIN_ID_SELECTOR)
            use_password = page.get_by_role("button", name=USE_PASSWORD_RE).filter(visible=True).first
        if use_password.count() and use_password.is_visible():
            use_password.click()
        wait_for(page, lambda: visible(page, LOGIN_PASSWORD_SELECTOR), 10)

    if not visible(page, LOGIN_PASSWORD_SELECTOR):
        return False, "Nie znalazłem pola hasła na stronie logowania Netflix."

    page.locator(LOGIN_PASSWORD_SELECTOR).filter(visible=True).first.fill(password)
    submit_login(page, LOGIN_PASSWORD_SELECTOR)
    page.wait_for_timeout(2000)
    if is_login_page(page):
        return False, "Logowanie do Netflixa nie powiodło się – sprawdź e-mail i hasło."
    log.info("Zalogowano do Netflixa")
    return True, ""


def dismiss_cookie_banner(page) -> None:
    for selector in COOKIE_REJECT_SELECTORS:
        if visible(page, selector):
            try:
                page.locator(selector).filter(visible=True).first.click(timeout=5_000)
                log.debug("Kliknięto %s na banerze cookies", selector)
                page.wait_for_timeout(1000)
                break
            except Exception:  # noqa: BLE001
                pass
    # Jeśli baner nadal jest (albo nie było przycisku) – usuń nakładkę ze strony.
    if visible(page, "#onetrust-banner-sdk"):
        page.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
        log.debug("Usunięto baner cookies ze strony")


def succeeded(page) -> bool:
    if page.locator(SUCCESS_SELECTOR).count():
        return True
    return find_button(page, 0) is None and bool(SUCCESS_RE.search(page.inner_text("body")))


def confirm(url: str) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            settle(page)

            if is_login_page(page):
                ok, message = login(page)
                if not ok:
                    shot = screenshot(page, "login")
                    return "login_failed", f"{message} (zrzut: {shot})"
                save_session(ctx)
                if find_button(page, 5) is None:
                    # Po logowaniu Netflix nie zawsze wraca na stronę z linku.
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    settle(page)

            button = find_button(page, timeout_s=20)
            if button is None:
                shot = screenshot(page, "no-button")
                body = page.inner_text("body")[:3000]
                log.debug("Treść strony:\n%s", body)
                if EXPIRED_RE.search(body):
                    return "expired", f"Link z maila już wygasł (zrzut: {shot})."
                return "failed", f"Nie znalazłem przycisku potwierdzenia na stronie (zrzut: {shot})."

            dismiss_cookie_banner(page)
            label = (button.inner_text() or "").strip()
            if opts.get("dry_run"):
                shot = screenshot(page, "dry-run")
                return "dry_run", f"Tryb testowy: znalazłem przycisk „{label}”, nie klikam (zrzut: {shot})."

            log.info("Klikam przycisk „%s”", label)
            button.click()
            settle(page)
            ok = wait_for(page, lambda: succeeded(page), 15)
            shot = screenshot(page, "after-click")

            if ok:
                save_session(ctx)
                return "confirmed", "Potwierdzono aktualizację gospodarstwa domowego Netflix."
            if find_button(page, 2) is None:
                return "uncertain", f"Kliknięto „{label}”, ale nie widzę komunikatu o sukcesie – sprawdź zrzut: {shot}"
            return "uncertain", f"Kliknięto „{label}”, ale przycisk nadal jest widoczny – sprawdź zrzut: {shot}"
        finally:
            browser.close()


# ----------------------------------------------------------------- main ----

def handle_message(msg: Message) -> tuple[str, str] | None:
    """Zwraca (status, komunikat) albo None, gdy mail nas nie dotyczy."""
    links = extract_links(msg)
    if not links:
        return None
    log.info("Mail „%s” – link: %s…", decode_subject(msg), links[0][:90])
    try:
        return confirm(links[0])
    except Exception as exc:  # noqa: BLE001
        log.exception("Błąd przeglądarki")
        return "failed", f"Błąd podczas otwierania linku: {exc}"


LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')


def quote_mailbox(name: str) -> str:
    return name if name.startswith('"') else f'"{name}"'


def connect() -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(
        opts["imap_host"], opts["imap_port"], ssl_context=SSL_CONTEXT, timeout=60
    )
    imap.login(opts["imap_user"], opts["imap_password"])
    typ, _ = imap.select(quote_mailbox(opts["imap_folder"]))
    if typ != "OK":
        raise RuntimeError(f"Nie mogę otworzyć folderu {opts['imap_folder']}")
    return imap


def find_trash(imap: imaplib.IMAP4_SSL) -> str | None:
    """Folder kosza po fladze \\Trash (RFC 6154) – działa niezależnie od języka Gmaila."""
    typ, data = imap.list()
    for raw in data if typ == "OK" else []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        match = LIST_RE.match(line)
        if match and "\\trash" in match["flags"].lower():
            return match["name"]
    return None


def after_confirm(imap: imaplib.IMAP4_SSL, uid: bytes, trash: str | None) -> None:
    action = opts.get("after_confirm", "trash")
    if action == "keep":
        return
    if action == "trash" and trash:
        if "MOVE" in imap.capabilities:
            typ, data = imap.uid("MOVE", uid, quote_mailbox(trash))
        else:
            typ, data = imap.uid("COPY", uid, quote_mailbox(trash))
            if typ == "OK":
                imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                imap.expunge()
        if typ == "OK":
            log.info("Przeniesiono mail do kosza (%s)", trash)
            return
        log.warning("Nie udało się przenieść maila do kosza: %s", data)
    elif action == "trash":
        log.warning("Nie znalazłem folderu kosza – oznaczam mail jako przeczytany")
    imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def process(imap: imaplib.IMAP4_SSL, state: dict, trash: str | None) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=opts["max_age_minutes"])
    # SINCE ma dokładność do dnia; dzień zapasu na strefy czasowe.
    since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")

    # Ponowny SELECT odświeża UIDVALIDITY i stan folderu.
    typ, _ = imap.select(quote_mailbox(opts["imap_folder"]))
    if typ != "OK":
        raise RuntimeError(f"Nie mogę otworzyć folderu {opts['imap_folder']}")
    uidvalidity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0].decode()

    typ, data = imap.uid("SEARCH", None, f'(FROM "{opts["sender_filter"]}" SINCE {since})')
    uids = data[0].split() if typ == "OK" and data and data[0] else []
    log.debug("Znaleziono %d maili od %s", len(uids), opts["sender_filter"])

    for uid in uids:
        key = f"{opts['imap_folder']}:{uidvalidity}:{uid.decode()}"
        if key in state["done"]:
            continue

        typ, msgdata = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
            continue
        msg = email.message_from_bytes(msgdata[0][1])
        subject = decode_subject(msg)

        date = message_date(msg)
        if date is None or date < cutoff:
            log.debug("Pomijam stary mail „%s” (%s)", subject, date)
            mark_done(state, key)
            continue

        result = handle_message(msg)
        if result is None:
            log.debug("Mail „%s” nie zawiera linku %s", subject, opts["link_pattern"])
            mark_done(state, key)
            continue

        status, message = result
        attempts = state["attempts"].get(key, 0) + 1
        if status == "failed" and attempts < MAX_ATTEMPTS:
            log.warning("Próba %d/%d nieudana: %s", attempts, MAX_ATTEMPTS, message)
            state["attempts"][key] = attempts
            save_state(state)
            continue

        report(status, message, subject)
        mark_done(state, key)
        if status == "confirmed":
            after_confirm(imap, uid, trash)


def wait_for_mail(imap: imaplib.IMAP4_SSL) -> bool:
    """IMAP IDLE: czeka, aż serwer zgłosi nowy mail (EXISTS), najdłużej IDLE_RENEW s."""
    with imap.idle(duration=IDLE_RENEW) as idler:
        for typ, data in idler:
            log.debug("IDLE: %s %s", typ, data)
            if typ == "EXISTS":
                return True
    return False


def run_session(state: dict) -> None:
    imap = connect()
    try:
        use_idle = "IDLE" in imap.capabilities
        trash = find_trash(imap) if opts.get("after_confirm") == "trash" else None
        log.info(
            "Połączono z %s – %s; kosz: %s",
            opts["imap_host"],
            "czekam na nowe maile (IMAP IDLE)" if use_idle
            else f"serwer nie obsługuje IDLE, sprawdzam co {opts['poll_interval']} s",
            trash or "—",
        )
        while True:
            process(imap, state, trash)
            if use_idle:
                if wait_for_mail(imap):
                    log.debug("Serwer zgłosił nowy mail")
            else:
                time.sleep(opts["poll_interval"])
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def stop(signum, _frame) -> None:
    # Python jako PID 1 w kontenerze ignoruje SIGTERM bez własnej obsługi – Supervisor
    # zabijałby wtedy aplikację po 10 s (kod 137 = błąd przy każdym stop/update).
    log.info("Zatrzymywanie (sygnał %s)", signal.Signals(signum).name)
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "Start: %s@%s:%s folder=%s nadawca=%s po potwierdzeniu=%s%s",
        opts["imap_user"], opts["imap_host"], opts["imap_port"], opts["imap_folder"],
        opts["sender_filter"], opts.get("after_confirm", "trash"),
        " [DRY RUN]" if opts.get("dry_run") else "",
    )
    log.info(
        "Logowanie awaryjne do Netflixa: %s; zapisana sesja: %s",
        "włączone" if opts.get("netflix_email") and opts.get("netflix_password") else "wyłączone",
        "tak" if SESSION_FILE.exists() else "nie",
    )
    if not opts["imap_user"] or not opts["imap_password"]:
        log.error("Uzupełnij imap_user i imap_password w konfiguracji dodatku.")
        while True:
            time.sleep(3600)

    state = load_state()
    backoff = RECONNECT_MIN
    while True:
        started = time.monotonic()
        try:
            run_session(state)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("Błąd IMAP: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Nieoczekiwany błąd")
        # Długo działające połączenie, które się zerwało → łącz od razu; seria błędów → coraz wolniej.
        if time.monotonic() - started > RECONNECT_MAX:
            backoff = RECONNECT_MIN
        log.info("Ponowne połączenie za %d s", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX)


if __name__ == "__main__":
    main()
