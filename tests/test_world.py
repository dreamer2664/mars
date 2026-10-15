#!/usr/bin/env python3
"""tests/test_world.py — M-STATE evidence (t-20261015-a8ee).

    python3 -m unittest discover -s tests -v

Runs against a scratch world root, no git, no network: membership, threads,
meetings, archive, and the canonical byte form.
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import world  # noqa: E402

CHARTER_SENTINEL = ROOT / "CHARTER.md"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = world.main(argv)
    return code, out.getvalue(), err.getvalue()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mars-test-"))
        (self.dir / "world").mkdir()
        shutil.copy(ROOT / "world" / "lock.json", self.dir / "world" / "lock.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def w(self):
        return world.World(self.dir, push=False)

    def here(self, *a):
        return run(["--root", str(self.dir), "--no-push", *a])


class Membership(Scratch):
    def test_join_creates_agent_and_channel(self):
        code, out, _ = self.here("join", "--name", "nova", "--caps", "research,script")
        self.assertEqual(code, 0)
        self.assertIn("WELCOME nova", out)
        a = json.loads((self.dir / "world/agents/nova.json").read_text())
        c = json.loads((self.dir / "world/channels/ch-nova.json").read_text())
        self.assertEqual(a["id"], "nova")
        self.assertEqual(a["location"], "entrance")
        self.assertEqual(a["caps"], ["research", "script"])
        self.assertEqual(a["schema"], 1)
        self.assertEqual(c["owner"], "nova")
        self.assertEqual(c["status"], "planned")

    def test_join_refuses_a_claimed_name(self):
        self.here("join", "--name", "nova")
        code, _, err = self.here("join", "--name", "nova")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("already claimed", err)

    def test_join_refuses_bad_names(self):
        for bad in ("Nova", "n", "no va", "9nova", "x" * 30, ""):
            code, _, err = self.here("join", "--name", bad)
            self.assertNotEqual(code, 0, bad)
            self.assertIn("name must be", err)

    def test_move_and_where(self):
        self.here("join", "--name", "nova")
        code, out, _ = self.here("move", "nova", "--to", "command")
        self.assertEqual(code, 0)
        self.assertIn("command", out)
        _, out2, _ = self.here("where")
        self.assertIn("nova", out2)
        self.assertIn("command", out2)

    def test_move_refuses_unknown_agent_and_location(self):
        code, _, err = self.here("move", "ghost", "--to", "command")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("no colonist", err)
        self.here("join", "--name", "nova")
        code, _, err = self.here("move", "nova", "--to", "the-moon")
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("unknown location", err)


class Threads(Scratch):
    def test_propose_and_respond(self):
        self.here("join", "--name", "nova")
        self.here("join", "--name", "atlas")
        code, out, _ = self.here("propose", "--from", "nova", "--subject", "first light",
                                 "--text", "proposal: run the first slice")
        self.assertEqual(code, 0)
        tid = out.split()[1]
        code, _, _ = self.here("respond", "--thread", tid, "--from", "atlas",
                               "--type", "disagreement", "--text", "the evidence is thin")
        self.assertEqual(code, 0)
        t = json.loads((self.dir / f"world/threads/{tid}.json").read_text())
        self.assertEqual(len(t["messages"]), 2)
        self.assertEqual(t["messages"][1]["type"], "disagreement")
        self.assertEqual(t["status"], "open")

    def test_propose_refuses_unregistered_author(self):
        code, _, err = self.here("propose", "--from", "ghost", "--subject", "s", "--text", "t")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("not registered", err)

    def test_respond_refuses_bad_type_and_missing_thread(self):
        self.here("join", "--name", "nova")
        _, out, _ = self.here("propose", "--from", "nova", "--subject", "s", "--text", "t")
        tid = out.split()[1]
        code, _, err = self.here("respond", "--thread", tid, "--from", "nova",
                                 "--type", "gossip", "--text", "x")
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("type must be", err)
        code, _, err = self.here("respond", "--thread", "th-20260101-ffff", "--from", "nova",
                                 "--type", "question", "--text", "x")
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("no thread", err)

    def test_decide_and_archive(self):
        self.here("join", "--name", "nova")
        code, out, _ = self.here("decide", "--by", "nova", "--subject", "publish it",
                                 "--outcome", "approved as directive d-123")
        self.assertEqual(code, 0)
        mid = out.split()[1]
        m = json.loads((self.dir / f"world/meetings/{mid}.json").read_text())
        self.assertEqual(m["status"], "closed")
        self.assertIn("directive", m["outcome"])
        code, out, _ = self.here("archive", "add", "--title", "glyph notes", "--by", "nova",
                                 "--tags", "ocr,research")
        self.assertEqual(code, 0)
        aid = out.split()[1]
        it = json.loads((self.dir / f"world/archive/{aid}.json").read_text())
        self.assertEqual(it["tags"], ["ocr", "research"])


class Canon(unittest.TestCase):
    def test_canonical_bytes_are_stable_and_sorted(self):
        obj = {"b": 1, "a": [2, 3], "c": {"z": None, "y": "x"}}
        first = world.canonical(obj)
        second = world.canonical(json.loads(first.decode()))
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertLess(first.index(b'"a"'), first.index(b'"b"'))

    def test_the_world_files_are_canonical(self):
        """Anything world.py wrote must already be in canonical form."""
        for f in (ROOT / "world").rglob("*.json"):
            raw = f.read_bytes()
            self.assertEqual(raw, world.canonical(json.loads(raw)),
                             f"{f.relative_to(ROOT)} is not canonical — run it through world.py")

    def test_status_smoke(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = world.main(["--root", str(ROOT), "--no-push", "status"])
        self.assertEqual(code, 0)
        self.assertIn("PC", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
