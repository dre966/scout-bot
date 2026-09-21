#!/bin/bash
# spawn.sh - interactive bot spawner for Debian 13 EC2
# Lists BOT_IDs from data/routing.json and spawns selected bots via docker

set -e

IMAGE="scout-bot-scout-bot"
DATA_DIR="$(cd "$(dirname "$0")" && pwd)"
ROUTING="$DATA_DIR/data/routing.json"

# Check deps
if ! command -v docker >/dev/null 2>&1; then
  echo "[!] docker not found. Install: sudo apt update && sudo apt install -y docker.io docker-compose"
  exit 1
fi

if [ ! -f "$ROUTING" ]; then
  echo "[!] routing.json not found at $ROUTING"
  exit 1
fi

# Read GMAIL password (from data file or env)
GMAIL_PASS="${GMAIL_FAXCHECK2_APP_PASSWORD:-}"
if [ -z "$GMAIL_PASS" ] && [ -f "$DATA_DIR/data/gmail_app_password.txt" ]; then
  GMAIL_PASS="$(tr -d ' \r\n' < "$DATA_DIR/data/gmail_app_password.txt")"
fi
if [ -z "$GMAIL_PASS" ]; then
  echo "[!] GMAIL_FAXCHECK2_APP_PASSWORD not set and data/gmail_app_password.txt not found"
  echo "    Set: export GMAIL_FAXCHECK2_APP_PASSWORD='your16char'"
  exit 1
fi

# Ensure image exists, else build
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[*] Building $IMAGE..."
  docker compose -f "$DATA_DIR/docker-compose.yml" build --no-cache
fi

# List BOT_IDs
echo ""
echo "=== Scout Bot IDs ==="
python3 - << 'PY'
import json, pathlib
routing=json.loads(pathlib.Path("data/routing.json").read_text())
for i, e in enumerate(routing):
    typ = e["type"]
    poll = e["poll_inbox"]
    print(f"  {i:2d}  {e['proxy']:30s}  -> {poll:25s}  [{typ}]")
print()
PY

echo "Enter BOT_ID(s) to spawn (e.g. 14  or  0,14,20  or  0-3  or 'all'):"
echo -n "> "
read -r INPUT

# Parse input
IDS=""
if [ "$INPUT" = "all" ] || [ "$INPUT" = "ALL" ]; then
  IDS=$(python3 -c "import json; print(' '.join(str(i) for i in range(len(json.load(open('data/routing.json'))))))")
else
  # normalize commas and dashes
  INPUT_NORM=$(echo "$INPUT" | tr ',' ' ')
  # expand ranges like 0-3
  IDS_EXPANDED=""
  for token in $INPUT_NORM; do
    if echo "$token" | grep -q "-"; then
      START=$(echo "$token" | cut -d- -f1)
      END=$(echo "$token" | cut -d- -f2)
      IDS_EXPANDED="$IDS_EXPANDED $(seq $START $END)"
    else
      IDS_EXPANDED="$IDS_EXPANDED $token"
    fi
  done
  IDS="$IDS_EXPANDED"
fi

if [ -z "$IDS" ]; then
  echo "[!] No IDs selected"
  exit 1
fi

echo ""
echo "[*] Spawning bots: $IDS"
echo ""

for ID in $IDS; do
  # validate numeric
  if ! echo "$ID" | grep -Eq '^[0-9]+$'; then echo "[!] skip invalid ID: $ID"; continue; fi

  PROXY=$(python3 -c "import json; print(json.load(open('data/routing.json'))[$ID]['proxy'])" 2>/dev/null || echo "?")
  R5900=$((5900 + ID))
  R6080=$((6080 + ID))
  R9222=$((9222 + ID))
  NAME="scout-bot-$ID"

  echo "[*] BOT_ID=$ID  proxy=$PROXY  ports $R5900:$R6080:$R9222  name=$NAME"

  # remove existing container if exists
  if docker ps -a --format '{{.Names}}' | grep -q "^${NAME}$"; then
    echo "    -> removing existing $NAME"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi

  docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    -e BOT_ID="$ID" \
    -e GMAIL_FAXCHECK2_APP_PASSWORD="$GMAIL_PASS" \
    -e SERVER_URL="${SERVER_URL:-http://host.docker.internal/scout-server/api}" \
    -e BOT_TOKEN="${BOT_TOKEN:-scout-secret}" \
    -p "$R5900:5900" \
    -p "$R6080:6080" \
    -p "$R9222:9222" \
    -v "$DATA_DIR/data:/app/data" \
    -v "$DATA_DIR/logs:/app/logs" \
    "$IMAGE" >/dev/null

  echo "    -> up (VNC http://<ec2-ip>:$R6080  Chrome http://<ec2-ip>:$R9222)"
done

echo ""
echo "[ok] Done. Check:"
echo "  docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'"
echo "  docker logs -f scout-bot-<ID>"
echo ""
echo "To stop all:  docker rm -f \$(docker ps -aq --filter name=scout-bot-)"
echo "To stop one:  docker rm -f scout-bot-14"
