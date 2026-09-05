# Claude Code transcript store — field notes

Written 2026-09-05 while building two-way session sync between opencode and
Claude Code on Termux/aarch64.

Verified against Claude Code **2.1.252** (native build, `linux-arm64`). Facts
here came from reading strings and function bodies out of the binary and from
probing real behaviour — where a claim rests on only one of those, it says so.
Anything I did not verify is marked as such.

Companion to [`opencode-database-format.md`](opencode-database-format.md),
which covers the other side. The two
formats differ in kind: opencode keeps one SQLite database, Claude Code keeps
append-only JSONL files.

---

## 1. Where things live

| Path | What |
| --- | --- |
| `~/.claude/projects/<projectKey>/<sessionId>.jsonl` | the transcript — one session per file |
| `~/.claude/projects/<projectKey>/<sessionId>/subagents/agent-<id>.jsonl` | subagent (sidechain) transcripts |
| `~/.claude/projects/<projectKey>/memory/` | per-project memory |
| `~/.claude/sessions/<pid>.json` | live session registry (see §7) |
| `~/tmp/cc-socks/<pid>.sock` | cross-session messaging socket |
| `~/.claude/settings.json` | user settings, `env`, `hooks` |
| `~/.claude.json` | trust decisions, per-project counters, MCP servers |
| `~/.claude/history.jsonl` | flat prompt history, unrelated to transcripts |
| `~/.local/share/claude/versions/<x.y.z>` | the binaries themselves (~205 MB each) |

`sessionId` **must** be a UUID. The loader validates against
`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`
and silently ignores any other filename. This is why importing an opencode
session cannot reuse its `ses_...` id — a file named `ses_*.jsonl` sits there
forever, read by nothing.

---

## 2. The projectKey — the thing that breaks imports

A working directory maps to a folder name by replacing **every** character that
is not `[a-zA-Z0-9]` with `-`:

```js
function k(e) { return e.replace(/[^a-zA-Z0-9]/g, "-") }
function gE(e) { let n = k(e); if (n.length <= 200) return n
                 return `${n.slice(0,200)}-${he(e)}` }
function he(e) { return Math.abs(vK(e)).toString(36) }        // djb2-ish, base36
function vK(t) { let e=0; for (...) e = (e<<5)-e+t.charCodeAt(r)|0; return e }
```

So `/data/data/com.termux/files/home` becomes:

```
-data-data-com-termux-files-home
```

Note the **dot in `com.termux` becomes a dash**. An importer that only replaces
`/` produces `-data-data-com.termux-files-home`, a folder Claude Code never
reads for this cwd. That single character was why an earlier import of 13
sessions was invisible for days: the files were valid, the directory was not.

Over 200 characters the name is truncated and a base36 hash of the *original*
path is appended, so two long sibling paths cannot collide.

---

## 3. Transcript file shape

One JSON object per line, appended. Line order matters for reconstruction; the
file is never rewritten in place by Claude Code itself.

Entry types seen in the wild:

| `type` | Role |
| --- | --- |
| `user` | a user turn, or a tool-result carrier (see §4) |
| `assistant` | a model turn |
| `attachment` | injected context: agent listings, skill listings, token reminders |
| `system` | slash-command echoes, compact boundaries, API errors |
| `progress` | streaming bookkeeping |
| `file-history-snapshot` | file backups for `--rewind` |
| `mode` / `permission-mode` | session mode records |
| `custom-title` / `ai-title` / `tag` | display metadata (see §6) |
| `last-prompt` | pointer to the leaf entry |
| `cost-state` | cumulative spend and token counters |
| `queue-operation` | prompt queue enqueue/dequeue |
| `atis-latch` | internal latch state |

A conversational entry carries at minimum:

```json
{
  "parentUuid": "<uuid or null>",
  "isSidechain": false,
  "userType": "external",
  "entrypoint": "cli",
  "cwd": "/data/data/com.termux/files/home",
  "sessionId": "<uuid>",
  "version": "2.1.252",
  "type": "user",
  "uuid": "<uuid>",
  "timestamp": "2026-08-29T07:42:16.135Z",
  "gitBranch": "HEAD",
  "message": { "role": "user", "content": [ ... ] }
}
```

Unknown top-level keys survive — the entry schema is `passthrough()`, not
strict. This is how an importer can stamp its own provenance marker on the first
entry and find it again later.

### `parentUuid` threads through non-conversational entries

`attachment` and `system` entries take part in the chain. A valid transcript
looks like:

```
user f0076ce7        parent=null
attachment 8d7c768e  parent=f0076ce7
attachment 504cb4fa  parent=8d7c768e
assistant cf49e44e   parent=504cb4fa
```

Any integrity check that tracks only `user`/`assistant` uuids will report a
correct native transcript as broken. I wrote that bug and it produced 174
false failures before I noticed.

If parent links are missing or dangle, Claude Code warns on resume and
reconstructs only what it can reach:

```
Warning: Resume transcript has 4 user/assistant records but none carry
parentUuid links; only 1 reached the resumed conversation.
```

---

## 4. Tool calls

A tool call is a `tool_use` block inside an **assistant** `message.content`.
Its result is a `tool_result` block inside a following **user** entry:

```json
{"type":"tool_use","id":"toolu_...","name":"Bash","input":{"command":"ls"}}
{"type":"tool_result","tool_use_id":"toolu_...","content":"a\nb\n","is_error":false}
```

Two rules that matter for anything writing transcripts:

- **Every `tool_use` needs a matching `tool_result`.** An unpaired one makes the
  resumed request invalid at the API. Trim on turn boundaries, never mid-turn.
- **Results do not strictly alternate.** Claude Code fires several tool calls
  before collecting any results, so pending ids accumulate across consecutive
  assistant entries. Verify by set membership per id, not by expecting
  `use → result → use → result`.

Empty `text` blocks (`{"type":"text","text":""}`) are also rejected on resume.
Native transcripts do contain them, but only in positions the loader tolerates;
do not emit them.

---

## 5. Trap: a slash in `message.model` hangs `--resume` forever

This one cost hours. Two transcripts identical except for the model string:

```
"model": "claude-opus-5"           -> resumes, replies in ~40s
"model": "gateway/claude-opus-5"   -> hangs indefinitely, killed at timeout
```

Bisected with six single-variable probe transcripts. It is not model
recognition — an unknown bare id like `glm-5.3` resumes fine, and so does the
sentinel `<synthetic>`. It is specifically the `provider/model` shape.

Consequence for importers: strip to the last path segment. Some source ids
carry a slash natively (`minimax/minimax-m2.7:free`), so sanitise the value
rather than trusting it:

```python
model.rsplit("/", 1)[-1] or "<synthetic>"
```

*Verified by probe; I did not find the parsing site in the binary, so the
mechanism is unconfirmed — only the symptom and the fix.*

---

## 6. Titles, tags, and what the picker shows

Resolution order for the label in `/resume`:

```js
q(s.customTitle) || q(s.aiTitle) || undefined
```

so `custom-title` beats `ai-title`. Both are separate trailer lines, not fields
on a message:

```json
{"type":"custom-title","customTitle":"My session","sessionId":"<uuid>"}
{"type":"ai-title","aiTitle":"Generated title","sessionId":"<uuid>"}
{"type":"tag","tag":"opencode","sessionId":"<uuid>"}
```

`tag` renders as a `[tag]` prefix in the picker — useful for marking imports.

The preview line is the first non-meta user message, via `Hpt()`, which skips
`isMeta`, skips compact summaries, unwraps `<command-name>` blocks, and skips
built-in command names. Message **count** is `s$e()`: user entries with
non-empty text/image/document content, plus assistant entries with non-empty
text. Tool-only turns do not count.

The picker sorts by file **mtime** and shows it as the session date. An importer
that leaves mtime at "now" makes every session look like it happened this
second — `os.utime` it to the real conversation time.

---

## 7. The live session registry, and why you cannot trust it

Every session writes `~/.claude/sessions/<pid>.json`:

```json
{
  "pid": 21702,
  "sessionId": "4889b138-...",
  "cwd": "/data/data/com.termux/files/home",
  "startedAt": 1788530679330,
  "version": "2.1.252",
  "peerProtocol": 1,
  "peerFeatures": ["notify_idle","reply_across_default_dirs","artifact_yield"],
  "kind": "interactive",
  "messagingSocketPath": ".../tmp/cc-socks/21702.sock",
  "name": "home-73",
  "nameSource": "derived"
}
```

**These files outlive their processes.** On this device six of seven entries
were stale, left by runs the low-memory killer took. Check
`/proc/<pid>` before believing any of them; stale entries also leave a peer
listed in `ListAgents` that can never answer.

`name` is what `SendMessage` addresses. It is derived from the cwd unless
`--name` is passed.

---

## 8. Cross-session messaging

It works, and it is on by default. Gate:

```js
function Jo() {
  let e = env.CLAUDE_CODE_HARBOR_KITE
  if (e !== undefined) return Le(e)
  if (platform === "windows" && !I("tengu_harbor_kite_win", true)) return false
  return I("tengu_harbor_kite", true)        // <- second arg is the DEFAULT
}
```

`ListAgents` and `SendMessage` both use `isEnabled(){ return Jo() }`. Verified
end to end: two sessions, one addressed the other by name, and the message
arrived in the recipient's transcript as a `queue-operation` wrapped in
`<cross-session-message from-name="peer-beta">`, which the recipient then
mentioned unprompted.

**Misreading to avoid.** `ListAgents`' input schema describes two optional
fields, `channel` and `q`, as `"Not available in this build; leave unset."`
That is about those two filter parameters, not the tool. Separately,
`"Cross-session messaging is not available in this session."` is the runtime
error for when the gate is *off* — not a statement that it is off. I concluded
the feature was disabled from those two strings and was wrong.

*Agent teams* is a different feature and is gated: needs
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` **and** the `tengu_amber_flint` gate.
`--agent-teams` is not a recognised flag in 2.1.252 (`error: unknown option`).

---

## 9. Retention — the sweep will delete your imports

`cleanupPeriodDays` defaults to **30**. The sweep walks the projects tree and
`rm`s anything whose mtime is older than the cutoff. Since imports are
backdated to the original conversation time (§6), a freshly imported year-old
session is **already past the cutoff** and can be deleted on the next launch.

Set it high:

```json
{ "cleanupPeriodDays": 3650 }
```

Minimum is 1; `0` is rejected for this key. To stop transcript writes entirely
the flag is `--no-session-persistence`, not `0`.

Cleanup is skipped, with a message, when settings cannot be parsed or when
`userSettings` is disabled via `--setting-sources` — so a broken settings file
fails safe.

Related caps found in the binary, none of which I hit in practice:
`52428800` bytes (tombstone-removal size limit), `200000` entries,
`630784` bytes per line for cross-session messages.

---

## 10. Resume classification

On resume, the tail of the transcript is classified, and the result decides
whether Claude Code answers your prompt or injects one of its own. If the tail
looks like an interrupted turn it injects `"Continue from where you left off."`
(overridable via `CLAUDE_CODE_RESUME_PROMPT`) and can reply
`"No response requested."` with `model: "<synthetic>"`.

I hit this loop while the model-slash bug (§5) was still present, and misread it
as the cause. It was a symptom. Worth knowing the mechanism exists so the same
mistake is not made twice.

`system` entries with `subtype: "compact_boundary"` mark where a compaction
happened; the loader slices at the newest one (`KOe`/`il`). Its
`compactMetadata` carries `trigger`, `preTokens`, `postTokens`,
`preservedSegment`, `preservedMessages`. Do **not** synthesise one of these
without real metadata — it changes what gets replayed.

---

## 11. Termux/aarch64 specifics

### The binary is glibc; Termux is bionic

The launcher must `patchelf --set-interpreter` to Termux's glibc runner
(`~/.local/share/claude/versions/<v>` are patched in place) and must unset
`LD_PRELOAD` before exec, or the bionic `libtagfix`/`libtermux-exec` preloads
are inherited by a glibc process. Symptom if the ELF interpreter is right but
the preload is wrong:

```
error while loading shared libraries: libdl.so: cannot open shared object file
```

### …but unsetting LD_PRELOAD breaks the Bash tool

`libtermux-exec-ld-preload.so` is also what rewrites shebangs. Claude Code's
Bash tool spawns a **hardcoded** `/bin/bash`:

```js
W.spawn("/bin/bash", ["--noprofile","--norc"], {...})
```

and Termux has no `/bin/bash` or `/usr/bin/env`. Without the shim any script
starting `#!/usr/bin/env …` or `#!/bin/bash` dies:

```
/usr/bin/env: bad interpreter: No such file or directory     (exit 126)
```

That kills `npx`, pip entry points, git hooks, and most project scripts.

Fix: give Claude Code a shell of its own that restores the preload and hands
off, then point `CLAUDE_CODE_SHELL` at it. The detection function `uIn()`
accepts any path containing `bash` or `zsh` that passes a `--version` check,
falling back to `$SHELL`, then `Ka("bash")`, then
`/bin`, `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`.

```sh
#!/data/data/com.termux/files/usr/bin/bash
export LD_PRELOAD=/data/data/com.termux/files/usr/lib/libtermux-exec-ld-preload.so
exec /data/data/com.termux/files/usr/bin/bash "$@"
```

### Updater hazards

The stale-`.tmp` sweep and the 15-minute lock steal both live **inside** the
one-updater lock, and only run when an update check is due. A download killed
mid-flight (routine here — Android's LMK takes a 205 MB download readily) leaves
both a partial `.tmp` and a held `.update.lock` that can sit for a day. Sweep
unconditionally, before taking the lock.

The wrapper's `smoke_test` runs `--init-only` in an isolated `HOME` and
distinguishes *definitely crashed* (fatal signal, or a Bun crash banner) from
*inconclusive* (timeout), blocklisting only the former. On a healthy build here
`--init-only` returns 0 in 1–2 s.

---

## 12. Hooks

`SessionEnd` fires on exit and receives JSON on stdin including `session_id`,
`transcript_path`, `cwd`, and `reason`. Configured in `settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [ { "type": "command",
                     "command": "/abs/path/hook.sh",
                     "timeout": 10 } ] }
    ]
  }
}
```

It runs during shutdown under a timeout, so anything slow must detach
(`setsid … &`) and exit 0. Known event names:
`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`,
`Stop`, `SubagentStop`, `PreCompact`, `Notification`.

---

## 13. Safe recipes

### Read a transcript

```python
entries = [json.loads(l) for l in open(path, errors="ignore") if l.strip()]
conv = [e for e in entries if e.get("type") in ("user", "assistant")]
```

### Validate one before trusting it to resume

```python
uses, results, seen = {}, set(), set()
for e in entries:
    if e.get("type") not in ("user","assistant","attachment","system","progress"):
        continue
    pu = e.get("parentUuid")
    assert pu is None or pu in seen, "dangling parent"
    if e.get("uuid"): seen.add(e["uuid"])
    c = (e.get("message") or {}).get("content")
    if not isinstance(c, list): continue
    for b in c:
        if b.get("type") == "tool_use":    uses[b["id"]] = True
        elif b.get("type") == "tool_result": results.add(b["tool_use_id"])
assert not set(uses) - results, "tool_use without tool_result"
assert not results - set(uses), "orphan tool_result"
```

Include `attachment`/`system` in the chain (§3) and compare tool ids as sets
(§4), or you will chase failures that are not there.

### Write one atomically

```python
tmp = path + ".tmp"
with open(tmp, "w") as f:
    for e in entries + trailers:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
os.replace(tmp, path)
os.utime(path, (mtime_s, mtime_s))      # picker sorts and dates by mtime
```

### Do not write a transcript a live process holds

Claude Code keeps session state in memory and appends on its own schedule;
rewriting the file underneath it loses whichever copy is written second. Check
the registry against `/proc` (§7) first.

---

## 14. Things I did not verify

- Where in the binary the `message.model` string is parsed, and why a slash
  hangs rather than erroring (§5). Symptom and fix are solid; mechanism is not.
- Whether `file-history-snapshot` entries must be consistent with the
  conversation for `--rewind` to work, or are advisory.
- What `atis-latch` is for.
- Whether the retention sweep can race a live session's append.
- Whether stale `~/.claude/sessions/*.json` entries cause anything worse than a
  phantom peer in `ListAgents`.
- The exact conditions under which resume classifies a tail as an interrupted
  turn (§10) — I mapped the entry points, not the full predicate.
