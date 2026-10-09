# Security

codex-bridge sends code to a third-party model (OpenAI Codex) and runs code that model wrote on your machine.
This page says exactly what crosses which boundary and what the bridge does and does not enforce.

## What leaves your machine

Everything Codex sees goes to OpenAI under your Codex account, exactly as if you ran `codex` yourself:

- **Ask / gate:** your prompt, the literal diff, and the files you name (Codex reads them read-only). Secret-bearing
  paths (`.env*`, credentials, tokens, keys, `.git`, `.ssh`, `.aws`, `.codex`, `.gnupg`) are refused as targets,
  and the read-canary line is never picked from a secret-looking line, PEM block or opaque token.
- **Worker jobs:** the task spec, and whatever Codex reads while working in its worktree. **Codex's reads during a
  worker run are restricted by prompt, not by sandbox**: it runs with `-s workspace-write`, so it can read what
  your user account can read. Only its writes are isolated (to the worktree, then scope-checked).

The Claude reviewer runs in your Claude Code session under your Anthropic account, as any subagent does.

Nothing else is sent anywhere. Telemetry is a local JSONL file.

## What runs on your machine, and how it is contained

Worker-written code runs when the bridge re-runs your tests. That re-run happens inside `codex sandbox` with a
bridge-written profile:

- network disabled;
- writes limited to the worktree and a temp directory;
- denied: `~/.ssh`, `~/.codex`, `~/.aws`, `~/.config`, `~/.claude`, `~/Library/Keychains`, `~/Documents`,
  `~/Desktop`, `~/Downloads`, the bridge's job records, and any `workspace_roots` you configure;
- inside the worktree, `.env*`, credentials, tokens, `*.pem`, `*.key` and SSH keys denied;
- an allowlisted environment (no API keys or tokens inherited from your shell).

**This is a deny-list, not an allow-list.** Home paths not listed above remain readable to the test run. With the
network off, such data could only leave through the diff, which the receipt and the reviewer both see, and which
you review before merging.

`/codex-bridge setup` runs a selftest that must observe a real permission denial for each forbidden action (not a
crash or a missing interpreter) before worker jobs are unlocked on that machine. The proof is tied to the Codex
version, OS and machine.

## What the bridge never does

- merge, commit, push, or apply a worker's diff;
- apply a worker's suggested edit to a protected file;
- edit `~/.codex/config.toml`;
- run two Codex dispatches at once (on one machine, among bridge calls).

## Known gaps

- Worker-run reads (above).
- Codex used outside the bridge, and other machines, are not coordinated with the bridge's lock.
- End-of-job process cleanup is best-effort; a process that double-forks out of the job's process group and is not
  an orphan in the worktree can survive.
- The usage-limit read uses an experimental Codex endpoint.

## Reporting a vulnerability

Please report privately via GitHub: **Security → Report a vulnerability** on this repository
(private security advisory). Do not open a public issue for a vulnerability. Include the version
(`plugins/codex-bridge/.claude-plugin/plugin.json`), your OS, `codex --version`, and a minimal reproduction.
This is a single-maintainer project; expect an acknowledgement within a week.
