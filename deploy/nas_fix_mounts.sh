#!/bin/sh
# Move the data volume out of app/ (it was created there by the first bind
# mount) and restart with the corrected sibling mounts.
#
# Set DGXCHAT_ROOT to your deployment directory if it is not the default.
set -e
ROOT=${DGXCHAT_ROOT:-/volume1/docker/dgxchat}
APP=$ROOT/app
DATA=$ROOT/data

echo "--- stopping ---"
cd "$APP"
docker compose down

if [ -d "$APP/data" ]; then
  echo "--- moving app/data -> data (preserving accounts) ---"
  mkdir -p "$DATA"
  cp -a "$APP/data/." "$DATA"/ 2>/dev/null || true
  rm -rf "$APP/data"
fi
if [ -d "$APP/web" ]; then
  echo "--- moving app/web -> web ---"
  mkdir -p "$ROOT/web"
  cp -a "$APP/web/." "$ROOT/web"/ 2>/dev/null || true
  rm -rf "$APP/web"
fi

echo "--- data contents ---"
ls -la "$DATA"
echo "--- web contents ---"
ls "$ROOT/web" | head

chown -R 10001:10001 "$DATA" 2>/dev/null || true

echo "--- starting ---"
cd "$APP"
docker compose up -d
sleep 6
docker compose ps
echo "--- health ---"
curl -s -m 8 http://127.0.0.1:8439/health; echo
echo "--- root (should be the web app) ---"
curl -s -m 8 http://127.0.0.1:8439/ | head -c 300; echo
