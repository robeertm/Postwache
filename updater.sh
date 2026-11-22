#!/bin/sh
# updater.sh — ONE tick of the update companion (POSIX sh, runs in `docker:cli`).
#
# The companion container fetches THIS file out of the running Postwache
# container before every tick (`docker cp postwache:/app/updater.sh`), so the
# script that runs is always the one belonging to the version that is running —
# it can never be older than the app it serves. (A companion that carries its own
# copy and never renews it keeps old bugs alive for ever.)
#
# One tick = write the heartbeat, then look for <data>/updater/anfrage.json.
# Nothing there: done. A request there: check -> back up -> swap -> verify, and
# if the new version does not come up healthy, roll back.
#
#   pruefen  the image is pulled; it must name a digest, carry exactly the
#            version asked for, and START (probe container with an EMPTY data
#            folder — never with the real mailbox access, that would put a
#            second watchman on the same mailbox)
#   sichern  <data> as a tarball (without updater/, ollama/, tailscale/) and the
#            running image tagged `postwache-lastgood:<old version>`
#   tauschen `docker compose up -d --no-deps postwache`
#   warten   healthy AND the version asked for, within WARTEN seconds
#   fehler   -> stop, restore the tarball, put the old image back as :latest,
#            start, and remember the version in gesperrt.json
#
# Forward only. Switched off by `"update_klick": false` in state/einstellungen.json
# or UPDATER_AUS=1. Every outcome lands in status.json; the page only reads it.
set -u

DATA="${DATA:-/data}"
APP="${APP:-postwache}"
IMAGE="${IMAGE:-ghcr.io/robeertm/postwache:latest}"
APP_URL="${APP_URL:-http://postwache:8110}"
WARTEN="${WARTEN:-180}"
PROBE_WARTEN="${PROBE_WARTEN:-60}"
PAUSE="${PAUSE:-3}"
BEHALTEN="${BEHALTEN:-3}"
COMPOSE="${COMPOSE:-docker compose}"
LASTGOOD="postwache-lastgood"
U="$DATA/updater"

mkdir -p "$U/sicherung"
chown 10001:10001 "$U" 2>/dev/null || true
chmod 755 "$U"
date +%s > "$U/herzschlag"
chmod 644 "$U/herzschlag"

[ -f "$U/anfrage.json" ] || exit 0
mv "$U/anfrage.json" "$U/anfrage.laeuft"

status() {  # phase ziel ergebnis grund
    printf '{"phase": "%s", "ziel": "%s", "ergebnis": "%s", "grund": "%s", "zeit": %s}\n' \
        "$1" "$2" "${3:-}" "${4:-}" "$(date +%s)" > "$U/status.json.tmp"
    chmod 644 "$U/status.json.tmp"
    mv "$U/status.json.tmp" "$U/status.json"
}

laufende_fassung() {
    wget -qO- -T 4 "$APP_URL/api/gesundheit" 2>/dev/null \
        | sed -n 's/.*"fassung": *"\([0-9.]*\)".*/\1/p'
}

neuer() {  # $1 newer than $2 ? (numeric, per component)
    awk -v a="$1" -v b="$2" 'BEGIN{
        n=split(a,x,"."); m=split(b,y,".");
        for(i=1;i<=3;i++){ if((x[i]+0)>(y[i]+0)) exit 0; if((x[i]+0)<(y[i]+0)) exit 1 }
        exit 1 }'
}

ende() {  # phase ziel ergebnis grund — finish this request
    status "$1" "$2" "$3" "$4"
    rm -f "$U/anfrage.laeuft"
    exit 0
}

ziel="$(sed -n 's/.*"ziel": *"\([0-9.]*\)".*/\1/p' "$U/anfrage.laeuft")"
alt="$(laufende_fassung)"
[ -n "$alt" ] || alt="0.0.0"

if [ "${UPDATER_AUS:-0}" = "1" ] || grep -q '"update_klick": *false' "$DATA/state/einstellungen.json" 2>/dev/null; then
    ende abgebrochen "$ziel" abgebrochen klick_aus
fi
echo "$ziel" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || ende abgebrochen "$ziel" abgebrochen ziel_ungueltig
neuer "$ziel" "$alt" || ende abgebrochen "$ziel" abgebrochen nicht_neuer
grep -q "\"version\": *\"$ziel\"" "$U/gesperrt.json" 2>/dev/null && ende abgebrochen "$ziel" abgebrochen gesperrt

# ── pruefen ──────────────────────────────────────────────────────────────
status pruefen "$ziel"
docker pull "$IMAGE" >/dev/null 2>&1 || ende abgebrochen "$ziel" abgebrochen pull
digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null)"
[ -n "$digest" ] || ende abgebrochen "$ziel" abgebrochen kein_digest
neu="$(docker run --rm --label wk.wegwerf=1 --entrypoint cat "$IMAGE" /app/VERSION 2>/dev/null | tr -d ' \r\n')"
# The feed can name a release whose image has not been promoted to :latest yet.
[ "$neu" = "$ziel" ] || ende abgebrochen "$ziel" abgebrochen fassung_passt_nicht

docker rm -f "$APP-probe" >/dev/null 2>&1
if ! docker run -d --name "$APP-probe" --label wk.wegwerf=1 \
        -e POSTWACHE_TAKT=86400 -e POSTWACHE_UPDATE_CHECK=0 "$IMAGE" >/dev/null 2>&1; then
    ende abgebrochen "$ziel" abgebrochen probe
fi
ok=0; i=0
while [ "$i" -lt "$PROBE_WARTEN" ]; do
    if docker exec "$APP-probe" python3 /usr/local/bin/gesundheit.py >/dev/null 2>&1; then ok=1; break; fi
    i=$((i + PAUSE)); sleep "$PAUSE"
done
docker rm -f "$APP-probe" >/dev/null 2>&1
[ "$ok" = 1 ] || ende abgebrochen "$ziel" abgebrochen probe

# ── sichern ──────────────────────────────────────────────────────────────
status sichern "$ziel"
sich="$U/sicherung/postwache-$alt-$(date +%Y%m%d-%H%M%S).tar.gz"
tar czf "$sich" -C "$DATA" --exclude=./updater --exclude=./ollama --exclude=./tailscale . 2>/dev/null \
    || { rm -f "$sich"; ende abgebrochen "$ziel" abgebrochen sicherung; }
tar tzf "$sich" >/dev/null 2>&1 || { rm -f "$sich"; ende abgebrochen "$ziel" abgebrochen sicherung; }
bild="$(docker inspect --format '{{.Image}}' "$APP" 2>/dev/null)"
if [ -n "$bild" ]; then
    docker tag "$bild" "$LASTGOOD:$alt" || ende abgebrochen "$ziel" abgebrochen sicherung
fi
# keep the newest few tarballs only
ls -t "$U"/sicherung/postwache-*.tar.gz 2>/dev/null | tail -n +$((BEHALTEN + 1)) | while read -r f; do rm -f "$f"; done

# ── tauschen + warten ────────────────────────────────────────────────────
gesund_und_neu() {  # $1 = version expected
    j=0
    while [ "$j" -lt "$WARTEN" ]; do
        h="$(docker inspect --format '{{.State.Health.Status}}' "$APP" 2>/dev/null)"
        if [ "$h" = "healthy" ] && [ "$(laufende_fassung)" = "$1" ]; then return 0; fi
        j=$((j + PAUSE)); sleep "$PAUSE"
    done
    return 1
}

status tauschen "$ziel"
cd "${PROJEKT:-.}" 2>/dev/null || true
$COMPOSE up -d --no-deps --pull never "$APP" >/dev/null 2>&1
status warten "$ziel"
if gesund_und_neu "$ziel"; then
    ende fertig "$ziel" ok ""
fi

# ── Rueckweg ─────────────────────────────────────────────────────────────
status zurueck "$ziel"
$COMPOSE stop "$APP" >/dev/null 2>&1
if [ -f "$sich" ]; then
    # restore what the backup holds; updater/, ollama/ and tailscale/ were never in it
    find "$DATA" -mindepth 1 -maxdepth 1 ! -name updater ! -name ollama ! -name tailscale -exec rm -rf {} + 2>/dev/null
    tar xzf "$sich" -C "$DATA" 2>/dev/null
fi
[ -n "$bild" ] && docker tag "$LASTGOOD:$alt" "$IMAGE" 2>/dev/null
printf '{"version": "%s", "zeit": %s}\n' "$ziel" "$(date +%s)" > "$U/gesperrt.json"
chmod 644 "$U/gesperrt.json"
$COMPOSE up -d --no-deps --pull never --force-recreate "$APP" >/dev/null 2>&1
if gesund_und_neu "$alt"; then
    ende zurueck "$ziel" zurueckgerollt neu_fassung_krank
fi
ende zurueck "$ziel" rollback_fehlgeschlagen neu_fassung_krank
