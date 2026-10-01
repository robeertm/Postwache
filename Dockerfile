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
ARG TAILSCALE_VERSION=1.86.2
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

# 🔴 NO `USER` here on purpose. The entrypoint starts as root, hands /data to
# the service user and then steps down with setpriv — that is the only way a
# mounted host directory becomes writable without asking everyone to chown it
# by hand. Nothing after that line runs as root.
VOLUME ["/data"]
EXPOSE 8110
# 🔴 401 counts as healthy. With the page lock switched on (5.0.0) `/api/lage`
# answers „please sign in" — which is the server working exactly as configured. A
# check that reads that as a failure restarts a perfectly healthy container.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request,urllib.error,sys\ntry:\n sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8110/api/lage',timeout=4).status==200 else 1)\nexcept urllib.error.HTTPError as e:\n sys.exit(0 if e.code==401 else 1)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/post_web.py"]
