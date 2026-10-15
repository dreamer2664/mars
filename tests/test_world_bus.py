#!/usr/bin/env python3
"""tests/test_world_bus.py — M-BUS (t-20261015-2296): the mirror and the gate.

The bus integration runs through a STUB chatcode checkout — a bin/chatcode that
appends a message and prints an id exactly like the real one, inside a real (tiny)
git repo so the commit leg works — so the tests exercise the real subprocess path
(propose/respond -> say -> id capture -> mirror) without touching the network.
The real-bus exchange is pasted in the PR body.
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import world  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STUB_SAY_ID = "deadbeef"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = world.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def bus_env(bus=None, token="stub-token-for-tests"):
    """--bus wiring without leaking env between tests."""
    old = {k: os.environ.get(k) for k in ("MARS_BUS", "CHATCODE_TOKEN")}
    os.environ.pop("MARS_BUS", None)
    if bus is not None:
        os.environ["MARS_BUS"] = str(bus)
    if token is not None:
        os.environ["CHATCODE_TOKEN"] = token
    else:
        os.environ.pop("CHATCODE_TOKEN", None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_stub_bus(root: Path) -> Path:
    """A chatcode checkout good enough for world.py: `say` prints an id, `git push`
    succeeds; it sits in a real (freshly init-ed) git repo so add/commit work."""
    (root / "bin").mkdir(parents=True)
    (root / "bus").mkdir()
    (root / "bus" / "general.jsonl").write_text("")
    script = root / "bin" / "chatcode"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'say':\n"
        "    print(f'\\u2713 #%s \\u2192 bus/general.jsonl (1 messages in channel)'\n"
        "          % '" + STUB_SAY_ID + "')\n"
        "    sys.exit(0)\n"
        "if args and args[0] == 'git':\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   env={k: v for k, v in os.environ.items() if k != "CHATCODE_TOKEN"})
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=stub",
                    "-c", "user.email=stub@test", "commit", "-q", "-m", "stub bus"],
                   check=True)
    return root


class BusTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mars-bus-"))
        (self.dir / "world").mkdir()
        (self.dir / "world" / "lock.json").write_bytes(
            (ROOT / "world" / "lock.json").read_bytes())
        (self.dir / "world" / "contract.json").write_bytes(
            (ROOT / "world" / "contract.json").read_bytes())
        run(["--no-push", "--root", str(self.dir), "join", "--name", "cormac"])
        run(["--no-push", "--root", str(self.dir), "join", "--name", "scipio"])
        run(["--no-push", "--root", str(self.dir), "join", "--name", "vesta"])
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video", "new",
                            "--title", "gate test", "--channel", "ch-cormac",
                            "--by", "cormac", "--evidence", "thread th-seed"])
        self.assertEqual(code, 0, out)
        self.vid = out.split()[1]
        self.bus = make_stub_bus(self.dir / "chatcode")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- helpers -------------------------------------------------------
    def propose(self, require=None, refs="", bus=None):
        argv = ["--no-push", "--root", str(self.dir), "propose", "--from", "cormac",
                "--subject", "needs review", "--text", "please look"]
        if refs:
            argv += ["--refs", refs]
        if require:
            argv += ["--require", require]
        with bus_env(bus):
            code, out, err = run(argv)
        self.assertEqual(code, 0, err)
        return self.last_thread_id(), out

    def respond(self, tid, frm, mtype, text, bus=None):
        with bus_env(bus):
            return run(["--no-push", "--root", str(self.dir), "respond",
                        "--thread", tid, "--from", frm, "--type", mtype,
                        "--text", text])

    def close(self, tid):
        return run(["--no-push", "--root", str(self.dir), "close-thread",
                    "--thread", tid, "--by", "cormac"])

    def last_thread_id(self):
        files = sorted((self.dir / "world" / "threads").glob("th-*.json"))
        return files[-1].stem

    def thread(self, tid):
        return json.loads((self.dir / "world" / "threads" / f"{tid}.json").read_text())


class TestMirror(BusTestBase):
    def test_propose_without_bus_is_a_draft_and_says_so(self):
        code, out, err = run(["--no-push", "--root", str(self.dir), "propose",
                              "--from", "cormac", "--subject", "s", "--text", "t"])
        self.assertEqual(code, 0, err)
        self.assertIn("NOT mirrored", err)
        tid = self.last_thread_id()
        self.assertIsNone(self.thread(tid)["messages"][0]["bus_id"])

    def test_local_only_response_does_not_satisfy_requirements(self):
        tid, _ = self.propose(require="scipio:evidence", refs=f"video={self.vid}")
        code, out, err = self.respond(tid, "scipio", "evidence", "local only")
        self.assertEqual(code, 0, out)
        self.assertIn("NOT mirrored", err)
        t = self.thread(tid)
        self.assertIsNone(t["messages"][-1]["bus_id"])
        self.assertEqual(world.World._unmet_required(t), ["scipio:evidence"])
        # and the gate holds: the video cannot advance
        code, out, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                              self.vid, "--to", "2", "--by", "cormac", "--evidence", "x"])
        self.assertEqual(code, 4)
        self.assertIn("still requires: scipio:evidence", err)

    def test_mirrored_propose_and_respond_carry_the_bus_id(self):
        tid, out = self.propose(require="scipio:evidence", bus=self.bus)
        self.assertIn(f"#{STUB_SAY_ID}", out)
        self.assertEqual(self.thread(tid)["messages"][0]["bus_id"], STUB_SAY_ID)
        code, out, err = self.respond(tid, "scipio", "evidence", "re-ran: 48/48",
                                      bus=self.bus)
        self.assertEqual(code, 0, err)
        self.assertIn(f"#{STUB_SAY_ID}", out)
        self.assertEqual(self.thread(tid)["messages"][-1]["bus_id"], STUB_SAY_ID)

    def test_require_validates_types_and_agents(self):
        code, out, err = run(["--no-push", "--root", str(self.dir), "propose",
                              "--from", "cormac", "--subject", "s", "--text", "t",
                              "--require", "scipio:not-a-type"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("charter types", err)
        code, out, err = run(["--no-push", "--root", str(self.dir), "propose",
                              "--from", "cormac", "--subject", "s", "--text", "t",
                              "--require", "stranger:evidence"])
        self.assertEqual(code, 4, out + err)
        self.assertIn("not a colonist", err)

    def test_bus_without_token_refuses_and_writes_nothing(self):
        argv = ["--no-push", "--root", str(self.dir), "--bus", str(self.bus),
                "propose", "--from", "cormac", "--subject", "s", "--text", "t"]
        with bus_env(bus=None, token=None):
            code, out, err = run(argv)
        self.assertEqual(code, 1, out + err)
        self.assertIn("CHATCODE_TOKEN", err)
        self.assertEqual(list((self.dir / "world" / "threads").glob("th-*.json")), [])


class TestGate(BusTestBase):
    def test_close_thread_refused_until_required_response_lands(self):
        tid, _ = self.propose(require="scipio:evidence", bus=self.bus)
        code, out, err = self.close(tid)
        self.assertEqual(code, 4)
        self.assertIn("still requires: scipio:evidence", err)
        self.assertIn("counts only when mirrored", err)
        # the response lands, mirrored with a bus id
        code, out, err = self.respond(tid, "scipio", "evidence", "re-ran the check",
                                      bus=self.bus)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.thread(tid)["messages"][-1]["bus_id"], STUB_SAY_ID)
        # now it closes
        code, out, err = self.close(tid)
        self.assertEqual(code, 0, out + err)
        t = self.thread(tid)
        self.assertEqual(t["status"], "resolved")
        self.assertEqual(t["resolved_by"], "cormac")
        self.assertIn("RESOLVED", out)

    def test_wrong_agent_or_wrong_type_does_not_satisfy(self):
        tid, _ = self.propose(require="scipio:evidence", bus=self.bus)
        self.respond(tid, "vesta", "evidence", "i am not scipio", bus=self.bus)
        self.respond(tid, "scipio", "approval", "wrong type", bus=self.bus)
        code, out, err = self.close(tid)
        self.assertEqual(code, 4)
        self.assertIn("still requires: scipio:evidence", err)
        self.respond(tid, "scipio", "evidence", "here it is", bus=self.bus)
        code, out, _ = self.close(tid)
        self.assertEqual(code, 0, out)

    def test_any_of_the_listed_types_satisfies(self):
        tid, _ = self.propose(require="scipio:evidence|approval", bus=self.bus)
        self.respond(tid, "scipio", "approval", "either way", bus=self.bus)
        code, out, _ = self.close(tid)
        self.assertEqual(code, 0, out)

    def test_task_blocked_refuses_then_clears(self):
        self.propose(require="scipio:evidence", refs="task=t-2296-dep", bus=self.bus)
        code, out, err = run(["--no-push", "--root", str(self.dir), "task-blocked",
                              "t-2296-dep"])
        self.assertEqual(code, 4)
        self.assertIn("BLOCKED by th-", out + err)
        self.assertIn("still requires scipio:evidence", out + err)
        code, out, _ = run(["--no-push", "--root", str(self.dir), "task-blocked",
                            "t-unrelated"])
        self.assertEqual(code, 0)
        self.assertIn("CLEAR", out)

    def test_task_blocked_clears_once_the_response_lands(self):
        tid, _ = self.propose(require="scipio:evidence", refs="task=t-2296-dep",
                              bus=self.bus)
        self.respond(tid, "scipio", "evidence", "here", bus=self.bus)
        code, out, _ = run(["--no-push", "--root", str(self.dir), "task-blocked",
                            "t-2296-dep"])
        self.assertEqual(code, 0, out)
        self.assertIn("CLEAR", out)

    def test_video_advance_gated_then_ungated(self):
        self.propose(require="scipio:evidence", refs=f"video={self.vid}", bus=self.bus)
        code, out, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                              self.vid, "--to", "2", "--by", "cormac",
                              "--evidence", "review done"])
        self.assertEqual(code, 4)
        self.assertIn(f"video {self.vid} is gated by thread th-", err)
        self.assertIn("still requires: scipio:evidence", err)
        tid = self.last_thread_id()
        self.respond(tid, "scipio", "evidence", "checked", bus=self.bus)
        code, out, err = self.close(tid)
        self.assertEqual(code, 0, out + err)
        code, out, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                              self.vid, "--to", "2", "--by", "cormac",
                              "--evidence", "review done"])
        self.assertEqual(code, 0, out + err)

    def test_resolved_thread_no_longer_gates(self):
        tid, _ = self.propose(require="scipio:evidence", refs=f"video={self.vid}",
                              bus=self.bus)
        self.respond(tid, "scipio", "evidence", "ok", bus=self.bus)
        self.close(tid)
        code, out, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                              self.vid, "--to", "2", "--by", "cormac",
                              "--evidence", "review done"])
        self.assertEqual(code, 0, out + err)

    def test_close_thread_refuses_unknown_or_already_resolved(self):
        code, out, err = run(["--no-push", "--root", str(self.dir), "close-thread",
                              "--thread", "th-nope", "--by", "cormac"])
        self.assertEqual(code, 4)
        tid, _ = self.propose(require="scipio:evidence", bus=self.bus)
        self.respond(tid, "scipio", "evidence", "ok", bus=self.bus)
        self.close(tid)
        code, out, err = self.close(tid)
        self.assertEqual(code, 4)
        self.assertIn("already resolved", err)


if __name__ == "__main__":
    unittest.main()
