#!/usr/bin/env bash
#
# Start all four mock instruments and the two mock robots. Ctrl-C stops them all.
#
# They must be four separate processes, not four threads: every instrument
# project has a top-level package named `sila`, so the first one imported in a
# process wins and the rest would silently reuse it.
#
# The flags go to the four SiLA mocks only. The MiR and UR mocks start with their
# defaults (8 s drives, 8 s tasks, interlock on). For other timings start them
# yourself: .venv/bin/python -m mock_servers.mir.server --help
#
# Usage:
#   ./run_all.sh                  # default ports, 20s runs, mDNS on
#   ./run_all.sh --run-seconds 60 # slower runs
#   ./run_all.sh --no-advertise   # stay off mDNS
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    echo "No interpreter at $PYTHON." >&2
    echo "Set it up with:  uv venv --python 3.12 .venv && uv pip install -e '.[dev]'" >&2
    exit 1
fi

LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"

pids=()

stop() {
    echo
    echo "stopping..."
    for pid in "${pids[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${pids[@]:-}"; do
        wait "$pid" 2>/dev/null || true
    done
    echo "stopped."
}
trap stop INT TERM EXIT

for instrument in x3dsc sdt gpc ftir; do
    "$PYTHON" -u -m "mock_servers.${instrument}.server" "$@" > "$LOG_DIR/${instrument}.log" 2>&1 &
    pids+=("$!")
    echo "  ${instrument}  pid $!  -> $LOG_DIR/${instrument}.log"
done

for robot in mir ur; do
    "$PYTHON" -u -m "mock_servers.${robot}.server" > "$LOG_DIR/${robot}.log" 2>&1 &
    pids+=("$!")
    echo "  ${robot}  pid $!  -> $LOG_DIR/${robot}.log"
done

"$PYTHON" -u -m mock_servers.opcua.server > "$LOG_DIR/opcua.log" 2>&1 &
pids+=("$!")
echo "  opcua  pid $!  -> $LOG_DIR/opcua.log"

echo
echo "Seven mocks starting. SiLA on 50061-50064, MiR on 50065, UR on 50066, OPC UA on 4840. Ctrl-C to stop."
echo "Watch them with:  tail -f $LOG_DIR/*.log"
echo

wait
