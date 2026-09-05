#!/usr/bin/env python3
"""oc2cc v3 -- import opencode sessions into Claude Code's transcript store.

Writes real Claude Code transcripts, so every opencode session shows up in
`claude --resume` / `/resume` in the right project, with its opencode title,
and can actually be resumed.

Why v3 exists (v1/v2 got these wrong):

  * project directory encoding. Claude Code maps a cwd to a folder name with
    `cwd.replace(/[^a-zA-Z0-9]/g, "-")`, so the DOT in "com.termux" becomes a
    dash: `-data-data-com-termux-files-home`. v1 only replaced "/", producing
    `-data-data-com.termux-files-home`, a folder Claude Code never reads for
    this cwd. That alone is why the old imports never appeared.
  * timestamps. v1 stamped every entry with "now", so the picker sorted and
    dated them all identically. v3 uses the real opencode millis.
  * tool calls. v1 emitted a tool_use and its tool_result as two separate
    turns and never guaranteed pairing, so resuming could 400 with
    "tool_use ids must have corresponding tool_result blocks". v3 pairs them
    per step, synthesises results for calls that never finished, and drops
    empty text blocks (the API rejects those too).
  * size. v1 replayed multi-megabyte bash output verbatim; a 7 MB transcript
    cannot be resumed. v3 truncates tool output and, by default, keeps only
    the newest turns that fit a byte budget, trimming on turn boundaries so
    pairing survives.

See claude-code-transcript-notes.md for the format details this relies on.

Usage:
  python3 oc2cc.py --list                 # opencode sessions + where each lands
  python3 oc2cc.py --all                  # import everything (idempotent)
  python3 oc2cc.py --session ses_x        # one session
  python3 oc2cc.py --all --dry-run        # show what would be written
  python3 oc2cc.py --prune-legacy         # move v1/v2 junk out of the way
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".local/share/opencode/opencode.db")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS = os.environ.get("OC2CC_OUT") or os.path.join(CLAUDE_DIR, "projects")
TRASH = os.path.join(CLAUDE_DIR, ".trash-oc2cc")
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL

# Marker so a re-run only ever overwrites its own output, never a real
# Claude Code session that happens to sit at the same path.
TAG = "opencode"

MAX_KEY = 200  # Claude Code truncates project keys past this, then appends a hash


# --------------------------------------------------------------------------
# Claude Code path layout
# --------------------------------------------------------------------------

def project_key(cwd):
    """Reproduce Claude Code's cwd -> folder name mapping, hash suffix included."""
    enc = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    if len(enc) <= MAX_KEY:
        return enc
    return "%s-%s" % (enc[:MAX_KEY], _b36(abs(_djb2(cwd))))


def _djb2(s):
    """`e=(e<<5)-e+charCodeAt(r)|0` -- 32-bit signed, as in the binary."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def _b36(n):
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        out = digits[n % 36] + out
        n //= 36
    return out


def session_uuid(sid):
    """Stable Claude session id for an opencode session id.

    Claude Code wants the filename to be the session id and `--session-id`
    only accepts UUIDs, so opencode's `ses_...` cannot be used directly.
    Deterministic means re-running overwrites instead of duplicating; it also
    matches v1's scheme, so old imports are replaced rather than doubled.
    """
    return str(uuid.uuid5(NS, "opencode:" + sid))


def entry_uuid(sess_uuid, n, kind=""):
    return str(uuid.uuid5(NS, "oc2cc:%s:%d:%s" % (sess_uuid, n, kind)))


def iso(ms):
    if not ms:
        ms = int(time.time() * 1000)
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def git_branch(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def claude_version():
    versions = os.path.join(HOME, ".local/share/claude/versions")
    best = None
    try:
        for name in os.listdir(versions):
            if re.fullmatch(r"\d+\.\d+\.\d+", name):
                key = tuple(int(x) for x in name.split("."))
                if best is None or key > best[0]:
                    best = (key, name)
    except OSError:
        pass
    return best[1] if best else "2.1.252"


# --------------------------------------------------------------------------
# opencode -> Claude tool mapping
# --------------------------------------------------------------------------

def _s(v):
    return v if isinstance(v, str) else ("" if v is None else json.dumps(v))


def map_tool(name, inp):
    """Return (claude_tool_name, claude_input).

    Tools whose schema maps 1:1 get Claude's real name so the transcript
    renders natively. Anything else keeps an `opencode_` name, which the UI
    renders generically instead of feeding a mismatched shape to a renderer
    that expects Claude's fields.
    """
    inp = inp if isinstance(inp, dict) else {}
    g = inp.get
    if name == "bash":
        out = {"command": _s(g("command"))}
        if g("description"):
            out["description"] = _s(g("description"))
        if g("timeout"):
            out["timeout"] = g("timeout")
        return "Bash", out
    if name == "read":
        out = {"file_path": _s(g("filePath"))}
        if g("offset") is not None:
            out["offset"] = g("offset")
        if g("limit") is not None:
            out["limit"] = g("limit")
        return "Read", out
    if name == "write":
        return "Write", {"file_path": _s(g("filePath")), "content": _s(g("content"))}
    if name == "edit":
        return "Edit", {"file_path": _s(g("filePath")),
                        "old_string": _s(g("oldString")),
                        "new_string": _s(g("newString")),
                        "replace_all": bool(g("replaceAll"))}
    if name == "grep":
        out = {"pattern": _s(g("pattern"))}
        if g("path"):
            out["path"] = _s(g("path"))
        if g("include"):
            out["glob"] = _s(g("include"))
        return "Grep", out
    if name == "glob":
        out = {"pattern": _s(g("pattern"))}
        if g("path"):
            out["path"] = _s(g("path"))
        return "Glob", out
    if name == "webfetch":
        return "WebFetch", {"url": _s(g("url")), "prompt": _s(g("format") or "fetch")}
    if name == "websearch":
        return "WebSearch", {"query": _s(g("query"))}
    if name == "task":
        return "Task", {"description": _s(g("description")),
                        "prompt": _s(g("prompt")),
                        "subagent_type": _s(g("subagent_type") or "general-purpose")}
    if name == "todowrite":
        todos = []
        for t in (g("todos") or []):
            if not isinstance(t, dict):
                continue
            content = _s(t.get("content"))
            todos.append({"content": content,
                          "status": _s(t.get("status")) or "pending",
                          "activeForm": content})
        return "TodoWrite", {"todos": todos}
    return "opencode_" + re.sub(r"[^A-Za-z0-9_]", "_", name or "tool"), inp


def clip(text, limit):
    """Truncate keeping both ends -- the tail of a command's output usually
    carries the result, the head carries what was run."""
    text = _s(text)
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return "%s\n\n... [oc2cc: %d chars elided] ...\n\n%s" % (
        text[:head], len(text) - limit, text[-tail:])


# --------------------------------------------------------------------------
# read opencode
# --------------------------------------------------------------------------

def connect(db=DB):
    if not os.path.exists(db):
        sys.exit("opencode DB not found: %s" % db)
    # read-only URI: safe to run while opencode is live (see opencode-db-notes.md)
    return sqlite3.connect("file:%s?mode=ro" % db, uri=True)


def list_sessions(cur):
    cur.execute("""
        SELECT s.id, s.title, s.directory, s.parent_id, s.time_created, s.time_updated,
               s.cost, s.tokens_input, s.tokens_output,
               s.tokens_cache_read, s.tokens_cache_write,
               (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id)
        FROM session s
        ORDER BY s.time_updated DESC
    """)
    cols = ("id title directory parent_id created updated cost tin tout "
            "tcread tcwrite messages").split()
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_messages(cur, sid):
    cur.execute("SELECT id, data, time_created FROM message "
                "WHERE session_id=? ORDER BY time_created ASC, id ASC", (sid,))
    msgs = []
    for mid, data, tc in cur.fetchall():
        try:
            md = json.loads(data)
        except Exception:
            md = {}
        cur.execute("SELECT data FROM part WHERE message_id=? "
                    "ORDER BY time_created ASC, id ASC", (mid,))
        parts = []
        for (pd,) in cur.fetchall():
            try:
                parts.append(json.loads(pd))
            except Exception:
                pass
        msgs.append({"id": mid, "data": md, "created": tc, "parts": parts})
    return msgs


# --------------------------------------------------------------------------
# opencode -> Claude transcript
# --------------------------------------------------------------------------

class Builder:
    """Turns one opencode session into a list of Claude transcript entries.

    Entries are emitted as "turns": a turn is one user entry or one
    assistant entry plus, when that assistant made tool calls, the single
    user entry carrying every matching tool_result. Trimming happens on turn
    boundaries so a tool_use never loses its result.
    """

    def __init__(self, sess_uuid, cwd, version, branch, opts):
        self.sess = sess_uuid
        self.cwd = cwd
        self.version = version
        self.branch = branch
        self.opts = opts
        self.turns = []          # list of lists of entries
        self.n = 0
        self.parent = None
        self.tool_calls = 0
        self.dropped_calls = 0

    # -- entry helpers -----------------------------------------------------

    def _base(self, kind, ms):
        self.n += 1
        e = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "entrypoint": "cli",
            "cwd": self.cwd,
            "sessionId": self.sess,
            "version": self.version,
            "type": kind,
            "uuid": entry_uuid(self.sess, self.n, kind),
            "timestamp": iso(ms),
        }
        if self.branch:
            e["gitBranch"] = self.branch
        self.parent = e["uuid"]
        return e

    def user(self, content, ms, meta=False, tool_result=None):
        e = self._base("user", ms)
        e["message"] = {"role": "user", "content": content}
        if meta:
            e["isMeta"] = True
        if tool_result is not None:
            # Claude Code stores the rendered result here; the picker and the
            # transcript view both read it, so keep it in sync with content.
            e["toolUseResult"] = tool_result
        return e

    def assistant(self, content, ms, model, usage=None):
        e = self._base("assistant", ms)
        msg = {
            "id": "msg_%s" % entry_uuid(self.sess, self.n, "mid").replace("-", "")[:24],
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": _model_id(model),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 0, "output_tokens": 0,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0,
                               "service_tier": "standard"},
        }
        e["message"] = msg
        return e

    # -- conversion --------------------------------------------------------

    def add(self, msgs):
        for m in msgs:
            data, parts = m["data"], m["parts"]
            role = data.get("role") or "user"
            ms = m["created"] or (data.get("time") or {}).get("created")
            # Bare model id only. A "provider/model" string makes Claude Code
            # hang on resume -- it parses the value and a slash sends it down a
            # path that never completes the turn. An unknown bare id is fine.
            model = data.get("modelID")

            if role == "user":
                self._add_user(parts, ms)
            else:
                self._add_assistant(parts, ms, model, data)

    def _add_user(self, parts, ms):
        blocks = []
        for p in parts:
            if p.get("type") == "text" and _s(p.get("text")).strip():
                blocks.append({"type": "text",
                               "text": clip(p["text"], self.opts.max_text)})
            elif p.get("type") == "compaction":
                # opencode's compaction trigger. Kept as a visible marker only.
                # Emitting a real Claude `compact_boundary` here would make
                # Claude Code slice the resumed context at this point and
                # expect compactMetadata we cannot honestly fill in, so the
                # marker stays inert and trimming is left to --max-bytes.
                blocks.append({"type": "text",
                               "text": "[opencode compaction boundary]"})
        if not blocks:
            return
        self.turns.append([self.user(blocks, ms)])

    def _add_assistant(self, parts, ms, model, data):
        o = self.opts
        content = []
        results = []

        def flush():
            """Close the current step: emit assistant + paired tool_results."""
            nonlocal content, results
            entries = []
            if content:
                entries.append(self.assistant(content, ms, model, _usage(data)))
            if results:
                entries.append(self.user(results, ms, tool_result="(opencode)"))
            if entries:
                self.turns.append(entries)
            content, results = [], []

        for p in parts:
            t = p.get("type")
            if t == "text":
                if _s(p.get("text")).strip():
                    content.append({"type": "text",
                                    "text": clip(p["text"], o.max_text)})
            elif t == "reasoning":
                if o.keep_reasoning and _s(p.get("text")).strip():
                    # Real thinking blocks need a signature Claude will not
                    # accept from us, so carry them as tagged text instead.
                    content.append({"type": "text",
                                    "text": "<opencode-thinking>\n%s\n</opencode-thinking>"
                                            % clip(p["text"], o.max_text)})
            elif t == "tool":
                st = p.get("state") or {}
                cid = p.get("callID") or ("call_%s" % uuid.uuid4().hex[:20])
                name, inp = map_tool(p.get("tool"), st.get("input"))
                content.append({"type": "tool_use", "id": cid,
                                "name": name, "input": inp})
                status = st.get("status")
                if status == "completed":
                    out = st.get("output")
                    if out is None:
                        out = (st.get("metadata") or {}).get("output")
                    body = clip(out, o.max_tool_out) or "(no output)"
                    err = False
                elif status == "error":
                    body = clip(st.get("error") or "tool failed", o.max_tool_out)
                    err = True
                else:
                    # running/pending at the time opencode stopped. A tool_use
                    # with no tool_result makes the resumed request invalid,
                    # so synthesise one.
                    body = "[oc2cc] tool never completed (opencode status: %s)" % (
                        status or "unknown")
                    err = True
                    self.dropped_calls += 1
                results.append({"type": "tool_result", "tool_use_id": cid,
                                "content": body, "is_error": err})
                self.tool_calls += 1
            elif t == "step-finish":
                flush()
            elif t == "step-start":
                if content or results:
                    flush()

        err = data.get("error")
        if err and o.keep_errors:
            name = (err or {}).get("name") or "error"
            detail = _s(((err or {}).get("data") or {}).get("message"))
            content.append({"type": "text",
                            "text": "[opencode %s] %s" % (name, clip(detail, 2000))})
        flush()


def _model_id(model):
    """Sanitise an opencode model id for Claude Code's `message.model`.

    A slash in this field makes `--resume` hang: Claude Code parses the value
    and a "provider/model" shape sends it down a path that never finishes the
    turn (verified by bisecting two otherwise identical transcripts). Some
    opencode model ids carry a slash natively -- e.g.
    "minimax/minimax-m2.7:free" -- so strip to the last segment rather than
    trusting the DB. Unknown bare ids resume fine.
    """
    if not model:
        return "<synthetic>"
    return model.rsplit("/", 1)[-1] or "<synthetic>"


def _usage(data):
    tok = data.get("tokens") or {}
    cache = tok.get("cache") or {}
    return {
        "input_tokens": int(tok.get("input") or 0),
        "output_tokens": int(tok.get("output") or 0),
        "cache_creation_input_tokens": int(cache.get("write") or 0),
        "cache_read_input_tokens": int(cache.get("read") or 0),
        "service_tier": "standard",
    }


# --------------------------------------------------------------------------
# write the transcript
# --------------------------------------------------------------------------

def trim(turns, max_bytes):
    """Keep the newest turns that fit `max_bytes`, plus the first user turn.

    Trimming whole turns is what keeps every tool_use paired with its
    tool_result. The oldest user turn is kept regardless because the /resume
    picker shows the first user message as the session's preview line.
    """
    if max_bytes <= 0:
        return turns, 0
    sizes = [sum(len(json.dumps(e, ensure_ascii=False)) + 1 for e in t) for t in turns]
    total = sum(sizes)
    if total <= max_bytes:
        return turns, 0
    head = 0
    while head < len(turns) and turns[head][0].get("type") != "user":
        head += 1
    head_turns = turns[head:head + 1] if head < len(turns) else []
    head_size = sizes[head] if head < len(turns) else 0

    kept, used = [], head_size
    for i in range(len(turns) - 1, -1, -1):
        if head_turns and i == head:
            continue
        if used + sizes[i] > max_bytes:
            break
        kept.append(turns[i])
        used += sizes[i]
    kept.reverse()
    dropped = len(turns) - len(kept) - len(head_turns)
    return head_turns + kept, dropped


def relink(entries):
    """Rewrite parentUuid into a single chain after trimming."""
    parent = None
    for e in entries:
        if "parentUuid" in e:
            e["parentUuid"] = parent
            parent = e["uuid"]
    return entries


def write_transcript(path, entries, trailers, mtime_ms):
    tmp = path + ".oc2cc.tmp"
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        for e in trailers:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    # The picker sorts by mtime and shows it as the session date, so make the
    # file look as old as the conversation it holds.
    if mtime_ms:
        sec = mtime_ms / 1000
        os.utime(path, (sec, sec))


def is_ours(path):
    """True if this file was written by oc2cc (checked before overwriting)."""
    try:
        with open(path) as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("oc2cc") or e.get("tag") == TAG:
                    return True
    except OSError:
        return False
    return False


def convert_one(cur, sess, opts, version):
    sid = sess["id"]
    cwd = sess["directory"] or os.getcwd()
    sess_uuid = session_uuid(sid)
    key = project_key(cwd)
    outdir = os.path.join(PROJECTS, key)
    path = os.path.join(outdir, sess_uuid + ".jsonl")

    if os.path.exists(path) and not is_ours(path) and not opts.force:
        print("  skip %s -> %s.jsonl (exists, not written by oc2cc; --force to override)"
              % (sid, sess_uuid[:8]))
        return None

    msgs = load_messages(cur, sid)
    b = Builder(sess_uuid, cwd, version, git_branch(cwd), opts)
    b.add(msgs)
    turns, dropped = trim(b.turns, opts.max_bytes)
    entries = relink([e for t in turns for e in t])

    if not entries:
        print("  skip %s (no convertible content)" % sid)
        return None

    # Mark ours, on the first entry, so re-runs can tell.
    entries[0]["oc2cc"] = {"sourceSessionId": sid, "tag": TAG,
                           "importedAt": iso(None)}

    title = (sess["title"] or "").strip()
    trailers = [
        {"type": "mode", "mode": "normal", "sessionId": sess_uuid},
        {"type": "permission-mode", "permissionMode": "default",
         "sessionId": sess_uuid},
    ]
    if title:
        # customTitle wins over aiTitle in the picker, so the opencode title
        # is what shows up.
        trailers.append({"type": "custom-title", "customTitle": title[:200],
                         "sessionId": sess_uuid})
    trailers.append({"type": "tag", "tag": TAG, "sessionId": sess_uuid})
    last_user = next((e["uuid"] for e in reversed(entries)
                      if e.get("type") == "user"), None)
    if last_user:
        trailers.append({"type": "last-prompt", "lastPrompt": "(imported from opencode)",
                         "leafUuid": last_user, "sessionId": sess_uuid})
    if sess["cost"]:
        trailers.append({
            "type": "cost-state", "sessionId": sess_uuid,
            "totalCostUSD": sess["cost"],
            "totalAPIDuration": 0, "totalAPIDurationWithoutRetries": 0,
            "totalToolDuration": 0, "totalLinesAdded": 0, "totalLinesRemoved": 0,
            "totalDuration": max(0, (sess["updated"] or 0) - (sess["created"] or 0)),
            "startTime": sess["created"] or 0,
            "modelUsage": {}, "hasUnknownModelCost": True,
        })

    nbytes = sum(len(json.dumps(e, ensure_ascii=False)) + 1
                 for e in entries + trailers)
    if opts.dry_run:
        print("  DRY %s -> %s/%s.jsonl  %d entries, %d tools, %s%s"
              % (sid, key, sess_uuid[:8], len(entries), b.tool_calls,
                 _human(nbytes), ", %d turns trimmed" % dropped if dropped else ""))
        return path

    os.makedirs(outdir, exist_ok=True)
    write_transcript(path, entries, trailers, sess["updated"])
    print("  %s -> %s.jsonl  %d entries, %d tools, %s%s"
          % (sid, sess_uuid[:8], len(entries), b.tool_calls, _human(nbytes),
             ", %d turns trimmed" % dropped if dropped else ""))
    return path


def _human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


# --------------------------------------------------------------------------
# legacy cleanup
# --------------------------------------------------------------------------

def prune_legacy(dry_run):
    """Move v1/v2 output out of the projects tree, into ~/.claude/.trash-oc2cc.

    Two kinds of junk:
      * whole project folders whose name is not what Claude Code computes for
        any cwd -- these came from the "/"-only encoder and are dead weight;
      * ses_*.jsonl files, which Claude Code ignores because a transcript
        filename has to be a UUID.
    Moved, not deleted, so nothing is lost if the guess is wrong.
    """
    if not os.path.isdir(PROJECTS):
        print("no projects dir at %s" % PROJECTS)
        return
    moved = []
    for name in sorted(os.listdir(PROJECTS)):
        p = os.path.join(PROJECTS, name)
        if os.path.isfile(p) and name == "MAPPING.txt":
            moved.append((p, name))
            continue
        if not os.path.isdir(p):
            continue
        # A valid key contains only [A-Za-z0-9-]; anything else can never be
        # produced by Claude Code's encoder.
        if re.search(r"[^A-Za-z0-9\-]", name):
            moved.append((p, name))
            continue
        for f in sorted(os.listdir(p)):
            if f.startswith("ses_") and f.endswith(".jsonl"):
                moved.append((os.path.join(p, f), os.path.join(name, f)))
    if not moved:
        print("nothing to prune")
        return
    for src, rel in moved:
        dst = os.path.join(TRASH, rel)
        print("  %s %s" % ("would move" if dry_run else "move", rel))
        if dry_run:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            shutil.rmtree(dst) if os.path.isdir(dst) else os.remove(dst)
        shutil.move(src, dst)
    if not dry_run:
        print("moved %d item(s) to %s" % (len(moved), TRASH))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Import opencode sessions into Claude Code.")
    ap.add_argument("--all", action="store_true", help="convert every session")
    ap.add_argument("--session", action="append", default=[],
                    help="opencode session id (repeatable)")
    ap.add_argument("--list", action="store_true",
                    help="list opencode sessions and their target paths")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite transcripts oc2cc did not write")
    ap.add_argument("--children", action="store_true",
                    help="also import subagent sessions (skipped by default: "
                         "they clutter the picker)")
    ap.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024,
                    metavar="N",
                    help="per-transcript budget, newest turns kept "
                         "(default 2MB; 0 = no limit)")
    ap.add_argument("--max-tool-out", type=int, default=4000, metavar="N",
                    help="chars kept per tool result (default 4000)")
    ap.add_argument("--max-text", type=int, default=20000, metavar="N",
                    help="chars kept per text block (default 20000)")
    ap.add_argument("--no-reasoning", dest="keep_reasoning",
                    action="store_false", help="drop thinking blocks")
    ap.add_argument("--no-errors", dest="keep_errors", action="store_false",
                    help="drop opencode API error notes")
    ap.add_argument("--prune-legacy", action="store_true",
                    help="move v1/v2 output to ~/.claude/.trash-oc2cc")
    ap.add_argument("--db", default=DB)
    opts = ap.parse_args()

    if opts.prune_legacy:
        prune_legacy(opts.dry_run)
        if not (opts.all or opts.session or opts.list):
            return

    con = connect(opts.db)
    cur = con.cursor()
    sessions = list_sessions(cur)
    version = claude_version()

    if opts.list:
        print("%-32s %6s %-10s %-11s %s"
              % ("opencode session", "msgs", "claude-id", "kind", "title"))
        for s in sessions:
            print("%-32s %6d %-10s %-11s %s"
                  % (s["id"], s["messages"],
                     session_uuid(s["id"])[:8],
                     "subagent" if s["parent_id"] else "top-level",
                     (s["title"] or "")[:44]))
        print("\nproject dir: %s" % os.path.join(PROJECTS, project_key(
            sessions[0]["directory"] if sessions else os.getcwd())))
        return

    if opts.session:
        wanted = [s for s in sessions if s["id"] in set(opts.session)]
        missing = set(opts.session) - {s["id"] for s in wanted}
        for m in missing:
            print("  session not found: %s" % m)
    elif opts.all:
        wanted = sessions if opts.children else [s for s in sessions
                                                 if not s["parent_id"]]
        # Sessions cc2oc.py brought in FROM Claude Code must not be shipped
        # back, or every sync cycle mints another copy of the same
        # conversation under a new id on the other side. cc2oc marks them in
        # the title because opencode's importer strips unknown keys from
        # messages and parts, leaving nowhere else to record provenance.
        before = len(wanted)
        wanted = [s for s in wanted
                  if not (s["title"] or "").rstrip().endswith("[claude]")]
        bounced = before - len(wanted)
        if bounced:
            print("  skipping %d session(s) imported from Claude Code" % bounced)
    else:
        ap.print_help()
        return

    print("converting %d session(s) -> %s" % (len(wanted), PROJECTS))
    ok = 0
    for s in wanted:
        if convert_one(cur, s, opts, version):
            ok += 1
    con.close()
    print("done: %d written%s" % (ok, " (dry run)" if opts.dry_run else ""))
    if ok and not opts.dry_run:
        print("open with:  claude --resume")


if __name__ == "__main__":
    main()
