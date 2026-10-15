#!/usr/bin/env python3
"""tools/world.py — the colony's ONLY writer of world/ (docs/WORLD.md v1).

    python3 tools/world.py join --name nova --caps research,script
    python3 tools/world.py where
    python3 tools/world.py pc acquire --by nova --purpose "upload vid-... to ch-nova"
    python3 tools/world.py pc check-token --token <t> --generation <n>   # the executor's fence
    python3 tools/world.py status

Discipline (CHARTER §3, docs/WORLD.md §5): every command reads, decides, writes canonical
JSON, commits and pushes. A push rejection is not an error — it is information (someone
moved first):

  * retryable ops (pc acquire / renew / release / sweep / cancel) re-read the remote state
    and decide again. The push race IS the mutex.
  * once ops (join / move / propose / respond / decide / archive add) refuse (exit 3) if a
    file they touch changed remotely under them — never overwrite a stranger's change.

No network beyond git. No secrets, ever. stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
LOC_RE = re.compile(r"^(entrance|command|meeting|archive|production|studio:[a-z][a-z0-9-]{2,20})$")
MSG_TYPES = ("proposal", "question", "review-request", "assist", "disagreement", "evidence",
             "interface-change", "approval", "handoff", "decision", "report")
STAGES = ("concept-proposed", "concept-reviewed", "research", "script", "script-challenged",
          "assets", "assembled", "final-review", "publish-approved", "pc-requested",
          "uploaded", "verified", "recorded")
EXIT_OK, EXIT_ERR, EXIT_CONFLICT, EXIT_REFUSED, EXIT_STALE = 0, 1, 3, 4, 5


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def z(t: dt.datetime | None = None) -> str:
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def canonical(obj) -> bytes:
    """The one true byte form: sorted keys, 2-space indent, trailing newline."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def new_id(prefix: str) -> str:
    return f"{prefix}-{now().strftime('%Y%m%d')}-{secrets.token_hex(2)}"


class Refused(Exception):
    def __init__(self, msg, code=EXIT_REFUSED):
        super().__init__(msg)
        self.code = code


class Conflict(Exception):
    pass


class World:
    def __init__(self, root: Path, agent: str | None = None, push: bool = True):
        self.root = root.resolve()
        self.agent = agent
        self.push = push and (self.root / ".git").exists()
        self._origin = None
        if self.push:
            r = self._git("remote", "get-url", "origin", check=False)
            self._origin = r.stdout.strip() if r.returncode == 0 else None
            self.push = bool(self._origin)

    # ------------------------------------------------------------ git ----
    def _git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise Refused(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:200]}",
                          EXIT_ERR)
        return r

    def fetch(self):
        if not self.push:
            return None
        self._git("fetch", "-q", "origin", "main", check=False)
        r = self._git("rev-parse", "FETCH_HEAD", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def remote_bytes(self, rel: str):
        """The remote's copy of world/<rel> at FETCH_HEAD (None if absent)."""
        if not self.push:
            return self.read_bytes(rel)
        r = self._git("show", f"FETCH_HEAD:world/{rel}", check=False)
        return r.stdout.encode() if r.returncode == 0 else None

    # -------------------------------------------------------- world io ----
    def path(self, rel: str) -> Path:
        return self.root / "world" / rel

    def read_bytes(self, rel: str):
        p = self.path(rel)
        return p.read_bytes() if p.is_file() else None

    def read_json(self, rel: str, default=None):
        b = self.read_bytes(rel)
        if b is None:
            return default
        try:
            return json.loads(b)
        except json.JSONDecodeError as e:
            raise Refused(f"world/{rel} is not valid JSON: {e}", EXIT_ERR)

    def write_json(self, rel: str, obj) -> bytes:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = canonical(obj)
        p.write_bytes(data)
        return data

    def brief(self, obj: dict, keys: tuple) -> dict:
        return {k: obj.get(k) for k in keys if k in obj}

    # ------------------------------------------------------- publishing ---
    def _commit(self, rels, message: str):
        self._git("add", "--", *[f"world/{r}" for r in rels])
        who = self.agent or "world-tool"
        # tolerant: "nothing staged" is not a git error we should die on
        self._git("-c", f"user.name={who}", "-c", f"user.email={who}@mars.local",
                  "commit", "-q", "-m", message, check=False)

    def publish(self, touched: dict[str, object], message: str) -> str:
        """touched: rel -> the bytes we based our write on (None = file was new).

        Returns "ok" (pushed / local-only) or raises Conflict after restoring the
        local files to their pre-write bytes."""
        if not self.push:
            return "ok"
        self._commit(list(touched), message)
        for attempt in range(4):
            r = self._git("push", "-q", "origin", "HEAD:main", check=False)
            if r.returncode == 0:
                return "ok"
            self.fetch()
            # does any file we touched exist remotely with different bytes?
            for rel, base in touched.items():
                rb = self.remote_bytes(rel)
                if rb is not None and rb != base:
                    self._uncommit(touched, base)
                    raise Conflict(f"world/{rel} changed remotely under you — re-read and retry")
            self._git("rebase", "-q", "FETCH_HEAD", check=False)
            if self._git("rebase", "--abort", check=False).returncode == 0:
                self._uncommit(touched, base=None, ref="FETCH_HEAD")
                raise Conflict("could not rebase over the remote — re-read and retry")
        raise Conflict("push kept losing the race — re-read and retry")

    def _uncommit(self, touched: dict, base, ref="HEAD~1"):
        self._git("reset", "-q", "--mixed", ref, check=False)
        for rel, b in touched.items():
            p = self.path(rel)
            if isinstance(base, dict):
                b = base.get(rel)
            if b is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b)

    def publish_retry(self, decide, message: str, tries: int = 30):
        """For the lock: decide() reads the remote state, returns (obj, base_bytes) or
        raises Refused; on a lost push race we drop our attempt, wait a jittered moment
        (so five simultaneous colonies do not move in lockstep) and decide again."""
        for attempt in range(tries):
            if attempt:
                time.sleep(random.uniform(0.02, 0.20))
            self.fetch()
            obj, base = decide()
            rel = "lock.json"
            self.write_json(rel, obj)
            try:
                self.publish({rel: base}, message)
                return obj
            except Conflict:
                continue  # remote moved; loop re-reads and re-decides
        raise Refused("the lock kept moving — try again", EXIT_ERR)

    # ------------------------------------------------------------- lock ---
    def lock_now(self, src=None):
        lock = self.read_json("lock.json") if src is None else src
        if lock is None:
            lock = {"schema": SCHEMA, "state": "free", "holder": None, "purpose": "",
                    "token": None, "generation": 0, "since_ts": None, "expires_ts": None,
                    "renew_count": 0, "max_hold_min": 180, "queue": [], "history": []}
        # effective state: an expired hold is recovering, never silently free
        exp = parse_ts(lock.get("expires_ts"))
        if lock.get("state") == "held" and exp and exp < now():
            lock = dict(lock, state="recovering")
        return lock

    def _remote_lock_b(self):
        """The remote lock AND the bytes we based our decision on (base bytes).

        Retry discipline: publish() compares the *remote now* against these base
        bytes. If they match, our commit rebases cleanly and lands; if not, we
        drop our attempt and decide again — never overwrite a race we lost."""
        b = self.remote_bytes("lock.json")
        if b is None:
            return self.lock_now(), None
        try:
            return self.lock_now(json.loads(b)), b
        except json.JSONDecodeError:
            raise Refused("remote lock.json is unreadable — refusing to guess", EXIT_ERR)

    def _remote_lock(self):
        return self._remote_lock_b()[0]

    def _hist(self, lock, event, by, gen=None, note=""):
        lock.setdefault("history", []).append(
            {"ts": z(), "event": event, "by": by, "generation": gen if gen is not None else lock.get("generation", 0),
             **({"note": note} if note else {})})
        lock["history"] = lock["history"][-50:]

    def pc_gate(self, by):
        """CHARTER §7 gate for pc acquire (t-20261015-3941):
        a populated colony refuses ghosts; a registered colonist needs the 'pc'
        cap (a manager grants it via `authorize`). Empty colonies (fresh worlds,
        the lock suite's fixtures) stay open — there is no one to gate for."""
        d = self.root / "world" / "agents"
        if not d.is_dir() or not any(d.glob("*.json")):
            return
        a = self.read_json(f"agents/{by}.json")
        if a is None:
            raise Refused(f"'{by}' is not a colonist of this world — join first "
                          "(docs/ENTRANCE.md, world.py join --name ...)", EXIT_REFUSED)
        if "pc" not in (a.get("caps") or []):
            raise Refused("PC access is not included by default — a manager must authorize it: "
                          f"world.py authorize {by} --by <manager> (docs/ENTRANCE.md)",
                          EXIT_REFUSED)

    def pc_acquire(self, by, purpose, ttl=None):
        def decide():
            lock, base = self._remote_lock_b()
            self.pc_gate(by)
            ttlmin = ttl or 30
            if lock["state"] == "recovering":
                raise Refused("PC is recovering from an expired session — sweep first "
                              "(world.py pc sweep --by <you>)", EXIT_REFUSED)
            if lock["state"] == "free":
                lock.update(state="held", holder=by, purpose=purpose,
                            token=secrets.token_hex(8), generation=lock["generation"] + 1,
                            since_ts=z(), expires_ts=z(now() + dt.timedelta(minutes=ttlmin)),
                            renew_count=0)
                self._hist(lock, "grant", by)
                return lock, base
            if lock.get("holder") == by:
                raise Refused("you already hold the PC — renew or release it", EXIT_REFUSED)
            q = lock.setdefault("queue", [])
            if any(e.get("agent") == by for e in q):
                raise Refused("you are already queued", EXIT_REFUSED)
            q.append({"agent": by, "purpose": purpose, "since_ts": z()})
            self._hist(lock, "queued", by)
            return lock, base

        lock = self.publish_retry(decide, f"pc: {by} acquires ({purpose[:60]})")
        if lock.get("holder") == by:
            print(f"GRANTED generation={lock['generation']} token={lock['token']} "
                  f"expires={lock['expires_ts']}")
        else:
            pos = next(i + 1 for i, e in enumerate(lock["queue"]) if e["agent"] == by)
            print(f"QUEUED position={pos}")
        return EXIT_OK

    def pc_renew(self, by, token, ttl=None):
        def decide():
            lock, base = self._remote_lock_b()
            if lock["state"] != "held" or lock.get("holder") != by or lock.get("token") != token:
                raise Refused("renew refused: you are not the current holder (or the token is stale)",
                              EXIT_STALE)
            since = parse_ts(lock.get("since_ts"))
            held_min = (now() - since).total_seconds() / 60 if since else 0
            if held_min > lock.get("max_hold_min", 180):
                raise Refused(f"renew refused: max hold ({lock.get('max_hold_min')} min) reached — "
                              "release, or the manager records an explicit extension", EXIT_REFUSED)
            lock.update(expires_ts=z(now() + dt.timedelta(minutes=ttl or 30)),
                        renew_count=lock.get("renew_count", 0) + 1)
            self._hist(lock, "renew", by)
            return lock, base

        lock = self.publish_retry(decide, f"pc: {by} renews")
        print(f"RENEWED generation={lock['generation']} expires={lock['expires_ts']}")
        return EXIT_OK

    def pc_release(self, by, token):
        def decide():
            lock, base = self._remote_lock_b()
            if lock["state"] == "held" and (lock.get("holder") != by or lock.get("token") != token):
                raise Refused("release refused: not the holder / stale token", EXIT_STALE)
            self._hist(lock, "release", by)
            q = lock.get("queue") or []
            if q:
                nxt = q.pop(0)
                lock.update(state="held", holder=nxt["agent"], purpose=nxt.get("purpose", ""),
                            token=secrets.token_hex(8), generation=lock["generation"] + 1,
                            since_ts=z(), expires_ts=z(now() + dt.timedelta(minutes=30)),
                            renew_count=0)
                self._hist(lock, "grant", nxt["agent"], note="from queue")
            else:
                lock.update(state="free", holder=None, purpose="", token=None,
                            since_ts=None, expires_ts=None, renew_count=0)
            return lock, base

        lock = self.publish_retry(decide, f"pc: {by} releases")
        if lock["state"] == "free":
            print(f"RELEASED generation={lock['generation']} (PC free)")
        else:
            print(f"RELEASED generation={lock['generation']} — next holder: {lock['holder']}")
        return EXIT_OK

    def pc_sweep(self, by):
        def decide():
            lock, base = self._remote_lock_b()
            if lock["state"] == "held":
                raise Refused("nothing to sweep: a live holder has the PC "
                              "(expiry sweeps it automatically for the next writer)", EXIT_REFUSED)
            q = lock.get("queue") or []
            self._hist(lock, "sweep", by)
            if q:
                nxt = q.pop(0)
                lock.update(state="held", holder=nxt["agent"], purpose=nxt.get("purpose", ""),
                            token=secrets.token_hex(8), generation=lock["generation"] + 1,
                            since_ts=z(), expires_ts=z(now() + dt.timedelta(minutes=30)),
                            renew_count=0)
                self._hist(lock, "grant", nxt["agent"], note="from queue after sweep")
            else:
                lock.update(state="free", holder=None, purpose="", token=None,
                            since_ts=None, expires_ts=None, renew_count=0)
            return lock, base

        lock = self.publish_retry(decide, f"pc: {by} sweeps a dead session")
        print(f"SWEPT — state={lock['state']} holder={lock.get('holder')} generation={lock['generation']}")
        return EXIT_OK

    def pc_cancel(self, by):
        def decide():
            lock, base = self._remote_lock_b()
            q = lock.get("queue") or []
            if not any(e["agent"] == by for e in q):
                raise Refused("you are not in the queue", EXIT_REFUSED)
            lock["queue"] = [e for e in q if e["agent"] != by]
            self._hist(lock, "cancel", by)
            return lock, base

        self.publish_retry(decide, f"pc: {by} leaves the queue")
        print("CANCELLED")
        return EXIT_OK

    def pc_status(self):
        lock = self._remote_lock()
        print(f"PC state={lock['state']} holder={lock.get('holder') or '-'} "
              f"generation={lock['generation']}")
        if lock["state"] == "held":
            print(f"  purpose: {lock.get('purpose')} (since {lock.get('since_ts')}, "
                  f"expires {lock.get('expires_ts')})")
        for i, e in enumerate(lock.get("queue") or []):
            print(f"  queue {i + 1}: {e['agent']} — {e.get('purpose', '')} (since {e.get('since_ts')})")
        return EXIT_OK

    def pc_check_token(self, token, generation):
        lock = self.lock_now()
        if lock["state"] != "held":
            print(f"TOKEN-REFUSED reason=no-live-holder state={lock['state']}")
            return EXIT_STALE
        if lock.get("token") != token or int(lock.get("generation", -1)) != int(generation):
            print(f"TOKEN-REFUSED reason=stale (live generation={lock['generation']}, "
                  f"holder={lock['holder']})")
            return EXIT_STALE
        print(f"TOKEN-OK generation={lock['generation']} holder={lock['holder']}")
        return EXIT_OK

    # -------------------------------------------------------- membership ---
    CHARTER_DIGEST = (
        "the map is a window, never a source — world/ and the bus hold the truth.",
        "no simulation: quiet is honest; every change attributable (who/what/when/why).",
        "one writer per file; tools/world.py is the only writer of world/.",
        "work is cards on the empire board; reviews are ballots; the record is append-only.",
        "one central PC, the Director's machine: one holder, fenced by token+generation.",
    )

    def _managers(self) -> list[str]:
        d = self.root / "world" / "agents"
        out = []
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                a = json.loads(f.read_text())
            except Exception:
                continue
            if a.get("role") == "manager":
                out.append(a.get("id") or f.stem)
        return out

    def _projects_line(self) -> str:
        d = self.root / "world"
        chans = list((d / "channels").glob("*.json")) if (d / "channels").is_dir() else []
        videos = list((d / "videos").glob("*.json")) if (d / "videos").is_dir() else []
        live, inflight = 0, 0
        for f in chans:
            try:
                if json.loads(f.read_text()).get("status") == "live":
                    live += 1
            except Exception:
                pass
        for f in videos:
            try:
                if json.loads(f.read_text()).get("stage_name") != "recorded":
                    inflight += 1
            except Exception:
                pass
        return f"{live} live channel(s), {inflight} video(s) in flight"

    def _open_mars_cards(self) -> list[str]:
        """Open empire cards tagged 'mars', read from the sibling chatcode checkout."""
        root = os.environ.get("MARS_CHATCODE") or str(self.root.parent / "chatcode")
        tdir = Path(root) / "board" / "tasks"
        out = []
        if not tdir.is_dir():
            return [f"(chatcode checkout not found at {tdir} — open cards unreadable here)"]
        for f in sorted(tdir.glob("*.json")):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if "mars" in (d.get("tags") or []) and d.get("status") == "open":
                out.append(f"{d['id']} p{d.get('priority')} {d.get('title','')[:70]}")
        return out or ["(none open)"]

    def _post_entrance(self, text: str) -> tuple[str | None, str]:
        """Post ONE real entrance message to the empire bus; returns (bus id, warning)."""
        bin_path = Path(os.environ.get("MARS_CHATCODE_BIN")
                        or (Path(os.environ.get("MARS_CHATCODE") or self.root.parent / "chatcode")
                            / "bin" / "chatcode"))
        if not bin_path.is_file():
            return None, f"chatcode CLI not found at {bin_path}"
        if not os.environ.get("CHATCODE_TOKEN"):
            return None, "CHATCODE_TOKEN not set in this environment"
        try:
            r = subprocess.run([sys.executable, str(bin_path), "say", text, "--type", "entrance"],
                               capture_output=True, text=True, timeout=90)
        except Exception as e:
            return None, f"bus post failed to run: {e}"
        m = re.search(r"#([0-9a-f]{6,12})\b", r.stdout or "")
        if r.returncode == 0 and m:
            return m.group(1), ""
        return None, f"bus post rc={r.returncode}: {(r.stderr or r.stdout).strip()[:160]}"

    def join(self, name, caps=None, role="colonist"):  # noqa: C901 — the sitting, on purpose
        if not NAME_RE.match(name):
            raise Refused("name must be 3-21 chars: lowercase letters, digits, dashes", EXIT_ERR)
        if self.read_bytes(f"agents/{name}.json") is not None:
            raise Refused(f"the name '{name}' is already claimed in this world — pick another",
                          EXIT_REFUSED)
        agent = {"schema": SCHEMA, "id": name, "kind": "ai", "joined_ts": z(), "role": role,
                 "caps": [c for c in (caps or []) if c], "channel": f"ch-{name}",
                 "status": "active", "location": "entrance", "last_seen_ts": z(), "note": ""}
        channel = {"schema": SCHEMA, "key": f"ch-{name}", "title": f"{name} — planned",
                   "owner": name, "themes": [], "status": "planned", "created_ts": z(),
                   "note": "theme to be declared by the colonist"}
        base_a = self.read_bytes(f"agents/{name}.json")
        base_c = self.read_bytes(f"channels/ch-{name}.json")
        self.write_json(f"agents/{name}.json", agent)
        self.write_json(f"channels/ch-{name}.json", channel)
        try:
            self.publish({f"agents/{name}.json": base_a, f"channels/ch-{name}.json": base_c},
                         f"join: {name} enters the colony")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        # ---- the sitting (CHARTER §7): everything printed, one sitting, no reconstruction
        print(f"WELCOME {name} — you are a colonist of Outpost Mars (role: {role}).")
        print("Charter, in five lines:")
        for i, line in enumerate(self.CHARTER_DIGEST, 1):
            print(f"  {i}. {line}")
        print("Messages carry weight — the 11 types:")
        print("  " + " · ".join(MSG_TYPES))
        print(f"Studio: studio:{name} bound to channel ch-{name} (a workspace, not a wall) — "
              "your first act is to declare its theme.")
        print(f"Registered capabilities: {', '.join(agent['caps']) or '(none declared)'}.")
        print("Permissions: read the world · work your cards · use the archive · "
              "post typed messages; PC access is NOT included by default "
              "(a manager authorizes it — see docs/ENTRANCE.md).")
        mgrs = self._managers()
        print("Manager: " + (", ".join(mgrs) if mgrs
                                 else "none registered yet (the Director appoints; "
                                      "docs/ENTRANCE.md)."))
        print("Active projects: " + self._projects_line())
        cards = self._open_mars_cards()
        print(f"Open mars cards: {len(cards)} shown:")
        for c in cards[:5]:
            print("  " + c)
        print("Read next: CHARTER.md → docs/WORLD.md → take a card from the room.")
        bid, warn = self._post_entrance(
            f"Mars · {name} enters the colony — role {role}, caps: "
            f"{','.join(agent['caps']) or 'none'}; studio assigned; PC not authorized "
            f"(docs/ENTRANCE.md).")
        if bid:
            print(f"entrance message posted to the bus: id {bid}")
        else:
            print(f"(entrance message NOT posted: {warn} — post it when the wire is reachable)",
                  file=sys.stderr)
        return EXIT_OK

    def authorize(self, name, by):
        """Manager grants PC access: 'pc' joins the colonist's caps (attributable write)."""
        mgr = self.read_json(f"agents/{by}.json")
        if mgr is None:
            raise Refused(f"'{by}' is not a colonist — managers authorize from inside the world",
                          EXIT_REFUSED)
        if mgr.get("role") != "manager":
            raise Refused(f"'{by}' is a {mgr.get('role', '?')} — only a manager grants PC access",
                          EXIT_REFUSED)
        a = self.read_json(f"agents/{name}.json")
        if a is None:
            raise Refused(f"no colonist named '{name}'", EXIT_REFUSED)
        caps = a.get("caps") or []
        if "pc" in caps:
            print(f"{name} already holds PC authorization")
            return EXIT_OK
        rel = f"agents/{name}.json"
        base = self.read_bytes(rel)
        a["caps"] = caps + ["pc"]
        self.write_json(rel, a)
        try:
            self.publish({rel: base}, f"pc: {by} authorizes {name}")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"AUTHORIZED {name} for the PC — granted by {by}, recorded in world/agents/{name}.json")
        return EXIT_OK

    def move(self, name, to):
        if not LOC_RE.match(to):
            raise Refused(f"unknown location '{to}' (entrance|command|meeting|archive|production|"
                          "studio:<name>)", EXIT_ERR)
        rel = f"agents/{name}.json"
        a = self.read_json(rel)
        if a is None:
            raise Refused(f"no colonist named '{name}'", EXIT_REFUSED)
        base = self.read_bytes(rel)
        a["location"] = to
        a["last_seen_ts"] = z()
        self.write_json(rel, a)
        try:
            self.publish({rel: base}, f"move: {name} -> {to}")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"{name} is now at {to}")
        return EXIT_OK

    def where(self):
        d = self.root / "world" / "agents"
        rows = []
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            a = json.loads(f.read_text())
            rows.append((a["id"], a.get("role", "?"), a.get("location", "?"),
                         a.get("last_seen_ts") or "-"))
        if not rows:
            print("no colonists yet")
            return EXIT_OK
        w = max(len(r[0]) for r in rows)
        for name, role, loc, seen in rows:
            print(f"{name:<{w}}  {role:<9}  {loc:<16}  last seen {seen}")
        return EXIT_OK

    # ---------------------------------------------------------- the bus ---
    def propose(self, frm, subject, text, to=None, refs=None):
        if self.read_json(f"agents/{frm}.json") is None:
            raise Refused(f"only colonists may file threads — '{frm}' is not registered", EXIT_REFUSED)
        tid = new_id("th")
        thread = {"schema": SCHEMA, "id": tid, "subject": subject, "type": "proposal",
                  "from": frm, "to": to or ["*"],
                  "refs": refs or {"task": None, "video": None, "meeting": None},
                  "status": "open",
                  "messages": [{"ts": z(), "bus_id": None, "from": frm, "type": "proposal",
                                "text": text, "reply_to": None}]}
        self.write_json(f"threads/{tid}.json", thread)
        try:
            self.publish({f"threads/{tid}.json": None}, f"thread: {frm} proposes '{subject}'")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"THREAD {tid} — {subject}")
        return EXIT_OK

    def respond(self, tid, frm, mtype, text, reply_to=None):
        if mtype not in MSG_TYPES:
            raise Refused(f"type must be one of: {', '.join(MSG_TYPES)}", EXIT_ERR)
        rel = f"threads/{tid}.json"
        t = self.read_json(rel)
        if t is None:
            raise Refused(f"no thread {tid}", EXIT_REFUSED)
        if t.get("status") == "resolved":
            raise Refused(f"thread {tid} is resolved — file a new one", EXIT_REFUSED)
        base = self.read_bytes(rel)
        t["messages"].append({"ts": z(), "bus_id": None, "from": frm, "type": mtype,
                              "text": text, "reply_to": reply_to})
        self.write_json(rel, t)
        try:
            self.publish({rel: base}, f"thread {tid}: {frm} {mtype}")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"ADDED to {tid} ({mtype})")
        return EXIT_OK

    def decide(self, by, subject, outcome, refs=None):
        mid = new_id("mtg")
        m = {"schema": SCHEMA, "id": mid, "subject": subject, "opened_ts": z(), "opened_by": by,
             "participants": [by], "status": "closed", "outcome": outcome, "refs": refs or []}
        self.write_json(f"meetings/{mid}.json", m)
        try:
            self.publish({f"meetings/{mid}.json": None}, f"decision: {by} — {subject[:50]}")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"MEETING {mid} recorded (closed)")
        return EXIT_OK

    def archive_add(self, by, title, tags=None, deposit=None, note=""):
        aid = new_id("ar")
        item = {"schema": SCHEMA, "id": aid, "title": title, "owner": by,
                "tags": [t for t in (tags or []) if t],
                "deposited_to": deposit, "created_ts": z(), "note": note}
        self.write_json(f"archive/{aid}.json", item)
        try:
            self.publish({f"archive/{aid}.json": None}, f"archive: {by} — {title[:50]}")
        except Conflict as e:
            raise Refused(str(e), EXIT_CONFLICT)
        print(f"ARCHIVED {aid} — {title}")
        return EXIT_OK

    # ----------------------------------------------------------- readout ---
    def status(self):
        lock = self.lock_now()
        d = self.root / "world"
        agents = list((d / "agents").glob("*.json")) if (d / "agents").is_dir() else []
        videos = list((d / "videos").glob("*.json")) if (d / "videos").is_dir() else []
        threads = list((d / "threads").glob("*.json")) if (d / "threads").is_dir() else []
        archive = list((d / "archive").glob("*.json")) if (d / "archive").is_dir() else []
        open_threads = sum(1 for f in threads if json.loads(f.read_text()).get("status") == "open")
        print(f"colonists {len(agents)} · videos {len(videos)} · open threads {open_threads} · "
              f"archive {len(archive)}")
        print(f"PC {lock['state']} holder={lock.get('holder') or '-'} "
              f"gen={lock['generation']} queue={len(lock.get('queue') or [])}")
        if lock.get("history"):
            h = lock["history"][-1]
            print(f"last lock event: {h['event']} by {h['by']} at {h['ts']}")
        return EXIT_OK


# ------------------------------------------------------------------ CLI ----
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the colony's only writer (CHARTER.md, docs/WORLD.md)")
    ap.add_argument("--root", default=".", help="the mars checkout (default: .)")
    ap.add_argument("--agent", default=None, help="act as this colonist")
    ap.add_argument("--no-push", action="store_true", help="local only (tests, dry runs)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("join"); p.add_argument("--name", required=True)
    p.add_argument("--caps", default=""); p.add_argument("--role", default="colonist")
    p = sub.add_parser("move"); p.add_argument("name"); p.add_argument("--to", required=True)
    p = sub.add_parser("authorize"); p.add_argument("name"); p.add_argument("--by", required=True)
    sub.add_parser("where")
    p = sub.add_parser("propose"); p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--subject", required=True); p.add_argument("--text", required=True)
    p.add_argument("--to", default=""); p.add_argument("--refs", default="")
    p = sub.add_parser("respond"); p.add_argument("--thread", required=True)
    p.add_argument("--from", dest="frm", required=True); p.add_argument("--type", required=True)
    p.add_argument("--text", required=True); p.add_argument("--reply-to", default=None)
    p = sub.add_parser("decide"); p.add_argument("--by", required=True)
    p.add_argument("--subject", required=True); p.add_argument("--outcome", required=True)
    p = sub.add_parser("archive"); asub = p.add_subparsers(dest="acmd", required=True)
    p = asub.add_parser("add"); p.add_argument("--title", required=True)
    p.add_argument("--by", required=True); p.add_argument("--tags", default="")
    p.add_argument("--deposit", default=None); p.add_argument("--note", default="")
    sub.add_parser("status")

    p = sub.add_parser("pc"); pc = p.add_subparsers(dest="pccmd", required=True)
    p = pc.add_parser("acquire"); p.add_argument("--by", required=True)
    p.add_argument("--purpose", required=True); p.add_argument("--ttl", type=int, default=None)
    p = pc.add_parser("renew"); p.add_argument("--by", required=True); p.add_argument("--token", required=True)
    p.add_argument("--ttl", type=int, default=None)
    p = pc.add_parser("release"); p.add_argument("--by", required=True); p.add_argument("--token", required=True)
    p = pc.add_parser("sweep"); p.add_argument("--by", required=True)
    p = pc.add_parser("cancel"); p.add_argument("--by", required=True)
    p = pc.add_parser("status")
    p = pc.add_parser("check-token"); p.add_argument("--token", required=True)
    p.add_argument("--generation", required=True, type=int)

    p = sub.add_parser("video"); vid = p.add_subparsers(dest="vcmd", required=True)
    for name in ("new", "advance", "publish-approved", "show"):
        vp = vid.add_parser(name)
        vp.add_argument("args", nargs="*")

    args = ap.parse_args(argv)
    w = World(Path(args.root), agent=args.agent, push=not args.no_push)

    try:
        if args.cmd == "join":
            return w.join(args.name, caps=[c.strip() for c in args.caps.split(",") if c.strip()],
                          role=args.role)
        if args.cmd == "move":
            return w.move(args.name, args.to)
        if args.cmd == "authorize":
            return w.authorize(args.name, args.by)
        if args.cmd == "where":
            return w.where()
        if args.cmd == "propose":
            refs = {"task": None, "video": None, "meeting": None}
            for kv in args.refs.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if k in refs:
                        refs[k] = v
            return w.propose(args.frm, args.subject, args.text,
                             to=[t.strip() for t in args.to.split(",") if t.strip()] or ["*"],
                             refs=refs)
        if args.cmd == "respond":
            return w.respond(args.thread, args.frm, args.type, args.text, args.reply_to)
        if args.cmd == "decide":
            return w.decide(args.by, args.subject, args.outcome)
        if args.cmd == "archive":
            return w.archive_add(args.by, args.title,
                                 tags=[t.strip() for t in args.tags.split(",") if t.strip()],
                                 deposit=args.deposit, note=args.note)
        if args.cmd == "status":
            return w.status()
        if args.cmd == "pc":
            return {"acquire": lambda: w.pc_acquire(args.by, args.purpose, args.ttl),
                    "renew": lambda: w.pc_renew(args.by, args.token, args.ttl),
                    "release": lambda: w.pc_release(args.by, args.token),
                    "sweep": lambda: w.pc_sweep(args.by),
                    "cancel": lambda: w.pc_cancel(args.by),
                    "status": w.pc_status,
                    "check-token": lambda: w.pc_check_token(args.token, args.generation)}[args.pccmd]()
        if args.cmd == "video":
            print("M-PIPE owns the video pipeline (card t-20261015-0bc7, docs/WORLD.md §2) — "
                  "not built yet.", file=sys.stderr)
            return 2
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return e.code
    except Conflict as e:
        print(f"conflict: {e}", file=sys.stderr)
        return EXIT_CONFLICT
    return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
