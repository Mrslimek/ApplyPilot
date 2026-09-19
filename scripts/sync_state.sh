#!/bin/bash
# Sync apply results from the Mac (where applies run) to the VPS (where the
# Telegram bot reports stats), and screening answers both ways.
# Best-effort: exits silently if the VPS is unreachable.

set -e
VPS="${APPLYPILOT_VPS:-vps1euro-applypilot}"
LOCAL_DB="$HOME/.applypilot/applypilot.db"
TMP=/tmp/applypilot_state_sync
mkdir -p "$TMP"

# fast reachability check — bail out quietly
ssh -o ConnectTimeout=5 -o BatchMode=yes "$VPS" true 2>/dev/null || { echo "vps unreachable, skip sync"; exit 0; }

# 1) apply-result columns, Mac -> VPS (UPDATE by url; jobs originate on the VPS)
sqlite3 "$LOCAL_DB" "SELECT json_group_array(json_object('url',url,'applied_at',applied_at,'apply_status',apply_status,'apply_error',apply_error,'apply_attempts',COALESCE(apply_attempts,0),'apply_duration_ms',COALESCE(apply_duration_ms,0),'apply_report',apply_report)) FROM jobs WHERE applied_at IS NOT NULL OR apply_status IS NOT NULL" > "$TMP/results.json"
scp -q "$TMP/results.json" "$VPS:/tmp/state_results.json"
ssh "$VPS" '/root/ApplyPilot/.venv/bin/python - <<EOF
import json, sqlite3
rows = json.load(open("/tmp/state_results.json"))
conn = sqlite3.connect("/root/.applypilot/applypilot.db")
for r in rows:
    # upsert: history rows may not exist on this side yet
    conn.execute("""INSERT OR IGNORE INTO jobs (url, applied_at, apply_status)
                    VALUES (?,?,?)""",
                 (r["url"], r["applied_at"], r["apply_status"]))
    conn.execute("""UPDATE jobs SET applied_at=?, apply_status=?, apply_error=?,
                    apply_attempts=?, apply_duration_ms=?, apply_report=? WHERE url=?""",
                 (r["applied_at"], r["apply_status"], r["apply_error"], r["apply_attempts"],
                  r["apply_duration_ms"], r["apply_report"], r["url"]))
conn.commit()
print("vps: apply results merged:", len(rows))
EOF'

# 2) screening answers both ways (questions Mac->VPS, answers VPS->Mac)
sqlite3 "$LOCAL_DB" "SELECT json_group_array(json_object('q',question,'o',COALESCE(options,''),'a',COALESCE(answer,''),'s',status)) FROM screening_answers" > "$TMP/answers.json"
scp -q "$TMP/answers.json" "$VPS:/tmp/state_answers.json"
ssh "$VPS" '/root/ApplyPilot/.venv/bin/python - <<EOF
import json, sqlite3
rows = json.load(open("/tmp/state_answers.json"))
conn = sqlite3.connect("/root/.applypilot/applypilot.db")
for r in rows:
    conn.execute("""INSERT INTO screening_answers (question, options, answer, status)
                    VALUES (?,?,?,?)
                    ON CONFLICT(question) DO UPDATE SET
                      answer=COALESCE(excluded.answer, screening_answers.answer),
                      status=CASE WHEN excluded.answer IS NOT NULL AND excluded.answer != ""
                              THEN "answered" ELSE screening_answers.status END""",
                 (r["q"], r["o"] or None, r["a"] or None, r["s"]))
conn.commit()
print("vps: answers merged:", len(rows))
EOF'
ssh "$VPS" "sqlite3 /root/.applypilot/applypilot.db \"SELECT json_group_array(json_object('q',question,'a',COALESCE(answer,''))) FROM screening_answers WHERE status='answered'\"" > "$TMP/answered.json"
python3 - "$TMP" <<'EOF'
import json, sqlite3, sys, os
answered = {r["q"]: r["a"] for r in json.load(open(os.path.join(sys.argv[1], "answered.json"))) if r.get("a")}
if not answered:
    print("mac: no remote answers to pull")
    sys.exit(0)
conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"))
n = 0
for q, a in answered.items():
    cur = conn.execute("UPDATE screening_answers SET answer=?, status='answered' WHERE question=? AND (answer IS NULL OR answer='')", (a, q))
    n += cur.rowcount
conn.commit()
print(f"mac: pulled {n} answered questions")
EOF

echo "sync done"
