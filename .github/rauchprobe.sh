#!/usr/bin/env bash
# Faehrt EIN veroeffentlichtes Abbild an und sagt, ob es laeuft.
#
#   ./.github/rauchprobe.sh <abbild> <datenverzeichnis> <name> <port>
#
# 🔴 WARUM DAS HIER STEHT UND NICHT IM TOR (04.10.2026)
#
# `pruefstaende/vor_auslieferung.py` startet den ARBEITSSTAND als Prozess. Das
# ist richtig und faengt Vorlagenfehler — aber es sagt nichts ueber das Abbild,
# das Installationen wirklich ziehen. Bei DocuSort war genau das die Luecke:
# das Tor war gruen, das veroeffentlichte Abbild startete auf bestehenden
# Daten nicht, und Watchtower trug es um 20:36 zu dessen Kunden.
#
# Drei Fragen, und jede einzelne war bei DocuSort die, die den Fehler gesehen
# haette:
#
#   1. antwortet `/api/gesundheit` mit 200?        (HTTP 000 war die Antwort)
#   2. steht ein Absturz im Protokoll?             (7 Treffer waren es)
#   3. haengt es in einer Startschleife?           (restarting, restarts=10)
#
# 🔑 Die Antwort auf 1 allein genuegt nicht: ein Container, der in einer
# Neustartschleife haengt, antwortet zwischendurch. Darum alle drei.
set -euo pipefail

ABBILD="${1:?Abbild fehlt}"
DATEN="${2:?Datenverzeichnis fehlt}"
NAME="${3:?Name fehlt}"
PORT="${4:?Port fehlt}"

mkdir -p "$DATEN"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "── $NAME: $ABBILD auf $DATEN (Port $PORT) ──"
# 🔴 `--restart unless-stopped` ist kein Beiwerk: ohne Neustartregel bleibt ein
# Absturz ein stilles `exited`, und `RestartCount` steht dann auf 0. Mit ihr
# sieht man die Startschleife, die der Benutzer auch sieht.
docker run -d --name "$NAME" --restart unless-stopped \
  -p "127.0.0.1:${PORT}:8110" \
  -v "${DATEN}:/data" \
  -e POSTWACHE_TAKT=3600 \
  "$ABBILD" >/dev/null

fehler=0
code="000"
for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/gesundheit" || echo 000)"
    [ "$code" = "200" ] && break
    sleep 2
done
if [ "$code" = "200" ]; then
    echo "  OK   /api/gesundheit antwortet 200"
else
    echo "  🔴   /api/gesundheit antwortet $code"
    fehler=1
fi

# 🔑 `grep -c` mit `|| true`: ohne Treffer gibt grep 1 zurueck und `set -e`
# wuerde die Probe beenden, bevor sie ihr Urteil sagen kann.
treffer="$(docker logs "$NAME" 2>&1 | grep -c 'Traceback\|OperationalError\|SyntaxError' || true)"
if [ "$treffer" = "0" ]; then
    echo "  OK   kein Absturz im Protokoll"
else
    echo "  🔴   $treffer Absturz-Spuren im Protokoll"
    fehler=1
fi

status="$(docker inspect -f '{{.State.Status}}' "$NAME")"
neustarts="$(docker inspect -f '{{.RestartCount}}' "$NAME")"
if [ "$status" = "running" ] && [ "$neustarts" = "0" ]; then
    echo "  OK   laeuft, keine Neustarts"
else
    echo "  🔴   Status=$status Neustarts=$neustarts"
    fehler=1
fi

if [ "$fehler" != "0" ]; then
    echo "── Protokoll von $NAME ──"
    docker logs "$NAME" 2>&1 | tail -60
fi

# Der Container geht weg, das Datenverzeichnis BLEIBT — der Aufstiegstest
# braucht genau das, was die vorige Fassung hinterlassen hat.
docker rm -f "$NAME" >/dev/null 2>&1 || true

if [ "$fehler" != "0" ]; then
    echo "🔴 NICHT AUSLIEFERN — $ABBILD"
    exit 1
fi
echo "✅ $ABBILD ist angefahren und laeuft"
