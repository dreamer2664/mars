#!/usr/bin/env python3
"""tests/test_world_lock.py — M-LOCK evidence (t-20261015-24a9).

Two halves:

A. SEMANTICS (no git): acquire / renew / release / expiry→recovering / sweep /
   cancel / fencing (stale token+generation refused).

B. THE RACE (local bare repo, no network): five colonists acquire the PC AT THE
   SAME INSTANT from five separate clones of the same origin. Exactly one may
   win; four must land in the queue. The push race is the mutex.
"""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import contextlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "world.py"
sys.path.insert(0, str(ROOT / "tools"))
import world  # noqa: E402


def run(root, *argv, push=False):
    out, err = io.StringIO(), io.StringIO()
    args = ["--root", str(root)] + ([] if push else ["--no-push"]) + list(argv)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = world.main(args)
    return code, out.getvalue(), err.getvalue()


class LockSemantics(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mars-lock-"))
        (self.dir / "world").mkdir()
        shutil.copy(ROOT / "world" / "lock.json", self.dir / "world" / "lock.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lock(self):
        return json.loads((self.dir / "world/lock.json").read_text())

    def test_acquire_grant_and_fencing(self):
        code, out, _ = run(self.dir, "pc", "acquire", "--by", "nova", "--purpose", "first")
        self.assertEqual(code, 0)
        self.assertIn("GRANTED generation=1", out)
        token = out.split("token=")[1].split()[0]
        l = self.lock()
        self.assertEqual(l["state"], "held")
        self.assertEqual(l["holder"], "nova")
        self.assertEqual(l["generation"], 1)

        code, out, _ = run(self.dir, "pc", "check-token", "--token", token, "--generation", "1")
        self.assertEqual(code, 0)
        self.assertIn("TOKEN-OK", out)

        code, out, _ = run(self.dir, "pc", "check-token", "--token", token, "--generation", "0")
        self.assertEqual(code, world.EXIT_STALE)
        self.assertIn("stale", out)

        code, out, _ = run(self.dir, "pc", "check-token", "--token", "deadbeef", "--generation", "1")
        self.assertEqual(code, world.EXIT_STALE)

    def test_queue_fifo_and_release_grants_next(self):
        _, out1, _ = run(self.dir, "pc", "acquire", "--by", "a", "--purpose", "x")
        tok_a = out1.split("token=")[1].split()[0]
        code, out2, _ = run(self.dir, "pc", "acquire", "--by", "b", "--purpose", "y")
        self.assertEqual(out2.strip(), "QUEUED position=1")
        code, out3, _ = run(self.dir, "pc", "acquire", "--by", "c", "--purpose", "z")
        self.assertEqual(out3.strip(), "QUEUED position=2")

        code, out4, _ = run(self.dir, "pc", "release", "--by", "a", "--token", tok_a)
        self.assertEqual(code, 0)
        l = self.lock()
        self.assertEqual(l["holder"], "b")
        self.assertEqual(l["generation"], 2)
        self.assertNotEqual(l["token"], tok_a)
        self.assertEqual([e["agent"] for e in l["queue"]], ["c"])

        # a's token is now stale — the fence holds after handover
        code, out5, _ = run(self.dir, "pc", "check-token", "--token", tok_a, "--generation", "2")
        self.assertEqual(code, world.EXIT_STALE)

        code, out6, _ = run(self.dir, "pc", "release", "--by", "b",
                            "--token", self.lock()["token"])
        self.assertEqual(code, 0)
        l = self.lock()
        self.assertEqual(l["holder"], "c")
        self.assertEqual(l["generation"], 3)
        self.assertEqual(l["queue"], [])

    def test_renew_and_wrong_holder_refused(self):
        _, out, _ = run(self.dir, "pc", "acquire", "--by", "a", "--purpose", "x")
        tok = out.split("token=")[1].split()[0]
        code, out2, _ = run(self.dir, "pc", "renew", "--by", "a", "--token", tok)
        self.assertEqual(code, 0)
        self.assertIn("RENEWED", out2)
        self.assertEqual(self.lock()["renew_count"], 1)
        code, _, err = run(self.dir, "pc", "renew", "--by", "b", "--token", tok)
        self.assertEqual(code, world.EXIT_STALE)
        self.assertIn("not the current holder", err)

    def test_expiry_becomes_recovering_never_silent_handover(self):
        _, out, _ = run(self.dir, "pc", "acquire", "--by", "a", "--purpose", "x", "--ttl", "1")
        l = self.lock()
        l["expires_ts"] = world.z(world.now() - dt.timedelta(minutes=5))
        (self.dir / "world/lock.json").write_bytes(world.canonical(l))

        code, out2, err = run(self.dir, "pc", "acquire", "--by", "b", "--purpose", "y")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("recovering", err)

        code, out3, _ = run(self.dir, "pc", "sweep", "--by", "b")
        self.assertEqual(code, 0)
        self.assertIn("state=free", out3)

        code, out4, _ = run(self.dir, "pc", "acquire", "--by", "b", "--purpose", "y")
        self.assertEqual(code, 0)
        self.assertIn("GRANTED generation=2", out4)

    def test_cancel_leaves_the_queue(self):
        run(self.dir, "pc", "acquire", "--by", "a", "--purpose", "x")
        run(self.dir, "pc", "acquire", "--by", "b", "--purpose", "y")
        code, out, _ = run(self.dir, "pc", "cancel", "--by", "b")
        self.assertEqual(code, 0)
        self.assertEqual(self.lock()["queue"], [])
        code, _, err = run(self.dir, "pc", "cancel", "--by", "b")
        self.assertEqual(code, world.EXIT_REFUSED)


class TheRace(unittest.TestCase):
    """Five simultaneous acquires from five clones of one origin: one winner."""

    AGENTS = ["a", "b", "c", "d", "e"]

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mars-race-"))
        seed = cls.tmp / "seed"
        seed.mkdir()
        (seed / "world").mkdir()
        shutil.copy(ROOT / "world" / "lock.json", seed / "world" / "lock.json")
        g = lambda *a, cwd=seed: subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], capture_output=True)
        g("add", "-A"); g("-c", "user.name=seed", "-c", "user.email=seed@mars.local",
                          "commit", "-q", "-m", "seed")
        cls.origin = cls.tmp / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(cls.origin)],
                       capture_output=True)
        cls.clones = {}
        for a in cls.AGENTS:
            cl = cls.tmp / f"clone-{a}"
            subprocess.run(["git", "clone", "-q", str(cls.origin), str(cl)], capture_output=True)
            cls.clones[a] = cl

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def origin_lock(self):
        r = subprocess.run(["git", "-C", str(self.origin), "show", "main:world/lock.json"],
                           capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_five_parallel_acquires_one_winner_four_queued(self):
        procs = []
        for a in self.AGENTS:
            p = subprocess.Popen(
                [sys.executable, str(TOOL), "--root", str(self.clones[a]), "--agent", a,
                 "pc", "acquire", "--by", a, "--purpose", f"race {a}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            procs.append((a, p))
        results = {}
        for a, p in procs:
            out, err = p.communicate(timeout=120)
            results[a] = (p.returncode, out, err)

        granted = [a for a, (c, o, e) in results.items() if "GRANTED" in o]
        queued = [a for a, (c, o, e) in results.items() if "QUEUED" in o]
        self.assertEqual(len(granted), 1,
                         f"exactly one winner expected — got {granted}. details: {results}")
        self.assertEqual(sorted(queued), sorted(set(self.AGENTS) - set(granted)))

        lock = self.origin_lock()
        self.assertEqual(lock["generation"], 1, "one grant = exactly one generation bump")
        self.assertEqual(lock["state"], "held")
        self.assertEqual(lock["holder"], granted[0])
        self.assertEqual(len(lock["queue"]), 4)
        self.assertEqual({e["agent"] for e in lock["queue"]}, set(self.AGENTS) - set(granted))

    def test_winner_releases_and_queue_advances_with_a_new_generation(self):
        lock = self.origin_lock()
        winner = lock["holder"]
        token = lock["token"]
        clone = self.clones[winner]
        p = subprocess.run([sys.executable, str(TOOL), "--root", str(clone), "--agent", winner,
                            "pc", "release", "--by", winner, "--token", token],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        lock2 = self.origin_lock()
        self.assertEqual(lock2["generation"], 2)
        self.assertEqual(lock2["holder"], "b" if lock["queue"][0]["agent"] == "b" else lock["queue"][0]["agent"])
        self.assertNotEqual(lock2["token"], token)
        self.assertEqual(len(lock2["queue"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
