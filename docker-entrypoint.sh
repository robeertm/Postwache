#!/bin/sh
# 🔴 A container that writes into a mounted directory has to OWN it.
#
# The image runs as an unprivileged user. A host directory mounted at /data
# belongs to whoever created it on the host — usually not that user — so the
# first write fails with "permission denied" and the container exits before it
# has said anything useful. Every volume-mounting image solves this the same
# way: start as root, hand /data to the service user, then step down and never
# come back.
#
# PUID/PGID let a NAS user match the ownership they already have, which is why
# every image on a Synology or a QNAP offers them.
set -e

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-10001}"
    PGID="${PGID:-10001}"

    if [ "$PGID" != "$(id -g postwache)" ]; then
        groupmod -o -g "$PGID" postwache
    fi
    if [ "$PUID" != "$(id -u postwache)" ]; then
        usermod -o -u "$PUID" postwache
    fi

    mkdir -p "${POSTWACHE_HOME:-/data}"
    # Only the top level and what we own — a big library of mail state should
    # not be walked on every start.
    chown postwache:postwache "${POSTWACHE_HOME:-/data}" || true
    find "${POSTWACHE_HOME:-/data}" -maxdepth 2 ! -user postwache \
         -exec chown postwache:postwache {} + 2>/dev/null || true

    exec setpriv --reuid=postwache --regid=postwache --init-groups "$@"
fi

exec "$@"
