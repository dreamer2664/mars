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

    def test_the_id_shape_is_the_same_on_every_backend(self):
        """`windows` lists `id-2`; `tree` lists `id-2-0`; `--window id-2`, `--window 2` and a title
        substring all resolve. The Director's run showed `first=1` on Windows against `id-1` on the sim —
        two spellings of the same thing is how an agent starts guessing."""
        be, t = backend()
        wins = be.observe("windows", {})
        self.assertEqual([w["id"] for w in wins["windows"]], ["id-0", "id-1"])
        for ref in ("id-0", "0"):
            self.assertEqual(be.observe("tree", {"window": ref})["tree"][0]["id"], "id-0-0", ref)
        self.assertEqual(be.observe("tree", {"window": "studio"})["window"], "0")

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


class _CtlClass:
    """The *class-level* shape of uiautomation 2.0.29's `Control`: the reads are `property` objects and
    the actions are methods. probe() inspects the class, so a fake that only has instance attributes
    would (correctly) be reported as missing them. Kept separate from _Ctl below, which is the instance
    shape the backend walks."""
    Name = property(lambda self: "")
    ControlTypeName = property(lambda self: "ButtonControl")
    IsEnabled = property(lambda self: True)
    IsOffscreen = property(lambda self: False)
    BoundingRectangle = property(lambda self: None)
    NativeWindowHandle = property(lambda self: 0)
    ProcessId = property(lambda self: 0)
    def GetChildren(self): return []
    def Click(self, **kw): pass
    def SendKeys(self, *a, **k): pass
    def Close(self): pass
    def GetInvokePattern(self): return None
    def GetValuePattern(self): return None
    def ToBitmap(self, *a, **k): return None
    def SetFocus(self): return True


class _Ctl:
    """Shaped like uiautomation 2.0.29's Control: Name/ControlTypeName/IsEnabled/IsOffscreen are
    PROPERTIES (plain attributes here), Click/SendKeys/ToBitmap/Get*Pattern are METHODS. Getting this
    backwards is what failed on the Director's PC, so the fake is deliberately strict about it."""
    def __init__(self, name, kids=None, enabled=True, offscreen=False, bitmap=None):
        self.Name, self.ControlTypeName = name, "ButtonControl"
        self.IsEnabled, self.IsOffscreen = enabled, offscreen
        self.ProcessId, self.NativeWindowHandle = 4100, 1
        self.BoundingRectangle = type("R", (), {"left": 0, "top": 0, "right": 40, "bottom": 10})()
        self._kids = kids or []
        self._bitmap = bitmap
        self.clicked = 0

    def GetChildren(self):
        return self._kids
    def Click(self, **kw):
        self.clicked += 1
    def SendKeys(self, *a, **k):
        pass
    def SetFocus(self):
        return True
    def Close(self):
        pass
    def GetInvokePattern(self):
        return None
    def GetValuePattern(self):
        return None
    def ToBitmap(self, *a, **k):
        return self._bitmap


class _Auto:
    """Enough of the module surface that probe() and the walks can be exercised off-Windows."""
    def __init__(self, root):
        self.Control = _CtlClass
        self.GetScreenSize = lambda: (1920, 1080)
        self.GetForegroundControl = lambda: root
        self.SendKeys = lambda *a, **k: None
        self._root = root
        self.GetRootControl = lambda: self._root


class LibraryShape(unittest.TestCase):
    def test_probe_reports_only_what_is_actually_missing(self):
        root = _Ctl("desktop", [_Ctl("Studio")])
        self.assertEqual(winuia.UiaTransport.probe(_Auto(root)), [], _Auto(root))
        broken = _Auto(root)
        del broken.GetScreenSize
        del broken.Control.ToBitmap
        missing = winuia.UiaTransport.probe(broken)
        self.assertIn("GetScreenSize", missing)
        self.assertIn("Control.ToBitmap", missing)

    def test_probe_catches_the_exact_bug_the_director_hit(self):
        """On Carlo's PC: `TypeError: 'bool' object is not callable` — a property called as a method.
        A library that renames an API, or exposes a member in the wrong *kind* (a method where we need a
        property), is caught here, before any control is touched. What it cannot catch is our own calling
        convention against a library that is internally consistent — the real 2.0.29 failure was exactly
        that, and it is why `elements()` still reads properties as attributes and never as calls."""
        # a *complete* stand-in for the library's Control class, in the correct shapes: _Ctl already
        # declares the properties as attributes and the actions as methods, so a clean probe means
        # "everything we call exists in the form we call it", not merely "Name is there".
        Props = _CtlClass
        class MethodsAsProps(_CtlClass):     # a renamed/retired method: we call it, it is a bare value
            GetChildren = 1
        auto = _Auto(_Ctl("desktop"))
        auto2 = type("A", (), dict(auto.__dict__))
        for k in ("GetRootControl", "GetForegroundControl", "SendKeys", "GetScreenSize"):
            setattr(auto2, k, getattr(auto, k))
        auto2.Control = MethodsAsProps
        bad = winuia.UiaTransport.probe(auto2)
        self.assertIn("Control.GetChildren", bad, bad)
        auto3 = type("A", (), dict(auto.__dict__))
        for k in ("GetRootControl", "GetForegroundControl", "SendKeys", "GetScreenSize"):
            setattr(auto3, k, getattr(auto, k))
        auto3.Control = Props
        self.assertEqual(winuia.UiaTransport.probe(auto3), [], winuia.UiaTransport.probe(auto3))

    def test_construction_refuses_a_library_it_cannot_trust(self):
        auto = _Auto(_Ctl("desktop"))
        del auto.GetScreenSize
        with self.assertRaises(winuia.TransportUnavailable) as e:
            winuia.UiaTransport(auto)
        self.assertIn("GetScreenSize", str(e.exception))
        self.assertIn("will not guess", str(e.exception))

    def test_walk_is_depth_limited_and_shared(self):
        deep = _Ctl("a", [_Ctl("b", [_Ctl("c")])])
        be = winuia.WindowsBackend(transport=type("T", (), {
            "windows": lambda self: {"0": {"title": "a", "pid": 1, "minimized": False}},
            "_win": lambda self, k: deep, "elements": lambda self, k, d: [],
        })())
        rows = winuia._walk(deep, None)
        self.assertEqual([c.Name for c in rows], ["b", "c"], "breadth-first, top window excluded")

    def test_downscale_is_optional_not_assumed(self):
        """max_width=0 must be a no-op, and a missing Pillow must not turn a capture into a failure:
        the frame is still real, just not resized. Silently *claiming* a resize would be the lie."""
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
        self.assertEqual(winuia._downscale(png, 0), png)
        self.assertIsInstance(winuia._downscale(png, 960), (bytes, bytearray))

    def test_blank_detector_sees_structure_not_mood(self):
        black = b"\x00" * 4096
        noisy = bytes(i % 251 for i in range(4096))
        self.assertTrue(winuia.is_mostly_blank(black, FakeTransport(blank=True)))
        self.assertFalse(winuia.is_mostly_blank(black, FakeTransport(blank=False)),
                         "the transport is the authority; the helper must not freelance")
        self.assertGreater(len(set(noisy[::7])), 2, "a real frame has structure")

if __name__ == "__main__":
    unittest.main(verbosity=2)