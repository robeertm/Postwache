#!/usr/bin/env sh
# Postwache — one-command install.
#
# Writes a docker-compose.yml next to itself (or into $POSTWACHE_DIR), pulls
# the published image and starts it. Re-running is safe: an existing
# docker-compose.yml is kept, never overwritten.
set -eu

DIR="${POSTWACHE_DIR:-./postwache}"
PORT="${POSTWACHE_PORT:-8110}"
IMAGE="${POSTWACHE_IMAGE:-ghcr.io/der Dienstbenutzer/postwache:latest}"
TZ_DEFAULT="${TZ:-Europe/Berlin}"

say()  { printf '  %s\n' "$1"; }
warn() { printf '  ! %s\n' "$1"; }
die()  { printf '\n  %s\n\n' "$1"; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is missing. Install Docker, then run this again."
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "Docker Compose is missing. Install the compose plugin and run this again."
fi

printf '\n  Postwache\n\n'
say "Installing into $DIR"
mkdir -p "$DIR/data"

if [ -f "$DIR/docker-compose.yml" ]; then
  warn "docker-compose.yml exists — keeping it."
else
  cat > "$DIR/docker-compose.yml" <<YAML
services:
  postwache:
    image: $IMAGE
    container_name: postwache
    restart: unless-stopped
    ports:
      - "$PORT:8110"
    environment:
      - TZ=$TZ_DEFAULT
      - POSTWACHE_TAKT=60
    volumes:
      - ./data:/data

  # Optional: let Watchtower pull new images by itself (nightly at 04:00).
  # It needs the Docker socket — effectively root on the host. Without it,
  # update by hand: docker compose pull && docker compose up -d
  # watchtower:
  #   image: containrrr/watchtower
  #   container_name: postwache-watchtower
  #   restart: unless-stopped
  #   volumes:
  #     - /var/run/docker.sock:/var/run/docker.sock:ro
  #   command: --cleanup --schedule "0 0 4 * * *" postwache
YAML
  say "Wrote $DIR/docker-compose.yml"
fi

say "Pulling $IMAGE"
( cd "$DIR" && $COMPOSE pull && $COMPOSE up -d )

HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="${HOST:-localhost}"
cat <<DONE

  The Postwache is starting.

    Page          http://$HOST:$PORT
    Its state     $DIR/data

  Open the page, go to Settings and add a mailbox. Nothing is moved until you
  arm it — until then it only writes down what it WOULD do.

  Update later:  cd $DIR && $COMPOSE pull && $COMPOSE up -d
                 (or uncomment the watchtower block in docker-compose.yml)
  Logs:          cd $DIR && $COMPOSE logs -f

DONE
