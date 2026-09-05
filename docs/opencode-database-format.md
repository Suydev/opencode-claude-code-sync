# opencode SQLite database — field notes

Written 2026-08-30 while recovering session `ses_fc1330eaaffezmgFKQzb1JOQgD`
after a failed compaction truncated its context to nothing.

Extended 2026-09-05 with the id scheme, the export/import contract, and the
plugin-loading breakage found while building two-way session sync (§8–§11).

Verified against opencode **1.17.9** on Termux/aarch64. Behaviour was confirmed
by reading strings out of `opencode.bin` and by replaying the real algorithm in
Python against a backup copy. Anything I did not verify is marked as such.

Companion to [`claude-code-transcript-format.md`](claude-code-transcript-format.md),
which covers the other side.

---

## 1. Where things live

| Path | What |
| --- | --- |
| `~/.local/share/opencode/opencode.db` | everything: sessions, messages, parts, todos, auth |
| `~/.local/share/opencode/opencode.db-wal` | write-ahead log; can hold megabytes of *uncommitted-to-main* data |
| `~/.local/share/opencode/opencode.db-shm` | shared-memory index for the WAL |
| `~/.local/share/opencode/auth.json` | provider credentials (not in the DB) |
| `~/.config/opencode/opencode.jsonc` | user config |
| `~/.config/opencode/{agent,command,skill}/` | agents, commands, skills (all singular — see §11) |
| `~/.local/share/opencode/log/opencode.log` | the log; the only place plugin load errors appear |
| `/data/data/com.termux/files/usr/libexec/opencode/opencode.bin` | the real binary (~142 MB); `bin/opencode` is a shell wrapper |

The DB is in WAL mode. **Never copy `opencode.db` alone with `cp`** — you will
get a torn snapshot missing whatever is still in the WAL. Use SQLite's online
backup API instead (see §6).

All running opencode processes share this one file. Check who has it open:

```sh
pgrep -af opencode.bin        # the wrapper's own name will not match
```

---

## 2. Tables that matter

Full schema: `select name, sql from sqlite_master where type='table'`.

### `session`

One row per session. Key columns:

- `id` — `ses_...`, and the id itself encodes its creation time (see §8)
- `title`, `slug`, `directory`
- `path` — the directory again, **without a leading slash**
  (`data/data/com.termux/files/home`). The export format carries both.
- `project_id` — `"global"` on this install; joins `project.id`
- `parent_id` — set on subagent sessions, `NULL` on top-level ones. This is the
  only reliable way to tell a subagent session from a real one.
- `agent`, `model` (JSON: `{"id":...,"providerID":...,"variant":...}`)
- `revert` — set when you use the revert feature; `NULL` otherwise
- `time_created`, `time_updated`, `time_compacting`, `time_archived`
- token/cost counters

Timestamps are **milliseconds** since epoch:
`datetime.fromtimestamp(v/1000)`.

`metadata` exists in the schema but is `NULL` for every row here, including
rows created through `opencode import` with a `metadata` key supplied — the
importer drops it (§9).

### `message`

One row per turn. `data` is a JSON blob; the interesting keys:

- `role` — `"user"` or `"assistant"`
- `parentID` — assistant messages point at the user message they answer
- `mode`, `agent` — e.g. `"build"`, or `"compaction"` for summary turns
- `modelID`, `providerID`
- `summary` — **overloaded, see the trap in §4**
- `finish` — `"stop"` etc.; absent if the turn never completed
- `error` — `{name, data:{message, statusCode, ...}}` when the call failed
- `tokens`, `cost`, `time`

### `part`

The actual content, one row per fragment, `message_id` → `message.id`.
`data.type` is one of:

| type | payload |
| --- | --- |
| `text` | `text` — visible prose |
| `reasoning` | `text` — thinking blocks, plus `time{start,end}` (**required**, §9) |
| `tool` | `tool`, `callID`, `state{status,input,output,metadata,title}` |
| `step-start` / `step-finish` | turn bookkeeping, `tokens`, `cost` |
| `compaction` | `auto`, `overflow`, `tail_start_id` — **the context boundary** |

Observed distribution in this DB (~18k messages): `step-start` 12430,
`step-finish` 12199, `tool` 11181, `text` 8203, `reasoning` 7466,
`compaction` 20. The bookkeeping parts outnumber the content.

`state.status` is one of `completed`, `error`, `running`, `pending`. The last
two persist when opencode exits mid-call — 9 rows here — and matter to any
exporter, because a tool call with no result is invalid on the Claude Code side
and must be given a synthetic one.

Key set differs by status, and both shapes are enforced on import (§9):

```
completed -> {input, metadata, output, status, time, title}
error     -> {error, input, status, time}   (+ sometimes metadata, raw)
```

Tool names are lowercase (`bash`, `read`, `write`, `edit`, `grep`, `glob`,
`webfetch`, `websearch`, `task`, `todowrite`, `skill`, `question`), against
Claude Code's TitleCase. Argument names differ too — `filePath` vs `file_path`,
`oldString` vs `old_string`, `include` vs `glob` — so a converter needs a real
mapping table, not a case transform.

`part` has `ON DELETE CASCADE` from `message`, but only if you
`PRAGMA foreign_keys=ON` (SQLite defaults to OFF). Delete parts explicitly to
be safe.

### `todo`

Flat rows keyed `(session_id, position)`: `content`, `status`, `priority`.
This is what the TodoWrite tool writes. Survives compaction — it is not part
of the message stream.

```sql
select position, status, priority, content
from todo where session_id = 'ses_...' order by position;
```

### `session_message`

Despite the name, **not** the conversation. Only sparse control events —
in the session I examined, 19 rows, all `model-switched`. Don't confuse it
with `message`.

### `event` / `event_sequence`

Append-only event log, `aggregate_id` = session id. ~25k rows for a large
session, almost all `message.part.updated.1` / `message.updated.1`. The TUI
reads from `message`/`part`, not from here. Rows referencing deleted messages
appear to be harmless — *I did not verify this deeply.*

### `session_context_epoch`

Exists in the schema, **empty in practice** (0 rows across all 13 sessions).
Do not assume it controls the context window; it does not, at least not in
1.17.9.

### `session_input`, `workspace`

Both empty here (0 rows) across 77 sessions. `session_input` looks like a
prompt-queue table (`prompt`, `delivery`, `admitted_seq`, `promoted_seq`);
`workspace` is referenced by `session.workspace_id`, which is also always
`NULL`. Neither is load-bearing on this install.

### `project`, `project_directory`

`project` has exactly one row here: `id="global"`, `worktree="/"`. Every
session's `project_id` points at it, and `project_directory` is empty. So the
`project` table is not how directories are tracked — `session.directory` is.

---

## 3. How the context window is actually decided

This is the important part, and it is not obvious.

Compaction does **not** delete messages. Everything stays in the DB forever.
What changes is where opencode starts reading. The mechanism:

1. A compaction writes a **synthetic user message** whose only part is
   `{"type":"compaction","auto":<bool>,"tail_start_id":"msg_..."}`.
2. Immediately after, an **assistant message with `summary: true`** and
   `parentID` pointing at that synthetic message holds the summary prose.
3. On load, opencode walks messages **newest → oldest** and stops at the
   newest valid compaction trigger. `tail_start_id` names the oldest message
   replayed verbatim after the summary.

Reimplemented from the binary (function `q6`), the walk is:

```
K = set()          # parentIDs of valid summaries
Y = None           # tail_start_id we are hunting for
for X in messages_newest_first:
    emit X
    if Y is not None:
        if X.id == Y: stop
        else: continue
    if X.role == "user" and X.id in K:
        U = first part of X with type == "compaction"
        if U is None: continue
        if not U.tail_start_id: stop
        Y = U.tail_start_id
        if X.id == Y: stop
        continue
    if X.role == "assistant" and X.summary is True
       and X.finish and not X.error:
        K.add(X.parentID)
```

then a reorder puts `[trigger..summary] + [tail_start..trigger) + rest` so the
summary is read before the replayed tail.

Two consequences worth internalising:

- A summary message is only honoured if `finish` is set **and** `error` is
  absent. A failed compaction should therefore be ignored…
- …but the **trigger** message is checked independently of whether its summary
  succeeded. So a trigger written for a compaction that then died still moves
  the boundary. That is the failure mode described in §5.

---

## 4. Trap: `summary` means two different things

```
message.data.summary == {"diffs": [...]}   # ordinary user message, diff stats
message.data.summary == true               # this IS a compaction summary
```

Naive filtering on `"summary" in data` matched 256 messages in my session when
only 4 were real compactions. Always test `is True`.

Find real compactions:

```sql
-- triggers (the thing that moves the boundary)
select m.id, m.time_created, p.data
from part p join message m on m.id = p.message_id
where p.session_id = 'ses_...'
  and json_extract(p.data,'$.type') = 'compaction';
```

```sql
-- summaries
select id, time_created, json_extract(data,'$.modelID')
from message
where session_id = 'ses_...'
  and json_extract(data,'$.summary') = json('true');
```

---

## 5. The failure mode: a dead compaction eats your context

What happened, exactly:

```
08-28 21:49  trigger + summary (opus)     OK — 16,962-char summary
08-30 12:00  audio MIME edit to server.mjs
08-30 12:09  user ".."  -> APIError (claude-opus-5)
08-30 12:12  auto-compaction fires on glm-5.3
             trigger written: tail_start_id = the 12:09 user message
             summary FAILS: 401 "该令牌已过期" (agentrouter token expired)
             row still persisted, with error, summary:true
08-30 12:17  manual retry on opus succeeds — but it summarised a context
             already truncated by the 12:12 trigger, so it emitted
             "(none)" for every section
```

Resulting context: **52 messages**, starting 12:12:14. The prior 807 messages —
including all the audio MIME work — were unreachable, though never deleted.

Fix: delete the orphaned trigger message. Simulated on a backup first:

```
CURRENT                     ctx=  52  earliest 08-30 12:12:14  mime=False
delete trigger              ctx= 858  earliest 08-28 21:49:06  mime=True
delete trigger+glm          ctx= 857  earliest 08-28 21:49:06  mime=True
delete trigger+glm+opus     ctx= 856  earliest 08-28 21:49:06  mime=True
delete glm+opus (no trig)   ctx= 857  earliest 08-28 21:37:58  mime=True
```

Note the last line: removing only the *summaries* and leaving the trigger still
"works" because `tail_start_id` is then chased from the older compaction — but
it is the trigger that must go. Deleting the two dead summaries as well is
cosmetic, so the empty `(none)` text stops being rendered.

**The general lesson:** if a compaction errors out, check for an orphaned
trigger part before doing anything else. Symptom is an assistant that has
forgotten everything while `select count(*) from message` is still huge.

---

## 6. Safe recipes

### Read-only query (safe while opencode runs)

```python
con = sqlite3.connect(
    "file:" + os.path.expanduser("~/.local/share/opencode/opencode.db")
    + "?mode=ro", uri=True)
```

### Consistent backup (safe while opencode runs)

```python
src = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
dst = sqlite3.connect(BACKUP)
src.backup(dst)          # online backup API, WAL-aware
dst.close(); src.close()
sqlite3.connect(BACKUP).execute("pragma integrity_check").fetchone()
```

Took 1.4 s for 126 MB. `cp` is not equivalent.

### Writing

Quit **every** opencode process first (`pgrep -af opencode`). A live process
holds its own state in memory and will overwrite your edits on exit. Then:

```python
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA busy_timeout=30000")
con.execute("PRAGMA foreign_keys=ON")
with con:                      # transaction
    con.execute("delete from part where message_id=?", (mid,))
    con.execute("delete from message where id=?", (mid,))
```

Restore is a plain file copy of the backup over the DB — remember to delete
stale `-wal` / `-shm` alongside it.

### Export a transcript

Join `message` → `part` ordered by `time_created, id`, and render `text`,
`reasoning`, and `tool` (`state.input.command` + `state.output`) parts. 145
messages produced 227 KB of Markdown. Useful before any destructive edit,
since it survives regardless of whether the DB surgery works.

---

## 7. The id scheme — `ses_` / `msg_` / `prt_`

Every id is `<prefix>` + 12 hex chars + 14 random base62 chars. The hex is a
48-bit field built from the creation time:

```
t = ((ms - 1786706395136) << 12) | low            # low 12 bits, always 1 here
ses_  ->  t is BIT-COMPLEMENTED:  ((1<<48)-1) - t     # descending
msg_  ->  t as-is                                     # ascending
prt_  ->  t as-is                                     # ascending
```

The epoch constant `1786706395136` is `13 * 2**37`. Verified by recovering
`time_created` from real ids: every session id landed within 3 ms of its row
timestamp and most were exact, message ids within 8 ms — the id is stamped just
before the row is written, so a small negative delta is normal.

Session ids descend so that a plain lexicographic sort puts the newest first;
message and part ids ascend so a sort restores conversation order. That is why
`ses_fc13...` (August) sorts *before* `ses_f8fa...` (September).

For an importer this matters twice over. Ids must be *generated*, not invented
— a random string in the hex field yields a nonsense timestamp — and if the
random tail is derived deterministically from the source id (e.g. a hash),
re-importing the same session **updates in place** instead of creating a
duplicate. Verified: importing the same document twice left exactly one session
and one copy of each message.

```python
EPOCH = 1786706395136
B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase

def oc_id(prefix, ms, counter, seed, descending=False):
    t = ((ms - EPOCH) << 12) | (counter & 0xfff)
    if descending:
        t = ((1 << 48) - 1) - t
    tail = "".join(B62[b % 62] for b in hashlib.sha256(seed.encode()).digest()[:14])
    return "%s%012x%s" % (prefix, t, tail)
```

---

## 8. `opencode export` / `opencode import`

These are the supported way in and out, and they are worth using instead of
writing SQLite directly: the DB is live and WAL-mode, and the importer owns id
allocation, project rows, and event bookkeeping.

```sh
opencode export <sessionID> > s.json      # also: --sanitize to redact
opencode import s.json                    # prints "Imported session: ses_..."
```

The document shape:

```json
{
  "info": { "id": "ses_...", "slug": "...", "projectID": "global",
            "directory": "/abs/path", "path": "abs/path",
            "title": "...", "agent": "build",
            "model": {"id": "...", "providerID": "..."},
            "version": "1.17.9",
            "summary": {"additions":0,"deletions":0,"files":0},
            "cost": 0,
            "tokens": {"input":0,"output":0,"reasoning":0,
                       "cache":{"read":0,"write":0}},
            "time": {"created": <ms>, "updated": <ms>} },
  "messages": [ { "info": {...}, "parts": [ {...} ] } ]
}
```

`import` is **idempotent on id** — same id, no duplicate — which makes it safe
to drive from a sync loop.

---

## 9. Import validation: the exact required keys

The importer validates strictly and reports only a JSON path, e.g.
`Missing key at ["parentID"]`. No line number, no field name in context. Each
of these cost a round trip to discover:

- **user** messages need a nested `model: {providerID, modelID}` **and**
  `summary: {diffs: []}`.
- **assistant** messages need `parentID`, and `modelID` / `providerID` as
  **flat** keys — not the nested `model` object the user form uses. The two
  roles genuinely differ.
- **`reasoning` parts** need `time: {start, end}`. Omitting it fails with a
  bare `at ["time"]`, which looks like a message-level problem and is not.
- **completed `tool` parts** need `state.title`. Omitting it fails with
  `at ["state"]["title"]`. Error-status tool parts must *not* carry one; they
  take `state.error` instead.

Anything unrecognised is **dropped silently**. A custom marker key placed on a
message or a part does not survive the round trip:

```
in:  message.info.cc2oc = {...}   ->  out: keys are [agent, model, role, summary, time]
in:  part.cc2oc = {...}           ->  out: keys are [text, type]
in:  info.metadata = {...}        ->  out: session.metadata IS NULL
```

So provenance has to live somewhere the importer keeps. The session **title**
is the only practical place — hence the `[claude]` suffix convention used by
the sync tooling, which doubles as its loop guard.

A failed import writes nothing, so a validation error is safe, just opaque.
Worth building a repair ladder that reads the reported path and retries with
the offending shape simplified (flatten tools to text, drop reasoning) rather
than surfacing the raw error.

---

## 10. Subagent sessions

A `task` tool call spawns a real, separate session. The link is in the parent's
tool part, not in the child:

```json
{"type":"tool","tool":"task",
 "state":{"metadata":{"parentSessionId":"ses_A","sessionId":"ses_B",
                      "model":{"modelID":"...","providerID":"..."}}}}
```

and the child row carries `parent_id = ses_A`. Of 77 sessions here, 39 were
subagents — over half. Titles are auto-generated as
`"<description> (@<agent> subagent)"`.

Any tool that lists or syncs sessions should filter on `parent_id IS NULL` by
default, or the picker on the other side fills up with research fragments.

---

## 11. Local plugins do not load in 1.17.9 (on this build)

The docs describe `~/.config/opencode/plugins/` (plural) for local plugin
files. That directory is **not scanned**. Neither is the singular form, nor a
project-level `.opencode/plugins/`. Verified with a plugin that appends to a
file on init and on every event: the file was never created.

Note the asymmetry — every *other* config directory here is **singular**:
`~/.config/opencode/agent/`, `command/`, `skill/`. There is no `plugin/`
equivalent that works either; I tried it.

Specs in the config `plugin` array are resolved by `SE()`, which accepts
`file://`, a leading `.`, and bare paths:

```js
function yI(I){ return I.startsWith("file://") || I.startsWith(".") || UU(I) }
```

All three forms **fail at load** on this build:

```
level=ERROR message="failed to load plugin"
  path=file:///.../probe.js error="ENOENT: no such file or directory, open"
```

The file exists and is readable from the shell. npm-published plugins load
fine (`opencode-agentrouter` does), so the mechanism works — local file
resolution is what is broken. Plausibly a Bun `/$bunfs/` path-interception
issue on Android, which the launcher script already works around for other
native libraries; *I did not confirm that root cause.*

Practical consequence: **there is no usable in-process exit hook.** For
anything that must run when a session ends, wrap the binary instead —

```sh
"$REAL_OPENCODE" "$@"; rc=$?
setsid <your-hook> >/dev/null 2>&1 &
exit "$rc"
```

which is arguably better placed anyway: it also fires when opencode crashes or
Android's low-memory killer takes it, which an in-process hook would miss.

Plugin load errors appear **only** in `~/.local/share/opencode/log/opencode.log`
and only with `--print-logs`. Nothing surfaces in the TUI.

---

## 12. Things I did not verify

- Whether stale `event` rows pointing at deleted messages ever cause trouble.
- What `session_context_epoch` is for, given it is always empty here.
- Whether the TUI caches anything outside the DB that could resurrect a
  deleted row.
- Whether `compaction.prune` (referenced in the binary, config key
  `compaction.prune`) physically removes parts. It was not enabled here.
- Why local plugin files fail to load (§11). The Bun-on-Android path-interception
  theory fits the launcher's existing workarounds but is unconfirmed.
- Whether `session_input` / `workspace` are used at all, or are forward-looking
  schema. Both are empty across 77 sessions here.
- What the low 12 bits of an id are for (§7). All 400 message ids I decoded had
  the value 1, so a collision counter is a guess; nothing here exercises it.
