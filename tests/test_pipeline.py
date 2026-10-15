#!/usr/bin/env python3
"""tests/test_pipeline.py — M-PIPE evidence (t-20261015-0bc7).

    python3 -m unittest discover -s tests -v

The 13-stage machine against a scratch world: the legal walk (including
middle-stage shortening), every refused transition, the stage-9
authorization contract (wrong approver, missing directive ref, the
silence window), the stage-10 PC check, the stub executor's labels,
and the stage-13 receipt.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
import contextlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import world  # noqa: E402

CONTRACT = {"schema": 1, "director": "carlo", "manager": "vesta",
            "approval_delegation": None, "fallback_silence_min": 45}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = world.main(argv)
    return code, out.getvalue(), err.getvalue()


def zminus(minutes):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mars-pipe-"))
        (self.dir / "world").mkdir()
        shutil.copy(ROOT / "world" / "lock.json", self.dir / "world" / "lock.json")
        (self.dir / "world" / "contract.json").write_bytes(world.canonical(CONTRACT))
        # a colonist + channel to own videos
        run(["--no-push", "--root", str(self.dir), "join", "--name", "nova",
             "--caps", "video"])
        self.vid = None

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def new_video(self):
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video", "new",
                            "--title", "first light (test)",
                            "--channel", "ch-nova", "--by", "nova",
                            "--evidence", "thread th-test-0001"])
        self.assertEqual(code, 0, out)
        self.vid = out.split()[1]
        return self.vid

    def advance(self, vid, to, by="nova", evidence="evidence-for-stage", url=None,
                expect=0):
        argv = ["--no-push", "--root", str(self.dir), "video", "advance", vid,
                "--to", str(to), "--by", by, "--evidence", evidence]
        if url:
            argv += ["--url", url]
        code, out, err = run(argv)
        self.assertEqual(code, expect, f"advance -> {to}: {out}{err}")
        return out, err

    def read_video(self, vid):
        return json.loads((self.dir / "world" / "videos" / f"{vid}.json").read_text())

    # ------------------------------------------------------------ stages --
    def test_new_creates_stage_one_with_evidence(self):
        vid = self.new_video()
        v = self.read_video(vid)
        self.assertEqual(v["stage"], 1)
        self.assertEqual(v["stage_name"], "concept-proposed")
        self.assertEqual(v["history"][0]["evidence"], "thread th-test-0001")
        self.assertEqual(v["owner"], "nova")

    def test_new_refuses_unknown_channel_and_unregistered_author(self):
        code, _, err = run(["--no-push", "--root", str(self.dir), "video", "new",
                            "--title", "x", "--channel", "ch-nope", "--by", "nova",
                            "--evidence", "e"])
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("no channel", err)
        code, _, err = run(["--no-push", "--root", str(self.dir), "video", "new",
                            "--title", "x", "--channel", "ch-nova", "--by", "stranger",
                            "--evidence", "e"])
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("not a registered colonist", err)

    def test_middle_stages_may_shorten_but_not_go_backwards(self):
        vid = self.new_video()
        self.advance(vid, 3)                       # skip 2 (shorten the middle)
        self.advance(vid, 2, expect=world.EXIT_REFUSED)   # backwards refused
        self.advance(vid, 3, expect=world.EXIT_REFUSED)   # re-enter refused
        self.advance(vid, 8)                       # jump to final-review
        self.assertEqual(self.read_video(vid)["stage"], 8)

    def test_every_transition_needs_evidence(self):
        vid = self.new_video()
        code, _, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                            vid, "--to", "2", "--by", "nova", "--evidence", " "])
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("needs --evidence", err)

    def test_the_illegal_transition_8_to_11_is_refused(self):
        vid = self.new_video()
        self.advance(vid, 8)
        _, err = self.advance(vid, 11, expect=world.EXIT_REFUSED)
        self.assertIn("illegal transition 8 -> 11", err)
        self.assertIn("skips the approval gate and the PC", err)

    def test_stage_nine_requires_the_recorded_approval(self):
        vid = self.new_video()
        self.advance(vid, 8)
        _, err = self.advance(vid, 9, expect=world.EXIT_REFUSED)
        self.assertIn("approval gate", err)

    # ------------------------------------------------------- the gate ------
    def test_stage_nine_refuses_the_wrong_approver(self):
        vid = self.new_video()
        self.advance(vid, 8)
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind", "director"])
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("Director-gated", err)
        self.assertIn("you are 'nova'", err)

    def test_directive_needs_its_ref(self):
        vid = self.new_video()
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind",
                            "directive"])
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("--ref", err)

    def test_directive_pre_authorizes_early_and_passes_the_gate_later(self):
        vid = self.new_video()                      # stage 1
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind",
                            "directive", "--ref", "bus-abc123"])
        self.assertEqual(code, 0)
        self.assertIn("PRE-AUTHORIZED", out)
        v = self.read_video(vid)
        self.assertEqual(v["publish"]["approval_kind"], "directive")
        self.assertEqual(v["publish"]["approved_by"], "carlo")   # the Director approved
        self.assertEqual(v["stage"], 1)              # no silent advance
        self.advance(vid, 8)
        self.advance(vid, 9)                         # now legal: approval exists
        self.assertEqual(self.read_video(vid)["stage"], 9)

    def test_pre_authorized_video_cannot_jump_to_9(self):
        # argus review (PR #1): the stage-9 gate must check ORDER too — a
        # pre-authorization recorded at stage 2 may not skip 3-8 in one command
        vid = self.new_video()                      # stage 1
        run(["--no-push", "--root", str(self.dir), "video", "publish-approved", vid,
             "--by", "nova", "--kind", "directive", "--ref", "bus-abc123"])
        self.assertEqual(self.read_video(vid)["stage"], 1)
        code, out, err = run(["--no-push", "--root", str(self.dir), "video", "advance",
                              vid, "--to", "9", "--by", "nova", "--evidence", "jump"])
        self.assertEqual(code, 4)
        self.assertIn("illegal transition 1 -> 9: publish-approved comes straight "
                      "after final-review", err)
        self.assertEqual(self.read_video(vid)["stage"], 1)   # unmoved
        # and only after honestly reaching 8 does the gate open
        self.advance(vid, 8)
        self.advance(vid, 9)
        self.assertEqual(self.read_video(vid)["stage"], 9)

    def test_director_approves_at_the_gate(self):
        vid = self.new_video()
        self.advance(vid, 8)
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "carlo", "--kind",
                            "director"])
        self.assertEqual(code, 0)
        self.assertIn("8 -> 9", out)
        v = self.read_video(vid)
        self.assertEqual(v["stage"], 9)
        self.assertEqual(v["publish"]["approved_by"], "carlo")

    def test_manager_fallback_respects_the_silence_window(self):
        vid = self.new_video()
        self.advance(vid, 8)
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "vesta", "--kind",
                            "manager-fallback", "--pinged-ts", zminus(10)])
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("silence window (45 min) has not elapsed", err)
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "vesta", "--kind",
                            "manager-fallback", "--pinged-ts", zminus(50)])
        self.assertEqual(code, 0)
        self.assertIn("8 -> 9", out)

    def test_fallback_refuses_non_manager_and_missing_ping(self):
        vid = self.new_video()
        self.advance(vid, 8)
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind",
                            "manager-fallback", "--pinged-ts", zminus(50)])
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("the manager's", err)
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "vesta", "--kind",
                            "manager-fallback"])
        self.assertEqual(code, world.EXIT_ERR)
        self.assertIn("--pinged-ts", err)

    def test_delegation_lets_the_named_agent_approve(self):
        contract = dict(CONTRACT, approval_delegation={"video": "REPLACED", "to": "nova",
                                                       "ts": zminus(5), "by": "vesta"})
        (self.dir / "world" / "contract.json").write_bytes(world.canonical(contract))
        vid = self.new_video()
        self.advance(vid, 8)
        # no delegation for THIS video yet -> nova still refused
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind", "director"])
        self.assertEqual(code, world.EXIT_REFUSED)
        contract["approval_delegation"]["video"] = vid
        (self.dir / "world" / "contract.json").write_bytes(world.canonical(contract))
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "nova", "--kind", "director"])
        self.assertEqual(code, 0)
        v = self.read_video(vid)
        self.assertEqual(v["publish"]["approved_by"], "nova")
        self.assertIn("recorded delegation", v["history"][-1]["evidence"])

    def test_double_approval_refused(self):
        vid = self.new_video()
        self.advance(vid, 8)
        run(["--no-push", "--root", str(self.dir), "video", "publish-approved", vid,
             "--by", "carlo", "--kind", "director"])
        code, _, err = run(["--no-push", "--root", str(self.dir), "video",
                            "publish-approved", vid, "--by", "carlo", "--kind",
                            "director"])
        self.assertEqual(code, world.EXIT_REFUSED)
        self.assertIn("already publish-approved", err)

    # ----------------------------------------------------------- the pc ---
    def test_stage_ten_checks_the_real_lock(self):
        vid = self.new_video()
        self.advance(vid, 8)
        run(["--no-push", "--root", str(self.dir), "video", "publish-approved", vid,
             "--by", "carlo", "--kind", "director"])
        # nova does NOT hold the PC -> refused
        _, err = self.advance(vid, 10, expect=world.EXIT_REFUSED)
        self.assertIn("needs the PC", err)
        # acquire it with the wrong purpose -> refused
        run(["--no-push", "--root", str(self.dir), "pc", "acquire", "--by", "nova",
             "--purpose", "unrelated work"])
        _, err = self.advance(vid, 10, expect=world.EXIT_REFUSED)
        self.assertIn("must name " + vid, err)
        # release, re-acquire with the right purpose -> passes
        lock = json.loads((self.dir / "world" / "lock.json").read_text())
        run(["--no-push", "--root", str(self.dir), "pc", "release", "--by", "nova",
             "--token", lock["token"]])
        run(["--no-push", "--root", str(self.dir), "pc", "acquire", "--by", "nova",
             "--purpose", f"upload {vid} to ch-nova"])
        self.advance(vid, 10)

    # ------------------------------------------------- the stub executor --
    def test_stage_twelve_requires_a_url_and_labels_the_stub(self):
        vid = self.walk_to(11)
        _, err = self.advance(vid, 12, expect=world.EXIT_ERR)
        self.assertIn("requires --url", err)
        self.advance(vid, 12, url="https://studio.example/watch?v=stub")
        v = self.read_video(vid)
        self.assertEqual(v["publish"]["url"], "https://studio.example/watch?v=stub")
        self.assertTrue(v["publish"]["verified_ts"])
        self.assertEqual(v["history"][-1]["executor"], "stub")   # labeled, always

    def test_stage_thirteen_writes_the_stub_receipt(self):
        vid = self.walk_to(12)
        out, _ = self.advance(vid, 13)
        self.assertIn("STUB executor", out)
        v = self.read_video(vid)
        self.assertEqual(v["stage"], 13)
        self.assertEqual(v["stage_name"], "recorded")
        rid = v["history"][-1]["receipt"]
        receipt = json.loads((self.dir / "world" / "receipts" / f"{rid}.json").read_text())
        self.assertEqual(receipt["video"], vid)
        self.assertEqual(receipt["executor"], "stub")
        self.assertEqual(receipt["deposit"]["status"], "stub-local")
        self.assertEqual(receipt["approval"]["by"], "carlo")
        self.assertTrue(receipt["pc"]["generation"] >= 1)      # a real lock was used

    # --------------------------------------------------------- the walk ---
    def walk_to(self, stage):
        vid = self.new_video()
        if stage >= 2:
            self.advance(vid, 2, evidence="concept reviewed in thread")
        if stage >= 3:
            self.advance(vid, 3, evidence="sources gathered")
        if stage >= 4:
            self.advance(vid, 4, evidence="script v1")
        if stage >= 5:
            self.advance(vid, 5, evidence="challenge answered")
        if stage >= 6:
            self.advance(vid, 6, evidence="assets ready")
        if stage >= 7:
            self.advance(vid, 7, evidence="render receipt")
        if stage >= 8:
            self.advance(vid, 8, evidence="final review thread")
        if stage >= 9:
            run(["--no-push", "--root", str(self.dir), "video", "publish-approved",
                 vid, "--by", "carlo", "--kind", "director"])
        if stage >= 10:
            run(["--no-push", "--root", str(self.dir), "pc", "acquire", "--by", "nova",
                 "--purpose", f"upload {vid} to ch-nova"])
            self.advance(vid, 10, evidence="pc granted for upload")
        if stage >= 11:
            self.advance(vid, 11, evidence="upload receipt rc-stub")
        if stage >= 12:
            self.advance(vid, 12, url="https://studio.example/watch?v=stub")
        return vid

    def test_the_full_thirteen_stage_walk(self):
        """the card's evidence: a dummy video through every stage, honestly."""
        vid = self.walk_to(12)
        out, _ = self.advance(vid, 13, evidence="receipt deposit (stub)")
        self.assertIn("ADVANCED", out)
        v = self.read_video(vid)
        self.assertEqual(v["stage"], 13)
        self.assertEqual([h["stage"] for h in v["history"]],
                         [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        # the last two stages are the stub's, and say so
        self.assertEqual(v["history"][-1]["executor"], "stub")
        self.assertEqual(v["history"][-2]["executor"], "stub")
        # the PC was used and released-able state is consistent
        lock = json.loads((self.dir / "world" / "lock.json").read_text())
        self.assertEqual(lock["holder"], "nova")

    def test_the_walk_is_refused_without_the_gate_order(self):
        """even the owner cannot jump the queue: 9 -> 11 is illegal."""
        vid = self.walk_to(9)
        _, err = self.advance(vid, 11, by="nova", expect=world.EXIT_REFUSED)
        self.assertIn("illegal transition 9 -> 11", err)

    def test_video_show(self):
        vid = self.new_video()
        code, out, _ = run(["--no-push", "--root", str(self.dir), "video", "show", vid])
        self.assertEqual(code, 0)
        self.assertIn("stage 1/13 concept-proposed", out)


if __name__ == "__main__":
    unittest.main()
