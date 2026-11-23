# Debian (nie Alpine), bo Playwright/Chromium wymaga glibc.
# Python 3.14+: imaplib.IMAP4.idle() (IMAP IDLE).
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN pip install --no-cache-dir playwright==1.64.1 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY run.py /run.py

CMD ["python3", "/run.py"]

LABEL \
    org.opencontainers.image.title="Home Assistant App: Netflix Household Auto-Confirm" \
    org.opencontainers.image.source="https://github.com/krzysztof-cislo/ha-apps"
