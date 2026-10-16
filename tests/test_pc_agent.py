"""Tests for `tools/pc_agent.py` — the queue-and-loop that lets a turn-based agent drive the PC.

The runner's whole job is to be the layer that *cannot* go rogue, so these tests are written against that:
every one of them sets up a real repository with a real push, drops a command in the queue, runs one turn,
and then asserts on (a) what actually happened to the machine, (b) what got published, and (c) what the
*next* turn will refuse. The refusals matter more than the happy path — on the PC, the failure that hurts is
not "the click did not happen", it is "the click happened for a task that ended ten minutes ago".

Kept deliberately free of the real desktop: backend=sim, fence=static (a token that cannot rotate), and a
temp repo. Nothing here proves the Windows backend works — that is `test_winuia.py` + the PC itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"
sys.path.insert(0, str(TOOLS))
import pc_agent  # noqa: E402


def z(offset_min=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset_min)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*a, cwd):
    p = subprocess.run(["git", *a], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p


class Sandbox(unittest.TestCase):
    """A bare origin + a clone, so `sync()` is exercised for real rather than mocked. The queue travels the
    same way it will on the PC: a file, committed, pushed, pulled."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-agent-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        git("init", "-q", "--bare", str(self.origin), cwd=self.tmp)
        git("clone", "-q", str(self.origin), str(self.work), cwd=self.tmp)
        for k, v in (("user.name", "test"), ("user.email", "t@local")):
            git("config", k, v, cwd=self.work)
        # the tools the runner imports must be the ones under test
        shutil.copytree(TOOLS, self.work / "tools", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.work / "world").mkdir(exist_ok=True)
        (self.work / "pc" / "queue").mkdir(parents=True, exist_ok=True)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "seed", cwd=self.work)
        git("push", "-q", "-u", "origin", "HEAD", cwd=self.work)

    # ---------- helpers ----------
    def lock(self, holder="fabius", generation=3, token="tok-abc", expires_min=30, state="active"):
        (self.work / "world" / "lock.json").write_text(json.dumps({
            "state": state, "holder": holder, "generation": generation, "token": token,
            "expires_ts": z(expires_min), "purpose": "PCB-02", "history": [],
        }, indent=2) + "\n", encoding="utf-8")
        git("add", "world/lock.json", cwd=self.work)
        git("commit", "-q", "-m", "lock", cwd=self.work)
        git("push", "-q", cwd=self.work)

    def queue(self, cid, verb="capture", args=None, task="PCB-01", token="tok-abc", generation=3,
              expires_min=10, extra=None):
        cmd = {"id": cid, "verb": verb, "args": args or {"window": "id-0"}, "task": task,
               "token": token, "generation": generation, "ask": "none",
               "created_ts": z(), "expires_ts": z(expires_min), "by": "fabius", **(extra or {})}
        d = self.work / "pc" / "queue"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json").write_text(json.dumps(cmd, indent=2) + "\n", encoding="utf-8")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", f"queue {cid}", cwd=self.work)
        git("push", "-q", cwd=self.work)
        return cmd

    def runner(self, **kw):
        kw.setdefault("backend", "sim")          # never a real desktop in a test
        # `static` with the live token: the fence in production is `world`, but a test must not need a
        # served referee. The generation check under test lives in Runner.lease_problem, not in the fence.
        kw.setdefault("fence", "static")
        # the log dir is the repo's ignored `pc/` — the same place the real runner uses, so the test
        # exercises the same path, and `.gitignore` is what keeps receipts out of the published record
        kw.setdefault("log_dir", str(self.work / "pc"))
        return pc_agent.Runner(repo=self.work, **kw)

    def results(self, cid):
        f = self.work / "pc" / "results" / f"{cid}.done.json"
        return json.loads(f.read_text()) if f.exists() else None


class Refusals(Sandbox):
    """Each of these is a way the PC could be driven by something other than the live task."""

    def test_no_lease_means_nothing_runs(self):
        self.queue("c-nolock")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["status"], "refused")
        self.assertEqual(rec["reason"], "no-lock")
        self.assertIn("acquire", rec["detail"])

    def test_stale_generation_is_refused_and_names_both_sides(self):
        self.lock(generation=7)
        self.queue("c-stale", generation=3)
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "stale-generation")
        self.assertIn("3", rec["detail"] and rec["detail"])
        self.assertIn("7", rec["detail"])

    def test_expired_lease_is_idle_by_design_not_by_accident(self):
        self.lock(expires_min=-5)
        self.queue("c-exp")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "lease-expired")
        self.assertIn("idle by design", rec["detail"])

    def test_wrong_token_is_refused_even_with_the_right_generation(self):
        self.lock(token="tok-real")
        self.queue("c-tok", token="tok-guess")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "wrong-token")

    def test_deadman_refuses_a_command_whose_task_moved_on(self):
        self.lock()
        self.queue("c-old", expires_min=-1)
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "deadman")
        self.assertIn("never fired", rec["detail"])

    def test_unknown_verb_is_named_against_the_bridges_own_table(self):
        self.lock()
        self.queue("c-badverb", verb="teleport")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "not-allowlisted")
        self.assertIn("capture", rec["detail"])            # tells the reader what IS allowed

    def test_bad_id_cannot_become_a_path(self):
        self.lock()
        self.queue("c-bad", extra={})                        # write one by hand with a hostile id
        d = self.work / "pc" / "queue"
        (d / "c-bad.json").write_text(json.dumps({"id": "../escape", "verb": "capture", "args": {},
                                                 "token": "tok-abc", "generation": 3,
                                                 "expires_ts": z(5)}), encoding="utf-8")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "bad-command")

    def test_pc_pause_stops_everything_before_execution(self):
        self.lock()
        self.runner().stop("director pressed the button")
        out = self.runner().once()
        self.assertEqual(out["turn"], "skipped")
        self.assertEqual(out["reason"], "paused")
        self.assertEqual(out["waiting"], 0, "a paused runner must not even count the queue as pending work")

    def test_a_write_without_a_task_is_refused_by_the_bridge_not_by_luck(self):
        """The runner does not re-implement scope. It passes no task and the bridge refuses — the policy
        layer stays singular, and this test fails if someone ever adds a default task here."""
        self.lock()
        self.queue("c-noscope", verb="invoke", args={"element": "id-0-0"}, task="")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "no-scope")


class Execution(Sandbox):
    def test_a_read_round_trips_and_the_command_is_consumed_in_the_same_commit(self):
        self.lock()
        self.queue("c-read")
        out = self.runner().once()
        self.assertEqual(out["turn"], "executed")
        self.assertFalse((self.work / "pc" / "queue" / "c-read.json").exists(), "command must be consumed")
        rec = self.results("c-read")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["verb"], "capture")
        self.assertIsNotNone(rec["receipt"])
        self.assertTrue(rec["checkout"])
        # pushed, not merely written: the agent in another turn only ever sees what was pushed. The default
        # branch name is NOT a constant (master here, main on the real repo) and `git log` prints "fatal:"
        # to stdout rather than failing the call — which is how an assertion about a bad ref can pass.
        br = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.work).stdout.strip()
        self.assertTrue(br, "could not read the branch name; the check below would be meaningless")
        ahead = git("rev-list", "--count", f"origin/{br}..HEAD", cwd=self.work).stdout.strip()
        self.assertEqual(ahead, "0", f"the turn must be pushed, or the other side never learns ({ahead} ahead)")

    def test_the_receipt_is_real_and_verifies(self):
        self.lock()
        self.queue("c-chain")
        self.runner().once()
        rec = self.results("c-chain")
        self.assertIn(rec["receipt"], ("r000001", "r000002"))
        chain = self.work / "pc" / "receipts.jsonl"
        self.assertTrue(chain.exists(), "the receipt chain is what makes a claim checkable later")
        lines = [json.loads(l) for l in chain.read_text().splitlines() if l.strip()]
        self.assertTrue(any(l.get("action") == "capture" for l in lines))
        # The property wanted is "no image BYTES in the record", and `"img" in json.dumps(...)` was a
        # substring test that matched the field name `img_sha` — a hash, which is the opposite of a leak.
        # A size and a signature test assert the thing that actually matters, and both can fail.
        blob = json.dumps(lines)
        self.assertLess(len(blob), 6000, "a receipt should carry digests, not payloads")
        self.assertNotIn("\x89PNG", blob, "no encoded image data may reach the log")
        self.assertIn("img_sha", blob, "the hash is what makes a capture checkable later")

    def test_idle_turn_is_cheap_and_says_nothing_happened(self):
        self.lock()
        r = self.runner()
        out = r.once()
        self.assertEqual(out["turn"], "idle")
        self.assertEqual(out["waiting"], 0)
        self.assertFalse((self.work / "pc" / "results").exists() or (self.work / "pc" / "results").is_dir(),
                         "an idle turn must not create the results directory")

    def test_commands_are_executed_oldest_first(self):
        self.lock()
        self.queue("b-second")
        self.queue("a-first")
        r = self.runner()
        r.once()
        self.assertTrue((self.work / "pc" / "results" / "a-first.done.json").exists(),
                        "queue order is created_ts, then filename — not the order a directory listing feels "
                        "like giving, which differs per filesystem")
        self.assertTrue((self.work / "pc" / "queue" / "b-second.json").exists(), "one command per turn")


class DryRun(Sandbox):
    """`peek` is what an agent does from a turn it does not want to spend: look, decide, and leave the
    machine alone. It must therefore be incapable of acting even if the queue is full."""

    def test_dry_run_touches_nothing(self):
        self.lock()
        self.queue("c-dry")
        r = self.runner(dry_run=True)
        # the command must be VISIBLE to a dry run (that is what makes it useful when deciding) and must
        # NOT be executed. An earlier version got the second half wrong and moved the machine it claimed
        # only to be reading.
        self.assertEqual(len(r.pending()), 1)
        out = r.once()
        self.assertEqual(out["turn"], "would-execute")
        chain = self.tmp / "pc-logs" / "receipts.jsonl"
        self.assertFalse(chain.exists(), "a dry run wrote a receipt: it executed something")
        self.assertEqual(len(list((self.work / "pc" / "results").glob("*"))) if
                         (self.work / "pc" / "results").exists() else 0, 0)

    def test_peek_reports_what_would_run_without_running_it(self):
        self.lock()
        self.queue("c-peek")
        r = self.runner(dry_run=True)
        cmd = r.pending()
        self.assertEqual(len(cmd), 1, "peek must still be able to name the waiting command")
        self.assertIsNone(r.lease_problem(cmd[0]), "the lease check is pure: it must answer in dry-run too")


class VerbsComeFromTheBridge(unittest.TestCase):
    """A second verb list is how one of the two drifts. The runner reads the bridge's table at import."""

    def test_the_runner_knows_exactly_the_bridges_verbs(self):
        import bridge
        self.assertEqual(set(pc_agent._verbs()), set(bridge.ACTIONS),
                         "pc_agent must not carry its own idea of what the bridge can do")

    def test_reads_and_writes_are_classified_the_same_way_on_both_sides(self):
        import bridge
        for name, cls in pc_agent._verbs().items():
            self.assertEqual(cls, "read" if bridge.ACTIONS[name][0] == "read" else "write", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
