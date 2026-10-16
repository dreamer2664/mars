#!/usr/bin/env python3
"""tools/winuia.py — the Windows side of the bridge (§7.8). Kept separate from `bridge.py` on purpose:
policy lives there, plumbing lives here, so the plumbing can be tested on Linux against a fake
transport and only the `uiautomation` import needs a real desktop.

    python3 tools/bridge.py --backend windows act status
    python3 tools/bridge.py --backend windows act windows

Element ids are **derived, never assigned**: `id-<n>` is the n-th window in the enumeration and
`id-<n>-<m>` is the m-th interesting element inside it. A stale id is therefore detectable (the
element resolves to something else or to nothing) instead of silently pointing at the wrong button —
which is the failure mode that makes coordinate clicking untrustworthy.
"""

from __future__ import annotations

import hashlib
import sys


def digest(obj) -> str:
    import json
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class TransportUnavailable(Exception):
    """Raised when the real UIA layer cannot be built. Never faked, never silently degraded."""


class WindowsBackend:
    """UIA-first: elements to act, pixels only to judge. `transport` is injectable for tests."""
    name = "windows"
    available = True

    # ~960 px wide is a phone-readable lens; ntfy.sh caps attachments at 2 MB, so that is the
    # second limit. A capture over the cap is refused, not silently downscaled into mush.
    MAX_WIDTH_DEFAULT = 960
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, transport=None, max_width: int = MAX_WIDTH_DEFAULT):
        self.t = transport if transport is not None else build_transport()
        self.max_width = int(max_width or self.MAX_WIDTH_DEFAULT)

    # ---------------------------------------------------------- observation ---
    def status(self):
        st = self.t.session_status()
        out = {"backend": self.name, "up": bool(st.get("interactive", True)), "windows": len(self.t.windows()),
               "free_mb": st.get("free_mb"), "session": st.get("session", "?")}
        # UIPI is the ceiling the Director should know about before he trusts a click: a Medium session
        # cannot drive an elevated window, and it fails *politely* — 0 elements, "successful" clicks.
        if st.get("elevated_held"):
            out["uipi"] = ("this session cannot read or drive elevated windows; if a window reports 0 "
                           "elements, that is why — it does not mean the app is broken")
        return out

    def _win(self, key: str):
        wins = self.t.windows()
        key = str(key or "")
        bare = key[3:] if key.startswith("id-") else key        # accept "id-2" and "2" alike
        if bare in wins:
            return bare, wins[bare]
        for k, w in wins.items():
            if key.lower() in str(w.get("title", "")).lower() or f"id-{k}" == key:
                return k, w
        for k, w in wins.items():
            if key.lower() in str(w.get("title", "")).lower():
                return k, w
        return None, None

    def observe(self, kind, args):
        if kind == "windows":
            flt = (args.get("filter") or "").lower()
            # `id-<n>`, exactly as the sim reports it and exactly what `tree` hands back for elements,
            # so one string is valid in every command on every backend. An id that changes shape between
            # rehearsal and the real machine is a rehearsal that teaches the wrong syntax.
            items = [{"id": f"id-{wid}", **w} for wid, w in self.t.windows().items()
                     if not flt or flt in str(w.get("title", "")).lower()]
            return {"kind": "windows", "count": len(items), "windows": items, "digest": digest(items)}
        if kind == "tree":
            win_key, win = (args.get("window"), None)
            if win_key:
                win_key, win = self._win(win_key)
                if win is None:
                    raise TransportUnavailable(f"no window matches {args.get('window')!r} — look again with `windows`")
            depth = int(args.get("depth") or 3)
            els = self.t.elements(win_key, depth)
            rows = [{"id": f"id-{win_key}-{i}",     # win_key is bare here, so ids read `id-2-0` "name": e.get("name", ""), "control": e.get("control", ""),
                     "enabled": bool(e.get("enabled")), "offscreen": bool(e.get("offscreen")),
                     "rect": e.get("rect")} for i, e in enumerate(els)]
            out = {"kind": "tree", "window": win_key, "elements": len(rows), "tree": rows,
                   # Both backends answer with `ids`, the exact strings a click accepts. An agent that has
                   # to derive `id-<win>-<i>` from a nested tree gets it wrong once, clicks nothing, and
                   # reports success — the least visible failure in this whole system. The ids are computed
                   # here, from the same single enumeration the rows come from, so they cannot disagree.
                   "ids": [r["id"] for r in rows],
                   "digest": digest(rows)}
            if not rows and win is not None and not win.get("minimized"):
                # An empty tree from a *visible* window is the UIPI signature (Medium session, elevated
                # target). Say it out loud, or the agent concludes "there is nothing there" and starts
                # clicking coordinates — the exact slide this whole file exists to prevent.
                out["suspect"] = ("0 elements in a non-minimised window: probable integrity mismatch "
                                  "(UIPI), not an empty app — do not click blind because of this")
            return out
        if kind == "capture":
            win_key, win = (args.get("window") or "screen"), None
            if win_key != "screen":
                win_key, win = self._win(win_key)
                if win is None:
                    raise TransportUnavailable(f"no window matches {args.get('window')!r}")
            img = self.t.capture(win_key, region=args.get("region"), max_width=self.max_width)
            if not img:
                # PrintWindow returns black/empty for DirectComposition content. An empty capture is a
                # fact about the API, not a fact about the app — so it is reported, never guessed around.
                raise TransportUnavailable("capture returned no pixels (PrintWindow is black for some "
                                           "composited windows) — say so, do not infer a broken app")
            h = hashlib.sha256(img).hexdigest()
            out = {"kind": "capture", "window": win_key, "img": img, "img_sha": h, "bytes": len(img),
                   "digest": digest([h, args.get("region")])}
            if len(img) > self.MAX_BYTES:
                raise TransportUnavailable(f"capture is {len(img)} B, over ntfy's {self.MAX_BYTES} B "
                                           "attachment cap — send the sha and the storagecode path instead")
            if is_mostly_blank(img, self.t):
                out["suspect"] = "frame is near-uniform: either nothing is drawn or the capture path is blind"
            return out
        if kind == "wait_for":
            cond = args.get("condition", "")
            met, detail = self.t.wait_for(cond, float(args.get("timeout_s") or 20))
            return {"kind": "wait_for", "condition": cond, "met": bool(met), "detail": detail,
                    "digest": digest([cond, bool(met), detail])}
        return {"kind": kind, "digest": digest(self.t.snapshot())}

    # ----------------------------------------------------------------- write ---
    def _resolve(self, ref: str):
        if not ref or not str(ref).startswith("id-"):
            raise TransportUnavailable(f"{ref!r} is not an element id — take it from `tree`, do not guess")
        parts = str(ref).split("-")
        if len(parts) < 3:
            raise TransportUnavailable(f"{ref!r} names a window, not an element: expected id-<window>-<n>")
        win_key = "-".join(parts[1:-1])           # "id-2-0" -> window "2", element 0
        try:
            idx = int(parts[-1])
        except ValueError:
            raise TransportUnavailable(f"{ref!r} has a non-numeric element index")
        if win_key not in self.t.windows():
            # a different message from "index out of range" on purpose: a vanished window means the
            # job is over or moved, a stale index means look again. "no longer exists" for both would
            # let an agent retry its way into whatever window happens to occupy that slot now.
            raise TransportUnavailable(f"window {win_key} is gone from the desktop — do not retry, re-look")
        els = self.t.elements(win_key, depth=99)
        if idx >= len(els):
            raise TransportUnavailable(f"{ref} no longer exists ({len(els)} elements in window "
                                       f"{win_key}) — the UI moved; look again before you act")
        return win_key, idx, els[idx]

    def run(self, action, args):
        t = self.t
        if action == "invoke":
            win_key, idx, el = self._resolve(args["element"])
            if not el.get("enabled", True):
                raise TransportUnavailable(f"{args['element']} is disabled — reporting a click on it as "
                                           "success is exactly how a misclick becomes a false 'it works'")
            t.invoke(win_key, idx, button=args.get("button", "left"))
            return {"invoked": el.get("name", ""), "control": el.get("control", ""), "id": args["element"]}
        if action == "set_text":
            win_key, idx, el = self._resolve(args["element"])
            t.set_text(win_key, idx, args["text"])
            return {"set": el.get("name", ""), "chars": len(args["text"]), "id": args["element"]}
        if action == "key_chord":
            t.send_keys(args["keys"])
            return {"sent": args["keys"], "note": "chords go to the focused window; nothing here can verify focus"}
        if action == "launch":
            return t.launch(args["app"], args.get("args") or "")
        if action == "open_url":
            return t.open_url(args["url"])
        if action == "save_as":
            if args.get("element"):
                win_key, idx, _ = self._resolve(args["element"])
                t.set_text(win_key, idx, args["path"])
                t.invoke(win_key, idx + 1 if idx + 1 < len(t.elements(win_key, 99)) else idx)
            else:
                t.save_as(args["path"])
            return {"saved": args["path"], "note": "verify with `tree`/`capture` that the dialog closed"}
        if action == "tidy":
            return t.tidy(args["kind"], args.get("target"))
        if action in ("upload_file", "set_metadata", "read_published_url", "send_email", "submit_form"):
            # high-consequence verbs: the transport decides, the bridge has already scoped and gated them
            return t.permitted_side_effect(action, args)
        raise TransportUnavailable(f"windows backend has no handler for {action}")


def is_mostly_blank(img: bytes, transport) -> bool:
    try:
        return bool(transport.is_blank(img))
    except Exception:
        return False


def build_transport():
    """The only line in this file that needs Windows. Everything above is testable without it."""
    if sys.platform != "win32":
        raise TransportUnavailable(
            f"real UIA needs Windows (this is {sys.platform}); run with --backend sim to exercise the "
            "bridge's policy layer, which is what the tests are about")
    try:                                                    # noqa: allow the real import to fail loudly
        import uiautomation as auto                         # type: ignore
    except Exception as e:
        raise TransportUnavailable(f"`pip install uiautomation` first: {e}")
    return UiaTransport(auto)


class UiaTransport:
    """Adapter over `uiautomation` 2.0.29. Every name below was read out of that package's own source,
    not guessed — the first version of this class invented three of them (`IsEnabled()` as a method,
    `CaptureToRect`, `WaitUntilReady`) and the Director's PC found all three in one run.

    Every name below is checked by `python3 tools/api_check.py --wheel <uiautomation.whl>`, which reads the
    library's own source and rejects any call this repo makes that the class cannot serve. Run it after
    touching this file; it is the only honest way to test a Windows API from Linux.

    Rules this class obeys, because they are the difference between a field failure and a silent one:
      * a member we need but cannot find raises `TransportUnavailable` at construction (`probe()`),
        so a library upgrade is reported at once instead of mid-click;
      * capture returns PNG bytes straight from the bitmap (`ToBytes("png")`), no PIL dependency;
      * the window list and the element list come from ONE walk, so the id an agent reads is the
        object the click resolves to.
    """
    #: what we require from the installed library. Kind matters as much as presence: `IsEnabled` is a
    #: PROPERTY in uiautomation and `Click` is a METHOD, and writing `IsEnabled()` is a TypeError on a
    #: live desktop that no Linux test can see coming. probe() therefore checks the *shape* too —
    #: it is one loop over attributes the library already exposes, and it turns "observe:tree: TypeError:
    #: 'bool' object is not callable" into a one-line diagnosis before any action is attempted.
    REQUIRED = ("GetRootControl", "GetForegroundControl", "SendKeys", "GetScreenSize")
    #: What MUST exist on the base `Control` class, because every element we touch is one. Checked by
    #: probe() at construction. `tools/api_check.py` asserts this list against the library's real source,
    #: so a name cannot be added from memory again: the first version demanded `Close`, `GetInvokePattern`
    #: and `GetValuePattern`, none of which are on `Control`, and the guard rejected a good install on the
    #: Director's PC instead of finding the bug in my code.
    REQUIRED_CONTROL = ("Name", "ControlTypeName", "IsEnabled", "IsOffscreen", "BoundingRectangle",
                        "NativeWindowHandle", "ProcessId", "GetChildren", "Click", "SendKeys",
                        "ToBitmap", "SetFocus")
    #: What we use *if the concrete control offers it*: `GetInvokePattern` on ButtonControl &c.,
    #: `GetValuePattern` on EditControl &c., `Close` on WindowPatternControl. Not on the base class, so
    #: demanding them there is wrong twice over — a false alarm on a working library, and still no alarm
    #: when the specific element lacks the pattern. Call sites ask the object with hasattr().
    OPTIONAL_CONTROL = ("GetInvokePattern", "GetValuePattern", "Close", "GetClickablePoint",
                        "ShowWindow", "SetWindowText")
    #: properties, checked on a live control by verify_shapes() rather than on the class

    def __init__(self, auto):
        self.auto = auto
        missing = self.probe(auto)
        if missing:
            raise TransportUnavailable(
                "uiautomation does not match what this backend calls: " + ", ".join(missing)
                + " — the bridge will not guess its way past a renamed API. Run "
                  "`python tools\api_check.py` to see which side is wrong; a reinstall fixes a broken "
                  "install but not a name we invented, and this list has been wrong before")

    @staticmethod
    def probe(auto):
        """What is provable from the *class*: the module functions and Control methods we call exist and
        are callable. Properties are deliberately NOT checked here — in uiautomation `Name`/`IsEnabled`
        are `property` objects on the class and plain values on an instance, so a class-level shape test
        would fail on a perfectly good library. That half belongs to verify_shapes(), run against a real
        control the first time we walk one."""
        bad = []
        for name in UiaTransport.REQUIRED:
            m = getattr(auto, name, None)
            if m is None:
                bad.append(name)
            elif not callable(m):
                bad.append(f"{name} (present but not callable)")
        ctl = getattr(auto, "Control", None)
        if ctl is not None:
            for name in UiaTransport.REQUIRED_CONTROL:
                if callable(getattr(ctl, name, None)):
                    continue
                if isinstance(getattr(ctl, name, None), property):
                    continue                       # a property is exactly what we expect at class level
                bad.append(f"Control.{name}")       # missing, or neither method nor property
        return bad

    @staticmethod
    def verify_shapes(control):
        """The other half, against a *live* control: the properties we read must not be callables. This
        is the check that would have turned 'TypeError: \'bool\' object is not callable' into a sentence
        about the library, before a single action was attempted."""
        bad = []
        for name in ("Name", "ControlTypeName", "IsEnabled", "IsOffscreen", "BoundingRectangle",
                     "ProcessId", "NativeWindowHandle"):
            v = getattr(type(control), name, None)
            if v is not None and callable(v) and not isinstance(v, property):
                bad.append(f"Control.{name} is a method in this library; this backend reads it as a value")
        return bad

    # ------------------------------------------------------------ enumeration ---
    def session_status(self):
        try:
            import ctypes
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        free_mb = None
        try:
            class EX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("ullAvailPhys", ctypes.c_uint64),
                            ("_pad", ctypes.c_uint64 * 6)]
            e = EX(); e.dwLength = ctypes.sizeof(EX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(e)):
                free_mb = e.ullAvailPhys // (1024 * 1024)
        except Exception:
            pass
        return {"interactive": True, "elevated_held": is_admin, "session": "console", "free_mb": free_mb}

    def _top_level(self):
        out = []
        for c in self.auto.GetRootControl().GetChildren():
            try:
                if not c.Name:                       # unnamed panes are shell furniture, not windows
                    continue
                if c.IsOffscreen and not c.IsEnabled:
                    continue
            except Exception:
                continue
            out.append(c)
        return out

    def windows(self):
        out = {}
        for i, w in enumerate(self._top_level()):
            r = w.BoundingRectangle
            out[str(i)] = {"title": w.Name, "pid": w.ProcessId,
                           "minimized": bool(r.right - r.left <= 0 or r.bottom - r.top <= 0),
                           "rect": [r.left, r.top, r.right, r.bottom]}
        return out

    def _win(self, key):
        for k, w in self.windows().items():
            if k == str(key):
                for c in self._top_level():
                    if c.Name == w["title"]:
                        return c
        return None

    def _flat(self, win_key):
        """THE one enumeration: what `elements()` shows and what `_ctl()` clicks are the same list."""
        w = self._win(win_key)
        if w is None:
            return []
        return _walk(w, self.auto)

    def elements(self, win_key, depth):
        rows = []
        walked = _walk(self._win(win_key), self.auto)
        if walked:
            bad = UiaTransport.verify_shapes(walked[0])
            if bad:
                raise TransportUnavailable("the installed uiautomation does not match this backend: "
                                            + "; ".join(bad))
        for c in walked[:400]:
            try:
                r = c.BoundingRectangle
                rect = [r.left, r.top, r.right, r.bottom]
            except Exception:
                rect = None
            rows.append({"name": c.Name, "control": c.ControlTypeName.replace("Control", ""),
                         "enabled": bool(c.IsEnabled), "offscreen": bool(c.IsOffscreen), "rect": rect})
        return rows

    # ----------------------------------------------------------------- capture ---
    def capture(self, win_key, region=None, max_width=960):
        target = "screen" if win_key in (None, "screen") else self._win(win_key)
        if target is None:
            raise TransportUnavailable(f"window {win_key} is gone before the capture")
        bmp = target.ToBitmap()
        if bmp is None:
            raise TransportUnavailable("ToBitmap returned nothing — this window will not give up pixels "
                                       "(composited or protected); say so rather than inferring a dead app")
        try:
            data = bmp.ToBytes("png")
            size = (bmp.Width, bmp.Height)
        finally:
            try:
                bmp.__exit__(None, None, None)
            except Exception:
                pass
        if not data:
            raise TransportUnavailable("png encode returned no bytes")
        return _downscale(bytes(data), max_width)

    def is_blank(self, img) -> bool:
        """Cheap and honest: a real desktop frame has structure; a black PrintWindow failure is uniform."""
        body = img[-4096:]
        return len(set(body[::7])) <= 2 if body else True

    # ------------------------------------------------------------------ action ---
    def _ctl(self, win_key, idx):
        flat = self._flat(win_key)
        if not flat:
            raise TransportUnavailable(f"window {win_key} is gone (0 elements at click time) — do not retry")
        if idx >= len(flat):
            raise TransportUnavailable(f"element index {idx} out of range: the UI moved, look again")
        return flat[idx]

    def invoke(self, win_key, idx, button="left"):
        """Click `c`, preferring the Invoke pattern when this control offers one.

        `GetInvokePattern` is NOT on uiautomation's base `Control`: it is defined on `ButtonControl`,
        `HeaderItemControl`, `HyperlinkControl` and friends, i.e. only where the UIA tree classified the
        element. Asking the concrete object is therefore the correct form; asking the *class* `Control` is
        a lie — which is what my first `probe()` did, and it reported a perfectly good install as "missing
        Control.GetInvokePattern". A pattern lookup can also raise if the element stopped supporting it
        between the walk and the click, so this branch is best-effort and a plain `Click` is always the
        fallback: never a failure invented by our own optimism.
        """
        c = self._ctl(win_key, idx)
        if button == "left" and hasattr(c, "GetInvokePattern"):
            try:
                pat = c.GetInvokePattern()
            except Exception:
                pat = None
            if pat is not None:
                pat.Invoke()
                return
        c.Click(simulateMove=False, button=button)

    def set_text(self, win_key, idx, text):
        c = self._ctl(win_key, idx)
        safe = text.replace("{", "{{").replace("}", "}}")
        pat = None
        if hasattr(c, "GetValuePattern"):   # on EditControl/ComboBoxControl/DocumentControl, not on Control
            try:
                pat = c.GetValuePattern()
            except Exception:
                pat = None
        if pat is not None:
            try:
                pat.SetValue(text)          # replaces the value outright: no select-all, no brace escaping
                return
            except Exception:
                pass                        # read-only ValuePattern is common; fall through to keys
        c.SetFocus()
        c.SendKeys("{Ctrl}a", waitTime=0.05)
        c.SendKeys(safe, waitTime=0.05)

    def send_keys(self, keys):
        self.auto.SendKeys(keys, waitTime=0.05)

    def wait_for(self, cond, timeout_s):
        import time
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            titles = [w["title"] for w in self.windows().values()]
            if cond.startswith("window:"):
                want = cond.split(":", 1)[1].lower()
                if any(want in t.lower() for t in titles):
                    return True, "window appeared"
            elif cond.startswith("text:"):
                want = cond.split(":", 1)[1].lower()
                for wid in self.windows():
                    if any(want in (e.get("name") or "").lower() for e in self.elements(wid, 6)):
                        return True, "text appeared"
            elif cond.startswith("title:"):
                return False, "use window:<substr> — title: is the same thing, spelled once"
            time.sleep(0.5)
        return False, f"timeout after {timeout_s:.0f}s"

    def snapshot(self):
        return {"windows": self.windows()}

    def launch(self, app, args):
        import subprocess
        p = subprocess.Popen(f'start "" "{app}" {args}', shell=True)
        return {"launched": app, "pid": p.pid}

    def open_url(self, url):
        import os
        os.startfile(url)                                       # noqa: Windows-only by construction
        return {"opened": url}

    def save_as(self, path):
        raise TransportUnavailable("save_as needs an element from the dialog's tree; a blind path keystroke "
                                   "into an unverified dialog is how a file lands in the wrong place")

    def tidy(self, kind, target=None):
        if kind == "close-idle":
            closed = []
            for wid, w in self.windows().items():
                if not w.get("minimized"):
                    continue
                c = self._win(wid)
                # `Close` arrives via WindowPatternControl; the base Control has none. A window the library
                # typed otherwise is SKIPPED and reported as skipped — closing is optional tidying, and
                # inventing a `Control.Close()` is how you kill a process instead of hiding a window.
                if c is None or not hasattr(c, "Close"):
                    closed.append({"title": w["title"], "closed": False,
                                   "why": "no Close on this control type"})
                    continue
                c.Close()
                closed.append({"title": w["title"], "closed": True})
            return {"closed": closed}
        if kind == "memory":
            try:
                import ctypes
                ctypes.windll.psapi.SetProcessWorkingSetSize(-1, 0xFFFFFFFF, 0xFFFFFFFF)
                return {"tidied": "memory", "note": "working-set trim only; it is a hint, not a promise"}
            except Exception as e:
                raise TransportUnavailable(f"memory tidy unavailable: {e}")
        raise TransportUnavailable(f"{kind} has no Windows implementation yet — say so rather than "
                                   "approximating it with something else")

    def permitted_side_effect(self, action, args):
        raise TransportUnavailable(f"{action} is not implemented on Windows yet. The bridge allowed it "
                                   "(it is scope-bound, not allowlist-bound), so this is a build gap, "
                                   "not a refusal — put it on the card")


def _walk(top, auto):
    """Breadth-first, depth-bounded, and *the only* walk — _flat and elements share it."""
    if top is None:
        return []
    rows, stack = [], [top]
    while stack:
        c = stack.pop(0)
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in kids:
            rows.append(k)
            stack.append(k)
        if len(rows) >= 400:
            break
    return rows


def _downscale(png: bytes, max_width: int) -> bytes:
    """Optional polish: shrink only if Pillow is present, and never pretend a resize happened."""
    if not max_width:
        return png
    try:
        import io
        from PIL import Image
    except Exception:
        return png
    try:
        im = Image.open(io.BytesIO(png))
        if im.width <= max_width:
            return png
        h = int(im.height * (max_width / im.width))
        out = io.BytesIO()
        im.convert("RGB").resize((max_width, h)).save(out, "PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return png
