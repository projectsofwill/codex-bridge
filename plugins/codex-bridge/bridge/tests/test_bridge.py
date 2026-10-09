"""Offline tests for bridge.py. Run: python3 -m unittest discover -s bridge/tests (from the mod root)."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def pem(kind, edge="BEGIN"):
    """A dummy PEM armor line, assembled at runtime so secret scanners don't flag the test source."""
    return "-" * 5 + f"{edge} {kind} PRIVATE" + " KEY" + "-" * 5

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_codex.py"
os.chmod(FAKE, 0o755)

HEALTHY = json.dumps({"ordinaryUsageAllowed": True, "rateLimits": {
    "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600},
    "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 86400},
    "rateLimitReachedType": None, "planType": "plus"}})
PASS_PYTEST = "==== 3 passed in 0.10s ===="
CLAIM_OK = "VERIFY-CLAIM\nexit: 0\n" + PASS_PYTEST + "\nEND-VERIFY-CLAIM\nOPEN\nnone\nEND-OPEN\n"


def load(home):
    os.environ["CODEX_BRIDGE_HOME"] = str(home)
    os.environ["CODEX_BRIDGE_CODEX"] = str(FAKE)
    sys.path.insert(0, str(HERE.parent))
    import bridge
    return importlib.reload(bridge)


class Base(unittest.TestCase):
    def start(self, verify="python3 -m pytest -q tests 2>&1 || true", **env):
        os.environ.update({f"FAKE_CODEX_{k}": v for k, v in env.items()})
        r = self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py", "tests"],
                              "verify": verify, "tier": "R0", "done": "d",
                              "manifest": ["tests/test_app.py"], "attestation": "not a gate category"})
        return r["job_id"]

    def inproc(self, **env):
        os.environ["CODEX_BRIDGE_NO_SPAWN"] = "1"
        try:
            jid = self.start(**env)
        finally:
            del os.environ["CODEX_BRIDGE_NO_SPAWN"]
        return jid

    def receipt(self, jid):
        return self.b.read_json(self.b.job_dir(jid) / "receipt.json")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.ws = root / "ws"
        self.repo = self.ws / "repo"
        self.repo.mkdir(parents=True)
        (self.ws / "ops" / "logs").mkdir(parents=True)
        # A multi-repo setup like the author's: extra protected paths, a workspace root, aliases, a log path.
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "protected_paths": ["ops/global/", "memory/", "context/", "data/raw/", "notes/",
                                "sync.py", "repos.json"],
            "workspace_roots": [str(self.ws)],
            "stakes_aliases": {"backbone": "unattended", "operative": "policy"},
            "log_path": str(self.ws / "ops" / "logs" / "burn.jsonl"),
            "worker_refuse_pct": {"primary": 95, "secondary": 95},
        }))
        os.environ.pop("CODEX_BRIDGE_CONFIG", None)
        os.environ["CODEX_BRIDGE_LIMITS_JSON"] = HEALTHY
        for k in [k for k in os.environ if k.startswith("FAKE_CODEX_")]:
            del os.environ[k]
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b  # the addition helper\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
        g("add", "-A")
        g("commit", "-qm", "init")
        self.b = load(self.home)
        self.prove()

    def prove(self, passed=True, version="codex-cli 0.0.0-fake"):
        self.home.mkdir(parents=True, exist_ok=True)
        self.b.write_json(self.home / "selftest.json", {"passed": passed, "codex_version": version,
                                                        "platform": self.b.platform.system(), "machine": self.b.machine()})

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, fn, *a):
        try:
            return fn(*a)
        except SystemExit:
            raise AssertionError("unexpected exit")

    def refused(self, fn, *a):
        with self.assertRaises(SystemExit) as cm:
            fn(*a)
        self.assertEqual(cm.exception.code, 1)


class Intake(Base):
    def test_gate_without_target_refused(self):
        self.refused(self.b.cmd_ask, {"mode": "gate", "trigger": "backbone", "prompt": "x", "cwd": str(self.repo)})

    def test_worker_missing_inputs_refused(self):
        self.refused(self.b.cmd_start, {"task": "x", "repo": str(self.repo)})

    def test_denylisted_scope_refused(self):
        for bad in ("memory/x.md", ".claude/settings.json", "sync.py", "a/.env", "../escape.py"):
            req = {"task": "t", "repo": str(self.repo), "scope": [bad], "verify": "true", "tier": "R0",
                   "done": "d", "manifest": ["tests/test_app.py"], "attestation": "ok"}
            self.refused(self.b.cmd_start, req)


class ReviewFindings(Base):
    def test_denylist_dotfiles_and_depth(self):  # finding 1
        for bad in (".claude/agents/codex-gate.md", ".env", ".env.keys", "./.env", "Projects/x/.claude/a.md",
                    "a/b/.git/config", "memory/x.md", "ops/global/skills/x.md", "."):
            self.assertTrue(self.b.denied(bad), bad)
        for ok in ("src/hooks/useThing.ts", "app.py", "lib/memory_cache.py", "tests/test_app.py"):
            self.assertFalse(self.b.denied(ok), ok)

    def test_secret_files_refused_for_ask(self):  # finding 7
        self.refused(self.b.cmd_ask, {"mode": "gate", "trigger": "backbone", "prompt": "x", "files": [".env"], "cwd": str(self.repo)})
        (self.repo / "cfg.py").write_text("API_KEY = 'sk-abc123456789'\nname = 'a perfectly normal line'\n")
        picks = self.b.pick_canaries(self.repo, ["cfg.py"])
        self.assertNotIn("API_KEY", picks["cfg.py"]["expect"])

    def test_canary_forgives_one_wrapper_only(self):  # smoke test: real Codex wraps quotes in backticks
        picks = {"calc.py": {"line": 12, "expect": 'raise ValueError("x")'}}
        ok = lambda q: self.b.check_canaries(f"READ-RECEIPT\ncalc.py | 12 | {q}\nEND-READ-RECEIPT", picks)["calc.py"]  # noqa: E731
        self.assertTrue(ok('raise ValueError("x")'))
        self.assertTrue(ok('`raise ValueError("x")`'))
        self.assertFalse(ok('`raise ValueError("y")`'))
        self.assertFalse(ok('raise ValueError'))

    def test_unittest_skipped_counts(self):  # finding 9
        n = self.b.normalize("Ran 3 tests in 0.001s\n\nOK (skipped=3)", 0)
        self.assertEqual((n["passed"], n["skipped"]), (0, 3))

    def test_reclaim_never_removes_a_live_slot(self):  # finding 3
        self.b.slots_dir().mkdir(parents=True, exist_ok=True)
        path = self.b.slot_path("t0")
        dead = {"holder": "999999:never", "owner": {}}
        self.b.write_json(path, dead)
        live = {"holder": self.b.identity(), "owner": {"k": "live"}}
        self.b.write_json(path, live)  # rewritten after we judged it dead
        self.b._reclaim(path, dead)
        self.assertEqual(self.b.read_json(path), live)

    def test_slot_file_is_never_half_written(self):  # finding 3a
        holder, tok = self.b.lock_acquire({"t": 1}, 0)
        self.assertIsNone(holder)
        self.assertIsNotNone(self.b.read_json(self.b.slot_path(tok)))  # atomic write: full JSON from the first instant
        self.b.lock_release(tok)

    def test_symlink_and_private_key_canaries_refused(self):  # gate finding 4
        secret = Path(self.tmp.name) / "id_key"
        secret.write_text(pem("OPENSSH") + "\nabc\n")
        (self.repo / "notes.txt").symlink_to(secret)
        self.refused(self.b.cmd_ask, {"mode": "gate", "trigger": "backbone", "prompt": "x", "files": ["notes.txt"], "cwd": str(self.repo)})
        (self.repo / "k.txt").write_text(pem("OPENSSH") + "\nthis is a normal sentence line\n")
        self.assertNotIn("BEGIN", self.b.pick_canaries(self.repo, ["k.txt"])["k.txt"]["expect"] or "")

    def test_missing_named_target_needs_the_diff(self):  # gate finding 7
        os.environ["FAKE_CODEX_REPLY"] = ("ok\nCONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\n"
                                          "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        r = self.b.cmd_ask({"mode": "gate", "trigger": "backbone", "prompt": "p", "files": ["typo.py"], "cwd": str(self.repo)})
        self.assertFalse(r["gate_satisfied"])
        r = self.b.cmd_ask({"mode": "gate", "trigger": "backbone", "prompt": "p", "files": ["gone.py"], "cwd": str(self.repo),
                            "diff": "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n-x = 1\n"})
        self.assertTrue(r["gate_satisfied"], r["read_evidence"])

    def test_failed_followup_keeps_first_answer(self):  # finding 6
        os.environ["FAKE_CODEX_REPLY"] = ("FINDINGS: real ones\nCONTEXT-OBJECTIONS\nNEED-FILE app.py\nEND-CONTEXT-OBJECTIONS\n"
                                          "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        real = self.b.codex_argv
        calls = {"n": 0}

        def argv(*a, **k):
            calls["n"] += 1
            v = real(*a, **k)
            return v if calls["n"] == 1 else [sys.executable, "-c", "import sys; sys.exit(3)"]
        self.b.codex_argv = argv
        try:
            r = self.b.cmd_ask({"mode": "gate", "trigger": "backbone", "prompt": "p", "files": ["app.py"], "cwd": str(self.repo)})
        finally:
            self.b.codex_argv = real
        self.assertIn("FINDINGS: real ones", r["final"])
        self.assertFalse(r["gate_satisfied"])


class Round2(Base):
    def test_pem_body_and_opaque_lines_never_canaries(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        (self.repo / "k.txt").write_text(f"{pem('RSA')}\n{body}\n{pem('RSA', 'END')}\n"
                                         "a perfectly ordinary sentence here\n")
        for _ in range(20):
            self.assertEqual(self.b.pick_canaries(self.repo, ["k.txt"])["k.txt"]["expect"], "a perfectly ordinary sentence here")

    def test_header_only_diff_is_not_deletion_evidence(self):
        self.assertFalse(self.b.in_diff("gone.py", "diff --git a/gone.py b/gone.py\n"))
        self.assertTrue(self.b.in_diff("gone.py", "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n-x = 1\n"))

    def test_directory_target_refused(self):
        self.refused(self.b.cmd_ask, {"mode": "gate", "trigger": "backbone", "prompt": "x", "files": ["tests"], "cwd": str(self.repo)})

    def test_target_changed_during_review_blocks_gate(self):
        os.environ["FAKE_CODEX_REPLY"] = ("ok\nCONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\n"
                                          "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        os.environ["FAKE_CODEX_EDIT"] = "tests/test_app.py=# changed mid-review\n"
        r = self.b.cmd_ask({"mode": "gate", "trigger": "backbone", "prompt": "p", "files": ["app.py", "tests/test_app.py"], "cwd": str(self.repo)})
        self.assertFalse(r["gate_satisfied"])
        self.assertTrue(any("changed while Codex was reviewing" in o for o in r["context_objections"]))

    def test_sweep_spares_a_process_with_a_living_parent(self):
        # a "person's shell" sitting in the worktree: parent alive, so it must survive cleanup
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        wt = self.b.worktree_dir(jid)
        bystander = subprocess.Popen(["sleep", "300"], cwd=wt)
        try:
            self.b.cmd_supervise(jid)
            self.assertIsNone(bystander.poll(), "cleanup killed a process that was not the job's")
        finally:
            bystander.kill()
            bystander.wait()

    def test_worker_auth_error_on_untouched_worktree_is_retried(self):
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          AUTH_FAIL_ONCE=str(Path(self.tmp.name) / "auth-marker"))
        self.b.cmd_supervise(jid)
        st = self.b.read_json(self.b.job_dir(jid) / "state.json")
        self.assertIn("retried once", st.get("auth_retry", ""))
        self.assertEqual(st["state"], "receipted")
        self.assertTrue((self.b.job_dir(jid) / "codex-stderr-auth.log").exists())

    def test_worker_auth_error_after_an_edit_is_not_retried(self):
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK, AUTH_FAIL_LATE="1",
                          AUTH_FAIL_ONCE=str(Path(self.tmp.name) / "auth-marker"), EDIT="app.py=x = 1\n")
        self.b.cmd_supervise(jid)
        st = self.b.read_json(self.b.job_dir(jid) / "state.json")
        self.assertNotIn("auth_retry", st)
        self.assertNotEqual(st.get("outcome"), "clean")

    def test_second_supervisor_is_a_noop(self):
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        self.b.cmd_supervise(jid)
        first = self.b.read_json(self.b.job_dir(jid) / "state.json")["history"]
        self.b.cmd_supervise(jid)
        self.assertEqual(self.b.read_json(self.b.job_dir(jid) / "state.json")["history"], first)

    def test_binary_and_ignored_changes_after_receipt_are_stale(self):
        (self.repo / ".gitignore").write_text("*.local\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "ignore"], check=True)
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK, EDIT="app.py=def add(a, b):\n    return b + a\n")
        wt = self.b.worktree_dir(jid)
        (wt / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.b.cmd_supervise(jid)
        job = self.b.read_json(self.b.job_dir(jid) / "job.json")
        snap = self.receipt(jid)["snapshot"]
        self.assertEqual(self.b.content_snapshot(wt, job["inputs"]["manifest"]), snap)
        (wt / "blob.bin").write_bytes(b"\x00\x09\x02")
        self.assertNotEqual(self.b.content_snapshot(wt, job["inputs"]["manifest"]), snap)
        (wt / "blob.bin").write_bytes(b"\x00\x01\x02")
        (wt / "secrets.local").write_text("x")
        self.assertNotEqual(self.b.content_snapshot(wt, job["inputs"]["manifest"]), snap)

    def test_accept_without_dispositions_is_downgraded(self):
        receipt = {"computed": {"verifier_changes": ["tests/test_app.py"]}}
        self.assertTrue(self.b.validate_verdict({"verdict": "accept", "requirements": [{"met": True}]}, receipt))
        self.assertFalse(self.b.validate_verdict({"verdict": "accept", "requirements": [{"met": True}],
                                                  "verifier_changes": [{"file": "tests/test_app.py", "ok": True}]}, receipt))
        self.assertTrue(self.b.validate_verdict({"verdict": "accept", "requirements": [{"met": False}]}, {"computed": {}}))


class Lock(Base):
    """0.3.0: one slot per dispatch. Asks/gates never wait; workers are capped by max_workers."""
    W = {"kind": "worker"}

    def worker(self, job, **kw):
        return self.b.lock_acquire({**self.W, "job": job}, 0, **kw)

    def test_asks_and_gates_run_side_by_side(self):
        a = self.b.lock_acquire({"kind": "ask"}, 0)
        g = self.b.lock_acquire({"kind": "gate"}, 0)
        w = self.worker("j1")
        self.assertEqual([a[0], g[0], w[0]], [None, None, None])
        self.assertEqual(len(self.b.lock_holder()), 3)
        for _, tok in (a, g, w):
            self.b.lock_release(tok)
        self.assertEqual(self.b.lock_holder(), [])

    def test_workers_capped_by_max_workers_and_asks_never(self):
        self.b.MAX_WORKERS = 2
        toks = [self.worker(f"j{i}")[1] for i in range(2)]
        busy, tok = self.worker("j3")
        self.assertIsNone(tok)
        self.assertIn("max_workers is 2", busy["busy"])
        self.assertEqual(len(busy["running"]), 2)
        ask = self.b.lock_acquire({"kind": "ask"}, 0)  # the cap is for workers only
        self.assertIsNone(ask[0])
        self.b.lock_release(toks[0])
        self.assertIsNone(self.worker("j3")[0])  # a freed place is taken at once

    def test_racing_processes_never_exceed_the_cap(self):
        (self.home / "config.json").write_text(json.dumps({"max_workers": 3}))
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import bridge; "
                "busy, tok = bridge.lock_acquire({'kind': 'worker', 'job': sys.argv[2]}, 0); "
                "print('got' if tok else 'busy', flush=True); time.sleep(3 if tok else 0)")
        env = {**os.environ, "CODEX_BRIDGE_HOME": str(self.home)}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(HERE.parent), f"j{i}"], env=env,
                                  stdout=subprocess.PIPE, text=True) for i in range(8)]
        results = [pr.communicate(timeout=60)[0].strip() for pr in procs]
        self.assertEqual(results.count("got"), 3, results)

    def test_zero_means_no_cap(self):
        self.b.MAX_WORKERS = 0
        self.assertTrue(all(self.worker(f"j{i}")[0] is None for i in range(5)))

    def test_supervisor_inherits_its_reservation_even_at_the_cap(self):
        self.b.MAX_WORKERS = 1
        _, res = self.worker("j1", lease_s=60)  # `start` reserves the only place
        self.assertIsNotNone(self.worker("j2")[0])  # nobody else gets it
        busy, tok = self.b.lock_acquire({**self.W, "job": "j1"}, 0, takeover_job="j1")
        self.assertIsNone(busy)
        self.assertFalse(self.b.slot_path(res).exists())  # the reservation became the supervisor's slot
        self.assertEqual(len(self.b.running_workers()), 1)

    def test_dead_worker_slot_frees_its_place(self):
        self.b.MAX_WORKERS = 1
        self.b.slots_dir().mkdir(parents=True, exist_ok=True)
        self.b.write_json(self.b.slot_path("dead"), {"holder": "999999:never", "owner": {**self.W, "job": "x"}, "tree": []})
        self.assertTrue(self.b.lock_holder()[0]["stale"])
        busy, tok = self.worker("j1")
        self.assertIsNone(busy)
        self.assertFalse(self.b.slot_path("dead").exists())

    def test_orphan_tree_holds_its_place_until_killed(self):  # gate finding 1
        self.b.MAX_WORKERS = 1
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        try:
            self.b.slots_dir().mkdir(parents=True, exist_ok=True)
            orphan = {"holder": "999999:dead supervisor", "owner": {**self.W, "job": "old"}, "tree": [self.b.identity(child.pid)],
                      "machine": self.b.machine(), "token": "t0"}
            self.b.write_json(self.b.slot_path("t0"), orphan)
            self.assertTrue(self.b.lock_held(orphan))  # supervisor dead, Codex alive: still counts
            holder, tok = self.worker("next")  # reclaim kills the orphan, then takes the place
            self.assertIsNone(holder)
            child.wait(timeout=10)
            self.b.lock_release(tok)
        finally:
            if child.poll() is None:
                child.kill()

    def test_lease_holds_a_place_until_released(self):  # gate finding 10
        self.b.MAX_WORKERS = 1
        holder, tok = self.worker("j1", lease_s=60)
        self.assertIsNone(holder)
        self.assertIsNotNone(self.worker("j2")[0])
        self.b.lock_release(tok)
        self.assertEqual(self.b.lock_holder(), [])

    def test_overlap_is_detected_both_ways(self):
        _, a = self.b.lock_acquire({"kind": "ask"}, 0)
        self.assertFalse(self.b.overlapped(a))  # alone so far
        _, b = self.b.lock_acquire({"kind": "gate"}, 0)  # started during a
        self.assertTrue(self.b.overlapped(a))
        self.assertTrue(self.b.overlapped(b))  # a was running when b started
        self.b.lock_release(a)
        self.b.lock_release(b)
        _, c = self.b.lock_acquire({"kind": "ask"}, 0)
        self.assertFalse(self.b.overlapped(c))
        self.b.lock_release(c)

    def test_shared_quota_never_feeds_the_estimate(self):
        lim = lambda u: {"ok": True, "windows": {"primary": {"used": u}}}  # noqa: E731
        self.assertEqual(self.b.quota_record(lim(10), lim(14), False)["delta"], {"primary": 4})
        rec = self.b.quota_record(lim(10), lim(14), True)
        self.assertIsNone(rec["delta"])
        self.assertEqual((rec["shared_delta"], rec["overlapping"]), ({"primary": 4}, True))

    def test_preflight_counts_running_workers(self):
        lim = self.b.codex_limits()  # primary 10% used
        self.b.estimate = lambda lane, window: 10.0 if window == "primary" else 0.0
        self.assertIsNone(self.b.preflight("worker", lim, running=8)[0])  # 10 + 10 x 9 = 100: fits
        refusal = self.b.preflight("worker", lim, running=9)[0]
        self.assertIn("plus 9 running", refusal)

    def test_max_workers_config_validated(self):
        for bad in (-1, "3", True, 1.5):
            (self.home / "config.json").write_text(json.dumps({"max_workers": bad}))
            r = subprocess.run([sys.executable, str(HERE.parent / "bridge.py"), "config"], capture_output=True, text=True,
                               env={**os.environ, "CODEX_BRIDGE_HOME": str(self.home)})
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("max_workers", r.stdout)

    def test_pid_reuse_guard(self):
        self.assertTrue(self.b.alive(self.b.identity()))
        self.assertFalse(self.b.alive(f"{os.getpid()}:some other start time"))


class Gate(Base):
    def ask(self, reply, files=("app.py",)):
        os.environ["FAKE_CODEX_REPLY"] = reply
        return self.b.cmd_ask({"mode": "gate", "trigger": "backbone", "prompt": "review", "files": list(files),
                               "diff": "+x", "cwd": str(self.repo)})

    def test_truthful_receipt_satisfies(self):
        r = self.ask("finding\nCONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\n"
                     "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        self.assertTrue(r["gate_satisfied"], r)

    def test_missing_or_wrong_receipt_fails(self):
        r = self.ask("CONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\nREAD-RECEIPT\napp.py | 1 | guessed\nEND-READ-RECEIPT\n")
        self.assertFalse(r["gate_satisfied"])
        self.assertEqual(r["read_evidence"]["app.py"], "NO EVIDENCE")

    def test_unresolved_objection_blocks_after_resumes(self):
        r = self.ask("CONTEXT-OBJECTIONS\nNEED-FILE app.py\nEND-CONTEXT-OBJECTIONS\n"
                     "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        self.assertFalse(r["gate_satisfied"])
        self.assertEqual(len(r["attempts"]), 1 + self.b.MAX_RESUMES)

    def test_blocker_objection_does_not_resume(self):
        r = self.ask("CONTEXT-OBJECTIONS\nBLOCKER the edit is unwritten\nEND-CONTEXT-OBJECTIONS\n"
                     "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        self.assertFalse(r["gate_satisfied"])
        self.assertEqual(len(r["attempts"]), 1)

    def test_nonzero_exit_fails_closed(self):
        os.environ["FAKE_CODEX_EXIT"] = "1"
        r = self.ask("anything")
        self.assertFalse(r["gate_satisfied"])
        self.assertFalse(r["ok"])

    def test_running_worker_never_blocks_a_gate(self):  # 0.3.0: was "lock busy refuses"
        self.b.slots_dir().mkdir(parents=True, exist_ok=True)
        self.b.write_json(self.b.slot_path("w"), {"holder": self.b.identity(), "owner": {"kind": "worker"}, "tree": []})
        r = self.ask("CONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\nREAD-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        self.assertTrue(r["gate_satisfied"], r)
        self.assertTrue(r["quota"]["overlapping"])  # a worker ran alongside: the delta is shared
        self.assertIsNone(r["quota"]["delta"])

    def test_auth_error_is_retried_once(self):
        os.environ["FAKE_CODEX_AUTH_FAIL_ONCE"] = str(Path(self.tmp.name) / "auth-marker")
        r = self.ask("CONTEXT-OBJECTIONS\nnone\nEND-CONTEXT-OBJECTIONS\nREAD-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        self.assertTrue(r["gate_satisfied"], r)
        self.assertIn("auth error", r["auth_retry"]["note"])
        self.assertEqual(len(r["attempts"]), 1)

    def test_persistent_auth_error_fails_closed_after_one_retry(self):
        os.environ["FAKE_CODEX_AUTH_FAIL_ALWAYS"] = "1"
        r = self.ask("anything")
        self.assertFalse(r["ok"])
        self.assertIn("auth_retry", r)  # retried exactly once, then failed closed: never a loop
        self.assertEqual(len(r["attempts"]), 1)


class Round3(Base):
    """Fresh gate on the re-scoped change (2026-10-08)."""
    ask = Gate.ask

    def test_unnamed_need_file_is_a_blocker_not_a_read(self):  # finding 3
        log = Path(self.tmp.name) / "prompts.log"
        os.environ["FAKE_CODEX_PROMPT_LOG"] = str(log)
        try:
            r = self.ask("CONTEXT-OBJECTIONS\nNEED-FILE .env\nEND-CONTEXT-OBJECTIONS\n"
                         "READ-RECEIPT\n{{ECHO_CANARIES}}\nEND-READ-RECEIPT\n")
        finally:
            del os.environ["FAKE_CODEX_PROMPT_LOG"]
        self.assertFalse(r["gate_satisfied"])
        self.assertEqual(len(r["attempts"]), 1)  # no follow-up was sent
        self.assertTrue(any("outside the caller's list" in o for o in r["context_objections"]))
        self.assertEqual(log.read_text().count("====="), 1)

    def test_named_need_file_resume_lists_only_named_files(self):  # finding 3
        log = Path(self.tmp.name) / "prompts.log"
        os.environ["FAKE_CODEX_PROMPT_LOG"] = str(log)
        try:
            self.ask("CONTEXT-OBJECTIONS\nNEED-FILE `app.py`\nEND-CONTEXT-OBJECTIONS\n")
        finally:
            del os.environ["FAKE_CODEX_PROMPT_LOG"]
        follow = log.read_text().split("\n=====\n")[1]
        self.assertIn("Read ONLY these files", follow)
        self.assertIn("app.py", follow)

    def test_ceiling_holds_when_child_never_reads_stdin(self):  # finding 2
        d = Path(self.tmp.name)
        t = time.time()
        rc, _ = self.b.run_contained([sys.executable, "-c", "import time; time.sleep(30)"], "x" * 1_000_000,
                                     d / "o.txt", d / "e.txt", timeout=1)
        self.assertIsNone(rc)
        self.assertLess(time.time() - t, 15)
        self.assertFalse((d / "o.txt.stdin").exists())  # the prompt copy is removed

    def test_kill_without_sigkill(self):  # finding 4 (Windows has no signal.SIGKILL)
        import signal as real
        import types
        child = subprocess.Popen(["sleep", "30"])
        ident = self.b.identity(child.pid)
        orig = self.b.signal
        self.b.signal = types.SimpleNamespace(SIGTERM=real.SIGTERM)
        try:
            self.assertTrue(self.b.kill_identities([ident]))
        finally:
            self.b.signal = orig
            child.kill()
            child.wait()

    def test_downgraded_verdict_is_returned(self):  # finding 1
        jid = Worker.start(self, verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        Worker.wait(self, jid)
        r = self.b.cmd_verdict(jid, {"verdict": "accept", "requirements": [{"met": False}], "snapshot": "x"})
        self.assertEqual(r["verdict"], "fix-list")
        self.assertTrue(r["downgraded"])

    def test_packet_carries_companions_and_baseline(self):  # finding 5
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK})
        r = self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py", "tests"],
                              "verify": "echo '==== 3 passed in 0.10s ===='", "tier": "R0", "done": "d",
                              "manifest": ["tests/test_app.py"], "attestation": "a",
                              "companions": ["tests/test_app.py"], "baseline": "3 passed"})
        Worker.wait(self, r["job_id"])
        task = (Path(self.b.cmd_packet(r["job_id"])["packet"]) / "task.md").read_text()
        self.assertIn("contract files (read these in the worktree; they did not change)\ntests/test_app.py", task)
        self.assertIn("3 passed", task)
        with self.assertRaises(SystemExit):  # companions are intake-vetted like scope
            self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py"], "verify": "true",
                              "tier": "R0", "done": "d", "manifest": ["tests/test_app.py"], "attestation": "a",
                              "companions": [".env"]})


class Round3b(Base):
    """Round-2 findings on the Round3 fixes."""

    def test_requirement_met_must_be_literal_true(self):  # r2 finding 3 + 1
        v = self.b.validate_verdict
        self.assertTrue(v({"verdict": "accept", "requirements": [{"met": "false"}]}, {}))
        self.assertTrue(v({"verdict": "accept", "requirements": [None]}, {}))  # no AttributeError
        self.assertTrue(v({"verdict": "accept", "requirements": "all"}, {}))
        self.assertTrue(v({"verdict": "accept", "requirements": [{"met": True}], "verifier_changes": [None]},
                          {"computed": {"verifier_changes": ["tests/test_app.py"]}}))

    def test_symlinked_companion_refused_and_never_listed(self):  # r2 finding 2
        secret = Path(self.tmp.name) / "creds.txt"
        secret.write_text("x")
        (self.repo / "contract.md").symlink_to(secret)
        with self.assertRaises(SystemExit):
            self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py"], "verify": "true",
                              "tier": "R0", "done": "d", "manifest": ["tests/test_app.py"], "attestation": "a",
                              "companions": ["contract.md"]})
        self.assertFalse(self.b.plain_file_in(self.repo, "contract.md"))
        self.assertFalse(self.b.plain_file_in(self.repo, "tests"))
        self.assertTrue(self.b.plain_file_in(self.repo, "app.py"))



class DeltaGate(Base):
    """Codex gate on the post-round-1 delta (2026-10-08)."""

    def test_denylist_is_case_insensitive(self):  # DG-1
        for rel in ("Context/about.md", "MEMORY/x.md", ".ENV", "TOKEN.JSON", ".Claude/settings.json",
                    "Data/Raw/a.md", "a/.GIT/config", "prod.ENV"):
            self.assertTrue(self.b.denied(rel), rel)
        (self.ws / "Context").mkdir()
        (self.ws / "Context" / "about.md").write_text("x")
        self.assertTrue(self.b.denied_root(self.ws / "Context"))  # write-protected (a worker repo)
        # 0.1.1: personal context is READABLE (0.1.1 design); only secrets are refused for reads
        self.assertTrue(self.b.plain_file_in(self.ws, "Context/about.md"))
        for rel in (".ENV", "TOKEN.JSON", "a/.GIT/config", "prod.ENV", "keys/Server.PEM"):
            self.assertTrue(self.b.secret(rel), rel)
        for rel in ("Context/about.md", "MEMORY/x.md", ".Claude/settings.json", "Data/Raw/a.md"):
            self.assertFalse(self.b.secret(rel), rel)

    def test_symlinked_ancestor_companion_refused(self):  # DG-2
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "notes.md").write_text("secret")
        (self.repo / "real").mkdir()
        (self.repo / "real" / "notes.md").write_text("ok")
        os.symlink(self.repo / ".claude", self.repo / "docs")
        os.symlink(self.repo / "real", self.repo / "alias")
        self.assertFalse(self.b.plain_file_in(self.repo, "docs/notes.md"))
        self.assertFalse(self.b.plain_file_in(self.repo, "alias/notes.md"))  # any symlinked ancestor
        self.assertTrue(self.b.plain_file_in(self.repo, "real/notes.md"))

    def test_denied_area_inside_project_repo_refused(self):  # DG-4
        for d in ("context", "memory", "data/raw", "ops/global"):
            (self.repo / d).mkdir(parents=True)
            self.assertTrue(self.b.denied_root(self.repo / d), d)
            self.assertFalse(self.b.denied_root(self.repo / d, write=False), d)  # readable for ask/gate
        self.assertFalse(self.b.denied_root(self.repo))
        self.assertFalse(self.b.denied_root(self.ws))
        (self.repo / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SystemExit):  # secrets stay refused as an ask cwd
            self.b.cmd_ask({"mode": "ask", "prompt": "p", "files": [], "cwd": str(self.repo / ".git" / "hooks")})


class DeltaGate2(Base):
    """Codex gate round 2 on the delta fixes (2026-10-08)."""

    def test_bad_config_fails_closed(self):  # DG2-1, now for the config file
        cfg = self.home / "config.json"
        for bad in ("{not json", "[]", '{"protected_path": []}', '{"models": {"tiny": "x"}}', '{"models": {"cheap": ""}}',
                    '{"stakes": {"irreversible": ["nope", "high"]}}', '{"review": {"R0": "gpt", "R1": "none", "R2": "none"}}',
                    '{"stakes_aliases": {"x": "missing"}}', '{"protected_paths": [""]}',
                    '{"worker_refuse_pct": {"primary": 0, "secondary": 50}}'):
            cfg.write_text(bad)
            with self.assertRaises(SystemExit, msg=bad):
                load(self.home)

    def test_neutral_defaults_without_a_config(self):
        (self.home / "config.json").unlink()
        b = load(self.home)
        for rel in (".claude/settings.json", ".github/workflows/ci.yml", "AGENTS.md", "docs/CLAUDE.md", ".mcp.json",
                    ".codex/config.toml", "a/.env"):
            self.assertTrue(b.denied(rel), rel)
        for rel in ("memory/x.md", "sync.py", "src/app.py", "docs/guide.md"):
            self.assertFalse(b.denied(rel), rel)  # the author's extras are config, not defaults
        self.assertEqual(sorted(b.STAKES), ["irreversible", "policy", "trust-boundary", "unattended"])
        self.assertNotIn("backbone", b.GATE_TRIGGERS)
        self.assertEqual(b.LOG_PATH, self.home / "burn.jsonl")
        self.assertEqual(b.WORKER_REFUSE_PCT, {"primary": 70, "secondary": 85})
        self.assertEqual(b.cmd_config()["review"], {"R0": "none", "R1": "sonnet", "R2": "opus"})

    def test_config_aliases_and_extra_paths(self):
        self.assertEqual(self.b.GATE_TRIGGERS["backbone"], self.b.GATE_TRIGGERS["unattended"])
        self.assertTrue(self.b.denied("Memory/notes.md"))  # extras compare case-insensitively too
        self.assertTrue(self.b.denied("AGENTS.md"))         # defaults still apply alongside extras

    def test_symlinked_home_dot_dir_still_denied(self):  # DG2-2
        home = Path(self.tmp.name) / "fakehome"
        ext = Path(self.tmp.name) / "elsewhere"
        (ext / "projects").mkdir(parents=True)
        home.mkdir()
        os.symlink(ext, home / ".claude")
        real = Path.home
        Path.home = staticmethod(lambda: home)
        try:
            self.assertTrue(self.b.denied_root(home / ".claude" / "projects"))
        finally:
            Path.home = real

    def test_rel_under_is_component_wise(self):  # DG2-3
        ru = self.b.rel_under
        # casefold equates these names; the remainder must be intact (was "/context/x" / "ontext/x")
        self.assertEqual(ru(Path("/a/STRASSE/context/x"), Path("/a/Straße")), "context/x")
        self.assertEqual(ru(Path("/a/Straße/context/x"), Path("/a/STRASSE")), "context/x")
        self.assertIsNone(ru(Path("/a/bc/x"), Path("/a/b")))
        self.assertEqual(ru(Path("/A/B/Context/x"), Path("/a/b")), "Context/x")
        self.assertEqual(ru(Path("/a/b"), Path("/a/b/")), ".")

    def test_accept_with_missing_companion_is_downgraded(self):  # DG3-1
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK})
        r = self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py"],
                              "verify": "echo '==== 3 passed in 0.10s ===='", "tier": "R0", "done": "d",
                              "manifest": ["tests/test_app.py"], "attestation": "a", "companions": ["app.py"]})
        jid = r["job_id"]
        Worker.wait(self, jid)
        (self.b.worktree_dir(jid) / "app.py").unlink()  # the worker deleted an in-scope companion
        v = self.b.cmd_verdict(jid, {"verdict": "accept", "requirements": [{"met": True}], "snapshot": "x"})
        self.assertEqual(v["verdict"], "fix-list")
        self.assertTrue(any("companions are missing" in d for d in v["downgraded"]))

    def test_uncommitted_companion_refused(self):  # DG2-4
        (self.repo / "contract.md").write_text("c")  # regular file, never committed
        with self.assertRaises(SystemExit):
            self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py"], "verify": "true",
                              "tier": "R0", "done": "d", "manifest": ["tests/test_app.py"], "attestation": "a",
                              "companions": ["contract.md"]})

class CodeReview(Base):
    """/code-review findings on the re-scoped change (2026-10-08)."""

    def test_cwd_or_repo_inside_denied_area_refused(self):  # CR-1
        (self.ws / "context").mkdir()
        self.assertTrue(self.b.denied_root(self.ws / "context"))
        self.assertFalse(self.b.denied_root(self.ws / "context", write=False))
        self.assertTrue(self.b.denied_root(Path.home() / ".claude" / "projects"))
        self.assertTrue(self.b.denied_root(Path.home() / ".claude" / "settings.json", write=False))
        self.assertFalse(self.b.denied_root(Path.home() / ".claude" / "projects" / "x" / "memory", write=False))
        self.assertTrue(self.b.denied_root(Path.home() / ".claude" / "projects" / "x" / "memory"))  # never written
        self.assertFalse(self.b.denied_root(self.repo))
        self.assertFalse(self.b.denied_root(self.ws))
        with self.assertRaises(SystemExit):
            self.b.cmd_ask({"mode": "ask", "prompt": "p", "files": [], "cwd": str(Path.home() / ".ssh")})

    def test_failed_start_releases_its_lease(self):  # CR-4
        real = self.b.git

        def git(cwd, *a):
            if a[:2] == ("worktree", "add"):
                return subprocess.CompletedProcess(a, 1, "", "boom")
            return real(cwd, *a)
        self.b.git = git
        try:
            with self.assertRaises(SystemExit):
                self.start(REPLY=CLAIM_OK)
        finally:
            self.b.git = real
        self.assertEqual(self.b.lock_holder(), [])
        self.assertEqual(list((self.home / "jobs").iterdir()) if (self.home / "jobs").exists() else [], [])

    def test_pytest_quiet_failure_summary_parses(self):  # CR-10
        n = self.b.normalize("F....\nFAILED tests/test_app.py::test_add - assert\n1 failed, 4 passed in 0.12s\n", 1)
        self.assertEqual((n["failed"], n["passed"]), (1, 4))
        self.assertEqual(n["failing"], ["tests/test_app.py::test_add"])


class Normalize(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HERE.parent))
        import bridge
        self.b = bridge

    def test_parsers(self):
        n = self.b.normalize("FAILED tests/t.py::test_x - assert\n=== 1 failed, 4 passed in 1s ===", 1)
        self.assertEqual((n["passed"], n["failed"], n["failing"]), (4, 1, ["tests/t.py::test_x"]))
        u = self.b.normalize("Ran 5 tests in 0.1s\n\nFAILED (failures=1, errors=1)", 1)
        self.assertEqual((u["passed"], u["failed"], u["errors"]), (3, 1, 1))
        self.assertEqual(self.b.normalize("Ran 2 tests in 0s\n\nOK", 0)["passed"], 2)
        self.assertIsNone(self.b.normalize("all good, trust me", 0))

    def test_fill_single_pass(self):
        self.assertEqual(self.b.fill("{{A}}/{{B}}", A="{{B}}", B="x"), "{{B}}/x")


class Worker(Base):
    def start(self, verify="python3 -m pytest -q tests 2>&1 || true", **env):
        os.environ.update({f"FAKE_CODEX_{k}": v for k, v in env.items()})
        r = self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py", "tests"],
                              "verify": verify, "tier": "R0", "done": "d",
                              "manifest": ["tests/test_app.py"], "attestation": "not a gate category"})
        return r["job_id"]

    def wait(self, jid, terminal=("receipted", "timeout", "crashed", "refused"), limit=60):
        t = time.time()
        while time.time() - t < limit:
            if self.b.state_of(jid) in terminal:
                return self.b.state_of(jid)
            time.sleep(0.3)
        self.fail(f"job stuck in {self.b.state_of(jid)}")

    def receipt(self, jid):
        return self.b.read_json(self.b.job_dir(jid) / "receipt.json")

    def test_clean_job(self):
        jid = self.start(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                         EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.assertEqual(self.wait(jid), "receipted")
        r = self.receipt(jid)
        self.assertEqual(r["outcome"], "clean", r)
        self.assertEqual(r["computed"]["changed"], ["app.py"])
        self.assertEqual(self.b.lock_holder(), [])  # released after the tree died

    def test_verifier_modified(self):
        jid = self.start(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                         EDIT="tests/test_app.py=def test_add():\n    pass\n")
        self.wait(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "verifier-modified")

    def test_verifier_change_allowed_is_clean_but_listed(self):
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK,
                           "FAKE_CODEX_EDIT": "tests/test_app.py=def test_add():\n    assert 1 + 2 == 3\n"})
        r = self.b.cmd_start({"task": "t", "repo": str(self.repo), "scope": ["app.py", "tests"],
                              "verify": "echo '==== 3 passed in 0.10s ===='", "tier": "R0", "done": "d",
                              "manifest": ["tests/test_app.py"], "attestation": "a", "verifier_changes_allowed": True})
        self.wait(r["job_id"])
        rc = self.receipt(r["job_id"])
        self.assertEqual(rc["outcome"], "clean")
        self.assertEqual(rc["computed"]["verifier_changes"], ["tests/test_app.py"])

    def test_out_of_scope_write(self):
        jid = self.start(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK, EDIT="other.py=x=1\n")
        self.wait(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "out-of-scope")

    def test_matching_red_runs_not_clean(self):
        red = "==== 1 failed, 2 passed in 0.1s ===="
        jid = self.start(verify=f"echo '{red}'; exit 1",
                         REPLY=f"VERIFY-CLAIM\nexit: 1\n{red}\nEND-VERIFY-CLAIM\n")
        self.wait(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "verify-failed")

    def test_zero_tests_executed_not_clean(self):
        z = "Ran 0 tests in 0.000s\n\nOK"
        jid = self.start(verify=f"printf '{z}'", REPLY=f"VERIFY-CLAIM\nexit: 0\n{z}\nEND-VERIFY-CLAIM\n")
        self.wait(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "verify-failed")

    def test_unparseable_verify_not_clean(self):
        jid = self.start(verify="echo looks fine", REPLY="VERIFY-CLAIM\nexit: 0\nlooks fine\nEND-VERIFY-CLAIM\n")
        self.wait(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "mismatch")

    def test_build_artifacts_not_scope_violations(self):  # smoke test: repo without .gitignore
        jid = self.start(verify="mkdir -p __pycache__ && echo x > __pycache__/app.cpython-313.pyc; echo '==== 3 passed in 0.10s ===='",
                         REPLY=CLAIM_OK, EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.wait(jid)
        r = self.receipt(jid)
        self.assertEqual(r["outcome"], "clean", r["computed"])
        self.assertEqual(r["computed"]["artifacts"], ["__pycache__/app.cpython-313.pyc"])
        patch = (self.b.job_dir(jid) / "diff.patch").read_text()
        self.assertNotIn(".pyc", patch)
        self.assertIn("app.py", patch)

    def test_verify_created_files_counted(self):
        jid = self.start(verify="echo x > made_by_verify.txt; echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        self.wait(jid)
        r = self.receipt(jid)
        self.assertIn("made_by_verify.txt", r["computed"]["changed"])
        self.assertEqual(r["outcome"], "out-of-scope")

    def inproc(self, **env):
        os.environ["CODEX_BRIDGE_NO_SPAWN"] = "1"
        try:
            jid = self.start(**env)
        finally:
            del os.environ["CODEX_BRIDGE_NO_SPAWN"]
        return jid

    def test_incomplete_scan(self):
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        real = self.b.git
        self.b.git = lambda wt, *a: (subprocess.CompletedProcess(a, 1, "", "warning: could not open directory 'x/'")
                                     if a[:1] == ("status",) else real(wt, *a))
        try:
            self.b.cmd_supervise(jid)
        finally:
            self.b.git = real
        self.assertEqual(self.receipt(jid)["outcome"], "incomplete-scan")

    def test_ceiling_kills_tree_including_setsid_escape(self):
        self.b.WORKER_CEILING_S = 2
        jid = self.inproc(SLEEP="120", CHILD="1", REPLY=CLAIM_OK)
        before = subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split()
        self.b.cmd_supervise(jid)
        st = self.b.read_json(self.b.job_dir(jid) / "state.json")
        self.assertEqual(st["state"], "timeout")
        self.assertTrue(st["quarantine"])
        self.assertEqual(self.b.lock_holder(), [])
        after = subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split()
        self.assertFalse(set(after) - set(before), "setsid grandchild survived the kill")

    def test_rename_from_out_of_scope_caught(self):  # finding 8
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        wt = self.b.worktree_dir(jid)
        subprocess.run(["git", "-C", str(wt), "mv", "tests/test_app.py", "app_moved_test.py"], check=True)
        job = self.b.read_json(self.b.job_dir(jid) / "job.json")
        job["inputs"]["scope"] = ["app_moved_test.py", "app.py"]
        (self.b.job_dir(jid) / "final.txt").write_text(CLAIM_OK)
        r = self.b.compute_receipt(jid, job, 0, 0)
        self.assertIn("tests/test_app.py", r["computed"]["out_of_scope"])

    def test_leftover_children_killed_on_normal_exit_and_lock_released(self):  # findings 4 + 5
        jid = self.inproc(CHILD="1", REPLY=CLAIM_OK, verify="echo '==== 3 passed in 0.10s ===='")
        before = set(subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split())
        self.b.cmd_supervise(jid)
        after = set(subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split())
        self.assertFalse(after - before, "child left by a normally-exiting Codex survived")
        self.assertEqual(self.b.lock_holder(), [])

    def test_denied_path_written_inside_broad_scope(self):  # finding 2
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        wt = self.b.worktree_dir(jid)
        (wt / "sub" / ".claude").mkdir(parents=True)
        (wt / "sub" / ".claude" / "settings.json").write_text("{}")
        job = self.b.read_json(self.b.job_dir(jid) / "job.json")
        job["inputs"]["scope"] = ["sub"]
        (self.b.job_dir(jid) / "final.txt").write_text(CLAIM_OK)
        r = self.b.compute_receipt(jid, job, 0, 0)
        self.assertEqual(r["outcome"], "out-of-scope")

    def test_verify_env_has_no_secrets_and_descendants_die(self):  # gate findings 2 + 3
        os.environ["CODEX_BRIDGE_TEST_SECRET"] = "s3cr3t-value"
        try:
            verify = ("env > env-dump.txt; (sleep 300 &) ; echo '==== 3 passed in 0.10s ===='")
            jid = self.inproc(verify=verify, REPLY=CLAIM_OK)
            before = set(subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split())
            self.b.cmd_supervise(jid)
            after = set(subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout.split())
            self.assertFalse(after - before, "verify's background child survived")
            dump = (self.b.worktree_dir(jid) / "env-dump.txt").read_text()
            self.assertNotIn("s3cr3t-value", dump)
        finally:
            del os.environ["CODEX_BRIDGE_TEST_SECRET"]

    def test_tracked_or_non_generated_artifact_paths_still_counted(self):  # gate finding 5
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "tracked.pyc").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "-Af"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "tracked pyc"], check=True)
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="__pycache__/tracked.pyc=changed;__pycache__/payload.py=evil()")
        self.b.cmd_supervise(jid)
        oos = self.receipt(jid)["computed"]["out_of_scope"]
        self.assertIn("__pycache__/tracked.pyc", oos)
        self.assertIn("__pycache__/payload.py", oos)

    def test_new_conftest_is_a_verifier_change(self):  # gate finding 8
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="tests/conftest.py=import pytest\n")
        self.b.cmd_supervise(jid)
        r = self.receipt(jid)
        self.assertIn("tests/conftest.py", r["computed"]["verifier_changes"])
        self.assertEqual(r["outcome"], "verifier-modified")

    def test_edit_after_receipt_is_stale(self):  # gate finding 6
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        snap = self.receipt(jid)["snapshot"]
        (self.b.worktree_dir(jid) / "app.py").write_text("def add(a, b):\n    return 0\n")
        self.refused(self.b.cmd_packet, jid)
        self.assertTrue(self.b.cmd_verdict(jid, {"verdict": "accept", "snapshot": snap})["stale"])

    def test_abandoned_review_resets(self):  # gate finding 9
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        self.assertTrue(self.b.cmd_review_start(jid)["started"])
        self.assertEqual(self.b.cmd_review_reset(jid), {"ok": True, "reset": True})
        self.assertEqual(self.b.state_of(jid), "receipted")
        self.assertTrue(self.b.cmd_review_start(jid)["started"])

    def test_unparseable_review_retries_once(self):
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        self.assertTrue(self.b.cmd_review_start(jid)["started"])
        self.assertEqual(self.b.cmd_review_retry(jid), {"ok": True, "retried": True})
        self.assertEqual(self.b.state_of(jid), "receipted")
        self.assertTrue(self.b.cmd_review_start(jid)["started"])
        self.assertEqual(self.b.cmd_review_retry(jid), {"ok": True, "retried": False})  # once only
        self.assertEqual(self.b.state_of(jid), "reviewing")
        r = self.b.cmd_verdict(jid, {"verdict": "unparseable", "snapshot": None})
        self.assertEqual(r["verdict"], "invalid")
        self.assertTrue(r["stale"])  # still fails closed
        self.assertFalse(r["worktree_changed"])  # but the worktree did not move

    def test_review_generation_guards_reset_retry_and_verdict(self):  # gate: reset/retry race
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        snap = self.receipt(jid)["snapshot"]
        g1 = self.b.cmd_review_start(jid)["review_gen"]
        self.assertTrue(self.b.cmd_review_retry(jid, str(g1))["retried"])
        g2 = self.b.cmd_review_start(jid)["review_gen"]
        self.assertNotEqual(g1, g2)
        # a reset aimed at the first (dead) review must not kill the replacement
        self.assertEqual(self.b.cmd_review_reset(jid, str(g1)), {"ok": True, "reset": False})
        self.assertEqual(self.b.state_of(jid), "reviewing")
        self.assertTrue(self.b.cmd_review_retry(jid, str(g1)).get("superseded"))
        # the first reviewer's late verdict is refused; the current one records
        self.assertFalse(self.b.cmd_verdict(jid, {"verdict": "accept", "snapshot": snap, "review_gen": g1})["ok"])
        self.assertFalse((self.b.job_dir(jid) / "verdict.json").exists())
        r = self.b.cmd_verdict(jid, {"verdict": "accept", "snapshot": snap, "review_gen": g2})
        self.assertTrue(r["ok"])
        # a stale generation can never retire the current review; the current one can
        self.assertFalse(self.b.cmd_mark(jid, "notified", str(g1))["marked"])
        self.assertEqual(self.b.state_of(jid), "reviewed")
        self.assertTrue(self.b.cmd_mark(jid, "notified", str(g2))["marked"])

    def test_losing_watcher_sees_benign_already_started(self):  # gate round 3 #2
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        self.assertTrue(self.b.cmd_review_start(jid)["started"])
        r = self.b.cmd_review_start(jid)  # a second session's watcher, one tick late
        self.assertEqual((r["ok"], r["started"]), (True, False))
        self.assertEqual(self.b.state_of(jid), "reviewing")

    def test_reset_revokes_the_old_reviewer(self):  # gate round 3 #3
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        snap = self.receipt(jid)["snapshot"]
        g1 = self.b.cmd_review_start(jid)["review_gen"]
        self.assertTrue(self.b.cmd_review_reset(jid, str(g1))["reset"])
        # late verdict from the reset reviewer, before any replacement starts
        self.assertFalse(self.b.cmd_verdict(jid, {"verdict": "accept", "snapshot": snap, "review_gen": g1})["ok"])
        self.assertEqual(self.b.state_of(jid), "receipted")
        self.assertTrue(self.b.cmd_review_start(jid)["started"])  # the replacement can still start

    def test_report_lease_is_exclusive_and_recovers_a_crashed_reporter(self):  # watcher audit
        jid = self.inproc(REPLY=CLAIM_OK)
        self.assertTrue(self.b.cmd_report_begin(jid)["won"])
        self.assertFalse(self.b.cmd_report_begin(jid)["won"])  # another session, mid-report
        lease = self.b.job_dir(jid) / "claim-reporting"
        old = time.time() - self.b.REPORT_LEASE_S - 5
        os.utime(lease, (old, old))  # the first reporter crashed long ago
        self.assertTrue(self.b.cmd_report_begin(jid)["won"])
        self.assertTrue(self.b.claim(jid, "notify"))
        self.assertFalse(self.b.cmd_report_begin(jid)["won"])  # reported: never again

    def test_review_start_under_contention_is_busy_not_a_refusal(self):  # watcher audit gate #1
        jid = self.inproc(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK,
                          EDIT="app.py=def add(a, b):\n    return b + a\n")
        self.b.cmd_supervise(jid)
        real = self.b.review_lock.__init__
        self.b.review_lock.__init__ = lambda self_, j, wait=15.0, raise_busy=False: real(self_, j, 0.2, raise_busy)
        try:
            with self.b.review_lock(jid):  # another session is mid-start
                r = self.b.cmd_review_start(jid)
        finally:
            self.b.review_lock.__init__ = real
        self.assertEqual(r, {"ok": True, "started": False, "reason": "busy"})
        self.assertEqual(self.b.state_of(jid), "receipted")

    def test_review_lock_excludes_and_releases(self):  # gate round 2: no age-based takeover
        jid = self.inproc(REPLY=CLAIM_OK)
        with self.b.review_lock(jid):
            with self.assertRaises(SystemExit):  # a second holder waits, then fails closed
                with self.b.review_lock(jid, wait=0.2):
                    pass
        with self.b.review_lock(jid, wait=0.2):  # released on exit
            pass
        self.assertTrue((self.b.job_dir(jid) / "review.lock").exists())  # never unlinked by path

    def test_sweep_marks_dead_supervisor_once(self):
        jid = "manual-job"
        self.b.job_dir(jid).mkdir(parents=True)
        self.b.write_json(self.b.job_dir(jid) / "job.json", {"id": jid, "machine": self.b.machine()})
        self.b.transition(jid, "running", supervisor="999999:dead")
        self.assertEqual(self.b.cmd_sweep()["crashed"], [jid])
        self.assertEqual(self.b.cmd_sweep()["crashed"], [])

    def test_finished_while_closed_reads_exited_not_crashed(self):
        jid = self.start(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        self.wait(jid)
        self.assertEqual(self.b.cmd_sweep()["crashed"], [])
        self.assertEqual(self.b.state_of(jid), "receipted")

    def test_claim_once_and_stale_verdict(self):
        jid = self.start(verify="echo '==== 3 passed in 0.10s ===='", REPLY=CLAIM_OK)
        self.wait(jid)
        self.assertTrue(self.b.claim(jid, "review"))
        self.assertFalse(self.b.claim(jid, "review"))
        self.assertTrue(self.b.cmd_verdict(jid, {"verdict": "accept", "snapshot": "old"})["stale"])

    def test_discard_refuses_running(self):
        jid = self.inproc(REPLY=CLAIM_OK)
        self.b.transition(jid, "running", supervisor=self.b.identity())
        self.refused(self.b.cmd_discard, jid)

def limits(primary=10, secondary=20, allowed=True, reached=None, p_reset=3600, s_reset=86400):
    return json.dumps({"ordinaryUsageAllowed": allowed, "rateLimits": {
        "primary": {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": int(time.time()) + p_reset},
        "secondary": {"usedPercent": secondary, "windowDurationMins": 10080, "resetsAt": int(time.time()) + s_reset},
        "rateLimitReachedType": reached, "planType": "plus"}})


class V011(Base):
    """0.1.1: model policy, usage preflight, denylist split, suggested patches, deps, JS parsers,
    selftest gating, cancel, Windows argv."""

    def req(self, **kw):
        base = {"task": "t", "repo": str(self.repo), "scope": ["app.py"], "verify": "echo '==== 3 passed in 0.10s ===='",
                "tier": "R0", "done": "d", "manifest": ["tests/test_app.py"], "attestation": "not a gate category"}
        return {**base, **kw}

    def start_inproc(self, **kw):
        os.environ["CODEX_BRIDGE_NO_SPAWN"] = "1"
        try:
            return self.b.cmd_start(self.req(**kw))
        finally:
            del os.environ["CODEX_BRIDGE_NO_SPAWN"]

    # ---- model policy
    def test_model_mapping_and_raise_only(self):
        cm = self.b.choose_model
        self.assertEqual(cm("gate", {"trigger": "irreversible"})[:2], (self.b.STANDARD, "high"))
        self.assertEqual(cm("gate", {"trigger": "backbone"})[:2], (self.b.STANDARD, "medium"))
        self.assertEqual(cm("worker", {"tier": "R2"})[:2], (self.b.STANDARD, "high"))
        self.assertEqual(cm("ask", {})[:2], (self.b.STANDARD, "medium"))
        self.assertIn("raised", cm("gate", {"trigger": "backbone", "effort": "high"})[2])
        self.assertEqual(cm("gate", {"trigger": "irreversible", "model": self.b.STRONG, "effort": "medium"})[0], self.b.STRONG)
        for lane, r in (("gate", {}), ("gate", {"trigger": "nope"}),                       # trigger required
                        ("gate", {"trigger": "irreversible", "effort": "medium"}),        # lower: refused
                        ("gate", {"trigger": "backbone", "model": self.b.CHEAP}),          # never Luna for a gate
                        ("gate", {"trigger": "backbone", "effort": "xhigh"}),             # never xhigh
                        ("ask", {"effort": "low"}),
                        ("worker", {"tier": "R0", "model": self.b.CHEAP, "effort": "high"}),  # below: needs experiment
                        ("worker", {"tier": "R0", "model": self.b.STRONG})):
            with self.assertRaises(SystemExit, msg=f"{lane} {r}"):
                cm(lane, r)
        m, e, why = cm("worker", {"tier": "R0", "model": self.b.CHEAP, "effort": "high", "experiment": "bench-1"})
        self.assertEqual((m, e), (self.b.CHEAP, "high"))
        self.assertIn("BELOW mapping for experiment", why)

    def test_gate_passes_chosen_model_to_codex(self):
        log = Path(self.tmp.name) / "argv.log"
        os.environ["FAKE_CODEX_REPLY"] = "x"
        self.b.CODEX_REAL = self.b.CODEX
        seen = []
        real = self.b.run_contained
        self.b.run_contained = lambda argv, *a, **k: (seen.append(argv), real(argv, *a, **k))[1]
        r = self.b.cmd_ask({"mode": "gate", "trigger": "trust-boundary", "prompt": "p", "diff": "+x", "cwd": str(self.repo)})
        self.assertIn('model="gpt-6.1-sol"', seen[0])
        self.assertIn('model_reasoning_effort="high"', seen[0])
        self.assertEqual((r["model"], r["effort"]), (self.b.STANDARD, "high"))
        self.assertIn("trigger=trust-boundary", r["model_reason"])
        self.assertEqual(r["resume"], "codex resume fake-thread")
        self.assertEqual(r["quota"]["delta"], {"primary": 0.0, "secondary": 0.0})

    # ---- usage preflight
    def test_preflight_rules(self):
        pf, parse = self.b.preflight, lambda j: self.b.parse_limits(json.loads(j))
        self.assertIsNone(pf("worker", {"ok": False, "error": "x"})[0])                       # unknown never blocks
        self.assertIn("unknown", pf("worker", {"ok": False, "error": "x"})[1][0])
        self.assertIsNone(pf("gate", parse(limits(primary=69)))[0])
        self.assertIn("worker cap", pf("worker", parse(limits(primary=96)))[0])            # static worker cap
        self.assertIn("worker cap", pf("worker", parse(limits(secondary=96)))[0])
        gate_refusal, gate_warn = pf("gate", parse(limits(primary=90)))
        self.assertIsNone(gate_refusal)                                                    # gate: warn only
        self.assertIn("5h 90%", gate_warn[0])
        refusal = pf("gate", parse(limits(allowed=False, reached="primary", p_reset=20 * 60)))[0]
        self.assertIn("limit reached", refusal)
        self.assertIn("in 20 min", refusal)
        self.assertIn("after the reset", refusal)                                          # reset hint (< 45 min)
        self.assertNotIn("after the reset", pf("worker", parse(limits(primary=96, p_reset=3 * 3600)))[0])

    def test_worker_cap_switches_to_measured_estimate(self):
        log = self.ws / "ops" / "logs" / "burn.jsonl"
        with open(log, "a", encoding="utf-8") as f:
            for d in (4, 5, 6):
                f.write(json.dumps({"lane": "codex-bridge-worker", "quota_delta": {"primary": d, "secondary": 1}}) + "\n")
        self.assertEqual(self.b.estimate("worker", "primary"), 5)
        parse = lambda j: self.b.parse_limits(json.loads(j))  # noqa: E731
        self.assertIsNone(self.b.preflight("worker", parse(limits(primary=80)))[0])         # 80 + 5 <= 100
        self.assertIn("measures ~5.0", self.b.preflight("worker", parse(limits(primary=97)))[0])

    def test_worker_start_refused_on_quota_with_reset(self):
        os.environ["CODEX_BRIDGE_LIMITS_JSON"] = limits(primary=97)
        with self.assertRaises(SystemExit):
            self.start_inproc()

    def test_real_limits_reader_over_app_server(self):
        del os.environ["CODEX_BRIDGE_LIMITS_JSON"]
        os.environ["FAKE_CODEX_LIMITS"] = limits(primary=33)
        lim = self.b.codex_limits(timeout=10)
        self.assertTrue(lim["ok"])
        self.assertEqual(lim["windows"]["primary"]["used"], 33)
        os.environ["FAKE_CODEX_LIMITS_HANG"] = "1"
        t = time.time()
        lim = self.b.codex_limits(timeout=2)
        self.assertFalse(lim["ok"])                     # a hung app-server is "unknown", not a hang
        self.assertLess(time.time() - t, 8)

    def test_usage_command(self):
        u = self.b.cmd_usage()
        self.assertTrue(u["ok"])
        self.assertIn("5h 10%", u["summary"])
        self.assertIsNone(u["preflight"]["worker"])

    # ---- denylist split + suggested patch
    def test_worker_scope_protected_but_reads_allowed(self):
        for bad in (["context/x.md"], ["memory/a.md"], [".claude/settings.json"], ["ops/global/x.md"]):
            with self.assertRaises(SystemExit, msg=bad):
                self.start_inproc(scope=bad)
        with self.assertRaises(SystemExit):
            self.start_inproc(manifest=[".env"])                     # secrets refused even as reads
        (self.repo / "context").mkdir()
        (self.repo / "context" / "about.md").write_text("who\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "ctx"], check=True)
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        r = self.start_inproc(companions=["context/about.md"])       # personal context is readable
        self.assertTrue(r["ok"])

    def test_suggested_patch_moved_out_and_not_out_of_scope(self):
        patch = "--- a/context/about.md\n+++ b/context/about.md\n@@ -1 +1 @@\n-who\n+who am I\n"
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK,
                           "FAKE_CODEX_EDIT": f"app.py=def add(a, b):\n    return a + b\n;.bridge-suggested.patch={patch}"})
        jid = self.start_inproc()["job_id"]
        self.b.cmd_supervise(jid)
        rc = self.receipt(jid)
        self.assertEqual(rc["outcome"], "clean", rc["computed"])
        self.assertEqual(rc["computed"]["suggested_patch"]["files"], ["context/about.md"])
        self.assertFalse(rc["computed"]["suggested_patch"]["applied"])
        self.assertFalse((Path(self.b.worktree_dir(jid)) / ".bridge-suggested.patch").exists())
        self.assertNotIn(".bridge-suggested.patch", rc["computed"]["changed"])

    def test_worker_records_model_quota_and_resume(self):
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        r = self.start_inproc(model=self.b.CHEAP, effort="high", experiment="bench")
        self.assertEqual((r["model"], r["effort"]), (self.b.CHEAP, "high"))
        self.b.cmd_supervise(r["job_id"])
        c = self.receipt(r["job_id"])["computed"]
        self.assertEqual((c["model"], c["effort"]), (self.b.CHEAP, "high"))
        self.assertEqual(c["quota"]["delta"], {"primary": 0.0, "secondary": 0.0})
        self.assertEqual(c["resume"], "codex resume fake-thread")

    def test_quota_death_is_its_own_outcome(self):
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK, "FAKE_CODEX_EXIT": "1"})
        jid = self.start_inproc()["job_id"]
        os.environ["CODEX_BRIDGE_LIMITS_JSON"] = limits(allowed=False, reached="primary")
        self.b.cmd_supervise(jid)
        self.assertEqual(self.receipt(jid)["outcome"], "quota-limited")

    # ---- deps
    def make_venv(self, version=None):
        v = self.repo / "tools" / ".venv"
        sp = v / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "depmod.py").write_text("VALUE = 41\n")
        ver = version or f"{sys.version_info[0]}.{sys.version_info[1]}.0"
        (v / "pyvenv.cfg").write_text(f"home = /x\nversion = {ver}\n")
        return "tools/.venv"

    def test_venv_dep_exposed_read_only_via_pythonpath(self):
        dep = self.make_venv()
        os.environ["FAKE_CODEX_REPLY"] = "VERIFY-CLAIM\nexit: 0\nRan 1 test in 0.0s\n\nOK\nEND-VERIFY-CLAIM\nOPEN\nnone\nEND-OPEN\n"
        verify = ("python3 -c \"import depmod, sys; assert depmod.VALUE == 41; "
                  "print('Ran 1 test in 0.0s'); print(); print('OK')\"")
        r = self.start_inproc(deps=[dep], verify=verify)
        self.b.cmd_supervise(r["job_id"])
        rc = self.receipt(r["job_id"])
        self.assertEqual(rc["outcome"], "clean", rc["computed"]["verify_rerun"])
        cfg = (self.home / "codexhome-jobs" / r["job_id"] / "config.toml").read_text()
        self.assertIn(str((self.repo / dep).resolve()), cfg)
        self.assertIn('= "read"', cfg)

    def test_bad_deps_refused(self):
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=["nope"])
        (self.repo / "node_modules").mkdir()
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=["node_modules"])   # only venvs in 0.1.1
        dep = self.make_venv(version="2.7.18")
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=[dep])              # interpreter version mismatch

    def test_dependency_files_are_verifier_changes(self):
        for f in ("requirements.txt", "tools/requirements-dev.txt", "package-lock.json", "bun.lock", "uv.lock"):
            self.assertTrue(self.b.VERIFIER.search(f), f)

    # ---- JS parsers
    def test_js_runner_summaries(self):
        n = self.b.normalize
        bun = "(pass) a > b [1.0ms]\n(fail) a > c [2.0ms]\n\n 11 pass\n 1 fail\nRan 12 tests across 1 file. [0.43s]\n"
        r = n(bun, 1)
        self.assertEqual((r["passed"], r["failed"]), (11, 1))
        self.assertEqual(r["failing"], ["a > c"])
        self.assertEqual(n(" 4 pass\n 0 fail\nRan 4 tests across 1 file. [0.23s]\n", 0)["passed"], 4)
        jest = "FAIL src/a.test.ts\nTests:       1 failed, 2 skipped, 5 passed, 8 total\n"
        r = n(jest, 1)
        self.assertEqual((r["passed"], r["failed"], r["skipped"]), (5, 1, 2))
        vit = " Test Files  1 passed (1)\n      Tests  3 failed | 12 passed (15)\n"
        r = n(vit, 1)
        self.assertEqual((r["passed"], r["failed"]), (12, 3))
        self.assertEqual(n("==== 3 passed in 0.10s ====", 0)["passed"], 3)   # pytest unchanged
        self.assertEqual(n("Ran 2 tests in 0.001s\n\nOK\n", 0)["passed"], 2)  # unittest unchanged

    # ---- selftest gating
    def test_workers_need_matching_selftest_proof(self):
        (self.home / "selftest.json").unlink()
        with self.assertRaises(SystemExit):
            self.start_inproc()
        self.prove(version="codex-cli 9.9.9")      # stale proof (codex upgraded since)
        with self.assertRaises(SystemExit):
            self.start_inproc()
        self.prove(passed=False)
        with self.assertRaises(SystemExit):
            self.start_inproc()

    def test_selftest_fails_honestly_without_a_real_sandbox(self):
        # the fake `codex sandbox` runs unsandboxed, so every must-fail probe succeeds: the proof must say so
        proof = self.b.cmd_selftest()
        self.assertFalse(proof["passed"])
        self.assertFalse(proof["results"]["read-dotenv"]["pass"])
        self.assertFalse(proof["results"]["write-outside"]["pass"])
        self.assertTrue(proof["results"]["runs"]["pass"])
        self.assertTrue(proof["results"]["env-secret-absent"]["pass"])   # the env allowlist works even unsandboxed
        self.assertFalse(self.b.selftest_ok("codex-cli 0.0.0-fake"))
        self.assertFalse(list(self.home.glob("selftest-outside-*")))     # probe litter removed

    # ---- cancel
    def test_cancel_running_worker(self):
        os.environ.update({"FAKE_CODEX_REPLY": CLAIM_OK, "FAKE_CODEX_SLEEP": "30"})
        jid = self.b.cmd_start(self.req())["job_id"]
        t = time.time()
        while self.b.state_of(jid) != "running" and time.time() - t < 20:
            time.sleep(0.2)
        self.assertTrue(self.b.cmd_cancel(jid)["cancelled"])   # waits for the supervisor's real outcome
        self.assertEqual(self.b.state_of(jid), "cancelled")
        self.assertLess(time.time() - t, 25)
        self.assertEqual(self.b.lock_holder(), [])
        with self.assertRaises(SystemExit):
            self.b.cmd_cancel(jid)                  # nothing left to cancel

    # ---- Windows argv
    def test_windows_verify_uses_cmd_and_keeps_system_env(self):
        seen = {}
        real = self.b.run_contained
        # like the real one, the fake creates its log files (sandboxed_verify moves them into jd)
        self.b.run_contained = lambda argv, _in, out, err, *a, **k: (out.write_text(""), err.write_text(""),
                                                                    seen.update(argv=argv, env=k.get("env")), (0, True))[-1]
        self.b.IS_WIN = True
        os.environ["SYSTEMROOT"] = r"C:\Windows"
        try:
            jd = Path(self.tmp.name) / "jd"
            jd.mkdir()
            (jd / "verify-out.log").write_text("")
            (jd / "verify-err.log").write_text("")
            self.b.sandboxed_verify(self.repo, "python -m pytest -q", jd)
        finally:
            self.b.IS_WIN = False
            self.b.run_contained = real
            del os.environ["SYSTEMROOT"]
        self.assertEqual(seen["argv"][-4:], ["cmd", "/d", "/c", "python -m pytest -q"])
        self.assertEqual(seen["env"]["SYSTEMROOT"], r"C:\Windows")

    def test_codex_resolved_via_path_lookup(self):
        src = (HERE.parent / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('shutil.which("codex")', src)


class Gate011(Base):
    """Codex gate round 1 on 0.1.1 (2026-10-08)."""
    req, start_inproc, make_venv = V011.req, V011.start_inproc, V011.make_venv

    def test_parent_cwd_cannot_reach_claude_settings(self):  # G1
        self.refused(self.b.cmd_ask, {"mode": "ask", "prompt": "p", "files": [".claude/settings.json"],
                                      "cwd": str(Path.home())})
        self.refused(self.b.cmd_ask, {"mode": "ask", "prompt": "p", "files": [".ssh/config"], "cwd": str(Path.home())})

    def test_deps_symlinked_ancestor_and_secret_contents_refused(self):  # G2, narrowed in round 2
        dep = self.make_venv()
        venv = self.repo / "tools" / ".venv"
        sp = next(venv.glob("lib/python3*/site-packages"))
        (sp / ".env").write_text("TOKEN=x\n")
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=[dep])                 # a secret-named file inside the READABLE site-packages
        (sp / ".env").unlink()
        os.symlink("/etc", sp / "out")
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=[dep])                 # a symlink out of site-packages
        (sp / "out").unlink()
        os.symlink(self.repo / "tools", self.repo / "alias")
        with self.assertRaises(SystemExit):
            self.start_inproc(deps=["alias/.venv"])       # a symlinked ancestor

    def test_real_venv_shape_accepted_and_only_site_packages_granted(self):  # G2-r2: bin/ symlinks are normal
        dep = self.make_venv()
        venv = self.repo / "tools" / ".venv"
        (venv / "bin").mkdir()
        os.symlink(sys.executable, venv / "bin" / "python3")   # every real venv has this
        (venv / ".env").write_text("NOT=exposed\n")             # outside site-packages: never granted
        info = self.b.dep_info(self.repo, dep)
        self.assertTrue(info["path"].endswith("site-packages"))
        self.assertEqual(info["path"], info["site_packages"])

    def test_parser_counts_each_suite_once(self):  # G3-r2
        n = self.b.normalize
        self.assertIsNone(n(" 4 pass\n 0 fail\n==== 3 passed in 0.1s ====\nRan 2 tests in 0.0s\n\nFAILED (failures=1)\n", 0))
        self.assertEqual(n(" 4 pass\n 0 fail\nRan 4 tests across 1 file.\n", 0)["passed"], 4)  # not unittest
        self.assertEqual(n("Tests:       5 passed, 5 total\n", 0)["passed"], 5)                # not pytest
        r = n("==== 1 failed, 1 passed in 0.1s ====\n==== 2 passed in 0.1s ====\n", 1)
        self.assertEqual((r["passed"], r["failed"]), (3, 1))                                  # same runner sums
        self.assertIsNone(n("Ran 3 tests in 0.1s\n", 0))                  # no verdict line: unparseable
        self.assertEqual(n("1 failed, 2 passed in 0.03s", 1)["failed"], 1)  # pytest -q
        self.assertEqual(n("Ran 1 test in 0.0s\n\nFAILED\n", 1)["failed"], 1)

    def test_cancel_during_verification_is_not_reviewed(self):  # G7-r2
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        jid = self.start_inproc()["job_id"]
        real = self.b.compute_receipt

        def receipt_then_cancel(*a, **k):
            r = real(*a, **k)
            (self.b.job_dir(jid) / "cancel").write_text("x")   # the cancel lands while verify/accounting runs
            return r
        self.b.compute_receipt = receipt_then_cancel
        try:
            self.b.cmd_supervise(jid)
        finally:
            self.b.compute_receipt = real
        self.assertEqual(self.b.state_of(jid), "cancelled")
        with self.assertRaises(SystemExit):
            self.b.cmd_review_start(jid)                         # never auto-reviewed

    def test_composite_verify_is_never_parsed(self):  # G3, final rule: one runner per verify
        self.assertIsNone(self.b.normalize(" 1 pass\n 0 fail\nRan 1 tests across 1 file.\n==== 2 failed, 3 passed in 0.2s ====\n", 0))

    def test_env_family_and_settings_glob(self):  # G4 + G5
        for rel in (".envrc", ".envprod", ".env/values.txt", "a/.env.local"):
            self.assertTrue(self.b.secret(rel), rel)
        self.assertTrue(self.b.denied("config/settings.production.json"))
        self.assertFalse(self.b.denied("config/app.json"))

    def test_experiment_label_only_unlocks_luna(self):  # G6
        with self.assertRaises(SystemExit):
            self.b.choose_model("worker", {"tier": "R2", "effort": "medium", "experiment": "bench"})

    def test_cancel_arriving_as_codex_finishes_is_honored(self):  # G7
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        jid = self.start_inproc()["job_id"]
        real = self.b.run_contained

        def finish_then_cancel(*a, **k):
            r = real(*a, **k)
            (self.b.job_dir(jid) / "cancel").write_text("x")   # the cancel lands just after Codex exits
            return r
        self.b.run_contained = finish_then_cancel
        try:
            self.b.cmd_supervise(jid)
        finally:
            self.b.run_contained = real
        self.assertEqual(self.b.state_of(jid), "cancelled")
        self.assertFalse((self.b.job_dir(jid) / "receipt.json").exists())  # never receipted, never reviewed
        res = self.b.cmd_result(jid)
        self.assertEqual(res["model"]["model"], self.b.STANDARD)
        self.assertIsNotNone(res["quota"])

    def test_cancel_after_finish_says_too_late(self):  # G7
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        jid = self.start_inproc()["job_id"]
        self.b.cmd_supervise(jid)
        self.refused(self.b.cmd_cancel, jid)
        self.assertFalse((self.b.job_dir(jid) / "cancel").exists())

    def test_old_mod_without_trigger_gets_a_restart_message(self):  # G8
        with self.assertRaises(SystemExit):
            self.b.choose_model("gate", {})
        out = subprocess.run([sys.executable, str(HERE.parent / "bridge.py"), "ask"], input=json.dumps(
            {"mode": "gate", "prompt": "p", "diff": "+x", "cwd": str(self.repo)}), capture_output=True, text=True)
        self.assertIn("Restart Claude Code", out.stdout)


class Gate011c(Base):
    """Fresh gate on the round-2 fixes (2026-10-08)."""
    req, start_inproc, make_venv = V011.req, V011.start_inproc, V011.make_venv

    def test_coloured_failing_pytest_is_counted(self):  # C1
        r = self.b.normalize("\x1b[31m==== 1 failed, 2 passed in 0.01s ====\x1b[0m\n", 0)
        self.assertEqual((r["passed"], r["failed"]), (2, 1))

    def test_expected_failures_and_log_lines(self):  # C4 + unittest key parsing
        self.assertEqual(self.b.normalize("Ran 2 tests in 0.0s\n\nOK (expected failures=1)\n", 0)["failed"], 0)
        r = self.b.normalize("ERROR logger: expected connection failure\n1 passed in 0.01s\n", 0)
        self.assertEqual((r["passed"], r["failing"]), (1, []))       # a log line is not a failing test
        r = self.b.normalize("FAILED tests/t.py::test_x - assert 0\n1 failed in 0.01s\n", 1)
        self.assertEqual(r["failing"], ["tests/t.py::test_x"])

    def test_venv_secret_rule_only_exempts_cacert(self):  # C3
        dep = self.make_venv()
        sp = next((self.repo / "tools" / ".venv").glob("lib/python3*/site-packages"))
        (sp / "certifi").mkdir()
        (sp / "certifi" / "cacert.pem").write_text("public\n")
        self.b.dep_info(self.repo, dep)                            # the public CA bundle is fine
        for bad in ("service.env", "server.key", "private.pem"):
            (sp / bad).write_text("x\n")
            with self.assertRaises(SystemExit, msg=bad):
                self.b.dep_info(self.repo, dep)
            (sp / bad).unlink()

    def test_cancelled_during_verify_reports_cancelled(self):  # C5
        os.environ["FAKE_CODEX_REPLY"] = CLAIM_OK
        jid = self.start_inproc()["job_id"]
        real = self.b.compute_receipt
        self.b.compute_receipt = lambda *a, **k: ((self.b.job_dir(jid) / "cancel").write_text("x"), real(*a, **k))[1]
        try:
            self.b.cmd_supervise(jid)
        finally:
            self.b.compute_receipt = real
        st = self.b.read_json(self.b.job_dir(jid) / "state.json")
        self.assertEqual((st["state"], st["outcome"], st["receipt_outcome"]), ("cancelled", "cancelled", "clean"))


class Gate011d(Base):
    """Final round of the fresh gate (2026-10-08): escapes + identities + belt."""

    def test_every_escape_form_is_stripped(self):  # D1
        for wrap in ("\x1b[38:2::255:0:0m{}\x1b[0m", "\x1b]8;;http://x\x07{}\x1b]8;;\x07", "\x1b]0;title\x1b\\{}", "\x1b[1;31m{}\x1b[m"):
            r = self.b.normalize(wrap.format("==== 1 failed in 0.01s ====") + "\n", 0)
            self.assertEqual(r["failed"], 1, repr(wrap))

    def test_belt_blocks_listed_failure_even_if_counts_missed(self):  # D1 belt
        r = self.b.normalize("FAILED tests/t.py::test_x - assert 0\n1 failed in 0.01s\n", 0)
        self.assertEqual(r["failing"], ["tests/t.py::test_x"])
        src = (HERE.parent / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('rerun["failing"] or unexpected_ignored', src)

    def test_collection_error_identities_kept(self):  # D2
        a = self.b.normalize("ERROR tests/test_a.py - RuntimeError\n1 error in 0.1s\n", 1)
        b = self.b.normalize("ERROR tests/test_b.py - RuntimeError\n1 error in 0.1s\n", 1)
        self.assertEqual(a["failing"], ["tests/test_a.py"])
        self.assertFalse(self.b.same(a, b))
        self.assertEqual(self.b.normalize("ERROR logger: x\n1 passed in 0.01s\n", 0)["failing"], [])


class Gate011e(Base):
    """Parser gate (2026-10-08): JS suite failures, bun errors, \r, C1 CSI, spaced param ids."""

    def test_no_false_clean_reproductions(self):
        n = self.b.normalize
        bad = {
            "jest": "FAIL tests/bad.test.js\n  Test suite failed to run\nPASS tests/good.test.js\n"
                    "Test Suites: 1 failed, 1 passed, 2 total\nTests:       1 passed, 1 total\n",
            "vitest": " Test Files  1 failed | 1 passed (2)\n      Tests  1 passed (1)\n",
            "cr": "F [100%]\r==== 1 failed in 0.01s ====\n",
            "c1": "\x9b31m==== 1 failed in 0.01s ====\x9b0m\n",
            "bun": "(pass) works [0.1ms]\nerror: boom\n 1 pass\n 0 fail\n 1 error\nRan 1 test across 2 files. [0.1s]\n",
        }
        for name, out in bad.items():
            r = n(out, 0)
            self.assertTrue(r is None or r["failed"] or r["errors"], name)

    def test_final_pass_reproductions(self):
        n = self.b.normalize
        vit = ("Unhandled Errors\nVitest caught 1 unhandled error during the test run.\nError: boom\n"
               " Test Files  1 passed (1)\n      Tests  1 passed (1)\n     Errors  1 error\n")
        self.assertEqual(n(vit, 0)["errors"], 1)
        self.assertIsNone(n("tests/test_a.py .\n!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!\n==== 1 passed in 0.50s ====\n", 0))
        a = n("FAILED tests/t.py::test_x[alpha [one] two] - assert 0\n1 failed in 0.01s\n", 1)
        b = n("FAILED tests/t.py::test_x[beta [two] three] - assert 0\n1 failed in 0.01s\n", 1)
        self.assertEqual(a["failing"], ["tests/t.py::test_x[alpha [one] two]"])
        self.assertFalse(self.b.same(a, b))

    def test_spaced_param_ids_kept(self):
        a = self.b.normalize("FAILED tests/t.py::test_x[alpha one] - assert 0\n1 failed in 0.01s\n", 1)
        b = self.b.normalize("FAILED tests/t.py::test_x[beta two] - assert 0\n1 failed in 0.01s\n", 1)
        self.assertEqual(a["failing"], ["tests/t.py::test_x[alpha one]"])
        self.assertFalse(self.b.same(a, b))


if __name__ == "__main__":
    unittest.main()
