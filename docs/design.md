# codex-bridge design

This document tells how codex-bridge works and why each rule exists. Some code comments contain section markers,
for example `(2c.5)`. These markers refer to the build history that this document summarizes.

## Structure

```
plugins/codex-bridge/
  hooks/register.tsx   thin Claude Code layer: tools, slash command, band, reviewer start, watcher timer
  bridge/bridge.py     the logic, standard-library Python: slots, jobs, supervisor, sandbox, receipt, read checks, cost log
  bridge/prompts/      the prompts for Codex and the reviewer (ask, gate, resume, worker, reviewer)
```

Each `bridge.py` subcommand prints one JSON object. Free text (prompts, diffs, specifications) goes as JSON on stdin,
not as arguments. Thus, no shell reads the text. Sometimes a bridge call does not start, stops at its time limit, or
prints no JSON. The mod then uses `{ok: false}`, and the caller refuses the action. An exception never decides an
outcome.

The part that decides outcomes is Python with unit tests and a fake `codex` binary. The mod only shows data and
sends requests. The watcher timer in the mod never decides an outcome.

## State

All state is on the local machine, outside all repositories: `~/.codex-bridge/`. To change this location, set
`CODEX_BRIDGE_HOME`.

| Path | Content |
|---|---|
| `jobs/<id>/` | `job.json` (inputs, machine, owner session, base revision), `state.json`, receipt, verdict, logs |
| `worktrees/<id>/` | the git worktree of the job (detached at HEAD) |
| `slots/<token>.json` | one file for each running Codex run: owner, holder identity, process tree |
| `selftest.json` | the sandbox proof for this machine |
| `burn.jsonl` | one cost line for each call (you can set the path) |

Each state write is atomic (a temporary file, then a rename). Records from other machines are for information only.
codex-bridge never uses their process IDs on the local machine.

## Parallel runs

In Codex, "one session per account" means one *login*. A new `codex login` cancels the other logins. Many
processes can use one login at the same time. We tested three parallel runs before this design. Thus, asks and gates
never wait. Only `max_workers` (default 3) limits worker jobs. Each worker also starts a sandboxed test run and a
Claude review. The limit also stops a bad plan that starts ten jobs.

Each Codex run holds a slot file in `slots/`. codex-bridge writes the file atomically. The file records the process
identity of the owner and the Codex process tree. The identity is the process ID **and** the start time. Thus, a
new process with a reused ID cannot act as a dead owner.

codex-bridge counts the worker slots and makes a new slot inside one mutex. Thus, two starts cannot both get the last
place. A test starts eight processes at the same time with a limit of three. Exactly three get a slot. codex-bridge
removes a dead slot. If a slot is orphaned (the holder is dead, but Codex still runs), codex-bridge first stops
the Codex process tree. The `start` command reserves a place for its worker with a short lease. The supervisor of
the job then takes the reservation.

**Quota attribution.** Usage windows apply to the full account. Thus, a reading before and after a call gives the
cost of that call only if no other run occurred at the same time. A run counter finds each overlap. An overlapped
call records its change as `shared_delta`, and `delta` is `null`. The cost estimate for new workers uses only
measurements without overlap. The worker check before a job also includes the expected cost of the running workers.

**Authentication.** OpenAI replaces the refresh token of the login each time that a process uses it. If two runs
refresh at the same time, one run can fail. We did not see this failure, and we did not test it. codex-bridge tries
again one time after an authentication error. An ask always tries again. A worker tries again only if its worktree
did not change.

codex-bridge does not count Codex runs outside codex-bridge or on other machines.

## Ask and gate (`codex` tool)

Codex runs read-only (`-s read-only --strict-config --json`). The prompt goes on stdin. The output goes to a new
file. This run must make that file.

**Read checks.** The JSON events of Codex show commands. They do not show which files Codex read. A command that
does not read a file can still exit with 0. Thus, for each named file, codex-bridge selects a random line. It does not
select secret lines, PEM blocks or tokens. Codex must quote the line exactly in a `READ-RECEIPT` block. If the quote
is missing or wrong, the file has no read evidence, and `gate_satisfied` is false. For deleted files and targets
that exist only in the diff, codex-bridge compares the quote with the diff.

**Objections.** Codex marks problems with its context as `NEED-FILE <path>` or `BLOCKER`. codex-bridge continues the
same Codex thread (two times maximum) only for a `NEED-FILE` that names a file from the caller. All other objections
are blockers. An open objection blocks `gate_satisfied: true`, even when all read checks pass. An early version
continued after each objection. It used 310K input tokens on one objection that it could not solve.

**Freshness.** At the end, codex-bridge calculates the hashes of the named files and HEAD again. A change during the
run is a blocker.

**Time limit.** The mod stops a process call after 10 minutes. Thus, all attempts and the cleanup must finish in
540 seconds from the process start. If a gate needs more time, it fails with "budget exhausted".

The reply keeps **calculated** fields (receipt, model, quota change, `gate_satisfied`) separate from the **words of
Codex** (objections, findings). codex-bridge sends the words of Codex without changes and never grades them again.

## Model choice

codex-bridge, not the caller, selects the model and effort. A gate gets them from its risk. A worker gets them from
its tier. An ask uses `standard/medium`. Claude can only increase them. The code enforces the allowed sets. Asks
and gates never use the `cheap` role, because the gate *is* the check. Workers can use it, because codex-bridge
checks and reviews their work. Effort is low, medium or high, never more. There is one exception to "increase only":
a worker can use a lower model with an `experiment` label. codex-bridge logs this. The benchmark used it to compare
models. The quota never lowers a model. If the quota is too low, codex-bridge refuses the call.

## Worker jobs

### Intake

A worker job needs these inputs:

- the task
- the scope
- the verify command
- the tier
- the definition of done
- the **verifier manifest**: all files that the test run uses (tests, conftest, fixtures, configuration files,
  helpers)
- an **attestation**: a statement from the caller that the task is safe to send

codex-bridge records the attestation as *claimed*, never as calculated. codex-bridge cannot judge the risk, so it
records who judged it.

codex-bridge also refuses some paths mechanically. Secret paths (`.env*`, credentials, keys, `.git`, `.ssh`, `.aws`,
`.codex` and others) are never readable or writable. Protected paths (agent and automation configuration, and your
`protected_paths`) are readable. codex-bridge refuses a scope that writes to them. The comparison ignores case,
because on some file systems `Context/` and `context/` are the same folder. codex-bridge refuses symbolic links at
all levels of a path. A worker can write `.bridge-suggested.patch` for a protected file. codex-bridge moves this
file out before the accounting, shows it separately, and never applies it.

A verify command must use **one** test runner. When codex-bridge added the summaries of several runners, some
failing runs looked clean. Thus, output with the summaries of two runners gives no result, and no result is never
clean.

### Supervisor

`bridge.py supervise` runs detached. On POSIX it uses a new session. On Windows it uses a detached process group.
The supervisor owns the Codex child process. It enforces the 60-minute limit itself, without the Claude session. It
stops the full process tree at the limit. It writes the final state atomically. It releases its slot only when all
recorded processes are dead.

After Codex stops, the supervisor also stops orphaned processes (parent process ID 1) that started during the run
inside the worktree. It never stops a process that has a living parent. Thus, your own shell is safe.

### Lifecycle

```
pending -> running -> exited -> receipted -> reviewing -> reviewed -> notified
other final states: timeout, crashed, refused, cancelled, discarded
```

If a job finishes while Claude is closed, its state is `exited`. The next session continues the job. A job is
`crashed` only when its supervisor is dead **and** it has no final state. codex-bridge keeps the worktrees of
`timeout` and `crashed` jobs and marks them as quarantined. A partial diff can still be useful. `codex_discard`
removes a worktree when you ask. It refuses while the job runs.

The receipt calculation and the review each take an exclusive claim file. Thus, two sessions never calculate the
same receipt or start two reviewers. Each report has a lease. The session that started a job reports it. Another
session on the same machine takes the report only if the job has no claim for 3 minutes.

### Receipt

The order is important. codex-bridge first runs the tests and then does the accounting. Thus, the accounting
includes the changes from the test run.

1. Codex must exit with 0 and write a new final-message file. If not, the outcome is `crashed`.
2. codex-bridge runs the tests again in the sandbox (refer to "Sandboxed verify"). The limit is 20 minutes. All
   child processes must be dead before the accounting.
3. The accounting: `git add -A`, the diff, deleted files and tests, manifest hashes, `status --ignored`, and paths
   outside the scope. A git error or an unreadable path gives `incomplete-scan`.
4. A snapshot hash includes the binary diff, the manifest hashes and the ignored files that are not build files. A
   verdict includes the snapshot that it reviewed. If the worktree changes after the review, the verdict is stale.

Outcome priority: `crashed > incomplete-scan > out-of-scope > verifier-modified > mismatch > verify-failed > clean`.
If no tests ran, the outcome is `verify-failed`, never clean. If both runs fail in the same way, the outcome is
`verify-failed`. The claim of Codex was true, but the code fails.

### Sandboxed verify

codex-bridge uses `codex sandbox -P bridge-verify` with a profile in its own `CODEX_HOME`. It never changes your
`~/.codex/config.toml`. The profile extends the `:read-only` profile of Codex:

- The worktree and the temporary directory are writable.
- These paths are blocked: `~/.ssh ~/.codex ~/.aws ~/.config ~/.claude ~/Library/Keychains ~/Documents ~/Desktop
  ~/Downloads`, the job records of codex-bridge, and your `workspace_roots`.
- Inside the worktree, `.env*`, credentials, tokens and key files are blocked.
- The network is off.

The environment contains only approved variables: `PATH`, `HOME`, locale settings, temporary directories and the
basic Windows variables. It is never a copy of your environment.

Python dependencies: a job can name virtual environments. The sandbox gives their `site-packages` folder read-only
through `PYTHONPATH`. The base interpreter runs the tests, because the `python` of a virtual environment points
through blocked folders. The virtual environment must have the same Python version as the interpreter. It must not
contain files that look like secrets.

`selftest` proves the sandbox on each machine. These actions must all fail with a real permission error:

- a write outside the worktree
- a read of `.env`
- a network connection
- a read of a secret environment variable
- reads of the workspace and `~/.ssh`

A run and a write inside the worktree must succeed. The proof applies to one Codex version, one platform and one
machine.

### Review

codex-bridge automatically reviews only `clean` jobs. The tier sets the depth. The default is no review for R0,
Sonnet for R1 and Opus for R2. If an R0 job changed tests, it gets a Sonnet review.

The reviewer is a Claude subagent type from the mod. Its model is set for each tier, not from the model of your
session. Each review records the model that it used. The reviewer can use only Read, Grep and Glob. It gets a
packet that cannot change. The packet contains:

- the task, the definition of done, the tier, the scope and the manifest
- the named companion files (normal, committed files inside the repository only)
- the baseline test counts
- the receipt, the diff and the raw test output

The packet marks the diff and the text of the worker as untrusted.

The reviewer must give an assessment for each requirement and a verdict block. The code then applies these rules:

- If an `accept` has a requirement that is not exactly `met: true`, it becomes `fix-list`.
- If an `accept` does not examine a changed test, it becomes `fix-list`.
- If an `accept` has a missing companion file, it becomes `fix-list`.
- If codex-bridge cannot read the verdict, it starts one more review. After a second failure, it records `invalid`.

If the recorded verdict is different from the words of the reviewer, the main session gets a correction. Thus, a
changed `accept` never looks like a pass.

`codex_diagnose` runs the Opus reviewer on a job that is not clean. The reviewer finds the cause: a defect, a bad
test or a spec conflict. The job stays not accepted.

## Usage limits

Before and after each call, codex-bridge reads the account limits from `codex app-server`
(`account/rateLimits/read`). This read uses no model turn and takes less than one second. Asks and gates refuse only
when a limit is already reached. They show a warning above 85%. Workers refuse above the set limits until three
measured runs exist. After that, a worker refuses when the median measured cost does not fit. The change for each
call goes to the cost log. The endpoint is experimental. If the read fails, the result is "unknown". An unknown
result never blocks a call.

## Cost log

The cost log has one JSONL line for each call. Each line records the lane, outcome, model and reason, and quota
change. It also records the Codex tokens, the reviewer model and tokens, and the result size. `total_claude_tokens` is always `null` with a note. The mod cannot see the token
use of the main session. An earlier value of 0 looked like "free".

## Known limits

- During the Codex run of a worker, only a prompt controls the reads (`-s workspace-write`). Only the test run is in
  the sandbox. The writes of the worker go to the worktree, and a scope check follows.
- The sandbox profile uses a block list. The test run can read the home paths that the list does not include. The
  network is off. Thus, data can leave only through the diff, and the receipt and the reviewer show the diff.
- codex-bridge does not count Codex runs on other machines or outside codex-bridge.
- Process cleanup is not complete. A process that leaves the process group fast enough can stay alive.
