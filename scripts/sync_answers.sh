#!/bin/bash
# Sync screening_answers between the Mac (where applies run) and the VPS
# (where the Telegram bot answers them).
#
#  Mac -> VPS: push pending questions collected during applies
#  VPS -> Mac: pull answers recorded via the bot (/answer)
#
# Run after an apply session on the Mac, or before the next one.

set -e
VPS="${APPLYPILOT_VPS:-vps1euro-applypilot}"
LOCAL_DB="$HOME/.applypilot/applypilot.db"
TMP=/tmp/applypilot_answers_sync

mkdir -p "$TMP"

# 1) export the full table from the Mac
sqlite3 "$LOCAL_DB" "SELECT id|'|'||question|'|'||COALESCE(options,'')|'|'||COALESCE(answer,'')|'|'||status|'|'||COALESCE(job_url,'') FROM screening_answers" > "$TMP/mac.tsv"

# 2) export the full table from the VPS
ssh "$VPS" "sqlite3 /root/.applypilot/applypilot.db \"SELECT id|'|'||question|'|'||COALESCE(options,'')|'|'||COALESCE(answer,'')|'|'||status|'|'||COALESCE(job_url,'') FROM screening_answers\"" > "$TMP/vps.tsv"

# 3) merge on the Mac: keep pending from VPS that Mac lacks, take VPS answers
python3 - "$TMP" <<'EOF'
import sqlite3, sys, os
tmp = sys.argv[1]
def rows(p):
    out = []
    for line in open(p):
        parts = line.rstrip("\n").split("|", 4)
        if len(parts) == 5:
            out.append(parts)
    return out
local = {r[1]: r for r in rows(os.path.join(tmp, "mac.tsv"))}
remote = rows(os.path.join(tmp, "vps.tsv"))
conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"))
added = updated = 0
for _id, q, opts, ans, status, *_ in [(r[0], r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else "") for r in remote]:
    if q not in local:
        conn.execute("INSERT OR IGNORE INTO screening_answers (question, options, answer, status, job_url) VALUES (?,?,?,?,?)",
                     (q, opts or None, ans or None, status, None))
        added += 1
    elif ans and ans != (local[q][3] if len(local[q]) > 3 else ""):
        conn.execute("UPDATE screening_answers SET answer=?, status='answered' WHERE question=?", (ans, q))
        updated += 1
conn.commit(); conn.close()
print(f"mac merge: +{added} from vps, {updated} answers applied")
EOF

# 4) push everything back to the VPS (question is UNIQUE -> safe upsert)
python3 - "$TMP" <<'EOF'
import sqlite3, sys, os, json
tmp = sys.argv[1]
conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"))
rows = conn.execute("SELECT question, COALESCE(options,''), COALESCE(answer,''), status, COALESCE(job_url,'') FROM screening_answers").fetchall()
json.dump(rows, open(os.path.join(tmp, "merged.json"), "w"))
print(f"exported {len(rows)} rows for vps")
EOF
scp -q "$TMP/merged.json" "$VPS:/tmp/answers_merged.json"
ssh "$VPS" '/root/ApplyPilot/.venv/bin/python - <<EOF
import json, sqlite3
rows = json.load(open("/tmp/answers_merged.json"))
conn = sqlite3.connect("/root/.applypilot/applypilot.db")
for q, opts, ans, status, job_url in rows:
    conn.execute("""INSERT INTO screening_answers (question, options, answer, status, job_url)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(question) DO UPDATE SET
                      answer=COALESCE(excluded.answer, screening_answers.answer),
                      status=CASE WHEN excluded.answer IS NOT NULL AND excluded.answer != ""
                              THEN "answered" ELSE screening_answers.status END""",
                 (q, opts or None, ans or None, status, job_url or None))
conn.commit()
print("vps merge done:", len(rows), "rows")
EOF'
