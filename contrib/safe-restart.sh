#!/bin/bash
# Restart llm-head only when nothing is in flight or queued, so no client request is cut
# off. Validates the config first, so a bad config never replaces a running process.
#
#   contrib/safe-restart.sh [unit] [config] [base-url] [max-wait-seconds]
set -euo pipefail
UNIT="${1:-llm-head}"
CONFIG="${2:-/opt/llm-head/config.yaml}"
BASE="${3:-http://127.0.0.1:40114}"
MAX_WAIT="${4:-120}"
BIN="${LLM_HEAD_BIN:-}"
for cand in "$BIN" "$(dirname "$0")/venv/bin/llm-head" /opt/llm-head/venv/bin/llm-head "$(command -v llm-head || true)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then BIN="$cand"; break; fi
done
[ -x "$BIN" ] || { echo "cannot find the llm-head binary; set LLM_HEAD_BIN" >&2; exit 1; }

"$BIN" check-config -c "$CONFIG"

for ((i = 0; i < MAX_WAIT; i++)); do
  busy=$(curl -fsS -m 5 "$BASE/internal/queue" | python3 -c '
import json, sys
q = json.load(sys.stdin)
print(len(q["waiting"]) + sum(sum(h["inflight"].values()) for h in q["hosts"].values()))')
  if [ "$busy" = 0 ]; then
    sudo systemctl restart "$UNIT"
    for ((j = 0; j < 30; j++)); do
      curl -fsS -m 2 "$BASE/internal/health" >/dev/null 2>&1 && { echo "restarted $UNIT (idle after ${i}s)"; exit 0; }
      sleep 1
    done
    echo "restarted $UNIT but it is not answering on $BASE" >&2
    exit 1
  fi
  sleep 1
done
echo "not restarted: still busy after ${MAX_WAIT}s" >&2
exit 1
