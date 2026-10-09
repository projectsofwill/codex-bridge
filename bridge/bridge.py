#!/usr/bin/env python3
"""codex-bridge core (stdlib only): lock, job registry, supervisor, receipt, gate runs, telemetry.

Every subcommand prints one JSON object on stdout. Inputs that carry free text (prompts, diffs,
specs) arrive as JSON on stdin, never argv, so no shell ever sees them.

Design notes: docs/design.md. Section refs like (2c.5) are build-history markers.
"""
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

WORKER_CEILING_S = 60 * 60     # hard ceiling, enforced by the supervisor, not the session
VERIFY_CEILING_S = 20 * 60
LOCK_WAIT_WORKER_S = 600
MAX_RESUMES = 2
ASK_DEADLINE_S = 540       # from PROCESS START: version check + attempts + teardown < the mod's 600 s cap
TEARDOWN_MARGIN_S = 20     # end_tree can wait ~12 s; hashing + result write follow
PROCESS_START = time.time()

HOME = Path(os.environ.get("CODEX_BRIDGE_HOME", Path.home() / ".codex-bridge"))
# Windows: npm installs codex as codex.cmd, which CreateProcess won't find without PATHEXT lookup.
CODEX = os.environ.get("CODEX_BRIDGE_CODEX") or shutil.which("codex") or "codex"
IS_WIN = os.name == "nt"

EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}  # xhigh and above are never allowed
# Model policy, stakes, tiers, protected paths and the refuse caps come from config (see load_config).

# ---- usage preflight (0.1.1)
WARN_PCT = 85
RESET_HINT_S = 45 * 60
MIN_SAMPLES = 3

# Mechanical intake lists (2b, split in 0.1.1). The driver's consequence judgment is the attestation.
# SECRET: never read, never written, never quoted off-machine.
SECRET_NAMES = {".env", ".env.keys", "credentials.json", "token.json", ".credentials.json", "auth.json"}
SECRET_PARTS = {".git", ".codex", ".ssh", ".aws", ".gnupg"}  # .git: remote URLs and hooks can carry tokens
# Readable (context for Codex), never written by a worker: agent + automation config by default, plus
# whatever the user's config adds (it can add, never remove). Names compare case-insensitively.
DEFAULT_PROTECTED_PREFIXES = (".claude/", ".codex/", ".github/workflows/")
DEFAULT_PROTECTED_NAMES = {"agents.md", "claude.md", ".mcp.json", "settings.json", "settings.local.json"}
PROTECTED_PARTS = {".claude"}
SUGGESTED_PATCH = ".bridge-suggested.patch"  # a worker's proposed edit to a protected file: never applied
# Ignored files a test run normally leaves; anything else ignored blocks `clean` (2c.5).
SECRETISH = re.compile(r"(?i)(api[_-]?key|token|secret|passw(or)?d|private[_-]?key|bearer|sk-[a-z0-9]|-----BEGIN"
                       r"|AKIA[0-9A-Z]{8}|ghp_|github_pat_|xox[bpas]-|credential)")
# A long unbroken token-like run (base64 body, hex digest, API key): never quoted off-machine as a canary.
OPAQUE = re.compile(r"^[A-Za-z0-9+/=_\-.:]{32,}$")
# Positively identified generated files: exempt from scope ONLY when newly added (never tracked, never denied).
GENERATED = re.compile(r"(^|/)__pycache__/[^/]+\.py[co]$|(^|/)\.pytest_cache/|(^|/)\.DS_Store$")
# Files that change what verification means: any new or changed one counts as a verifier change (gate finding 8).
VERIFIER = re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|\.coveragerc|noxfile\.py"
                      r"|package\.json|(jest|vitest|playwright|karma)\.config\.[cm]?[jt]s"
                      r"|requirements[^/]*\.txt|package-lock\.json|bun\.lockb?|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock)$"
                      r"|(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$")
EXPECTED_IGNORED = re.compile(r"(^|/)(__pycache__|\.pytest_cache|node_modules|\.mypy_cache|\.ruff_cache)(/|$)"
                              r"|\.py[co]$|(^|/)\.DS_Store$")


# ---------------------------------------------------------------- small utilities

def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=True, indent=2))
    sys.exit(code)


def fail(msg, **extra):
    out({"ok": False, "error": msg, **extra}, 1)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=True) + "\n")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- config

DEFAULT_CONFIG = {
    # Model roles: `cheap` may only run verified + reviewed worker jobs; gates never go below `standard`.
    "models": {"cheap": "gpt-6-luna", "standard": "gpt-6.1-sol", "strong": "gpt-6-astra"},
    # Gate stakes -> (model role, effort). A gate call must name the stakes that make it a gate.
    "stakes": {"irreversible": ["standard", "high"], "trust-boundary": ["standard", "high"],
               "unattended": ["standard", "medium"], "policy": ["standard", "medium"]},
    "stakes_aliases": {},  # your own rule names -> a stakes name, e.g. {"backbone": "unattended"}
    "tiers": {"R0": ["standard", "medium"], "R1": ["standard", "medium"], "R2": ["standard", "high"]},
    "review": {"R0": "none", "R1": "sonnet", "R2": "opus"},  # Claude reviewer per worker tier
    "protected_paths": [],  # added to the defaults: "dir/" = a prefix, anything else = a file name
    "workspace_roots": [],  # extra roots whose protected areas also deny a worker repo / worktree
    "log_path": None,       # cost log (JSONL); default ~/.codex-bridge/burn.jsonl
    "worker_refuse_pct": {"primary": 70, "secondary": 85},  # until MIN_SAMPLES measured runs exist
    "max_workers": 3,       # worker jobs running at once on this machine; 0 = no cap. Asks/gates never wait.
}
REVIEWERS = ("none", "sonnet", "opus")


def config_path():
    return Path(os.environ.get("CODEX_BRIDGE_CONFIG") or HOME / "config.json")


def load_config():
    """Defaults merged with the user's config. Fails CLOSED: a malformed config refuses every command
    rather than silently running with a policy the user didn't write."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = config_path()
    if not p.is_file():
        return cfg
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"config {p} is unreadable: {e}")
    if not isinstance(user, dict):
        fail(f"config {p} must be a JSON object")
    bad = sorted(set(user) - set(DEFAULT_CONFIG))
    if bad:
        fail(f"config {p}: unknown key(s) {bad}; valid: {sorted(DEFAULT_CONFIG)}")
    for k, v in user.items():
        cfg[k] = {**cfg[k], **v} if isinstance(cfg[k], dict) and isinstance(v, dict) else v
    problems = []
    roles = cfg["models"]
    if set(roles) != {"cheap", "standard", "strong"} or not all(isinstance(m, str) and m for m in roles.values()):
        problems.append("models needs exactly cheap, standard, strong (model ids)")
    for table in ("stakes", "tiers"):
        for name, val in cfg[table].items():
            if not (isinstance(val, list) and len(val) == 2 and val[0] in roles and val[1] in EFFORT_RANK):
                problems.append(f"{table}.{name} must be [role, effort], role in {sorted(roles)}, effort in {sorted(EFFORT_RANK)}")
    if set(cfg["tiers"]) != {"R0", "R1", "R2"}:
        problems.append("tiers needs exactly R0, R1, R2")
    for name, target in cfg["stakes_aliases"].items():
        if target not in cfg["stakes"] or name in cfg["stakes"]:
            problems.append(f"stakes_aliases.{name} must point at a stakes name and not shadow one")
    if set(cfg["review"]) != {"R0", "R1", "R2"} or not all(v in REVIEWERS for v in cfg["review"].values()):
        problems.append(f"review needs R0, R1, R2, each one of {list(REVIEWERS)}")
    for key in ("protected_paths", "workspace_roots"):
        if not (isinstance(cfg[key], list) and all(isinstance(x, str) and x.strip() for x in cfg[key])):
            problems.append(f"{key} must be a list of non-empty strings")
    if cfg["log_path"] is not None and not isinstance(cfg["log_path"], str):
        problems.append("log_path must be a string or null")
    pct = cfg["worker_refuse_pct"]
    if set(pct) != {"primary", "secondary"} or not all(isinstance(x, (int, float)) and 0 < x <= 100 for x in pct.values()):
        problems.append("worker_refuse_pct needs primary and secondary in (0, 100]")
    mw = cfg["max_workers"]
    if isinstance(mw, bool) or not isinstance(mw, int) or mw < 0:
        problems.append("max_workers must be a whole number >= 0 (0 = no cap)")
    if problems:
        fail(f"config {p} is invalid", problems=problems)
    return cfg


CFG = load_config()
CHEAP, STANDARD, STRONG = (CFG["models"][r] for r in ("cheap", "standard", "strong"))
MODEL, EFFORT = STANDARD, "medium"  # fallback for helpers; per-call choice is choose_model()
MODEL_RANK = {CHEAP: 0, STANDARD: 1, STRONG: 2}
ALLOWED = {
    "ask": {STANDARD: ("medium",), STRONG: ("medium",)},
    "gate": {STANDARD: ("medium", "high"), STRONG: ("medium", "high")},  # never cheap: the gate IS the check
    "worker": {STANDARD: ("medium", "high"), CHEAP: ("medium", "high")},  # its work is verified + reviewed
}
STAKES = {n: (CFG["models"][r], e) for n, (r, e) in CFG["stakes"].items()}
GATE_TRIGGERS = {**STAKES, **{a: STAKES[t] for a, t in CFG["stakes_aliases"].items()}}
TIER_DEFAULT = {t: (CFG["models"][r], e) for t, (r, e) in CFG["tiers"].items()}
WORKER_REFUSE_PCT = CFG["worker_refuse_pct"]
MAX_WORKERS = CFG["max_workers"]
_extra = [x.strip().replace("\\", "/").casefold() for x in CFG["protected_paths"]]
PROTECTED_PREFIXES = DEFAULT_PROTECTED_PREFIXES + tuple(x for x in _extra if x.endswith("/"))
PROTECTED_NAMES = DEFAULT_PROTECTED_NAMES | {x for x in _extra if not x.endswith("/")}
WORKSPACE_ROOTS = [Path(x).expanduser() for x in CFG["workspace_roots"]]
LOG_PATH = Path(CFG["log_path"]).expanduser() if CFG["log_path"] else HOME / "burn.jsonl"


def cmd_config():
    """What the mod needs to build its tools: stakes names, aliases, reviewers per tier, model roles."""
    return {"ok": True, "path": str(config_path()), "models": CFG["models"], "stakes": sorted(CFG["stakes"]),
            "stakes_aliases": CFG["stakes_aliases"], "review": CFG["review"],
            "protected": sorted(PROTECTED_PREFIXES) + sorted(PROTECTED_NAMES), "log_path": str(LOG_PATH),
            "max_workers": MAX_WORKERS}


def run(argv, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    return subprocess.run(argv, **kw)


def git(wt, *args):
    return run(["git", "-C", str(wt), *args])


def machine():
    return platform.node()


# ---------------------------------------------------------------- process identity (2.0)
# pid alone is not an identity: pids get reused. identity = "<pid>:<process start time>".

def proc_start(pid):
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            ok = ctypes.windll.kernel32.GetProcessTimes(h, *[ctypes.byref(x) for x in t])
            return f"{t[0].dwHighDateTime}.{t[0].dwLowDateTime}" if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    r = run(["ps", "-o", "stat=,lstart=", "-p", str(pid)])
    parts = r.stdout.strip().split(None, 1)
    if len(parts) < 2 or parts[0].startswith("Z"):
        return None  # gone, or a zombie: it can no longer run or write, so it is not alive
    return parts[1]


def identity(pid=None):
    pid = pid or os.getpid()
    return f"{pid}:{proc_start(pid)}"


def alive(ident):
    if not ident or ":" not in ident:
        return False
    pid, start = ident.split(":", 1)
    return start != "None" and proc_start(int(pid)) == start


# ---------------------------------------------------------------- Codex dispatch slots (2.0; concurrent since 0.3.0)
# One slot file per Codex dispatch, held by the process that runs Codex (an `ask` run or a worker
# supervisor) and released once its tree is dead. Codex's "one session per account" means one LOGIN, not
# one running process (3 parallel runs on one login: tested 2026-10-08), so asks and gates never wait.
# Only worker jobs are capped (`max_workers`): each drags a sandboxed re-run and a Claude review behind it.
#
# A slot is held while ANY of these holds (gate finding 1):
#   - its holder process is alive, or
#   - its lease has not expired (`start` reserving the slot its supervisor takes over), or
#   - any recorded member of the Codex process tree is alive (a supervisor's death frees nothing).
# An orphaned tree (holder dead, members alive) is killed before the slot is reclaimed.

def slots_dir():
    return HOME / "slots"


def slot_path(token):
    return slots_dir() / f"{token}.json"


def slots():
    """(path, body) for every readable slot file on this machine."""
    if not slots_dir().is_dir():
        return []
    return [(p, b) for p in sorted(slots_dir().glob("*.json")) if (b := read_json(p)) is not None]


def lock_held(h):
    if not h:
        return False
    if alive(h.get("holder")):
        return True
    if h.get("lease_until") and time.time() < h["lease_until"]:
        return True
    return any(alive(m) for m in h.get("tree", []))


def orphaned(h):
    """Holder dead and lease over, but Codex members still running: kill them, then the slot frees."""
    return (bool(h) and not alive(h.get("holder"))
            and not (h.get("lease_until") and time.time() < h["lease_until"])
            and any(alive(m) for m in h.get("tree", [])))


def is_worker(h):
    return (h.get("owner") or {}).get("kind") == "worker"


def lock_holder():
    """Every dispatch slot on this machine, stale ones flagged ([] when Codex is idle here)."""
    return [h if lock_held(h) else {**h, "stale": True} for _, h in slots()]


def running_workers(exclude_job=None):
    return [h for _, h in slots()
            if is_worker(h) and lock_held(h) and (h.get("owner") or {}).get("job") != exclude_job]


def kill_identities(idents):
    """TERM, then KILL whatever ignored it. Waits only when something was actually signalled."""
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):  # Windows: no SIGKILL
        live = [i for i in idents if alive(i)]
        if not live:
            return True
        for ident in live:
            try:
                os.kill(int(ident.split(":")[0]), sig)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(10):  # up to 1 s, returning as soon as they are gone
            if not any(alive(i) for i in live):
                break
            time.sleep(0.1)
        reap(live)
    return not any(alive(i) for i in idents)


def reap(idents):
    """Collect zombies of killed processes that are our children (orphans adopted as Linux subreaper).
    By pid only, never waitpid(-1): that could take the status a live Popen still waits for. The only
    Popen pid that can appear here is a contained child end_tree already waited for."""
    for ident in idents:
        try:
            os.waitpid(int(ident.split(":")[0]), os.WNOHANG)
        except (ChildProcessError, OSError, ValueError, AttributeError):
            pass  # not our child, already reaped, or Windows


def _reclaim(path, seen):
    """Remove a dead slot, under the slot mutex, only if it is still the exact slot judged dead.
    Orphaned Codex members (holder dead, lease over, tree alive) are killed first; if they won't die,
    no reclaim."""
    try:
        with slot_mutex():
            cur = read_json(path)
            if cur != seen:
                return
            if orphaned(cur):
                orphans = [m for m in cur.get("tree", []) if alive(m)]
                if cur.get("machine") == machine() and kill_identities(orphans):
                    cur = {**cur, "tree": []}
                else:
                    return
            if not lock_held(cur):
                path.unlink(missing_ok=True)
    except TimeoutError:
        time.sleep(0.2)


def _next_seq():
    """Dispatch counter (call under the slot mutex): a run whose counter moved on was overlapped."""
    f = HOME / "slots.seq"
    try:
        n = int(f.read_text(encoding="utf-8").strip() or 0) + 1
    except (OSError, ValueError):
        n = 1
    atomic_write(f, str(n))
    return n


def overlapped(token):
    """True when another Codex dispatch ran at any point during this slot's run (its quota delta then
    mixes in their spend). Call before releasing the slot."""
    h = read_json(slot_path(token)) or {}
    try:
        now_seq = int((HOME / "slots.seq").read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True  # unknown => never attribute a shared delta to one call
    return bool(h.get("others_at_start")) or now_seq != h.get("seq")


def lock_acquire(owner, wait_s, lease_s=None, takeover_job=None):
    """Take a dispatch slot. Asks and gates always get one; a worker waits (up to wait_s) while
    max_workers worker slots are live. lease_s: a timed lease with no holder process (`start` reserving
    the slot its supervisor takes over). takeover_job: that supervisor. Returns (busy info or None, token)."""
    slots_dir().mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_s
    token = uuid.uuid4().hex
    body = {"owner": owner, "since": now(), "machine": machine(), "tree": [],
            "holder": None if lease_s else identity(), "token": token,
            "lease_until": time.time() + lease_s if lease_s else None}
    worker = owner.get("kind") == "worker"
    while True:
        busy = None
        try:
            with slot_mutex():  # count + create in one critical section: two starts can't both take the last place
                for p, h in slots():
                    if not lock_held(h):
                        p.unlink(missing_ok=True)  # dead and no live tree: safe to drop under the mutex
                live = [(p, h) for p, h in slots() if lock_held(h)]
                reserved = [p for p, h in live if takeover_job and (h.get("owner") or {}).get("job") == takeover_job]
                workers = running_workers(exclude_job=takeover_job) if worker else []
                if reserved or not worker or not MAX_WORKERS or len(workers) < MAX_WORKERS:
                    write_json(slot_path(token), {**body, "seq": _next_seq(),
                                                  "others_at_start": len(live) - len(reserved)})
                    for p in reserved:
                        p.unlink(missing_ok=True)  # the supervisor inherits the reservation `start` made
                    return None, token
                busy = {"busy": f"{len(workers)} worker job(s) running; max_workers is {MAX_WORKERS}",
                        "running": [h.get("owner") for h in workers]}
        except TimeoutError:
            busy = {"busy": "slot mutex busy"}
        before = {p for p, _ in slots()}
        for p, h in slots():
            if not lock_held(h) or orphaned(h):
                _reclaim(p, h)
        if {p for p, _ in slots()} != before:
            continue  # something was reclaimed: re-check capacity at once
        if time.time() >= deadline:
            return busy, None
        time.sleep(2)


class slot_mutex:
    """Serializes every read-modify-write of the slots (acquire, reclaim, update, release): no stale
    owner can overwrite or unlink a slot someone else took in between, and capacity is counted exactly."""

    def __enter__(self):
        gate = HOME / "slots.mutex"
        for _ in range(200):
            try:
                os.close(os.open(gate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - gate.stat().st_mtime > 30:
                        gate.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
                time.sleep(0.05)
        raise TimeoutError("slot mutex busy")

    def __exit__(self, *exc):
        (HOME / "slots.mutex").unlink(missing_ok=True)


def lock_update(token, **fields):
    """Holder-only rewrite (record the Codex tree)."""
    with slot_mutex():
        h = read_json(slot_path(token))
        if h and h.get("token") == token:
            write_json(slot_path(token), {**h, **fields})
            return True
        return False


def lock_release(token):
    with slot_mutex():
        slot_path(token).unlink(missing_ok=True)


# Codex failing on auth mid-flight: most likely two runs refreshing the shared login's rotating token at
# once (untested; see the 2026-10-08 concurrency note). Retried once; a real logout fails again.
AUTH_ERROR = re.compile(r"(?i)\b401\b|unauthori[sz]ed|refresh[ _-]?token|token[ _-]?(?:expired|revoked)"
                        r"|invalid_grant|(?:log|sign)[ -]?in again")


# ---------------------------------------------------------------- contained processes (gate findings 1-2)

def popen_contained(argv, **kw):
    if IS_WIN:
        kw["creationflags"] = kw.get("creationflags", 0) | 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def end_tree(proc, members):
    """Kill a contained child's whole group plus every member identity seen; True when all are dead."""
    if IS_WIN:
        run(["taskkill", "/T", "/F", "/PID", str(proc.pid)])
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)  # EPERM on macOS = zombies only
            except (ProcessLookupError, PermissionError):
                break  # no live member left in the group: nothing to wait for
            time.sleep(0.2)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return kill_identities(members)


def run_contained(argv, stdin_text, out_path, err_path, timeout, token=None, cwd_sweep=None, env=None,
                  cancel_file=None):
    """Run a child in its own process group. On every exit path (exit, timeout, exception) the group and
    every descendant identity seen while it ran are killed; with cwd_sweep, so are orphaned processes born
    during the run whose cwd is in that directory. BEST-EFFORT, stated honestly: a descendant that
    double-forks, leaves the group AND leaves the directory between samples is not found (the old
    an ad-hoc subagent has no tracking at all). Output goes to files (a pipe could fill and stall the child).
    Members are recorded in the lock when a token is given. Returns (exit code or None on timeout, all_dead)."""
    started = time.time()
    # stdin comes from a file, not a pipe: a pipe write blocks forever if the child never reads, and that
    # would happen before the deadline below exists (gate finding: the ceiling skipped a blocking step).
    in_path = Path(str(out_path) + ".stdin")
    in_path.write_text(stdin_text or "", encoding="utf-8")
    with open(in_path, "r", encoding="utf-8") as in_f, open(out_path, "w", encoding="utf-8") as out_f, \
            open(err_path, "w", encoding="utf-8") as err_f:
        proc = popen_contained(argv, stdin=in_f, stdout=out_f, stderr=err_f, text=True,
                               encoding="utf-8", errors="replace", env=env)
        members, timed_out = set(), False
        try:
            members.add(identity(proc.pid))
            if token:
                lock_update(token, tree=sorted(members))
            deadline = started + timeout
            while proc.poll() is None:
                new = {identity(p) for p in descendants(proc.pid)} - members
                if new:
                    members |= new
                    if token:
                        lock_update(token, tree=sorted(members))
                if time.time() > deadline or (cancel_file and Path(cancel_file).exists()):
                    timed_out = True  # a cancel ends the run like a timeout; the caller tells them apart
                    break
                time.sleep(0.25)
        finally:
            if cwd_sweep:
                members |= worktree_procs(cwd_sweep, started)
            dead = end_tree(proc, members)
            if cwd_sweep:
                dead = kill_identities(worktree_procs(cwd_sweep, started)) and dead
    in_path.unlink(missing_ok=True)  # it held the prompt (code under review); outputs stay
    return (None if timed_out else proc.returncode), dead


# ---------------------------------------------------------------- jobs + state machine (2.0)

STATES = {"pending", "running", "exited", "receipted", "reviewing", "reviewed", "notified", "cancelled",
          "timeout", "crashed", "refused", "discarded"}
ACTIVE = {"pending", "running", "exited"}  # supervisor still owns the job


def job_dir(jid):
    return HOME / "jobs" / jid


def worktree_dir(jid):
    return HOME / "worktrees" / jid


def state_of(jid):
    return read_json(job_dir(jid) / "state.json", {}).get("state")


def transition(jid, state, **fields):
    assert state in STATES, state
    p = job_dir(jid) / "state.json"
    cur = read_json(p, {"history": []})
    cur["history"].append({"state": state, "at": now()})
    cur.update(fields, state=state, updated=now())
    write_json(p, cur)


def claim(jid, name):
    """Exclusive, once-only claim (O_EXCL). Two sessions never both review one job."""
    try:
        fd = os.open(job_dir(jid) / f"claim-{name}", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{identity()} {now()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


# ---------------------------------------------------------------- workspace + intake (2b)

def git_toplevel(path):
    """The git work tree containing `path`, or None outside any repo."""
    d = Path(path)
    d = d if d.is_dir() else d.parent
    if not d.is_dir():
        return None
    r = git(d, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def guard_roots(path=None):
    """Roots whose protected areas apply: the git repo containing `path` (its own .claude/, workflows, ...)
    plus every configured workspace root (a parent folder holding several repos, say)."""
    roots = [r.resolve() for r in WORKSPACE_ROOTS]
    top = git_toplevel(path) if path is not None else None
    return list(dict.fromkeys(([top] if top else []) + roots))


def norm(rel):
    """Repo-relative, forward slashes, leading './' segments removed (never lstrip: it eats dotfiles)."""
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.rstrip("/")


def secret(rel):
    """Never read or quoted: credentials, keys, VCS internals."""
    rel = norm(rel).casefold()  # macOS/Windows filesystems are case-insensitive: .ENV IS .env
    parts = set(rel.split("/"))
    name = rel.rsplit("/", 1)[-1]
    return (name in SECRET_NAMES or bool(parts & SECRET_PARTS) or name.endswith((".env", ".pem", ".key"))
            or any(x.startswith(".env") for x in parts)  # .env, .envrc, .env.prod, .env/values.txt
            or name.startswith(("id_rsa", "id_ed25519")) or rel in ("", "."))


def denied(rel):
    """Never WRITTEN by a worker: secrets plus the readable-but-protected backbone and personal context."""
    if secret(rel):
        return True
    rel = norm(rel).casefold()
    name = rel.rsplit("/", 1)[-1]
    return (rel.startswith(PROTECTED_PREFIXES) or name in PROTECTED_NAMES or bool(set(rel.split("/")) & PROTECTED_PARTS)
            or re.fullmatch(r"settings.*\.json", name) is not None)


def denied_root(path, write=True):
    """A cwd/repo that IS or sits inside a denied area. The relative lists alone could be bypassed by
    pointing cwd at the area itself. write=True (a worker repo): every protected area. write=False (an
    ask/gate cwd): secret areas only; ~/.claude is secret except its per-project memory folders."""
    r = Path(path).resolve()
    home = Path.home().resolve()

    def under(d):
        for base in (d, d.resolve()):  # d may be a symlink
            rel = rel_under(r, base)
            if rel is not None:
                return rel
        return None
    for d in (home / ".codex", home / ".ssh", home / ".aws", home / ".config", home / ".gnupg"):
        if under(d) is not None:
            return True
    rel = under(home / ".claude")
    if rel is not None:
        parts = rel.split("/")
        is_memory = len(parts) >= 3 and parts[0] == "projects" and parts[2].casefold() == "memory"
        if write or not is_memory:
            return True
    # Check against the repo that contains it and every configured workspace root: <root>/<repo>/.claude
    # is as protected relative to <repo> as .claude/ is relative to <root>.
    check = denied if write else secret
    for base in guard_roots(r):
        rel = rel_under(r, base)
        if rel is not None and rel != "." and check(rel + "/x"):
            return True
    return False


def rel_under(path, base):
    """path relative to base (posix), compared case-insensitively; None if path is not base or inside it."""
    # Compare component-wise: casefold can change a name's length (Straße/STRASSE), so string slicing lies.
    pp, bp = path.parts, base.parts
    if len(pp) < len(bp) or [x.casefold() for x in pp[:len(bp)]] != [x.casefold() for x in bp]:
        return None
    return "/".join(pp[len(bp):]) or "."


def bad_path(rel):
    p = rel.replace("\\", "/")
    return p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p) is not None


def in_scope(path, scope):
    path = norm(path)
    for s in scope:
        s = norm(s)
        if path == s or path.startswith(s + "/"):
            return True
    return False


def hash_manifest(wt, manifest):
    hashes = {}
    for rel in manifest:
        f = Path(wt) / rel
        hashes[rel] = git(wt, "hash-object", rel).stdout.strip() if f.is_file() else "absent"
    return hashes


# ---------------------------------------------------------------- codex invocation

def codex_version():
    try:
        r = run([CODEX, "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return (r.stdout.strip(), None) if r.returncode == 0 else (None, r.stderr.strip())


def codex_argv(cwd, sandbox, out_file, thread=None, model=MODEL, effort=EFFORT):
    argv = [CODEX, "exec", "--strict-config", "-s", sandbox, "-C", str(cwd), "--json"]
    if thread:
        argv += ["resume", thread]
    return argv + ["-c", f'model="{model}"', "-c", f'model_reasoning_effort="{effort}"', "-o", str(out_file), "-"]


def choose_model(lane, req):
    """(model, effort, reason). Mapped from required inputs; the driver may only RAISE (a stronger model,
    or the same model at higher effort). The one exception: a worker may go below its mapping with an
    `experiment` label (to benchmark the cheap model), and that is logged."""
    if lane == "gate":
        trig = req.get("trigger")
        if trig is None and req.get("protocol") is None:
            fail("gate refused: this codex-bridge MOD is older than its bridge.py (0.1.0 hook, 0.1.1 core): it cannot "
                 "send the required `trigger`. Restart Claude Code to reload the mod.")
        if trig not in GATE_TRIGGERS:
            fail(f"gate refused: trigger must name the gate rule that fired: one of {sorted(GATE_TRIGGERS)}")
        base, why = GATE_TRIGGERS[trig], f"trigger={trig}"
    elif lane == "worker":
        base, why = TIER_DEFAULT[req["tier"]], f"tier={req['tier']}"
    else:
        base, why = (STANDARD, "medium"), "ask default"
    model, effort = req.get("model") or base[0], req.get("effort") or base[1]
    allowed = ALLOWED[lane]
    if model not in allowed or effort not in allowed[model]:
        fail(f"refused: {model}/{effort} is not allowed for {lane}", allowed={m: list(e) for m, e in allowed.items()})
    lower = (MODEL_RANK[model] < MODEL_RANK[base[0]]
             or (model == base[0] and EFFORT_RANK[effort] < EFFORT_RANK[base[1]]))
    if lower:
        if lane != "worker" or model != CHEAP or not str(req.get("experiment") or "").strip():
            fail(f"refused: {model}/{effort} is below the mapping for {why} ({base[0]}/{base[1]}); "
                 "the driver may only raise")
        why += f"; BELOW mapping for experiment {str(req['experiment'])[:60]!r}"
    elif (model, effort) != base:
        why += "; raised by the driver"
    return model, effort, f"{model}/{effort} because {why}"


# ---------------------------------------------------------------- Codex usage limits (0.1.1)

def codex_limits(timeout=15):
    """The account's live limits from the Codex app server (`account/rateLimits/read`: an account lookup,
    no model turn, so it spends no quota). Experimental upstream API: any failure returns ok=False and
    callers proceed ("unknown" never blocks). Fake: CODEX_BRIDGE_LIMITS_JSON (tests)."""
    fake = os.environ.get("CODEX_BRIDGE_LIMITS_JSON")
    if fake is not None:
        try:
            return parse_limits(json.loads(fake))
        except ValueError as e:
            return {"ok": False, "error": f"bad fake: {e}"}
    try:
        proc = popen_contained([CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": f"app-server did not start: {e}"}
    lines, got = [], threading.Event()

    def reader():  # a thread, not select(): pipes aren't selectable on Windows
        for ln in proc.stdout:
            lines.append(ln)
            if '"id":2' in ln.replace(" ", ""):
                got.set()
                return
    threading.Thread(target=reader, daemon=True).start()
    try:
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "codex-bridge", "version": "0.1.1"}}},
                    {"jsonrpc": "2.0", "method": "initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None}):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if not got.wait(timeout):
            return {"ok": False, "error": f"no rate-limit reply within {timeout}s"}
        for ln in lines:
            try:
                m = json.loads(ln)
            except ValueError:
                continue
            if m.get("id") == 2:
                if "error" in m:
                    return {"ok": False, "error": str(m["error"])[:300]}
                return parse_limits(m.get("result") or {})
        return {"ok": False, "error": "rate-limit reply unparseable"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    finally:
        end_tree(proc, set())


def parse_limits(res):
    rl = res.get("rateLimits") or {}
    wins = {}
    for name in ("primary", "secondary"):
        w = rl.get(name)
        if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)):
            wins[name] = {"used": float(w["usedPercent"]), "window_min": w.get("windowDurationMins"),
                          "resets_at": w.get("resetsAt")}
    if not wins:
        return {"ok": False, "error": "no usage windows in reply"}
    return {"ok": True, "allowed": res.get("ordinaryUsageAllowed") is not False,
            "reached": rl.get("rateLimitReachedType"), "plan": rl.get("planType"), "windows": wins,
            "read_at": time.time()}


def window_label(name, w):
    m = w.get("window_min")
    return {300: "5h", 10080: "wk"}.get(m, f"{m}m" if m else name)


def reset_text(w):
    t = w.get("resets_at")
    if not isinstance(t, (int, float)):
        return "reset time unknown"
    mins = max(0, round((t - time.time()) / 60))
    when = dt.datetime.fromtimestamp(t).strftime("%a %H:%M")
    if mins >= 24 * 60:
        return f"resets {when} (in {mins // 1440}d{mins % 1440 // 60:02d}h)"
    return f"resets {when} (in {mins // 60}h{mins % 60:02d}m)" if mins >= 60 else f"resets {when} (in {mins} min)"


def quota_record(before, after, shared):
    """before/after/delta for one call. An overlapped call's delta mixes in other runs' spend: it is kept as
    shared_delta and `delta` is None, so it never reaches the per-call cost estimate."""
    d = quota_delta(before, after)
    if shared:
        return {"before": before, "after": after, "delta": None, "shared_delta": d, "overlapping": True}
    return {"before": before, "after": after, "delta": d}


def quota_delta(before, after):
    if not (before.get("ok") and after.get("ok")):
        return None
    return {n: round(after["windows"][n]["used"] - before["windows"][n]["used"], 2)
            for n in before["windows"] if n in after["windows"]}


def estimate(lane, window):
    """Median measured cost (percentage points) of one call in this lane, or None below MIN_SAMPLES."""
    path = LOG_PATH
    vals = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            d = row.get("quota_delta") if row.get("lane") == f"codex-bridge-{lane}" else None
            if isinstance(d, dict) and isinstance(d.get(window), (int, float)) and d[window] >= 0:
                vals.append(d[window])
    except OSError:
        return None
    return statistics.median(vals) if len(vals) >= MIN_SAMPLES else None


def preflight(lane, limits, running=0):
    """(refusal text or None, warnings). Quota never downgrades the model: it refuses or warns.
    running: worker jobs already in flight, whose measured cost is still to come out of the window."""
    if not limits.get("ok"):
        return None, [f"Codex limits unknown ({limits.get('error')}); proceeding"]
    warns, blocks = [], []
    for name, w in limits["windows"].items():
        label, used = window_label(name, w), w["used"]
        if lane == "worker":
            est = estimate("worker", name)
            if est is not None and used + est * (running + 1) > 100:
                inflight = f" plus {running} running" if running else ""
                blocks.append((w, f"{label} {used:.0f}% used; a worker job measures ~{est:.1f} points{inflight}"))
            elif est is None and used > WORKER_REFUSE_PCT[name]:
                blocks.append((w, f"{label} {used:.0f}% used (> {WORKER_REFUSE_PCT[name]}% worker cap)"))
        if used >= WARN_PCT and not any(b[0] is w for b in blocks):
            warns.append(f"Codex {label} {used:.0f}% used, {reset_text(w)}")
    if not limits["allowed"] or limits.get("reached"):
        named = limits["windows"].get(str(limits.get("reached") or ""))  # the window Codex says is exhausted
        hit = named or max(limits["windows"].values(), key=lambda w: w["used"])
        blocks = [(hit, f"limit reached ({limits.get('reached') or 'usage not allowed'})")]
    if not blocks:
        return None, warns
    w, why = blocks[0]
    msg = f"refused: Codex {why}; {reset_text(w)}."
    t = w.get("resets_at")
    if isinstance(t, (int, float)) and 0 <= t - time.time() <= RESET_HINT_S:
        msg += " That is soon: start it again after the reset."
    return msg, warns


def cmd_usage():
    lim = codex_limits()
    out_lim = dict(lim)
    if lim.get("ok"):
        out_lim["summary"] = " · ".join(f"{window_label(n, w)} {w['used']:.0f}% ({reset_text(w)})"
                                        for n, w in lim["windows"].items())
        out_lim["preflight"] = {lane: preflight(lane, lim)[0] for lane in ("ask", "gate", "worker")}
        out_lim["estimates"] = {lane: {n: estimate(lane, n) for n in lim["windows"]} for lane in ("ask", "gate", "worker")}
    return out_lim


def parse_events(path):
    """thread id, summed token usage, command executions. No per-file read events exist (Phase 1.1)."""
    thread, usage, commands = None, {}, []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started":
            thread = ev.get("thread_id")
        elif ev.get("type") == "turn.completed":
            for k, v in (ev.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
        elif ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "command_execution":
            it = ev["item"]
            commands.append({"command": it.get("command", "")[:300], "exit_code": it.get("exit_code")})
    return thread, usage, commands


def block(text, name):
    """Text between a NAME line and END-NAME line in Codex's final message, or None."""
    m = re.search(rf"^{name}\s*$(.*?)^END-{name}\s*$", text or "", re.M | re.S)
    return m.group(1).strip("\n") if m else None


# ---------------------------------------------------------------- read canary (2a, Phase 1 results)

def pick_canaries(cwd, files):
    """One random distinctive line per named file. A path mention or a failed read can't quote it."""
    rng = random.SystemRandom()
    picks = {}
    for rel in files:
        f = Path(cwd) / rel
        if not f.is_file():
            picks[rel] = {"line": None, "expect": None, "absent": True}  # must appear in the supplied diff
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        counts = {}
        for ln in lines:
            counts[ln.strip()] = counts.get(ln.strip(), 0) + 1
        in_block, blocked = False, set()
        for i, ln in enumerate(lines):  # PEM-style blocks: header, body and footer are all off limits
            if "-----BEGIN" in ln:
                in_block = True
            if in_block:
                blocked.add(i)
            if "-----END" in ln:
                in_block = False
        ok = lambda i, ln: (ln.strip() and counts[ln.strip()] == 1 and i not in blocked  # noqa: E731
                            and not SECRETISH.search(ln) and not OPAQUE.match(ln.strip()))
        good = ([i for i, ln in enumerate(lines) if len(ln.strip()) >= 12 and ok(i, ln)]
                or [i for i, ln in enumerate(lines) if ok(i, ln)])
        if good:
            i = rng.choice(good)
            picks[rel] = {"line": i + 1, "expect": lines[i].strip()}
        else:
            picks[rel] = {"line": None, "expect": None}  # nothing distinctive to quote: no evidence possible
    return picks


def unwrap(quote):
    """Accept the quote as written or with ONE layer of markdown/quote wrapping (`x`, "x", 'x').
    Exact content is still required; only the wrapper is forgiven (smoke test: real Codex adds backticks)."""
    forms = {quote}
    for a, b in (("`", "`"), ('"', '"'), ("'", "'")):
        if len(quote) >= 2 and quote.startswith(a) and quote.endswith(b):
            forms.add(quote[1:-1].strip())
    return forms


def in_diff(rel, diff):
    """An absent named file is grounded only by a diff section that really deletes it."""
    rel, lines = norm(rel), (diff or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"diff --git a/{rel} ") or ln == f"--- a/{rel}":
            section = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("diff --git "):
                    break
                section.append(nxt)
            if any(x.startswith("deleted file mode") or x == "+++ /dev/null" for x in section) and \
                    any(x.startswith("-") and not x.startswith("---") for x in section):
                return True
    return False


def check_canaries(final, picks, diff=""):
    body = block(final, "READ-RECEIPT") or ""
    got = {}
    for ln in body.splitlines():
        m = re.match(r"\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(.*)$", ln)
        if m:
            got[m.group(1).strip()] = (int(m.group(2)), m.group(3).strip())
    result = {}
    for rel, p in picks.items():
        if p.get("absent"):
            result[rel] = in_diff(rel, diff)  # no file on disk: grounded only by the supplied diff
            continue
        q = got.get(rel)
        result[rel] = bool(p["expect"]) and q is not None and q[0] == p["line"] and p["expect"] in unwrap(q[1])
    return result


def objections(final):
    b = block(final, "CONTEXT-OBJECTIONS")
    if b is None:
        return ["(no CONTEXT-OBJECTIONS block: treated as unresolved)"]
    items = [ln.strip("-* ").strip() for ln in b.splitlines() if ln.strip()]
    return [] if items in ([], ["none"], ["None"]) else items


def need_files(objs):
    return [o.split(None, 1)[1].strip().strip("`") for o in objs if o.startswith("NEED-FILE") and len(o.split(None, 1)) > 1]


# ---------------------------------------------------------------- ask / gate (2a)

def prompt_file(name):
    return (Path(__file__).parent / "prompts" / name).read_text(encoding="utf-8")


def fill(template, **vals):
    """Single-pass substitution, so text inside one value can't inject another {{SLOT}}."""
    return re.sub(r"\{\{([A-Z]+)\}\}", lambda m: str(vals.get(m.group(1), m.group(0))), template)


def cmd_ask(req):
    mode = req.get("mode")
    if mode not in ("ask", "gate"):
        fail("mode must be ask or gate")
    cwd = Path(req.get("cwd") or ".").resolve()
    if denied_root(cwd, write=False):
        fail(f"refused: cwd {cwd} is inside a secret area")
    files = [f for f in req.get("files") or []]
    diff = req.get("diff") or ""
    if mode == "gate" and not (files or diff.strip()):
        fail("gate refused: no named files and no diff. Never pick the review target.")
    if any(bad_path(f) for f in files):
        fail("files must be repo-relative paths without '..'")
    root = cwd.resolve()

    def risky(f):
        p = cwd / f
        if p.is_symlink() or p.is_dir():
            return True
        if p.exists():
            r = p.resolve()
            if root not in r.parents:
                return True
            f = str(r.relative_to(root))
        # A parent cwd (e.g. ~) must not reach a secret area by a relative name (.claude/settings.json).
        return secret(f) or denied_root(root / f, write=False)
    risky_files = [f for f in files if risky(f)]
    if risky_files:
        fail("refused: symlinked, outside-repo or secret-bearing files can't be named "
             "(their lines would be quoted off-machine)", files=risky_files)
    model, effort, reason = choose_model(mode, req)
    version, err = codex_version()
    if not version:
        fail(f"codex unreachable: {err}")
    holder, token = lock_acquire({"kind": mode, "pid": os.getpid()}, 0)
    if holder:  # asks are never capped: only a jammed slot mutex lands here
        out({"ok": False, "error": f"busy: {holder.get('busy')}", "holder": holder}, 1)
    result = None
    try:
        before = codex_limits()  # the delta is this call's alone only if nothing overlapped (see overlapped())
        refusal, warns = preflight(mode, before)
        if refusal:
            fail(refusal, limits=before)
        result = _ask_locked(mode, cwd, files, diff, req.get("prompt") or "", version, token,
                             model=model, effort=effort, reason=reason, before=before, warns=warns)
        return result
    finally:
        h = read_json(slot_path(token)) or {}
        live = [m for m in h.get("tree", []) if alive(m)] if h.get("token") == token else []
        if not live and (result is None or result.get("tree_dead", True)):
            lock_release(token)  # else: members stay recorded; the slot frees only once they are dead


def _ask_locked(mode, cwd, files, diff, user_prompt, version, token, model=MODEL, effort=EFFORT, reason="",
                before=None, warns=()):
    run_id = uuid.uuid4().hex
    rdir = HOME / "runs" / run_id
    rdir.mkdir(parents=True)
    picks = pick_canaries(cwd, files)
    hashes = {f: hashlib.sha256((cwd / f).read_bytes()).hexdigest() for f in files if (cwd / f).is_file()}
    base = git(cwd, "rev-parse", "HEAD").stdout.strip() or None
    canary_lines = "\n".join(f"{rel} | {p['line']}" for rel, p in picks.items() if p["line"])
    prompt = fill(prompt_file("gate.md" if mode == "gate" else "ask.md"), TASK=user_prompt,
                  FILES="\n".join(files) or "(none named)", DIFF=diff or "(no diff supplied)",
                  CANARIES=canary_lines or "(none)")
    thread, usage, attempts, final, exit_code, last_ok, tree_dead = None, {}, [], "", None, None, True
    auth_retry = None
    deadline = PROCESS_START + ASK_DEADLINE_S - TEARDOWN_MARGIN_S
    for attempt in range(MAX_RESUMES + 1):
        remaining = deadline - time.time()
        if remaining < 30:
            attempts.append({"exit": None, "note": "ask budget exhausted before this follow-up"})
            break
        ofile, efile = rdir / f"final-{attempt}.txt", rdir / f"events-{attempt}.jsonl"
        text = prompt if attempt == 0 else fill(prompt_file("resume.md"), FILES="\n".join(wanted))
        while True:
            started = time.time()
            rc, dead = run_contained(codex_argv(cwd, "read-only", ofile, thread, model, effort), text, efile,
                                     rdir / f"stderr-{attempt}.log", max(1, deadline - time.time()), token=token)
            tree_dead = tree_dead and dead
            err = (rdir / f"stderr-{attempt}.log").read_text(encoding="utf-8", errors="replace") if rc else ""
            if rc and dead and not auth_retry and AUTH_ERROR.search(err) and deadline - time.time() > 30:
                auth_retry = {"attempt": attempt, "note": "Codex auth error (likely a concurrent token refresh); "
                              "retried once", "stderr_tail": err[-400:]}
                time.sleep(2)
                continue
            break
        if rc is None:
            attempts.append({"exit": None, "note": f"attempt {attempt} timed out (ask budget)", "tree_dead": dead})
            break
        exit_code = rc
        stderr = (rdir / f"stderr-{attempt}.log").read_text(encoding="utf-8", errors="replace")
        fresh = ofile.is_file() and ofile.stat().st_mtime >= started - 1
        t, u, cmds = parse_events(efile)
        thread = thread or t
        for k, v in u.items():
            usage[k] = usage.get(k, 0) + v
        attempts.append({"exit": exit_code, "fresh_output": fresh, "commands": len(cmds),
                         "stderr_tail": stderr[-400:] if exit_code else ""})
        if exit_code != 0 or not fresh:
            break  # keep the last good answer; a failed follow-up must not erase attempt 0's critique
        final = ofile.read_text(encoding="utf-8", errors="replace")
        last_ok = attempt
        # Resume only when a follow-up can actually help: Codex re-reads a file the CALLER named (already
        # intake-vetted). A request outside that list fails
        # closed back to the driver (computed BLOCKER below), never an instruction to read it.
        # BLOCKERs (unwritten edit, out-of-scope, requester-only context) fail closed without spending a round.
        wanted = need_files(objections(final))
        if mode != "gate" or not thread or not wanted or not set(wanted) <= set(files):
            break
    reads = check_canaries(final, picks, diff)
    after = {f: hashlib.sha256((cwd / f).read_bytes()).hexdigest() for f in files if (cwd / f).is_file()}
    base_after = git(cwd, "rev-parse", "HEAD").stdout.strip() or None
    objs = objections(final) if final else ["(no final message)"]
    unnamed = sorted(set(need_files(objs)) - set(files))
    if unnamed:
        objs = objs + [f"BLOCKER Codex asked for files outside the caller's list: {unnamed}; the driver decides "
                       "whether to name them in a new gate (computed)"]
    if after != hashes or base_after != base:
        objs = objs + ["BLOCKER named target files or HEAD changed while Codex was reviewing (computed)"]
    # Diff-only gates are grounded by the embedded diff; every named on-disk file needs read evidence.
    # Satisfied needs the LAST attempt to have succeeded: a failed follow-up leaves objections unresolved.
    last_attempt_ok = last_ok is not None and last_ok == len(attempts) - 1
    satisfied = (last_attempt_ok and bool(final) and all(reads.values()) and not objs) if mode == "gate" else None
    lim_after = codex_limits()
    shared = overlapped(token)
    result = {
        "ok": bool(final), "mode": mode, "run_id": run_id, "codex_version": version,
        "cwd": str(cwd), "base_rev": base, "file_hashes": hashes, "thread_id": thread,
        "read_evidence": {rel: (("absent on disk; present in supplied diff" if picks[rel].get("absent")
                                 else "quoted line %s verbatim" % picks[rel]["line"]) if ok else "NO EVIDENCE")
                          for rel, ok in reads.items()},
        "context_objections": objs, "attempts": attempts, "usage": usage,
        "model": model, "effort": effort, "model_reason": reason,
        "effort_note": "passed per call; --strict-config validated the key; effective value unverified",
        "quota": quota_record(before or {}, lim_after, shared),
        "warnings": list(warns), "resume": f"codex resume {thread}" if thread else None,
        "final": final, "gate_satisfied": satisfied, "tree_dead": tree_dead,
        **({"auth_retry": auth_retry} if auth_retry else {}),
    }
    write_json(rdir / "result.json", result)
    return result


# ---------------------------------------------------------------- worker intake (2b)

REQUIRED = ("task", "repo", "scope", "verify", "tier", "done", "manifest", "attestation")


def plain_file_in(root, rel):
    """A regular, non-symlinked, non-secret file that resolves inside root: safe to point a reader at."""
    p = root / rel
    if not p.is_file() or secret(rel):
        return False
    # No symlink at ANY level: the resolved path must be exactly root/rel (case-insensitively), so a
    # symlinked ancestor (docs -> .claude) is refused; and the resolved spelling must not be denied either.
    real = rel_under(p.resolve(), root.resolve())
    return real is not None and real.casefold() == norm(rel).casefold() and not secret(real)


def cmd_start(req):
    missing = [k for k in REQUIRED if not req.get(k)]
    if missing:
        fail(f"refused: missing required inputs {missing} (never infer them)")
    if req["tier"] not in ("R0", "R1", "R2"):
        fail("tier must be R0, R1 or R2")
    model, effort, reason = choose_model("worker", req)
    version, err = codex_version()
    if not version:
        fail(f"codex unreachable: {err}")
    if not selftest_ok(version):
        fail("refused: workers need a passing sandbox selftest on this machine for this codex version. "
             "Run `bridge.py selftest` (or run /codex-bridge setup).",
             proof=read_json(HOME / "selftest.json"))
    reads = list(req["manifest"]) + list(req.get("companions") or [])
    bad = [p for p in list(req["scope"]) + reads if bad_path(p)]
    # Scope = writes: backbone and personal context are write-protected (the code-level stand-in for the
    # old agent's consequence judgment). Manifest/companions = reads: only secrets are refused.
    hit = [p for p in req["scope"] if denied(p)] + [p for p in reads if secret(p)]
    if bad or hit:
        fail("refused: scope/manifest paths (scope may not touch protected or secret paths; reads may not "
             f"touch secrets). To change a protected file, have the worker write {SUGGESTED_PATCH} instead",
             bad=bad, denylisted=hit)
    repo = Path(req["repo"]).resolve()
    if denied_root(repo):
        fail(f"refused: repo {repo} is inside a denylisted area")
    if git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        fail(f"not a git work tree: {repo}")
    linked = [c for c in req.get("companions") or [] if not plain_file_in(repo, c)]
    if linked:
        fail("refused: companions must be regular files inside the repo (no symlinks, no directories)", files=linked)
    # The review worktree is built from HEAD: a companion that is not committed would silently vanish.
    untracked = [c for c in req.get("companions") or [] if git(repo, "cat-file", "-e", f"HEAD:{norm(c)}").returncode != 0]
    if untracked:
        fail("refused: companions must be committed at HEAD (the review worktree is built from HEAD)", files=untracked)
    deps = [dep_info(repo, d) for d in req.get("deps") or []]
    limits = codex_limits()
    refusal, warns = preflight("worker", limits, running=len(running_workers()))
    if refusal:
        fail(refusal, limits=limits)
    req = {**req, "_model": model, "_effort": effort, "_model_reason": reason, "_deps": deps, "_warnings": warns}
    jid = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    # Reserve the slot NOW (a lease the supervisor takes over), so no other worker takes the last place.
    holder, token = lock_acquire({"kind": "worker", "job": jid}, 0, lease_s=120)
    if holder:
        out({"ok": False, "error": f"busy: {holder.get('busy')}; start it again when one finishes "
             "(or raise max_workers in the config)", "holder": holder}, 1)
    try:
        return _start_reserved(req, repo, jid)
    except BaseException:
        lock_release(token)  # the supervisor never took the lease over: free the slot now
        if job_dir(jid).is_dir() and not (job_dir(jid) / "job.json").exists():
            shutil.rmtree(job_dir(jid), ignore_errors=True)
        raise


def _start_reserved(req, repo, jid):
    wt = worktree_dir(jid)
    for base in [Path(repo).resolve(), *guard_roots(repo)]:
        if wt.resolve() == base or base in wt.resolve().parents:
            fail(f"worktree path {wt} is inside {base}: set CODEX_BRIDGE_HOME outside your repos")
    job_dir(jid).mkdir(parents=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = git(repo, "worktree", "add", str(wt), "HEAD", "--detach")
    if r.returncode != 0:
        fail("git worktree add failed", stderr=r.stderr[-500:])
    job = {"id": jid, "machine": machine(), "owner": req.get("owner"), "created": now(), "repo": str(repo), "worktree": str(wt),
           "base_rev": git(wt, "rev-parse", "HEAD").stdout.strip(),
           "model": req["_model"], "effort": req["_effort"], "model_reason": req["_model_reason"],
           "deps": req["_deps"],
           "inputs": {**{k: req[k] for k in REQUIRED},
                      "verifier_changes_allowed": bool(req.get("verifier_changes_allowed")),
                      "companions": list(req.get("companions") or []), "baseline": req.get("baseline") or ""},
           "manifest_hashes": hash_manifest(wt, req["manifest"])}
    write_json(job_dir(jid) / "job.json", job)
    transition(jid, "pending")
    if not os.environ.get("CODEX_BRIDGE_NO_SPAWN"):  # test seam: tests run the supervisor in-process
        spawn_detached([sys.executable, str(Path(__file__).resolve()), "supervise", jid], job_dir(jid))
    return {"ok": True, "job_id": jid, "worktree": str(wt), "model": job["model"], "effort": job["effort"],
            "model_reason": job["model_reason"], "warnings": req.get("_warnings") or []}


def py_name():
    return "python" if IS_WIN else "python3"


def dep_info(repo, rel):
    """A dependency dir the sandboxed verify may READ from the live checkout. 0.1.1 supports Python venvs
    only (exposed via PYTHONPATH; a venv's own interpreter can't start under the sandbox, probe
    2026-10-08). node_modules is parked until a task needs it."""
    if bad_path(rel) or secret(rel):
        fail(f"refused: dep {rel!r} is not a plain repo-relative path")
    live = repo / rel
    real = rel_under(live.resolve(), repo.resolve()) if live.is_dir() else None
    # No symlink at ANY level (an ancestor alias into .git would pass a last-component check), and the
    # resolved spelling must not be secret either.
    if real is None or real.casefold() != norm(rel).casefold() or secret(real):
        fail(f"refused: dep {rel!r} must be a real, non-symlinked directory inside the repo")
    cfg = live / "pyvenv.cfg"
    if cfg.is_symlink() or not cfg.is_file():
        fail(f"refused: dep {rel!r} is not a Python venv (only venvs are supported in 0.1.1)")
    sps = sorted(live.glob("lib/python3*/site-packages")) or [p for p in [live / "Lib" / "site-packages"] if p.is_dir()]
    sps = [sp for sp in sps if not sp.is_symlink() and rel_under(sp.resolve(), live.resolve()) is not None]
    if not sps:
        fail(f"refused: no site-packages inside venv {rel!r}")
    # Only site-packages becomes readable (the base interpreter + PYTHONPATH needs nothing else; a venv's
    # bin/ is interpreter symlinks out of the venv by design). The sandbox's secret globs cover only the
    # worktree, so refuse a site-packages that holds anything secret-named or links out of itself.
    sp_real = sps[0].resolve()
    for dirpath, dirnames, filenames in os.walk(sps[0]):
        for nm in dirnames + filenames:
            full = Path(dirpath, nm)
            relp = full.relative_to(sps[0]).as_posix()
            # The full secret rule, with ONE exemption: public CA bundles named cacert.pem (certifi, pip's
            # vendored copy), which every real venv ships. Any other .pem/.key refuses the venv: fail closed.
            public_ca = nm.casefold() == "cacert.pem"
            if (secret(relp) and not public_ca) or (full.is_symlink() and rel_under(full.resolve(), sp_real) is None):
                fail(f"refused: site-packages of {rel!r} contains {relp!r} (secret-named, or a symlink out of it)")
    m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", cfg.read_text(encoding="utf-8", errors="replace"), re.M)
    venv_v = f"{m.group(1)}.{m.group(2)}" if m else None
    r = run([py_name(), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
    sys_v = r.stdout.strip()
    if venv_v and venv_v != sys_v:
        fail(f"refused: venv {rel!r} is Python {venv_v} but the verify interpreter `{py_name()}` is {sys_v or 'missing'}; "
             "compiled packages would not load")
    return {"rel": rel, "path": str(sp_real), "site_packages": str(sp_real)}  # path = the ONLY read grant


def spawn_detached(argv, logdir):
    log = open(Path(logdir) / "supervisor.log", "a", encoding="utf-8")
    kw = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


# ---------------------------------------------------------------- supervisor (2b)

def descendants(pid):
    """Every descendant pid of `pid` right now (ancestry is only visible while the parent lives)."""
    if IS_WIN:
        return []
    r = run(["ps", "-A", "-o", "pid=,ppid="])
    kids = {}
    for ln in r.stdout.splitlines():
        try:
            p, pp = map(int, ln.split())
        except ValueError:
            continue
        kids.setdefault(pp, []).append(p)
    seen, stack = [], [pid]
    while stack:
        for c in kids.get(stack.pop(), []):
            seen.append(c)
            stack.append(c)
    return seen


def worktree_procs(wt, since):
    """Escaped writers: processes whose cwd is inside the worktree AND that are orphaned (parent died,
    reparented to launchd/init, or to this supervisor as Linux subreaper, from another session) AND were
    born after `since`. That is the setsid/double-fork escape. A process with any other living parent (a
    shell, a terminal's child, a direct child of ours in our session) is never matched. A direct child in
    another session IS matched: fine for a supervisor (a job-dedicated process), not for a shared caller
    that starts unrelated detached children in the worktree (the in-process test seam). A detached process a
    PERSON starts in a job's worktree during the run (`nohup ... &`, a GUI app opened there) IS matched
    and killed: worktrees are bridge-owned, so don't run things in one while its job runs. Best-effort:
    an escape that also leaves the worktree is not found here."""
    if IS_WIN:
        return set()
    r = run(["lsof", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"])
    root, pid, cands = str(Path(wt).resolve()), None, []
    for ln in r.stdout.splitlines():
        if ln.startswith("p"):
            pid = int(ln[1:])
        elif ln.startswith("n") and pid and pid != os.getpid():
            if ln[1:] == root or ln[1:].startswith(root + "/"):
                cands.append(pid)
    found = set()
    for pid in cands:
        info = run(["ps", "-o", "ppid=,lstart=", "-p", str(pid)]).stdout.strip().split(None, 1)
        if len(info) < 2 or not (info[0] == "1" or (info[0] == str(os.getpid()) and foreign_session(pid))):
            continue
        try:
            born = time.mktime(time.strptime(info[1].strip(), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        if born >= since - 1:
            found.add(identity(pid))
    return found


def cmd_supervise(jid):
    try:
        _supervise(jid)
    except Exception as e:  # never leave a job without a terminal state
        transition(jid, "crashed", reason=f"supervisor error: {type(e).__name__}: {e}", quarantine=True)
        raise


def become_subreaper():
    """Linux: orphans of our descendants reparent to us, not to init. Under systemd (user manager) or a
    CI runner, the nearest subreaper is otherwise some other living process, so an escaped child's ppid
    is not 1 and worktree_procs would never match it. No-op elsewhere (macOS orphans go to launchd, pid 1)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        return prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def foreign_session(pid):
    """True when `pid` is not in our session: Codex runs in its own (popen_contained), so an orphan
    reparented to us from its tree is foreign, while our own direct children are not."""
    try:
        return os.getsid(pid) != os.getsid(0)
    except OSError:
        return False


def _supervise(jid):
    if not claim(jid, "supervise"):
        return  # a supervisor already ran (or runs) for this job: never two
    become_subreaper()
    job = read_json(job_dir(jid) / "job.json")
    transition(jid, "pending", supervisor=identity())
    holder, token = lock_acquire({"kind": "worker", "job": jid}, LOCK_WAIT_WORKER_S, takeover_job=jid)
    if holder:
        transition(jid, "refused", reason=f"no worker slot: {holder.get('busy')}", holder=holder)
        return
    jd, wt, inp = job_dir(jid), Path(job["worktree"]), job["inputs"]
    cancel = jd / "cancel"
    if cancel.exists():
        lock_release(token)
        transition(jid, "cancelled", note="cancelled before Codex started")
        return
    started, dead, before, shared = time.time(), False, codex_limits(), True
    after = {"ok": False, "error": "Codex run raised before the after-read"}
    try:
        prompt = fill(prompt_file("worker.md"), TASK=inp["task"], SCOPE="\n".join(inp["scope"]),
                      VERIFY=inp["verify"], TIER=inp["tier"], DONE=inp["done"],
                      PROTECTED=", ".join(sorted(PROTECTED_PREFIXES) + sorted(PROTECTED_NAMES)))
        (jd / "prompt.md").write_text(prompt, encoding="utf-8")
        version, _ = codex_version()
        transition(jid, "running", codex_version=version, started_ts=started)
        for auth_try in (0, 1):
            rc, dead = run_contained(codex_argv(wt, "workspace-write", jd / "final.txt", None,
                                                job.get("model", MODEL), job.get("effort", EFFORT)), prompt,
                                     jd / "events.jsonl", jd / "codex-stderr.log", WORKER_CEILING_S - (time.time() - started),
                                     token=token, cwd_sweep=wt, cancel_file=cancel)
            err = (jd / "codex-stderr.log").read_text(encoding="utf-8", errors="replace") if rc else ""
            # Retry an auth failure once, and only while the worktree is untouched: a rerun on top of a
            # partial edit would be a different job.
            if (auth_try == 0 and rc and dead and not cancel.exists() and AUTH_ERROR.search(err)
                    and not git(wt, "status", "--porcelain", "--untracked-files=all").stdout.strip()):
                shutil.copy(jd / "codex-stderr.log", jd / "codex-stderr-auth.log")
                transition(jid, "running", auth_retry="Codex auth error on an untouched worktree; retried once")
                time.sleep(2)
                continue
            break
        after = codex_limits()
        shared = overlapped(token)  # read before the slot is released
    finally:
        if dead:
            lock_release(token)
        # else: the slot keeps the live members recorded; _reclaim kills them before anyone reuses it
    if not dead:
        transition(jid, "crashed", reason="Codex process tree still alive after kill; slot kept", quarantine=True)
        return
    wall = round((time.time() - started) / 60, 1)
    write_json(jd / "quota.json", quota_record(before, after, shared))
    if cancel.exists():
        transition(jid, "cancelled", wallclock_min=wall, quarantine=True,
                   note="cancelled mid-run: worktree is partial, do not review")
        return
    if rc is None:
        transition(jid, "timeout", wallclock_min=wall, quarantine=True,
                   note="timed out: worktree quarantined, unsalvageable, do not review")
        return
    transition(jid, "exited", exit_code=rc, wallclock_min=wall)
    if cancel.exists():  # the decision point a cancel that arrived during `running`/`exited` is guaranteed to hit
        transition(jid, "cancelled", wallclock_min=wall, quarantine=True, note="cancelled as Codex finished: do not review")
        return
    # A proposed edit to a protected file leaves the worktree before accounting: shown, never applied.
    sp = wt / SUGGESTED_PATCH
    if sp.is_symlink():
        sp.unlink()
    elif sp.is_file():
        shutil.move(str(sp), str(jd / "suggested.patch"))
    err = (jd / "codex-stderr.log").read_text(encoding="utf-8", errors="replace") if (jd / "codex-stderr.log").is_file() else ""
    quota_hit = rc != 0 and ((after.get("ok") and (not after["allowed"] or after.get("reached")))
                             or re.search(r"(?i)usage limit|rate limit", err) is not None)
    receipt = compute_receipt(jid, job, rc, started, quota_hit=quota_hit)
    if cancel.exists():  # a cancel during verify/accounting (state `exited`) wins: never review it
        transition(jid, "cancelled", outcome="cancelled", receipt_outcome=receipt["outcome"], quarantine=True,
                   note="cancelled during verification: receipt kept for the record, not reviewed")
        return
    transition(jid, "receipted", outcome=receipt["outcome"])


# ---------------------------------------------------------------- verify normalization (2c)

# Every terminal escape: CSI in any parameter form (incl. colon, e.g. ESC[38:2::255:0:0m), OSC (ESC ] ... BEL|ST),
# other two-byte escapes, then any stray control character. A summary hidden by an escape would vanish.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]")
PYTEST_SUMMARY = re.compile(r"^(?:=+ )?((?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)"
                            r"(?:, )?)+) in [\d.]+s(?: \([^)]*\))?(?: =+)?$")
UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s$")  # not bun's "Ran N tests across N files"


def normalize(text, exit_code):
    """Counts from ONE recognised test runner's own summary lines: pytest final summaries, unittest
    "Ran N tests in" + its OK/FAILED line, bun / `claude plugin test` pass/fail/error lines, jest
    "Tests:"/"Test Suites:", vitest "Tests"/"Test Files". Several runs of the SAME runner sum.
    Summaries of TWO different recognised runners => None: a composite verify (`a; b`) can mask a failing
    suite behind a passing one and its own exit code. (Only recognised summaries are detected, so this is a
    guard, not proof that one runner ran: the driver must still use one runner per verify.) An interrupted
    pytest run ("!!!" banner / KeyboardInterrupt) => None.
    None = unrecognised, mixed, or a unittest run without its verdict line = never clean."""
    n = {"exit": exit_code, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failing": []}
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x9b", "\x1b[")  # \r = a line boundary
    lines = ANSI.sub("", t).splitlines()
    runners, failing = set(), set()

    def count(chunk, word):
        m = re.search(rf"(\d+) {word}", chunk)
        return int(m.group(1)) if m else 0
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if re.match(r"^!{3,}", ln) or "KeyboardInterrupt" in ln:
            return None  # an interrupted pytest run can still print "N passed": never clean
        m = PYTEST_SUMMARY.match(ln)
        if m:
            runners.add("pytest")
            c = m.group(1)
            n["passed"] += count(c, "passed")
            n["failed"] += count(c, "failed")
            n["errors"] += count(c, r"errors?\b")
            n["skipped"] += count(c, "skipped")
            continue
        m = UNITTEST_RAN.match(ln)
        if m:
            verdict = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            vm = re.match(r"^(OK|FAILED)\b(?: \(([^)]*)\))?$", verdict)
            if not vm:
                return None  # a unittest run without its verdict line: unparseable, never clean
            parts = {k.strip(): v for k, v in re.findall(r"([a-z ]+)=(\d+)", vm.group(2) or "")}  # "expected failures" != failures
            f, e, sk = int(parts.get("failures", 0)), int(parts.get("errors", 0)), int(parts.get("skipped", 0))
            if vm.group(1) == "FAILED" and not (f or e):
                f = 1  # FAILED with no counts still failed
            runners.add("unittest")
            n["failed"] += f
            n["errors"] += e
            n["skipped"] += sk
            n["passed"] += int(m.group(1)) - f - e - sk
            continue
        m = re.match(r"^(\d+) (pass|fail|skip|errors?)$", ln)  # bun / claude plugin test
        if m:
            runners.add("bun")
            key = {"pass": "passed", "fail": "failed", "skip": "skipped"}.get(m.group(2), "errors")
            n[key] += int(m.group(1))
            continue
        m = re.match(r"^Test Suites:\s+(.+)$", raw) or re.match(r"^\s*Test Files\s{2,}(\d.+)$", raw)  # jest / vitest
        if m:
            runners.add("js")
            n["errors"] += count(m.group(1), "failed")  # a suite that failed to run has no failing test
            continue
        m = re.match(r"^Tests:\s+(.+)$", raw) or re.match(r"^\s+Tests\s{2,}(\d.+)$", raw)
        if m:
            runners.add("js")
            for key in ("passed", "failed", "skipped"):
                n[key] += count(m.group(1), key)
            continue
        m = re.match(r"^\s*Errors\s+(\d+) errors?\b", raw) or re.match(r"^Vitest caught (\d+) unhandled errors?", ln)
        if m:  # vitest's separate unhandled-error summary
            runners.add("js")
            n["errors"] = max(n["errors"], int(m.group(1)))  # both lines describe the same errors
            continue
        if ln == "Test suite failed to run":
            n["errors"] += 1  # jest prints this for import/setup failures; counted even if a summary was cut
        # Test identities only (pytest "FAILED a.py::t[param id]", unittest "FAIL: t (mod.C)", bun "(fail) a > b"):
        # a captured log line like "ERROR logger: ..." must not read as a failing test.
        fm = (re.match(r"^(?:FAILED|ERROR) (\S+\.py(?:::.+?)?)(?: - .*)?$", ln)  # incl. collection errors + nested param ids
              or re.match(r"^(?:FAIL|ERROR): (\w+ \([\w.]+\))", ln)
              or re.match(r"^\(fail\) (.+?)(?: \[[\d.]+m?s\])?$", ln))
        if fm:
            failing.add(fm.group(1))
    if len(runners) != 1:
        return None  # none recognised, or two different runners: never clean
    n["failing"] = sorted(failing)
    return n


def same(a, b):
    keys = ("exit", "passed", "failed", "errors", "skipped")
    if any(a[k] != b[k] for k in keys):
        return False
    return not (a["failing"] and b["failing"]) or a["failing"] == b["failing"]


# ---------------------------------------------------------------- sandbox profile (Phase 1.2)

def sandbox_home(at=None, reads=()):
    """A bridge-owned CODEX_HOME holding only the verify profile. Never edits ~/.codex/config.toml.
    `reads`: dependency dirs the run may READ inside otherwise-denied areas (a nested allow; probe 2026-10-08)."""
    ch = Path(at) if at else HOME / "codexhome"
    ch.mkdir(parents=True, exist_ok=True)
    deny = ["~/.ssh", "~/.codex", "~/.aws", "~/.config", "~/.claude", "~/Library/Keychains",
            "~/Documents", "~/Desktop", "~/Downloads", str(HOME / "jobs"), str(HOME / "runs"),
            *(str(r) for r in WORKSPACE_ROOTS)]
    lines = ['[permissions.bridge-verify]',
             'description = "codex-bridge verify re-run: write worktree+tmp only, no secrets, no network"',
             'extends = ":read-only"', "", "[permissions.bridge-verify.filesystem]", '":tmpdir" = "write"']
    lines += [f'{json.dumps(d)} = "deny"' for d in dict.fromkeys(deny)]
    lines += [f'{json.dumps(str(d))} = "read"' for d in dict.fromkeys(reads)]
    lines += ["", '[permissions.bridge-verify.filesystem.":workspace_roots"]', '"." = "write"']
    lines += [f'"{g}" = "deny"' for g in ("**/*.env", "**/.env*", "**/credentials.json", "**/token.json",
                                          "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*")]
    lines += ["", "[permissions.bridge-verify.network]", "enabled = false", ""]
    atomic_write(ch / "config.toml", "\n".join(lines))
    return ch


VERIFY_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
                   # Windows needs these for Python, sockets and cmd to start at all; none carries a secret
                   "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "USERPROFILE", "TEMP", "TMP")


def sandboxed_verify(wt, cmd, jd, deps=(), argv_tail=None):
    """Worker-modified code runs here: no network, no secrets on disk, AND no secrets in the environment
    (an allowlist, never a copy of os.environ). Every descendant is dead before accounting starts.
    deps: venvs exposed read-only via PYTHONPATH. argv_tail: run this argv instead of a shell (selftest)."""
    env = {k: os.environ[k] for k in VERIFY_ENV_KEEP if k in os.environ}
    reads = [d["path"] for d in deps]
    # A per-job profile lives outside HOME/jobs (which the profile itself denies).
    env.update(CODEX_HOME=str(sandbox_home(HOME / "codexhome-jobs" / Path(jd).name if reads else None, reads)),
               PYTHONDONTWRITEBYTECODE="1")
    if deps:
        env["PYTHONPATH"] = os.pathsep.join(d["site_packages"] for d in deps)
    shell = ["cmd", "/d", "/c", cmd] if IS_WIN else ["/bin/sh", "-c", cmd]
    argv = [CODEX, "sandbox", "-P", "bridge-verify", "-C", str(wt), "--", *(argv_tail or shell)]
    # The logs are written OUTSIDE jobs/ (which the profile denies), then moved in: with the child's stdout
    # in a denied path, pytest's fd capture can't write it back and Python exits 120 with empty logs.
    io = HOME / "verify-io" / Path(jd).name
    shutil.rmtree(io, ignore_errors=True)
    io.mkdir(parents=True)
    rc, dead = run_contained(argv, "", io / "verify-out.log", io / "verify-err.log", VERIFY_CEILING_S,
                             cwd_sweep=wt, env=env)
    for name in ("verify-out.log", "verify-err.log"):
        os.replace(io / name, Path(jd) / name)
    io.rmdir()
    text = ((jd / "verify-out.log").read_text(encoding="utf-8", errors="replace")
            + (jd / "verify-err.log").read_text(encoding="utf-8", errors="replace"))
    if rc is None:
        return 124, f"verify timed out after {VERIFY_CEILING_S}s\n{text}", dead
    return rc, text, dead


# ---------------------------------------------------------------- receipt (2c)

def stage(wt):
    """Stage everything, then unstage ONLY newly added, positively generated files. Returns the staged
    diff, its stat, name-status rows [(status, paths)], the artifact list and any scan errors."""
    errors, outs = [], {}

    def step(name, *args):
        r = git(wt, *args)
        outs[name] = r.stdout
        if r.returncode != 0 or re.search(r"Permission denied|could not open directory", r.stderr or ""):
            errors.append(f"git {' '.join(args)}: rc={r.returncode} {(r.stderr or '')[-200:]}")

    step("add", "add", "-A")
    step("names", "diff", "--cached", "--name-status", "HEAD")
    rows = [(ln.split("\t")[0], ln.split("\t")[1:]) for ln in outs["names"].splitlines() if "\t" in ln]
    artifacts = [p[-1] for st, p in rows if st == "A" and GENERATED.search(p[-1]) and not denied(p[-1])]
    if artifacts:
        step("unstage", "reset", "-q", "--", *artifacts)
        rows = [(st, p) for st, p in rows if not (st == "A" and p[-1] in artifacts)]
    step("stat", "diff", "--cached", "--stat", "HEAD")
    step("diff", "diff", "--cached", "--binary", "HEAD")
    step("porcelain", "status", "--porcelain", "--untracked-files=all", "--ignored")
    return outs, rows, artifacts, errors


def content_snapshot(wt, manifest, diff=None):
    """Binds the deliverable: staged diff WITH binary contents, manifest hashes, and every ignored file
    outside the usual build dirs (an ignored .env added later changes it). Mutates only the index of the
    bridge-owned worktree. A scan error yields a unique value, so it can never match (= stale)."""
    outs, errors = (None, [])
    if diff is None:
        outs, _, _, errors = stage(wt)
        diff = outs["diff"]
    if errors:
        return "scan-error-" + uuid.uuid4().hex
    porcelain = outs["porcelain"] if outs else git(wt, "status", "--porcelain", "--untracked-files=all", "--ignored").stdout
    ignored = sorted(ln[3:] for ln in porcelain.splitlines()
                     if ln.startswith("!! ") and not EXPECTED_IGNORED.search(ln[3:]))
    ign = {p: hashlib.sha256((Path(wt) / p).read_bytes()).hexdigest() if (Path(wt) / p).is_file() else "dir"
           for p in ignored}
    blob = diff + json.dumps(hash_manifest(wt, manifest), sort_keys=True) + json.dumps(ign, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def compute_receipt(jid, job, codex_exit, started, quota_hit=False):
    jd, wt, inp = job_dir(jid), Path(job["worktree"]), job["inputs"]
    ofile = jd / "final.txt"
    final = ofile.read_text(encoding="utf-8", errors="replace") if ofile.is_file() else ""
    codex_ok = codex_exit == 0 and ofile.is_file() and ofile.stat().st_mtime >= started - 1
    thread, usage, cmds = parse_events(jd / "events.jsonl")

    # 1. verify re-run first, sandboxed; accounting comes after so verify-created changes count
    v_exit, v_out, v_dead = sandboxed_verify(wt, inp["verify"], jd, job.get("deps") or []) if codex_ok else (None, "", True)
    (jd / "verify-rerun.txt").write_text(v_out, encoding="utf-8")
    rerun = normalize(v_out, v_exit) if codex_ok else None

    claim_txt = block(final, "VERIFY-CLAIM") or ""
    m = re.search(r"^exit:\s*(-?\d+)", claim_txt, re.M)
    claimed = normalize(claim_txt, int(m.group(1))) if m else None

    # 2. accounting (after verify, whose descendants are all dead by now)
    steps, rows, artifacts, scan_errors = stage(wt)
    if not v_dead:
        scan_errors.append("verify left a process alive in the worktree; accounting would race it")
    (jd / "diff.patch").write_text(steps["diff"], encoding="utf-8")
    changed, deleted = [], []
    for status, paths in rows:
        changed.extend(paths)  # R/C entries carry old AND new path: both must be in scope
        if status.startswith("D") or status.startswith("R"):
            deleted.append(paths[0])
    ignored = [ln[3:] for ln in steps["porcelain"].splitlines() if ln.startswith("!! ")]
    unexpected_ignored = [p for p in ignored if not EXPECTED_IGNORED.search(p)]
    out_of_scope = [p for p in changed if not in_scope(p, inp["scope"]) or denied(p)]
    new_hashes = hash_manifest(wt, inp["manifest"])
    verifier_changes = [p for p in inp["manifest"] if new_hashes.get(p) != job["manifest_hashes"].get(p)]
    # Any other changed/added file that shapes verification counts too, with a mandatory disposition.
    verifier_changes += [p for p in changed if VERIFIER.search(p) and p not in verifier_changes]
    snapshot = content_snapshot(wt, inp["manifest"], steps["diff"])

    # 3. outcome: strict conjunction (2c.5)
    if not codex_ok:
        outcome = "quota-limited" if quota_hit else "crashed"
    elif scan_errors:
        outcome = "incomplete-scan"
    elif out_of_scope:
        outcome = "out-of-scope"
    elif verifier_changes and not inp.get("verifier_changes_allowed"):
        outcome = "verifier-modified"
    elif rerun is None or claimed is None or not same(rerun, claimed):
        outcome = "mismatch"
    elif (v_exit != 0 or rerun["failed"] or rerun["errors"] or rerun["failing"] or unexpected_ignored
          or rerun["passed"] + rerun["failed"] + rerun["errors"] == 0):  # zero tests executed is a failure
        outcome = "verify-failed"
    else:
        outcome = "clean"

    receipt = {
        "job_id": jid, "outcome": outcome, "snapshot": snapshot,
        "computed": {
            "codex_exit": codex_exit, "fresh_final_output": codex_ok, "codex_tokens": usage,
            "codex_commands": len(cmds), "thread_id": thread,
            "changed": changed, "deleted": deleted, "artifacts": artifacts, "diff_stat": steps["stat"].strip(),
            "diff_lines": steps["diff"].count("\n"), "out_of_scope": out_of_scope,
            "verifier_changes": verifier_changes, "unexpected_ignored": unexpected_ignored,
            "verifier_changes_allowed": bool(inp.get("verifier_changes_allowed")),
            "scan_errors": scan_errors, "verify_rerun": rerun, "verify_rerun_exit": v_exit,
            "verify_sandbox": "codex sandbox -P bridge-verify (no network, no secrets, writes: worktree+tmp)",
            "model": job.get("model", MODEL), "effort": job.get("effort", EFFORT), "model_reason": job.get("model_reason"),
            "deps": [d["rel"] for d in job.get("deps") or []],
            "quota": read_json(jd / "quota.json"), "suggested_patch": suggested_summary(jd),
            "resume": f"codex resume {thread}" if thread else None,
        },
        "claimed": {
            "verify": claimed, "verify_raw": claim_txt[:2000], "open": (block(final, "OPEN") or "")[:4000],
            "intake_attestation": inp["attestation"],
        },
    }
    write_json(jd / "receipt.json", receipt)
    return receipt


def suggested_summary(jd):
    sp = jd / "suggested.patch"
    if not sp.is_file():
        return None
    text = sp.read_text(encoding="utf-8", errors="replace")
    return {"path": str(sp), "files": sorted(set(re.findall(r"^\+\+\+ b/(\S+)", text, re.M))),
            "lines": text.count("\n"), "applied": False}


# ---------------------------------------------------------------- status / result / discard / sweep

def jobs():
    d = HOME / "jobs"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def cmd_sweep():
    """crashed = supervisor identity dead AND no terminal state. Reported once (state changes)."""
    swept = []
    for jid in jobs():
        job, st = read_json(job_dir(jid) / "job.json", {}), read_json(job_dir(jid) / "state.json", {})
        if job.get("machine") != machine() or st.get("state") not in ACTIVE:
            continue
        if st.get("supervisor") and not alive(st["supervisor"]):
            transition(jid, "crashed", reason="supervisor died without a terminal state", quarantine=True)
            swept.append(jid)
        elif not st.get("supervisor") and time.time() - (job_dir(jid) / "state.json").stat().st_mtime > 180:
            transition(jid, "crashed", reason="supervisor never started", quarantine=True)
            swept.append(jid)
    return {"ok": True, "crashed": swept}


def cmd_status(jid=None):
    rows = []
    for j in ([jid] if jid else jobs()):
        job, st = read_json(job_dir(j) / "job.json", {}), read_json(job_dir(j) / "state.json", {})
        wt = Path(job.get("worktree", ""))
        rows.append({"id": j, "machine": job.get("machine"), "local": job.get("machine") == machine(),
                     "state": st.get("state"), "outcome": st.get("outcome"), "created": job.get("created"),
                     "task": (job.get("inputs") or {}).get("task", "")[:80],
                     "worktree_retained": wt.is_dir(), "quarantine": st.get("quarantine", False),
                     "reported": (job_dir(j) / "claim-notify").exists(),
                     "review_started": st.get("review_started"),
                     "owner": job.get("owner"), "updated": st.get("updated"),
                     "tier": (job.get("inputs") or {}).get("tier")})
    return {"ok": True, "slots": lock_holder(), "max_workers": MAX_WORKERS, "jobs": rows}


def cmd_result(jid):
    job = read_json(job_dir(jid) / "job.json") or {}
    return {"ok": True, "state": read_json(job_dir(jid) / "state.json"),
            "quota": read_json(job_dir(jid) / "quota.json"),
            "model": {k: job.get(k) for k in ("model", "effort", "model_reason")},
            "receipt": read_json(job_dir(jid) / "receipt.json"),
            "verdict": read_json(job_dir(jid) / "verdict.json")}


def cmd_discard(jid):
    job, st = read_json(job_dir(jid) / "job.json"), state_of(jid)
    if not job:
        fail("no such job")
    if job["machine"] != machine():
        fail("job belongs to another machine; its record is informational here")
    if st in ACTIVE:
        fail(f"refused: job is {st}; wait for it to finish")
    r = git(job["repo"], "worktree", "remove", "--force", job["worktree"])
    git(job["repo"], "worktree", "prune")
    transition(jid, "discarded", worktree_removal=("ok" if r.returncode == 0 else r.stderr[-300:]))
    return {"ok": r.returncode == 0, "stderr": r.stderr[-300:]}


def cmd_packet(jid):
    """Immutable review packet (2d): copied into the job dir, made read-only."""
    jd, job = job_dir(jid), read_json(job_dir(jid) / "job.json")
    receipt = read_json(jd / "receipt.json")
    if not receipt:
        fail("no receipt yet")
    current = content_snapshot(Path(job["worktree"]), job["inputs"]["manifest"])
    if current != receipt["snapshot"]:
        fail("refused: the worktree changed after its receipt; the receipt is stale (re-run or discard)",
             receipt_snapshot=receipt["snapshot"], current=current)
    pk = jd / "packet"
    if pk.exists():
        return {"ok": True, "packet": str(pk), "worktree": job["worktree"], "snapshot": receipt["snapshot"]}
    pk.mkdir()
    inp = job["inputs"]
    (pk / "task.md").write_text(
        f"# Task\n{inp['task']}\n\n# Definition of done\n{inp['done']}\n\n# Tier\n{inp['tier']}\n\n"
        f"# Scope\n" + "\n".join(inp["scope"]) + "\n\n# Verifier manifest\n" + "\n".join(inp["manifest"])
        + ("\n\n# Verifier changes were ALLOWED for this task: give each changed verifier an explicit disposition"
           if inp.get("verifier_changes_allowed") else "")
        + f"\n\n# Driver intake attestation (CLAIMED, not computed)\n{inp['attestation']}\n"
        + "\n# Conventions + companion/contract files (read these in the worktree; they did not change)\n"
        + "\n".join(c for c in ["CLAUDE.md", "AGENTS.md"] + list(inp.get("companions") or [])
                     if plain_file_in(Path(job["worktree"]), c)) + "\n"
        + "".join(f"MISSING caller-required companion (verdict must be fix-list): {c}\n"
                  for c in inp.get("companions") or [] if not plain_file_in(Path(job["worktree"]), c))
        + f"\n# Baseline test counts before the change (CLAIMED by the driver)\n{inp.get('baseline') or '(not given)'}\n",
        encoding="utf-8")
    for name in ("receipt.json", "diff.patch", "verify-rerun.txt", "final.txt"):
        src = jd / name
        if src.is_file():
            (pk / name).write_bytes(src.read_bytes())
    for f in pk.iterdir():
        f.chmod(0o444)
    pk.chmod(0o555)
    return {"ok": True, "packet": str(pk), "worktree": job["worktree"], "snapshot": receipt["snapshot"]}


class review_lock:
    """Per-job mutex for the review lifecycle (start / reset / retry / verdict / mark), so a reset from one
    session and a retry from another can never interleave. An OS lock on an open file (flock / msvcrt): the
    kernel releases it when the holder exits or dies, so there is no age-based takeover and the file is never
    deleted (deleting by path is what let two holders overlap)."""

    class Busy(Exception):
        """Raised instead of fail() when the caller asked for it: contention is retryable, not a refusal."""

    def __init__(self, jid, wait=15.0, raise_busy=False):
        self.p, self.wait, self.raise_busy = job_dir(jid) / "review.lock", wait, raise_busy

    def _try(self):
        if IS_WIN:
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self):
        self.f = open(self.p, "a+b")
        deadline = time.time() + self.wait
        try:
            while True:
                try:
                    self._try()
                    return self
                except OSError:
                    if time.time() > deadline:
                        if self.raise_busy:
                            raise review_lock.Busy()
                        fail("review lock busy; retry later")
                    time.sleep(0.05)
        except BaseException:  # timeout (SystemExit) or anything unexpected: never leak the fd
            self.f.close()
            raise

    def __exit__(self, *exc):
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()  # closing also drops a POSIX flock


def same_review(st, gen):
    """`gen` is the review_started stamp the caller saw (a generation token); None = no check (legacy caller)."""
    if gen is None:
        return True
    try:
        return float(st["review_started"]) == float(gen)
    except (KeyError, TypeError, ValueError):
        return False


def cmd_review_start(jid):
    """Claim (once) + packet + reviewing. The caller spawns the reviewer only when started is true."""
    try:
        return _review_start(jid)
    except review_lock.Busy:  # another session is mid-start: retry next tick, never report or retire
        return {"ok": True, "started": False, "reason": "busy"}


def _review_start(jid):
    with review_lock(jid, raise_busy=True):
        st = read_json(job_dir(jid) / "state.json", {})
        if st.get("state") in ("reviewing", "reviewed", "notified"):
            # Another session's watcher won the race: benign, so the loser never reports or retires it.
            return {"ok": True, "started": False, "reason": "already started"}
        if st.get("outcome") != "clean" or st.get("state") != "receipted":  # a cancelled job keeps its outcome
            fail("auto-review is only for clean, receipted jobs; use diagnose for the rest")
        pk = cmd_packet(jid)  # refuses (exits) on a stale receipt BEFORE any claim is taken
        if not claim(jid, "review"):
            return {"ok": True, "started": False, "reason": "already claimed"}
        started = time.time()
        transition(jid, "reviewing", review_started=started)
    receipt = read_json(job_dir(jid) / "receipt.json", {})
    return {**pk, "started": True, "review_gen": started,
            "verifier_changes": (receipt.get("computed") or {}).get("verifier_changes") or []}


def cmd_review_reset(jid, gen=None):
    """Recover a review whose reviewer died with its session: free the claim, back to receipted. With `gen`,
    only the review the caller judged dead is reset, never a replacement started meanwhile."""
    with review_lock(jid):
        st = read_json(job_dir(jid) / "state.json", {})
        if st.get("state") != "reviewing" or (job_dir(jid) / "verdict.json").exists() or not same_review(st, gen):
            return {"ok": True, "reset": False}
        (job_dir(jid) / "claim-review").unlink(missing_ok=True)
        transition(jid, "receipted", review_reset=now(), review_started=None)  # revokes the old reviewer
    return {"ok": True, "reset": True}


def cmd_review_retry(jid, gen=None):
    """A reviewer answered without a parseable VERDICT-JSON: free the claim so the watcher re-runs the review,
    ONCE per job. The second unparseable answer is recorded (as invalid) instead, so this can never loop.
    `superseded` = the caller's review is no longer the current one: the caller records nothing."""
    with review_lock(jid):
        st = read_json(job_dir(jid) / "state.json", {})
        if not same_review(st, gen):
            return {"ok": True, "retried": False, "superseded": True}
        if st.get("state") != "reviewing" or (job_dir(jid) / "verdict.json").exists() or st.get("review_retries", 0) >= 1:
            return {"ok": True, "retried": False}
        (job_dir(jid) / "claim-review").unlink(missing_ok=True)
        transition(jid, "receipted", review_retries=st.get("review_retries", 0) + 1, review_retry_at=now(),
                   review_started=None)  # revokes the old reviewer
    return {"ok": True, "retried": True}


REPORT_LEASE_S = 600


def cmd_report_begin(jid):
    """Exclusive right to report a job to a main session. `claim-notify` (written after the report lands) means
    done; a `claim-reporting` lease younger than REPORT_LEASE_S means another session is mid-report (its
    prompt.submit can block for a whole turn). An older lease is a crashed reporter: take it over, so a crash
    repeats a report instead of losing it."""
    with review_lock(jid):
        jd = job_dir(jid)
        if (jd / "claim-notify").exists():
            return {"ok": True, "won": False, "reason": "already reported"}
        lease = jd / "claim-reporting"
        try:
            if time.time() - lease.stat().st_mtime < REPORT_LEASE_S:
                return {"ok": True, "won": False, "reason": "another session is reporting"}
        except FileNotFoundError:
            pass
        lease.write_text(f"{identity()} {now()}\n", encoding="utf-8")
    return {"ok": True, "won": True}


def cmd_review_failed(jid, reason):
    transition(jid, "receipted", review_failed=reason[:300])
    return {"ok": True}


def cmd_diagnose(jid):
    """On-demand read-only review of a non-clean job (2d). Never changes acceptance."""
    st = read_json(job_dir(jid) / "state.json", {})
    if st.get("state") not in ("receipted", "reviewed", "notified", "timeout", "crashed"):
        fail(f"nothing to diagnose: job is {st.get('state')}")
    return {**cmd_packet(jid), "outcome": st.get("outcome")}


def cmd_mark(jid, state, gen=None):
    if state not in ("notified",):
        fail("only 'notified' can be set from outside")
    with review_lock(jid):
        if not same_review(read_json(job_dir(jid) / "state.json", {}), gen):
            return {"ok": True, "marked": False, "superseded": True}  # never retire a replacement review
        transition(jid, state)
    return {"ok": True, "marked": True}


def validate_verdict(req, receipt):
    """An `accept` must be earned: every requirement met, every verifier change disposed ok."""
    problems = []
    if req.get("verdict") not in ("accept", "fix-list", "reject"):
        problems.append(f"unknown verdict {req.get('verdict')!r}")
    if req.get("verdict") == "accept":
        reqs = req.get("requirements") or []
        if not isinstance(reqs, list) or not reqs or any(not isinstance(r, dict) or r.get("met") is not True for r in reqs):
            problems.append("accept with missing, malformed or unmet requirements (met must be literal true)")
        changed = (receipt.get("computed") or {}).get("verifier_changes") or []
        vc = req.get("verifier_changes") or []
        disposed = {d.get("file"): d.get("ok") for d in (vc if isinstance(vc, list) else []) if isinstance(d, dict)}
        if any(disposed.get(f) is not True for f in changed):
            problems.append("accept without an ok disposition for every verifier change")
    return problems


def cmd_verdict(jid, req):
    with review_lock(jid):
        return _record_verdict(jid, req)


def _record_verdict(jid, req):
    gen = req.pop("review_gen", None)
    st = read_json(job_dir(jid) / "state.json", {})
    if not same_review(st, gen) or (gen is not None and st.get("state") != "reviewing"):
        return {"ok": False, "error": "superseded: a newer review of this job replaced this reviewer; nothing recorded"}
    receipt = read_json(job_dir(jid) / "receipt.json") or {}
    problems = validate_verdict(req, receipt)
    job = read_json(job_dir(jid) / "job.json") or {}
    if req.get("verdict") == "accept" and job:
        # The packet's MISSING line is enforced here, not left to the reviewer.
        missing = [c for c in job["inputs"].get("companions") or [] if not plain_file_in(Path(job["worktree"]), c)]
        if missing:
            problems.append(f"accept while caller-required companions are missing from the worktree: {missing}")
    if problems:
        req = {**req, "verdict": "fix-list" if req.get("verdict") == "accept" else "invalid",
               "downgraded": problems}
    current = content_snapshot(Path(job["worktree"]), job["inputs"]["manifest"]) if job else None
    # Two different causes, one fail-closed flag: only worktree_changed means the code moved after review.
    worktree_changed = current != receipt.get("snapshot")
    stale = req.get("snapshot") != receipt.get("snapshot") or worktree_changed
    write_json(job_dir(jid) / "verdict.json", {**req, "stale": stale, "worktree_changed": worktree_changed, "recorded": now()})
    transition(jid, "reviewed", verdict=req.get("verdict"), stale=stale)
    return {"ok": True, "stale": stale, "worktree_changed": worktree_changed, "verdict": req.get("verdict"),
            "downgraded": req.get("downgraded") or []}


# ---------------------------------------------------------------- selftest (0.1.1): unlocks workers per machine

def selftest_ok(version):
    pr = read_json(HOME / "selftest.json") or {}
    return (pr.get("passed") is True and pr.get("codex_version") == version
            and pr.get("platform") == platform.system() and pr.get("machine") == machine())


def cmd_selftest():
    """Prove the verify sandbox on THIS machine: what must fail fails, what must work works. The proof is
    bound to the codex-cli version + platform + machine, so an upgrade re-requires it."""
    version, err = codex_version()
    if not version:
        fail(f"codex unreachable: {err}")
    base = HOME / "selftest"
    shutil.rmtree(base, ignore_errors=True)
    wt = base / "wt"
    wt.mkdir(parents=True)
    (wt / ".env").write_text("SELFTEST_ENV=1\n", encoding="utf-8")
    outside = HOME / f"selftest-outside-{uuid.uuid4().hex[:8]}.txt"
    os.environ["CODEX_BRIDGE_SELFTEST_CANARY"] = uuid.uuid4().hex  # must NOT reach the sandboxed child
    ws_file = next((r / "README.md" for r in WORKSPACE_ROOTS if (r / "README.md").is_file()), None)
    probes = [  # (name, python code, must succeed)
        ("runs", "print('ok')", True),
        ("write-inside", "open('inside.txt', 'w').write('x'); print(open('inside.txt').read())", True),
        ("write-outside", f"open({str(outside)!r}, 'w').write('x')", False),
        ("read-dotenv", "print(open('.env').read())", False),
        ("network", "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)", False),
        ("env-canary-absent", "import os, sys; sys.exit(1 if os.environ.get('CODEX_BRIDGE_SELFTEST_CANARY') else 0)", True),
    ]
    if ws_file:
        probes.append(("read-workspace", f"print(open({str(ws_file)!r}).read()[:10])", False))
    if (Path.home() / ".ssh").is_dir():
        probes.append(("read-ssh", f"import os; print(os.listdir({str(Path.home() / '.ssh')!r}))", False))
    results = {}
    for name, code, must_work in probes:
        script = base / f"probe-{name}.py"
        script.write_text(code, encoding="utf-8")
        rc, text, _ = sandboxed_verify(wt, "", base, argv_tail=[sys.executable, str(script)])
        # A must-fail probe passes only on a real permission denial: offline, a crash or a missing
        # interpreter also exit non-zero and would otherwise read as "blocked".
        denied_here = re.search(r"PermissionError|Operation not permitted|Access is denied", text) is not None
        ok = rc == 0 if must_work else (rc != 0 and denied_here)
        if name == "write-outside":
            ok = ok and not outside.exists()
        results[name] = {"pass": ok, "rc": rc, "must_succeed": must_work, "tail": text[-300:]}
    outside.unlink(missing_ok=True)
    os.environ.pop("CODEX_BRIDGE_SELFTEST_CANARY", None)
    proof = {"passed": all(r["pass"] for r in results.values()), "codex_version": version,
             "platform": platform.system(), "machine": machine(), "at": now(), "results": results}
    write_json(HOME / "selftest.json", proof)
    return {"ok": True, **proof}


def cmd_cancel(jid):
    job, st = read_json(job_dir(jid) / "job.json"), state_of(jid)
    if not job:
        fail("no such job")
    if job["machine"] != machine():
        fail("job belongs to another machine")
    if st not in ("pending", "running", "exited"):
        fail(f"nothing to cancel: job is {st}")
    flag = job_dir(jid) / "cancel"
    flag.write_text(now(), encoding="utf-8")  # flag FIRST, then observe: the supervisor re-checks after `exited`
    deadline = time.time() + 20
    while time.time() < deadline:
        st = state_of(jid)
        if st == "cancelled":
            return {"ok": True, "cancelled": True, "note": "Codex stopped; the worktree is kept, quarantined"}
        if st not in ("pending", "running", "exited"):
            flag.unlink(missing_ok=True)
            fail(f"too late: the job finished first (state {st}); nothing was cancelled")
        if st == "pending":
            return {"ok": True, "cancelled": False, "pending": True,
                    "note": "the job is waiting for the Codex slot; it is cancelled the moment it gets it"}
        time.sleep(0.25)
    if state_of(jid) == "exited":
        return {"ok": True, "cancelled": False, "note": "cancel requested; Codex already exited and verification is "
                "running (up to 20 min): the job is marked cancelled, not reviewed, when it ends"}
    return {"ok": True, "cancelled": False, "note": "cancel requested; the supervisor has not acted within 20 s (check codex_status)"}


# ---------------------------------------------------------------- telemetry (2e)

def cmd_burn(req):
    path = LOG_PATH
    if not CFG["log_path"]:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():  # a configured path must exist: a typo shouldn't scatter logs
        fail(f"log_path directory missing: {path.parent}")
    row = {"run_uuid": uuid.uuid4().hex, "date": dt.date.today().isoformat(), "machine": machine(),
           "wrapper_tokens": 0, **req}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")
    return {"ok": True, "run_uuid": row["run_uuid"]}


# ---------------------------------------------------------------- entry

def main(argv):
    if not argv:
        fail("usage: bridge.py ask|start|supervise|status|result|discard|cancel|sweep|claim|packet|verdict|burn|lock"
             "|usage|selftest")
    cmd, rest = argv[0], argv[1:]
    stdin_json = lambda: json.loads(sys.stdin.read() or "{}")  # noqa: E731
    if cmd == "ask":
        out(cmd_ask(stdin_json()))
    elif cmd == "start":
        out(cmd_start(stdin_json()))
    elif cmd == "supervise":
        cmd_supervise(rest[0])
    elif cmd == "status":
        out(cmd_status(rest[0] if rest else None))
    elif cmd == "result":
        out(cmd_result(rest[0]))
    elif cmd == "discard":
        out(cmd_discard(rest[0]))
    elif cmd == "sweep":
        out(cmd_sweep())
    elif cmd == "claim":
        out({"ok": True, "claimed": claim(rest[0], rest[1])})
    elif cmd == "packet":
        out(cmd_packet(rest[0]))
    elif cmd == "review-start":
        out(cmd_review_start(rest[0]))
    elif cmd == "review-reset":
        out(cmd_review_reset(rest[0], rest[1] if len(rest) > 1 else None))
    elif cmd == "review-retry":
        out(cmd_review_retry(rest[0], rest[1] if len(rest) > 1 else None))
    elif cmd == "report-begin":
        out(cmd_report_begin(rest[0]))
    elif cmd == "review-failed":
        out(cmd_review_failed(rest[0], " ".join(rest[1:]) or "spawn failed"))
    elif cmd == "diagnose":
        out(cmd_diagnose(rest[0]))
    elif cmd == "mark":
        out(cmd_mark(rest[0], rest[1], rest[2] if len(rest) > 2 else None))
    elif cmd == "verdict":
        out(cmd_verdict(rest[0], stdin_json()))
    elif cmd == "config":
        out(cmd_config())
    elif cmd == "burn":
        out(cmd_burn(stdin_json()))
    elif cmd == "lock":
        out({"ok": True, "slots": lock_holder()})
    elif cmd == "usage":
        out(cmd_usage())
    elif cmd == "selftest":
        out(cmd_selftest())
    elif cmd == "cancel":
        out(cmd_cancel(rest[0]))
    else:
        fail(f"unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
