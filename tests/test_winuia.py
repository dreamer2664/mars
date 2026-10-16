#!/usr/bin/env python3
"""tests/test_winuia.py — the Windows backend's *decisions*, proven on Linux.

Honest scope note, because this is exactly where a manager could be tempted to overclaim: nothing
here talks to Windows. What it proves is that the backend refuses, reports and resolves the way
§7.8 says it will — the element-id contract, the black-capture rule, the disabled-element rule, the
over-cap rule, the UIPI hints. The real `uiautomation` calls live in one thin adapter (`UiaTransport`)
that no test here executes. A green run therefore means "**the logic is right**", not "this works on a
desktop". Those are different rungs (E3 vs E5), and keeping them apart is the whole culture of this
repo.

    python3 tests/test_winuia.py
"""

import contextlib
import io
import tempfile
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bridge  # noqa: E402
import winuia  # noqa: E402

TREE0 = [{"name": "Play", "control": "Button", "enabled": True, "offscreen": False, "rect": [0, 0, 10, 10]},
         {"name": "Output Log", "control": "Document", "enabled": True, "offscreen": False, "rect": [0, 0, 1, 1]},
         {"name": "Play", "control": "Button", "enabled": False, "offscreen": False, "rect": [0, 0, 10, 10]}]


class FakeTransport:
    """A pretend desktop holding only the properties the contract actually turns on."""
    def __init__(self, elevated=False, blank=False, over_cap=False, empty_capture=False):
        self._windows = {"0": {"title": "Roblox Studio — FloodlinePlace9", "pid": 4100, "minimized": False},
                         "1": {"title": "Code — world.py", "pid": 4200, "minimized": True}}
        self.elevated, self.blank = elevated, blank
        self.over_cap, self.empty_capture = over_cap, empty_capture
        self.actions, self.gone = [], set()

    # ---- observation ----
    def session_status(self):
        return {"interactive": True, "elevated_held": self.elevated, "session": "console", "free_mb": 8192}

    def windows(self):
        return {k: v for k, v in self._windows.items() if k not in self.gone}

    def elements(self, win_key, depth):
        if str(win_key) in self.gone:
            return []
        return TREE0 if str(win_key) == "0" else []      # window 1: real window, no tree (UIPI shape)

    def capture(self, win_key, region=None, max_width=960):
        if self.empty_capture:
            return None
        if self.over_cap:
            return b"\x00" * (winuia.WindowsBackend.MAX_BYTES + 1)
        return b"\x00" * 64 if self.blank else b"\x89PNG" + bytes(range(60))

    def is_blank(self, img):
        return self.blank

    def wait_for(self, cond, timeout_s):
        return (True, "matched") if "studio" in cond else (False, "timeout")

    def snapshot(self):
        return {"windows": self.windows()}

    # ---- action ----
    def invoke(self, win_key, idx, button="left"):
        self.actions.append(("invoke", win_key, idx, button))

    def set_text(self, win_key, idx, text):
        self.actions.append(("set_text", win_key, idx, len(text)))

    def send_keys(self, keys):
        self.actions.append(("keys", keys))

    def launch(self, app, args):
        return {"launched": app, "pid": 999}

    def open_url(self, url):
        return {"opened": url}

    def save_as(self, path):
        self.actions.append(("save_as", path))

    def tidy(self, kind, target=None):
        return {"closed": ["Code — world.py"]} if kind == "close-idle" else {"tidied": kind}

    def permitted_side_effect(self, action, args):
        raise winuia.TransportUnavailable(f"{action} not implemented on Windows yet")


def backend(**kw):
    t = FakeTransport(**kw)
    return winuia.WindowsBackend(transport=t), t


class IdsAndResolution(unittest.TestCase):
    def test_the_id_you_read_is_the_object_you_click(self):
        """The most dangerous place in a desktop agent. One enumeration feeds both the tree the agent
        reads and the click, so the id arithmetic has to match exactly — no second walk."""
        be, t = backend()
        tree = be.observe("tree", {"window": "0"})
        self.assertEqual([r["id"] for r in tree["tree"]], ["id-0-0", "id-0-1", "id-0-2"])
        out = be.run("invoke", {"element": "id-0-0"})
        self.assertEqual(t.actions[-1], ("invoke", "0", 0, "left"))
        self.assertEqual(out["invoked"], "Play")

    def test_stale_index_is_refused_and_touches_nothing(self):
        be, t = backend()
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.run("invoke", {"element": "id-0-9"})
        self.assertIn("no longer exists", str(e.exception))
        self.assertEqual(t.actions, [])

    def test_window_can_be_named_by_title_substring(self):
        be, t = backend()
        self.assertEqual(be.observe("tree", {"window": "studio"})["window"], "0")

    def test_coordinates_are_not_an_option(self):
        be, t = backend()
        for bad in ("12,40", "id-0", "", None, "button"):
            with self.assertRaises(winuia.TransportUnavailable, msg=bad):
                be.run("invoke", {"element": bad})
        self.assertEqual(t.actions, [])

    def test_disabled_element_refuses_loudly(self):
        be, t = backend()
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.run("invoke", {"element": "id-0-2"})
        self.assertIn("disabled", str(e.exception))
        self.assertEqual(t.actions, [])

    def test_vanished_window_is_not_retried(self):
        be, t = backend()
        t.gone.add("0")
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.run("invoke", {"element": "id-0-0"})
        self.assertIn("do not retry", str(e.exception))

    def test_set_text_reports_what_was_written_by_size_not_content(self):
        be, t = backend()
        out = be.run("set_text", {"element": "id-0-1", "text": "hello world"})
        self.assertEqual(out["chars"], 11)
        self.assertNotIn("hello", json.dumps(out), "a receipt must not carry the text it typed")


class CaptureHonesty(unittest.TestCase):
    def test_black_capture_is_reported_not_inferred(self):
        be, t = backend(empty_capture=True)
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.observe("capture", {"window": "0"})
        self.assertIn("PrintWindow", str(e.exception))

    def test_near_uniform_frame_is_flagged_not_polished(self):
        be, t = backend(blank=True)
        obs = be.observe("capture", {"window": "0"})
        self.assertIn("suspect", obs)

    def test_over_cap_capture_is_refused(self):
        be, t = backend(over_cap=True)
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.observe("capture", {"window": "0"})
        self.assertIn("ntfy", str(e.exception))

    def test_receipts_carry_a_digest_never_the_bytes(self):
        be, t = backend()
        obs = be.observe("capture", {"window": "0"})
        self.assertIn("img_sha", obs)
        slim = bridge._obs(obs)
        self.assertNotIn("img", slim)
        with tempfile.TemporaryDirectory() as d:
            core = bridge.Core(Path(d), be, bridge.Fence("off"), ask="none")
            rec = core.act("capture", {"window": "0"})
            self.assertEqual(rec["state"], "observed")
            self.assertNotIn("img", json.dumps(rec, sort_keys=True).replace("img_sha", ""))
            on_disk = (Path(d) / "receipts.jsonl").read_text()
            self.assertNotIn("\\x89PNG", on_disk)


class UiPiSignals(unittest.TestCase):
    def test_elevated_session_is_announced_in_status(self):
        be, t = backend(elevated=True)
        self.assertIn("uipi", be.status())
        self.assertNotIn("uipi", backend()[0].status())

    def test_empty_tree_is_suspicious_only_on_a_visible_window(self):
        be, t = backend()
        self.assertNotIn("suspect", be.observe("tree", {"window": "1"}))    # minimised: normal
        t._windows["1"]["minimized"] = False
        obs = be.observe("tree", {"window": "1"})
        self.assertIn("suspect", obs)
        self.assertIn("integrity", obs["suspect"])


class ReadsAndWrites(unittest.TestCase):
    def test_wait_for_reports_met_and_unmet(self):
        be, t = backend()
        self.assertTrue(be.observe("wait_for", {"condition": "window:studio"})["met"])
        self.assertFalse(be.observe("wait_for", {"condition": "window:nope"})["met"])

    def test_unimplemented_side_effects_say_so_instead_of_faking_success(self):
        be, t = backend()
        with self.assertRaises(winuia.TransportUnavailable) as e:
            be.run("send_email", {"to": "a@b.c", "subject": "s", "body": "x"})
        self.assertIn("not implemented", str(e.exception))

    def test_launch_and_open_url_pass_through(self):
        be, t = backend()
        self.assertEqual(be.run("launch", {"app": "notepad.exe"})["launched"], "notepad.exe")
        self.assertEqual(be.run("open_url", {"url": "https://example.com"})["opened"], "https://example.com")

    def test_tidy_close_idle_goes_through_the_transport(self):
        be, t = backend()
        self.assertEqual(be.run("tidy", {"kind": "close-idle"})["closed"], ["Code — world.py"])


class BackendSelection(unittest.TestCase):
    def test_windows_backend_on_a_linux_box_fails_closed(self):
        """Someone will inevitably run the bridge on the wrong machine. The answer must be a refusal
        with a reason, not a sim that quietly says ok."""
        if sys.platform == "win32":
            self.skipTest("this case is about *not* being on Windows")
        with self.assertRaises(winuia.TransportUnavailable) as e:
            winuia.build_transport()
        self.assertIn("Windows", str(e.exception))
        tmp = tempfile.mkdtemp()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = bridge.main(["--backend", "windows", "--log-dir", tmp,
                                "act", "invoke", "--element", "id-0-0", "--task", "t-1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["refused"], "backend-unreachable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
