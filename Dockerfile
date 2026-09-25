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

WORKDIR /app
COPY postwache.py post_web.py post_web.html VERSION /app/
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
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8110/api/lage',timeout=4).status==200 else 1)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/post_web.py"]
