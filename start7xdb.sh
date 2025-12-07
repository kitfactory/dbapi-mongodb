#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MONGO_DIR="$ROOT/mongodb-7.0"
MONGOD_BIN="$MONGO_DIR/bin/mongod"
MONGO_BIN="$MONGO_DIR/bin/mongo"
DATABASE_DIR="$MONGO_DIR/data/db"
LOG_DIR="$MONGO_DIR/logs"
LOG_FILE="$LOG_DIR/mongod.log"
PID_FILE="$LOG_DIR/mongod.pid"
PORT="${PORT:-27029}"
REPL_SET="${REPL_SET:-rs0}"

if [ ! -x "$MONGOD_BIN" ]; then
  echo "[mdb][start] mongod not found at $MONGOD_BIN" >&2
  exit 1
fi
if [ ! -x "$MONGO_BIN" ]; then
  echo "[mdb][start] mongo shell not found at $MONGO_BIN" >&2
  exit 1
fi

mkdir -p "$DATABASE_DIR" "$LOG_DIR"

"$MONGOD_BIN" \
  --dbpath "$DATABASE_DIR" \
  --bind_ip 127.0.0.1 \
  --port "$PORT" \
  --replSet "$REPL_SET" \
  --logpath "$LOG_FILE" \
  --pidfilepath "$PID_FILE" \
  --fork

"$MONGO_BIN" \
  --port "$PORT" \
  --quiet \
  --eval "
var status = null;
try {
  status = rs.status();
} catch (e) {
  if (e.code !== 94) { throw e; }
}
if (!status || status.ok !== 1) {
  const cfg = { _id: \"$REPL_SET\", members: [{ _id: 0, host: \"127.0.0.1:$PORT\" }] };
  const initRes = rs.initiate(cfg);
  if (initRes.ok !== 1) { throw new Error('rs.initiate failed: ' + tojson(initRes)); }
}
for (let i = 0; i < 30; i++) {
  const hello = db.adminCommand({ hello: 1 });
  if (hello.isWritablePrimary || hello.ismaster) { quit(0); }
  sleep(200);
}
throw new Error('primary not ready');
" >/dev/null

echo "[mdb][start] mongod 7.x started on 127.0.0.1:${PORT} (dbpath: $DATABASE_DIR)"
