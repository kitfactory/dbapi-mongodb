#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MONGO_DIR="$ROOT/mongodb-3.6"
MONGOD_BIN="$MONGO_DIR/bin/mongod"
DATABASE_DIR="$MONGO_DIR/data/db"
LOG_DIR="$MONGO_DIR/logs"
LOG_FILE="$LOG_DIR/mongod.log"
PID_FILE="$LOG_DIR/mongod.pid"
PORT="${PORT:-27017}"

if [ ! -x "$MONGOD_BIN" ]; then
  echo "[mdb][start] mongod not found at $MONGOD_BIN" >&2
  exit 1
fi

mkdir -p "$DATABASE_DIR" "$LOG_DIR"

"$MONGOD_BIN" \
  --dbpath "$DATABASE_DIR" \
  --bind_ip 127.0.0.1 \
  --port "$PORT" \
  --logpath "$LOG_FILE" \
  --pidfilepath "$PID_FILE" \
  --fork

echo "[mdb][start] mongod started on 127.0.0.1:${PORT} (dbpath: $DATABASE_DIR)"
