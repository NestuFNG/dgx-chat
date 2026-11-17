#!/bin/sh
# Reset the deployment to a clean production state (DESTROYS the database and
# uploaded files). Set DGXCHAT_ROOT to your deployment directory.
set -e
ROOT=${DGXCHAT_ROOT:-/volume1/docker/dgxchat}
APP=$ROOT/app
DATA=$ROOT/data

echo "--- stopping ---"
cd "$APP"
docker compose down 2>/dev/null || true

echo "--- wiping dev leftovers from the app dir ---"
rm -rf "$APP/data" "$APP/artifacts" "$APP/tests/__pycache__" "$APP/app/__pycache__"
find "$APP" -name '*.pyc' -delete 2>/dev/null || true
find "$APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "--- wiping data volume (SQLite + uploads) ---"
rm -rf "$DATA"/*
ls -la "$DATA"

echo "--- starting ---"
cd "$APP"
docker compose up -d
sleep 5
docker compose ps
echo "--- health ---"
curl -s -m 8 http://127.0.0.1:8439/health; echo
