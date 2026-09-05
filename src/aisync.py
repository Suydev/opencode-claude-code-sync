#!/usr/bin/env python3
"""aisync -- the coordination layer between oc2cc and cc2oc.

oc2cc and cc2oc are one-way converters. Run naively in both directions they
lose work, because each one overwrites its destination wholesale:

    session X lives in opencode, oc2cc pushes it to Claude Code as uuid U.
    You then add turns to U inside Claude Code, and separately add turns to X
    inside opencode. Next sync: oc2cc rebuilds U from X and the Claude-side
    turns are gone. cc2oc, seeing U marked as an opencode import, skips it --
    so they never come back either. Silent, total loss of one side's work.

This module owns the decisions that prevent that. It does not convert anything
itself; it drives oc2cc and cc2oc as libraries and adds:

  * a ledger of paired sessions with a fingerprint of each side as of the last
    successful sync, which is what makes "who changed since we last agreed?"
    answerable at all;
  * a decision per pair -- forward, reverse, merge, defer, or skip -- instead
    of unconditional overwrite;
  * a union merge when both sides moved, so neither side's turns are dropped
    and no duplicate session is minted;
  * a liveness guard, because rewriting a transcript that a running process has
    open loses whichever copy is written second;
  * a repair ladder that reads the validator's own error path and retries with
    the offending shape corrected, then quarantines with backoff rather than
    failing the same way forever.

Nothing here deletes. The worst case for any pair is "deferred, try later",
and every overwrite keeps a backup of what it replaced.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
REGISTRY = os.path.join(CLAUDE_DIR, "sessions")
STATE_DIR = os.path.join(HOME, ".local/share/opencode")
LEDGER = os.path.join(STATE_DIR, "aisync-ledger.json")
BACKUPS = os.path.join(STATE_DIR, "aisync-backups")
LEDGER_VERSION = 2

# A session touched this recently is assumed to still be in use, even with no
# process evidence: opencode's TUI holds state in memory and flushes on its own
# schedule, so writing underneath it races that flush.
FRESH_SECS = 180
# Quarantine backoff after repeated conversion failures. Doubles each time,
# capped, so a permanently malformed transcript costs one attempt a day rather
# than one attempt a minute.
QUARANTINE_BASE_SECS = 900
QUARANTINE_MAX_SECS = 86400


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Sibling modules, loaded by path so the three files can live anywhere
# together (see AISYNC_HOME in bin/ai-sync).
_HERE = os.path.dirname(os.path.abspath(__file__))
OC2CC = _load_module("oc2cc", os.path.join(_HERE, "oc2cc.py"))
CC2OC = _load_module("cc2oc", os.path.join(_HERE, "cc2oc.py"))


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def load_ledger():
    try:
        with open(LEDGER) as f:
            led = json.load(f)
        if led.get("version") != LEDGER_VERSION:
            # A ledger from an older layout is not wrong, just unreadable.
            # Keep it for forensics and start clean; the first pass after that
            # treats every pair as new, which is safe because a new pair with
            # content on both sides is a merge, never an overwrite.
            os.replace(LEDGER, LEDGER + ".v%s" % led.get("version", "0"))
            raise ValueError("ledger version changed")
        led.setdefault("pairs", {})
        return led
    except Exception:
        return {"version": LEDGER_VERSION, "pairs": {}}


def save_ledger(led):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


# --------------------------------------------------------------------------
# fingerprints
# --------------------------------------------------------------------------
# Cheap by design. A conversation only ever grows at the tail, so
# (count, last id) detects every append without reading message bodies -- which
# matters when the Claude side alone is ~20 MB and this runs after every turn.

def fp_opencode(cur, sid):
    cur.execute("SELECT COUNT(*) FROM message WHERE session_id=?", (sid,))
    n = cur.fetchone()[0]
    cur.execute("SELECT id FROM message WHERE session_id=? "
                "ORDER BY time_created DESC, id DESC LIMIT 1", (sid,))
    row = cur.fetchone()
    last = row[0] if row else ""
    cur.execute("SELECT COUNT(*) FROM part WHERE session_id=?", (sid,))
    parts = cur.fetchone()[0]
    return {"messages": n, "parts": parts, "last": last,
            "h": _h("oc", sid, n, parts, last)}


def fp_claude(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    conv, _ = CC2OC.read_transcript(path)
    last = conv[-1]["uuid"] if conv else ""
    return {"entries": len(conv), "size": st.st_size, "last": last,
            "h": _h("cc", len(conv), last)}


def _h(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------

def live_claude_sessions():
    """Session uuids currently held by a running Claude Code process.

    Claude Code registers every session under ~/.claude/sessions/<pid>.json and
    does not always remove the file on exit -- six of seven entries here were
    stale from killed runs -- so the pid is checked against /proc rather than
    trusted.
    """
    live = set()
    try:
        names = os.listdir(REGISTRY)
    except OSError:
        return live
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(REGISTRY, name)
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not pid or not sid:
            continue
        if os.path.isdir("/proc/%d" % int(pid)):
            live.add(sid)
        else:
            # Reap the stale entry so ListAgents and the picker stop offering a
            # peer that cannot answer.
            try:
                os.unlink(p)
            except OSError:
                pass
    return live


def opencode_running():
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if b"opencode.bin" in f.read():
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------
# normalised turns, for merging
# --------------------------------------------------------------------------
# Round-tripping is lossy: tool output gets truncated, reasoning gets rewrapped,
# ids are regenerated. So a merge cannot match turns by id or by exact content.
# It matches on a normalised signature -- kind plus a short prefix of the
# salient text -- which survives both conversions and is enough to recognise
# "these two are the same turn" across the boundary.

def sig_claude(entry):
    msg = entry.get("message") or {}
    c = msg.get("content")
    kind = entry.get("type")
    bits = []
    if isinstance(c, str):
        bits.append(("t", _norm(c)))
    elif isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                bits.append(("t", _norm(b.get("text"))))
            elif b.get("type") == "tool_use":
                bits.append(("u", b.get("name"), _norm(_primary(b.get("input")))))
            elif b.get("type") == "tool_result":
                bits.append(("r",))
            elif b.get("type") == "thinking":
                bits.append(("k", _norm(b.get("thinking"))))
    if not bits:
        return None
    return _h(kind, *[str(x) for x in bits])


def sig_opencode(msg):
    info = msg.get("info") or msg.get("data") or {}
    kind = info.get("role")
    bits = []
    for p in msg.get("parts", []):
        t = p.get("type")
        if t == "text":
            bits.append(("t", _norm(p.get("text"))))
        elif t == "reasoning":
            bits.append(("k", _norm(p.get("text"))))
        elif t == "tool":
            name = _cc_name(p.get("tool"))
            inp = (p.get("state") or {}).get("input")
            bits.append(("u", name, _norm(_primary(inp))))
    if not bits:
        return None
    return _h("user" if kind == "user" else "assistant",
              *[str(x) for x in bits])


_CC_NAME = {v: k for k, v in CC2OC.TOOL_MAP.items() if k != "Agent"}


def _cc_name(oc_tool):
    return _CC_NAME.get(oc_tool, oc_tool)


def _norm(s):
    """Collapse whitespace and keep a short prefix.

    Short, because a truncated tool result and its untruncated original must
    hash the same; long enough that two different commands do not collide.
    """
    if not isinstance(s, str):
        return ""
    return " ".join(s.split())[:160]


def _primary(inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "filePath", "pattern", "url", "query",
              "prompt", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

FORWARD = "forward"     # opencode -> Claude Code
REVERSE = "reverse"     # Claude Code -> opencode
MERGE = "merge"         # both moved, union them
NOOP = "noop"
DEFER = "defer"


def decide(rec, oc_fp, cc_fp, oc_live, cc_live):
    """Which way this pair should move, given both fingerprints and the ledger.

    The ledger records what both sides looked like when they last agreed. From
    that, "changed" is decidable per side, and the four combinations map onto
    four different actions -- which is the whole point. Without the ledger the
    only available action is overwrite, and overwrite is how work gets lost.
    """
    if oc_live or cc_live:
        return DEFER, "session in use"

    seen_oc = (rec.get("oc") or {}).get("h")
    seen_cc = (rec.get("cc") or {}).get("h")

    oc_now = oc_fp["h"] if oc_fp else None
    cc_now = cc_fp["h"] if cc_fp else None

    # Never synced before.
    if seen_oc is None and seen_cc is None:
        if oc_now and cc_now:
            # Both sides already hold content for the same pairing and we have
            # no record of a common ancestor. Overwriting either direction
            # would be a guess; union is the only choice that cannot lose a
            # turn.
            return MERGE, "first sight, content on both sides"
        if oc_now:
            return FORWARD, "new to Claude Code"
        if cc_now:
            return REVERSE, "new to opencode"
        return NOOP, "nothing on either side"

    oc_changed = oc_now != seen_oc
    cc_changed = cc_now != seen_cc

    if not oc_changed and not cc_changed:
        return NOOP, "in sync"
    if oc_changed and not cc_changed:
        return FORWARD, "opencode moved"
    if cc_changed and not oc_changed:
        return REVERSE, "Claude Code moved"
    return MERGE, "both moved since last sync"


def quarantined(rec, now_ms):
    until = rec.get("quarantined_until") or 0
    return until > now_ms


def note_failure(rec, why, now_ms):
    rec["failures"] = int(rec.get("failures") or 0) + 1
    backoff = min(QUARANTINE_BASE_SECS * (2 ** (rec["failures"] - 1)),
                  QUARANTINE_MAX_SECS)
    rec["quarantined_until"] = now_ms + backoff * 1000
    rec["last_error"] = str(why)[:300]
    return backoff


def note_success(rec, oc_fp, cc_fp, now_ms, action):
    rec["failures"] = 0
    rec.pop("quarantined_until", None)
    rec.pop("last_error", None)
    if oc_fp:
        rec["oc"] = oc_fp
    if cc_fp:
        rec["cc"] = cc_fp
    rec["last_sync"] = now_ms
    rec["last_action"] = action


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------

def backup(path, tag):
    """Copy a file aside before it is replaced.

    Kept because a merge is a judgement call: if the signature matching gets it
    wrong the original is still on disk, and one file per pair per tag keeps
    that bounded instead of growing without limit.
    """
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUPS, exist_ok=True)
    dst = os.path.join(BACKUPS, "%s.%s.bak" % (os.path.basename(path), tag))
    try:
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None


# --------------------------------------------------------------------------
# repair ladder
# --------------------------------------------------------------------------
# opencode validates imports strictly and reports only a JSON path, e.g.
# `at ["state"]["title"]` or `at ["time"]`. Rather than surface that to a human,
# the path is matched against known repairs and the import retried. Each rung
# gives up a little fidelity to gain a successful import; the last rung is
# plain text only, which always validates.

def _strip_reasoning(doc):
    changed = False
    for m in doc["messages"]:
        keep = [p for p in m["parts"] if p.get("type") != "reasoning"]
        if len(keep) != len(m["parts"]):
            m["parts"], changed = keep, True
    return changed


def _plain_tools(doc):
    """Turn tool parts into text. Loses structure, never fails validation."""
    changed = False
    for m in doc["messages"]:
        out = []
        for p in m["parts"]:
            if p.get("type") == "tool":
                st = p.get("state") or {}
                body = st.get("output") or st.get("error") or ""
                out.append({"type": "text",
                            "text": "[%s] %s\n%s" % (p.get("tool"),
                                                     _primary(st.get("input")),
                                                     body)[:4000],
                            "id": p["id"], "sessionID": p["sessionID"],
                            "messageID": p["messageID"]})
                changed = True
            else:
                out.append(p)
        m["parts"] = out
    return changed


def _drop_empty(doc):
    before = len(doc["messages"])
    doc["messages"] = [m for m in doc["messages"] if m["parts"]]
    return len(doc["messages"]) != before


REPAIRS = [
    # (matches error path, repair, description)
    (re.compile(r'\["state"\]'), _plain_tools, "flattened tool parts"),
    (re.compile(r"reasoning|\[\"time\"\]"), _strip_reasoning, "dropped reasoning"),
    (re.compile(r"."), _plain_tools, "flattened tool parts"),
    (re.compile(r"."), _strip_reasoning, "dropped reasoning"),
]


def import_with_repair(doc, log=None):
    """Import into opencode, adapting to whatever the validator objects to.

    Returns (ok, note). The point is that a schema change in a future opencode
    release degrades fidelity instead of stopping the sync.
    """
    ok, note = CC2OC.oc_import(doc)
    if ok:
        return True, "ok"
    tried = []
    for pattern, repair, desc in REPAIRS:
        if not pattern.search(note or ""):
            continue
        if desc in tried:
            continue
        if not repair(doc):
            continue
        _drop_empty(doc)
        tried.append(desc)
        if log:
            log("    retrying: %s (validator said %s)" % (desc, note.strip()))
        ok, note = CC2OC.oc_import(doc)
        if ok:
            return True, "ok after " + ", ".join(tried)
    return False, note


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

def merge_forward(cur, sess, opts, version, cc_path, log):
    """Rebuild the Claude transcript from opencode, then re-append the turns
    that only ever existed on the Claude side.

    Direction matters: opencode is the base and the Claude-only tail is
    appended, not the reverse, because opencode holds the full untruncated
    history while the Claude copy is already a lossy, size-capped rendering of
    it. Rebuilding from the lossy copy would bake that truncation in forever.

    Both sides are compared *in the same representation* -- the opencode side is
    rendered to Claude entries first, then signatures are taken from those.
    Comparing an opencode message against a Claude entry directly does not work:
    a message whose only content is an API error has no opencode parts at all,
    so it produces no signature, and its rendered "[opencode APIError] ..." text
    on the Claude side then looks like a Claude-only turn and gets duplicated.
    """
    sid = sess["id"]
    sess_uuid = OC2CC.session_uuid(sid)
    cwd = sess["directory"] or os.getcwd()

    # What the Claude side holds right now, before it is rebuilt.
    old_conv, _ = CC2OC.read_transcript(cc_path)

    # The opencode side, rendered exactly as a plain forward sync would.
    msgs = OC2CC.load_messages(cur, sid)
    b = OC2CC.Builder(sess_uuid, cwd, version, OC2CC.git_branch(cwd), opts)
    b.add(msgs)
    turns, _ = OC2CC.trim(b.turns, opts.max_bytes)
    base = [e for t in turns for e in t]

    base_sigs = {sig_claude(e) for e in base}
    base_sigs.discard(None)
    base_uuids = {e["uuid"] for e in base}

    # Claude-side entries with no counterpart in the rebuilt base. The uuid
    # check is a second line of defence: a deterministic id that already
    # appears in the base is the same entry however its text was rendered.
    extra = [e for e in old_conv
             if e["uuid"] not in base_uuids
             and sig_claude(e) not in base_sigs]

    if not extra:
        return None, "no Claude-only turns; plain forward"

    extra = _seal_pairs(extra)
    if not extra:
        return None, "Claude-only turns were unpaired tool calls; plain forward"

    backup(cc_path, "merge")

    # Re-stamp the appended entries onto this transcript. parentUuid is SET to
    # None rather than deleted: relink only rewrites keys that exist, so
    # popping it would leave the appended entries unlinked and Claude Code
    # reconstructs only the first turn ("none carry parentUuid links").
    for e in extra:
        e["sessionId"] = sess_uuid
        e["parentUuid"] = None
    entries = OC2CC.relink(base + extra)
    entries[0]["oc2cc"] = {"sourceSessionId": sid, "tag": OC2CC.TAG,
                           "importedAt": OC2CC.iso(None),
                           "merged": len(extra)}

    trailers = _trailers_for(sess, sess_uuid, entries)
    OC2CC.write_transcript(cc_path, entries, trailers, int(time.time() * 1000))
    log("    merged %d Claude-only entr%s into the transcript"
        % (len(extra), "y" if len(extra) == 1 else "ies"))
    return extra, "merged"


def merge_back(oc_id, extra, sess, log):
    """Append Claude-only turns to the REAL opencode session.

    Without this, a merge is only half done: the Claude transcript gains the
    union, but opencode never learns the turns that happened on the other side,
    so every later pass re-merges the same entries and the two stores never
    actually converge.

    It cannot go through cc2oc's normal path, for two reasons. cc2oc refuses a
    transcript bearing an oc2cc marker (correctly -- that is the loop guard),
    and it derives the opencode session id from the Claude uuid, which would
    mint a SECOND opencode session holding the same conversation. Both are
    avoided by taking opencode's own `opencode export` of the existing session
    as the base and appending to it, keyed by the original session id.

    Using the real export as the base also means opencode's serialisation is
    never second-guessed: whatever fields a future version adds survive,
    because they are copied through rather than reconstructed.
    """
    doc = _oc_export(oc_id)
    if doc is None:
        return False, "opencode export failed"

    existing = doc.get("messages") or []
    if not existing:
        return False, "export held no messages"

    last_id = existing[-1]["info"]["id"]
    base_ms = max(int((mm["info"].get("time") or {}).get("created") or 0)
                  for mm in existing) or int(time.time() * 1000)

    added = []
    for i, e in enumerate(extra):
        ts = CC2OC.ms_of(e.get("timestamp"), base_ms + (i + 1) * 1000)
        # Keep it strictly after the existing tail; opencode orders by
        # time_created, so an appended turn that claims an earlier timestamp
        # would be interleaved into the middle of the conversation.
        ts = max(ts, base_ms + (i + 1) * 1000)
        built = _oc_message(e, oc_id, ts, i, last_id, sess)
        if built is None:
            continue
        added.append(built)
        last_id = built["info"]["id"]

    if not added:
        return False, "nothing convertible to append"

    doc["messages"] = existing + added
    info = doc.get("info") or {}
    info["time"] = {"created": (info.get("time") or {}).get("created") or base_ms,
                    "updated": int(time.time() * 1000)}
    doc["info"] = info

    ok, note = import_with_repair(doc, log)
    if ok:
        log("    appended %d turn(s) to opencode session" % len(added))
    return ok, note


def _oc_export(oc_id):
    """`opencode export`, as the authoritative serialisation of a session."""
    import subprocess
    try:
        r = subprocess.run(["opencode", "export", oc_id],
                           capture_output=True, text=True, timeout=180)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        doc = json.loads(r.stdout)
    except Exception:
        # The wrapper prints gateway chatter on some paths; take the JSON body.
        start = r.stdout.find("{")
        if start < 0:
            return None
        try:
            doc = json.loads(r.stdout[start:])
        except Exception:
            return None
    return doc if isinstance(doc, dict) and "messages" in doc else None


def _oc_message(entry, oc_id, ts, seq, parent_id, sess):
    """One Claude entry -> one opencode export message."""
    kind = entry.get("type")
    if kind not in ("user", "assistant"):
        return None
    msg = entry.get("message") or {}
    content = msg.get("content")
    seed = entry.get("uuid") or "%s:%d" % (oc_id, seq)
    mid = CC2OC.oc_id("msg_", ts, 1, "back:m:" + seed)

    parts = []
    blocks = content if isinstance(content, list) else (
        [{"type": "text", "text": content}] if isinstance(content, str) else [])
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        pid = CC2OC.oc_id("prt_", ts, i + 2, "back:p%d:%s" % (i, seed))
        if b.get("type") == "text" and (b.get("text") or "").strip():
            parts.append({"type": "text", "text": b["text"][:20000],
                          "id": pid, "sessionID": oc_id, "messageID": mid})
        elif b.get("type") == "thinking" and (b.get("thinking") or "").strip():
            parts.append({"type": "reasoning", "text": b["thinking"][:20000],
                          "time": {"start": ts, "end": ts},
                          "id": pid, "sessionID": oc_id, "messageID": mid})
        elif b.get("type") == "tool_use":
            name = CC2OC.TOOL_MAP.get(b.get("name"), b.get("name") or "tool")
            parts.append({"type": "tool", "tool": name,
                          "callID": b.get("id") or ("call_" + pid[-12:]),
                          "state": {"status": "completed",
                                    "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                                    "output": "", "metadata": {"output": ""},
                                    "title": CC2OC._title_for(name, b.get("input")),
                                    "time": {"start": ts, "end": ts}},
                          "id": pid, "sessionID": oc_id, "messageID": mid})
    if not parts:
        return None

    model = (sess.get("model") or "")
    if isinstance(model, str) and model.startswith("{"):
        try:
            model = json.loads(model).get("id") or "claude-opus-5"
        except Exception:
            model = "claude-opus-5"
    model = (model or "claude-opus-5").rsplit("/", 1)[-1]

    if kind == "user":
        info = {"role": "user", "time": {"created": ts}, "agent": "build",
                "model": {"providerID": "claude", "modelID": model},
                "summary": {"diffs": []},
                "id": mid, "sessionID": oc_id}
    else:
        info = {"parentID": parent_id, "role": "assistant", "mode": "build",
                "agent": "build", "modelID": model, "providerID": "claude",
                "path": {"cwd": sess.get("directory") or os.getcwd(), "root": "/"},
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
                "time": {"created": ts, "completed": ts},
                "finish": "stop", "id": mid, "sessionID": oc_id}
    return {"info": info, "parts": parts}


def _seal_pairs(entries):
    """Drop a trailing assistant whose tool_results were left behind.

    An entry list cut at an arbitrary point can end with tool_use blocks whose
    tool_result lives in the next entry. Claude rejects that on resume, so the
    tail is walked back to the last complete pair.
    """
    while entries:
        last = entries[-1]
        msg = last.get("message") or {}
        c = msg.get("content")
        has_use = isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in c)
        if not has_use:
            break
        ids = {b["id"] for b in c
               if isinstance(b, dict) and b.get("type") == "tool_use"}
        # Its results would have to be in an entry we do not have.
        entries = entries[:-1]
        if not ids:
            break
    return entries


def _trailers_for(sess, sess_uuid, entries):
    title = (sess["title"] or "").strip()
    tr = [{"type": "mode", "mode": "normal", "sessionId": sess_uuid},
          {"type": "permission-mode", "permissionMode": "default",
           "sessionId": sess_uuid}]
    if title:
        tr.append({"type": "custom-title", "customTitle": title[:200],
                   "sessionId": sess_uuid})
    tr.append({"type": "tag", "tag": OC2CC.TAG, "sessionId": sess_uuid})
    last_user = next((e["uuid"] for e in reversed(entries)
                      if e.get("type") == "user"), None)
    if last_user:
        tr.append({"type": "last-prompt",
                   "lastPrompt": "(synced from opencode)",
                   "leafUuid": last_user, "sessionId": sess_uuid})
    return tr


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------

def build_pairs(cur):
    """Every session that exists on either side, keyed by a stable pair id.

    A pair is one conversation, however many copies of it exist. Both
    converters derive their output id from the input id, so the mapping is
    computable in both directions without storing it -- the ledger records
    state, not identity.
    """
    pairs = {}
    oc_sessions = OC2CC.list_sessions(cur)
    by_uuid = {}

    for s in oc_sessions:
        if s["parent_id"]:
            continue                      # subagent sessions, opt-in only
        title = (s["title"] or "").rstrip()
        if title.endswith("[claude]"):
            continue                      # came from Claude Code; that side owns it
        uuid = OC2CC.session_uuid(s["id"])
        by_uuid[uuid] = s
        pairs["oc:" + s["id"]] = {"kind": "oc", "oc": s, "uuid": uuid,
                                  "cc_path": _cc_path(s["directory"], uuid)}

    for t in CC2OC.scan_projects():
        if t["uuid"] in by_uuid:
            continue                      # already covered as an opencode pair
        conv, meta = CC2OC.read_transcript(t["path"])
        if not conv:
            continue
        if meta.get("tag") == OC2CC.TAG or conv[0].get("oc2cc"):
            continue                      # ours, and its opencode side is gone
        pairs["cc:" + t["uuid"]] = {"kind": "cc", "cc": t, "uuid": t["uuid"],
                                    "cc_path": t["path"]}
    return pairs


def _cc_path(directory, uuid):
    key = OC2CC.project_key(directory or os.getcwd())
    return os.path.join(PROJECTS, key, uuid + ".jsonl")


# --------------------------------------------------------------------------
# the sync pass
# --------------------------------------------------------------------------

class Opts:
    """Conversion knobs. Defaults match the standalone scripts."""
    max_bytes = 2 * 1024 * 1024
    max_tool_out = 4000
    max_text = 20000
    keep_reasoning = True
    keep_errors = True
    dry_run = False
    force = False

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def sync(only=None, dry_run=False, verbose=True, log=print):
    """One coordinated pass. Returns a summary dict.

    `only` restricts to a single session, given as either side's id, which is
    what the hooks use -- syncing one session after it finishes costs a
    fraction of a full sweep.
    """
    opts = Opts(dry_run=dry_run)
    led = load_ledger()
    now = int(time.time() * 1000)
    version = OC2CC.claude_version()

    cc_live = live_claude_sessions()
    oc_busy = opencode_running()

    con = OC2CC.connect()
    cur = con.cursor()
    pairs = build_pairs(cur)

    if only:
        pairs = {k: v for k, v in pairs.items()
                 if only in (k, v["uuid"], v.get("oc", {}).get("id"))
                 or k.endswith(":" + only) or v["uuid"].startswith(only)}
        if not pairs:
            log("  no session matching %s" % only)

    counts = {"forward": 0, "reverse": 0, "merge": 0, "noop": 0,
              "defer": 0, "fail": 0, "quarantine": 0}

    for pid, pair in sorted(pairs.items()):
        rec = led["pairs"].setdefault(pid, {})
        if quarantined(rec, now):
            counts["quarantine"] += 1
            if verbose:
                left = int((rec["quarantined_until"] - now) / 1000)
                log("  hold %s (%d failure(s), retry in %ds): %s"
                    % (_label(pair), rec.get("failures", 0), left,
                       rec.get("last_error", "")[:60]))
            continue

        oc = pair.get("oc")
        oc_fp = fp_opencode(cur, oc["id"]) if oc else None
        cc_fp = fp_claude(pair["cc_path"])

        # An opencode session touched moments ago is very likely the one the
        # user is in right now, whether or not a process shows up in /proc.
        oc_fresh = bool(oc and oc_busy
                        and (now - (oc["updated"] or 0)) < FRESH_SECS * 1000)
        cc_in_use = pair["uuid"] in cc_live

        action, why = decide(rec, oc_fp, cc_fp, oc_fresh, cc_in_use)

        if action == NOOP:
            counts["noop"] += 1
            continue
        if action == DEFER:
            counts["defer"] += 1
            if verbose:
                log("  defer %s (%s)" % (_label(pair), why))
            continue

        if dry_run:
            counts[action] += 1
            log("  DRY %-7s %s (%s)" % (action, _label(pair), why))
            continue

        try:
            ok = _apply(action, cur, pair, opts, version, log, verbose)
        except Exception as e:                       # noqa: BLE001
            ok, e_note = False, "%s: %s" % (type(e).__name__, e)
        else:
            e_note = None

        if ok:
            counts[action] += 1
            note_success(rec,
                         fp_opencode(cur, oc["id"]) if oc else None,
                         fp_claude(pair["cc_path"]),
                         now, action)
        else:
            counts["fail"] += 1
            backoff = note_failure(rec, e_note or "conversion failed", now)
            log("  FAIL %s (%s) -- holding %ds" % (_label(pair),
                                                   (e_note or "")[:70], backoff))

    con.close()
    if not dry_run:
        save_ledger(led)
    return counts


def _label(pair):
    oc = pair.get("oc")
    if oc:
        return "%s %s" % (oc["id"][:22], (oc["title"] or "")[:28])
    return "%s %s" % (pair["uuid"][:8], "(Claude Code)")


def _apply(action, cur, pair, opts, version, log, verbose):
    oc = pair.get("oc")

    if action == FORWARD:
        if not oc:
            return False
        return bool(OC2CC.convert_one(cur, oc, opts, version))

    if action == REVERSE:
        t = pair.get("cc") or {"uuid": pair["uuid"], "path": pair["cc_path"],
                               "key": os.path.basename(
                                   os.path.dirname(pair["cc_path"]))}
        c = CC2OC.convert(t, opts)
        if c is None:
            return False
        ok, note = import_with_repair(c["doc"], log if verbose else None)
        if verbose and ok and note != "ok":
            log("    imported with fallback: %s" % note)
        return ok

    if action == MERGE:
        if not oc:
            # No opencode side to merge against; the Claude copy is the only
            # source, so this is a plain reverse.
            return _apply(REVERSE, cur, pair, opts, version, log, verbose)
        extra, note = merge_forward(cur, oc, opts, version,
                                    pair["cc_path"], log)
        if extra is None:
            # Divergence was only apparent; a plain forward is correct and
            # cheaper than a merge.
            if verbose:
                log("    %s" % note)
            return bool(OC2CC.convert_one(cur, oc, opts, version))
        # The transcript now holds the union. opencode still does not, so the
        # Claude-only turns are appended to the REAL opencode session -- not
        # re-imported as a new one, which is what cc2oc would do.
        ok, back_note = merge_back(oc["id"], extra, oc, log)
        if not ok and verbose:
            log("    note: opencode-side append failed (%s); transcript still "
                "holds both sides" % str(back_note)[:70])
        # A failed append is not a failed sync: nothing was lost, and the next
        # pass retries. Reporting failure here would quarantine a pair whose
        # Claude side is correct.
        return True

    return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Coordinated two-way sync between opencode and Claude Code.")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and report, change nothing")
    ap.add_argument("--one", metavar="ID",
                    help="only this session (either side's id)")
    ap.add_argument("--quiet", action="store_true",
                    help="summary line only")
    ap.add_argument("--status", action="store_true",
                    help="show the ledger and what each pair would do")
    opts = ap.parse_args()

    if opts.status:
        led = load_ledger()
        pairs = led.get("pairs", {})
        print("ledger: %s (%d pair(s))" % (LEDGER, len(pairs)))
        held = [(k, v) for k, v in pairs.items() if v.get("quarantined_until")]
        for k, v in sorted(pairs.items()):
            mark = "HOLD" if v.get("quarantined_until") else v.get("last_action", "-")
            print("  %-6s %-42s %s" % (mark, k[:42], v.get("last_error", "")[:40]))
        if held:
            print("\n%d pair(s) on hold; they retry automatically." % len(held))
        return

    log = (lambda *a: None) if opts.quiet else print
    counts = sync(only=opts.one, dry_run=opts.dry_run,
                  verbose=not opts.quiet, log=log)
    parts = ["%s=%d" % (k, v) for k, v in counts.items() if v]
    print("aisync: %s%s" % (", ".join(parts) or "nothing to do",
                            " (dry run)" if opts.dry_run else ""))


if __name__ == "__main__":
    main()
