#!/bin/bash
# spawn.sh - scout-bot docker spawner for EC2
# Usage:
#   ./spawn.sh              interactive: lists BOT_IDs, asks which to spawn
#   ./spawn.sh 14           spawn one bot
#   ./spawn.sh 0,1,2        spawn several
#   ./spawn.sh 0-5          spawn a range
#   ./spawn.sh all          spawn every BOT_ID in data/routing.json
set -euo pipefail

IMAGE="scout-bot"
DATA_DIR="$(cd "$(dirname "$0")" && pwd)"
ROUTING="$DATA_DIR/data/routing.json"
ENV_FILE="${SCOUT_ENV_FILE:-/etc/scout-bot.env}"

# server + secrets (SERVER_URL, BOT_TOKEN, GMAIL_* ...)
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export SERVER_URL="${SERVER_URL:-http://host.docker.internal/api}"
export BOT_TOKEN="${BOT_TOKEN:-scout-secret}"

die() { echo "[!] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found: sudo apt install -y docker.io"
[ -f "$ROUTING" ] || die "routing.json not found at $ROUTING"

# build image if missing
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[*] Building $IMAGE ..."
  docker build -t "$IMAGE" "$DATA_DIR" >/dev/null
fi

# env vars to pass into the container
ENV_ARGS=()
while IFS= read -r v; do ENV_ARGS+=(-e "$v"); done < <(
  compgen -v | grep -E '^(GMAIL_|SERVER_URL|BOT_TOKEN|BOT_EMAIL|LICENSE_ID|SITE_DOMAIN|BOT_MODE|API_PAIRING|POLL_INTERVAL|HUMAN_LIKE_MODE|DEBUGGER_PORT|CHROME_)' || true
)

# ---- select ids ------------------------------------------------------------
INPUT="${1:-}"
if [ -z "$INPUT" ]; then
  echo ""
  echo "=== Scout Bot IDs ==="
  python3 - "$ROUTING" <<'PY'
import json, sys, pathlib
routing = json.loads(pathlib.Path(sys.argv[1]).read_text())
for i, e in enumerate(routing):
    print(f"  {i:2d}  {e.get('proxy','?'):30s}  -> {e.get('poll_inbox','?'):25s}  [{e.get('type','?')}]")
print()
PY
  echo -n "Enter BOT_ID(s) (e.g. 14 or 0,14,20 or 0-3 or 'all'): "
  read -r INPUT
fi

N_IDS=$(python3 -c "import json;print(len(json.load(open('$ROUTING'))))")

if [ "$INPUT" = "all" ] || [ "$INPUT" = "ALL" ]; then
  IDS=$(seq 0 $((N_IDS - 1)))
else
  IDS=""
  for token in $(echo "$INPUT" | tr ',' ' '); do
    if echo "$token" | grep -qE '^[0-9]+-[0-9]+$'; then
      IDS="$IDS $(seq "$(echo "$token" | cut -d- -f1)" "$(echo "$token" | cut -d- -f2)")"
    elif echo "$token" | grep -qE '^[0-9]+$'; then
      IDS="$IDS $token"
    else
      die "invalid id: $token"
    fi
  done
fi
IDS=$(echo $IDS | tr ' ' '\n' | sort -n | uniq | tr '\n' ' ')
[ -n "$(echo $IDS | tr -d ' ')" ] || die "no ids selected"

echo ""
echo "[*] SERVER_URL=$SERVER_URL  spawning:$IDS"

for ID in $IDS; do
  if [ "$ID" -ge "$N_IDS" ]; then echo "[!] skip $ID (routing.json has $N_IDS entries)"; continue; fi
  PROXY=$(python3 -c "import json;print(json.load(open('$ROUTING'))[$ID].get('proxy','?'))" 2>/dev/null || echo "?")
  NAME="scout-bot-$ID"

  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "[*] $NAME exists -> recreating"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi

  docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    --shm-size=512m \
    --add-host=host.docker.internal:host-gateway \
    -e BOT_ID="$ID" \
    -p "$((9222 + ID)):9222" \
    -v "$DATA_DIR/data:/app/data" \
    -v "$DATA_DIR/logs:/app/logs" \
    "${ENV_ARGS[@]}" \
    "$IMAGE" >/dev/null

  echo "[ok] $NAME up  proxy=$PROXY  chrome=:$((9222 + ID))"
done

echo ""
docker ps --filter name=scout-bot- --format '  {{.Names}}  {{.Status}}'
