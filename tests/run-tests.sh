#!/usr/bin/env bash
# tests/run-tests.sh -- the suite CI runs on every OS in the matrix.
#
# Deliberately hermetic: every test builds a throwaway HOME, so nothing here
# reads or writes the developer's real ~/.claude or opencode database. That is
# also what makes it safe to run on a machine that has both tools installed for
# real, which is the normal case for anyone hacking on this.
#
#   tests/run-tests.sh              run everything
#   tests/run-tests.sh -v           also echo each command
#
# Exits non-zero on the first failure. POSIX-ish bash, no bashisms beyond
# arrays and [[ ]] so it runs under Git Bash on Windows unchanged.
set -uo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

PASS=0
FAIL=0
TMPROOT="$(mktemp -d 2>/dev/null || mktemp -d -t aisync)"
trap 'rm -rf "$TMPROOT"' EXIT

ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# assert_eq <label> <expected> <actual>
assert_eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$2] got [$3]"; fi
}
assert_ne() {
  if [ "$2" != "$3" ]; then ok "$1"; else bad "$1" "did not expect [$2]"; fi
}
assert_file() {
  if [ -f "$2" ]; then ok "$1"; else bad "$1" "missing file: $2"; fi
}
assert_dir() {
  if [ -d "$2" ]; then ok "$1"; else bad "$1" "missing dir: $2"; fi
}
assert_absent() {
  if [ ! -e "$2" ]; then ok "$1"; else bad "$1" "should not exist: $2"; fi
}
# assert_grep <label> <pattern> <file>
assert_grep() {
  if grep -qE "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1" "no /$2/ in $3"; fi
}
assert_nogrep() {
  if grep -qE "$2" "$3" 2>/dev/null; then bad "$1" "found /$2/ in $3"; else ok "$1"; fi
}
# contains <label> <haystack> <pattern> -- for captured output, no file needed
contains() {
  if printf '%s' "$2" | grep -qE "$3"; then ok "$1"; else bad "$1" "no /$3/ in: $(printf '%s' "$2" | head -3 | tr '\n' ' ')"; fi
}
lacks() {
  if printf '%s' "$2" | grep -qE "$3"; then bad "$1" "found /$3/ in: $(printf '%s' "$2" | head -3 | tr '\n' ' ')"; else ok "$1"; fi
}

run() {
  if [ "$VERBOSE" = 1 ]; then printf '       $ %s\n' "$*"; fi
  "$@" 2>>"$TMPROOT/stderr.log"
}

# A fake HOME with just enough of a device on it to be detected as one.
# Echoes the path; caller passes it as HOME with a PATH containing the stubs.
make_device() {
  local home="$TMPROOT/$1"
  mkdir -p "$home/.local/bin" "$home/.opencode/bin" "$home/bin"
  printf '#!/bin/sh\necho "stub opencode 0.0.0"\n' >"$home/.opencode/bin/opencode"
  printf '#!/bin/sh\necho "stub claude 0.0.0"\n'  >"$home/.local/bin/claude"
  chmod 755 "$home/.opencode/bin/opencode" "$home/.local/bin/claude"
  printf '%s' "$home"
}

dev_run() {
  # dev_run <home> <script> [args...] -- run installer in a hermetic env.
  #
  # Overrides HOME, PATH and the repo URL rather than using `env -i`. A scrubbed
  # environment is tidier in principle but breaks on Windows: Python cannot
  # start without SYSTEMROOT, so `env -i` made every installer invocation exit
  # 1 there for a reason that had nothing to do with the installer. The
  # variables that could leak a real config in are unset explicitly instead.
  local home="$1"; shift
  env -u CLAUDE_CONFIG_DIR -u XDG_DATA_HOME -u XDG_CONFIG_HOME \
      -u AISYNC_HOME -u OPENCODE_DB -u OPENCODE_STATE_DIR -u OPENCODE_BIN \
      HOME="$home" \
      PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
      AISYNC_REPO_URL="https://example.invalid/opencode-claude-code-sync" \
      "$@"
}

# ---------------------------------------------------------------------------
head_ "static checks"

if command -v shellcheck >/dev/null; then
  if shellcheck -S warning "$REPO_DIR/install.sh" "$REPO_DIR/bin/ai-sync" \
       "$REPO_DIR/hooks/claude-session-end.sh" "$REPO_DIR/hooks/opencode-wrapper.sh" \
       2>"$TMPROOT/shellcheck.log"; then
    ok "shellcheck (warning and above)"
  else
    bad "shellcheck" "$(head -20 "$TMPROOT/shellcheck.log" | tr '\n' ' ')"
  fi
else
  printf '  \033[33mskip\033[0m shellcheck not installed\n'
fi

for f in "$REPO_DIR"/install.sh "$REPO_DIR"/bin/ai-sync "$REPO_DIR"/hooks/*.sh; do
  if bash -n "$f" 2>/dev/null; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f")"; fi
done

for f in "$REPO_DIR"/src/*.py; do
  if python3 -m py_compile "$f" 2>"$TMPROOT/py.log"; then
    ok "py_compile $(basename "$f")"
  else
    bad "py_compile $(basename "$f")" "$(head -3 "$TMPROOT/py.log" | tr '\n' ' ')"
  fi
done

# ---------------------------------------------------------------------------
head_ "portability: no baked-in device paths"

# The whole point of the installer is that it works on a device it has never
# seen. A stray /root or /home/ali in a shell script is the failure that only
# shows up on someone else's laptop.
scan() {
  local hits
  hits="$(grep -nE "$2" "$@" 2>/dev/null | grep -vE '(Termux|README|docs/|#.*example)' || true)"
  if [ -z "$hits" ]; then ok "$1"; else bad "$1" "$(echo "$hits" | head -3 | tr '\n' ' ')"; fi
}
scan "no /root/ literal in scripts" '/root/' \
  "$REPO_DIR/install.sh" "$REPO_DIR/bin/ai-sync" "$REPO_DIR/hooks/"*.sh
scan "no /home/<user> literal in scripts" '/home/[a-z]' \
  "$REPO_DIR/install.sh" "$REPO_DIR/bin/ai-sync" "$REPO_DIR/hooks/"*.sh
# Note: hooks/opencode-wrapper.sh is *expected* to ship /usr/bin/opencode as
# its fallback, because that is the bug the installer exists to patch on
# devices where opencode lives elsewhere. The patched output is asserted below
# against the file the installer actually writes.

# Python must not hardcode an absolute store path either.
#
# The invariant is "derived from this device's home", which cannot be checked by
# looking for a foreign home: on a box where HOME is /root, a correct derived
# path and a hardcoded one are indistinguishable. So point home at a sentinel
# and require every resolved store path to contain it. USERPROFILE is set too
# because ntpath.expanduser consults that before HOME on Windows.
sentinel="$TMPROOT/sentinel-home"
mkdir -p "$sentinel"
py_paths="$(cd "$REPO_DIR" && HOME="$sentinel" USERPROFILE="$sentinel" python3 -c "
import sys; sys.path.insert(0, 'src')
import aisync, oc2cc, cc2oc
for p in (aisync.STATE_DIR, oc2cc.DB, cc2oc.LEDGER):
    print(p.replace(chr(92), '/'))
" 2>&1)"

if printf '%s' "$py_paths" | grep -qiE "Traceback|Error"; then
  bad "python modules import" "$(printf '%s' "$py_paths" | head -3 | tr '\n' ' ')"
else
  ok "python modules import"
  offhome="$(printf '%s\n' "$py_paths" | grep -vF "sentinel-home" || true)"
  if [ -z "$offhome" ]; then
    ok "python store paths derive from this device's home"
  else
    bad "python store paths derive from this device's home" "$(printf '%s' "$offhome" | tr '\n' ' ')"
  fi
  tails="$(printf '%s\n' "$py_paths" | grep -cE "(opencode|opencode\.db|cc2oc-ledger\.json)$")"
  assert_eq "all three store paths have the expected tail" "3" "$tails"
fi

# XDG_DATA_HOME must be honoured, and OPENCODE_DB must win over it. Asserted by
# substring because Windows resolves a leading-slash override against the Git
# root (C:/Program Files/Git/xdgtest), which is correct behaviour, not a bug.
xdg="$(cd "$REPO_DIR" && XDG_DATA_HOME=/xdgtest python3 -c "
import sys; sys.path.insert(0,'src'); import aisync
print(aisync.STATE_DIR.replace(chr(92), '/'))" 2>/dev/null)"
contains "XDG_DATA_HOME honoured" "$xdg" "xdgtest(/|\\\\)opencode$"

dbovr="$(cd "$REPO_DIR" && XDG_DATA_HOME=/xdgtest OPENCODE_DB=/explicit/oc.db python3 -c "
import sys; sys.path.insert(0,'src'); import oc2cc
print(oc2cc.DB.replace(chr(92), '/'))" 2>/dev/null)"
contains "OPENCODE_DB overrides XDG" "$dbovr" "explicit(/|\\\\)oc\.db$"
lacks    "OPENCODE_DB does not fall back to XDG" "$dbovr" "xdgtest"

# ---------------------------------------------------------------------------
head_ "installer: dry run changes nothing"

home="$(make_device dryrun)"
before="$(find "$home" -type f | sort)"
out="$(dev_run "$home" bash "$REPO_DIR/install.sh" --dry-run 2>&1)"
rc=$?
assert_eq "dry run exits 0" "0" "$rc"
assert_eq "dry run leaves the device untouched" "$before" "$(find "$home" -type f | sort)"
if printf '%s' "$out" | grep -q "would install"; then ok "dry run reports intended changes"
else bad "dry run reports intended changes" "$(printf '%s' "$out" | head -3 | tr '\n' ' ')"; fi

# ---------------------------------------------------------------------------
head_ "installer: fresh device, no ~/.claude"

home="$(make_device fresh)"
dev_run "$home" bash "$REPO_DIR/install.sh" >"$TMPROOT/fresh.log" 2>&1
rc=$?
assert_eq "install on a device with no ~/.claude exits 0" "0" "$rc"
assert_file "ai-sync installed"        "$home/.local/bin/ai-sync"
assert_file "wrapper installed"        "$home/.local/bin/opencode"
assert_dir  "claude dir created"       "$home/.claude"
assert_file "claude hook installed"    "$home/.claude/hooks-sync.sh"
assert_file "claude settings written"  "$home/.claude/settings.json"
assert_file "modules installed"        "$home/.local/share/opencode-claude-code-sync/aisync.py"
assert_file "cc2oc module installed"   "$home/.local/share/opencode-claude-code-sync/cc2oc.py"
assert_file "oc2cc module installed"   "$home/.local/share/opencode-claude-code-sync/oc2cc.py"
assert_file "bashrc written"           "$home/.bashrc"
assert_eq   "wrapper is executable"    "yes" "$([ -x "$home/.local/bin/opencode" ] && echo yes || echo no)"
assert_grep "bashrc exports OPENCODE_BIN" "export OPENCODE_BIN=" "$home/.bashrc"
assert_grep "wrapper points at the real binary" "$home/.opencode/bin/opencode" "$home/.local/bin/opencode"
assert_nogrep "wrapper does not assume /usr/bin/opencode" "/usr/bin/opencode" "$home/.local/bin/opencode"

# The wrapper must shadow the stub only because it is earlier on PATH, and it
# must still be able to run the binary it wraps.
wrapped="$("$home/.local/bin/opencode" --version 2>&1 | tr -d '\r')"
assert_eq "wrapper runs the real binary" "stub opencode 0.0.0" "$wrapped"

# ---------------------------------------------------------------------------
head_ "installer: settings.json is merged, not clobbered"

home="$(make_device merge)"
mkdir -p "$home/.claude"
cat >"$home/.claude/settings.json" <<'JSON'
{
  "env": {"MY_TOKEN": "keep-me"},
  "model": "some-model",
  "theme": "dark"
}
JSON
dev_run "$home" bash "$REPO_DIR/install.sh" >/dev/null 2>&1
kept="$(python3 -c "
import json,sys
s=json.load(open(sys.argv[1]))
print(s.get('env',{}).get('MY_TOKEN'), s.get('model'), s.get('theme'))" "$home/.claude/settings.json" 2>/dev/null)"
assert_eq "existing settings preserved" "keep-me some-model dark" "$kept"
clean="$(python3 -c "
import json,sys
s=json.load(open(sys.argv[1]))
print(s.get('cleanupPeriodDays'))" "$home/.claude/settings.json" 2>/dev/null)"
assert_eq "cleanupPeriodDays raised to 3650" "3650" "$clean"

# ---------------------------------------------------------------------------
head_ "installer: idempotency"

home="$(make_device idem)"
dev_run "$home" bash "$REPO_DIR/install.sh" >/dev/null 2>&1
first="$(cat "$home/.claude/settings.json")"
dev_run "$home" bash "$REPO_DIR/install.sh" >/dev/null 2>&1
dev_run "$home" bash "$REPO_DIR/install.sh" >/dev/null 2>&1
assert_eq "settings.json byte-identical after 3 installs" "$first" "$(cat "$home/.claude/settings.json")"
hooks_n="$(python3 -c "
import json,sys
print(len(json.load(open(sys.argv[1]))['hooks']['SessionEnd']))" "$home/.claude/settings.json" 2>/dev/null)"
assert_eq "exactly one SessionEnd hook" "1" "$hooks_n"
blocks="$(grep -c 'opencode-claude-code-sync >>>' "$home/.bashrc" 2>/dev/null || echo 0)"
assert_eq "exactly one bashrc marker block" "1" "$blocks"

# ---------------------------------------------------------------------------
head_ "installer: uninstall"

home="$(make_device uninst)"
dev_run "$home" bash "$REPO_DIR/install.sh" >/dev/null 2>&1
dev_run "$home" bash "$REPO_DIR/install.sh" --uninstall >/dev/null 2>&1
rc=$?
assert_eq "uninstall exits 0" "0" "$rc"
assert_absent "wrapper removed"    "$home/.local/bin/opencode"
assert_absent "ai-sync removed"   "$home/.local/bin/ai-sync"
assert_absent "claude hook removed" "$home/.claude/hooks-sync.sh"
# The rc file itself must survive -- it belongs to the user, not to us. Only
# our marked block comes out.
assert_nogrep "OPENCODE_BIN block removed from bashrc" "OPENCODE_BIN" "$home/.bashrc"
left="$(python3 -c "
import json,sys
s=json.load(open(sys.argv[1]))
print('hooks' in s)" "$home/.claude/settings.json" 2>/dev/null)"
assert_eq "hooks key gone from settings.json" "False" "$left"

# Uninstall must not touch the ledger: that is session data, not config.
assert_dir "module dir left in place" "$home/.local/share/opencode-claude-code-sync"

# ---------------------------------------------------------------------------
head_ "installer: bootstrap fetch path"

# Standalone invocation: only install.sh exists, the rest is fetched over HTTP.
# Served locally so the test needs no network and cannot hit the real repo.
home="$(make_device fetch)"
serve="$TMPROOT/raw"
mkdir -p "$serve/main"
cp -r "$REPO_DIR/src" "$REPO_DIR/bin" "$REPO_DIR/hooks" "$serve/main/"
cp "$REPO_DIR/install.sh" "$serve/main/install.sh"
port=8799
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$serve" >"$TMPROOT/http.log" 2>&1 &
http_pid=$!
# Wait for it rather than sleeping a fixed amount.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS "http://127.0.0.1:$port/main/install.sh" -o /dev/null 2>/dev/null; then break; fi
  sleep 0.3
done
kill "$http_pid" 2>/dev/null; wait "$http_pid" 2>/dev/null

if curl -fsS "http://127.0.0.1:$port/main/install.sh" -o /dev/null 2>/dev/null; then
  printf '  \033[33mskip\033[0m bootstrap fetch (no curl on this runner)\n'
else
  # Serve for the duration of the two runs.
  python3 -m http.server "$port" --bind 127.0.0.1 --directory "$serve" >"$TMPROOT/http.log" 2>&1 &
  http_pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS "http://127.0.0.1:$port/main/install.sh" -o /dev/null 2>/dev/null && break
    sleep 0.3
  done
  standalone="$TMPROOT/standalone"
  mkdir -p "$standalone"
  cp "$REPO_DIR/install.sh" "$standalone/"          # ONLY install.sh
  env -i HOME="$home" \
      PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
      AISYNC_RAW_BASE="http://127.0.0.1:$port" \
      bash "$standalone/install.sh" >"$TMPROOT/fetch.log" 2>&1
  rc=$?
  kill "$http_pid" 2>/dev/null; wait "$http_pid" 2>/dev/null
  assert_eq "standalone curl-style install exits 0" "0" "$rc"
  assert_file "modules fetched over HTTP" "$home/.local/share/opencode-claude-code-sync/aisync.py"
  assert_file "ai-sync fetched over HTTP" "$home/.local/bin/ai-sync"
  assert_grep "fetch reported in output" "fetching project files" "$TMPROOT/fetch.log"

  # Second standalone run must reuse the cache, not refetch.
  python3 -m http.server "$port" --bind 127.0.0.1 --directory "$serve" >"$TMPROOT/http2.log" 2>&1 &
  http_pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS "http://127.0.0.1:$port/main/install.sh" -o /dev/null 2>/dev/null && break
    sleep 0.3
  done
  env -i HOME="$home" \
      PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
      AISYNC_RAW_BASE="http://127.0.0.1:$port" \
      bash "$standalone/install.sh" >"$TMPROOT/fetch2.log" 2>&1
  kill "$http_pid" 2>/dev/null; wait "$http_pid" 2>/dev/null
  assert_grep "second standalone run reuses cache" "reusing cached copy" "$TMPROOT/fetch2.log"
fi

# ---------------------------------------------------------------------------
head_ "piped invocation (curl | bash)"

# The documented one-liner pipes this script into bash, where it is not a file.
# BASH_SOURCE[0] is then "bash", so anything deriving the repo directory from it
# dies under `set -u` -- which is how this was shipped broken until the published
# URL was actually curled. Run from a directory with no src/ so the standalone
# path is the one exercised.
home="$(make_device piped)"
# Port 1 is not listening, so the fetch cannot succeed. That is the point: it
# proves the script got all the way into bootstrap() and failed for the honest
# reason, rather than dying on line 17 before it started.
piped="$(cd "$TMPROOT" && cat "$REPO_DIR/install.sh" | \
  env -i HOME="$home" PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
  AISYNC_RAW_BASE="http://127.0.0.1:1" bash 2>&1)"
piped_rc=$?
lacks   "piped run has no unbound-variable error" "$piped" "unbound variable"
contains "piped run reaches the bootstrap fetch"    "$piped" "fetching project files"
contains "piped run fails with a usable message"    "$piped" "could not fetch|git clone"
assert_ne "piped run exit code is non-zero" "0" "$piped_rc"

# The default raw URL must carry the owner. Getting this wrong 404s every fetch
# while every local test still passes, because they all override the host.
slug="$(cd "$TMPROOT" && cat "$REPO_DIR/install.sh" | \
  env -i HOME="$home" PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
  bash -s -- --dry-run 2>&1)"
contains "default raw URL is owner-qualified" "$slug" "raw\.githubusercontent\.com/Suydev/opencode-claude-code-sync/main"
lacks   "fetch log does not repeat the ref"    "$slug" "main/main"

# --dry-run short-circuits before any fetch, so it must succeed outright.
piped_dry="$(cd "$TMPROOT" && cat "$REPO_DIR/install.sh" | \
  env -i HOME="$home" PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" \
  bash -s -- --dry-run 2>&1)"
dry_rc=$?
assert_eq "piped --dry-run exits 0" "0" "$dry_rc"
contains "piped --dry-run reports intended changes" "$piped_dry" "would install"
lacks   "piped --dry-run writes nothing" "$piped_dry" "installed wrapper|exported OPENCODE_BIN"

# Same again for --help, which used to sed its own $0 and so printed nothing.
piped_help="$(cd "$TMPROOT" && cat "$REPO_DIR/install.sh" | \
  env -i HOME="$home" PATH="$home/.local/bin:$home/.opencode/bin:/usr/bin:/bin" bash -s -- --help 2>&1)"
contains "piped --help prints usage" "$piped_help" "install.sh -- set up"

# ---------------------------------------------------------------------------
head_ "cli surface"

for flag in --help --status --dry-run --quiet; do
  if [ "$flag" = "--help" ]; then
    if bash "$REPO_DIR/bin/ai-sync" --help >/dev/null 2>&1; then ok "ai-sync --help"
    else bad "ai-sync --help"; fi
  else
    if AISYNC_HOME="$TMPROOT/no-such-home" bash "$REPO_DIR/bin/ai-sync" "$flag" >/dev/null 2>&1; then
      ok "ai-sync $flag (missing install degrades, not crashes)"
    else
      rc=$?
      if [ "$rc" = 1 ]; then ok "ai-sync $flag exits 1 with a clear message"
      else bad "ai-sync $flag" "unexpected rc=$rc"; fi
    fi
  fi
done

if bash "$REPO_DIR/bin/ai-sync" --bogus-flag >/dev/null 2>&1; then
  bad "ai-sync rejects unknown flags"
else
  ok "ai-sync rejects unknown flags"
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
