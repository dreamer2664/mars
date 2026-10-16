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
    def lock(self, holder="fabius", generation=3, token="tok-abc", expires_min=30, state="held"):
        # "held" is the world's word (world.py: free | held | recovering), and it must be the DEFAULT here.
        # This fixture originally said "active" — the same invented word the runner tested for — so 17 tests
        # passed against a state the real lock never takes, and every real command would have been refused.
        # A fixture that agrees with the code instead of with the world is a mirror, not a test.
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

    def test_a_free_lock_is_no_lock(self):
        """world.py reports a released lease as `free` with holder=None. If this ever stops being refused,
        a released PC could still be driven by whatever was sitting in the queue."""
        self.lock(state="free", holder=None)
        self.queue("c-free")
        rec = self.runner().once()["record"]
        self.assertEqual(rec["reason"], "no-lock")
        self.assertIn("free", rec["detail"])

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


class Queueing(Sandbox):
    """`queue` is how a command gets made without hand-writing JSON and a token. It must refuse to make one
    that nobody can run — a command queued against no lease sits in the repo looking like work and gets
    inherited by whoever acquires next."""

    def test_a_queued_command_carries_no_token_and_the_runner_supplies_the_live_one(self):
        self.lock(token="tok-live", generation=4)
        cmd = self.runner(fence="static").enqueue("capture", {"window": "id-0"}, "PCB-02", push=False)
        self.assertNotIn("token", cmd, "the token is published in world/lock.json anyway: keeping it out of "
                                       "the command is honest, pretending it is a secret is not")
        self.assertEqual(cmd["generation"], 4)
        on_disk = json.loads((self.work / "pc" / "queue" / f"{cmd['id']}.json").read_text())
        self.assertNotIn("token", on_disk)
        # and the runner executes it by injecting the live token
        out = self.runner(fence="static", backend="sim").once()
        self.assertEqual(out["turn"], "executed")
        self.assertEqual(out["record"]["status"], "done", out["record"])

    def test_a_command_with_the_wrong_token_is_still_refused(self):
        """Optional token must not mean 'no check': a command that names one is held to it."""
        self.lock(token="tok-live", generation=4)
        self.queue("c-lying", token="tok-other", generation=4)
        rec = self.runner(fence="static").once()["record"]
        self.assertEqual(rec["reason"], "wrong-token")

    def test_queueing_without_a_lease_is_refused_before_it_is_written(self):
        from bridge import Refused
        r = self.runner(fence="static")
        with self.assertRaises(Refused) as e:
            r.enqueue("capture", {"window": "id-0"}, "PCB-02", push=False)
        self.assertEqual(e.exception.reason, "no-lock")
        self.assertFalse((self.work / "pc" / "queue").exists() and
                         list((self.work / "pc" / "queue").glob("c-*")), "it wrote the file anyway")

    def test_queueing_validates_the_verb_and_its_fields(self):
        from bridge import Refused
        self.lock()
        r = self.runner(fence="static")
        with self.assertRaises(Refused) as e:
            r.enqueue("teleport", {}, "PCB-02", push=False)
        self.assertEqual(e.exception.reason, "not-allowlisted")
        with self.assertRaises(Refused) as e2:                 # a real verb, a missing required field
            r.enqueue("set_text", {"element": "id-0-0"}, "PCB-02", push=False)
        self.assertEqual(e2.exception.reason, "bad-args")
        self.assertIn("text", str(e2.exception.hint))
        with self.assertRaises(Refused) as e3:                 # a typo must not ride along to the OS
            r.enqueue("capture", {"window": "id-0", "windwo": "id-1"}, "PCB-02", push=False)
        self.assertEqual(e3.exception.reason, "bad-args")

    def test_a_queued_command_survives_the_round_trip_through_git(self):
        """The whole point of the channel: what the agent writes is what the PC pulls."""
        self.lock(token="tok-live", generation=4)
        cmd = self.runner(fence="static").enqueue("windows", {}, "PCB-01", push=False)
        git("add", "-A", cwd=self.work); git("commit", "-q", "-m", "q", cwd=self.work); git("push", "-q", cwd=self.work)
        other = self.tmp / "other"
        git("clone", "-q", str(self.origin), str(other), cwd=self.tmp)
        pulled = json.loads((other / "pc" / "queue" / f"{cmd['id']}.json").read_text())
        self.assertEqual(pulled["verb"], "windows")
        self.assertEqual(pulled["id"], cmd["id"])
        self.assertNotIn("token", pulled)


class AgainstTheRealWorld(Sandbox):
    """The check that would have caught the vocabulary bug: drive `world.py` itself, in a temp repo, and
    assert the runner accepts what the world actually produces.

    `Sandbox.lock()` writes a fixture. A fixture is written by the same hand as the code that reads it, so
    when both used the word "active" — a state `world.py` has never heard of — 17 tests passed and every
    real command would have been refused. This class exists so that at least one test cannot agree with me.
    """

    def _world(self, *args):
        return subprocess.run([sys.executable, str(self.work / "tools" / "world.py"), *args],
                              cwd=str(self.work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_a_lease_from_the_real_world_is_one_the_runner_accepts(self):
        p = self._world("pc", "acquire", "--by", "fabius", "--purpose", "against the real world", "--ttl", "30")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("GRANTED", p.stdout)
        lock = json.loads((self.work / "world" / "lock.json").read_text())
        token, gen = lock["token"], lock["generation"]
        self.assertTrue(token and gen, f"the world granted no grip: {lock}")

        self.queue("c-real", token=token, generation=gen)
        r = self.runner(fence="static")            # static-with-live-lock: no served referee needed in CI
        cmd = r.pending()[0]
        self.assertIsNone(r.lease_problem(cmd),
                         "the runner refused a lease that world.py had just granted — the states disagree")

        # and the fence the bridge itself uses must agree, in the exact form it invokes it
        chk = self._world("--no-push", "pc", "check-token", "--token", token, "--generation", str(gen))
        self.assertEqual(chk.returncode, 0, chk.stdout)
        self.assertIn("TOKEN-OK", chk.stdout)

    def test_the_runners_idea_of_a_live_lease_matches_the_worlds_after_a_release(self):
        p = self._world("pc", "acquire", "--by", "fabius", "--purpose", "release path", "--ttl", "30")
        lock = json.loads((self.work / "world" / "lock.json").read_text())
        self.assertEqual(lock["state"], "held", "world.py changed its vocabulary: update the runner, not this test")
        rel = self._world("pc", "release", "--by", "fabius", "--token", lock["token"])
        self.assertEqual(rel.returncode, 0, rel.stdout)
        after = json.loads((self.work / "world" / "lock.json").read_text())
        self.assertEqual(after["state"], "free")
        self.queue("c-after", token=lock["token"], generation=lock["generation"])
        r = self.runner(fence="static")
        self.assertEqual(r.lease_problem(r.pending()[0]).split(":")[0], "no-lock",
                         "a released lease must refuse, naming the state it actually found")


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
