#!/usr/bin/env python3
"""Executors — what actually happens when an action passes the fence.

Three of them, one interface, so the crew can test the whole protocol without
Windows (docs/BRIDGE.md §5):

  SimulatorExecutor  the Linux simulator. A mock browser with real state:
                     channels, an active channel chip, uploaded videos. Used by
                     tests/test_bridge_sim.py to prove acquire → act → refuse
                     stale gen → refuse wrong token → release.
  DryRunExecutor     the bridge's own dry-run mode. Walks the same code path
                     and writes real receipts, but touches no browser and
                     claims no observable result — every result is labelled
                     "dry-run", so a dry run can never be mistaken for a
                     publication.
  CdpExecutor        the real road: attach to the Director's browser over CDP.
                     Not exercised on Linux; see bridge/README.md.

Fail-closed is the rule for all three: anything unexpected => act nothing,
return ok:false with a reason. The bridge never releases the lock.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request


class Executor:
    """The interface. Every method returns (ok, reason, result)."""

    name = "base"

    def status(self) -> dict:
        raise NotImplementedError

    def upload_file(self, args: dict) -> tuple[bool, str, dict]:
        raise NotImplementedError

    def set_metadata(self, args: dict) -> tuple[bool, str, dict]:
        raise NotImplementedError

    def read_published_url(self, args: dict) -> tuple[bool, str, dict]:
        raise NotImplementedError

    def dispatch(self, action: str, args: dict) -> tuple[bool, str, dict]:
        fn = getattr(self, action, None)
        if fn is None:
            return False, "not-allowlisted", {}
        return fn(args)


class SimulatorExecutor(Executor):
    """A mock browser with real state, so the protocol is testable on Linux.

    `dialog` simulates the unexpected-dialog case: with it set, every action
    fails closed (act nothing) — that is the fail-closed law, exercised.
    """

    name = "simulator"

    def __init__(self, channels: list[str] | None = None, active_channel: str | None = None,
                 browser_up: bool = True, dialog: str | None = None):
        self.channels = list(channels or ["ch-collective"])
        self.active_channel = active_channel or (self.channels[0] if self.channels else None)
        self.browser_up = browser_up
        self.dialog = dialog            # e.g. "Sign in again" — an unexpected dialog
        self.videos: dict[str, dict] = {}
        self.busy = False
        self._seq = 0

    # -- helpers ---------------------------------------------------------
    def _fail_closed(self, reason: str) -> tuple[bool, str, dict]:
        return False, reason, {"browser_up": self.browser_up,
                               "active_channel": self.active_channel,
                               "dialog": self.dialog}

    def _new_id(self) -> str:
        self._seq += 1
        seed = f"{time.time_ns()}:{self._seq}"
        return "vid-" + hashlib.sha256(seed.encode()).hexdigest()[:11]

    # -- interface -------------------------------------------------------
    def status(self) -> dict:
        return {"browser_up": self.browser_up, "active_channel": self.active_channel,
                "channels": self.channels, "busy": self.busy, "dialog": self.dialog,
                "videos": len(self.videos)}

    def upload_file(self, args: dict) -> tuple[bool, str, dict]:
        if not self.browser_up:
            return self._fail_closed("browser-unreachable")
        if self.dialog:
            return self._fail_closed("unexpected-dialog")
        target = args.get("channel")
        if target not in self.channels:
            return self._fail_closed(f"unknown-channel:{target}")
        # the channel chip must read the target channel before anything uploads
        self.active_channel = target
        vid = self._new_id()
        self.videos[vid] = {"channel": target, "path": args.get("path"),
                            "url": f"https://example.invalid/watch?v={vid}",
                            "title": None, "live": True}
        return True, "", {"video_id": vid, "active_channel": self.active_channel,
                          "url": self.videos[vid]["url"],
                          "page_title": f"YouTube Studio — {target}"}

    def set_metadata(self, args: dict) -> tuple[bool, str, dict]:
        if not self.browser_up:
            return self._fail_closed("browser-unreachable")
        if self.dialog:
            return self._fail_closed("unexpected-dialog")
        vid = args.get("video_id")
        if vid not in self.videos:
            return self._fail_closed(f"unknown-video:{vid}")
        for k in ("title", "description", "thumbnail"):
            if args.get(k) is not None:
                self.videos[vid][k] = args[k]
        return True, "", {"video_id": vid, "set": {k: self.videos[vid].get(k)
                                                   for k in ("title", "description", "thumbnail")},
                          "page_title": f"Video details — {vid}"}

    def read_published_url(self, args: dict) -> tuple[bool, str, dict]:
        if not self.browser_up:
            return self._fail_closed("browser-unreachable")
        if self.dialog:
            return self._fail_closed("unexpected-dialog")
        vid = args.get("video_id")
        if vid not in self.videos:
            return self._fail_closed(f"unknown-video:{vid}")
        v = self.videos[vid]
        if not v.get("live"):
            return self._fail_closed(f"not-live:{vid}")
        return True, "", {"video_id": vid, "url": v["url"], "live": True,
                          "page_title": v["url"]}


class DryRunExecutor(Executor):
    """Same path, real receipts, no browser, no claimed result."""

    name = "dry-run"

    def __init__(self, active_channel: str = "ch-collective"):
        self.active_channel = active_channel
        self.busy = False

    def status(self) -> dict:
        return {"browser_up": False, "active_channel": self.active_channel,
                "busy": self.busy, "mode": "dry-run",
                "note": "no browser attached; no observable result is claimed"}

    def _ok(self, action: str, args: dict) -> tuple[bool, str, dict]:
        return True, "", {"mode": "dry-run", "action": action,
                          "would_use_args": sorted(args),
                          "observable_result": None,
                          "note": "dry run — nothing was published"}

    def upload_file(self, args): return self._ok("upload_file", args)
    def set_metadata(self, args): return self._ok("set_metadata", args)
    def read_published_url(self, args): return self._ok("read_published_url", args)


class CdpExecutor(Executor):
    """The real road — attach to the Director's browser over CDP.

    Only reachability is decided here; the Studio choreography (channel chip
    verification, upload, processing wait) runs on the Director's PC as the
    first M1 act, with him watching (docs/BRIDGE.md §5). Until that run has
    happened, every action fails closed rather than claiming a result nobody
    observed.
    """

    name = "cdp"

    def __init__(self, endpoint: str = "http://127.0.0.1:9222", timeout: float = 3.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.busy = False

    def _reachable(self) -> bool:
        try:
            with urllib.request.urlopen(self.endpoint + "/json/version", timeout=self.timeout) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def status(self) -> dict:
        up = self._reachable()
        return {"browser_up": up, "active_channel": None, "busy": self.busy,
                "mode": "cdp", "endpoint": self.endpoint}

    def _refuse(self, action: str) -> tuple[bool, str, dict]:
        if not self._reachable():
            return False, "browser-unreachable", {"mode": "cdp", "endpoint": self.endpoint}
        return False, "cdp-choreography-not-yet-run", {
            "mode": "cdp", "action": action,
            "note": "the browser is up; the Studio flow has not been verified on the "
                    "Director's PC yet, so the bridge acts on nothing (fail closed)"}

    def upload_file(self, args): return self._refuse("upload_file")
    def set_metadata(self, args): return self._refuse("set_metadata")
    def read_published_url(self, args): return self._refuse("read_published_url")
