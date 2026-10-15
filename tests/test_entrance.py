#!/usr/bin/env python3
"""tests/test_entrance.py — M-ENTRANCE evidence (t-20261015-3941).

Runs against scratch world roots, no git, no network, no token:
  * the full sitting prints (charter digest, 11 types, studio, caps, permissions,
    manager, projects, open cards)
  * claimed-name refusal
  * the entrance bus post seam (stubbed CLI; success + failure, never the real wire)
  * the PC gate: fresh colonist refused, ghost refused in a populated colony,
    open in an empty one; the authorize path (manager grants, others may not)
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import world  # noqa: E402

OK_STUB = ("#!/usr/bin/env python3\nimport sys, pathlib\n"
           "pathlib.Path(sys.argv[0] + '.argv.txt').write_text('\\n'.join(sys.argv[1:]))\n"
           "print('✓ #abcd1234 → bus/general.jsonl (301 messages in channel)')\nsys.exit(0)\n")
FAIL_STUB = "#!/usr/bin/env python3\nimport sys\nprint('error: rate limit hit', file=sys.stderr)\nsys.exit(1)\n"


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    old = {k: os.environ.get(k) for k in ("MARS_CHATCODE", "MARS_CHATCODE_BIN", "CHATCODE_TOKEN")}
    if env:
        os.environ.update(env)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = world.main(argv)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue(), err.getvalue()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mars-entrance-"))
        (self.dir / "world").mkdir()
        shutil.copy(ROOT / "world" / "lock.json", self.dir / "world" / "lock.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def here(self, *a, env=None):
        return run(["--root", str(self.dir), "--no-push", *a], env=env)

    def join(self, name="nova", caps=None, env=None):
        argv = ["join", "--name", name]
        if caps is not None:
            argv += ["--caps", caps]
        return self.here(*argv, env=env)

    def agent(self, name):
        return json.loads((self.dir / f"world/agents/{name}.json").read_text())


class Sitting(Scratch):
    def test_join_prints_the_whole_sitting(self):
        code, out, err = self.join(caps="research,script")
        self.assertEqual(code, 0, err)
        # registration + files
        self.assertIn("WELCOME nova", out)
        a, c = self.agent("nova"), json.loads((self.dir / "world/channels/ch-nova.json").read_text())
        self.assertEqual(a["id"], "nova")
        self.assertEqual(a["caps"], ["research", "script"])
        self.assertEqual(c["owner"], "nova")
        # charter digest (five lines) + the 11 types, verbatim
        self.assertIn("Charter, in five lines:", out)
        for t in world.MSG_TYPES:
            self.assertIn(t, out)
        self.assertEqual(len(world.MSG_TYPES), 11)
        # studio slot assigned and named
        self.assertIn("Studio: studio:nova bound to channel ch-nova", out)
        # capabilities + permissions with the PC NOT-included line
        self.assertIn("Registered capabilities: research, script.", out)
        self.assertIn("PC access is NOT included by default", out)
        # manager + projects + open cards lines exist even degraded
        self.assertIn("Manager:", out)
        self.assertIn("Active projects:", out)
        self.assertIn("Open mars cards:", out)
        # offline skips the post with a reason, never fails the join
        self.assertIn("entrance message NOT posted", err)

    def test_join_refuses_a_claimed_name(self):
        self.join()
        code, _, err = self.join()
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("already claimed", err)


class BusSeam(Scratch):
    def _stub(self, body):
        bin_dir = Path(tempfile.mkdtemp(prefix="mars-stub-"))
        stub = bin_dir / "chatcode"
        stub.write_text(body)
        return str(stub)

    def test_entrance_message_posts_once_and_bus_id_prints(self):
        stub = self._stub(OK_STUB)
        env = {"MARS_CHATCODE_BIN": stub, "CHATCODE_TOKEN": "x"}
        code, out, err = self.join(env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("entrance message posted to the bus: id abcd1234", out)
        self.assertNotIn("NOT posted", err)
        # pin the exact CLI invocation: one say, legal chatcode type 'join'
        argv_sent = Path(stub + ".argv.txt").read_text().splitlines()
        self.assertEqual(argv_sent[0], "say")
        tpos = argv_sent.index("--type")
        self.assertEqual(argv_sent[tpos + 1], "join",
                         "chatcode say accepts only its enum types; 'entrance' is invalid")

    def test_post_failure_never_fails_the_join(self):
        env = {"MARS_CHATCODE_BIN": self._stub(FAIL_STUB), "CHATCODE_TOKEN": "x"}
        code, out, err = self.join(env=env)
        self.assertEqual(code, 0)
        self.assertIn("entrance message NOT posted", err)
        self.assertIn("rate limit", err)

    def test_no_token_no_post(self):
        env = {"MARS_CHATCODE_BIN": self._stub(OK_STUB), "CHATCODE_TOKEN": ""}
        code, out, err = self.join(env=env)
        self.assertEqual(code, 0)
        self.assertIn("CHATCODE_TOKEN not set", err)


class OpenCards(Scratch):
    def test_open_mars_cards_listed_from_sibling(self):
        cc = Path(tempfile.mkdtemp(prefix="mars-cc-")) / "board" / "tasks"
        cc.mkdir(parents=True)
        rows = [
            ("t-20261015-0001", "open", ["mars"], 2, "M-TEST one"),
            ("t-20261015-0002", "open", ["mars"], 3, "M-TEST two"),
            ("t-20261015-0003", "done", ["mars"], 2, "M-TEST closed"),
            ("t-20261015-0004", "open", ["chatcode"], 2, "not mars"),
        ]
        for tid, st, tags, pr, title in rows:
            (cc / f"{tid}.json").write_text(json.dumps(
                {"id": tid, "status": st, "tags": tags, "priority": pr, "title": title}))
        code, out, err = self.join(env={"MARS_CHATCODE": str(cc.parent.parent)})
        self.assertEqual(code, 0, err)
        self.assertIn("Open mars cards: 2 shown:", out)
        self.assertIn("t-20261015-0001 p2 M-TEST one", out)
        self.assertIn("t-20261015-0002 p3 M-TEST two", out)
        self.assertNotIn("closed", out)
        self.assertNotIn("not mars", out)


class PcGate(Scratch):
    def test_fresh_colonist_acquire_refused_with_authorize_hint(self):
        self.join(name="nova")
        code, _, err = self.here("pc", "acquire", "--by", "nova", "--purpose", "upload")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("not included by default", err)
        self.assertIn("world.py authorize nova --by <manager>", err)

    def test_ghost_refused_in_populated_colony(self):
        self.join(name="nova")
        code, _, err = self.here("pc", "acquire", "--by", "ghost", "--purpose", "x")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("not a colonist", err)

    def test_empty_world_acquire_open(self):
        code, out, _ = self.here("pc", "acquire", "--by", "anyone", "--purpose", "x")
        self.assertEqual(code, 0)
        self.assertIn("GRANTED", out)

    def test_authorize_path_end_to_end(self):
        self.join(name="nova")
        self.here("join", "--name", "boss", "--role", "manager")
        code, out, _ = self.here("authorize", "nova", "--by", "boss")
        self.assertEqual(code, 0)
        self.assertIn("AUTHORIZED nova", out)
        self.assertIn("pc", self.agent("nova")["caps"])
        code, out, _ = self.here("pc", "acquire", "--by", "nova", "--purpose", "upload")
        self.assertEqual(code, 0)
        self.assertIn("GRANTED", out)

    def test_authorize_refuses_non_manager_and_unknown_target(self):
        self.join(name="nova")
        self.join(name="atlas")
        code, _, err = self.here("authorize", "atlas", "--by", "nova")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("only a manager", err)
        self.here("join", "--name", "boss", "--role", "manager")
        code, _, err = self.here("authorize", "ghost", "--by", "boss")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("no colonist named", err)
        code, _, err = self.here("authorize", "nova", "--by", "ghost")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("not a colonist", err)


if __name__ == "__main__":
    unittest.main()
