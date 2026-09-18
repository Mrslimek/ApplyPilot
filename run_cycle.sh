#!/bin/bash
# ApplyPilot daily cycle: discovery -> enrichment -> scoring -> tailoring -> cover letters.
# Auto-apply is intentionally NOT here — run it manually: /root/ApplyPilot/.venv/bin/applypilot apply --limit N
export PATH=/usr/local/bin:/usr/bin:/bin
export HOME=/root
cd /root/ApplyPilot
mkdir -p /root/.applypilot/logs
exec >> /root/.applypilot/logs/cron.log 2>&1
echo "========================================================"
echo "[cycle start] $(date -u '+%Y-%m-%d %H:%M UTC')"
boards/applypilot-boards
.venv/bin/applypilot run discover enrich -w 1
.venv/bin/applypilot run score
.venv/bin/applypilot run tailor cover
echo "[cycle end] $(date -u '+%Y-%m-%d %H:%M UTC')"
.venv/bin/applypilot status
