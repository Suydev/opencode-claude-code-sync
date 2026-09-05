#!/usr/bin/env bash
# opencode exit hook: mirror the finished session into Claude Code.
#
# opencode 1.17.9 does not load local plugin files (see
# docs/opencode-database-format.md §11), so there is no usable in-process exit
# hook. Wrapping the binary is the alternative, and it is arguably the better
# place: it also fires when opencode crashes or is killed by an OOM reaper,
# which an in-process hook would miss.
#
# Install by putting this earlier on PATH than the real opencode, e.g. as
# ~/.local/bin/opencode, and pointing OPENCODE_BIN at the real one.
#
#   OPENCODE_NO_SYNC=1 opencode ...   runs without the exit sync
set -u

REAL_OPENCODE="${OPENCODE_BIN:-/usr/bin/opencode}"
AI_SYNC="${AI_SYNC_BIN:-$HOME/.local/bin/ai-sync}"

if [ ! -x "$REAL_OPENCODE" ]; then
  echo "opencode-wrapper: OPENCODE_BIN does not point at an executable: $REAL_OPENCODE" >&2
  exit 127
fi

if [ "${OPENCODE_NO_SYNC:-0}" = "1" ] || [ ! -x "$AI_SYNC" ]; then
  exec "$REAL_OPENCODE" "$@"
fi

# Subcommands that create no session worth syncing, or that are long-lived
# services which will sync on their own exit anyway.
case " $* " in
  *" export "*|*" import "*|*" models "*|*" stats "*|*" auth "*|*" providers "*|\
  *" upgrade "*|*" uninstall "*|*" completion "*|*" mcp "*|*" plugin "*|*" db "*)
    exec "$REAL_OPENCODE" "$@"
    ;;
esac

# Foreground, so the sync sees the session's final state; then detach, so
# quitting opencode returns the prompt immediately.
"$REAL_OPENCODE" "$@"
rc=$?
setsid "$AI_SYNC" --quiet >/dev/null 2>&1 &
exit "$rc"
