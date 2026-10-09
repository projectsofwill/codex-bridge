#!/usr/bin/env python3
"""Stand-in for the codex CLI. Behaviour is chosen by FAKE_CODEX_* env vars set per test."""
import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("codex-cli 0.0.0-fake")
    sys.exit(0)

if argv[:1] == ["app-server"]:  # JSON-RPC over stdio: answer initialize + account/rateLimits/read
    limits = json.loads(os.environ.get("FAKE_CODEX_LIMITS", '{"ordinaryUsageAllowed": true, "rateLimits": '
                        '{"primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1}}}'))
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            if os.environ.get("FAKE_CODEX_LIMITS_HANG"):
                time.sleep(60)
            print(json.dumps({"id": msg["id"], "result": limits}), flush=True)
    sys.exit(0)

if argv[:1] == ["sandbox"]:  # run the verify command unsandboxed (tests only)
    cmd = argv[argv.index("--") + 1:]
    cwd = argv[argv.index("-C") + 1]
    sys.exit(subprocess.run(cmd, cwd=cwd).returncode)

if argv[:1] == ["exec"]:
    out_file = argv[argv.index("-o") + 1]
    cwd = argv[argv.index("-C") + 1]
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_CODEX_PROMPT_LOG"):
        with open(os.environ["FAKE_CODEX_PROMPT_LOG"], "a", encoding="utf-8") as f:
            f.write(prompt + "\n=====\n")
    print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
    sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
    os.chdir(cwd)  # real Codex runs its commands with cwd = the -C directory
    if os.environ.get("FAKE_CODEX_CHILD"):  # leave a grandchild running in a new session (setsid escape)
        subprocess.Popen(["sleep", "300"], start_new_session=True)
    time.sleep(sleep)
    edit = os.environ.get("FAKE_CODEX_EDIT")  # "path=content;path2=content2"
    if edit:
        for pair in edit.split(";"):
            path, content = pair.split("=", 1)
            full = os.path.join(cwd, path)
            os.makedirs(os.path.dirname(full) or cwd, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
    reply = os.environ.get("FAKE_CODEX_REPLY", "answer")
    if "{{ECHO_CANARIES}}" in reply:  # quote the requested lines truthfully
        lines = []
        tail = prompt.split("READ-RECEIPT:", 1)[-1] if "READ-RECEIPT:" in prompt else prompt
        for ln in tail.splitlines():
            if " | " in ln and ln.split(" | ")[-1].strip().isdigit():
                rel, n = ln.split(" | ")
                text = open(os.path.join(cwd, rel.strip()), encoding="utf-8").read().splitlines()[int(n) - 1]
                lines.append(f"{rel.strip()} | {n.strip()} | {text.strip()}")
        reply = reply.replace("{{ECHO_CANARIES}}", "\n".join(lines))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}), flush=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(reply)
    sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))

sys.exit(2)
