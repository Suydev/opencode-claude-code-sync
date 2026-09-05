#!/usr/bin/env bash
# SessionEnd hook: mirror the finished Claude Code session into opencode.
#
# Claude Code feeds hook input as JSON on stdin. Only session_id is needed --
# ai-sync reads the transcript itself from the projects tree.
#
# Detached and silent: a SessionEnd hook runs during shutdown under a timeout,
# so blocking on a sync would delay exit or get killed halfway. Output goes to
# ai-sync.log, never to the terminal.
set -u
payload=$(cat 2>/dev/null || true)
sid=$(printf '%s' "$payload" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('session_id',''))
except Exception: print('')
" 2>/dev/null)
[ -n "$sid" ] || exit 0
setsid "${AI_SYNC_BIN:-$HOME/.local/bin/ai-sync}" --one-cc "$sid" >/dev/null 2>&1 &
exit 0
