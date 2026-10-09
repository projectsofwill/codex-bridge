# Security

codex-bridge sends code to a third-party model (OpenAI Codex). It also runs code from that model on your machine.
This page tells you which data crosses which boundary. It also tells you what codex-bridge enforces and what it does
not enforce.

## Data that leaves your machine

All data that Codex sees goes to OpenAI under your Codex account. This is the same as when you run `codex` yourself.

- **Ask and gate:** Codex gets your prompt, the literal diff, and the files that you name. Codex can only read these
  files. codex-bridge refuses secret paths as targets: `.env*`, credentials, tokens, keys, `.git`, `.ssh`, `.aws`,
  `.codex` and `.gnupg`. codex-bridge never selects the read-check line from a secret line, a PEM block or a token.
- **Worker jobs:** Codex gets the task and the files that it reads in its worktree. **A prompt controls the reads
  of Codex during a worker job. A sandbox does not control them.** Codex runs with `-s workspace-write`. Thus, it
  can read the files that your user account can read. codex-bridge isolates only the writes of Codex: to the
  worktree, with a scope check after the job.

The Claude reviewer runs in your Claude Code session under your Anthropic account. This is the same as all subagents.

codex-bridge sends no other data. The cost log is a local JSONL file.

## Code that runs on your machine

Code from a worker runs when codex-bridge runs your tests again. That test run occurs inside `codex sandbox` with a
profile from codex-bridge:

- The network is off.
- Writes go only to the worktree and to a temporary directory.
- The sandbox blocks these paths: `~/.ssh`, `~/.codex`, `~/.aws`, `~/.config`, `~/.claude`,
  `~/Library/Keychains`, `~/Documents`, `~/Desktop`, `~/Downloads`, the job records of codex-bridge, and all
  `workspace_roots` that you set.
- Inside the worktree, the sandbox blocks `.env*`, credentials, tokens, `*.pem`, `*.key` and SSH keys.
- The environment contains only approved variables. The test run gets no API keys or tokens from your shell.

**The sandbox uses a block list, not an allow list.** The test run can read the home paths that the list above
does not include. The network is off. Thus, this data can leave only through the diff. The receipt and the reviewer
both show the diff, and you examine it before you merge.

`/codex-bridge setup` runs a selftest. For each blocked action, the selftest must see a real permission error. A
crash or a missing interpreter does not count. Worker jobs stay locked on a machine until the selftest passes. The
proof applies to one Codex version, one operating system and one machine.

## Actions that codex-bridge never does

- It never merges, commits, pushes or applies the diff of a worker.
- It never applies a suggested change from a worker to a protected file.
- It never changes `~/.codex/config.toml`.
- It never runs more than `max_workers` worker jobs at the same time on one machine.

## Known gaps

- Codex reads during a worker job (refer to "Data that leaves your machine").
- The worker limit does not count Codex runs outside codex-bridge or on other machines.
- Process cleanup at the end of a job is not complete. A process can stay alive if it leaves the process group of
  the job. This occurs when the process also has a parent process or runs outside the worktree.
- codex-bridge reads the usage limits through an experimental Codex endpoint.

## Report a vulnerability

1. On GitHub, open this repository.
2. Select **Security**, then **Report a vulnerability**. This makes a private security advisory.
3. Include the version (from `plugins/codex-bridge/.claude-plugin/plugin.json`), your operating system, the output
   of `codex --version`, and the minimum steps to show the problem.

Do not open a public issue for a vulnerability. One person maintains this project. You get a reply in one week or
less.
