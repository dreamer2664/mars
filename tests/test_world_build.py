#!/usr/bin/env python3
"""tests/test_world_build.py — M-MAP evidence (t-20261015-a550).

The compiler is deterministic; the phantom check is the anti-simulation law made
executable: inject a fake avatar into the compiled file and the check must fail,
by name. Run: python3 -m unittest discover -s tests -v
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
import world_build as wb  # noqa: E402


class Compile(unittest.TestCase):
    def test_compiles_every_section(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "world.json"
            code = wb.main(["--root", str(ROOT), "--out", str(out), "--now", "2026-10-15T09:00:00Z"])
            self.assertEqual(code, 0)
            w = json.loads(out.read_text())
            for key in ("schema", "generated_ts", "world_head", "warnings", "counts", "lock",
                        "agents", "channels", "videos", "threads", "meetings", "archive"):
                self.assertIn(key, w)
            self.assertEqual(w["schema"], 1)
            self.assertEqual(w["generated_ts"], "2026-10-15T09:00:00Z")
            self.assertEqual(w["counts"]["colonists"], len(w["agents"]))
            self.assertEqual(w["counts"]["channels"], len(w["channels"]))
            self.assertGreaterEqual(w["counts"]["colonists"], 5)
            self.assertIsNotNone(w["world_head"])
            self.assertEqual(w["warnings"], [])

    def test_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.json", Path(td) / "b.json"
            wb.main(["--root", str(ROOT), "--out", str(a), "--now", "2026-10-15T09:00:00Z"])
            wb.main(["--root", str(ROOT), "--out", str(b), "--now", "2026-10-15T09:00:00Z"])
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(a.read_bytes(), world.canonical(json.loads(a.read_bytes())))

    def test_agents_carry_their_source(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "w.json"
            wb.main(["--root", str(ROOT), "--out", str(out), "--now", "2026-10-15T09:00:00Z"])
            w = json.loads(out.read_text())
            for a in w["agents"]:
                self.assertEqual(a["_source"], f"world/agents/{a['id']}.json")
                self.assertTrue((ROOT / a["_source"]).is_file())

    def test_committed_world_json_is_current(self):
        """app/world.json in the repo must equal a fresh compile (no drift)."""
        committed = ROOT / "app" / "world.json"
        if not committed.is_file():
            self.skipTest("app/world.json not committed yet")
        w = json.loads(committed.read_text())
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "w.json"
            wb.main(["--root", str(ROOT), "--out", str(out), "--now", w["generated_ts"]])
            self.assertEqual(committed.read_bytes(), out.read_bytes(),
                             "app/world.json is stale — run tools/world_build.py")


class PhantomCheck(unittest.TestCase):
    def compile(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "w.json"
            wb.main(["--root", str(ROOT), "--out", str(out), "--now", "2026-10-15T09:00:00Z"])
            return json.loads(out.read_text())

    def test_clean_world_passes(self):
        findings = wb.check_phantoms(self.compile(), ROOT)
        self.assertEqual(findings, [])

    def test_injected_avatar_fails_the_build(self):
        w = self.compile()
        w["agents"].append({"id": "ghost", "role": "colonist", "caps": [], "channel": "ch-ghost",
                            "location": "command", "status": "active", "last_seen_ts": None,
                            "note": "", "_source": "world/agents/ghost.json"})
        findings = wb.check_phantoms(w, ROOT)
        self.assertTrue(findings, "a fabricated avatar MUST fail the check")
        self.assertTrue(any("ghost" in f and "PHANTOM" in f for f in findings), findings)

    def test_a_lie_about_the_lock_fails(self):
        w = self.compile()
        w["lock"] = dict(w["lock"])
        w["lock"]["holder"] = "ghost"
        w["lock"]["state"] = "held"
        findings = wb.check_phantoms(w, ROOT)
        self.assertTrue(any("holder" in f for f in findings), findings)

    def test_check_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "w.json"
            wb.main(["--root", str(ROOT), "--out", str(out), "--now", "2026-10-15T09:00:00Z"])
            self.assertEqual(wb.main(["--root", str(ROOT), "--check", str(out)]), 0)
            bad = json.loads(out.read_text())
            bad["agents"].append({"id": "phantom", "_source": "world/agents/phantom.json"})
            badfile = Path(td) / "bad.json"
            badfile.write_text(json.dumps(bad))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = wb.main(["--root", str(ROOT), "--check", str(badfile)])
            self.assertEqual(code, 1)
            self.assertIn("PHANTOM", buf.getvalue())


class Preview(unittest.TestCase):
    def test_preview_is_self_contained(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "preview.html"
            wb.main(["--root", str(ROOT), "--preview", str(out), "--now", "2026-10-15T09:00:00Z"])
            html = out.read_text()
            self.assertIn("window.__WORLD__", html)
            self.assertNotIn('href="./app.css"', html)     # style inlined
            self.assertNotIn('src="./app.js"', html)       # script inlined
            self.assertIn("OUTPOST", html)
            w = json.loads(html.split("window.__WORLD__ = ", 1)[1].split(";</script>", 1)[0])
            self.assertEqual(w["schema"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
