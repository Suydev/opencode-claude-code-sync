#!/usr/bin/env python3
"""cc2oc -- import Claude Code sessions into opencode.

The mirror image of oc2cc.py. Reads Claude Code transcripts out of
~/.claude/projects/<projectKey>/<uuid>.jsonl, converts them into opencode's
export format, and feeds them to `opencode import`, so Claude Code work shows
up in opencode's session list.

Why it goes through `opencode import` rather than writing SQLite directly:
opencode's DB is live (WAL, shared by every running process) and its own
importer already owns id allocation, project rows, and event bookkeeping.
Driving the supported entry point is the difference between a sync tool and a
corruption tool. Cost is that opencode validates strictly -- see below.

What the format demands (learned by probing opencode 1.17.9):

  * ids carry their own timestamp. `msg_`/`prt_` are ascending, `ses_` is
    descending (bit-complemented), both over a 48-bit field built as
    ((ms - 1786706395136) << 12) | counter, then 14 random base62 chars.
    cc2oc derives those chars from a hash of the Claude uuid, so re-running
    produces identical ids and the import updates in place instead of
    duplicating.
  * assistant messages require `parentID`, `modelID` and `providerID` as flat
    keys. User messages instead carry a nested `model: {providerID, modelID}`
    plus `summary: {diffs: []}`. Getting this wrong fails with a bare
    "Missing key at [...]" and writes nothing.
  * unknown keys are dropped silently. There is no room for an import marker
    inside a message or part, so provenance is recorded in the session title
    and in a local ledger file instead.
  * tool parts survive intact, including state/input/output/metadata.

Usage:
  python3 cc2oc.py --list             # Claude sessions and what would import
  python3 cc2oc.py --all              # import all of them (idempotent)
  python3 cc2oc.py --session <uuid>   # just one
  python3 cc2oc.py --all --dry-run    # write the JSON, do not import
"""

import argparse
import hashlib
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
LEDGER = os.path.join(HOME, ".local/share/opencode/cc2oc-ledger.json")

# opencode id encoding
EPOCH = 1786706395136          # 13 * 2**37
ID_BITS = 48
COUNTER_BITS = 12
B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase

# Claude tool name -> (opencode tool name, input remapper)
TOOL_MAP = {
    "Bash": "bash", "Read": "read", "Write": "write", "Edit": "edit",
    "Grep": "grep", "Glob": "glob", "WebFetch": "webfetch",
    "WebSearch": "websearch", "Task": "task", "Agent": "task",
    "TodoWrite": "todowrite", "Skill": "skill",
}


# --------------------------------------------------------------------------
# opencode id generation
# --------------------------------------------------------------------------

def _suffix(seed, n=14):
    """Deterministic base62 tail, so a given Claude entry always maps to the
    same opencode id and re-imports overwrite rather than duplicate."""
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(B62[b % 62] for b in h[:n])


def oc_id(prefix, ms, counter, seed, descending=False):
    t = ((int(ms) - EPOCH) << COUNTER_BITS) | (counter & ((1 << COUNTER_BITS) - 1))
    if t < 0:
        t = 0
    if descending:
        t = ((1 << ID_BITS) - 1) - t
    return "%s%012x%s" % (prefix, t, _suffix(seed))


def ms_of(iso_ts, fallback=None):
    if not iso_ts:
        return fallback or int(time.time() * 1000)
    s = iso_ts.replace("Z", "+00:00")
    try:
        import datetime
        return int(datetime.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return fallback or int(time.time() * 1000)


# --------------------------------------------------------------------------
# read Claude transcripts
# --------------------------------------------------------------------------

def read_transcript(path):
    """Return (entries, trailers) -- conversation vs sidecar records."""
    conv, meta = [], {}
    for line in open(path, errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        if t in ("user", "assistant"):
            conv.append(e)
        elif t == "custom-title":
            meta["title"] = e.get("customTitle")
        elif t == "ai-title":
            meta.setdefault("title", e.get("aiTitle"))
        elif t == "tag":
            meta["tag"] = e.get("tag")
        elif t == "cost-state":
            meta["cost"] = e.get("totalCostUSD")
    return conv, meta


def scan_projects():
    """Every Claude transcript, grouped by project folder."""
    out = []
    if not os.path.isdir(PROJECTS):
        return out
    for key in sorted(os.listdir(PROJECTS)):
        d = os.path.join(PROJECTS, key)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".jsonl"):
                continue
            uid = f[:-6]
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uid):
                continue
            out.append({"key": key, "uuid": uid, "path": os.path.join(d, f)})
    return out


def decode_key(key):
    """Best-effort projectKey -> cwd.

    The encoder is lossy (every non-alphanumeric becomes "-"), so the cwd is
    recovered from the transcript's own `cwd` field instead. This is only the
    fallback when a transcript carries none.
    """
    return "/" + key.lstrip("-").replace("-", "/")


# --------------------------------------------------------------------------
# convert to opencode export format
# --------------------------------------------------------------------------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def convert(sess, opts):
    conv, meta = read_transcript(sess["path"])
    if not conv:
        return None

    # Skip what oc2cc put there: importing opencode's own sessions back would
    # create a loop and a second copy under a different id.
    if meta.get("tag") == "opencode":
        return None
    head = conv[0]
    if head.get("oc2cc"):
        return None

    cwd = head.get("cwd") or decode_key(sess["key"])
    created = ms_of(head.get("timestamp"))
    updated = ms_of(conv[-1].get("timestamp"), created)
    sid = oc_id("ses_", created, 1, "cc2oc:" + sess["uuid"], descending=True)

    model_id = "claude-opus-5"
    for e in conv:
        if e.get("type") == "assistant":
            m = (e.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                model_id = m.rsplit("/", 1)[-1]
                break

    messages = []
    counter = 1
    last_user_id = None
    # Claude splits one exchange across several entries (assistant text,
    # assistant tool_use, user tool_result). opencode wants tool calls and
    # their output inside the assistant message that made them, so results are
    # collected first and folded back in.
    results = {}
    for e in conv:
        if e.get("type") != "user":
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = b

    for e in conv:
        kind = e.get("type")
        msg = e.get("message") or {}
        content = msg.get("content")
        ts = ms_of(e.get("timestamp"), created)

        if kind == "user":
            if e.get("isMeta"):
                continue
            # A user entry that only carries tool_result is bookkeeping; its
            # payload already went into the assistant message.
            if isinstance(content, list) and content and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content):
                continue
            body = text_of(content)
            if not body.strip():
                continue
            mid = oc_id("msg_", ts, counter, "m:" + e["uuid"])
            pid = oc_id("prt_", ts, counter + 1, "p:" + e["uuid"])
            counter += 2
            last_user_id = mid
            messages.append({
                "info": {"role": "user", "time": {"created": ts},
                         "agent": "build",
                         "model": {"providerID": "claude",
                                   "modelID": model_id},
                         "summary": {"diffs": []},
                         "id": mid, "sessionID": sid},
                "parts": [{"type": "text", "text": body[:opts.max_text],
                           "id": pid, "sessionID": sid, "messageID": mid}],
            })
            continue

        if kind != "assistant":
            continue

        parts = []
        mid = oc_id("msg_", ts, counter, "m:" + e["uuid"])
        counter += 1
        blocks = content if isinstance(content, list) else []
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            pid = oc_id("prt_", ts, counter, "p%d:%s" % (i, e["uuid"]))
            counter += 1
            if b.get("type") == "text":
                if not (b.get("text") or "").strip():
                    continue
                parts.append({"type": "text", "text": b["text"][:opts.max_text],
                              "id": pid, "sessionID": sid, "messageID": mid})
            elif b.get("type") == "thinking":
                if not opts.keep_reasoning:
                    continue
                body = b.get("thinking") or ""
                if not body.strip():
                    continue
                # A reasoning part must carry its own time span; opencode
                # rejects the message with a bare `at ["time"]` otherwise.
                parts.append({"type": "reasoning", "text": body[:opts.max_text],
                              "time": {"start": ts, "end": ts},
                              "id": pid, "sessionID": sid, "messageID": mid})
            elif b.get("type") == "tool_use":
                name = TOOL_MAP.get(b.get("name"), b.get("name") or "tool")
                res = results.get(b.get("id"))
                out = ""
                status = "completed"
                if res is None:
                    status, out = "error", "[cc2oc] no tool_result recorded"
                else:
                    rc = res.get("content")
                    if isinstance(rc, list):
                        out = "\n".join(x.get("text", "") for x in rc
                                        if isinstance(x, dict))
                    else:
                        out = rc if isinstance(rc, str) else json.dumps(rc)
                    if res.get("is_error"):
                        status = "error"
                out = (out or "")[:opts.max_tool_out]
                state = {"status": status,
                         "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                         "time": {"start": ts, "end": ts}}
                if status == "error":
                    state["error"] = out or "tool failed"
                else:
                    # A completed tool state must carry output, metadata AND
                    # title; opencode rejects the message with
                    # `at ["state"]["title"]` when title is absent. Error
                    # states take error instead and want no title.
                    state["output"] = out
                    state["metadata"] = {"output": out}
                    state["title"] = _title_for(name, b.get("input"))
                parts.append({"type": "tool", "tool": name,
                              "callID": b.get("id") or ("call_%s" % pid[-12:]),
                              "state": state,
                              "id": pid, "sessionID": sid, "messageID": mid})

        if not parts:
            continue
        info = {"parentID": last_user_id or messages[0]["info"]["id"] if messages else None,
                "role": "assistant", "mode": "build", "agent": "build",
                "modelID": model_id, "providerID": "claude",
                "path": {"cwd": cwd, "root": "/"},
                "cost": 0,
                "tokens": _tokens(msg.get("usage") or {}),
                "time": {"created": ts, "completed": ts},
                "finish": "stop",
                "id": mid, "sessionID": sid}
        if info["parentID"] is None:
            continue
        messages.append({"info": info, "parts": parts})

    if not messages:
        return None
    if messages[0]["info"]["role"] != "user":
        return None

    title = (meta.get("title") or "").strip()
    if not title:
        title = (text_of((conv[0].get("message") or {}).get("content"))
                 or "Claude Code session").strip().splitlines()[0]
    # Provenance has to live in a field opencode keeps; unknown keys are
    # stripped from messages and parts on import.
    title = "%s [claude]" % title[:180]

    doc = {
        "info": {"id": sid, "slug": "cc-" + sess["uuid"][:8],
                 "projectID": "global",
                 "directory": cwd, "path": cwd.lstrip("/"),
                 "title": title, "agent": "build",
                 "model": {"id": model_id, "providerID": "claude"},
                 "version": "1.17.9",
                 "summary": {"additions": 0, "deletions": 0, "files": 0},
                 "cost": meta.get("cost") or 0,
                 "tokens": {"input": 0, "output": 0, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}},
                 "time": {"created": created, "updated": updated}},
        "messages": messages,
    }
    return {"doc": doc, "sid": sid, "messages": len(messages),
            "tools": sum(1 for m in messages for p in m["parts"]
                         if p.get("type") == "tool")}


def _title_for(tool, inp):
    """Short label for a tool call, matching what opencode stores.

    opencode uses this as the collapsed one-line summary in its TUI. Claude
    Code has no equivalent field, so it is reconstructed from the arguments:
    the description when the tool provides one, otherwise the primary argument.
    """
    inp = inp if isinstance(inp, dict) else {}
    for k in ("description", "command", "file_path", "pattern", "url",
              "query", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            return v[:120]
    return tool


def _tokens(usage):
    return {"input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "reasoning": 0,
            "cache": {"read": int(usage.get("cache_read_input_tokens") or 0),
                      "write": int(usage.get("cache_creation_input_tokens") or 0)}}


# --------------------------------------------------------------------------
# drive `opencode import`
# --------------------------------------------------------------------------

def oc_import(doc, timeout=180):
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="cc2oc-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        r = subprocess.run(["opencode", "import", tmp],
                           capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0 and "Imported session" in (r.stdout + r.stderr)
        msg = (r.stdout + r.stderr).strip().splitlines()
        return ok, (msg[-1] if msg else "no output")
    except subprocess.TimeoutExpired:
        return False, "opencode import timed out"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_ledger():
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except Exception:
        return {}


def save_ledger(led):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1)
    os.replace(tmp, LEDGER)


def main():
    ap = argparse.ArgumentParser(
        description="Import Claude Code sessions into opencode.")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--session", action="append", default=[],
                    help="Claude session uuid (repeatable)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="convert and validate, but do not import")
    ap.add_argument("--max-tool-out", type=int, default=4000)
    ap.add_argument("--max-text", type=int, default=20000)
    ap.add_argument("--no-reasoning", dest="keep_reasoning",
                    action="store_false")
    ap.add_argument("--min-messages", type=int, default=2,
                    help="skip transcripts shorter than this (default 2)")
    opts = ap.parse_args()

    found = scan_projects()
    if not found:
        print("no Claude transcripts under %s" % PROJECTS)
        return

    if opts.session:
        want = set(opts.session)
        found = [s for s in found
                 if s["uuid"] in want or s["uuid"][:8] in want]

    led = load_ledger()

    if opts.list:
        print("%-38s %5s %5s  %s" % ("claude session", "msgs", "tools", "title"))
        for s in found:
            c = convert(s, opts)
            if c is None:
                print("%-38s %5s %5s  %s" % (s["uuid"][:36], "-", "-",
                                             "(skipped: opencode import or empty)"))
                continue
            state = "synced" if led.get(s["uuid"], {}).get("sid") == c["sid"] else "new"
            print("%-38s %5d %5d  %-6s %s"
                  % (s["uuid"][:36], c["messages"], c["tools"], state,
                     c["doc"]["info"]["title"][:40]))
        return

    if not (opts.all or opts.session):
        ap.print_help()
        return

    print("scanning %d Claude transcript(s)" % len(found))
    ok = skipped = failed = 0
    for s in found:
        c = convert(s, opts)
        if c is None:
            skipped += 1
            continue
        if c["messages"] < opts.min_messages:
            skipped += 1
            continue
        if opts.dry_run:
            print("  DRY %s -> %s  %d msgs, %d tools"
                  % (s["uuid"][:8], c["sid"], c["messages"], c["tools"]))
            ok += 1
            continue
        good, note = oc_import(c["doc"])
        if good:
            print("  %s -> %s  %d msgs, %d tools"
                  % (s["uuid"][:8], c["sid"], c["messages"], c["tools"]))
            led[s["uuid"]] = {"sid": c["sid"], "at": int(time.time() * 1000),
                              "messages": c["messages"]}
            ok += 1
        else:
            print("  FAIL %s: %s" % (s["uuid"][:8], note))
            failed += 1
    if not opts.dry_run:
        save_ledger(led)
    print("done: %d imported, %d skipped, %d failed%s"
          % (ok, skipped, failed, " (dry run)" if opts.dry_run else ""))


if __name__ == "__main__":
    main()
