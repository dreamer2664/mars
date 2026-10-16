#!/usr/bin/env python3
"""tools/bridge.py — the desktop bridge: the ONLY thing on this machine that can act.

Interface: docs/BRIDGE.md §1–§7 (frozen parts honoured; §7 is this implementation's amendment).

    python3 tools/bridge.py status
    python3 tools/bridge.py act capture   --window id-1
    python3 tools/bridge.py act invoke    --task t-20261016-9999 --element id-1-2 --token T --generation 1
    python3 tools/bridge.py outcomes --last 10 [--verify-chain]
    python3 tools/bridge.py approve <id> | deny <id> --reason "…" | pending
    python3 tools/bridge.py pause | resume
    python3 tools/bridge.py serve --backend sim --port 8765

Discipline (the four things that make this more than a remote control):

  * **Fence first.** token+generation are verified before anything is touched, by asking the
    world (`tools/world.py pc check-token`) or an explicit test fence. A stale grant cannot act,
    so a crashed controller cannot keep typing into your machine.
  * **Three refusals live here, in code.** Elevated consent clicks, credential reads and the
    secure-attention chord are rejected by name before a backend exists to obey them — the same
    reasoning as tests/test_world_lock.py and chatcode's no-touch guard: a rule kept only in an
    agent's memory is a rule with N chances to fail. Everything else is scope-bound, not
    allowlist-bound, and a write with no `--task` is refused because it could never be reviewed.
  * **Receipts chain.** one line per action, `prev`+`sha256` over the meaningful fields, `before`
    and `after` observation digests. `ok:true` means the world changed, not that a command ran.
  * **Fail closed.** no backend, no fence, unreadable chain, pending approval past the deadline ⇒
    nothing happens, and the refusal is on the record.

stdlib only. No secrets, ever: this file must never log a capture's contents, only its digest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCHEMA = 1
DEFAULT_LOG_DIR = "pc"                      # pc/receipts.jsonl, pc/pending.json (never under world/)
HOLD_TIMEOUT_MIN_DEFAULT = 10
TIDY_MAX_PER_HOUR_DEFAULT = 12
CAPTURE_MAX_WIDTH_DEFAULT = 960             # a phone view, and ntfy.sh caps attachments at 2 MB
CAPTURE_MAX_BYTES = 2 * 1024 * 1024
EXIT_WORLD_STALE = 5                      # tools/world.py's EXIT_STALE: "the grant is no longer live"

# ---------------------------------------------------------------- refusals ---
# §7.1. Code, not prompt. Names are stable because agents quote them in reports.
REFUSALS = [
    ("elevated-action", re.compile(r"runas|runasadmin|elevat|\buac\b|consent.?(dialog|prompt)|"
                                   r"yes.?on.?consent|admin.?approval|override.?uac", re.I)),
    ("credential-access", re.compile(r"\.env\b|\.private\b|id_rsa|id_ed25519|\.pem\b|known_hosts|"
                                     r"key3\.db|key4\.db|logins\.json|signons\.sqlite|cookies?\.sqlite|"
                                     r"login data|web data|\bcredentials\b|credential store|"
                                     r"cmdkey|lazagne|mimikatz|\btoken\b.{0,12}\bread\b", re.I)),
    ("not-a-verb", re.compile(r"ctrl\+?alt\+?del|ctrl alt del|secure.?attention|sas\b|taskmgr\?.*logout", re.I)),
    # The Director keeps one shared GitHub token for the whole crew (his call, 2026-10-16), and chatcode
    # says the quiet part itself: its captchaproject guard "is not a wall - it is the promise written down
    # where it cannot be forgotten, misread or argued with". So the promise is written down here too, at the
    # one boundary that moves a mouse. It grants nothing and removes no privilege; it removes the accident
    # (an agent with a browser open is one misclick from a forbidden repo it never meant to visit).
    ("forbidden-target", re.compile(r"captchaproject|captchaprogram", re.I)),
]

# ------------------------------------------------------------------ actions ---
# name -> (class, perm, required fields, optional fields).  class read ⇒ no scope needed.
ACTIONS: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "status":            ("read",  "none",      (), ("include",)),
    "windows":           ("read",  "none",      (), ("filter",)),
    "tree":              ("read",  "none",      ("window",), ("depth",)),
    "capture":           ("read",  "none",      ("window",), ("region", "max_width", "save_to")),
    "wait_for":          ("read",  "none",      ("condition",), ("timeout_s",)),
    "invoke":            ("write", "important", ("element",), ("why", "button")),
    "set_text":          ("write", "important", ("element", "text"), ("why",)),
    "key_chord":         ("write", "important", ("keys",), ("why",)),
    "launch":            ("write", "important", ("app",), ("args", "why")),
    "open_url":          ("write", "important", ("url",), ("why",)),
    "save_as":           ("write", "important", ("path",), ("element", "why")),
    "tidy":              ("write", "important", ("kind",), ("target", "why")),
    "upload_file":       ("write", "important", ("path", "channel"), ("why",)),
    "set_metadata":      ("write", "important", ("video", "fields"), ("why",)),
    "read_published_url": ("write", "important", ("video",), ("why",)),
    "send_email":        ("write", "important", ("to", "subject", "body"), ("attach", "why")),
    "submit_form":       ("write", "important", ("form",), ("values", "why")),
}
READ_ONLY = {k for k, v in ACTIONS.items() if v[0] == "read"}
TIDY_KINDS = ("memory", "close-idle", "temp")
DIRTY_TIDY = re.compile(r"(^|/)(world|\.git|app)(/|$)|PUSHES\.md|data/INDEX\.md|agent/[a-z0-9-]+/", re.I)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def z() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Refused(Exception):
    def __init__(self, reason: str, hint: str = ""):
        super().__init__(reason)
        self.reason, self.hint = reason, hint


# ------------------------------------------------------------------ backends ---
class NullBackend:
    """Wiring only: every action fails closed. Nothing observed, nothing touched."""
    name, available = "null", False

    def status(self):
        return {"backend": self.name, "up": False, "detail": "no backend configured"}

    def observe(self, kind, args):
        raise Refused("backend-unreachable", f"cannot even {kind}: nothing is connected, so nothing is known")

    def run(self, action, args):
        raise Refused("backend-unreachable", "start with --backend sim for tests, windows on the PC")


class SimBackend:
    """A deterministic pretend desktop, so the refusals/chain/fence are CI-testable (§7.8).

    State is one dict; actions mutate it; captures are a real (tiny) PNG of the state so the
    capture path is exercised end to end, not stubbed.
    """
    name = "sim"
    available = True      # the sim is "connected" to a pretend desktop; windows.py will not be

    def __init__(self):
        self.state = {
            # keys are bare indices and ids are `id-<n>` / `id-<n>-<m>`: byte-for-byte the shape the
            # windows backend reports, so an agent never learns "window 1" on one box and "id-1" on
            # another. A convention that differs between sim and real is how rehearsal stops being rehearsal.
            "windows": {
                "1": {"title": "Roblox Studio — FloodlinePlace9", "pid": 4100, "minimized": False},
                "2": {"title": "Code — world.py", "pid": 4200, "minimized": False},
                "3": {"title": "Save As", "pid": 4200, "minimized": True},
            },
            "elements": {
                "1-0": {"window": "1", "name": "Play", "control": "Button", "enabled": True},
                "1-1": {"window": "1", "name": "Output Log", "control": "Document", "enabled": True},
                "2-0": {"window": "2", "name": "editor", "control": "Edit", "enabled": True},
                "3-0": {"window": "3", "name": "Cancel", "control": "Button", "enabled": True},
            },
            "values": {}, "log": [], "free_mb": 9_000, "idle_closed": 0, "temp_cleared_mb": 0,
            "downloads": 3, "playing": False, "tidy_count": 0,
        }

    def status(self):
        return {"backend": self.name, "up": True, "windows": len(self.state["windows"]),
                "free_mb": self.state["free_mb"], "downloads": self.state["downloads"]}

    def observe(self, kind, args):
        if kind == "windows":
            flt = (args.get("filter") or "").lower()
            wins = [dict(w, id=f"id-{t}") for t, w in self.state["windows"].items()
                    if not flt or flt in t.lower() or flt in w["title"].lower()]
            return {"kind": "windows", "count": len(wins), "windows": wins, "digest": digest(wins)}
        if kind == "tree":
            win = args.get("window")
            els = {k: v for k, v in self.state["elements"].items() if not win or v["window"] == win}
            return {"kind": "tree", "window": win, "elements": len(els), "digest": digest(els)}
        if kind == "capture":
            win = args.get("window", "screen")
            img = _tiny_png(f"{win}|{digest(self.state)}".encode())
            return {"kind": "capture", "window": win, "img": img, "img_sha": hashlib.sha256(img).hexdigest(),
                    "bytes": len(img), "width": 8, "height": 8, "digest": digest(self.state)}
        if kind == "wait_for":
            cond = args.get("condition", "")
            hit = cond in ("playing", ) and self.state["playing"]
            hit = hit or (cond.startswith("free_mb>") and self.state["free_mb"] > int(cond.split(">")[1]))
            hit = hit or cond in ("idle", "tidied")
            return {"kind": "wait_for", "condition": cond, "met": bool(hit), "digest": digest([cond, hit])}
        return {"kind": kind, "digest": digest(self.state), "state": dict(self.state)}

    def run(self, action, args):
        s = self.state
        if action == "invoke":
            el = s["elements"].get(_elkey(args["element"]))
            if not el or not el["enabled"]:
                raise Refused("element-unavailable", f"{args['element']} is absent or disabled — look first")
            if el["name"] == "Play":
                s["playing"] = not s["playing"]
            s["log"].append(f"invoke {el['name']}")
            return {"invoked": el["name"], "playing": s["playing"]}
        if action == "set_text":
            el = s["elements"].get(_elkey(args["element"]))
            if not el:
                raise Refused("element-unavailable", args["element"])
            s["values"][_elkey(args["element"])] = args["text"]
            s["log"].append(f"set_text {el['name']}={len(args['text'])}c")
            return {"set": el["name"], "chars": len(args["text"])}
        if action == "key_chord":
            s["log"].append(f"keys {args['keys']}")
            return {"sent": args["keys"]}
        if action == "launch":
            s["windows"][f"id-{len(s['windows'])+1}"] = {"title": f"{args['app']} (sim)", "pid": 5000 + len(s["windows"]),
                                                          "minimized": False}
            s["log"].append(f"launch {args['app']}")
            return {"launched": args["app"]}
        if action == "open_url":
            s["log"].append(f"open {args['url']}")
            return {"opened": args["url"]}
        if action == "save_as":
            s["log"].append(f"save {args['path']}")
            return {"saved": args["path"], "bytes": 1234}
        if action == "tidy":
            kind = args["kind"]
            if kind not in TIDY_KINDS:
                raise Refused("bad-tidy-kind", f"one of {TIDY_KINDS}")
            s["tidy_count"] += 1
            if kind == "memory":
                s["free_mb"] += 800
            elif kind == "close-idle":
                for wid, w in list(s["windows"].items()):
                    if w["minimized"]:
                        s["idle_closed"] += 1
                        s["free_mb"] += 300
                        del s["windows"][wid]
            else:
                s["downloads"] = max(0, s["downloads"] - 3)
                s["temp_cleared_mb"] += 40
                s["free_mb"] += 40
            s["log"].append(f"tidy {kind}")
            return {"tidied": kind, "free_mb": s["free_mb"]}
        if action in ("upload_file", "set_metadata", "read_published_url", "send_email", "submit_form"):
            s["log"].append(f"{action} {args}")
            return {"permitted": action, "note": "sim: no real side effect"}
        raise Refused("backend-refused", f"sim has no handler for {action}")


def _elkey(ref) -> str:
    """`id-1-0` and `1-0` are the same element to the sim. Accepting both costs four lines and prevents
    the most demoralising failure an agent can have: a correct id that resolves to nothing."""
    r = str(ref or "")
    return r[3:] if r.startswith("id-") else r


def _tiny_png(payload: bytes) -> bytes:
    """8x8 grey PNG whose pixels come from payload — a real image, cheap and deterministic."""
    px = bytes(payload[i % len(payload)] if payload else 0 for i in range(64))
    raw = b"".join(b"\x00" + px[r * 8:r * 8 + 8] for r in range(8))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# -------------------------------------------------------------------- fence ---
class Fence:
    """Who is allowed to move the mouse. The world decides; we only enforce its answer."""
    def __init__(self, mode="world", root=None, token=None, generation=None, lock_file=None):
        self.mode, self.root = mode, (Path(root) if root else None)
        # "static" is a fake world for CI, and a fake is only useful if it can go stale on purpose:
        # --lock-file points at a JSON {token, generation} that any test may edit between calls.
        self.lock_file = Path(lock_file) if lock_file else None
        self.static = None if mode != "static" else {"token": token, "generation": generation}

    def check(self, token, generation):
        if self.mode == "off":
            return {"ok": True, "note": "fence disabled (tests only)"}
        if token is None or generation is None:
            raise Refused("no-lock", "an action needs --token and --generation from the PC grant")
        if self.mode == "static":
            if self.lock_file is not None:
                if not self.lock_file.exists():
                    raise Refused("fence-unreachable", f"no lock file at {self.lock_file}")
                try:
                    cur = json.loads(self.lock_file.read_text())
                except Exception as e:
                    raise Refused("fence-unreachable", f"unreadable lock file: {e}")
                self.static = {"token": cur.get("token"), "generation": cur.get("generation")}
            try:
                want, have = int(self.static["generation"]), int(generation)
            except (TypeError, ValueError):
                raise Refused("bad-lock", "--generation must be the integer from world/lock.json")
            if want != have:
                raise Refused("stale-generation" if have < want else "wrong-token",
                              "the world's grant moved; re-read it, do not retry blindly (§7.5)")
            if token != self.static["token"]:
                raise Refused("wrong-token", "this machine no longer holds the PC lease")
            return {"ok": True, "generation": generation}
        root = self.root or Path(__file__).resolve().parents[1]
        tool = Path(root) / "tools" / "world.py"
        if not tool.exists():                                     # a missing referee is not a verdict
            raise Refused("fence-unreachable", f"nothing to ask at {tool} — the fence cannot be checked, so nothing acts")
        try:
            # --no-push, never a "local" flag: world.py has no such option, and a parser error
            # must not be allowed to masquerade as the world's verdict.
            p = subprocess.run([sys.executable, str(tool), "--root", str(root), "--no-push",
                                "pc", "check-token", "--token", str(token),
                                "--generation", str(generation)], capture_output=True, text=True, timeout=30)
        except Exception as e:                                    # fail closed, loudly
            raise Refused("fence-unreachable", f"{type(e).__name__}: {e}")
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0:
            # world.py answers a bad grant with its dedicated "stale" exit code (5) and everything
            # else — a parser error, a broken checkout, a bad JSON — as a fault. We copy that line
            # exactly: a broken referee must never be reported as "your grant is stale", because the
            # first means "stop and be seen" and the second tells the agent to go re-acquire and retry.
            line = out.splitlines()[-1:] or ["the world gave no answer"]
            if p.returncode == EXIT_WORLD_STALE:
                raise Refused("stale-generation", " ".join(str(x) for x in line))
            raise Refused("fence-unreachable", f"world.py exited {p.returncode}: {line[0]}")
        return {"ok": True, "raw": out[:200]}


# --------------------------------------------------------------------- core ---
class Core:
    # The knobs below are the Director's per-task calls (§7.3, §7.4, §7.7). None of them can
    # relax §7.1: the three refusals are not configurable, by design.
    def __init__(self, log_dir, backend, fence, ask="important", hold_timeout_min=HOLD_TIMEOUT_MIN_DEFAULT,
                 tidy_max_per_hour=TIDY_MAX_PER_HOUR_DEFAULT, capture_max_width=CAPTURE_MAX_WIDTH_DEFAULT):
        self.log_dir, self.backend, self.fence = Path(log_dir), backend, fence
        self.ask, self.hold_timeout_min = ask, hold_timeout_min
        self.tidy_max_per_hour, self.capture_max_width = tidy_max_per_hour, capture_max_width
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.pause_file = self.log_dir / "PAUSE"
        # Pause has to survive the process: `bridge.py pause` and `bridge.py act` are different
        # invocations, and a halt that resets itself on the next command is not a halt (§7.7).
        self.paused = self.pause_file.exists()
        self.pause_reason = self.pause_file.read_text().strip() if self.paused else ""

    # ---------- chain ----------
    @property
    def log_file(self):
        return self.log_dir / "receipts.jsonl"

    def _last(self):
        if not self.log_file.exists():
            return 0, None
        lines = [l for l in self.log_file.read_text().splitlines() if l.strip()]
        if not lines:
            return 0, None
        try:
            r = json.loads(lines[-1])
            return int(r.get("seq", 0)), r.get("sha")
        except Exception:
            raise Refused("chain-unreadable", "refusing to append to a log we cannot verify")

    def append(self, rec):
        seq, prev = self._last()
        rec = dict(rec, schema=SCHEMA, seq=seq + 1, prev=prev, ts=z())
        body = {k: rec[k] for k in ("seq", "ts", "action", "task", "state")}
        rec["sha"] = hashlib.sha256(f"{rec['seq']}|{rec['ts']}|{rec['action']}|{rec['task']}|{rec['state']}".encode()).hexdigest()[:16]
        rec["_body_digest"] = digest(body)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        return rec

    def outcomes(self, last=50, verify_chain=False):
        if not self.log_file.exists():
            return {"count": 0, "receipts": [], "chain": None if not verify_chain else {"ok": True, "checked": 0}}
        rows = [json.loads(l) for l in self.log_file.read_text().splitlines() if l.strip()]
        out = {"count": len(rows), "receipts": rows[-last:]}
        if verify_chain:
            prev, bad = None, []
            for r in rows:
                want = hashlib.sha256(f"{r['seq']}|{r['ts']}|{r['action']}|{r['task']}|{r['state']}".encode()).hexdigest()[:16]
                if want != r.get("sha"):
                    bad.append({"seq": r["seq"], "why": "digest-mismatch"})
                if r.get("prev") != prev:
                    bad.append({"seq": r["seq"], "why": "chain-break"})
                prev = r.get("sha")
            out["chain"] = {"ok": not bad, "checked": len(rows), "broken": bad[:5]}
        return out

    # ---------- policy ----------
    @staticmethod
    def _refuse_if_forbidden(action, args):
        blob = " ".join([action] + [f"{k}={v}" for k, v in args.items()])
        for rid, rx in REFUSALS:
            m = rx.search(blob)
            if m:
                raise Refused(rid, f"'{m.group(0)}' matched refusal '{rid}' — see docs/BRIDGE.md §7.1")

    def _validate(self, action, args):
        spec = ACTIONS.get(action)
        if spec is None:
            raise Refused("not-allowlisted", f"'{action}' is not a bridge verb; refuse is by name (§7.1)")
        cls, perm, req, opt = spec
        missing = [k for k in req if k not in args or args[k] in (None, "")]
        if missing:
            raise Refused("bad-args", f"{action} needs {missing}")
        extra = [k for k in args if k not in req and k not in opt]
        if extra:
            raise Refused("bad-args", f"{action} has no field(s) {extra} — a typo must not ride along to the OS")
        return cls, perm

    def _needs_approval(self, perm):
        if self.ask == "none":
            return False
        if self.ask == "all":
            return True
        return perm == "important"

    def _tidy_budget(self, args):
        if args.get("_ignore_budget"):
            return
        cut = now() - dt.timedelta(minutes=60)
        n = 0
        if self.log_file.exists():
            for l in self.log_file.read_text().splitlines():
                try:
                    r = json.loads(l)
                except Exception:
                    continue
                if r.get("action") == "tidy" and (r.get("state") == "done") and str(r.get("ts", "")) >= cut.strftime("%Y-%m-%dT%H:%M:%SZ"):
                    n += 1
        if n >= self.tidy_max_per_hour:
            raise Refused("tidy-budget", f"{n} tidies in the last hour; a tidy loop is a stuck loop — report it")

    # ---------- act ----------
    def _auto_pause(self):
        """§7.7 — an approval nobody answers is not permission to wait quietly; it halts."""
        for pid, item in self._pending().items():
            age = _age_min(item.get("asked_ts"))
            if age is not None and age > self.hold_timeout_min:
                self.pause(f"approval {pid} unacknowledged for {age:.0f} min (§7.7)")
                return True
        return False

    def act(self, action, args, token=None, generation=None, task=None, approval=None):
        self._auto_pause()
        args = {k: v for k, v in (args or {}).items() if not k.startswith("_")}
        # scope travels with the call, not with the verb: lift it out *before* validating, so a
        # write's `task` can never be read as a stray field on an action that has no such arg.
        task = task or args.pop("task", None)
        cls, perm = self._validate(action, args)
        self._refuse_if_forbidden(action, args)
        if cls == "write" and not task:
            raise Refused("no-scope", "a write must name the card it serves (--task t-…); "
                                      "unattributable actions cannot be reviewed (§7.2)")
        if action == "tidy":
            self._tidy_budget(args)
            tgt = str(args.get("target", ""))
            if DIRTY_TIDY.search(tgt):
                raise Refused("tidy-elsewhere", f"'{tgt}' is somebody else's work or the record — tidy the machine, not the colony (§7.4)")
        self.fence.check(token, generation)
        if not getattr(self.backend, "available", False):
            # before any approval gate: asking a human to approve an action on a machine that is
            # not connected is worse than refusing it — the yes would be spent on nothing.
            raise Refused("backend-unreachable", f"backend {self.backend.name} is not connected")
        if cls == "read":
            # A read never reaches the executor and is never gated by fence, scope or approval:
            # looking cannot mutate, and the lens is how the human checks on a paused machine.
            # It does still need a live connection — an unverified view is worse than none.
            obs = self.backend.observe(action, args)
            # `--save-to` is honoured here, at the one boundary that decides what may leave the machine,
            # rather than inside a backend: a flag that is accepted and ignored is how an agent reports
            # "sent you the frame" having sent nothing.
            save_to = args.get("save_to")
            if save_to and obs.get("img"):
                p_ = Path(save_to).expanduser()
                p_.parent.mkdir(parents=True, exist_ok=True)
                p_.write_bytes(obs["img"])
                obs = dict(obs, saved_to=str(p_), saved_bytes=len(obs["img"]))
            return self.append({"action": action, "task": task or "-", "args_digest": digest(args),
                                "perm": perm, "class": "read", "backend": self.backend.name,
                                "approval": "none-required", "result": _obs(obs),
                                "before": None, "after": _obs(obs), "state": "observed"})
        if self.paused:
            raise Refused("paused", self.pause_reason or "the human said stop; a resume is a decision, not a retry")
        if cls == "write" and self._needs_approval(perm) and not approval:
            return self._park(action, args, token, generation, task)
        before = self.backend.observe("capture", args)
        result = self.backend.run(action, args)
        after = self.backend.observe("capture", args)
        # ok:true must mean the machine changed, not that a command ran. Same digest on both
        # sides of a write is exactly the silent-misclick failure mode BRIDGE.md §7.1 describes.
        state = "done"
        if before.get("digest") is not None and before.get("digest") == after.get("digest"):
            state = "done-unverified"
        rec = self.append({"action": action, "task": task or "-", "args_digest": digest(args), "perm": perm,
                           "class": cls, "backend": self.backend.name, "approval": approval or "auto",
                           "before": _obs(before), "after": _obs(after), "result": result, "state": state})
        return rec

    def _park(self, action, args, token, generation, task):
        pend = self.log_dir / "pending.json"
        cur = json.loads(pend.read_text()) if pend.exists() else {}
        pid = f"apr-{now().strftime('%Y%m%d')}-{len(cur) + 1:02d}"
        cur[pid] = {"action": action, "args": args, "token": token, "generation": generation, "task": task,
                    "asked_ts": z()}
        pend.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n")
        age = _age_min(cur[pid]["asked_ts"])
        if age is not None and age > self.hold_timeout_min:
            self.pause(f"approval {pid} unacknowledged for {age:.0f} min (§7.7)")
        return {"state": "pending-approval", "id": pid, "action": action, "task": task,
                "ask": f"chatcode dm <director> \"approve {pid}: {action} for {task}\" (bridge.py approve {pid})"}

    # ---------- approval ----------
    def _pending(self):
        p = self.log_dir / "pending.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save_pending(self, d):
        p = self.log_dir / "pending.json"
        if d:
            p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        elif p.exists():
            p.unlink()

    def approve(self, pid, by="director"):
        d = self._pending()
        if pid not in d:
            raise Refused("unknown-approval", f"{pid} is not pending (it may have expired or been taken)")
        item = d.pop(pid)
        self._save_pending(d)
        rec = self.act(item["action"], item["args"], token=item["token"], generation=item["generation"],
                       task=item["task"], approval=f"approved:{by}")
        return rec

    def deny(self, pid, reason=""):
        d = self._pending()
        if pid not in d:
            raise Refused("unknown-approval", pid)
        item = d.pop(pid)
        self._save_pending(d)
        return self.append({"action": item["action"], "task": item.get("task", "-"), "state": "denied",
                            "perm": "-", "class": "write", "backend": self.backend.name,
                            "approval": f"denied:{reason or 'no reason given'}", "args_digest": digest(item["args"])})

    # ---------- self-test (PCB-01: the "quick and light" connection probe) ----------
    def selftest(self):
        """A deterministic tour of the whole wire, in seconds, with no agent at the wheel.

        Deliberately not a success meter. Every check says what it proved, and a sim backend passes the
        policy checks while *failing* the "is this a real desktop" check on purpose — a self-test that
        goes green on a pretend machine is the exact false confidence this repo refuses (the same logic
        as `done-unverified`, §7.5). Three verdicts, not two: PASS, PASS-POLICY-ONLY, FAIL.
        """
        checks, notes = [], []
        def chk(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        real = self.backend.name == "windows"
        # informational, never a failure: on the sim this line is the *explanation* of the verdict, and
        # a check that can only go red in a mode where red is expected trains people to ignore red.
        if real:
            ver = getattr(getattr(self.backend, "t", None), "auto", None)
            ver = getattr(ver, "__version__", "unknown") if ver else "unknown"
            missing = []
            try:
                import winuia
                missing = winuia.UiaTransport.probe(self.backend.t.auto) if ver != "unknown" else []
            except Exception as e:
                missing = [f"probe failed: {e}"]
            chk("library-shape", not missing,
                f"uiautomation {ver}" + ("" if not missing else " — missing: " + ", ".join(missing)))
        chk("fence-live", (not real) or self.fence.mode != "off",
            f"fence={self.fence.mode}" + ("" if self.fence.mode != "off" or not real else
            " — a real desktop driven without the world's lease check is a remote control, not a bridge"))
        chk("backend", True, f"backend={self.backend.name}"
            + ("" if real else " — the sim proves the gates, not the desktop"))

        # each §7.1 refusal is *exercised through act()*, because a rule that is only configured is not
        # a rule: if the policy layer is ever bypassed, this goes red on the PC before a task runs.
        for action, args, want in (("key_chord", {"keys": "ctrl+alt+del", "task": "t-selftest"}, "not-a-verb"),
                                  ("launch", {"app": "runas", "task": "t-selftest"}, "elevated-action"),
                                  ("set_text", {"element": "id-0-0", "text": "read ~/.private/crew.env",
                                               "task": "t-selftest"}, "credential-access"),
                                  ("open_url", {"url": "https://github.com/dreamer2664/captchaproject",
                                                "task": "t-selftest"}, "forbidden-target")):
            got = None
            try:
                self.act(action, dict(args))
            except Refused as r:
                got = r.reason
            chk(f"refusal:{want}", got == want, got or "the bridge DID NOT REFUSE")
        got = None
        try:
            self.act("invoke", {"element": "id-0-0"})
        except Refused as r:
            got = r.reason
        chk("refusal:no-scope", got == "no-scope", got or "an unattributable write ran")

        first = None
        try:
            wins = self.backend.observe("windows", {})
            items = wins.get("windows") or [{"id": k} for k in (getattr(self.backend, "state", {})
                                                                .get("windows", {}))]
            first = (items[0].get("id") if items else None) or "screen"
            chk("observe:windows", wins.get("count", len(items)) > 0, f"{wins.get('count', len(items))} windows, first={first}")
        except Exception as e:
            chk("observe:windows", False, f"{type(e).__name__}: {e}")
        for probe, args in (("tree", {"window": first}), ("capture", {"window": first})):
            try:
                obs = self.backend.observe(probe, args)
                chk(f"observe:{probe}", obs is not None and obs.get("digest") is not None, digest(obs))
                if probe == "tree" and real and not obs.get("elements"):
                    notes.append("tree returned 0 elements on a real window — probable UIPI integrity "
                                 "mismatch, not an empty app")
                if probe == "capture" and obs.get("suspect"):
                    notes.append("capture flagged: " + obs["suspect"])
            except Exception as e:
                chk(f"observe:{probe}", False, f"{type(e).__name__}: {e}")
        try:
            img = self.backend.observe("capture", {"window": first or "screen"}).get("img") or b""
            chk("capture-under-ntfy-cap", 0 < len(img) <= CAPTURE_MAX_BYTES, f"{len(img)} bytes")
        except Exception as e:
            chk("capture-under-ntfy-cap", False, str(e))

        # write the receipt *then* verify, so the chain check covers a real append on this box
        try:
            self.append({"action": "selftest", "task": "-", "state": "observed", "perm": "-",
                         "class": "control", "backend": self.backend.name, "approval": "selftest",
                         "args_digest": "0" * 16, "result": {"checks": len(checks)}})
        except Exception as e:
            chk("receipt-append", False, f"{type(e).__name__}: {e}")
        else:
            out = self.outcomes(50, True)
            chk("receipt-chain-verifies", bool(out.get("chain", {}).get("ok")), f"{out['count']} receipts")

        passed = sum(1 for c in checks if c["ok"])
        all_ok = passed == len(checks)
        verdict = "PASS" if (all_ok and real) else ("PASS-POLICY-ONLY" if all_ok else "FAIL")
        return {"time": z(), "backend": self.backend.name, "passed": passed, "total": len(checks),
                "checks": checks, "notes": notes, "verdict": verdict,
                "meaning": {"PASS": "a real desktop answered the lens and every gate refused correctly",
                            "PASS-POLICY-ONLY": "the gates are proven; no machine was touched (sim backend)",
                            "FAIL": "do not run a task on this box — fix what is red first"}[verdict]}

    # ---------- human controls ----------
    def pause(self, reason="manual"):
        self.paused, self.pause_reason = True, reason
        self.pause_file.write_text(reason + "\n")
        self.append({"action": "pause", "task": "-", "state": "paused", "perm": "-", "class": "control",
                     "backend": self.backend.name, "approval": "human", "args_digest": digest(reason),
                     "error": reason})
        return {"state": "paused", "reason": reason}

    def resume(self, by="director"):
        self.paused, self.pause_reason = False, ""
        if self.pause_file.exists():
            self.pause_file.unlink()
        return self.append({"action": "resume", "task": "-", "state": "running", "perm": "-", "class": "control",
                            "backend": self.backend.name, "approval": f"human:{by}", "args_digest": digest(by)})

    def status(self):
        if self.paused != self.pause_file.exists():
            self.paused, self.pause_reason = self.pause_file.exists(), (
                self.pause_file.read_text().strip() if self.pause_file.exists() else "")
        d = self._pending()
        stale = [k for k, v in d.items() if (_age_min(v.get("asked_ts")) or 0) > self.hold_timeout_min]
        return {"time": z(), "backend": self.backend.status(), "paused": self.paused,
                "pause_reason": self.pause_reason or None, "ask": self.ask,
                "pending": list(d), "pending_stale_min": [round(_age_min(d[k]['asked_ts']) or 0, 1) for k in d],
                "auto_pause_due": stale, "hold_timeout_min": self.hold_timeout_min,
                "tidy_max_per_hour": self.tidy_max_per_hour, "fence": self.fence.mode,
                "receipts": (0 if not self.log_file.exists() else len(self.log_file.read_text().splitlines()))}


def _obs(o):
    if not o:
        return None
    out = {k: v for k, v in o.items() if k != "img"}
    if "img" in o:
        out["bytes"] = o.get("bytes")
        out["img_sha"] = o.get("img_sha")
    return out


def _age_min(iso):
    if not iso:
        return None
    try:
        t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None
    return (now() - t).total_seconds() / 60.0


# -------------------------------------------------------------------- CLI ---
def _refuse(reason: str, hint: str = "", **extra) -> int:
    """One place a refusal is formatted, so a caller (agent, script, phone bridge) never has to read
    two streams to learn whether the bridge said no. Exit code 1 is the same answer in mechanical form."""
    print(json.dumps(dict({"ok": False, "refused": reason, "hint": hint}, **extra),
                      sort_keys=True, ensure_ascii=False, default=str))
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bridge.py", description=__doc__.splitlines()[0])
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--backend", default="sim", choices=["sim", "null", "windows"])
    ap.add_argument("--fence", default=None, choices=["off", "static", "world"],
                    help="defaults to `world` on the real desktop and `off` for the sim; `off` + "
                         "--backend windows is refused, because a bridge that skips the lease check "
                         "is not a bridge, it is a remote control")
    ap.add_argument("--root", default=None, help="mars checkout root for --fence world")
    ap.add_argument("--token", default=None, help="fallback grant for approve/resume; act has its own")
    ap.add_argument("--generation", default=None, help="fallback generation for approve/resume")
    ap.add_argument("--lock-file", default=None, help="for --fence static only: a file a test can move")
    ap.add_argument("--ask", default="important", choices=["important", "all", "none"])
    ap.add_argument("--hold-timeout-min", type=int, default=HOLD_TIMEOUT_MIN_DEFAULT)
    ap.add_argument("--tidy-max-per-hour", type=int, default=TIDY_MAX_PER_HOUR_DEFAULT)
    g = ap.add_subparsers(dest="cmd", required=True)
    g.add_parser("status")
    g.add_parser("selftest", help="run the whole-wire connection probe and exit non-zero on failure")
    a = g.add_parser("act"); a.add_argument("action"); a.add_argument("--task"); a.add_argument("--why")
    # the grant travels with the call that needs it: an agent types `act ... --token`, not a flag
    # two positions earlier. (The top-level pair still serves approve/resume/tests.)
    a.add_argument("--token", default=None); a.add_argument("--generation", default=None, type=int)
    for f in ("window", "element", "text", "keys", "app", "args", "url", "path", "channel", "video",
              "fields", "to", "subject", "body", "attach", "form", "values", "kind", "target",
              "condition", "timeout_s", "region", "max_width", "save_to", "depth", "filter", "include"):
        # dash on the command line, underscore in the code. Declaring the underscore spelling as the
        # dest (and not as a flag) is what keeps `args.save_to` and `--save-to` the same thing.
        a.add_argument(f"--{f.replace('_', '-')}", dest=f, default=None)
    a.add_argument("--allow-budget-bypass", action="store_true", help=argparse.SUPPRESS)
    o = g.add_parser("outcomes"); o.add_argument("--last", type=int, default=50)
    o.add_argument("--verify-chain", action="store_true")
    g.add_parser("pending")
    p = g.add_parser("approve"); p.add_argument("id"); p.add_argument("--by", default="director")
    dn = g.add_parser("deny"); dn.add_argument("id"); dn.add_argument("--reason", default="")
    g.add_parser("pause"); rp = g.add_parser("resume"); rp.add_argument("--by", default="director")
    s = g.add_parser("serve"); s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.fence is None:
        # The safe default is chosen from what the bridge would act *on*, not from a global constant.
        # On the sim, no fence means fast tests; on a real desktop it would mean "any process holding
        # the repo can move Carlo's mouse", which is the exact failure §7.5 exists to prevent.
        args.fence = "world" if args.backend == "windows" else "off"
    if args.fence == "off" and args.backend == "windows":
        return _refuse("no-fence", "on the real machine the lease check is not optional: drop --fence off "
                       "and pass --token/--generation from `world.py pc acquire`")

    if args.backend == "windows":
        # the import is inside the factory on purpose: policy stays importable (and testable) on any
        # box, and a machine with no UIA fails here, at the boundary, with a reason.
        try:
            import winuia
            be = winuia.WindowsBackend()
        except Exception as e:
            return _refuse("backend-unreachable", str(e), backend="windows")
    else:
        be = NullBackend() if args.backend == "null" else SimBackend()

    core = Core(args.log_dir, be, Fence(args.fence, args.root, args.token, args.generation, args.lock_file), ask=args.ask,
                hold_timeout_min=args.hold_timeout_min, tidy_max_per_hour=args.tidy_max_per_hour)

    if args.cmd == "serve":
        return serve(core, args.port)

    a = vars(args)
    act_args = {k.replace("-", "_"): v for k, v in a.items()
                if k in {"window", "element", "text", "keys", "app", "args", "url", "path", "channel",
                         "video", "fields", "to", "subject", "body", "attach", "form", "values", "kind",
                         "target", "condition", "timeout_s", "region", "max_width", "save_to", "depth",
                         "filter", "include", "why"} and v is not None}
    try:
        if args.cmd == "status":
            out = core.status()
        elif args.cmd == "act":
            out = core.act(args.action, act_args, token=args.token, generation=args.generation, task=args.task)
        elif args.cmd == "outcomes":
            out = core.outcomes(args.last, args.verify_chain)
        elif args.cmd == "pending":
            out = core._pending()
        elif args.cmd == "approve":
            out = core.approve(args.id, args.by)
        elif args.cmd == "deny":
            out = core.deny(args.id, args.reason)
        elif args.cmd == "pause":
            out = core.pause()
        elif args.cmd == "resume":
            out = core.resume(args.by)
        elif args.cmd == "selftest":
            out = core.selftest()
            print(json.dumps(out, sort_keys=True, indent=2, default=str))
            return 0 if out["verdict"] != "FAIL" else 1
        elif args.cmd == "serve":
            return serve(core, args.port)
        else:
            raise Refused("unknown-command", args.cmd)
    except Refused as r:
        return _refuse(r.reason, r.hint)
    print(json.dumps(out, sort_keys=True, ensure_ascii=False, default=str))
    return 0


# ---------------------------------------------------------------- HTTP ------
def _handler_for(core: Core):
    """The localhost control surface, as a class so both serve() and the tests build the same thing."""
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _denied_origin(self):
            # Bound to 127.0.0.1 already, but a page in the very browser the agent drives must not
            # be able to click "resume" on the human's behalf.
            o = (self.headers.get("Origin") or self.headers.get("Referer") or "")
            return bool(o) and "127.0.0.1" not in o and "localhost" not in o

        def do_POST(self):
            if self._denied_origin():
                return self._send(403, {"ok": False, "refused": "not-localhost"})
            try:
                body = self._body()
            except Exception:
                return self._send(400, {"ok": False, "refused": "bad-json"})
            routes = {"/status": lambda: core.status(),
                      "/pause": lambda: core.pause(body.get("reason", "http")),
                      "/resume": lambda: core.resume(body.get("by", "director")),
                      "/approve": lambda: core.approve(body["id"]),
                      "/deny": lambda: core.deny(body["id"], body.get("reason", "")),
                      "/act": lambda: core.act(body.get("action"), body.get("args"), body.get("token"),
                                                body.get("generation"), body.get("task"))}
            fn = routes.get(self.path)
            if fn is None:
                return self._send(404, {"ok": False, "refused": "unknown-path"})
            try:
                return self._send(200, fn())
            except Refused as r:
                # 409, not 500: "refused on purpose" is an answer, not a fault
                return self._send(409, {"ok": False, "refused": r.reason, "hint": r.hint})
            except KeyError as e:
                return self._send(400, {"ok": False, "refused": "missing-field", "hint": str(e)})

        def log_message(self, *a):        # no request bodies in logs; captures are not traffic logs
            pass
    return Handler


def serve(core: Core, port: int):
    """localhost only. The pause button must not need a firewall to be safe."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(core))
    print(f"bridge: listening on http://127.0.0.1:{port} (backend={core.backend.name}, "
          f"ask={core.ask}, fence={core.fence.mode}, log={core.log_dir})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
