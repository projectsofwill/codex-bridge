# Benchmark

We used these runs to tune codex-bridge. The runs occurred in October 2026, on macOS, in the real repositories of
one developer. The task names and repositories are anonymous. The specifications, numbers and outcomes are as
recorded. **Each stage has only 3 tasks. Use these results as a direction, not as statistical proof.**

## Setup

Three arms ran the same task specification without changes. One job ran at a time.

| Arm | Method | Model / effort |
|---|---|---|
| A | The previous method: a Claude subagent runs `codex exec` and relays the result. The main Claude session then reviews the diff. | gpt-6.1-sol / medium |
| B | codex-bridge `codex_start` with the default model for the tier | gpt-6.1-sol / medium |
| C | codex-bridge with the low-cost model at high effort (a run with an `experiment` label) | gpt-6-luna / high |

Each task had a definition of done, a verify command and a tier. The quota cost is the change in percentage points
of the Codex 5-hour window. We read the account limits before and after each run.

Tasks:

- **M (mechanical, R0):** Make each `print()` in a folder of Python scripts write only ASCII characters. (Some
  Unicode characters stop Windows consoles.) Add an AST test that enforces this rule.
- **T (test writing, R0):** Write an offline test suite for a CLI tool. The specification for stage 1 had **two
  defects on purpose**: a flag that does not exist, and a function that the tool does not have. These defects
  showed how each arm handles a conflict between the specification and the code.
- **I (implementation, R1):** In stage 1, add grouped subheadings to a Markdown report generator. In stage 2, make a
  TypeScript Claude Code mod find its Python launcher and home directory on all platforms.

## Stage 1 (Opus review on each clean bridge job)

| Task | Arm | Outcome | Review | 5h quota | Claude tokens | Time |
|---|---|---|---|---|---|---|
| M | A | clean | main-session review: accept | +5 | ~36K relay + 353K review | ~1.5 min |
| M | B | clean | Opus: accept | +4 | 207K review | 1.0 min |
| M | C | clean | Opus: accept | +1 | 350K review | 0.9 min |
| T | A | clean (14 tests), found both defects | main-session review: accept | +3 | ~35K relay + 333K review | ~1.3 min |
| T | B | **not clean**: 12 pass, 2 fail (it tested the defective specification, but it reported the defects) | none | +4 | 0 | 1.2 min |
| T | C | clean (9 tests) | Opus said accept. codex-bridge **recorded fix-list**, because one requirement (a planted defect) was not met. | +1 | 84K review | 0.8 min |
| I | A | clean (13 tests) | main-session review: accept | +3 | ~38K relay + 345K review | ~1.5 min |
| I | B | clean (10 tests) | Opus: accept | +3 | 172K review | 1.4 min |
| I | C | clean (8 tests) | Opus: accept | +0 | 155K review | 0.9 min |

Results:

- **Quality.** A and C handled the specification conflict correctly. B wrote tests for the wrong specification. The
  low-cost model at high effort was equal to or better than the standard model on all three tasks. It used about a
  quarter of the 5-hour quota (C +2 in total, B +11, A +11).
- **Claude cost.** The main-session review of arm A used 333-353K tokens for each task. Most of these tokens were
  cache reads of a long session context. Thus, this cost increases with the session length, not with the diff. Each
  later turn also pays it again. The separate Opus review of codex-bridge used 84-350K tokens and kept the main
  context small. With only three tasks, neither method was clearly cheaper in raw tokens.
- **A mechanical review added little.** The R0 task had four automatic checks: the sandboxed test run, the scope
  check, the test freeze and the claim comparison. These checks already covered the checks of the Opus review. Both reviews found only a gap in the
  specification. This result gave **review depth by tier** (R0 none, R1 Sonnet, R2 Opus).
- **Bridge bugs found:** The pytest run in the sandbox exited with 120 and empty logs, because the logs were in a
  blocked directory. Any local session could take the job report of another live session. We repaired both bugs.

## Stage 2 (review by tier)

Only arms B and C ran, from a new session. Expected reviews: M R0 → Sonnet (tests changed), T R0 → Sonnet (tests
changed), I R1 → Sonnet.

| Task | Arm | Outcome | Review | 5h / weekly quota | Reviewer tokens | Time |
|---|---|---|---|---|---|---|
| M | B | clean (1 test) | Sonnet: accept | +4 / +1 | 98K | 0.8 min |
| M | C | clean (2 tests, with a positive control) | Sonnet: **fix-list**. It also changed a string that is not in a `print()` call and goes to a file. This broke the specification, and the AST test cannot see it. We confirmed it manually. | +0 / +0 | 100K | 1.0 min |
| T | B | clean (11 tests) | Sonnet wrote "accept" in text but did not write the necessary verdict block. codex-bridge recorded `invalid`, and the job stopped. | +3 / +0 | n/a | 1.1 min |
| T | C | clean (9 tests) | Sonnet: accept | +1 / +0 | 71K | 0.8 min |
| I | B | clean (9 tests) | Sonnet: accept | +4 / +1 | 75K | 1.5 min |
| I | C | clean (8 tests) | Sonnet: accept (small issue: `??` does not go to the next value when `HOME` is empty; the `\|\|` of B does) | +0 / +0 | 48K | 1.1 min |

Results:

- The low-cost model used almost no quota on each task. The reviewer accepted it 2 times out of 3. Its one failure
  was a change outside the scope, and the Sonnet reviewer found it.
- Sonnet reviews used 48-100K tokens. Opus reviews in stage 1 used about 85-350K. Sonnet found the one real defect.
- **Bridge bugs found:** When a reviewer did not write the verdict block, the job stopped. The correction then said
  incorrectly that the worktree changed after the review. Repairs: codex-bridge now starts one more review when the
  verdict block is missing. The stale check no longer triggers on a verdict that codex-bridge cannot read.

## Changes from these results

- Review depth by tier (default R0 none, R1 Sonnet, R2 Opus). You can change it.
- The code enforces the verdict rules: an `accept` with an unmet requirement becomes `fix-list` (stage 1, T/C).
- One more review for a missing verdict, and a repaired stale check (stage 2, T/B).
- codex-bridge replaced the previous worker with a relay subagent.

## Gate cost, for scale

We also used Codex gates to review codex-bridge. One large gate round (a diff of about 1,900 lines) used about 21
points of the 5-hour window and 3 points of the weekly window. A chain of five gate rounds on three changes moved
the 5-hour window from 0% to 60%.
