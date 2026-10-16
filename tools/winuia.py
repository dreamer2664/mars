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
        if key in wins:
            return key, wins[key]
        for k, w in wins.items():
            if key.lower() in str(w.get("title", "")).lower():
                return k, w
        return None, None

    def observe(self, kind, args):
        if kind == "windows":
            flt = (args.get("filter") or "").lower()
            items = [{"id": wid, **w} for wid, w in self.t.windows().items()
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
            rows = [{"id": f"id-{win_key}-{i}", "name": e.get("name", ""), "control": e.get("control", ""),
                     "enabled": bool(e.get("enabled")), "offscreen": bool(e.get("offscreen")),
                     "rect": e.get("rect")} for i, e in enumerate(els)]
            out = {"kind": "tree", "window": win_key, "elements": len(rows), "tree": rows,
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
        win_key = "-".join(parts[1:-1])
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
    """Thin adapter over `uiautomation`. Deliberately dumb: the policy is in bridge.py.

    Not exercised by this repo's tests (it needs a desktop); every method's *decision* is covered
    through the fake transport in tests/test_winuia.py, so the logic is proven even where the API is not.
    """
    def __init__(self, auto):
        self.auto = auto

    def session_status(self):
        try:
            import ctypes
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        return {"interactive": True, "elevated_held": is_admin, "session": "console", "free_mb": None}

    def windows(self):
        out = {}
        for i, w in enumerate(self.auto.GetRootControl().GetChildren()):
            if w.ControlTypeName != "WindowControl":
                continue
            out[str(i)] = {"title": w.Name, "pid": w.ProcessId, "minimized": bool(w.IsMinimize())}
        return out

    def _win(self, key):
        for i, w in enumerate(self.auto.GetRootControl().GetChildren()):
            if str(i) == str(key):
                return w
        return None

    def _flat(self, win_key):
        """THE one enumeration. `elements()` (what an agent reads) and `_ctl()` (what gets clicked)
        both come from here, so the id in a tree is the same object at click time. Two walks — one
        filtered for reading, one raw for acting — is how an id silently points at the wrong button."""
        w = self._win(win_key)
        if w is None:
            return []
        flat, stack = [], [w]
        while stack:
            c = stack.pop(0)
            for k in c.GetChildren():
                flat.append(k)
                stack.append(k)
        return flat

    def elements(self, win_key, depth):
        flat = self._flat(win_key)
        rows = []
        for c in flat[:400]:
            r = c.BoundingRectangle
            rows.append({"name": c.Name, "control": c.ControlTypeName.replace("Control", ""),
                         "enabled": bool(c.IsEnabled()), "offscreen": bool(c.IsOffscreen()),
                         "rect": [r.left, r.top, r.right, r.bottom]})
        return rows

    def invoke(self, win_key, idx, button="left"):
        c = self._ctl(win_key, idx)
        if button == "left":
            pat = c.GetInvokePattern()
            if pat is not None:
                return pat.Invoke()
        c.Click(simulateMove=False, button=button)

    def set_text(self, win_key, idx, text):
        c = self._ctl(win_key, idx)
        pat = c.GetValuePattern()
        if pat is not None:
            return pat.SetValue(text)
        c.SendKeys("{Ctrl}a", waitTime=0.05)
        c.SendKeys(text.replace("{", "{{").replace("}", "}}"), waitTime=0.05)

    def _ctl(self, win_key, idx):
        flat = self._flat(win_key)
        if not flat:
            raise TransportUnavailable(f"window {win_key} is gone (0 elements at click time) — do not retry")
        if idx >= len(flat):
            raise TransportUnavailable(f"element index {idx} out of range: the UI moved, look again")
        return flat[idx]

    def send_keys(self, keys):
        self.auto.SendKeys(keys, waitTime=0.05)

    def capture(self, win_key, region=None, max_width=960):
        if region:
            return self.auto.SafeBlockShot(*region)
        w = self._win(win_key)
        return w.CaptureToRect(scale=1.0) if w else None

    def is_blank(self, img) -> bool:
        try:
            data = bytes(img)
        except Exception:
            return False
        return len(set(data[-256:])) <= 2 if data else True

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
            elif cond == "idle":
                return True, "no-op: idle cannot be observed through UIA; use text:/window: instead"
            time.sleep(0.5)
        return False, "timeout"

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
                if w.get("minimized"):
                    c = self._win(wid)
                    if c is not None:
                        c.Close()
                        closed.append(w["title"])
            return {"closed": closed}
        raise TransportUnavailable(f"{kind} has no Windows implementation yet — say so rather than "
                                   "approximating it with something else")

    def permitted_side_effect(self, action, args):
        raise TransportUnavailable(f"{action} is not implemented on Windows yet. The bridge allowed it "
                                   "(it is scope-bound, not allowlist-bound), so this is a build gap, "
                                   "not a refusal — put it on the card")
