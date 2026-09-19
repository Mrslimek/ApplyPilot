#!/bin/bash
# Open the ApplyPilot dashboard from the VPS via an SSH tunnel (IPv4-friendly).
# Usage: ./open_dashboard.sh   (then open http://127.0.0.1:8730)

set -e
PORT=8730
HOST="${APPLYPILOT_VPS:-vps1euro-applypilot}"

# kill any previous tunnel on this port
lsof -ti tcp:$PORT | xargs kill 2>/dev/null || true
sleep 0.3

ssh -f -N -L $PORT:127.0.0.1:$PORT -L 5432:127.0.0.1:5432 "$HOST"
echo "tunnel up: http://127.0.0.1:$PORT"
if command -v open >/dev/null; then open "http://127.0.0.1:$PORT"; fi
