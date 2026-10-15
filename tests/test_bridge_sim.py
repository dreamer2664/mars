#!/usr/bin/env python3
"""tests/test_bridge_sim.py — M-BRIDGE evidence (t-20261015-f9ac).

The full protocol, on Linux, with no Windows and no browser: acquire a real
lock with the real world CLI, then drive the bridge against it — correct
generation acts, stale generation is refused with its reason, wrong token is
refused, unknown actions are refused by name, and the fail-closed cases
(browser down, unexpected dialog) act on nothing. Every action writes exactly
one receipt, and world/lock.json is never written by the bridge.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "tools"))

from executors import SimulatorExecutor  # noqa: E402
from serve import Bridge  # noqa: E402

WORLD_PY = ROOT / "tools" / "world.py"


def world(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(WORLD_PY), *args],
                          cwd=cwd, capture_output=True, text=True)


class BridgeProtocol(unittest.TestCase):
    """Fencing, allowlist, receipts, fail-closed — against a real lock grant."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.world_dir = cls.tmp / "world"
        shutil.copytree(ROOT / "world", cls.world_dir)
        cls.lock = cls.world_dir / "lock.json"
        cls.receipts = cls.tmp / "receipts"
        # a real grant, via the real CLI (the only writer of the lock)
        r = world("pc", "acquire", "--by", "scipio", "--purpose", "test upload",
                  cwd=cls.tmp)
        assert r.returncode == 0, f"acquire failed: {r.stdout}{r.stderr}"
        lock = json.loads(cls.lock.read_text())
        cls.token = lock["token"]
        cls.gen = lock["generation"]
        cls.executor = SimulatorExecutor(channels=["ch-collective", "ch-cormac"])
        cls.bridge = Bridge(lock_path=cls.lock, receipts_dir=cls.receipts,
                            executor=cls.executor)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def lock_bytes(self) -> bytes:
        return self.lock.read_bytes()

    # -- fencing ---------------------------------------------------------
    def test_correct_generation_acts(self):
        before = self.lock_bytes()
        r = self.bridge.act({"token": self.token, "generation": self.gen,
                             "action": "upload_file",
                             "args": {"path": "vid.mp4", "channel": "ch-collective"}})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["url"])
        self.assertEqual(r["generation"], self.gen)
        self.assertEqual(self.lock_bytes(), before, "the bridge must never write the lock")

    def test_stale_generation_refused_with_reason(self):
        r = self.bridge.act({"token": self.token, "generation": self.gen - 1,
                             "action": "upload_file",
                             "args": {"path": "vid.mp4", "channel": "ch-collective"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "stale-generation")

    def test_wrong_token_refused_with_reason(self):
        r = self.bridge.act({"token": "not-the-token", "generation": self.gen,
                             "action": "upload_file",
                             "args": {"path": "vid.mp4", "channel": "ch-collective"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "wrong-token")

    def test_future_generation_also_refused(self):
        # fencing is equality, not "at least" — a caller cannot pre-empt a grant
        r = self.bridge.act({"token": self.token, "generation": self.gen + 1,
                             "action": "upload_file",
                             "args": {"path": "vid.mp4", "channel": "ch-collective"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "stale-generation")

    def test_no_lock_refused(self):
        free = json.loads(self.lock.read_text())
        free.update({"holder": None, "state": "free", "token": None})
        other = self.tmp / "free-lock.json"
        other.write_text(json.dumps(free))
        b = Bridge(lock_path=other, receipts_dir=self.receipts, executor=self.executor)
        r = b.act({"token": "x", "generation": 1, "action": "upload_file",
                   "args": {"path": "v.mp4", "channel": "ch-collective"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no-lock")

    # -- allowlist -------------------------------------------------------
    def test_unknown_action_refused_by_name(self):
        r = self.bridge.act({"token": self.token, "generation": self.gen,
                             "action": "delete_channel", "args": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not-allowlisted")

    def test_missing_args_refused(self):
        r = self.bridge.act({"token": self.token, "generation": self.gen,
                             "action": "upload_file", "args": {"path": "v.mp4"}})
        self.assertFalse(r["ok"])
        self.assertIn("missing-args", r["reason"])

    # -- fail closed -----------------------------------------------------
    def test_browser_unreachable_acts_on_nothing(self):
        self.executor.browser_up = False
        try:
            r = self.bridge.act({"token": self.token, "generation": self.gen,
                                 "action": "upload_file",
                                 "args": {"path": "v.mp4", "channel": "ch-collective"}})
        finally:
            self.executor.browser_up = True
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "browser-unreachable")
        self.assertTrue(r["receipt_path"], "a refusal still gets a receipt")

    def test_unexpected_dialog_acts_on_nothing(self):
        self.executor.dialog = "Sign in again"
        try:
            r = self.bridge.act({"token": self.token, "generation": self.gen,
                                 "action": "upload_file",
                                 "args": {"path": "v.mp4", "channel": "ch-collective"}})
        finally:
            self.executor.dialog = None
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "unexpected-dialog")

    def test_wrong_channel_chip_refused(self):
        r = self.bridge.act({"token": self.token, "generation": self.gen,
                             "action": "upload_file",
                             "args": {"path": "v.mp4", "channel": "ch-nobody"}})
        self.assertFalse(r["ok"])
        self.assertIn("unknown-channel", r["reason"])

    # -- receipts and the metadata/url pair ------------------------------
    def test_metadata_then_published_url(self):
        up = self.bridge.act({"token": self.token, "generation": self.gen,
                              "action": "upload_file",
                              "args": {"path": "v.mp4", "channel": "ch-cormac"}})
        vid = up["result"]["video_id"]
        meta = self.bridge.act({"token": self.token, "generation": self.gen,
                                "action": "set_metadata",
                                "args": {"video_id": vid, "title": "Outpost Mars — day 1"}})
        self.assertTrue(meta["ok"], meta)
        self.assertEqual(meta["result"]["set"]["title"], "Outpost Mars — day 1")
        url = self.bridge.act({"token": self.token, "generation": self.gen,
                               "action": "read_published_url", "args": {"video_id": vid}})
        self.assertTrue(url["ok"], url)
        self.assertEqual(url["result"]["url"], up["result"]["url"])

    def test_every_action_writes_exactly_one_receipt(self):
        before = len(list(self.receipts.glob("*.json")))
        self.bridge.act({"token": self.token, "generation": self.gen,
                         "action": "read_published_url", "args": {"video_id": "vid-nope"}})
        after = list(self.receipts.glob("*.json"))
        self.assertEqual(len(after), before + 1)
        rec = json.loads(sorted(after, key=lambda p: p.stat().st_mtime)[-1].read_text())
        for k in ("ts", "action", "args_digest", "generation", "ok", "reason"):
            self.assertIn(k, rec)

    def test_same_second_refusals_keep_separate_receipts(self):
        # two refused actions, same args, same second: neither may overwrite
        # the other — a refusal is evidence too
        before = len(list(self.receipts.glob("*.json")))
        args = {"path": "dupe.mp4", "channel": "ch-collective"}
        self.bridge.act({"token": "ghost", "generation": self.gen,
                         "action": "upload_file", "args": args})
        self.bridge.act({"token": "ghost2", "generation": self.gen,
                         "action": "upload_file", "args": args})
        after = len(list(self.receipts.glob("*.json")))
        self.assertEqual(after, before + 2)

    def test_bridge_never_writes_world_state(self):
        before = {p.name: p.read_bytes() for p in self.world_dir.rglob("*.json")}
        self.bridge.act({"token": self.token, "generation": self.gen,
                         "action": "upload_file",
                         "args": {"path": "v.mp4", "channel": "ch-collective"}})
        self.bridge.status()
        after = {p.name: p.read_bytes() for p in self.world_dir.rglob("*.json")}
        self.assertEqual(before, after, "one writer per fact — the bridge is not a writer")


class Lifecycle(unittest.TestCase):
    """acquire → act → release → the old grant is dead (the crash case)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.world_dir = self.tmp / "world"
        shutil.copytree(ROOT / "world", self.world_dir)
        self.lock = self.world_dir / "lock.json"
        self.receipts = self.tmp / "receipts"
        self.executor = SimulatorExecutor(channels=["ch-collective"])
        self.bridge = Bridge(lock_path=self.lock, receipts_dir=self.receipts,
                             executor=self.executor)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_release_kills_the_old_grant(self):
        r = world("pc", "acquire", "--by", "scipio", "--purpose", "upload", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        lock = json.loads(self.lock.read_text())
        token, gen = lock["token"], lock["generation"]

        ok = self.bridge.act({"token": token, "generation": gen, "action": "upload_file",
                              "args": {"path": "v.mp4", "channel": "ch-collective"}})
        self.assertTrue(ok["ok"], ok)

        r = world("pc", "release", "--by", "scipio", "--token", token, cwd=self.tmp)
        self.assertEqual(r.returncode == 0, True, r.stderr)

        dead = self.bridge.act({"token": token, "generation": gen, "action": "upload_file",
                                "args": {"path": "v2.mp4", "channel": "ch-collective"}})
        self.assertFalse(dead["ok"])
        self.assertIn(dead["reason"], ("no-lock", "stale-generation", "wrong-token"))

    def test_new_holder_gets_a_new_generation_and_the_old_token_is_dead(self):
        world("pc", "acquire", "--by", "scipio", "--purpose", "first", cwd=self.tmp)
        first = json.loads(self.lock.read_text())
        world("pc", "acquire", "--by", "vesta", "--purpose", "queued", cwd=self.tmp)
        world("pc", "release", "--by", "scipio", "--token", first["token"], cwd=self.tmp)
        second = json.loads(self.lock.read_text())

        self.assertEqual(second["holder"], "vesta")
        self.assertNotEqual(second["generation"], first["generation"])

        stale = self.bridge.act({"token": first["token"], "generation": first["generation"],
                                 "action": "upload_file",
                                 "args": {"path": "v.mp4", "channel": "ch-collective"}})
        self.assertFalse(stale["ok"], "a crashed controller must not keep acting")
        self.assertEqual(stale["reason"], "stale-generation")

        fresh = self.bridge.act({"token": second["token"], "generation": second["generation"],
                                 "action": "upload_file",
                                 "args": {"path": "v.mp4", "channel": "ch-collective"}})
        self.assertTrue(fresh["ok"], fresh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
