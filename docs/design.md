# codex-bridge design

How the bridge works and why each rule exists. Section markers like `(2c.5)` in code comments refer to the build
history this document condenses.

## Shape

```
plugins/codex-bridge/
  hooks/register.tsx   thin Claude Code layer: tools, slash command, band, reviewer spawn, watcher timer
  bridge/bridge.py     the logic, stdlib-only Python: lock, jobs, supervisor, sandbox, receipt, grounding, telemetry
  bridge/prompts/      the prompts Codex and the reviewer get (ask, gate, resume, worker, reviewer)
```

Every `bridge.py` subcommand prints exactly one JSON object. Free text (prompts, diffs, specs) travels as JSON on
stdin, never argv, so no shell ever parses it. The mod treats a bridge call that fails to start, times out or
prints no JSON as `{ok: false}`: callers fail closed, never on an exception path.

The split exists so the part that decides outcomes is plain, unit-tested Python with a fake `codex` binary, and the
mod only displays and routes. The watcher timer in the mod never decides an outcome.

## State

Machine-local, outside every repository: `~/.codex-bridge/` (override with `CODEX_BRIDGE_HOME`).

| Path | Holds |
|---|---|
| `jobs/<id>/` | `job.json` (inputs, machine, owner session, base revision), `state.json`, receipt, verdict, logs |
| `worktrees/<id>/` | the job's git worktree (detached at HEAD) |
| `codex.lock` | the one Codex slot, with the holder's identity |
| `selftest.json` | this machine's sandbox proof |
| `burn.jsonl` | one cost line per call (path configurable) |

Every state write is atomic (temp file + rename). Records from another machine are informational; their process
ids are never interpreted locally.

## One Codex at a time

Codex is one session per account, so the bridge never overlaps its own Codex runs. The lock is created with its
full content atomically, records the owner's process identity (pid **plus** start time, so a reused pid can't
impersonate a dead owner), and a stale lock is reclaimed under a mutex that removes only the exact lock judged
dead. An ask waits up to 120 s for the slot, a worker up to 10 min; both then refuse rather than run in parallel.

What the lock cannot see: Codex run by hand, other tools that run Codex, other machines. That is stated in the tool
descriptions rather than pretended away.

## Ask and gate (`codex` tool)

Codex runs read-only (`-s read-only --strict-config --json`), the prompt over stdin, output to a fresh file that
this run must have created.

**Grounding.** Codex's JSON events show commands but not which files were actually read (a command that failed to
read a file can still exit 0). So for each named file the bridge picks a random line (skipping secret-looking
lines, PEM blocks and opaque tokens) and requires Codex to quote it verbatim in a `READ-RECEIPT` block. A missing or
wrong quote means no read evidence for that file, and `gate_satisfied` is false. Deleted files and inline-diff-only
targets are checked against the embedded diff instead.

**Objections.** Codex tags context problems `NEED-FILE <path>` or `BLOCKER`. The bridge resumes the same Codex
thread (at most twice) only for `NEED-FILE` naming a file the caller named; anything else is a computed blocker.
Any unresolved objection blocks `gate_satisfied: true`, even with every canary passing. An early version resumed on
every objection and burned 310K input tokens on one unresolvable one.

**Freshness.** Named files and HEAD are re-hashed at the end; a change during the run is a blocker.

**Budget.** The mod's process call caps at 10 minutes, so lock wait plus all attempts plus teardown must fit 540 s
from process start; a gate that needs longer fails closed with "budget exhausted".

The reply separates **computed** fields (receipt, model, quota delta, `gate_satisfied`) from **Codex's words**
(objections, findings), which are relayed verbatim and never re-graded.

## Model choice

The bridge, not the caller, picks model and effort: a gate from its stakes, a worker from its tier, an ask at
`standard/medium`. Claude may only raise. Code-enforced allowed sets: ask and gate never run on the `cheap` role
(the gate *is* the check); workers may (their work is re-verified and reviewed). Effort is low/medium/high, never
above. One exception to raise-only: a worker may run below its mapping with an `experiment` label, logged, which is
how the benchmark compared models. Quota never downgrades a model; it refuses.

## Worker jobs

### Intake

Required: task, scope, verify command, tier, definition of done, the **verifier manifest** (every file the verify
run trusts: tests, conftest, fixtures, configs, helpers), and an **attestation**: the driver's written judgment
that the task is safe to delegate. The attestation is recorded as *claimed*, never computed: the bridge can't judge
consequence, so it records who did.

Mechanical refusals on top: secret paths (`.env*`, credentials, keys, `.git`, `.ssh`, `.aws`, `.codex`, ...) are
never readable or writable; protected paths (agent and automation config, plus your `protected_paths`) are
readable but a scope that would write them is refused. Matching is case-insensitive (case-insensitive filesystems
made `Context/` slip past `context/`) and symlinks anywhere in a path chain are refused. A worker may write
`.bridge-suggested.patch` for a protected file: it is moved out before accounting, shown separately, never applied.

Verify commands use **one** test runner. Summing several runners' summaries kept producing false-clean routes, so
output with two runners' summaries parses as nothing, and nothing is never clean.

### Supervisor

`bridge.py supervise` runs detached (new session on POSIX; detached process group on Windows) and owns the Codex
child. It holds the 60-minute ceiling itself, independent of the Claude session; kills the whole tree at the
deadline; writes the terminal state atomically; and releases the lock only once every tracked process is dead.
After Codex exits it also kills orphaned processes (parent pid 1) born during the run whose working directory is
inside the worktree. A process with a living parent is never touched, so a person's own shell is safe.

### Lifecycle

```
pending -> running -> exited -> receipted -> reviewing -> reviewed -> notified
terminal also: timeout, crashed, refused, cancelled, discarded
```

A job that finished while Claude was closed is simply `exited`; the next session picks it up. A job is `crashed`
only when its supervisor is dead **and** it has no terminal state. Timeout and crash worktrees are kept and marked
quarantined, since a partial diff can still be useful. `codex_discard` removes a worktree on request and refuses
while the job runs.

Receipt computation and review each take an exclusive claim file, so two sessions never compute the same receipt
or spawn two reviewers. Reports carry a lease: the session that started a job reports it; another session on the
same machine takes over only after the job has sat unclaimed for 3 minutes.

### Receipt

Order matters: verify first, then account, so changes made by the verify run count.

1. Codex exited 0 with a fresh final-message file, else `crashed`.
2. Verify re-run in the sandbox (below), with a 20-minute cap and every descendant dead before accounting.
3. Accounting: `git add -A`, diff, deleted files and tests, manifest hashes, `status --ignored`, out-of-scope paths.
   Any git error or unreadable path is `incomplete-scan`.
4. A snapshot hash binds the binary diff, manifest hashes and non-build ignored files. A verdict carries the
   snapshot it reviewed; if the worktree changes afterwards, the verdict is stale.

Outcome precedence: `crashed > incomplete-scan > out-of-scope > verifier-modified > mismatch > verify-failed >
clean`. Zero tests executed is `verify-failed`, never clean. Two matching red runs are `verify-failed` (the claim
was honest, the code fails).

### Sandboxed verify

`codex sandbox -P bridge-verify` with a profile the bridge writes into its own `CODEX_HOME` (your
`~/.codex/config.toml` is never edited). It extends Codex's `:read-only` with: worktree and tmp writable; denied
`~/.ssh ~/.codex ~/.aws ~/.config ~/.claude ~/Library/Keychains ~/Documents ~/Desktop ~/Downloads`, the bridge's own
job records and your `workspace_roots`; `.env*`, credentials, tokens and key files denied inside the worktree;
network off. The environment is an allowlist (PATH, HOME, locale, temp dirs and the Windows basics), never a copy
of yours.

Python dependencies: a job may name virtualenvs; their `site-packages` is exposed read-only via `PYTHONPATH` and the
base interpreter runs the tests (a venv's own `python` resolves through denied parents). The venv must match the
interpreter's version and contain no secret-looking files.

`selftest` proves this on each machine (write outside, read `.env`, network, environment secret, workspace and
`~/.ssh` reads must all be *denied* with a real permission error, not merely fail; running and writing inside must
work) and binds the proof to the Codex version, platform and machine.

### Review

Only `clean` jobs are reviewed automatically, at the depth the tier configures (default R0 none, R1 Sonnet,
R2 Opus; R0 escalates to Sonnet when tests changed). The reviewer is a Claude subagent type registered by the mod,
restricted to Read, Grep and Glob, given an immutable packet: task, definition of done, tier, scope, manifest,
named companion files (regular, committed, in-repo files only), baseline test counts, receipt, diff and raw verify
output, with diff and worker text marked untrusted.

It must return a requirement-by-requirement assessment and a verdict block. In code: an `accept` with any
requirement not literally `met: true`, a missing disposition for a changed test, or a missing companion is
downgraded to `fix-list`; a verdict that can't be parsed gets one re-review, then is recorded `invalid`. When the
recorded verdict differs from the reviewer's words, the main session gets a correction, so a downgraded accept
never reads as green.

`codex_diagnose` runs the Opus reviewer on demand for a job that wasn't clean, to tell a defect from a bad test or a
spec conflict. The job stays unaccepted either way.

## Usage limits

Before and after each call the bridge reads the account's limits from `codex app-server`
(`account/rateLimits/read`, no model turn, under a second). Asks and gates refuse only when a limit is already hit
and warn above 85%. Workers refuse above the configured caps until three measured runs exist, then when the median
measured cost won't fit. The delta per call goes to the cost log. The endpoint is experimental; a failed read is
"unknown", never a block.

## Telemetry

One JSONL line per call: lane, outcome, model and reason, quota delta, Codex tokens, reviewer tokens, result size.
`total_claude_tokens` is always `null` with a note: the mod can't see main-loop token usage, and a fake 0 once read
as "free".

## Known limits

- The worker's own reads during the Codex run are restricted by prompt only (`-s workspace-write`); only the
  verify re-run is sandboxed. Writes are isolated to the worktree and scope-checked.
- The sandbox profile is a deny-list: home paths not listed stay readable to the verify run (network is off, so
  data can only leave through the diff, which the receipt and reviewer see).
- Cross-machine and non-bridge Codex use are not coordinated.
- Process cleanup is best-effort (a process that double-forks fast enough can escape).
