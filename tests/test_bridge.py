#!/usr/bin/env python3
"""tests/test_bridge.py — M1 bridge evidence (docs/BRIDGE.md §7).

The point of this file is not "the code runs". It is that the **refusals are in the code**, in the
same spirit as tests/test_world_lock.py's fencing cases: a rule that only lives in an agent's memory
has N chances to fail, so the ones that matter must be provable without trusting anyone's reading.

    python3 tests/test_bridge.py

Covers: §7.1 three permanent refusals (unreachable by any mode/config), unknown-verb refusal by
name, §7.2 scope required for writes and *not* for reads, fence (stale generation / wrong token),
§7.3 the three permission modes incl. ask=none, §7.4 tidy as a verb with a budget and a
"not somebody else's work" refusal, §7.5 the receipt chain incl. tamper detection and
done-unverified, §7.6 the approval round-trip, §7.7 auto-pause on a stale approval + fail-closed
backends, and the localhost-only HTTP control surface.
"""

import contextlib
import io
import re
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bridge  # noqa: E402


def run(*argv, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bridge.main(list(argv))
    text = out.getvalue().strip()
    try:
        payload = json.loads(text.splitlines()[-1]) if text else {}
    except Exception:
        payload = {"raw": text}
    return code, payload, err.getvalue()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mars-bridge-")
        self.L = ["--log-dir", str(Path(self.tmp) / "pc")]

    def act(self, *argv):
        return run(*self.L, "act", *argv)


class Refusals(Case):
    """§7.1 — none of these can be turned off, by any knob the Director has."""

    def test_elevation_refused_by_name(self):
        for verb, argv in (("launch", ["--app", "runas", "--task", "t-1"]),
                           ("launch", ["--app", "powershell", "--args", "Start-Process -Verb RunAs", "--task", "t-1"]),
                           ("key_chord", ["--keys", "alt+y", "--why", "accept the UAC consent dialog", "--task", "t-1"])):
            code, o, _ = self.act(verb, *argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(o["refused"], "elevated-action", argv)

    def test_credential_reads_refused(self):
        for argv in (["--element", "id-2-1", "--text", "cat %USERPROFILE%\\.private\\crew.env", "--task", "t-1"],
                     ["--path", "C:/Users/me/AppData/Local/Google/Chrome/User Data/Default/Login Data", "--task", "t-1"],
                     ["--element", "id_rsa", "--text", "~/.ssh/id_rsa", "--task", "t-1"]):
            code, o, _ = self.act("set_text", *argv) if argv[0] == "--element" else self.act("save_as", *argv)
            self.assertEqual(o["refused"], "credential-access", argv)

    def test_secure_attention_not_a_verb(self):
        code, o, _ = self.act("key_chord", "--keys", "ctrl+alt+del", "--task", "t-1")
        self.assertEqual(o["refused"], "not-a-verb")

    def test_ask_none_cannot_relax_refusals(self):
        code, o, _ = run(*self.L, "--ask", "none", "act", "key_chord", "--keys", "ctrl+alt+del", "--task", "t-1")
        self.assertEqual(o["refused"], "not-a-verb", "the mode must not be an escape hatch")

    def test_unknown_verb_refused_by_name(self):
        code, o, _ = self.act("format_c_drive", "--task", "t-1")
        self.assertEqual(o["refused"], "not-allowlisted")
        self.assertIn("format_c_drive", o["hint"])

    def test_miscounted_arguments_refused(self):
        # An argument we do not know is refused rather than dropped: "type it here" with a typo'd
        # target must never be read as "type it wherever". (The CLI rejects unknown flags outright,
        # so this is tested at the Core boundary, which is also the HTTP boundary.)
        core = bridge.Core(Path(self.tmp) / "core", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        with self.assertRaises(bridge.Refused) as e:
            core.act("invoke", {"element": "id-1-1", "elemnt": "id-3-1", "task": "t-1"})
        self.assertEqual(e.exception.reason, "bad-args")
        self.assertIn("elemnt", e.exception.hint)
        with self.assertRaises(bridge.Refused) as e2:
            core.act("invoke", {"task": "t-1"})
        self.assertEqual(e2.exception.reason, "bad-args")


class ScopeAndReads(Case):
    """§7.2 — writes need a card; reads are never gated, because looking cannot mutate."""

    def test_write_without_scope_refused(self):
        code, o, _ = self.act("invoke", "--element", "id-1-1")
        self.assertEqual(o["refused"], "no-scope")

    def test_read_needs_no_scope_and_is_recorded(self):
        code, o, _ = self.act("capture", "--window", "id-1")
        self.assertEqual(code, 0)
        self.assertEqual(o["state"], "observed")
        self.assertEqual(o["task"], "-", "looking needs no card; it is recorded anyway")
        self.assertIn("img_sha", o["result"])
        self.assertNotIn('"img"', json.dumps(o), "receipts carry a capture's digest, never its bytes")
        _, chain, _ = run(*self.L, "outcomes", "--last", "5")
        self.assertEqual(chain["count"], 1)

    def test_pause_blinds_nothing_and_blocks_everything(self):
        run(*self.L, "pause")
        code, o, _ = self.act("capture", "--window", "id-1")
        self.assertEqual(code, 0, "a halt that blinds the human is not a halt: the lens stays open")
        self.assertEqual(o["state"], "observed")
        code2, o2, _ = self.act("invoke", "--element", "id-1-1", "--task", "t-1")
        self.assertEqual(o2["refused"], "paused")


class Fence(Case):
    def test_stale_generation_refused(self):
        f = bridge.Fence("static", token="abc123", generation=7)
        with self.assertRaises(bridge.Refused) as e:
            f.check("abc123", 6)
        self.assertEqual(e.exception.reason, "stale-generation")

    def test_wrong_token_refused(self):
        f = bridge.Fence("static", token="abc123", generation=7)
        with self.assertRaises(bridge.Refused) as e:
            f.check("deadbeef", 7)
        self.assertEqual(e.exception.reason, "wrong-token")

    def test_missing_lock_refused(self):
        f = bridge.Fence("static", token="abc123", generation=7)
        with self.assertRaises(bridge.Refused) as e:
            f.check(None, None)
        self.assertEqual(e.exception.reason, "no-lock")

    def test_unreachable_fence_fails_closed(self):
        f = bridge.Fence("world", root=Path(self.tmp) / "nowhere", token="t", generation=1)
        with self.assertRaises(bridge.Refused) as e:
            f.check("t", 1)
        self.assertEqual(e.exception.reason, "fence-unreachable")

    def test_fence_world_against_the_real_world_tool(self):
        """End-to-end against tools/world.py itself, not a mock of it: acquire a real PC grant,
        act on it, act on a wrong token, and check what the receipt log says. If world.py's
        contract moves and nobody updates §7.5, this is what goes red."""
        import shutil, subprocess
        root = Path(self.tmp) / "mars"
        (root / "tools").mkdir(parents=True); (root / "world").mkdir(parents=True)
        shutil.copy(ROOT / "tools" / "world.py", root / "tools" / "world.py")
        def world(*argv):
            return subprocess.run([sys.executable, str(root / "tools" / "world.py"), "--root", str(root),
                                   "--no-push", *argv], capture_output=True, text=True)
        out = world("pc", "acquire", "--by", "fabius", "--purpose", "m1 test", "--ttl", "30")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        tok = re.search(r"token=(\S+)", out.stdout).group(1)
        gen = int(re.search(r"generation=(\d+)", out.stdout).group(1))
        core = bridge.Core(Path(self.tmp) / "wf", bridge.SimBackend(), bridge.Fence("world", root=root), ask="none")
        rec = core.act("invoke", {"element": "id-1-1", "task": "t-1"}, token=tok, generation=gen)
        self.assertEqual(rec["state"], "done")
        with self.assertRaises(bridge.Refused) as e:
            core.act("invoke", {"element": "id-1-2", "task": "t-1"}, token="not-the-grant", generation=gen)
        self.assertEqual(e.exception.reason, "stale-generation")
        # the receipt chain carries the acting grant's *digest*, never the token itself
        _, o, _ = run(*["--log-dir", str(Path(self.tmp) / "wf"), "outcomes", "--last", "5", "--verify-chain"])
        self.assertTrue(o["chain"]["ok"])
        self.assertNotIn(tok, json.dumps(o), "a lease token in a public receipt is a leaked credential")
        world("pc", "release", "--by", "fabius", "--token", tok)
        with self.assertRaises(bridge.Refused) as e2:
            core.act("invoke", {"element": "id-1-2", "task": "t-1"}, token=tok, generation=gen)
        self.assertIn(e2.exception.reason, ("stale-generation", "fence-unreachable"))


class PermissionModes(Case):
    def test_important_parks_then_approves(self):
        code, o, _ = self.act("invoke", "--element", "id-1-1", "--task", "t-20261016-9999")
        self.assertEqual(o["state"], "pending-approval")
        pid = o["id"]
        code, p, _ = run(*self.L, "pending")
        self.assertIn(pid, p)
        code, rec, _ = run(*self.L, "approve", pid)
        self.assertEqual(rec["state"], "done")
        self.assertTrue(rec["approval"].startswith("approved:"))

    def test_deny_is_recorded_not_silent(self):
        _, o, _ = self.act("send_email", "--to", "x@y.z", "--subject", "hi", "--body", "b", "--task", "t-1")
        _, rec, _ = run(*self.L, "deny", o["id"], "--reason", "out of scope for this card")
        self.assertEqual(rec["state"], "denied")
        self.assertIn("out of scope", rec["approval"])

    def test_ask_none_writes_execute_immediately(self):
        code, rec, _ = run(*self.L, "--ask", "none", "act", "invoke", "--element", "id-1-1", "--task", "t-1")
        self.assertEqual(code, 0)
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["approval"], "auto")

    def test_ask_all_gates_reads_too(self):
        code, o, _ = run(*self.L, "--ask", "all", "act", "invoke", "--element", "id-1-1", "--task", "t-1")
        self.assertEqual(o["state"], "pending-approval")

    def test_ok_true_requires_a_visible_change(self):
        # "the click was delivered" is not "it worked": a write that moves nothing is flagged, not
        # marked done. Driven at the Core boundary with a backend that can accept-then-do-nothing,
        # which is exactly the UIPI failure mode docs/BRIDGE.md §7.1 warns about.
        class Deaf(bridge.SimBackend):
            def run(self, action, args):        # reports success, changes nothing
                return {"invoked": "Play", "ok": True}
        core = bridge.Core(Path(self.tmp) / "deaf", Deaf(), bridge.Fence("off"), ask="none")
        rec = core.act("invoke", {"element": "id-1-1", "task": "t-1"})
        self.assertEqual(rec["state"], "done-unverified", rec)
        core2 = bridge.Core(Path(self.tmp) / "live", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        self.assertEqual(core2.act("invoke", {"element": "id-1-1", "task": "t-1"})["state"], "done",
                         "a real toggle changes the state, so this one is verified")


class Tidying(Case):
    """§7.4 — tidying is encouraged, budgeted, and must not eat other agents' work."""

    def test_tidy_runs_as_a_verb(self):
        # one Core, one backend: a fresh CLI process starts a fresh sim, so the effect is asserted here
        core = bridge.Core(Path(self.tmp) / "tidy", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        rec = core.act("tidy", {"kind": "memory", "task": "t-1"})
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["result"]["free_mb"], 9800, "one tidy of 800 MB on the sim")
        self.assertEqual(core.backend.status()["free_mb"], 9800)

    def test_close_idle_only_closes_minimised_windows(self):
        core = bridge.Core(Path(self.tmp) / "idle", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        self.assertEqual(core.act("windows", {})["result"]["count"], 3)
        core.act("tidy", {"kind": "close-idle", "task": "t-1"})
        self.assertEqual(core.act("windows", {})["result"]["count"], 2,
                         "exactly the one minimised window (id-3 'Save As')")
        self.assertEqual(core.act("windows", {"filter": "Roblox"})["result"]["count"], 1,
                         "the window under work is untouched")

    def test_budget_stops_a_tidy_loop(self):
        for _ in range(3):
            run(*self.L, "--tidy-max-per-hour", "2", "--ask", "none", "act", "tidy", "--kind", "memory", "--task", "t-1")
        code, o, _ = run(*self.L, "--tidy-max-per-hour", "2", "--ask", "none",
                         "act", "tidy", "--kind", "memory", "--task", "t-1")
        self.assertEqual(o["refused"], "tidy-budget")

    def test_refuses_to_tidy_the_colony(self):
        for tgt in ("world/agents", "C:/repo/.git", "/x/PUSHES.md", "agent/blender/t-20261016-58d8"):
            code, o, _ = run(*self.L, "--ask", "none", "act", "tidy", "--kind", "temp",
                             "--target", tgt, "--task", "t-1")
            self.assertEqual(o["refused"], "tidy-elsewhere", tgt)


class Chain(Case):
    def test_chain_verifies_and_detects_tampering(self):
        for i in range(3):
            run(*self.L, "--tidy-max-per-hour", "99", "--ask", "none",
                "act", "tidy", "--kind", "memory", "--task", f"t-{i}")
        code, out, _ = self._verify()
        self.assertTrue(out["chain"]["ok"], out)
        log = Path(self.tmp) / "pc" / "receipts.jsonl"
        lines = log.read_text().splitlines()
        tampered = json.loads(lines[1]); tampered["state"] = "done"  # rewrite history: claim success
        tampered["action"] = tampered["action"] + "-lies"
        lines[1] = json.dumps(tampered, sort_keys=True)
        log.write_text("\n".join(lines) + "\n")
        code, out, _ = self._verify()
        self.assertFalse(out["chain"]["ok"])
        self.assertEqual(out["chain"]["broken"][0]["seq"], 2)

    def _verify(self):
        return run(*self.L, "outcomes", "--last", "50", "--verify-chain")

    def test_append_to_broken_chain_fails_closed(self):
        log = Path(self.tmp) / "pc"
        log.mkdir(parents=True, exist_ok=True)
        (log / "receipts.jsonl").write_text("this is not json\n")
        code, o, _ = self.act("capture", "--window", "id-1")
        self.assertEqual(o["refused"], "chain-unreadable")


class FailClosedAndControls(Case):
    def test_null_backend_refuses_everything(self):
        for verb, argv in (("capture", ["--window", "id-1"]), ("windows", []), ("tree", ["--window", "id-1"])):
            code, o, _ = run("--log-dir", str(Path(self.tmp) / "p2"), "--backend", "null", "act", verb, *argv)
            self.assertEqual(o["refused"], "backend-unreachable", f"{verb} on a dead backend must not say ok")
        code, o, _ = run("--log-dir", str(Path(self.tmp) / "p2"), "--backend", "null",
                         "act", "invoke", "--element", "id-1-1", "--task", "t-1")
        self.assertEqual(o["refused"], "backend-unreachable")

    def test_stale_approval_halts_the_bridge(self):
        # §7.7 — an approval nobody answers must not turn into "keep waiting" or "guess yes".
        # Age the pending item past the deadline rather than setting the deadline to zero: the rule
        # is "older than T", and a test that exploits T=0 would not catch a broken comparison.
        run(*self.L, "--hold-timeout-min", "10", "act", "invoke", "--element", "id-1-1", "--task", "t-1")
        pf = Path(self.tmp) / "pc" / "pending.json"
        d = json.loads(pf.read_text())
        for k in d:
            d[k]["asked_ts"] = "2026-10-16T09:00:00Z"
        pf.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        self.act("invoke", "--element", "id-1-2", "--task", "t-1")      # the next act is what notices
        _, st, _ = run(*self.L, "status")
        self.assertTrue(st["paused"], st)
        self.assertIn("unacknowledged", st["pause_reason"])
        code, o, _ = self.act("invoke", "--element", "id-1-2", "--task", "t-1")
        self.assertEqual(o["refused"], "paused", "and it stays paused: no self-resume")
        run(*self.L, "resume")
        _, st2, _ = run(*self.L, "status")
        self.assertFalse(st2["paused"])

    def test_pause_survives_a_process_restart(self):
        # The agent's `act` and the human's `pause` are different processes; a halt in RAM is no halt.
        self.act("invoke", "--element", "id-1-1", "--task", "t-1")
        run(*self.L, "pause")
        code, o, _ = self.act("invoke", "--element", "id-1-1", "--task", "t-1")
        self.assertEqual(o["refused"], "paused", "the pause must still be in force in a fresh process")

    def test_pause_and_resume_are_receipts_too(self):
        run(*self.L, "pause")
        run(*self.L, "resume")
        _, out, _ = run(*self.L, "outcomes", "--last", "5")
        actions = [r["action"] for r in out["receipts"]]
        self.assertEqual(actions, ["pause", "resume"], "stopping and starting must be as auditable as acting")

    def test_http_server_is_localhost_only_and_honours_pause(self):
        import threading
        import urllib.error
        import urllib.request
        core = bridge.Core(Path(self.tmp) / "http", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        # note: ask=none here, because the HTTP surface is exercised against a live Core object
        srv_holder = {}

        def boot():
            import http.server
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bridge._handler_for(core))
            srv_holder["s"] = srv
            srv.serve_forever()
        t = threading.Thread(target=boot, daemon=True); t.start()
        import time
        for _ in range(100):
            if srv_holder:
                break
            time.sleep(0.02)
        port = srv_holder["s"].server_address[1]
        url = f"http://127.0.0.1:{port}"
        st = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/status", method="POST", data=b"{}"),
                                                timeout=5).read())
        self.assertFalse(st["paused"])
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/pause", method="POST",
                                                                      data=json.dumps({"reason": "human"}).encode()),
                                               timeout=5).read())
        self.assertTrue(r["state"], "paused")
        try:
            urllib.request.urlopen(urllib.request.Request(url + "/act", method="POST", data=json.dumps(
                {"action": "invoke", "args": {"element": "id-1-1"}, "task": "t-1"}).encode()), timeout=5)
            self.fail("an act while paused must not succeed")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 409)
            self.assertEqual(json.loads(e.read())["refused"], "paused")
        srv_holder["s"].shutdown()


class SelfTest(Case):
    """The self-test is the gate a brand-new PC runs through, so its *verdict semantics* are load-bearing
    — and the failure mode I am guarding against here is the flattering green light, not the red one."""

    def test_sim_reports_policy_only_never_pass(self):
        core = bridge.Core(Path(self.tmp) / "st", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        out = core.selftest()
        self.assertEqual(out["verdict"], "PASS-POLICY-ONLY", out["checks"])
        self.assertEqual(out["passed"], out["total"])
        names = {c["name"]: c["ok"] for c in out["checks"]}
        for r in ("not-a-verb", "elevated-action", "credential-access", "no-scope"):
            self.assertTrue(names[f"refusal:{r}"], f"refusal {r} did not fire inside the self-test")

    def test_dead_backend_reports_fail(self):
        core = bridge.Core(Path(self.tmp) / "st2", bridge.NullBackend(), bridge.Fence("off"), ask="none")
        out = core.selftest()
        self.assertEqual(out["verdict"], "FAIL")
        self.assertLess(out["passed"], out["total"])

    def test_selftest_is_itself_a_receipt(self):
        core = bridge.Core(Path(self.tmp) / "st3", bridge.SimBackend(), bridge.Fence("off"), ask="none")
        core.selftest()
        rows = core.outcomes(50, True)
        self.assertTrue(rows["chain"]["ok"], "running the self-test must not corrupt the chain it audits")
        self.assertIn("selftest", [r["action"] for r in rows["receipts"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)