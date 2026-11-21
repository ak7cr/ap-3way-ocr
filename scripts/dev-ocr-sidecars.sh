#!/usr/bin/env bash
#
# Starts the two imported OCR engines as HTTP sidecars used for scanned images.
#
#   invoice-ocr-c++          -> http://127.0.0.1:8021
#   invoice-ocr-line-system  -> http://127.0.0.1:8022
#
# Why sidecars: on the AP sample kit these engines score 2/40 each on scanned
# images while this repo's own extractor scores 0/40, so scanned images are
# routed to them (see backend/services/ocr_router.py). Digital PDFs stay on this
# repo's extractor, which scores 25/40 against 9/40 for the sidecars.
#
# The upstream projects are NOT modified: they are launched as-is, with an
# isolated database each so their template/log tables never touch ours.
#
# IMPORTANT: the c++ project's own virtualenv is used for both services. Running
# them on this repo's interpreter changes the OCR output (0/40 instead of 2/40),
# so the interpreter is part of the measurement.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CPP_DIR="$REPO_ROOT/invoice-ocr-c++"
LINE_DIR="$REPO_ROOT/invoice-ocr-line-system"
CPP_PY="$CPP_DIR/venv/bin/python"
STATE_DIR="$REPO_ROOT/.sidecars"

CPP_PORT="${CPP_OCR_PORT:-8021}"
LINE_PORT="${LINE_OCR_PORT:-8022}"
HOST="${OCR_SIDECAR_HOST:-127.0.0.1}"

die() { echo "✖ $*" >&2; exit 1; }

[[ -d "$CPP_DIR" ]]  || die "$CPP_DIR not found (the upstream imports must be present)"
[[ -d "$LINE_DIR" ]] || die "$LINE_DIR not found (the upstream imports must be present)"
[[ -x "$CPP_PY" ]]   || die "$CPP_PY not found. Create it with: cd invoice-ocr-c++ && python3.13 -m venv venv && ./venv/bin/pip install -r requirements.txt"

mkdir -p "$STATE_DIR"

start() {
  local name="$1" dir="$2" port="$3"
  local db="$STATE_DIR/${name}.db"
  echo "▶ $name -> http://$HOST:$port (db: $db)"
  (
    cd "$dir"
    DATABASE_URL="sqlite:///$db" \
    "$CPP_PY" -m uvicorn backend.main:app --host "$HOST" --port "$port" \
      > "$STATE_DIR/${name}.log" 2>&1
  ) &
  echo $! > "$STATE_DIR/${name}.pid"
}

cleanup() {
  for name in cpp linesystem; do
    if [[ -f "$STATE_DIR/${name}.pid" ]]; then
      kill "$(cat "$STATE_DIR/${name}.pid")" 2>/dev/null || true
      rm -f "$STATE_DIR/${name}.pid"
    fi
  done
}
trap cleanup EXIT INT TERM

start cpp "$CPP_DIR" "$CPP_PORT"
start linesystem "$LINE_DIR" "$LINE_PORT"

echo "waiting for both sidecars to report ready..."
for port in "$CPP_PORT" "$LINE_PORT"; do
  for _ in $(seq 1 60); do
    if curl -fsS -m 2 "http://$HOST:$port/" >/dev/null 2>&1; then
      echo "  :$port ready"
      break
    fi
    sleep 1
  done
done

echo
echo "Sidecars are up. Our API routes scanned images to them automatically"
echo "(EXTERNAL_OCR_SERVICES, EXTERNAL_OCR_ENABLED). Logs: $STATE_DIR/*.log"
echo "Press Ctrl-C to stop."

wait
