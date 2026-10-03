#!/usr/bin/env bash
# install.sh -- set up opencode <-> Claude Code session sync on this device.
#
#   ./install.sh              install (or re-install; safe to repeat)
#   ./install.sh --dry-run    print every change, touch nothing
#   ./install.sh --uninstall  remove what this script installed
#   ./install.sh --no-wrapper skip the opencode exit-sync wrapper
#
# Nothing here is device-specific. Every path is derived from $HOME, $XDG_*
# and what is actually found on PATH, so the same script works on a workstation,
# a laptop, WSL, or Termux. The only value baked into a file is the real
# opencode binary's path, and that is discovered rather than assumed -- the
# upstream wrapper defaults to /usr/bin/opencode, which does not exist on most
# installs and would make every opencode launch fail with exit 127.
set -euo pipefail

# Where is this script? Normally its own directory, which is where src/, bin/
# and hooks/ live. But `curl ... | bash` -- the documented one-liner -- has no
# file at all: BASH_SOURCE[0] is then "bash", or unset under `set -u`. Treat
# "not a readable file" as standalone and let bootstrap() fetch the rest.
SELF="${BASH_SOURCE[0]:-}"
REPO_DIR=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  REPO_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"
fi

usage() {
  cat <<'USAGE'
install.sh -- set up opencode <-> Claude Code session sync on this device.

    ./install.sh              install (or re-install; safe to repeat)
    ./install.sh --dry-run    print every change, touch nothing
    ./install.sh --uninstall  remove what this script installed
    ./install.sh --no-wrapper skip the opencode exit-sync wrapper

Also works when piped, with no clone and no arguments:

    curl -fsSL https://raw.githubusercontent.com/Suydev/opencode-claude-code-sync/main/install.sh | bash

Nothing here is device-specific. Every path is derived from $HOME, $XDG_* and
what is found on PATH, so the same script works on a workstation, a laptop, WSL
or Termux.
USAGE
}

HOME="${HOME:?HOME must be set}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

AISYNC_HOME="${AISYNC_HOME:-$XDG_DATA_HOME/opencode-claude-code-sync}"
OPENCODE_STATE_DIR="${OPENCODE_STATE_DIR:-$XDG_DATA_HOME/opencode}"

# First writable dir on PATH, else ~/.local/bin. Used for ai-sync and the
# wrapper so we never install somewhere the shell will not look.
BIN_DIR=""
IFS=: read -r -a _path_dirs <<<"$PATH"
for d in "${_path_dirs[@]}"; do
  [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ] && { BIN_DIR="$d"; break; }
done
[ -n "$BIN_DIR" ] || BIN_DIR="$HOME/.local/bin"

AI_SYNC="$BIN_DIR/ai-sync"
WRAPPER="$BIN_DIR/opencode"
CC_HOOK="$CLAUDE_CONFIG_DIR/hooks-sync.sh"
CC_SETTINGS="$CLAUDE_CONFIG_DIR/settings.json"
SHELL_RC="$HOME/.bashrc"

RC_BEGIN="# >>> opencode-claude-code-sync >>>"
RC_END="# <<< opencode-claude-code-sync <<<"

MODE=install
WANT_WRAPPER=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)   MODE=dry-run ;;
    --uninstall) MODE=uninstall ;;
    --no-wrapper) WANT_WRAPPER=0 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
# Both must always succeed: under `set -e` a helper that returns non-zero as a
# standalone statement aborts the script.
plan() {
  [ "$MODE" = dry-run ] && printf 'would %s\n' "$*"
  return 0
}
run() {
  [ "$MODE" = dry-run ] || "$@"
  return 0
}

# `install(1)` is coreutils and is absent on Git Bash, which is how the Windows
# leg of the test matrix runs this script. cp+chmod is equivalent here because
# these are all plain file copies into a directory we control.
# Print the installed module filenames, space separated. A glob loop rather
# than `ls | xargs basename`, which mangles anything unusual and trips
# Shellcheck's SC2011. (Capitalised deliberately: a comment starting with
# lowercase "shellcheck" is parsed as a directive.)
list_modules() {
  local f out=""
  for f in "$AISYNC_HOME"/*.py; do
    [ -e "$f" ] || continue
    out="$out ${f##*/}"
  done
  printf '%s' "${out# }"
}

install_file() {  # install_file <mode> <src> <dst>
  if command -v install >/dev/null 2>&1; then
    install -m"$1" "$2" "$3"
  else
    mkdir -p "$(dirname -- "$3")"
    cp "$2" "$3"
    chmod "$1" "$3"
  fi
}

# --- bootstrap -------------------------------------------------------------
# Support two shapes of invocation:
#
#   git clone ... && ./install.sh     -> everything is already on disk
#   curl -fsSL <raw>/install.sh|bash  -> only this one file exists
#
# In the second case the sibling files (the three modules, ai-sync, the hooks)
# have to be fetched. They land in a cache dir keyed on REF, so a second run
# reuses them instead of re-downloading, and an interrupted run can be
# inspected instead of being a mystery.
REPO_URL="${AISYNC_REPO_URL:-https://github.com/Suydev/opencode-claude-code-sync}"
REF="${AISYNC_REF:-main}"
BOOTSTRAP_DIR="$AISYNC_HOME/bootstrap-$REF"
# Owner-qualified path, taken from the repo URL so a fork keeps working:
# https://github.com/Suydev/name -> Suydev/name. Stripping to the basename
# instead would silently drop the owner and 404 on every fetch.
REPO_SLUG="${REPO_URL#*github.com/}"
REPO_SLUG="${REPO_SLUG%.git}"
REPO_SLUG="${REPO_SLUG%/}"
# Host + path, without the ref. Overridable so forks, mirrors, and the test
# suite can point somewhere else; the ref is always appended.
RAW_BASE="${AISYNC_RAW_BASE:-https://raw.githubusercontent.com/$REPO_SLUG}"
RAW="$RAW_BASE/$REF"

have_tree() {
  [ -n "$REPO_DIR" ] && [ -f "$REPO_DIR/bin/ai-sync" ] && [ -f "$REPO_DIR/src/aisync.py" ]
}

fetch_to() {
  # fetch_to <url> <dest>; fetch_to - <dest> reads the script's own stdin.
  local url="$1" dest="$2" tmp
  mkdir -p "$(dirname -- "$dest")"
  tmp="$dest.part.$$"
  if [ "$url" = "-" ]; then
    cat >"$tmp"
  else
    if command -v curl >/dev/null; then
      curl -fsSL "$url" -o "$tmp" || { rm -f "$tmp"; return 1; }
    elif command -v wget >/dev/null; then
      wget -qO "$tmp" "$url" || { rm -f "$tmp"; return 1; }
    else
      say "install.sh: need curl or wget to fetch $url" >&2
      return 1
    fi
  fi
  # A proxy or captive portal can answer 200 with HTML. Refuse to install that.
  if ! head -c 200 "$tmp" | grep -qE '^#!.*(ba)?sh|^\{|^import |^#|^<'; then
    say "install.sh: refusing to install $dest -- response does not look like a script" >&2
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$dest"
}

bootstrap() {
  have_tree && return 0
  if [ -f "$BOOTSTRAP_DIR/bin/ai-sync" ] && [ -f "$BOOTSTRAP_DIR/src/aisync.py" ]; then
    REPO_DIR="$BOOTSTRAP_DIR"
    say "reusing cached copy at $REPO_DIR"
    return 0
  fi
  plan "fetch project files from $RAW"
  if [ "$MODE" = dry-run ]; then
    REPO_DIR="$BOOTSTRAP_DIR"
    return 0
  fi
  say "fetching project files from $RAW"
  local f
  for f in src/aisync.py src/cc2oc.py src/oc2cc.py bin/ai-sync \
           hooks/claude-session-end.sh hooks/opencode-wrapper.sh; do
    if [ -n "$REPO_DIR" ] && [ -f "$REPO_DIR/$f" ]; then
      cp "$REPO_DIR/$f" "$BOOTSTRAP_DIR/$f"
      continue
    fi
    fetch_to "$RAW/$f" "$BOOTSTRAP_DIR/$f" || {
      say "install.sh: could not fetch $f" >&2
      say "  try: git clone --depth 1 \"$REPO_URL\" && cd \"${REPO_URL##*/}\" && ./install.sh" >&2
      exit 1
    }
  done
  chmod 755 "$BOOTSTRAP_DIR/bin/ai-sync" "$BOOTSTRAP_DIR/hooks/"*.sh 2>/dev/null || true
  REPO_DIR="$BOOTSTRAP_DIR"
}

bootstrap

# Locate the real opencode: an executable named opencode on PATH that is not
# this script's own wrapper. Returns 1 if none is found.
REAL_OPENCODE=""
detect_real_opencode() {
  local candidate resolved
  REAL_OPENCODE="${OPENCODE_BIN:-}"
  if [ -n "$REAL_OPENCODE" ] && [ -x "$REAL_OPENCODE" ]; then
    return 0
  fi
  for candidate in "${_path_dirs[@]}"; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate/opencode" ] || continue
    resolved="$(cd -- "$(dirname -- "$candidate/opencode")" && pwd -P)/$(basename -- "$candidate/opencode")"
    # Skip our own wrapper, and skip anything that is a symlink back to it.
    [ "$resolved" = "$WRAPPER" ] && continue
    [ "$(readlink -f -- "$candidate/opencode" 2>/dev/null || echo "$candidate/opencode")" = "$WRAPPER" ] && continue
    grep -q "opencode-wrapper" "$candidate/opencode" 2>/dev/null && continue
    REAL_OPENCODE="$resolved"
    return 0
  done
  return 1
}

is_our_wrapper() {
  [ -f "$WRAPPER" ] && grep -q "opencode-wrapper\|opencode exit hook" "$WRAPPER" 2>/dev/null
}

# Merge a SessionEnd hook into Claude Code's settings.json without disturbing
# anything already in it. Idempotent: re-running replaces our entry instead of
# appending a second one.
register_cc_hook() {
  [ -f "$CC_SETTINGS" ] || { printf '{}\n' >"$CC_SETTINGS"; }
  python3 - "$CC_SETTINGS" "$CC_HOOK" <<'PY'
import json, sys
path, hook = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        cfg = json.load(fh)
except (OSError, ValueError):
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}
hooks = cfg.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}
    cfg["hooks"] = hooks
sessend = [e for e in (hooks.get("SessionEnd") or [])
           if not (isinstance(e, dict) and any(
               isinstance(h, dict) and hook in json.dumps(h) for h in (e.get("hooks") or [])))]
sessend.insert(0, {"hooks": [{"type": "command", "command": hook, "timeout": 10}]})
hooks["SessionEnd"] = sessend
# Claude Code prunes transcripts older than this (default 30 days). Imported
# sessions are backdated to the original conversation time, so an older import
# is already past the cutoff and gets swept on next launch.
if not isinstance(cfg.get("cleanupPeriodDays"), int) or cfg["cleanupPeriodDays"] < 3650:
    cfg["cleanupPeriodDays"] = 3650
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print("registered SessionEnd hook + cleanupPeriodDays in", path)
PY
}

unregister_cc_hook() {
  [ -f "$CC_SETTINGS" ] || return 0
  python3 - "$CC_SETTINGS" "$CC_HOOK" <<'PY'
import json, sys
path, hook = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        cfg = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
hooks = cfg.get("hooks") or {}
sessend = hooks.get("SessionEnd") or []
kept = [e for e in sessend
        if not (isinstance(e, dict) and any(
            isinstance(h, dict) and hook in json.dumps(h) for h in (e.get("hooks") or [])))]
if kept:
    hooks["SessionEnd"] = kept
else:
    hooks.pop("SessionEnd", None)
if not hooks:
    cfg.pop("hooks", None)
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print("removed SessionEnd hook from", path)
PY
}

# --- shell rc: only the marker block, removed cleanly on uninstall -----------
rc_has_block() { [ -f "$SHELL_RC" ] && grep -qF "$RC_BEGIN" "$SHELL_RC"; }

write_rc_block() {
  if [ "$MODE" = dry-run ]; then
    plan "write OPENCODE_BIN block to $SHELL_RC"
    return 0
  fi
  [ -f "$SHELL_RC" ] || : >"$SHELL_RC"
  if rc_has_block; then
    say "refreshing OPENCODE_BIN block in $SHELL_RC"
    python3 - "$SHELL_RC" "$RC_BEGIN" "$RC_END" <<'PY'
import re, sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
text = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)
open(path, "w").write(text.rstrip("\n") + "\n")
PY
  fi
  say "" >>"$SHELL_RC"
  {
    say "$RC_BEGIN"
    say "# Real opencode binary, for the exit-sync wrapper in $WRAPPER."
    say "# Without this the wrapper cannot find the binary it wraps."
    say "export OPENCODE_BIN=\"$REAL_OPENCODE\""
    say "$RC_END"
  } >>"$SHELL_RC"
  say "exported OPENCODE_BIN in $SHELL_RC"
}

drop_rc_block() {
  rc_has_block || return 0
  plan "remove OPENCODE_BIN block from $SHELL_RC"
  [ "$MODE" = dry-run ] && return 0
  python3 - "$SHELL_RC" "$RC_BEGIN" "$RC_END" <<'PY'
import re, sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
text = re.sub(r"\n?" + re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "\n", text, flags=re.S)
open(path, "w").write(text.rstrip("\n") + "\n")
PY
  say "removed OPENCODE_BIN block from $SHELL_RC"
}

if [ "$MODE" = uninstall ]; then
  say "uninstalling opencode-claude-code-sync"
  if is_our_wrapper; then
    plan "remove wrapper $WRAPPER"
    [ "$MODE" = dry-run ] || rm -f "$WRAPPER"
    say "removed wrapper $WRAPPER"
  fi
  plan "remove $AI_SYNC"
  [ "$MODE" = dry-run ] || rm -f "$AI_SYNC"
  say "removed $AI_SYNC"
  plan "remove $CC_HOOK"
  [ "$MODE" = dry-run ] || rm -f "$CC_HOOK"
  say "removed $CC_HOOK"
  [ "$MODE" = dry-run ] || unregister_cc_hook
  [ "$MODE" = dry-run ] || drop_rc_block
  say "left $AISYNC_HOME in place (your ledger and session data are still there)"
  exit 0
fi

command -v python3 >/dev/null || { say "install.sh: python3 is required" >&2; exit 1; }
command -v claude  >/dev/null || say "warning: claude not on PATH; reverse sync will have nothing to read"
detect_real_opencode || say "warning: no real opencode found on PATH; set OPENCODE_BIN before using the wrapper"

say "installing opencode-claude-code-sync"
say "  repo:        $REPO_DIR"
say "  modules:     $AISYNC_HOME"
say "  bin dir:     $BIN_DIR"
say "  claude dir:  $CLAUDE_CONFIG_DIR"
say "  real opencode: ${REAL_OPENCODE:-<not found, wrapper disabled>}"

plan "create $AISYNC_HOME"
run mkdir -p "$AISYNC_HOME"
plan "copy src/*.py -> $AISYNC_HOME"
run cp "$REPO_DIR"/src/*.py "$AISYNC_HOME"/
if [ "$MODE" = dry-run ]; then
  say "modules present: $(list_modules)"
else
  say "installed modules: $(list_modules)"
fi

plan "install ai-sync -> $AI_SYNC"
run install_file 755 "$REPO_DIR/bin/ai-sync" "$AI_SYNC"
say "$([ "$MODE" = dry-run ] && echo 'ai-sync would be at' || echo 'installed') $AI_SYNC"

if [ "$WANT_WRAPPER" = 1 ] && [ -n "$REAL_OPENCODE" ]; then
  plan "install exit-sync wrapper -> $WRAPPER (real binary: $REAL_OPENCODE)"
  if [ "$MODE" = dry-run ]; then
    # The redirect has to be inside the guard: `sed > file` truncates before
    # run() ever gets a say, which would leave an empty wrapper behind on a
    # dry run.
    say "wrapper would be at $WRAPPER"
  else
    sed "s|REAL_OPENCODE=\"\${OPENCODE_BIN:-[^}]*}\"|REAL_OPENCODE=\"\${OPENCODE_BIN:-$REAL_OPENCODE}\"|" \
      "$REPO_DIR/hooks/opencode-wrapper.sh" >"$WRAPPER"
    chmod 755 "$WRAPPER"
    say "installed wrapper $WRAPPER"
  fi
  write_rc_block
  if [ "$BIN_DIR" != "$(dirname -- "$(command -v opencode 2>/dev/null || echo /nonexistent)")" ]; then
    say "note: $BIN_DIR precedes the real opencode on PATH, so the wrapper wins. That is intended."
  fi
else
  say "skipping wrapper (--no-wrapper or no real opencode found); run $AI_SYNC by hand"
fi

plan "install Claude Code SessionEnd hook -> $CC_HOOK"
# A device that has never run Claude Code has no ~/.claude at all, and
# `install` refuses to create the parent directory itself.
plan "create $CLAUDE_CONFIG_DIR"
run mkdir -p "$CLAUDE_CONFIG_DIR"
run install_file 755 "$REPO_DIR/hooks/claude-session-end.sh" "$CC_HOOK"
say "$([ "$MODE" = dry-run ] && echo 'hook would be at' || echo 'installed') $CC_HOOK"

plan "merge hook into $CC_SETTINGS"
if [ "$MODE" = dry-run ]; then
  say "would register SessionEnd hook and set cleanupPeriodDays=3650 in $CC_SETTINGS"
else
  # register_cc_hook drops any previous entry of ours before inserting, so this
  # is a single pass on purpose. Unregistering first and then registering also
  # works, but it deletes the "hooks" key and re-adds it at the end, which
  # reorders the file on every run for no benefit.
  register_cc_hook
fi

say ""
say "done. next:"
say "  ai-sync --dry-run   see what it would do, change nothing"
say "  ai-sync             sync now, then open Claude Code's resume picker"
say "  ai-sync --status    per-session state and anything on hold"
exit 0
