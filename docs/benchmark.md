# Benchmark

Head-to-head runs used to tune codex-bridge, October 2026, macOS, one developer's real repositories. Task names
and repositories are anonymized; specs, numbers and outcomes are as recorded. **n = 3 tasks per stage. Treat
everything here as directional, not statistically meaningful.**

## Setup

Three arms ran the same task spec verbatim, one job at a time:

| Arm | What | Model / effort |
|---|---|---|
| A | The previous approach: a Claude subagent that runs `codex exec` and relays the result; the main Claude session then reviews the diff | gpt-6.1-sol / medium |
| B | codex-bridge `codex_start`, default model for the tier | gpt-6.1-sol / medium |
| C | codex-bridge with the cheap model at high effort (an `experiment`-labelled run) | gpt-6-luna / high |

Each task had a definition of done, a verify command and a tier. Quota cost is the change in percentage points of
the Codex 5-hour window, read from the account's limits before and after.

Tasks:

- **M (mechanical, R0):** make every `print()` in a folder of Python scripts emit ASCII only (Windows consoles crash
  on some Unicode), plus an AST-based test that enforces it.
- **T (test-writing, R0):** write an offline test suite for an existing CLI tool. Stage 1's spec contained **two
  deliberate defects** (a flag that doesn't exist; a behaviour the tool explicitly doesn't have) to see how each arm
  handles spec-vs-code conflicts.
- **I (implementation, R1):** stage 1, add grouped sub-headings to a Markdown report generator; stage 2, make a
  TypeScript Claude Code mod resolve its Python launcher and home directory cross-platform.

## Stage 1 (Opus review on every clean bridge job)

| Task | Arm | Outcome | Review | 5h quota | Claude tokens | Wall |
|---|---|---|---|---|---|---|
| M | A | clean | main-session review: accept | +5 | ~36K relay + 353K review | ~1.5 min |
| M | B | clean | Opus: accept | +4 | 207K review | 1.0 min |
| M | C | clean | Opus: accept | +1 | 350K review | 0.9 min |
| T | A | clean (14 tests), flagged both spec defects | main-session review: accept | +3 | ~35K relay + 333K review | ~1.3 min |
| T | B | **not clean**: 12 pass, 2 fail (it tested the defective spec as written, though it flagged the defects) | none | +4 | 0 | 1.2 min |
| T | C | clean (9 tests) | Opus said accept; the bridge **recorded fix-list** because one requirement (a planted defect) was unmet | +1 | 84K review | 0.8 min |
| I | A | clean (13 tests) | main-session review: accept | +3 | ~38K relay + 345K review | ~1.5 min |
| I | B | clean (10 tests) | Opus: accept | +3 | 172K review | 1.4 min |
| I | C | clean (8 tests) | Opus: accept | +0 | 155K review | 0.9 min |

Readings:

- **Quality.** A and C handled the spec conflict correctly; B wrote tests asserting the wrong spec. The cheap model
  at high effort matched or beat the standard model on all three tasks at about a quarter of the 5-hour quota (C
  +2 total vs B +11 vs A +11).
- **Claude cost.** Arm A's main-session review measured 333-353K tokens per task, but most of that is cache reads of
  a long session context: it scales with session length, not the diff, and every later turn re-bills it. The
  bridge's separate Opus review cost 84-350K and leaves the main context lean. Neither path was clearly cheaper in
  raw tokens at this n.
- **Mechanical review added little.** On the R0 task, the sandboxed re-run, scope check, test freeze and
  claim-vs-rerun match had already covered what the Opus review checked; both reviews only found a spec gap. This
  led to **review depth by tier** (R0 none, R1 Sonnet, R2 Opus).
- **Bridge bugs found:** the pytest re-run exited 120 with empty logs (logs were in a sandbox-denied directory); any
  local session could claim another live session's job report. Both fixed.

## Stage 2 (tiered review)

Arms B and C only, from a fresh session. Expected review: M R0 → Sonnet (tests changed), T R0 → Sonnet (tests
changed), I R1 → Sonnet.

| Task | Arm | Outcome | Review | 5h / weekly quota | Reviewer tokens | Wall |
|---|---|---|---|---|---|---|
| M | B | clean (1 test) | Sonnet: accept | +4 / +1 | 98K | 0.8 min |
| M | C | clean (2 tests, incl. a positive control) | Sonnet: **fix-list**: it also changed a non-print string that is written to a file (a spec violation the AST test can't see; confirmed by hand) | +0 / +0 | 100K | 1.0 min |
| T | B | clean (11 tests) | Sonnet wrote "accept" in prose but omitted the required verdict block: recorded `invalid`, job stuck | +3 / +0 | n/a | 1.1 min |
| T | C | clean (9 tests) | Sonnet: accept | +1 / +0 | 71K | 0.8 min |
| I | B | clean (9 tests) | Sonnet: accept | +4 / +1 | 75K | 1.5 min |
| I | C | clean (8 tests) | Sonnet: accept (minor: `??` doesn't fall through on an empty `HOME`; B's `\|\|` does) | +0 / +0 | 48K | 1.1 min |

Readings:

- The cheap model cost ~0 quota on every task and was accepted 2 of 3 times; its one miss was a scope overreach the
  Sonnet reviewer caught.
- Sonnet reviews ran 48-100K tokens versus ~85-350K for Opus in stage 1, and caught the one real defect.
- **Bridge bugs found:** a reviewer that omits the verdict block left the job stuck, and the correction then wrongly
  said the worktree had changed after review. Fixed: one automatic re-review on a missing verdict block, and stale
  detection no longer fires on an unparseable verdict.

## What changed because of this

- Review depth by tier (default R0 none, R1 Sonnet, R2 Opus), configurable.
- The verdict contract is enforced in code: `accept` with an unmet requirement becomes `fix-list` (stage 1, T/C).
- Missing-verdict re-review and a fixed stale check (stage 2, T/B).
- The previous subagent-relay worker was retired in favour of the bridge.

## Gate cost, for scale

The bridge's own critique gates were run through Codex too. One large gate round (a ~1,900-line diff) cost about
21 points of the 5-hour window and 3 of the weekly. A five-round gate chain across three changes took the 5-hour
window from 0% to 60%.
