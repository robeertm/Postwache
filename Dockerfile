# Postwache — a watchman for your mailbox.
#
# One process does both jobs: it serves the page and, because a container has
# no cron, it ticks the watcher itself (POSTWACHE_TAKT). Everything the
# Postwache learns lives in /data, which you mount — the image carries code,
# never state.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Postwache" \
      org.opencontainers.image.description="A watchman for your mailbox: sorts the noise out, leaves what matters where you see it." \
      org.opencontainers.image.licenses="LicenseRef-Proprietary-Use"

ENV PYTHONUNBUFFERED=1 \
    POSTWACHE_HOME=/data \
    POSTWACHE_WEB_PORT=8110 \
    POSTWACHE_TAKT=60 \
    TZ=Europe/Berlin

# ── Tailscale, carried in the image ─────────────────────────────────────────
# 🔑 WHY IN THE IMAGE AND NOT AS A SIDECAR
# A sidecar means editing a compose file, on a command line, on the machine.
# In here it means one field and one button on the settings page.
#
# 🔑 WHY THIS WORKS WITHOUT PRIVILEGES — measured, not hoped:
# `tailscaled --tun=userspace-networking` needs neither `NET_ADMIN` nor
# `/dev/net/tun`. Measured twice in a bare container: as root, and as **uid
# 10001**, which is what this image steps down to. Both start and report
# „Logged out." The second measurement is the one that mattered — nothing here
# runs as root.
#
# `TARGETARCH` is set by Docker per architecture; without it an arm64 image
# would pull the amd64 binaries.
ARG TARGETARCH
# 🔴 EINE FESTGENAGELTE FASSUNG BEWEGT SICH NIE VON SELBST.
# Das Abbild wird stuendlich erneuert, aber dieses Tailscale bleibt auf genau
# der Nummer, die hier steht — auf dessen Admin-Seite erschien darum ein
# Update-Pfeil an einem Knoten, dessen Abbild taeglich frisch gebaut wurde.
# Die Nummer bleibt trotzdem fest, weil ein Bau, der „das Neueste" holt, nicht
# wiederholbar ist und eine schlechte Veroeffentlichung still einzieht.
# Stattdessen bewacht `probe_tailscale.py` diese Zeile: er fragt
# pkgs.tailscale.com nach der stabilen Fassung und wird ROT, wenn hier eine
# aeltere steht. So faellt das Nachziehen am Tor auf, nicht beim Benutzer.
ARG TAILSCALE_VERSION=1.102.4
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_${TARGETARCH}.tgz" \
      -o /tmp/ts.tgz; \
    tar xzf /tmp/ts.tgz -C /tmp; \
    mv /tmp/tailscale_${TAILSCALE_VERSION}_${TARGETARCH}/tailscaled /usr/local/bin/; \
    mv /tmp/tailscale_${TAILSCALE_VERSION}_${TARGETARCH}/tailscale  /usr/local/bin/; \
    rm -rf /tmp/ts.tgz /tmp/tailscale_*; \
    apt-get purge -y curl && apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    tailscaled --version

WORKDIR /app
COPY postwache.py post_web.py post_web.html VERSION ollama_einrichten.py umbau.py \
     umzug.py klient.py post_klient.html post_mobil.html tailscale_zugang.py /app/
# The language files belong to the program, not to the state. Without them the
# page shows its keys instead of text — and nothing reports that.
COPY locales /app/locales

# No third-party packages. The Postwache speaks IMAP and HTTP with what the
# standard library already brings — that is why this image is small and why
# there is nothing here to keep patched.
RUN useradd --create-home --uid 10001 postwache \
 && mkdir -p /data && chown -R postwache:postwache /data /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY gesundheit.py /usr/local/bin/gesundheit.py

# 🔴 NO `USER` here on purpose. The entrypoint starts as root, hands /data to
# the service user and then steps down with setpriv — that is the only way a
# mounted host directory becomes writable without asking everyone to chown it
# by hand. Nothing after that line runs as root.
VOLUME ["/data"]
EXPOSE 8110
# 🔑 The check asks `/api/gesundheit` — a door of its own that is not behind
# the page lock and does no work. The old check asked `/api/lage`, which reads
# state and (since 5.11.0) starts a Tailscale subprocess: measured 120–320 ms
# against 3 ms for the page, inside a 5 s limit every 60 s. It also sat behind
# the lock, which is why it had to count 401 as healthy — a special case that
# only existed because the wrong door was used.
# 🔴 A FILE, not a one-liner. The previous version wrote `\n` inside a
# double-quoted shell string; `sh -c` does not turn that into a newline, so
# Python got a backslash and an n and died with a SyntaxError on every single
# run since 3.0.0. See gesundheit.py for the whole story.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD ["python3", "/usr/local/bin/gesundheit.py"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/post_web.py"]
